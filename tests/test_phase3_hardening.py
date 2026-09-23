"""Fast, deterministic regression coverage for Phase 3 hardening contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.ingestion.models import AssetRecord
from core.memory.models import KnowledgeAtom, MemoryRecord
from core.storage.evidence_store import EvidenceStore
from core.storage.memory_store import MemoryStore


class MemoryProcessingStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_status_is_durable_and_turn_records_are_filterable(self) -> None:
        self.assertTrue(self.store.claim_extraction("assistant-turn", "phase3-test"))
        self.assertEqual(
            self.store.get_extraction_status("assistant-turn", "phase3-test")["status"], "running",
        )
        record = MemoryRecord(
            memory_id="memory-from-turn", owner_id="owner-a", scope="project", project_id="project-a",
            project_scope="Project A", kind="knowledge", status="active", user_confirmed=True,
            source_turn_id="assistant-turn", payload=KnowledgeAtom(subject="system", predicate="uses", object="memory"),
        )
        self.store.upsert(record)
        self.store.complete_extraction("assistant-turn", "phase3-test", status="completed")
        self.assertEqual(
            [item.memory_id for item in self.store.list(owner_id="owner-a", source_turn_id="assistant-turn", status=None)],
            ["memory-from-turn"],
        )
        self.assertEqual(
            self.store.get_extraction_status("assistant-turn", "phase3-test")["status"], "completed",
        )

    def test_failed_extraction_retains_a_safe_error_for_operations(self) -> None:
        self.store.claim_extraction("assistant-turn", "phase3-test")
        self.store.complete_extraction("assistant-turn", "phase3-test", status="failed", error="provider unavailable")
        status = self.store.get_extraction_status("assistant-turn", "phase3-test")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "provider unavailable")


class IngestionRetryPrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = EvidenceStore(str(Path(self.temp.name) / "evidence.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_failed_run_keeps_immutable_asset_for_a_fresh_retry_run(self) -> None:
        asset = AssetRecord(
            asset_id="video-a", filename="https://example.test/video", media_type="video/x-youtube",
            raw_file_uri="https://example.test/video", content_hash="video-a",
        )
        self.store.upsert_asset(asset)
        first = self.store.start_ingestion("video-a", modality="youtube")
        self.store.complete_ingestion(first["run_id"], chunk_count=0, error="temporary network failure")
        saved = self.store.get_asset("video-a")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.raw_file_uri, "https://example.test/video")

        retry = self.store.start_ingestion("video-a", modality="youtube")
        self.assertNotEqual(retry["run_id"], first["run_id"])
        self.assertEqual(self.store.get_document_status("video-a")["status"], "processing")


if __name__ == "__main__":
    unittest.main()
