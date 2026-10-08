"""Conversational update targets use durable trace links, never prompt labels."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core.memory.extractor import MemoryExtractor
from core.memory.models import EventClaim, EventMemory, MemoryRecord
from core.memory.update_context import resolve_update_context
from core.storage.memory_store import MemoryStore


class UpdateContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp.name) / "memory.sqlite3"))
        self.flight = self._event("flight", "flight", "19:00")
        self.train = self._event("train", "train", "22:00")
        self.store.upsert(self.flight)
        self.store.upsert(self.train)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(memory_id: str, entity: str, departure_time: str) -> MemoryRecord:
        return MemoryRecord(
            memory_id=memory_id, owner_id="default", scope="project", project_id="schedule-v2",
            project_scope="schedule-v2", session_id="chat-1", kind="event", status="active",
            user_confirmed=True, confidence=1.0,
            payload=EventMemory(event_type=f"{entity}_departure", summary=f"{entity} today",
                                entities=[entity], claims=[EventClaim(attribute="departure_time", value=departure_time)]),
        )

    def test_supplies_the_real_memory_as_a_candidate_from_previous_answer_trace(self) -> None:
        self.store.record_access_event(trace_id="flight-answer", session_id="chat-1", message_id="user-1",
                                       memory_id="flight", event_type="injected", prompt_label="M1")
        result = asyncio.run(resolve_update_context(
            user_content="It has changed.",
            messages=[{"role": "assistant", "content": "Flight at 7 PM [M1]", "trace_id": "flight-answer"}],
            store=self.store, owner_id="default", session_id="chat-1", project_id="schedule-v2",
            project_scope="schedule-v2",
        ))
        self.assertEqual([record.memory_id for record in result.continuity_candidates], ["flight"])

    def test_multiple_previous_events_remain_candidates_for_the_current_turn_planner(self) -> None:
        for memory_id in ("flight", "train"):
            self.store.record_access_event(trace_id="combined-answer", session_id="chat-1", message_id="user-1",
                                           memory_id=memory_id, event_type="injected", prompt_label="M1")
        result = asyncio.run(resolve_update_context(
            user_content="It has changed.",
            messages=[{"role": "assistant", "content": "Schedules [M1] [M2]", "trace_id": "combined-answer"}],
            store=self.store, owner_id="default", session_id="chat-1", project_id="schedule-v2",
            project_scope="schedule-v2",
        ))
        self.assertEqual(
            [record.memory_id for record in result.continuity_candidates],
            ["flight", "train"],
        )

    def test_prompt_labels_are_not_used_as_identity(self) -> None:
        self.store.record_access_event(trace_id="train-answer", session_id="chat-1", message_id="user-2",
                                       memory_id="train", event_type="injected", prompt_label="M1")
        result = asyncio.run(resolve_update_context(
            user_content="It has changed.",
            messages=[{"role": "assistant", "content": "Train at 10 PM [M1]", "trace_id": "train-answer"}],
            store=self.store, owner_id="default", session_id="chat-1", project_id="schedule-v2",
            project_scope="schedule-v2",
        ))
        self.assertEqual([record.memory_id for record in result.continuity_candidates], ["train"])

    def test_explicit_identifier_does_not_turn_trace_evidence_into_authority(self) -> None:
        self.store.record_access_event(trace_id="flight-answer", session_id="chat-1", message_id="user-1",
                                       memory_id="flight", event_type="injected", prompt_label="M1")
        result = asyncio.run(resolve_update_context(
            user_content="TRAIN-42 has changed.",
            messages=[{"role": "assistant", "content": "Flight at 7 PM [M1]", "trace_id": "flight-answer"}],
            store=self.store, owner_id="default", session_id="chat-1", project_id="schedule-v2",
            project_scope="schedule-v2",
        ))
        self.assertEqual([record.memory_id for record in result.continuity_candidates], ["flight"])

    def test_target_constraint_rejects_an_unconnected_event_candidate(self) -> None:
        extractor = MemoryExtractor(_Provider({"candidates": [{
            "kind": "event", "confidence": 0.95, "evidence_refs": [],
            "payload": {
                "event_type": "status note", "summary": "A change was reported",
                "entities": [], "locations": [], "temporal_scope": None,
                "identifier_references": [],
                "claims": [{"attribute": "duration", "value": "45 minutes", "confidence": 0.95}],
            },
        }]}))
        result = asyncio.run(extractor.extract_turn(
            session_id="chat-1", user_content="It changed by 45 minutes.",
            assistant_content="", evidence_refs=[], update_target=self.flight,
        ))
        self.assertEqual(result.candidates, [])

    def test_trace_resolved_event_without_entities_accepts_matching_changed_claim(self) -> None:
        target = self.flight.model_copy(deep=True)
        target.payload.entities = []
        extractor = MemoryExtractor(_Provider({"candidates": [{
            "kind": "event", "confidence": 0.95, "evidence_refs": [],
            "payload": {
                "event_type": "travel", "summary": "Updated event time",
                "entities": [], "locations": [], "temporal_scope": "19:40",
                "identifier_references": [],
                "claims": [{"attribute": "departure_time", "value": "19:40", "confidence": 0.95}],
            },
        }]}))
        result = asyncio.run(extractor.extract_turn(
            session_id="chat-1", user_content="It changed.", assistant_content="",
            evidence_refs=[], update_target=target,
        ))
        self.assertEqual(len(result.candidates), 1)


class _Provider:
    def __init__(self, response: dict) -> None:
        self.response = response

    async def generate_json(self, prompt: str) -> dict:
        return self.response


if __name__ == "__main__":
    unittest.main()
