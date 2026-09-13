"""Attachment metadata is validated, persisted, and restart-safe."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from api.schemas import AttachmentMetadata, SendMessageRequest
from db.session_store import SessionStore


ATTACHMENT = {
    "attachment_id": "c2b6f0f8-c632-4891-932c-172a3517c2e7",
    "filename": "arrival-plan.pdf",
    "mime_type": "application/pdf",
    "size_bytes": 2048,
    "source_mode": "knowledge_base",
    "document_id": "document-123",
    "evidence_id": "evidence-456",
}


def _message_router_without_runtime_services():
    """Load the HTTP handler without booting embeddings or an LLM client."""
    engine = types.ModuleType("core.retrieval.engine")
    engine.RAG_SYSTEM_PROMPT = "test"
    engine.RetrievalResult = lambda context, chunks: types.SimpleNamespace(context=context, chunks=chunks)
    engine.build_rag_prompt = lambda content, _context: content
    engine.retrieve = lambda query: engine.RetrievalResult("", [])
    memory_result = types.SimpleNamespace(context="", memories=[])
    retrieval_memory = types.ModuleType("core.retrieval.memory")
    retrieval_memory.retrieve_memories = lambda *args, **kwargs: memory_result
    stubs = {
        "core.llm.gemini": types.SimpleNamespace(gemini_provider=object()),
        "core.memory.extractor": types.SimpleNamespace(MemoryExtractor=object),
        "core.memory.jobs": types.SimpleNamespace(extract_turn_memories=object()),
        "core.storage.memory_store": types.SimpleNamespace(memory_store=object()),
        "core.retrieval.engine": engine,
        "core.retrieval.memory": retrieval_memory,
    }
    sys.modules.pop("api.routers.messages", None)
    with patch.dict(sys.modules, stubs):
        return importlib.import_module("api.routers.messages")


class AttachmentMetadataTests(unittest.TestCase):
    def test_contract_rejects_invalid_or_unknown_metadata(self) -> None:
        with self.assertRaises(ValidationError):
            AttachmentMetadata(**{**ATTACHMENT, "mime_type": "not-a-mime-type"})
        with self.assertRaises(ValidationError):
            AttachmentMetadata(**{**ATTACHMENT, "unexpected": "field"})

    @unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed in this Python environment")
    def test_message_endpoint_persists_api_attachment_metadata(self) -> None:
        """The POST handler stores the validated body before response streaming."""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SessionStore(str(Path(temp_dir) / "sessions.sqlite3"))
            session = store.create_session()
            request = SendMessageRequest(content="Summarise this", attachments=[ATTACHMENT])

            router = _message_router_without_runtime_services()
            with patch.object(router, "session_store", store):
                asyncio.run(router.send_message(session["session_id"], request))

            persisted = store.get_messages(session["session_id"])
            self.assertEqual(persisted[0]["attachments"], [ATTACHMENT])

    def test_metadata_survives_repository_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "sessions.sqlite3")
            store = SessionStore(db_path)
            session = store.create_session()
            store.append_message(session["session_id"], "user", "Review this", attachments=[ATTACHMENT])

            restarted_store = SessionStore(db_path)
            restored = restarted_store.get_messages(session["session_id"])

            self.assertEqual(restored[0]["attachments"], [ATTACHMENT])


if __name__ == "__main__":
    unittest.main()
