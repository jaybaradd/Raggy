"""Opt-in Phase 2E test against a running Raggy API.

Run with the server already started (without reload):

    RUN_PHASE2E_E2E=1 python3 -m unittest tests.test_phase2e_api_e2e -v

This intentionally uses real HTTP calls, the configured memory SQLite store,
the real memory projection, and the real Qdrant retrieval path. It is opt-in
because it calls the configured LLM provider and requires a running server.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from urllib.request import Request, urlopen

from config import settings


BASE_URL = os.getenv("RAGGY_API_URL", "http://127.0.0.1:8000").rstrip("/")
MEMORY_DB = str(settings.memory_db_path)


def _request(method: str, path: str, payload: dict | None = None, *, headers: dict | None = None) -> tuple[int, bytes]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{BASE_URL}{path}", data=body, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urlopen(request, timeout=180) as response:
        return response.status, response.read()


class Phase2EApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.getenv("RUN_PHASE2E_E2E") != "1":
            raise unittest.SkipTest("Set RUN_PHASE2E_E2E=1 to call the running API")

    def _create_session(self, project: str) -> str:
        status, raw = _request("POST", "/api/sessions", {"title": "Phase 2E test", "project_scope": project})
        self.assertEqual(status, 201)
        return json.loads(raw)["session_id"]

    def _send(self, session_id: str, content: str) -> str:
        status, raw = _request("POST", f"/api/sessions/{session_id}/messages", {"content": content})
        self.assertEqual(status, 200)
        return raw.decode("utf-8", errors="replace")

    def test_preference_promotes_and_retrieves_across_sessions(self) -> None:
        project = f"phase2e-{int(time.time())}"
        phrase = f"phase two says hello {project}"
        first_session = self._create_session(project)
        self._send(
            first_session,
            f"For this project, start every answer with the phrase '{phrase}'. Remember this preference.",
        )

        candidate = None
        for _ in range(30):
            _, raw = _request("GET", "/api/memories/candidates")
            candidates = json.loads(raw)["memories"]
            candidate = next(
                (
                    item for item in candidates
                    if item.get("kind") == "preference"
                    and phrase in item.get("payload", {}).get("preferred_behavior", "").lower()
                ),
                None,
            )
            if candidate:
                break
            time.sleep(2)
        self.assertIsNotNone(candidate, "memory extraction did not produce the preference candidate")
        memory_id = candidate["memory_id"]
        self.assertEqual(candidate["scope"], "session")
        self.assertEqual(candidate["status"], "candidate")

        status, raw = _request("POST", f"/api/memories/{memory_id}/confirm")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "active")
        status, raw = _request(
            "POST", f"/api/memories/{memory_id}/promote",
            {"scope": "project", "project_scope": project},
        )
        self.assertEqual(status, 200)
        promoted = json.loads(raw)
        self.assertEqual(promoted["scope"], "project")
        self.assertEqual(promoted["project_scope"], project)

        second_session = self._create_session(project)
        sse = self._send(second_session, "Please answer a project question about our next milestone.")
        self.assertIn("event: memories", sse)
        self.assertIn(memory_id, sse)

        with sqlite3.connect(MEMORY_DB) as connection:
            events = connection.execute(
                "SELECT event_type FROM memory_access_events WHERE memory_id = ?",
                (memory_id,),
            ).fetchall()
        self.assertIn(("retrieved",), events)
        self.assertIn(("injected",), events)

        other_project_session = self._create_session(f"other-{project}")
        isolated_sse = self._send(other_project_session, "Please answer a project question about our next milestone.")
        self.assertNotIn(memory_id, isolated_sse)


if __name__ == "__main__":
    unittest.main()
