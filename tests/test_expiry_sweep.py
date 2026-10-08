"""Deterministic lifecycle tests for scheduled memory expiry."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.memory.models import MemoryRecord, PreferenceMemory
from core.storage.memory_store import MemoryStore


class ExpirySweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.sqlite3")
        self.store = MemoryStore(self.db_path)
        self.now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _memory(self, *, owner: str = "user-1", valid_to: datetime | None) -> MemoryRecord:
        return MemoryRecord(
            owner_id=owner, scope="user", kind="preference", status="active", user_confirmed=True,
            valid_to=valid_to, payload=PreferenceMemory(preferred_behavior="Use concise shipment updates"),
        )

    def test_sweep_expires_due_memory_audits_and_enqueues_projections(self) -> None:
        due = self._memory(valid_to=self.now - timedelta(seconds=1))
        self.store.upsert(due)

        expired = self.store.expire_due(now=self.now)

        self.assertEqual([record.memory_id for record in expired], [due.memory_id])
        self.assertEqual(self.store.get(due.memory_id).status, "expired")
        jobs = self.store.list_projection_jobs(owner_id="user-1", status="pending")
        self.assertEqual({job["target"] for job in jobs}, {"qdrant", "graph"})
        with sqlite3.connect(self.db_path) as connection:
            audit = connection.execute(
                "SELECT event_type, actor_id FROM memory_audit_events WHERE memory_id = ? ORDER BY event_id DESC LIMIT 1",
                (due.memory_id,),
            ).fetchone()
        self.assertEqual(audit, ("expired_by_sweep", "system"))

    def test_sweep_is_idempotent_and_does_not_cross_owner_or_future_deadlines(self) -> None:
        due = self._memory(valid_to=self.now - timedelta(days=1))
        future = self._memory(valid_to=self.now + timedelta(days=1))
        other_owner = self._memory(owner="user-2", valid_to=self.now - timedelta(days=1))
        for record in (due, future, other_owner):
            self.store.upsert(record)

        self.assertEqual([record.memory_id for record in self.store.expire_due(now=self.now, owner_id="user-1")],
                         [due.memory_id])
        self.assertEqual(self.store.expire_due(now=self.now, owner_id="user-1"), [])
        self.assertEqual(self.store.get(future.memory_id).status, "active")
        self.assertEqual(self.store.get(other_owner.memory_id).status, "active")


if __name__ == "__main__":
    unittest.main()
