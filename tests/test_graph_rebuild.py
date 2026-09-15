"""Tests for explicit rebuild of the derived graph without touching Qdrant."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.graph_rebuild import rebuild_graph_projection
from core.memory.models import EventMemory, MemoryRecord
from core.storage.graph_store import GraphStore
from core.storage.memory_store import MemoryStore


class _FailIfUsedQdrant:
    def upsert_memory(self, _record: MemoryRecord) -> None:
        raise AssertionError("a graph-only rebuild must not write Qdrant")


class GraphRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.memories = MemoryStore(str(root / "memories.sqlite3"))
        self.graph = GraphStore(str(root / "graph.sqlite3"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rebuild_replaces_graph_from_authoritative_records_only(self) -> None:
        record = MemoryRecord(
            owner_id="owner-a", scope="user", kind="event", status="active",
            user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="meeting", summary="Meeting with manager"),
        )
        self.memories.upsert(record)
        self.graph.sync_memory(MemoryRecord(
            owner_id="stale-owner", scope="user", kind="event", status="active",
            user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="stale", summary="Stale graph node"),
        ))

        result = rebuild_graph_projection(store=self.memories, graph=self.graph, batch_size=10)

        self.assertEqual(result, {"scheduled": 1, "completed": 1, "failed": 0})
        self.assertEqual(len(self.graph.list_nodes(owner_id="owner-a")), 1)
        self.assertEqual(self.graph.list_nodes(owner_id="stale-owner"), [])


if __name__ == "__main__":
    unittest.main()
