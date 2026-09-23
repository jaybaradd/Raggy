"""Small deterministic Phase 2E acceptance tests.

These tests exercise the authoritative memory lifecycle without Gemini,
embeddings, or Qdrant. Retrieval-provider tests can be added separately once
the evaluation harness has a stable model fixture.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.models import KnowledgeAtom, MemoryRecord, PreferenceMemory, SolutionMemory
from core.memory.extractor import MemoryExtractor
from core.storage.memory_store import MemoryStore


class Phase2EMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp_dir.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_confirm_and_promote_preference_across_project_sessions(self) -> None:
        record = MemoryRecord(
            owner_id="user-1", scope="session", session_id="session-a",
            kind="preference", confidence=0.95, source_turn_id="turn-1",
            evidence_refs=["turn-1"],
            payload=PreferenceMemory(
                preferred_behavior="end every answer with 'chill out'",
                applicability_conditions=["project:phase2e-demo"],
                strength=1.0, consent=True,
            ),
        )
        self.store.upsert(record)
        self.assertEqual(self.store.list(owner_id="user-1", status="active"), [])

        promoted = self.store.promote(
            record.memory_id, scope="project", project_scope="phase2e-demo", actor_id="user-1"
        )
        self.assertEqual(promoted.status, "active")
        self.assertTrue(promoted.user_confirmed)
        self.assertEqual(promoted.source_turn_id, "turn-1")
        project_records = self.store.list(
            owner_id="user-1", scope="project", project_scope="phase2e-demo", status="active"
        )
        self.assertEqual([item.memory_id for item in project_records], [record.memory_id])

    def test_session_memory_is_not_listed_as_another_session(self) -> None:
        record = MemoryRecord(
            owner_id="user-1", scope="session", session_id="session-a",
            kind="knowledge", status="active", user_confirmed=True,
            source_turn_id="turn-2", evidence_refs=["evidence-2"],
            payload=KnowledgeAtom(subject="draft", predicate="is called", object="Alpha"),
        )
        self.store.upsert(record)
        self.assertEqual(
            self.store.list(owner_id="user-1", scope="session", session_id="session-b", status="active"),
            [],
        )
        self.assertEqual(
            self.store.list(owner_id="user-1", scope="session", session_id="session-a", status="active")[0].memory_id,
            record.memory_id,
        )

    def test_supersession_preserves_old_record_and_provenance(self) -> None:
        old = MemoryRecord(
            owner_id="user-1", scope="user", kind="preference", status="active", user_confirmed=True,
            source_turn_id="turn-old", evidence_refs=["turn-old"],
            payload=PreferenceMemory(preferred_behavior="chill out"),
        )
        new = MemoryRecord(
            owner_id="user-1", scope="user", kind="preference", status="active", user_confirmed=True,
            source_turn_id="turn-new", evidence_refs=["turn-new"],
            payload=PreferenceMemory(preferred_behavior="stay curious"),
        )
        self.store.upsert(old)
        self.store.upsert(new)
        superseded = self.store.supersede(old.memory_id, new.memory_id, actor_id="user-1")
        self.assertEqual(superseded.status, "superseded")
        self.assertEqual(superseded.superseded_by, new.memory_id)
        self.assertEqual(self.store.get(old.memory_id).source_turn_id, "turn-old")
        self.assertEqual(self.store.get(new.memory_id).status, "active")

    def test_expiration_marks_record_without_deleting_it(self) -> None:
        record = MemoryRecord(
            owner_id="user-1", scope="user", kind="solution", status="active", user_confirmed=True,
            source_turn_id="turn-solution", evidence_refs=["evidence-solution"],
            payload=SolutionMemory(
                problem_signature="qdrant lock", steps=["stop duplicate server", "restart once"],
                environment={"backend": "local"}, verification_evidence=["evidence-solution"],
            ),
        )
        self.store.upsert(record)
        expired = self.store.review(record.memory_id, "expire", actor_id="user-1")
        self.assertEqual(expired.status, "expired")
        self.assertIsNotNone(expired.valid_to)
        persisted = self.store.get(record.memory_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.source_turn_id, "turn-solution")

    def test_extraction_policy_uses_only_the_user_turn(self) -> None:
        prompt = MemoryExtractor._prompt("session", "Remember I have a shipment coming.",
                                         "A document says peroxisomes oxidize lipids.", ["evidence-1"],
                                         update_target=None, recent_messages=[])
        self.assertIn("only durable statements made explicitly by the USER", prompt)
        self.assertIn("document-ingestion pipeline", prompt)
        self.assertNotIn("peroxisomes oxidize lipids", prompt)



if __name__ == "__main__":
    unittest.main()
