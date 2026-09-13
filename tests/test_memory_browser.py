"""Repository coverage for browser inspection and soft-forget lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.models import MemoryRecord, PreferenceMemory
from core.storage.memory_store import MemoryStore


class MemoryBrowserRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp_dir.name) / "memory.sqlite3"))
        self.record = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="imports", kind="preference",
            status="active", user_confirmed=True,
            payload=PreferenceMemory(preferred_behavior="Use concise shipment updates"),
        )
        self.store.upsert(self.record)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_browser_detail_data_is_owner_scoped(self) -> None:
        self.store.record_access_event(trace_id="trace-1", session_id="session-1", memory_id=self.record.memory_id,
                                       event_type="retrieved")
        self.assertEqual(self.store.get_owned(self.record.memory_id, owner_id="user-1").memory_id, self.record.memory_id)
        self.assertIsNone(self.store.get_owned(self.record.memory_id, owner_id="user-2"))
        self.assertEqual(self.store.list_audit_events(self.record.memory_id, owner_id="user-1")[0]["event_type"], "created")
        self.assertEqual(self.store.list_access_events(self.record.memory_id, owner_id="user-1")[0]["trace_id"], "trace-1")
        with self.assertRaises(KeyError):
            self.store.list_audit_events(self.record.memory_id, owner_id="user-2")

    def test_forget_is_auditable_idempotent_and_excluded_from_default_listing(self) -> None:
        deleted = self.store.forget(self.record.memory_id, actor_id="user-1")
        self.assertEqual(deleted.status, "deleted")
        self.assertEqual(self.store.list(owner_id="user-1", project_scope="imports"), [])
        self.assertEqual(self.store.list(owner_id="user-1", project_scope="imports", status="deleted")[0].memory_id,
                         self.record.memory_id)
        self.assertEqual(self.store.forget(self.record.memory_id, actor_id="user-1").status, "deleted")
        audits = self.store.list_audit_events(self.record.memory_id, owner_id="user-1")
        self.assertEqual([event["event_type"] for event in audits].count("deleted_by_user"), 1)
        self.assertEqual({job["target"] for job in self.store.list_projection_jobs(owner_id="user-1", status="pending")},
                         {"qdrant", "graph"})


if __name__ == "__main__":
    unittest.main()
