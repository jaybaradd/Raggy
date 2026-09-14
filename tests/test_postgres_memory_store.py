"""Opt-in PostgreSQL integration coverage for memory lifecycle and outbox work."""
from __future__ import annotations

import importlib.util
import os
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from core.memory.models import EventMemory, IdentifierReference, MemoryRecord, PreferenceMemory
from core.memory.reconciliation import ReconciliationDecision, reconciliation_details

RUN = os.getenv("RUN_POSTGRES_INTEGRATION_TESTS") == "1"
URL = os.getenv("POSTGRES_DATABASE_URL", "")


@unittest.skipUnless(RUN and URL and importlib.util.find_spec("psycopg"),
                     "set RUN_POSTGRES_INTEGRATION_TESTS=1 and POSTGRES_DATABASE_URL to run")
class PostgresMemoryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg
        from db.postgres_memory_store import PostgresMemoryRepository
        self.schema = f"raggy_test_{uuid.uuid4().hex}"
        with psycopg.connect(URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.store = PostgresMemoryRepository(URL, schema=self.schema, worker_id="worker-a")

    def tearDown(self) -> None:
        import psycopg
        if re.fullmatch(r"raggy_test_[0-9a-f]+", self.schema):
            with psycopg.connect(URL, autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

    @staticmethod
    def _event(temporal: str = "Friday") -> MemoryRecord:
        return MemoryRecord(owner_id="owner-a", scope="project", project_scope="imports", kind="event",
                            status="active", user_confirmed=True,
                            payload=EventMemory(event_type="shipment arrival", summary="AC-42 arrives",
                                                entities=["AC-42"], locations=["Tokyo"], temporal_scope=temporal))

    def test_dedupe_conflict_resolution_audit_and_restart(self) -> None:
        existing = self.store.capture_event(self._event(), event_type="created")
        duplicate = self.store.capture_event(self._event(), event_type="created")
        conflict = self.store.capture_event(self._event("Monday"), event_type="created")

        self.assertEqual(duplicate.outcome, "duplicate")
        self.assertEqual(duplicate.record.memory_id, existing.record.memory_id)
        self.assertEqual(conflict.outcome, "conflict")
        resolved = self.store.resolve_conflict(conflict.conflict_id, action="supersede_existing", actor_id="owner-a")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(self.store.get(existing.record.memory_id).status, "superseded")
        self.assertEqual(self.store.get(conflict.record.memory_id).status, "active")
        self.assertIn("duplicate_detected", [event["event_type"] for event in self.store.list_audit_events(existing.record.memory_id, owner_id="owner-a")])

        from db.postgres_memory_store import PostgresMemoryRepository
        restarted = PostgresMemoryRepository(URL, schema=self.schema)
        self.assertEqual(restarted.get(conflict.record.memory_id).status, "active")
        self.assertEqual(len(restarted.list_conflicts(owner_id="owner-a", status=None)), 1)

    def test_expiry_and_outbox_claims_are_idempotent_and_disjoint(self) -> None:
        now = datetime(2026, 9, 14, tzinfo=timezone.utc)
        due = MemoryRecord(owner_id="owner-a", scope="user", kind="preference", status="active", user_confirmed=True,
                           valid_to=now - timedelta(seconds=1), payload=PreferenceMemory(preferred_behavior="Be concise"))
        self.store.upsert(due)
        self.assertEqual([record.memory_id for record in self.store.expire_due(now=now)], [due.memory_id])
        self.assertEqual(self.store.expire_due(now=now), [])
        self.assertEqual(self.store.get(due.memory_id).status, "expired")
        self.assertIn("expired_by_sweep", [event["event_type"] for event in self.store.list_audit_events(due.memory_id, owner_id="owner-a")])

        from db.postgres_memory_store import PostgresMemoryRepository
        second_worker = PostgresMemoryRepository(URL, schema=self.schema, worker_id="worker-b")
        first_jobs = self.store.claim_projection_jobs(limit=1)
        second_jobs = second_worker.claim_projection_jobs(limit=10)
        self.assertEqual({job["job_id"] for job in first_jobs} & {job["job_id"] for job in second_jobs}, set())
        self.assertEqual(len(first_jobs) + len(second_jobs), 2)
        for job in first_jobs: self.store.complete_projection_job(job["job_id"])
        for job in second_jobs: second_worker.complete_projection_job(job["job_id"])
        self.assertEqual(self.store.list_projection_jobs(owner_id="owner-a", status="pending"), [])

    def test_owner_isolation(self) -> None:
        record = MemoryRecord(owner_id="owner-a", scope="user", kind="preference", status="active", user_confirmed=True,
                              payload=PreferenceMemory(preferred_behavior="Be concise"))
        self.store.upsert(record)
        self.assertIsNone(self.store.get_owned(record.memory_id, owner_id="owner-b"))
        with self.assertRaises(KeyError):
            self.store.list_audit_events(record.memory_id, owner_id="owner-b")

    def test_identifier_candidates_are_project_scoped_and_restart_safe(self) -> None:
        record = self._event()
        record.project_id = "imports-id"
        record.payload.identifier_references = [IdentifierReference(value="EK-420", mention="order EK-420")]
        self.store.upsert(record)

        candidates = self.store.find_memory_candidates(
            owner_id="owner-a", session_id="other-chat", project_id="imports-id", project_scope="imports",
            identifier_references=[IdentifierReference(value="EK420")],
        )
        self.assertEqual([item.memory_id for item in candidates], [record.memory_id])

        from db.postgres_memory_store import PostgresMemoryRepository
        restarted = PostgresMemoryRepository(URL, schema=self.schema)
        self.assertEqual(
            [item.memory_id for item in restarted.find_memory_candidates(
                owner_id="owner-a", session_id="other-chat", project_id="wrong-project", project_scope="imports",
                identifier_references=[IdentifierReference(value="EK420")],
            )],
            [],
        )

    def test_reconciliation_update_keeps_existing_fact_active(self) -> None:
        existing = self._event("Tuesday")
        existing.payload.identifier_references = [IdentifierReference(value="INC-19")]
        self.store.upsert(existing)
        incoming = self._event("Thursday")
        incoming.payload.event_type = "incident update"
        incoming.payload.identifier_references = [IdentifierReference(value="INC19")]
        decision = ReconciliationDecision(
            outcome="update", existing_memory_id=existing.memory_id,
            matched_identifier_values=["inc19"],
            changed_claims={"expected_arrival": {"existing": "Tuesday", "incoming": "Thursday"}},
            confidence=0.97, reason="Same incident reference with changed status.",
        )
        result = self.store.apply_event_reconciliation(
            incoming, outcome=decision.outcome, existing_memory_id=existing.memory_id,
            details=reconciliation_details(decision, incoming), actor_id="system",
        )
        self.assertEqual(result.outcome, "conflict")
        conflict = self.store.get_conflict(result.conflict_id, owner_id="owner-a")
        self.assertEqual(conflict["conflict_type"], "claim_mismatch")
        self.assertEqual(self.store.get(existing.memory_id).status, "active")
        self.assertEqual(self.store.get(incoming.memory_id).status, "candidate")

    def test_related_link_is_idempotent(self) -> None:
        existing = self._event("Tuesday")
        existing.payload.identifier_references = [IdentifierReference(value="MTG-88")]
        self.store.upsert(existing)
        related = self._event("Friday")
        related.payload.event_type = "meeting note"
        related.payload.identifier_references = [IdentifierReference(value="MTG88")]
        decision = ReconciliationDecision(outcome="related", existing_memory_id=existing.memory_id,
                                          confidence=0.9, reason="Related operational note.")
        for _ in range(2):
            self.store.apply_event_reconciliation(
                related, outcome="related", existing_memory_id=existing.memory_id,
                details=reconciliation_details(decision, related), actor_id="system",
            )
        with self.store._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM memory_relationships WHERE relationship_type='related'")
            self.assertEqual(cursor.fetchone()["count"], 1)


if __name__ == "__main__":
    unittest.main()
