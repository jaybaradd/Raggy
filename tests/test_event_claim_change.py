"""Bounded event-claim correction command coverage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.commands import propose_event_claim_change
from core.memory.models import EventClaim, EventMemory, IdentifierReference, MemoryRecord
from core.storage.memory_store import MemoryStore


class EventClaimChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(*, memory_id: str, arrival: str, project_id: str = "project-a",
               identifier: str = "SHP-42", source_turn_id: str | None = None,
               status: str = "active") -> MemoryRecord:
        return MemoryRecord(
            memory_id=memory_id,
            owner_id="owner-a",
            scope="project",
            session_id="chat-a",
            project_id=project_id,
            project_scope="Imports",
            kind="event",
            status=status,
            user_confirmed=status == "active",
            confidence=0.97,
            source_turn_id=source_turn_id,
            payload=EventMemory(
                event_type="shipment arrival",
                summary=f"Shipment {identifier} arrives {arrival}",
                entities=["shipment", identifier],
                identifier_references=[IdentifierReference(value=identifier)],
                claims=[EventClaim(attribute="expected_arrival", value=arrival)],
            ),
        )

    def _propose(self, incoming: MemoryRecord, target: MemoryRecord):
        return propose_event_claim_change(
            store=self.store,
            incoming=incoming,
            target_memory_id=target.memory_id,
            owner_id="owner-a",
            session_id="chat-b",
            project_id="project-a",
            project_scope="Imports",
            confidence=0.98,
            reason="The explicit shipment identifier selected one active subject.",
            matched_identifier_values=["shp42"],
        )

    def test_command_opens_existing_review_conflict_and_reuses_lifecycle_tables(self) -> None:
        existing = self._event(memory_id="old", arrival="Tuesday")
        self.store.upsert(existing)
        incoming = self._event(
            memory_id="temporary", arrival="Friday", identifier="PO-900",
            source_turn_id="assistant-turn-1", status="candidate",
        )

        result = self._propose(incoming, existing)

        self.assertEqual(result.outcome, "conflict")
        persisted = self.store.get(result.record.memory_id)
        self.assertEqual(persisted.status, "candidate")
        self.assertEqual(persisted.subject_id, existing.subject_id)
        self.assertEqual(persisted.scope, existing.scope)
        self.assertEqual(persisted.project_id, existing.project_id)
        conflict = self.store.get_conflict(result.conflict_id or 0, owner_id="owner-a")
        self.assertEqual(conflict["details"]["command"], "propose_event_claim_change")
        self.assertEqual(conflict["details"]["target_subject_id"], existing.subject_id)
        self.assertEqual(
            conflict["details"]["changed_claims"]["expected_arrival"],
            {"existing": "Tuesday", "incoming": "Friday"},
        )
        jobs = self.store.list_projection_jobs(owner_id="owner-a", status="pending")
        self.assertEqual({job["target"] for job in jobs if job["memory_id"] == persisted.memory_id},
                         {"qdrant", "graph"})

    def test_retry_returns_the_same_proposal_and_conflict(self) -> None:
        existing = self._event(memory_id="old", arrival="Tuesday")
        self.store.upsert(existing)

        first = self._propose(self._event(
            memory_id="random-a", arrival="Friday", source_turn_id="same-turn", status="candidate",
        ), existing)
        second = self._propose(self._event(
            memory_id="random-b", arrival="Friday", source_turn_id="same-turn", status="candidate",
        ), existing)

        self.assertEqual(second.record.memory_id, first.record.memory_id)
        self.assertEqual(second.conflict_id, first.conflict_id)
        self.assertEqual(len(self.store.list_conflicts(owner_id="owner-a", status=None)), 1)

    def test_command_rejects_cross_project_target(self) -> None:
        other_project = self._event(memory_id="other", arrival="Tuesday", project_id="project-b")
        self.store.upsert(other_project)

        with self.assertRaises(PermissionError):
            self._propose(self._event(
                memory_id="incoming", arrival="Friday", source_turn_id="turn", status="candidate",
            ), other_project)

        self.assertEqual(self.store.list_conflicts(owner_id="owner-a", status=None), [])
        self.assertIsNone(self.store.get("incoming"))

    def test_review_acceptance_preserves_old_and_new_identifier_aliases(self) -> None:
        existing = self._event(memory_id="old", arrival="Tuesday", identifier="SHP-42")
        self.store.upsert(existing)
        result = self._propose(self._event(
            memory_id="incoming", arrival="Friday", identifier="PO-900",
            source_turn_id="identifier-change", status="candidate",
        ), existing)

        self.store.resolve_conflict(
            result.conflict_id or 0, action="supersede_existing", actor_id="owner-a",
        )

        for identifier in ("SHP42", "PO900"):
            matches = self.store.find_memory_candidates(
                owner_id="owner-a",
                session_id="chat-b",
                project_id="project-a",
                project_scope="Imports",
                identifier_references=[IdentifierReference(value=identifier)],
            )
            self.assertEqual([record.memory_id for record in matches], [result.record.memory_id])


if __name__ == "__main__":
    unittest.main()
