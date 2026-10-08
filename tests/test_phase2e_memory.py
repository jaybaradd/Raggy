"""Small deterministic Phase 2E acceptance tests.

These tests exercise the authoritative memory lifecycle without Gemini,
embeddings, or Qdrant. Retrieval-provider tests can be added separately once
the evaluation harness has a stable model fixture.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.models import (EventMemory, IdentifierReference, KnowledgeAtom, MemoryRecord,
                                PreferenceMemory, SolutionMemory)
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

    def test_edit_creates_a_replacement_without_mutating_history(self) -> None:
        original = MemoryRecord(
            owner_id="user-1", scope="user", kind="preference", status="active",
            user_confirmed=True, source_turn_id="turn-original", evidence_refs=["turn-original"],
            payload=PreferenceMemory(preferred_behavior="Use short answers"),
        )
        self.store.upsert(original)

        replacement = self.store.edit(
            original.memory_id,
            {"preferred_behavior": "Use detailed answers", "applicability_conditions": [],
             "strength": 0.5, "consent": False},
            actor_id="user-1",
        )

        persisted_original = self.store.get(original.memory_id)
        self.assertNotEqual(replacement.memory_id, original.memory_id)
        self.assertEqual(persisted_original.status, "superseded")
        self.assertEqual(persisted_original.superseded_by, replacement.memory_id)
        self.assertEqual(persisted_original.payload.preferred_behavior, "Use short answers")
        self.assertEqual(replacement.status, "active")
        self.assertEqual(replacement.payload.preferred_behavior, "Use detailed answers")
        self.assertEqual(replacement.source_turn_id, "turn-original")
        relationship = self.store.list_relationships(original.memory_id, owner_id="user-1")[0]
        self.assertEqual(relationship["relationship_type"], "superseded_by")
        self.assertEqual(relationship["source"], "manual_edit")

    def test_superseded_memory_cannot_be_edited_again(self) -> None:
        original = MemoryRecord(
            owner_id="user-1", scope="user", kind="preference", status="active",
            user_confirmed=True,
            payload=PreferenceMemory(preferred_behavior="Use short answers"),
        )
        self.store.upsert(original)
        self.store.edit(
            original.memory_id,
            {"preferred_behavior": "Use detailed answers", "applicability_conditions": [],
             "strength": 0.5, "consent": False},
            actor_id="user-1",
        )
        with self.assertRaisesRegex(ValueError, "Cannot edit a superseded memory"):
            self.store.edit(
                original.memory_id,
                {"preferred_behavior": "Use examples", "applicability_conditions": [],
                 "strength": 0.5, "consent": False},
                actor_id="user-1",
            )

    def test_event_edit_preserves_old_identifier_as_a_subject_alias(self) -> None:
        original = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="imports", kind="event",
            status="active", user_confirmed=True,
            payload=EventMemory(
                event_type="shipment", summary="AC-42 arrives Tuesday", entities=["AC-42"],
                identifier_references=[IdentifierReference(value="AC-42")],
            ),
        )
        self.store.upsert(original)
        replacement = self.store.edit(
            original.memory_id,
            {"event_type": "shipment", "summary": "Bill BL-900 arrives Thursday",
             "entities": ["BL-900"], "locations": [], "temporal_scope": "Thursday",
             "identifier_references": [{"value": "BL-900"}], "claims": []},
            actor_id="user-1",
        )
        self.assertEqual(replacement.subject_id, original.subject_id)
        for identifier in ("AC-42", "BL-900"):
            matches = self.store.find_memory_candidates(
                owner_id="user-1", session_id="unused", project_id=None,
                project_scope="imports", identifier_references=[IdentifierReference(value=identifier)],
            )
            self.assertEqual([record.memory_id for record in matches], [replacement.memory_id])
        self.assertEqual(
            [record.memory_id for record in self.store.list_subject_records(
                replacement.subject_id, owner_id="user-1",
            )],
            [original.memory_id, replacement.memory_id],
        )

    def test_same_identifier_in_different_projects_has_different_subjects(self) -> None:
        first = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="imports", kind="event",
            status="active", user_confirmed=True,
            payload=EventMemory(event_type="shipment", summary="AC-42 in imports",
                                identifier_references=[IdentifierReference(value="AC-42")]),
        )
        second = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="exports", kind="event",
            status="active", user_confirmed=True,
            payload=EventMemory(event_type="shipment", summary="AC-42 in exports",
                                identifier_references=[IdentifierReference(value="AC-42")]),
        )
        self.store.upsert(first)
        self.store.upsert(second)
        self.assertIsNotNone(first.subject_id)
        self.assertIsNotNone(second.subject_id)
        self.assertNotEqual(first.subject_id, second.subject_id)

    def test_subject_survives_project_id_stabilization(self) -> None:
        record = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="imports", kind="event",
            status="active", user_confirmed=True,
            payload=EventMemory(event_type="shipment", summary="AC-42 in imports",
                                identifier_references=[IdentifierReference(value="AC-42")]),
        )
        self.store.upsert(record)
        subject_id = record.subject_id
        record.project_id = "imports-id"
        self.store.upsert(record, event_type="project_id_assigned")
        self.assertEqual(record.subject_id, subject_id)
        matches = self.store.find_memory_candidates(
            owner_id="user-1", session_id="unused", project_id="imports-id",
            project_scope="imports", identifier_references=[IdentifierReference(value="AC-42")],
        )
        self.assertEqual([item.memory_id for item in matches], [record.memory_id])

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
