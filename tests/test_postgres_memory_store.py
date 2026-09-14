"""Opt-in PostgreSQL integration coverage for memory lifecycle and outbox work."""
from __future__ import annotations

import importlib.util
import os
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from core.memory.models import EventMemory, MemoryRecord, PreferenceMemory

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


if __name__ == "__main__":
    unittest.main()
