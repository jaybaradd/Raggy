"""Deterministic repository tests for event deduplication and conflicts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.models import EventMemory, MemoryRecord
from core.storage.memory_store import MemoryStore


class EventCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp_dir.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _shipment(self, *, project: str = "imports", temporal: str = "Friday", session: str = "session-a",
                  event_type: str = "shipment arrival", entities: list[str] | None = None) -> MemoryRecord:
        return MemoryRecord(
            owner_id="user-1", scope="project", project_scope=project, kind="event",
            session_id=session, status="active", user_confirmed=True, source_turn_id=f"turn-{temporal}",
            payload=EventMemory(event_type=event_type, summary="Cargo shipment is arriving",
                                entities=entities or ["Shipment AC-42", "Tokyo supplier"], locations=["Tokyo"],
                                temporal_scope=temporal),
        )

    def test_identical_event_is_audited_without_creating_a_second_memory(self) -> None:
        first = self.store.capture_event(self._shipment(), event_type="auto_promoted_project_event")
        duplicate = self.store.capture_event(
            self._shipment(session="session-b", event_type="shipment_arrival", entities=["AC-42"]),
            event_type="auto_promoted_project_event",
        )

        self.assertEqual(first.outcome, "created")
        self.assertEqual(duplicate.outcome, "duplicate")
        self.assertEqual(duplicate.record.memory_id, first.record.memory_id)
        self.assertEqual(first.record.payload.event_type, "shipment_arrival")
        self.assertEqual(first.record.payload.event_type_raw, "shipment arrival")
        self.assertEqual(len(self.store.list(owner_id="user-1", project_scope="imports", kind="event", status=None)), 1)

    def test_changed_event_time_is_preserved_as_an_open_conflict(self) -> None:
        existing = self.store.capture_event(self._shipment(temporal="Friday", event_type="shipment arrival",
                                                           entities=["AC-42", "imports project"]), event_type="auto_promoted_project_event")
        conflict = self.store.capture_event(self._shipment(temporal="Monday", event_type="shipment_arrival",
                                                           entities=["AC-42"]), event_type="auto_promoted_project_event")

        self.assertEqual(conflict.outcome, "conflict")
        self.assertIsNotNone(conflict.conflict_id)
        self.assertEqual(conflict.record.status, "candidate")
        open_conflicts = self.store.list_conflicts(owner_id="user-1")
        self.assertEqual(open_conflicts[0]["existing_memory_id"], existing.record.memory_id)
        self.assertEqual(open_conflicts[0]["conflict_type"], "claim_mismatch")
        self.assertIn("temporal_scope", open_conflicts[0]["details"]["changed_claims"])

    def test_conflict_resolution_supersedes_old_event_and_activates_new_one(self) -> None:
        existing = self.store.capture_event(self._shipment(temporal="Friday"), event_type="auto_promoted_project_event")
        conflict = self.store.capture_event(self._shipment(temporal="Monday"), event_type="auto_promoted_project_event")

        resolved = self.store.resolve_conflict(conflict.conflict_id or 0,
                                               action="supersede_existing", actor_id="user-1")

        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(self.store.get(existing.record.memory_id).status, "superseded")
        self.assertEqual(self.store.get(conflict.record.memory_id).status, "active")

    def test_project_scope_and_expired_events_do_not_block_new_events(self) -> None:
        imports = self.store.capture_event(self._shipment(project="imports"), event_type="auto_promoted_project_event")
        other_project = self.store.capture_event(self._shipment(project="exports"), event_type="auto_promoted_project_event")
        self.store.review(imports.record.memory_id, "expire", actor_id="user-1")
        new_imports = self.store.capture_event(self._shipment(project="imports"), event_type="auto_promoted_project_event")

        self.assertEqual(other_project.outcome, "created")
        self.assertEqual(new_imports.outcome, "created")


if __name__ == "__main__":
    unittest.main()
