"""Stable project IDs propagate through authoritative and derived memory state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.models import EventMemory, MemoryRecord
from core.storage.graph_store import GraphStore
from core.storage.memory_store import MemoryStore


class ProjectIdPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp_dir.name) / "memory.sqlite3")
        self.store = MemoryStore(self.path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _record(self, *, project_id: str | None = None) -> MemoryRecord:
        return MemoryRecord(owner_id="owner-a", scope="project", project_id=project_id,
                            project_scope="Imports", kind="event", status="active", user_confirmed=True,
                            confidence=.95, payload=EventMemory(event_type="shipment", summary="AC-42 arrives",
                            entities=["AC-42"], locations=["Tokyo"], temporal_scope="Friday"))

    def test_backfill_only_uses_owner_scoped_canonical_mapping(self) -> None:
        legacy = self._record()
        self.store.upsert(legacy)
        result = self.store.backfill_project_ids({("owner-a", "imports"): {"project_id": "project-a"}})
        self.assertEqual(result, {"updated": 1, "unmatched": 0})
        self.assertEqual(self.store.get(legacy.memory_id).project_id, "project-a")
        self.assertEqual(self.store.list_audit_events(legacy.memory_id, owner_id="owner-a")[0]["event_type"], "project_id_backfilled")

    def test_graph_projection_retains_project_id(self) -> None:
        graph = GraphStore(self.path)
        record = self._record(project_id="project-a")
        self.store.upsert(record)
        graph.sync_memory(record)
        edges = graph.list_edges(owner_id="owner-a", project_id="project-a")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["project_id"], "project-a")


if __name__ == "__main__":
    unittest.main()
