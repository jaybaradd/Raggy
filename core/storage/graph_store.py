"""Rebuildable SQLite graph projection for active memory records.

The authoritative source remains MemoryStore. This store holds resolved nodes,
aliases, and active graph edges derived from those records.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import NAMESPACE_URL, uuid4, uuid5

from config import settings
from core.memory.models import MemoryRecord
from core.memory.relations import normalize_label, normalize_predicate, relation_family


class GraphStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._path = Path(db_path or settings.memory_db_path)
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
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    node_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
                    session_id TEXT, project_scope TEXT, node_type TEXT NOT NULL,
                    canonical_label TEXT NOT NULL, normalized_label TEXT NOT NULL,
                    attributes_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_graph_nodes_identity
                    ON graph_nodes(owner_id, scope, session_id, project_scope, normalized_label);
                CREATE TABLE IF NOT EXISTS graph_aliases (
                    alias_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    scope TEXT NOT NULL, session_id TEXT, project_scope TEXT,
                    alias TEXT NOT NULL, normalized_alias TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(node_id, normalized_alias),
                    FOREIGN KEY(node_id) REFERENCES graph_nodes(node_id)
                );
                CREATE INDEX IF NOT EXISTS idx_graph_aliases_identity
                    ON graph_aliases(owner_id, scope, session_id, project_scope, normalized_alias);
                CREATE TABLE IF NOT EXISTS graph_edges (
                    edge_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
                    session_id TEXT, project_scope TEXT, subject_node_id TEXT NOT NULL,
                    predicate TEXT NOT NULL, relation_family TEXT NOT NULL,
                    object_node_id TEXT NOT NULL, qualifiers_json TEXT NOT NULL,
                    memory_id TEXT NOT NULL UNIQUE, source_turn_id TEXT,
                    evidence_refs_json TEXT NOT NULL, provenance_kind TEXT NOT NULL,
                    confidence REAL NOT NULL, status TEXT NOT NULL,
                    valid_from TEXT NOT NULL, valid_to TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(subject_node_id) REFERENCES graph_nodes(node_id),
                    FOREIGN KEY(object_node_id) REFERENCES graph_nodes(node_id)
                );
                CREATE INDEX IF NOT EXISTS idx_graph_edges_subject
                    ON graph_edges(owner_id, scope, session_id, project_scope, subject_node_id, status);
                CREATE INDEX IF NOT EXISTS idx_graph_edges_object
                    ON graph_edges(owner_id, scope, session_id, project_scope, object_node_id, status);
            """)

    def sync_memory(self, record: MemoryRecord) -> None:
        """Project one record. Inactive records deactivate their derived edge."""
        with self._lock, self._connect() as connection:
            now = _now()
            connection.execute(
                "UPDATE graph_edges SET status = 'inactive', updated_at = ? WHERE memory_id = ?",
                (now, record.memory_id),
            )
            if record.status != "active" or not record.user_confirmed:
                return

            if record.kind == "entity":
                self._resolve_node(connection, record, record.payload.canonical_name,
                                   record.payload.entity_type, record.payload.aliases,
                                   record.payload.external_ids)
                return

            if record.kind == "knowledge":
                qualifiers = dict(record.payload.qualifiers)
                if record.payload.temporal_scope:
                    qualifiers["temporal_scope"] = record.payload.temporal_scope
                self._upsert_edge(
                    connection, record, record.payload.subject, record.payload.predicate,
                    record.payload.object, qualifiers,
                )
                return

            if record.kind == "solution":
                self._upsert_edge(
                    connection, record, f"solution:{record.memory_id}", "solves", record.payload.problem_signature,
                    {"environment": record.payload.environment, "steps": record.payload.steps,
                     "outcome": record.payload.outcome or ""},
                    subject_type="solution", object_type="problem",
                )
                return

            if record.kind == "event":
                # One event record becomes one provenance-preserving edge. Its
                # full entity/location set remains in qualifiers for later
                # graph expansion, while the first location anchors the edge.
                anchor = record.payload.locations[0] if record.payload.locations else record.payload.event_type
                self._upsert_edge(
                    connection, record, f"event:{record.memory_id}", "relates_to", anchor,
                    {"event_type": record.payload.event_type, "summary": record.payload.summary,
                     "entities": record.payload.entities, "locations": record.payload.locations,
                     "temporal_scope": record.payload.temporal_scope or ""},
                    subject_type="event", object_type="location" if record.payload.locations else "event_type",
                )
                return

            # Preferences are represented as a relationship from the owner to
            # a durable preference concept, without asserting it as a fact.
            self._upsert_edge(
                connection, record, f"user:{record.owner_id}", "prefers",
                record.payload.preferred_behavior,
                {"applicability_conditions": record.payload.applicability_conditions,
                 "strength": record.payload.strength, "consent": record.payload.consent},
                subject_type="user", object_type="preference",
            )

    def list_edges(self, *, owner_id: str, scope: str | None = None,
                   project_scope: str | None = None, status: str = "active") -> list[dict]:
        clauses = ["owner_id = ?", "status = ?"]
        params: list[object] = [owner_id, status]
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if project_scope is not None:
            clauses.append("project_scope = ?")
            params.append(project_scope)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM graph_edges WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC", params
            ).fetchall()
        return [dict(row) for row in rows]

    def list_nodes(self, *, owner_id: str, scope: str | None = None,
                   project_scope: str | None = None) -> list[dict]:
        clauses = ["owner_id = ?"]
        params: list[object] = [owner_id]
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if project_scope is not None:
            clauses.append("project_scope = ?")
            params.append(project_scope)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM graph_nodes WHERE {' AND '.join(clauses)} ORDER BY canonical_label", params
            ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_edge(self, connection: sqlite3.Connection, record: MemoryRecord,
                     subject: str, predicate: str, object_label: str, qualifiers: dict,
                     *, subject_type: str = "concept", object_type: str = "concept") -> None:
        subject_node = self._resolve_node(connection, record, subject, subject_type)
        object_node = self._resolve_node(connection, record, object_label, object_type)
        now = _now()
        provenance_kind = "evidence" if record.evidence_refs else "conversation" if record.source_turn_id else "inferred"
        edge_id = str(uuid5(NAMESPACE_URL, f"raggy:graph-edge:{record.memory_id}"))
        connection.execute("""
            INSERT INTO graph_edges (
                edge_id, owner_id, scope, session_id, project_scope, subject_node_id,
                predicate, relation_family, object_node_id, qualifiers_json, memory_id,
                source_turn_id, evidence_refs_json, provenance_kind, confidence, status,
                valid_from, valid_to, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                owner_id=excluded.owner_id, scope=excluded.scope, session_id=excluded.session_id,
                project_scope=excluded.project_scope, subject_node_id=excluded.subject_node_id,
                predicate=excluded.predicate, relation_family=excluded.relation_family,
                object_node_id=excluded.object_node_id, qualifiers_json=excluded.qualifiers_json,
                source_turn_id=excluded.source_turn_id, evidence_refs_json=excluded.evidence_refs_json,
                provenance_kind=excluded.provenance_kind, confidence=excluded.confidence,
                status='active', valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                updated_at=excluded.updated_at
        """, (
            edge_id, record.owner_id, record.scope, record.session_id, record.project_scope,
            subject_node, normalize_predicate(predicate), relation_family(predicate), object_node,
            json.dumps(qualifiers, sort_keys=True), record.memory_id, record.source_turn_id,
            json.dumps(record.evidence_refs), provenance_kind, record.confidence,
            record.valid_from.isoformat(), record.valid_to.isoformat() if record.valid_to else None,
            now, now,
        ))

    def _resolve_node(self, connection: sqlite3.Connection, record: MemoryRecord, label: str,
                      node_type: str, aliases: list[str] | None = None,
                      attributes: dict | None = None) -> str:
        normalized = normalize_label(label)
        clauses = ["owner_id = ?", "scope = ?", "COALESCE(session_id, '') = COALESCE(?, '')",
                   "COALESCE(project_scope, '') = COALESCE(?, '')"]
        params: list[object] = [record.owner_id, record.scope, record.session_id, record.project_scope]
        row = connection.execute(
            f"SELECT node_id FROM graph_nodes WHERE {' AND '.join(clauses)} AND normalized_label = ?",
            [*params, normalized],
        ).fetchone()
        if row is None:
            row = connection.execute(
                """SELECT node_id FROM graph_aliases WHERE owner_id = ? AND scope = ?
                   AND COALESCE(session_id, '') = COALESCE(?, '')
                   AND COALESCE(project_scope, '') = COALESCE(?, '') AND normalized_alias = ?""",
                [*params, normalized],
            ).fetchone()
        now = _now()
        if row is None:
            node_id = str(uuid4())
            connection.execute(
                """INSERT INTO graph_nodes (
                    node_id, owner_id, scope, session_id, project_scope, node_type,
                    canonical_label, normalized_label, attributes_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (node_id, record.owner_id, record.scope, record.session_id, record.project_scope,
                 node_type, label, normalized, json.dumps(attributes or {}, sort_keys=True), now, now),
            )
        else:
            node_id = row["node_id"]
            connection.execute("UPDATE graph_nodes SET updated_at = ? WHERE node_id = ?", (now, node_id))
        for alias in {label, *(aliases or [])}:
            normalized_alias = normalize_label(alias)
            if normalized_alias:
                connection.execute(
                    """INSERT OR IGNORE INTO graph_aliases (
                        alias_id, node_id, owner_id, scope, session_id, project_scope,
                        alias, normalized_alias, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid4()), node_id, record.owner_id, record.scope, record.session_id,
                     record.project_scope, alias, normalized_alias, now),
                )
        return node_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


graph_store = GraphStore()
