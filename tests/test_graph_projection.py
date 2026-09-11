"""Deterministic tests for the rebuildable graph-compatible memory projection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.models import EntityMemory, EventMemory, KnowledgeAtom, MemoryRecord
from core.memory.projections import sync_pending_projections
from core.storage.graph_store import GraphStore
from core.storage.memory_store import MemoryStore


class GraphProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.sqlite3")
        self.graph = GraphStore(self.db_path)
        self.store = MemoryStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_alias_resolution_projects_one_edge_with_provenance(self) -> None:
        entity = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="biology",
            kind="entity", status="active", user_confirmed=True,
            source_turn_id="turn-entity", evidence_refs=["ev-entity"],
            payload=EntityMemory(
                canonical_name="Michaelis-Menten Equation", entity_type="concept",
                aliases=["MM equation"],
            ),
        )
        atom = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="biology",
            kind="knowledge", status="active", user_confirmed=True, confidence=0.91,
            source_turn_id="turn-atom", evidence_refs=["ev-page-12"],
            payload=KnowledgeAtom(
                subject="Enzyme Kinetics", predicate="requires understanding of",
                object="MM equation", qualifiers={"course": "biology"},
            ),
        )
        self.graph.sync_memory(entity)
        self.graph.sync_memory(atom)

        edges = self.graph.list_edges(owner_id="user-1", scope="project", project_scope="biology")
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge["predicate"], "requires_understanding_of")
        self.assertEqual(edge["relation_family"], "dependency")
        self.assertEqual(edge["memory_id"], atom.memory_id)
        self.assertEqual(edge["provenance_kind"], "evidence")
        self.assertIn("ev-page-12", edge["evidence_refs_json"])

        nodes = {node["node_id"]: node for node in self.graph.list_nodes(
            owner_id="user-1", scope="project", project_scope="biology"
        )}
        self.assertEqual(nodes[edge["object_node_id"]]["canonical_label"], "Michaelis-Menten Equation")

    def test_inactive_memory_deactivates_its_edge_without_deleting_nodes(self) -> None:
        atom = MemoryRecord(
            owner_id="user-1", scope="user", kind="knowledge", status="active", user_confirmed=True,
            source_turn_id="turn-1", evidence_refs=["ev-1"],
            payload=KnowledgeAtom(subject="A", predicate="causes", object="B"),
        )
        self.graph.sync_memory(atom)
        atom.status = "expired"
        self.graph.sync_memory(atom)
        self.assertEqual(self.graph.list_edges(owner_id="user-1", scope="user"), [])
        self.assertGreaterEqual(len(self.graph.list_nodes(owner_id="user-1", scope="user")), 2)

    def test_project_event_projects_to_a_provenance_linked_graph_edge(self) -> None:
        event = MemoryRecord(
            owner_id="user-1", scope="project", project_scope="imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            source_turn_id="turn-shipment",
            payload=EventMemory(
                event_type="shipment", summary="Flight-cargo shipment arriving from Tokyo",
                entities=["shipment", "flight cargo"], locations=["Tokyo"], temporal_scope="in 2 days",
            ),
        )
        self.graph.sync_memory(event)

        edge = self.graph.list_edges(owner_id="user-1", scope="project", project_scope="imports")[0]
        self.assertEqual(edge["predicate"], "relates_to")
        self.assertEqual(edge["relation_family"], "association")
        self.assertEqual(edge["provenance_kind"], "conversation")

    def test_outbox_projects_authoritative_memory_and_records_completion(self) -> None:
        class FakeQdrant:
            def __init__(self) -> None:
                self.memory_ids: list[str] = []

            def upsert_memory(self, record: MemoryRecord) -> None:
                self.memory_ids.append(record.memory_id)

        record = MemoryRecord(
            owner_id="user-1", scope="user", kind="knowledge", status="active", user_confirmed=True,
            source_turn_id="turn-outbox", evidence_refs=["ev-outbox"],
            payload=KnowledgeAtom(subject="A", predicate="supports", object="B"),
        )
        self.store.upsert(record)
        qdrant = FakeQdrant()
        result = sync_pending_projections(store=self.store, graph=self.graph, qdrant=qdrant)
        self.assertEqual(result, {"completed": 2, "failed": 0})
        self.assertEqual(qdrant.memory_ids, [record.memory_id])
        self.assertEqual(len(self.graph.list_edges(owner_id="user-1", scope="user")), 1)
        jobs = self.store.list_projection_jobs(owner_id="user-1")
        self.assertEqual({job["status"] for job in jobs}, {"completed"})


if __name__ == "__main__":
    unittest.main()
