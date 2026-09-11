"""Deterministic policy tests for automatic project-event retention."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory.extractor import ExtractionBatch, MemoryCandidate
from core.memory.jobs import extract_turn_memories
from core.memory.policy import decide_project_capture
from core.storage.memory_store import MemoryStore


class ProjectCapturePolicyTests(unittest.TestCase):
    def test_high_confidence_project_event_is_active_and_time_bound(self) -> None:
        captured_at = datetime(2026, 9, 10, tzinfo=timezone.utc)
        candidate = MemoryCandidate(
            kind="event", confidence=0.9,
            payload={
                "event_type": "shipment",
                "summary": "Flight-cargo shipment arriving from Tokyo",
                "entities": ["shipment", "flight cargo"],
                "locations": ["Tokyo"],
                "temporal_scope": "in 2 days",
            }, evidence_refs=[],
        )

        decision = decide_project_capture(
            candidate=candidate, project_scope="imports",
            user_content="I have a shipment coming from Tokyo in 2 days by flight cargo.",
            captured_at=captured_at,
        )

        self.assertEqual(decision.scope, "project")
        self.assertEqual(decision.status, "active")
        self.assertTrue(decision.user_confirmed)
        self.assertEqual(decision.valid_to, captured_at + timedelta(days=9))

    def test_event_without_project_or_generic_knowledge_stays_a_candidate(self) -> None:
        event = MemoryCandidate(
            kind="event", confidence=0.99,
            payload={"event_type": "shipment", "summary": "A shipment is arriving",
                     "entities": ["shipment"], "locations": [], "temporal_scope": None},
            evidence_refs=[],
        )
        knowledge = MemoryCandidate(
            kind="knowledge", confidence=0.99,
            payload={"subject": "Supplier", "predicate": "is located in", "object": "Tokyo",
                     "qualifiers": {}, "temporal_scope": None},
            evidence_refs=[],
        )

        self.assertEqual(
            decide_project_capture(candidate=event, project_scope=None, user_content="shipment").status,
            "candidate",
        )
        self.assertEqual(
            decide_project_capture(candidate=knowledge, project_scope="imports", user_content="fact").status,
            "candidate",
        )


class ProjectCaptureJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_is_promoted_by_the_post_turn_job(self) -> None:
        class FakeExtractor:
            version = "project-capture-test"
            provider = object()

            async def extract_turn(self, **_: object) -> ExtractionBatch:
                return ExtractionBatch(candidates=[MemoryCandidate(
                    kind="event", confidence=0.9,
                    payload={
                        "event_type": "shipment",
                        "summary": "Flight-cargo shipment arriving from Tokyo",
                        "entities": ["shipment", "flight cargo"],
                        "locations": ["Tokyo"],
                        "temporal_scope": "in 2 days",
                    }, evidence_refs=[],
                )])

        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(str(Path(directory) / "memory.sqlite3"))
            with patch("core.memory.jobs.sync_pending_projections", return_value={"completed": 2, "failed": 0}):
                created = await extract_turn_memories(
                    store=store, extractor=FakeExtractor(), source_turn_id="turn-1",
                    session_id="session-a", project_scope="imports", owner_id="user-1",
                    user_content="I have a shipment coming from Tokyo in 2 days by flight cargo.",
                    assistant_content="I will help track the shipment.", evidence_refs=[],
                )

            self.assertEqual(created, 1)
            record = store.list(owner_id="user-1", scope="project", project_scope="imports")[0]
            self.assertEqual(record.kind, "event")
            self.assertEqual(record.status, "active")
            self.assertTrue(record.user_confirmed)
            self.assertIsNotNone(record.valid_to)


if __name__ == "__main__":
    unittest.main()
