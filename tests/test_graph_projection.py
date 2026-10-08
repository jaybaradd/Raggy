"""Regression tests for the conservative, rebuildable memory graph model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path

from core.memory.graph_projection import MemoryGraphRelationship
from core.memory.models import EventMemory, KnowledgeAtom, MemoryRecord
from core.memory.projections import sync_pending_projections
from core.storage.graph_store import GraphStore
from core.storage.memory_store import MemoryStore


class GraphProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp_dir.name) / "memory.sqlite3"))
        self.graph = GraphStore(str(Path(self.temp_dir.name) / "graph.sqlite3"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _record(*, owner_id: str = "user-1", project_id: str | None = None,
                status: str = "active", valid_to: datetime | None = None) -> MemoryRecord:
        return MemoryRecord(
            owner_id=owner_id, scope="project" if project_id else "user", project_id=project_id,
            project_scope="imports" if project_id else None, kind="event", status=status,
            user_confirmed=status == "active", confidence=0.9, source_turn_id="turn-1", valid_to=valid_to,
            payload=EventMemory(event_type="shipment arrival", summary="Order EK420 arrives from Dubai",
                                entities=["EK420"], locations=["Dubai"], temporal_scope="Tuesday"),
        )

    def test_memory_node_is_lossless_and_project_id_is_preferred(self) -> None:
        record = self._record(project_id="project-imports")
        self.graph.sync_memory(record)

        nodes = self.graph.list_nodes(owner_id="user-1", project_id="project-imports")

        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["memory_id"], record.memory_id)
        self.assertEqual(node["project_id"], "project-imports")
        self.assertEqual(node["status"], "active")
        self.assertEqual(node["is_active"], 1)
        self.assertEqual(json.loads(node["payload_json"])["entities"], ["EK420"])
        self.assertEqual(self.graph.list_nodes(owner_id="other-user", active=None), [])

    def test_relationship_requires_two_eligible_endpoint_memories(self) -> None:
        source = self._record(project_id="project-imports")
        target = self._record(project_id="project-imports")
        relationship = MemoryGraphRelationship("related-1", "related", source, target, datetime.now(timezone.utc))

        self.graph.sync_relationship(relationship)
        self.assertEqual(len(self.graph.list_edges(owner_id="user-1", project_id="project-imports")), 1)

        target.status = "expired"
        target.user_confirmed = True
        self.graph.sync_memory(target)
        self.assertEqual(self.graph.list_edges(owner_id="user-1", project_id="project-imports"), [])

    def test_expired_memory_is_retained_as_inactive_node(self) -> None:
        record = self._record(valid_to=datetime.now(timezone.utc) - timedelta(seconds=1))
        self.graph.sync_memory(record)

        self.assertEqual(self.graph.list_nodes(owner_id="user-1"), [])
        nodes = self.graph.list_nodes(owner_id="user-1", active=None)
        self.assertEqual(nodes[0]["status"], "active")
        self.assertEqual(nodes[0]["is_active"], 0)

    def test_open_conflict_does_not_create_a_graph_contradiction(self) -> None:
        existing = self._record(project_id="project-imports")
        incoming = self._record(project_id="project-imports")
        self.store.upsert(existing)
        self.store.apply_event_reconciliation(
            incoming, outcome="update", existing_memory_id=existing.memory_id,
            details={"changed_claims": {"temporal_scope": {"existing": "Tuesday", "incoming": "Thursday"}}},
        )

        self.assertEqual(
            self.store.list_graph_relationships(owner_id="user-1", memory_id=existing.memory_id), [],
        )

    def test_outbox_projects_authoritative_related_link(self) -> None:
        class FakeQdrant:
            def upsert_memory(self, _record: MemoryRecord) -> None:
                pass

        existing = self._record(project_id="project-imports")
        incoming = self._record(project_id="project-imports")
        self.store.upsert(existing)
        self.store.apply_event_reconciliation(
            incoming, outcome="related", existing_memory_id=existing.memory_id, details={},
        )

        result = sync_pending_projections(store=self.store, graph=self.graph, qdrant=FakeQdrant())

        self.assertEqual(result, {"completed": 4, "failed": 0})
        edges = self.graph.list_edges(owner_id="user-1", project_id="project-imports")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["relationship_type"], "RELATED")

    def test_outbox_uses_the_explicit_graph_repository(self) -> None:
        class RecordingGraph:
            def __init__(self) -> None:
                self.memory_ids: list[str] = []

            def sync_memory(self, record: MemoryRecord) -> None:
                self.memory_ids.append(record.memory_id)

            def sync_relationship(self, _relationship: MemoryGraphRelationship) -> None:
                pass

            def remove_relationship(self, _relationship_id: str) -> None:
                pass

        class FakeQdrant:
            def upsert_memory(self, _record: MemoryRecord) -> None:
                pass

        record = MemoryRecord(
            owner_id="user-1", scope="user", kind="knowledge", status="active", user_confirmed=True,
            source_turn_id="turn-explicit-graph", payload=KnowledgeAtom(subject="A", predicate="supports", object="B"),
        )
        self.store.upsert(record)
        graph = RecordingGraph()

        result = sync_pending_projections(store=self.store, graph=graph, qdrant=FakeQdrant())

        self.assertEqual(result, {"completed": 2, "failed": 0})
        self.assertEqual(graph.memory_ids, [record.memory_id])

    def test_relationship_reads_are_scoped_to_the_requested_owner(self) -> None:
        source = self._record(owner_id="user-1", project_id="project-imports")
        target = self._record(owner_id="user-1", project_id="project-imports")
        self.store.upsert(source)
        self.store.upsert(target)
        self.store.apply_event_reconciliation(
            target, outcome="related", existing_memory_id=source.memory_id, details={},
        )

        self.assertEqual(
            self.store.list_graph_relationships(owner_id="another-owner", memory_id=source.memory_id), [],
        )
        self.assertEqual(
            len(self.store.list_graph_relationships(owner_id="user-1", memory_id=source.memory_id)), 1,
        )

    def test_graph_expansion_is_one_hop_and_project_scoped(self) -> None:
        source = self._record(project_id="project-imports")
        target = self._record(project_id="project-imports")
        other_project = self._record(project_id="project-other")
        self.graph.sync_relationship(MemoryGraphRelationship(
            "imports-link", "related", source, target, datetime.now(timezone.utc),
        ))
        self.graph.sync_relationship(MemoryGraphRelationship(
            "other-link", "related", source, other_project, datetime.now(timezone.utc),
        ))

        candidates = self.graph.expand_memory_candidates(
            seed_memory_ids=[source.memory_id], owner_id="user-1", session_id="chat-1",
            project_id="project-imports", project_scope="imports", limit=4,
        )

        self.assertEqual([(item.memory_id, item.seed_memory_id) for item in candidates], [(target.memory_id, source.memory_id)])

    def test_supersession_is_visible_as_history_but_not_expanded_for_retrieval(self) -> None:
        old = self._record(project_id="project-imports", status="superseded")
        replacement = self._record(project_id="project-imports")
        old.superseded_by = replacement.memory_id
        relationship = MemoryGraphRelationship(
            "replacement-link", "superseded_by", old, replacement, datetime.now(timezone.utc),
        )

        self.graph.sync_relationship(relationship)

        edges = self.graph.list_edges(owner_id="user-1", project_id="project-imports")
        self.assertEqual(edges[0]["relationship_type"], "SUPERSEDED_BY")
        candidates = self.graph.expand_memory_candidates(
            seed_memory_ids=[replacement.memory_id], owner_id="user-1", session_id="chat-1",
            project_id="project-imports", project_scope="imports",
        )
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
