"""Unit tests for the FalkorDB graph adapter without a running Falkor server."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from core.memory.graph_projection import MemoryGraphRelationship
from core.memory.models import EventMemory, MemoryRecord
from core.storage.falkor_graph_store import FalkorGraphStore


class _Result:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.result_set = rows or []


class _Graph:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []
        self.reads: list[tuple[str, dict]] = []
        self.next_rows: list[object] = []

    def query(self, query: str, params: dict) -> _Result:
        self.writes.append((query, params))
        return _Result()

    def ro_query(self, query: str, params: dict) -> _Result:
        self.reads.append((query, params))
        return _Result(self.next_rows)


class _Client:
    def __init__(self) -> None:
        self.graph = _Graph()
        self.selected_graph: str | None = None

    def execute_command(self, command: str) -> bool:
        assert command == "PING"
        return True

    def select_graph(self, name: str) -> _Graph:
        self.selected_graph = name
        return self.graph


class FalkorGraphStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _Client()
        self.store = FalkorGraphStore(
            url="redis://127.0.0.1:6380", graph_name="raggy_memory_projection_v1", client=self.client,
        )

    @staticmethod
    def _record(*, owner_id: str = "owner-a", project_id: str | None = "project-a") -> MemoryRecord:
        return MemoryRecord(
            owner_id=owner_id, scope="project", project_id=project_id, project_scope="imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment arrival", summary="Order EK420 arrives", entities=["EK420"]),
        )

    def test_sync_memory_uses_memory_id_and_lossless_properties(self) -> None:
        record = self._record()

        self.store.sync_memory(record)

        query, params = self.client.graph.writes[0]
        self.assertIn("MERGE (memory:Memory {memory_id: $memory_id})", query)
        self.assertEqual(params["memory_id"], record.memory_id)
        self.assertEqual(params["properties"]["owner_id"], "owner-a")
        self.assertIn("EK420", params["properties"]["payload_json"])

    def test_relationship_writes_only_the_supported_durable_edge_type(self) -> None:
        source, target = self._record(), self._record()
        relationship = MemoryGraphRelationship("relationship-1", "related", source, target, datetime.now(timezone.utc))

        self.store.sync_relationship(relationship)

        query, params = self.client.graph.writes[-1]
        self.assertIn("[edge:RELATED", query)
        self.assertEqual(params["owner_id"], "owner-a")
        self.assertEqual(params["relationship_id"], "relationship-1")

    def test_legacy_contradiction_relationship_remains_projectable(self) -> None:
        source, target = self._record(), self._record()
        relationship = MemoryGraphRelationship(
            "contradiction-1", "contradiction", source, target, datetime.now(timezone.utc),
        )

        self.store.sync_relationship(relationship)

        query, params = self.client.graph.writes[-1]
        self.assertIn("[edge:CONTRADICTION", query)
        self.assertEqual(params["relationship_type"], "CONTRADICTION")

    def test_relationship_never_bridges_projects(self) -> None:
        source = self._record(project_id="project-a")
        target = self._record(project_id="project-b")
        relationship = MemoryGraphRelationship(
            "cross-project", "related", source, target, datetime.now(timezone.utc),
        )

        self.store.sync_relationship(relationship)

        self.assertFalse(relationship.is_projectable)
        query, params = self.client.graph.writes[-1]
        self.assertIn("DELETE edge", query)
        self.assertEqual(params, {"relationship_id": "cross-project"})

    def test_reads_always_constrain_owner_and_prefer_project_id(self) -> None:
        self.client.graph.next_rows = [[{"memory_id": "memory-1", "owner_id": "owner-a"}]]

        nodes = self.store.list_nodes(
            owner_id="owner-a", project_id="project-a", project_scope="imports", active=True,
        )

        query, params = self.client.graph.reads[-1]
        self.assertIn("memory.owner_id = $owner_id", query)
        self.assertIn("memory.project_id = $project_id", query)
        self.assertNotIn("memory.project_scope", query)
        self.assertEqual(params, {"owner_id": "owner-a", "active": True, "project_id": "project-a"})
        self.assertEqual(nodes, [{"memory_id": "memory-1", "owner_id": "owner-a"}])

    def test_inactive_memory_removes_incident_edges(self) -> None:
        record = self._record()
        record.status = "expired"

        self.store.sync_memory(record)

        self.assertIn("DELETE edge", self.client.graph.writes[-1][0])

    def test_reset_clears_only_memory_nodes_in_the_selected_graph(self) -> None:
        self.store.reset_projection()

        query, params = self.client.graph.writes[-1]
        self.assertIn("MATCH (memory:Memory) DETACH DELETE memory", query)
        self.assertEqual(params, {})

    def test_expansion_is_owner_scoped_and_returns_only_provenance(self) -> None:
        self.client.graph.next_rows = [["memory-2", "memory-1", "relationship-1", "RELATED"]]

        candidates = self.store.expand_memory_candidates(
            seed_memory_ids=["memory-1"], owner_id="owner-a", session_id="session-a",
            project_id="project-a", project_scope="imports", limit=4,
        )

        query, params = self.client.graph.reads[-1]
        self.assertIn("MATCH (seed:Memory)-[edge:RELATED]-(candidate:Memory)", query)
        self.assertIn("candidate.owner_id = $owner_id", query)
        self.assertIn("candidate.project_id = $project_id", query)
        self.assertEqual(params["seed_memory_ids"], ["memory-1"])
        self.assertEqual(candidates[0].memory_id, "memory-2")
        self.assertEqual(candidates[0].relationship_type, "related")

    def test_empty_falkor_graph_read_is_an_empty_result(self) -> None:
        def empty_graph(_query: str, _params: dict) -> _Result:
            raise RuntimeError("Invalid graph operation on empty key")

        self.client.graph.ro_query = empty_graph  # type: ignore[method-assign]

        self.assertEqual(self.store.list_nodes(owner_id="owner-a"), [])

    def test_default_client_constructor_uses_only_supported_connection_arguments(self) -> None:
        client = _Client()
        with patch("falkordb.FalkorDB.from_url", return_value=client) as constructor:
            FalkorGraphStore(url="redis://127.0.0.1:6380", graph_name="test-graph")

        constructor.assert_called_once_with("redis://127.0.0.1:6380")

    def test_queries_keep_dynamic_values_in_parameters(self) -> None:
        record = self._record(owner_id="owner-'quoted'", project_id="project-'quoted'")

        self.store.sync_memory(record)
        self.store.list_nodes(owner_id=record.owner_id, project_id=record.project_id)
        self.store.expand_memory_candidates(
            seed_memory_ids=[record.memory_id], owner_id=record.owner_id, session_id="session-'quoted'",
            project_id=record.project_id, project_scope="imports", limit=4,
        )

        for query, _params in [*self.client.graph.writes, *self.client.graph.reads]:
            self.assertNotIn(record.owner_id, query)
            self.assertNotIn(record.project_id or "", query)
            self.assertNotIn(record.memory_id, query)


if __name__ == "__main__":
    unittest.main()
