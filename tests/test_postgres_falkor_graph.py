"""Opt-in integration coverage for the Postgres-to-Falkor graph projection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import os
import re
import unittest
import uuid

from core.memory.models import EventMemory, MemoryRecord, PreferenceMemory
from core.memory.graph_rebuild import rebuild_graph_projection
from core.memory.projections import sync_pending_projections
from core.storage.falkor_graph_store import FalkorGraphStore
from db.postgres_memory_store import PostgresMemoryRepository


RUN = os.getenv("RUN_FALKOR_INTEGRATION_TESTS") == "1"
POSTGRES_URL = os.getenv("POSTGRES_DATABASE_URL", "")
FALKOR_URL = os.getenv("FALKORDB_URL", "")


@unittest.skipUnless(
    RUN and POSTGRES_URL and FALKOR_URL and importlib.util.find_spec("psycopg") and importlib.util.find_spec("falkordb"),
    "set RUN_FALKOR_INTEGRATION_TESTS=1, POSTGRES_DATABASE_URL, and FALKORDB_URL",
)
class PostgresFalkorGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        self.schema = f"raggy_falkor_{uuid.uuid4().hex}"
        self.graph_name = f"raggy_falkor_{uuid.uuid4().hex}"
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.memories = PostgresMemoryRepository(POSTGRES_URL, schema=self.schema)
        self.graph = FalkorGraphStore(url=FALKOR_URL, graph_name=self.graph_name)

    def tearDown(self) -> None:
        import psycopg

        # reset_projection only touches this run's random named graph.
        self.graph.reset_projection()
        if re.fullmatch(r"raggy_falkor_[0-9a-f]+", self.schema):
            with psycopg.connect(POSTGRES_URL, autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

    def test_postgres_outbox_projects_and_expands_a_falkor_relationship(self) -> None:
        record = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment arrival", summary="Order EK420 arrives"),
        )
        self.memories.upsert(record)
        connected = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment delay", summary="Order EK420 has a supplier delay"),
        )
        self.memories.apply_event_reconciliation(
            connected, outcome="related", existing_memory_id=record.memory_id, details={},
        )

        result = sync_pending_projections(
            store=self.memories, graph=self.graph, targets={"graph"}, limit=10,
        )

        self.assertEqual(result, {"completed": 2, "failed": 0})
        self.assertEqual(
            {node["memory_id"] for node in self.graph.list_nodes(owner_id="owner-a", project_id="project-a")},
            {record.memory_id, connected.memory_id},
        )
        self.assertEqual(self.graph.list_nodes(owner_id="other-owner", active=None), [])
        candidates = self.graph.expand_memory_candidates(
            seed_memory_ids=[record.memory_id], owner_id="owner-a", session_id="chat-a",
            project_id="project-a", project_scope="Imports",
        )
        self.assertEqual([candidate.memory_id for candidate in candidates], [connected.memory_id])

        # Retried projections are idempotent: Cypher MERGE leaves one node and edge.
        self.graph.sync_memory(record)
        self.graph.sync_memory(record)
        relationship = self.memories.list_graph_relationships(
            owner_id="owner-a", memory_id=record.memory_id,
        )[0]
        self.graph.sync_relationship(relationship)
        self.graph.sync_relationship(relationship)
        self.assertEqual(
            len(self.graph.list_nodes(owner_id="owner-a", project_id="project-a")), 2,
        )
        self.assertEqual(
            len(self.graph.list_edges(owner_id="owner-a", project_id="project-a")), 1,
        )

    def test_expiry_removes_memory_from_active_graph_reads(self) -> None:
        record = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            valid_to=datetime.now(timezone.utc) + timedelta(minutes=5),
            payload=EventMemory(event_type="meeting", summary="Team meeting at 4pm"),
        )
        self.memories.upsert(record)
        self.assertEqual(
            sync_pending_projections(store=self.memories, graph=self.graph, targets={"graph"}),
            {"completed": 1, "failed": 0},
        )
        self.assertEqual(len(self.graph.list_nodes(owner_id="owner-a", project_id="project-a")), 1)

        self.assertEqual(
            [item.memory_id for item in self.memories.expire_due(
                now=datetime.now(timezone.utc) + timedelta(minutes=10), owner_id="owner-a",
            )],
            [record.memory_id],
        )
        self.assertEqual(
            sync_pending_projections(store=self.memories, graph=self.graph, targets={"graph"}),
            {"completed": 1, "failed": 0},
        )
        self.assertEqual(self.graph.list_nodes(owner_id="owner-a", project_id="project-a"), [])
        self.assertEqual(
            self.graph.list_nodes(owner_id="owner-a", project_id="project-a", active=None)[0]["is_active"],
            False,
        )

    def test_legacy_durable_contradiction_projects_idempotently(self) -> None:
        source = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment", summary="Order EK420 arrives Tuesday"),
        )
        target = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment", summary="Order EK420 arrives Thursday"),
        )
        self.memories.upsert(source)
        self.memories.upsert(target)
        # New correction flows use SUPERSEDED_BY.  This is a compatibility
        # fixture for an already-durable unresolved contradiction.
        with self.memories._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO memory_relationships(
                from_memory_id, to_memory_id, relationship_type, source, details_json, created_at)
                VALUES (%s, %s, 'contradiction', 'legacy_test', '{}'::jsonb, %s)""", (
                source.memory_id, target.memory_id, datetime.now(timezone.utc),
            ))

        self.assertEqual(
            sync_pending_projections(store=self.memories, graph=self.graph, targets={"graph"}),
            {"completed": 2, "failed": 0},
        )
        edges = self.graph.list_edges(owner_id="owner-a", project_id="project-a")
        self.assertEqual([edge["relationship_type"] for edge in edges], ["CONTRADICTION"])
        # A repeated relationship projection cannot duplicate the edge.
        relationship = self.memories.list_graph_relationships(owner_id="owner-a")[0]
        self.graph.sync_relationship(relationship)
        self.assertEqual(len(self.graph.list_edges(owner_id="owner-a", project_id="project-a")), 1)

    def test_supersession_projects_inactive_history_without_retrieval_edge(self) -> None:
        old = MemoryRecord(owner_id="owner-a", scope="user", kind="preference", status="active",
                           user_confirmed=True, payload=PreferenceMemory(preferred_behavior="Send updates at 4pm"))
        replacement = MemoryRecord(owner_id="owner-a", scope="user", kind="preference", status="active",
                                   user_confirmed=True, payload=PreferenceMemory(preferred_behavior="Send updates at 12pm"))
        self.memories.upsert(old)
        self.memories.upsert(replacement)
        self.memories.supersede(old.memory_id, replacement.memory_id, actor_id="owner-a")
        self.assertEqual(
            [item.relationship_type for item in self.memories.list_graph_relationships(owner_id="owner-a")],
            ["superseded_by"],
        )

        result = sync_pending_projections(store=self.memories, graph=self.graph, targets={"graph"}, limit=10)

        self.assertEqual(result, {"completed": 2, "failed": 0})
        nodes = self.graph.list_nodes(owner_id="owner-a")
        self.assertEqual([node["memory_id"] for node in nodes], [replacement.memory_id])
        edges = self.graph.list_edges(owner_id="owner-a")
        self.assertEqual(edges[0]["relationship_type"], "SUPERSEDED_BY")
        self.assertEqual(self.graph.expand_memory_candidates(
            seed_memory_ids=[replacement.memory_id], owner_id="owner-a", session_id="chat-a",
            project_id=None, project_scope=None,
        ), [])

    def test_rebuild_and_adapter_restart_restore_only_authoritative_graph_state(self) -> None:
        source = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment", summary="Order EK420 left Dubai"),
        )
        related = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="delay", summary="Order EK420 is delayed"),
        )
        self.memories.upsert(source)
        self.memories.apply_event_reconciliation(
            related, outcome="related", existing_memory_id=source.memory_id, details={},
        )
        sync_pending_projections(store=self.memories, graph=self.graph, targets={"graph"})
        self.graph.reset_projection()
        self.assertEqual(self.graph.list_nodes(owner_id="owner-a"), [])

        rebuilt = rebuild_graph_projection(store=self.memories, graph=self.graph, batch_size=10)
        self.assertEqual(rebuilt, {"scheduled": 2, "completed": 2, "failed": 0})

        restarted_adapter = FalkorGraphStore(url=FALKOR_URL, graph_name=self.graph_name)
        self.assertEqual(
            {node["memory_id"] for node in restarted_adapter.list_nodes(owner_id="owner-a", project_id="project-a")},
            {source.memory_id, related.memory_id},
        )
        self.assertEqual(
            [edge["relationship_type"] for edge in restarted_adapter.list_edges(
                owner_id="owner-a", project_id="project-a",
            )],
            ["RELATED"],
        )

    def test_project_and_owner_isolation_hold_for_graph_reads(self) -> None:
        first = MemoryRecord(
            owner_id="owner-a", scope="project", project_id="project-a", project_scope="Imports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment", summary="Order A"),
        )
        second = MemoryRecord(
            owner_id="owner-b", scope="project", project_id="project-b", project_scope="Exports",
            kind="event", status="active", user_confirmed=True, confidence=0.9,
            payload=EventMemory(event_type="shipment", summary="Order B"),
        )
        self.memories.upsert(first)
        self.memories.upsert(second)
        sync_pending_projections(store=self.memories, graph=self.graph, targets={"graph"})

        self.assertEqual(self.graph.list_nodes(owner_id="owner-a", project_id="project-b"), [])
        self.assertEqual(self.graph.list_nodes(owner_id="owner-b", project_id="project-a"), [])
        self.assertEqual(self.graph.list_edges(owner_id="owner-a", project_id="project-b"), [])
        self.assertEqual(self.graph.expand_memory_candidates(
            seed_memory_ids=[first.memory_id], owner_id="owner-a", session_id="chat-a",
            project_id="project-b", project_scope="Exports",
        ), [])


if __name__ == "__main__":
    unittest.main()
