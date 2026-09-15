"""Generic post-turn reconciliation preserves facts until a human reviews updates."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core.memory.jobs import _reconcile_event
from core.memory.models import EventClaim, EventMemory, IdentifierReference, MemoryRecord
from core.memory.reconciliation import (MemoryReconciler, ReconciliationCandidate,
                                        ReconciliationDecision, reconciliation_details)
from core.storage.memory_store import MemoryStore


class _Provider:
    def __init__(self, response: dict | Exception) -> None:
        self.response = response

    async def generate_json(self, prompt: str, schema=None) -> dict:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class MemoryReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(memory_id: str, *, identifier: str, event_type: str, claim: tuple[str, str],
               status: str = "active") -> MemoryRecord:
        return MemoryRecord(
            memory_id=memory_id, owner_id="owner-a", scope="project", project_id="operations-id",
            project_scope="operations", session_id="chat-a", kind="event", status=status,
            user_confirmed=status == "active", confidence=0.95,
            payload=EventMemory(
                event_type=event_type, summary=f"{identifier}: {claim[1]}", entities=[identifier],
                identifier_references=[IdentifierReference(value=identifier)],
                claims=[EventClaim(attribute=claim[0], value=claim[1])],
            ),
        )

    @staticmethod
    def _details(decision: ReconciliationDecision, record: MemoryRecord) -> dict:
        return reconciliation_details(decision, record)

    def test_updates_are_generic_reviewable_conflicts(self) -> None:
        cases = [
            ("EK420", "shipment arrival", ("expected_arrival", "Tuesday"), "delivery delay", ("expected_arrival", "Thursday")),
            ("INC-19", "incident status", ("status", "investigating"), "incident update", ("status", "mitigated")),
            ("MTG-88", "meeting schedule", ("start_time", "Monday 10:00"), "calendar change", ("start_time", "Tuesday 14:00")),
        ]
        for index, (identifier, old_type, old_claim, new_type, new_claim) in enumerate(cases):
            existing = self._event(f"old-{index}", identifier=identifier, event_type=old_type, claim=old_claim)
            incoming = self._event(f"new-{index}", identifier=identifier, event_type=new_type, claim=new_claim)
            self.store.upsert(existing)
            decision = ReconciliationDecision(
                outcome="update", existing_memory_id=existing.memory_id,
                matched_identifier_values=[identifier.casefold().replace("-", "")],
                changed_claims={new_claim[0]: {"existing": old_claim[1], "incoming": new_claim[1]}},
                confidence=0.97, reason="Same identifier with a material claim change.",
            )
            result = self.store.apply_event_reconciliation(
                incoming, outcome=decision.outcome, existing_memory_id=decision.existing_memory_id,
                details=self._details(decision, incoming), actor_id="system",
            )
            self.assertEqual(result.outcome, "conflict")
            self.assertEqual(self.store.get(existing.memory_id).status, "active")
            self.assertEqual(self.store.get(incoming.memory_id).status, "candidate")
            conflict = self.store.get_conflict(result.conflict_id or 0, owner_id="owner-a")
            self.assertEqual(conflict["conflict_type"], "claim_mismatch")
            self.assertEqual(conflict["details"]["changed_claims"][new_claim[0]]["incoming"], new_claim[1])

    def test_duplicate_is_audited_without_storing_a_second_record(self) -> None:
        existing = self._event("existing", identifier="EK420", event_type="arrival", claim=("time", "Tuesday"))
        incoming = self._event("incoming", identifier="EK420", event_type="reschedule", claim=("time", "Tuesday"))
        self.store.upsert(existing)
        decision = ReconciliationDecision(outcome="duplicate", existing_memory_id="existing", confidence=0.99,
                                          reason="Same identifier and claims.")
        result = self.store.apply_event_reconciliation(
            incoming, outcome="duplicate", existing_memory_id="existing",
            details=self._details(decision, incoming), actor_id="system",
        )
        self.assertEqual(result.record.memory_id, "existing")
        self.assertIsNone(self.store.get("incoming"))
        self.assertIn("duplicate_detected", [event["event_type"] for event in self.store.list_audit_events("existing", owner_id="owner-a")])

    def test_related_link_is_idempotent_and_uncertain_stays_candidate(self) -> None:
        existing = self._event("existing", identifier="EK420", event_type="arrival", claim=("time", "Tuesday"))
        related = self._event("related", identifier="EK420", event_type="supplier note", claim=("contact", "called"))
        uncertain = self._event("uncertain", identifier="PO77", event_type="status", claim=("state", "pending"))
        self.store.upsert(existing)
        related_decision = ReconciliationDecision(outcome="related", existing_memory_id="existing", confidence=0.86,
                                                   reason="Separate but connected operational note.")
        for _ in range(2):
            self.store.apply_event_reconciliation(
                related, outcome="related", existing_memory_id="existing",
                details=self._details(related_decision, related), actor_id="system",
            )
        with self.store._connect() as connection:  # Assert the durable table contract directly.
            links = connection.execute("SELECT * FROM memory_relationships WHERE relationship_type = 'related'").fetchall()
        self.assertEqual(len(links), 1)
        uncertain_decision = ReconciliationDecision(outcome="uncertain", confidence=0.0, reason="Planner unavailable.")
        self.store.apply_event_reconciliation(
            uncertain, outcome="uncertain", existing_memory_id=None,
            details=self._details(uncertain_decision, uncertain), actor_id="system",
        )
        self.assertEqual(self.store.get("uncertain").status, "candidate")

    def test_accepted_update_creates_audited_supersession_link(self) -> None:
        existing = self._event("old", identifier="MTG-88", event_type="meeting", claim=("time", "4pm"))
        incoming = self._event("new", identifier="MTG-88", event_type="meeting update", claim=("time", "12pm"))
        self.store.upsert(existing)
        conflict = self.store.apply_event_reconciliation(
            incoming, outcome="update", existing_memory_id=existing.memory_id,
            details={"changed_claims": {"time": {"existing": "4pm", "incoming": "12pm"}}}, actor_id="owner-a",
        )

        self.store.resolve_conflict(conflict.conflict_id or 0, action="supersede_existing", actor_id="owner-a")

        self.assertEqual(self.store.get("old").status, "superseded")
        relationships = self.store.list_relationships("old", owner_id="owner-a")
        self.assertEqual(relationships[0]["relationship_type"], "superseded_by")
        self.assertEqual(relationships[0]["peer_memory_id"], "new")
        self.assertEqual(relationships[0]["source"], "conflict_resolution")
        self.assertEqual(relationships[0]["conflict_id"], conflict.conflict_id)

    def test_invalid_reconciler_selection_is_rejected_and_job_fallback_is_safe(self) -> None:
        existing = self._event("existing", identifier="EK420", event_type="arrival", claim=("time", "Tuesday"))
        incoming = self._event("incoming", identifier="EK420", event_type="delay", claim=("time", "Thursday"))
        self.store.upsert(existing)
        with self.assertRaises(ValueError):
            asyncio.run(MemoryReconciler(_Provider({
                "outcome": "update", "existing_memory_id": "outside", "matched_identifier_values": [],
                "changed_claims": {}, "confidence": 0.9, "reason": "invalid",
            })).reconcile(incoming=incoming, candidates=[ReconciliationCandidate(existing, "exact_identifier")]))
        with self.assertRaises(ValueError):
            asyncio.run(MemoryReconciler(_Provider({
                "outcome": "update", "existing_memory_id": "existing", "matched_identifier_values": [],
                "changed_claims": {}, "confidence": 0.9, "reason": "missing differences",
            })).reconcile(incoming=incoming, candidates=[ReconciliationCandidate(existing, "exact_identifier")]))
        asyncio.run(_reconcile_event(
            store=self.store, record=incoming, provider=_Provider(RuntimeError("offline")),
            policy_reason="test",
        ))
        self.assertEqual(self.store.get("existing").status, "active")
        self.assertEqual(self.store.get("incoming").status, "candidate")

    def test_selected_context_allows_high_confidence_implicit_update(self) -> None:
        existing = self._event("meeting-old", identifier="MEETING-1", event_type="meeting",
                               claim=("start_time", "today at 8 PM IST"))
        # Natural-language references do not have to carry a synthetic ID.
        existing.payload.identifier_references = []
        existing.payload.entities = ["manager"]
        incoming = self._event("meeting-new", identifier="MEETING-1", event_type="calendar change",
                               claim=("start_time", "tomorrow at 10 AM"))
        incoming.payload.identifier_references = []
        incoming.payload.entities = ["manager"]
        self.store.upsert(existing)
        provider = _Provider(RuntimeError("the second planner must not be needed"))
        asyncio.run(_reconcile_event(
            store=self.store, record=incoming, provider=provider, policy_reason="test",
            reconciliation_hints=[{
                "memory_id": "meeting-old", "candidate_source": "semantic",
                "selection_relation": "updates", "selection_confidence": 0.96,
            }],
        ))
        self.assertEqual(self.store.get("meeting-old").status, "active")
        self.assertEqual(self.store.get("meeting-new").status, "candidate")
        conflicts = self.store.list_conflicts(owner_id="owner-a")
        self.assertEqual(conflicts[0]["existing_memory_id"], "meeting-old")

    def test_two_contextual_updates_in_one_turn_open_two_independent_conflicts(self) -> None:
        appointment = self._event("appointment-old", identifier="APPOINTMENT-1", event_type="appointment",
                                  claim=("time", "12 PM"))
        appointment.payload.identifier_references = []
        appointment.payload.entities = ["doctor"]
        meeting = self._event("meeting-old", identifier="MEETING-1", event_type="meeting",
                              claim=("time", "4 PM"))
        meeting.payload.identifier_references = []
        meeting.payload.entities = ["manager"]
        cancelled = self._event("appointment-cancelled", identifier="APPOINTMENT-2", event_type="appointment cancellation",
                                claim=("status", "cancelled"))
        cancelled.payload.identifier_references = []
        cancelled.payload.entities = ["doctor's appointment", "receptionist"]
        rescheduled = self._event("meeting-rescheduled", identifier="MEETING-2", event_type="meeting reschedule",
                                  claim=("new_time", "12 PM"))
        rescheduled.payload.identifier_references = []
        rescheduled.payload.entities = ["meeting", "manager"]
        self.store.upsert(appointment)
        self.store.upsert(meeting)
        hints = [
            {"memory_id": appointment.memory_id, "selection_relation": "updates", "selection_confidence": 0.98},
            {"memory_id": meeting.memory_id, "selection_relation": "updates", "selection_confidence": 0.98},
        ]
        offline = _Provider(RuntimeError("second planner must not be needed"))

        asyncio.run(_reconcile_event(
            store=self.store, record=cancelled, provider=offline, policy_reason="test", reconciliation_hints=hints,
        ))
        asyncio.run(_reconcile_event(
            store=self.store, record=rescheduled, provider=offline, policy_reason="test", reconciliation_hints=hints,
        ))

        conflicts = self.store.list_conflicts(owner_id="owner-a")
        self.assertEqual({(item["incoming_memory_id"], item["existing_memory_id"]) for item in conflicts}, {
            (cancelled.memory_id, appointment.memory_id),
            (rescheduled.memory_id, meeting.memory_id),
        })

    def test_shared_time_without_subject_overlap_stays_reviewable(self) -> None:
        first = self._event("first", identifier="FIRST-1", event_type="appointment", claim=("time", "12 PM"))
        second = self._event("second", identifier="SECOND-1", event_type="meeting", claim=("time", "12 PM"))
        incoming = self._event("incoming", identifier="INCOMING-1", event_type="status", claim=("time", "12 PM"))
        for record, entities in ((first, ["alex"]), (second, ["sam"]), (incoming, ["unknown"])):
            record.payload.identifier_references = []
            record.payload.entities = entities
        self.store.upsert(first)
        self.store.upsert(second)

        asyncio.run(_reconcile_event(
            store=self.store, record=incoming, provider=_Provider(RuntimeError("offline")), policy_reason="test",
            reconciliation_hints=[
                {"memory_id": first.memory_id, "selection_relation": "updates", "selection_confidence": 0.98},
                {"memory_id": second.memory_id, "selection_relation": "updates", "selection_confidence": 0.98},
            ],
        ))

        self.assertEqual(self.store.get(incoming.memory_id).status, "candidate")
        self.assertEqual(self.store.list_conflicts(owner_id="owner-a"), [])

    def test_low_confidence_implicit_update_becomes_reviewable_uncertain_record(self) -> None:
        existing = self._event("meeting-old", identifier="MEETING-1", event_type="meeting",
                               claim=("start_time", "today at 8 PM IST"))
        existing.payload.identifier_references = []
        incoming = self._event("meeting-new", identifier="MEETING-1", event_type="calendar change",
                               claim=("start_time", "tomorrow at 10 AM"))
        incoming.payload.identifier_references = []
        self.store.upsert(existing)
        provider = _Provider({
            "outcome": "update", "existing_memory_id": "meeting-old", "matched_identifier_values": [],
            "changed_claims": {"start_time": {"existing": "today", "incoming": "tomorrow"}},
            "confidence": 0.60, "reason": "Possibly the same meeting.",
        })
        asyncio.run(_reconcile_event(
            store=self.store, record=incoming, provider=provider, policy_reason="test",
            reconciliation_hints=[{"memory_id": "meeting-old"}],
        ))
        self.assertEqual(self.store.get("meeting-old").status, "active")
        self.assertEqual(self.store.get("meeting-new").status, "candidate")
        self.assertEqual(self.store.list_conflicts(owner_id="owner-a"), [])


if __name__ == "__main__":
    unittest.main()
