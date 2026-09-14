"""Opt-in black-box Postgres restart test through a real Uvicorn process.

Run with ``RUN_POSTGRES_API_E2E=1`` and ``POSTGRES_DATABASE_URL`` set.  It
creates an isolated Postgres schema plus temporary upload, graph, and Qdrant
directories; it never reads or writes the developer's normal application data.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


RUN = os.getenv("RUN_POSTGRES_API_E2E") == "1"
DATABASE_URL = os.getenv("POSTGRES_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[1]


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _json_request(base_url: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    raw = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{base_url}{path}", data=raw, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read())


def _stream_request(base_url: str, path: str, payload: dict) -> tuple[int, str]:
    request = Request(
        f"{base_url}{path}", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        return response.status, response.read().decode("utf-8")


def _multipart_upload(base_url: str, filename: str, content: bytes) -> tuple[int, dict]:
    boundary = f"raggy-e2e-{uuid.uuid4().hex}"
    body = b"".join((
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: text/csv\r\n\r\n",
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ))
    request = Request(
        f"{base_url}/api/documents", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read())


@unittest.skipUnless(
    RUN and DATABASE_URL and importlib.util.find_spec("psycopg"),
    "set RUN_POSTGRES_API_E2E=1 and POSTGRES_DATABASE_URL",
)
class PostgresApiE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        self.schema = f"raggy_api_e2e_{uuid.uuid4().hex}"
        self.temp = tempfile.TemporaryDirectory()
        self.port = _unused_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[str] | None = None
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')

    def tearDown(self) -> None:
        import psycopg

        self._stop_server()
        if re.fullmatch(r"raggy_api_e2e_[0-9a-f]+", self.schema):
            with psycopg.connect(DATABASE_URL, autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.temp.cleanup()

    def _server_environment(self) -> dict[str, str]:
        directory = Path(self.temp.name)
        environment = os.environ.copy()
        environment.update({
            "AUTHORITATIVE_DB_BACKEND": "postgres",
            "POSTGRES_DATABASE_URL": DATABASE_URL,
            "POSTGRES_SCHEMA": self.schema,
            "UPLOAD_DIR": str(directory / "uploads"),
            "GRAPH_DB_PATH": str(directory / "graph.sqlite3"),
            "QDRANT_URL": str(directory / "qdrant"),
            "QDRANT_COLLECTION": f"evidence_{self.schema}",
            "MEMORY_COLLECTION": f"memory_{self.schema}",
            "MEMORY_EXPIRY_SWEEP_INTERVAL_SECONDS": "3600",
            "PYTHONPATH": str(ROOT),
            # This test must use the locally cached models; it must not turn a
            # persistence check into an implicit model download.
            "HF_HUB_OFFLINE": "1",
        })
        return environment

    def _start_server(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tests.api_e2e_app:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT,
            env=self._server_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 180
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                self.fail(f"Uvicorn exited during startup:\n{output}")
            try:
                status, payload = _json_request(self.base_url, "GET", "/api/health")
                if status == 200 and payload["status"] == "ok":
                    return
            except (URLError, TimeoutError, ConnectionError) as error:
                last_error = error
            time.sleep(0.5)
        self.fail(f"Uvicorn did not become healthy: {last_error}")

    def _stop_server(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.process = None

    def _wait_for_document(self, doc_id: str) -> dict:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            _, status = _json_request(self.base_url, "GET", f"/api/documents/{doc_id}/status")
            if status["status"] in {"done", "failed"}:
                return status
            time.sleep(0.5)
        self.fail("Document ingestion did not finish")

    def test_http_state_survives_a_real_server_restart(self) -> None:
        self._start_server()
        status, project = _json_request(self.base_url, "POST", "/api/projects", {"name": "API E2E Imports"})
        self.assertEqual(status, 201)
        status, session = _json_request(
            self.base_url, "POST", "/api/sessions",
            {"title": "Restart-safe chat", "project_id": project["project_id"]},
        )
        self.assertEqual(status, 201)

        status, stream = _stream_request(
            self.base_url, f"/api/sessions/{session['session_id']}/messages",
            {"content": "Persist this turn", "use_knowledge_base": False},
        )
        self.assertEqual(status, 200)
        self.assertIn("[DONE]", stream)
        _, before_restart = _json_request(self.base_url, "GET", f"/api/sessions/{session['session_id']}/messages")
        self.assertEqual([message["role"] for message in before_restart["messages"]], ["user", "assistant"])
        self.assertIn("Test reply", before_restart["messages"][1]["content"])

        status, document = _multipart_upload(
            self.base_url, "arrivals.csv", b"shipment,arrival\nAC-42,Friday\n"
        )
        self.assertEqual(status, 202)
        document_status = self._wait_for_document(document["doc_id"])
        self.assertEqual(document_status["status"], "done", document_status)

        self._stop_server()
        self._start_server()
        _, after_restart = _json_request(self.base_url, "GET", f"/api/sessions/{session['session_id']}/messages")
        self.assertEqual(after_restart["messages"], before_restart["messages"])
        _, reloaded_document = _json_request(self.base_url, "GET", f"/api/documents/{document['doc_id']}/status")
        self.assertEqual(reloaded_document["status"], "done")
        self.assertEqual(reloaded_document["chunk_count"], document_status["chunk_count"])
        with urlopen(f"{self.base_url}/api/documents/{document['doc_id']}/source", timeout=30) as response:
            self.assertEqual(response.read(), b"shipment,arrival\nAC-42,Friday\n")


if __name__ == "__main__":
    unittest.main()
