"""Rebuildable SQLite implementation of the conservative memory graph contract.

The authoritative memory and relationship records live elsewhere. This store
keeps lossless memory nodes and only materializes durable relationship rows; it
does not treat extracted entity strings as canonical graph facts.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config import settings
from core.memory.graph_projection import (GRAPH_PROJECTION_VERSION, MemoryGraphNode,
                                          GraphMemoryCandidate, MemoryGraphRelationship)
from core.memory.models import MemoryRecord


class GraphStore:
    """Local implementation of the backend-neutral graph repository contract."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = Path(db_path or settings.graph_db_path)
        if self._path.parent != Path("."):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS memory_graph_nodes (
                    memory_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
                    session_id TEXT, project_id TEXT, project_scope TEXT, kind TEXT NOT NULL,
                    status TEXT NOT NULL, confidence REAL NOT NULL, user_confirmed INTEGER NOT NULL,
                    valid_from TEXT NOT NULL, valid_to TEXT, payload_json TEXT NOT NULL,
                    source_turn_id TEXT, is_active INTEGER NOT NULL,
                    projection_version TEXT NOT NULL, projected_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_graph_nodes_owner_project
                    ON memory_graph_nodes(owner_id, project_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_memory_graph_nodes_owner_scope
                    ON memory_graph_nodes(owner_id, scope, session_id, project_scope, is_active);
                CREATE TABLE IF NOT EXISTS memory_graph_edges (
                    relationship_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    from_memory_id TEXT NOT NULL, to_memory_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL, created_at TEXT NOT NULL, projected_at TEXT NOT NULL,
                    UNIQUE(from_memory_id, to_memory_id, relationship_type)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_graph_edges_owner_from
                    ON memory_graph_edges(owner_id, from_memory_id);
                CREATE INDEX IF NOT EXISTS idx_memory_graph_edges_owner_to
                    ON memory_graph_edges(owner_id, to_memory_id);
            """)

    def sync_memory(self, record: MemoryRecord) -> None:
        """Upsert a memory node and remove incident edges pending reconstruction."""
        node = MemoryGraphNode.from_record(record)
        with self._lock, self._connect() as connection:
            self._upsert_node(connection, node)
            # A later sync_relationship recreates only currently eligible links.
            connection.execute(
                "DELETE FROM memory_graph_edges WHERE from_memory_id = ? OR to_memory_id = ?",
                (record.memory_id, record.memory_id),
            )

    def sync_relationship(self, relationship: MemoryGraphRelationship) -> None:
        """Project one durable link only while both endpoint records are eligible."""
        if not relationship.is_projectable:
            self.remove_relationship(relationship.relationship_id)
            return
        with self._lock, self._connect() as connection:
            self._upsert_node(connection, MemoryGraphNode.from_record(relationship.from_record))
            self._upsert_node(connection, MemoryGraphNode.from_record(relationship.to_record))
            connection.execute("""
                INSERT INTO memory_graph_edges (
                    relationship_id, owner_id, from_memory_id, to_memory_id, relationship_type, created_at, projected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relationship_id) DO UPDATE SET
                    owner_id=excluded.owner_id, from_memory_id=excluded.from_memory_id,
                    to_memory_id=excluded.to_memory_id, relationship_type=excluded.relationship_type,
                    created_at=excluded.created_at, projected_at=excluded.projected_at
            """, (
                relationship.relationship_id, relationship.from_record.owner_id,
                relationship.from_record.memory_id, relationship.to_record.memory_id,
                relationship.relationship_type.upper(), relationship.created_at.isoformat(), _now(),
            ))

    def remove_relationship(self, relationship_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM memory_graph_edges WHERE relationship_id = ?", (relationship_id,))

    def reset_projection(self) -> None:
        """Clear only this rebuildable graph projection."""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM memory_graph_edges")
            connection.execute("DELETE FROM memory_graph_nodes")

    def list_nodes(self, *, owner_id: str, scope: str | None = None,
                   project_id: str | None = None, project_scope: str | None = None,
                   active: bool | None = True) -> list[dict]:
        clauses = ["owner_id = ?"]
        params: list[object] = [owner_id]
        if active is not None:
            clauses.append("is_active = ?")
            params.append(int(active))
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        elif project_scope is not None:
            clauses.append("project_scope = ?")
            params.append(project_scope)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_graph_nodes WHERE {' AND '.join(clauses)} ORDER BY projected_at DESC", params
            ).fetchall()
        return [dict(row) for row in rows]

    def list_edges(self, *, owner_id: str, scope: str | None = None,
                   project_id: str | None = None, project_scope: str | None = None) -> list[dict]:
        clauses = ["edges.owner_id = ?", "target.is_active = 1",
                   "(source.is_active = 1 OR edges.relationship_type = 'SUPERSEDED_BY')"]
        params: list[object] = [owner_id]
        if scope is not None:
            clauses.append("source.scope = ?")
            params.append(scope)
        if project_id is not None:
            clauses.append("source.project_id = ?")
            params.append(project_id)
        elif project_scope is not None:
            clauses.append("source.project_scope = ?")
            params.append(project_scope)
        with self._connect() as connection:
            rows = connection.execute(f"""
                SELECT edges.* FROM memory_graph_edges edges
                JOIN memory_graph_nodes source ON source.memory_id = edges.from_memory_id
                JOIN memory_graph_nodes target ON target.memory_id = edges.to_memory_id
                WHERE {' AND '.join(clauses)} ORDER BY edges.projected_at DESC
            """, params).fetchall()
        return [dict(row) for row in rows]

    def expand_memory_candidates(self, *, seed_memory_ids: list[str], owner_id: str,
                                 session_id: str, project_id: str | None,
                                 project_scope: str | None, limit: int = 4) -> list[GraphMemoryCandidate]:
        """Return one-hop, active graph neighbours visible to this chat."""
        if not seed_memory_ids or limit < 1:
            return []
        seeds = list(dict.fromkeys(seed_memory_ids))[:50]
        placeholders = ", ".join("?" for _ in seeds)
        visibility, visibility_params = _visibility_clause(
            alias="candidate", session_id=session_id, project_id=project_id, project_scope=project_scope,
        )
        with self._connect() as connection:
            rows = connection.execute(f"""
                SELECT edges.relationship_id, edges.relationship_type,
                       CASE WHEN edges.from_memory_id IN ({placeholders})
                            THEN edges.to_memory_id ELSE edges.from_memory_id END AS memory_id,
                       CASE WHEN edges.from_memory_id IN ({placeholders})
                            THEN edges.from_memory_id ELSE edges.to_memory_id END AS seed_memory_id
                FROM memory_graph_edges edges
                JOIN memory_graph_nodes candidate ON candidate.memory_id =
                    CASE WHEN edges.from_memory_id IN ({placeholders})
                         THEN edges.to_memory_id ELSE edges.from_memory_id END
                WHERE edges.relationship_type = 'RELATED' AND edges.owner_id = ? AND candidate.owner_id = ? AND candidate.is_active = 1
                  AND (edges.from_memory_id IN ({placeholders}) OR edges.to_memory_id IN ({placeholders}))
                  AND {visibility}
                ORDER BY edges.projected_at DESC LIMIT ?
            """, [*seeds, *seeds, *seeds, owner_id, owner_id, *seeds, *seeds,
                   *visibility_params, limit]).fetchall()
        return [GraphMemoryCandidate(
            memory_id=row["memory_id"], seed_memory_id=row["seed_memory_id"],
            relationship_id=row["relationship_id"], relationship_type=row["relationship_type"].lower(),
        ) for row in rows]

    @staticmethod
    def _upsert_node(connection: sqlite3.Connection, node: MemoryGraphNode) -> None:
        connection.execute("""
            INSERT INTO memory_graph_nodes (
                memory_id, owner_id, scope, session_id, project_id, project_scope, kind,
                status, confidence, user_confirmed, valid_from, valid_to, payload_json,
                source_turn_id, is_active, projection_version, projected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                owner_id=excluded.owner_id, scope=excluded.scope, session_id=excluded.session_id,
                project_id=excluded.project_id, project_scope=excluded.project_scope, kind=excluded.kind,
                status=excluded.status, confidence=excluded.confidence, user_confirmed=excluded.user_confirmed,
                valid_from=excluded.valid_from, valid_to=excluded.valid_to, payload_json=excluded.payload_json,
                source_turn_id=excluded.source_turn_id, is_active=excluded.is_active,
                projection_version=excluded.projection_version, projected_at=excluded.projected_at
        """, _node_params(node, _now()))


def _node_params(node: MemoryGraphNode, projected_at: str) -> tuple[object, ...]:
    return (
        node.memory_id, node.owner_id, node.scope, node.session_id, node.project_id, node.project_scope,
        node.kind, node.status, node.confidence, int(node.user_confirmed), node.valid_from.isoformat(),
        node.valid_to.isoformat() if node.valid_to else None, node.payload_json, node.source_turn_id,
        int(node.is_active), GRAPH_PROJECTION_VERSION, projected_at,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _visibility_clause(*, alias: str, session_id: str, project_id: str | None,
                       project_scope: str | None) -> tuple[str, list[object]]:
    clauses = [f"({alias}.scope = 'session' AND {alias}.session_id = ?)"]
    params: list[object] = [session_id]
    if project_id:
        clauses.append(f"({alias}.scope = 'project' AND {alias}.project_id = ?)")
        params.append(project_id)
    if project_scope:
        clauses.append(f"({alias}.scope = 'project' AND {alias}.project_id IS NULL AND {alias}.project_scope = ?)")
        params.append(project_scope)
    return f"({' OR '.join(clauses)})", params


graph_store = GraphStore()
