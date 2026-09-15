"""SQLite repository for Phase 2A memory records and audit history."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config import settings
from db.migrations import Migration, MigrationRunner
from core.memory.models import (
    EventMemory, EntityMemory, IdentifierReference, KnowledgeAtom, MemoryRecord, MemoryScope, MemoryStatus,
    PreferenceMemory, SolutionMemory,
)
from core.memory.identity import (events_are_comparable, event_differences, event_identity_key,
                                  legacy_identifier_values)
from core.memory.graph_projection import MemoryGraphRelationship, is_memory_projectable


@dataclass(frozen=True)
class EventCaptureResult:
    outcome: str
    record: MemoryRecord
    conflict_id: int | None = None


class MemoryStore:
    """Durable local repository with an interface that can later use Postgres."""

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
                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
                    session_id TEXT, project_id TEXT, project_scope TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
                    confidence REAL NOT NULL, user_confirmed INTEGER NOT NULL,
                    evidence_refs_json TEXT NOT NULL, source_turn_id TEXT,
                    extraction_model TEXT NOT NULL, extraction_version TEXT NOT NULL,
                    identity_key TEXT, valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_scope
                    ON memory_records(owner_id, scope, session_id, project_scope, status);
                CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_records(kind, status);
                CREATE INDEX IF NOT EXISTS idx_memory_expiry
                    ON memory_records(status, valid_to);
                CREATE TABLE IF NOT EXISTS memory_audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, actor_id TEXT, details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, FOREIGN KEY(memory_id) REFERENCES memory_records(memory_id)
                );
                CREATE TABLE IF NOT EXISTS memory_extractions (
                    source_turn_id TEXT NOT NULL,
                    extraction_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_turn_id, extraction_version)
                );
                CREATE TABLE IF NOT EXISTS memory_relationships (
                    relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_memory_id TEXT NOT NULL,
                    to_memory_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'system',
                    conflict_id INTEGER,
                    created_by TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(from_memory_id) REFERENCES memory_records(memory_id),
                    FOREIGN KEY(to_memory_id) REFERENCES memory_records(memory_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_relationship
                    ON memory_relationships(from_memory_id, to_memory_id, relationship_type);
                CREATE TABLE IF NOT EXISTS memory_conflicts (
                    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incoming_memory_id TEXT NOT NULL,
                    existing_memory_id TEXT NOT NULL,
                    conflict_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    details_json TEXT NOT NULL,
                    resolved_by TEXT,
                    resolution_json TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(incoming_memory_id) REFERENCES memory_records(memory_id),
                    FOREIGN KEY(existing_memory_id) REFERENCES memory_records(memory_id),
                    UNIQUE(incoming_memory_id, existing_memory_id, status)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_conflicts_status
                    ON memory_conflicts(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS memory_access_events (
                    access_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    message_id TEXT,
                    memory_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    prompt_label TEXT,
                    rank INTEGER,
                    score REAL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_access_trace
                    ON memory_access_events(trace_id, event_type);
                CREATE TABLE IF NOT EXISTS memory_projection_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(memory_id, target),
                    FOREIGN KEY(memory_id) REFERENCES memory_records(memory_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_projection_jobs_pending
                    ON memory_projection_jobs(status, updated_at);
                CREATE TABLE IF NOT EXISTS memory_identifier_references (
                    reference_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL REFERENCES memory_records(memory_id) ON DELETE CASCADE,
                    scheme TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    raw_value TEXT NOT NULL,
                    mention TEXT,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(memory_id, scheme, normalized_value)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_identifier_lookup
                    ON memory_identifier_references(scheme, normalized_value, memory_id);
                CREATE INDEX IF NOT EXISTS idx_memory_candidate_project
                    ON memory_records(owner_id, status, user_confirmed, project_id);
                CREATE INDEX IF NOT EXISTS idx_memory_candidate_project_scope
                    ON memory_records(owner_id, status, user_confirmed, project_scope);
                CREATE INDEX IF NOT EXISTS idx_memory_candidate_session
                    ON memory_records(owner_id, status, user_confirmed, session_id);
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_records)")}
            if "project_id" not in columns:
                connection.execute("ALTER TABLE memory_records ADD COLUMN project_id TEXT")
            if "identity_key" not in columns:
                connection.execute("ALTER TABLE memory_records ADD COLUMN identity_key TEXT")
            relationship_columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_relationships)")}
            for name, definition in (
                ("source", "TEXT NOT NULL DEFAULT 'system'"), ("conflict_id", "INTEGER"),
                ("created_by", "TEXT"), ("details_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in relationship_columns:
                    connection.execute(f"ALTER TABLE memory_relationships ADD COLUMN {name} {definition}")
            connection.execute("""CREATE INDEX IF NOT EXISTS idx_memory_event_identity
                                ON memory_records(owner_id, scope, session_id, project_scope, identity_key)""")
            for row in connection.execute("SELECT * FROM memory_records WHERE kind = 'event'").fetchall():
                record = self._from_row(row)
                connection.execute(
                    "UPDATE memory_records SET identity_key = ?, payload_json = ? WHERE memory_id = ?",
                    (event_identity_key(record), json.dumps(record.payload.model_dump(mode="json"), sort_keys=True),
                     record.memory_id),
                )
            MigrationRunner("memories").apply(connection, [
                Migration(1, "legacy_memory_schema_baseline", lambda _: None),
                Migration(2, "event_identity_and_conflicts", lambda _: None),
                Migration(3, "project_id_propagation", lambda _: None),
                Migration(4, "memory_identifier_references", lambda _: None),
                Migration(5, "memory_relationship_uniqueness", lambda _: None),
                Migration(6, "relationship_provenance", lambda _: None),
            ])
    def record_access_event(self, *, trace_id: str, session_id: str, memory_id: str,
                            event_type: str, message_id: str | None = None,
                            prompt_label: str | None = None, rank: int | None = None,
                            score: float | None = None, details: dict | None = None) -> None:
        """Record retrieval/injection now; attribution can be added later."""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_access_events (
                    trace_id, session_id, message_id, memory_id, event_type,
                    prompt_label, rank, score, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trace_id, session_id, message_id, memory_id, event_type, prompt_label,
                 rank, score, json.dumps(details or {}, sort_keys=True), datetime.now(timezone.utc).isoformat()),
            )

    def claim_extraction(self, source_turn_id: str, extraction_version: str) -> bool:
        """Claim a turn/version, allowing failed jobs to be retried safely."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO memory_extractions (source_turn_id, extraction_version, status, created_at, updated_at) VALUES (?, ?, 'running', ?, ?)",
                (source_turn_id, extraction_version, now, now),
            )
            if cursor.rowcount == 1:
                return True
            existing = connection.execute(
                "SELECT status FROM memory_extractions WHERE source_turn_id = ? AND extraction_version = ?",
                (source_turn_id, extraction_version),
            ).fetchone()
            if existing and existing["status"] == "failed":
                connection.execute(
                    "UPDATE memory_extractions SET status = 'running', error = NULL, updated_at = ? WHERE source_turn_id = ? AND extraction_version = ?",
                    (now, source_turn_id, extraction_version),
                )
                return True
            return False

    def complete_extraction(self, source_turn_id: str, extraction_version: str, *, status: str,
                            error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE memory_extractions SET status = ?, error = ?, updated_at = ? WHERE source_turn_id = ? AND extraction_version = ?",
                (status, error, now, source_turn_id, extraction_version),
            )

    def upsert(self, record: MemoryRecord, *, event_type: str = "created",
               actor_id: str | None = None, details: dict | None = None) -> None:
        now = datetime.now(timezone.utc)
        record.updated_at = now
        with self._lock, self._connect() as connection:
            self._upsert_locked(connection, record, event_type=event_type, actor_id=actor_id,
                                details=details, now=now)

    def _upsert_locked(self, connection: sqlite3.Connection, record: MemoryRecord, *, event_type: str,
                       actor_id: str | None, details: dict | None, now: datetime) -> None:
        connection.execute("""
                INSERT INTO memory_records (
                    memory_id, owner_id, scope, session_id, project_id, project_scope, kind, status,
                    confidence, user_confirmed, evidence_refs_json, source_turn_id,
                    extraction_model, extraction_version, identity_key, valid_from, valid_to, superseded_by,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    owner_id=excluded.owner_id, scope=excluded.scope,
                    session_id=excluded.session_id, project_id=excluded.project_id, project_scope=excluded.project_scope,
                    status=excluded.status, confidence=excluded.confidence,
                    user_confirmed=excluded.user_confirmed, evidence_refs_json=excluded.evidence_refs_json,
                    identity_key=excluded.identity_key, valid_to=excluded.valid_to, superseded_by=excluded.superseded_by,
                    payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """, self._params(record))
        self._sync_identifier_references_locked(connection, record, now)
        self._audit_locked(connection, record.memory_id, event_type, actor_id, details, now)
        self._enqueue_projection_locked(connection, record.memory_id, "qdrant", now.isoformat())
        self._enqueue_projection_locked(connection, record.memory_id, "graph", now.isoformat())

    @staticmethod
    def _sync_identifier_references_locked(connection: sqlite3.Connection, record: MemoryRecord,
                                           now: datetime) -> None:
        """Replace explicit references atomically with their parent memory write."""
        connection.execute("DELETE FROM memory_identifier_references WHERE memory_id = ?", (record.memory_id,))
        if record.kind != "event":
            return
        references: dict[tuple[str, str], IdentifierReference] = {}
        for reference in record.payload.identifier_references:
            key = (reference.scheme, reference.normalized_value)
            if key not in references or reference.confidence > references[key].confidence:
                references[key] = reference
        connection.executemany(
            """INSERT INTO memory_identifier_references (
                memory_id, scheme, normalized_value, raw_value, mention, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (record.memory_id, reference.scheme, reference.normalized_value, reference.value,
                 reference.mention, reference.confidence, now.isoformat())
                for reference in references.values()
            ],
        )

    @staticmethod
    def _audit_locked(connection: sqlite3.Connection, memory_id: str, event_type: str,
                      actor_id: str | None, details: dict | None, now: datetime) -> None:
        connection.execute(
            "INSERT INTO memory_audit_events (memory_id, event_type, actor_id, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (memory_id, event_type, actor_id, json.dumps(details or {}, sort_keys=True), now.isoformat()),
        )

    def capture_event(self, record: MemoryRecord, *, event_type: str, details: dict | None = None,
                      actor_id: str | None = None) -> EventCaptureResult:
        """Atomically save a new event, discard an exact duplicate, or open a conflict."""
        if record.kind != "event":
            raise ValueError("capture_event requires an event memory")
        record.identity_key = event_identity_key(record)
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            session_clause = "AND COALESCE(session_id, '') = COALESCE(?, '')" if record.scope == "session" else ""
            session_params: tuple[object, ...] = (record.session_id,) if record.scope == "session" else ()
            rows = connection.execute(
                """SELECT * FROM memory_records WHERE owner_id = ? AND scope = ?
                   %s
                   AND COALESCE(project_scope, '') = COALESCE(?, '')
                   AND kind = 'event' AND status IN ('candidate', 'active')""" % session_clause,
                (record.owner_id, record.scope, *session_params, record.project_scope),
            ).fetchall()
            existing = [self._from_row(row) for row in rows]
            exact = next((item for item in existing if event_identity_key(item) == record.identity_key), None)
            if exact:
                self._audit_locked(connection, exact.memory_id, "duplicate_detected", actor_id,
                                   {"source_turn_id": record.source_turn_id, "identity_key": record.identity_key}, now)
                return EventCaptureResult("duplicate", exact)

            comparable = next((item for item in existing if events_are_comparable(item, record)), None)
            if comparable:
                differences = event_differences(comparable, record)
                if differences:
                    record.status = "candidate"
                    record.user_confirmed = False
                    record.updated_at = now
                    self._upsert_locked(connection, record, event_type="conflict_candidate_created",
                                        actor_id=actor_id, details=details, now=now)
                    cursor = connection.execute(
                        """INSERT INTO memory_conflicts (
                            incoming_memory_id, existing_memory_id, conflict_type, status, details_json, created_at
                        ) VALUES (?, ?, ?, 'open', ?, ?)""",
                        (record.memory_id, comparable.memory_id, "claim_mismatch",
                         json.dumps({"changed_claims": differences}, sort_keys=True), now.isoformat()),
                    )
                    self._audit_locked(connection, comparable.memory_id, "conflict_detected", actor_id,
                                       {"conflict_id": cursor.lastrowid, "incoming_memory_id": record.memory_id,
                                        "changed_claims": differences}, now)
                    return EventCaptureResult("conflict", record, cursor.lastrowid)

            record.updated_at = now
            self._upsert_locked(connection, record, event_type=event_type, actor_id=actor_id,
                                details=details, now=now)
        return EventCaptureResult("created", record)

    def apply_event_reconciliation(self, record: MemoryRecord, *, outcome: str,
                                   existing_memory_id: str | None, details: dict,
                                   actor_id: str | None = None) -> EventCaptureResult:
        """Atomically apply a validated post-turn reconciliation decision."""
        if record.kind != "event":
            raise ValueError("event reconciliation requires an event memory")
        if outcome not in {"new", "duplicate", "update", "related", "uncertain"}:
            raise ValueError(f"Unsupported reconciliation outcome: {outcome}")
        if outcome in {"duplicate", "update", "related"} and not existing_memory_id:
            raise ValueError(f"{outcome} reconciliation requires an existing memory")
        record.identity_key = event_identity_key(record)
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            existing = None
            if existing_memory_id:
                row = connection.execute("SELECT * FROM memory_records WHERE memory_id = ?", (existing_memory_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Memory '{existing_memory_id}' not found")
                existing = self._from_row(row)
                if existing.owner_id != record.owner_id:
                    raise PermissionError("Reconciliation candidate belongs to another owner")
            if outcome == "duplicate":
                assert existing is not None
                self._audit_locked(connection, existing.memory_id, "duplicate_detected", actor_id, details, now)
                return EventCaptureResult("duplicate", existing)
            if outcome == "update":
                assert existing is not None
                record.status, record.user_confirmed = "candidate", False
                self._upsert_locked(connection, record, event_type="conflict_candidate_created",
                                    actor_id=actor_id, details=details, now=now)
                cursor = connection.execute(
                    """INSERT INTO memory_conflicts (
                        incoming_memory_id, existing_memory_id, conflict_type, status, details_json, created_at
                    ) VALUES (?, ?, 'claim_mismatch', 'open', ?, ?)""",
                    (record.memory_id, existing.memory_id, json.dumps(details, sort_keys=True), now.isoformat()),
                )
                self._audit_locked(connection, existing.memory_id, "conflict_detected", actor_id,
                                   {"conflict_id": cursor.lastrowid, "incoming_memory_id": record.memory_id, **details}, now)
                return EventCaptureResult("conflict", record, cursor.lastrowid)
            if outcome == "uncertain":
                record.status, record.user_confirmed = "candidate", False
                event_type = "reconciliation_uncertain"
            else:
                event_type = "reconciliation_created" if outcome == "new" else "reconciliation_related"
            self._upsert_locked(connection, record, event_type=event_type, actor_id=actor_id, details=details, now=now)
            if outcome == "related":
                assert existing is not None
                self._link_locked(connection, record.memory_id, existing.memory_id, "related", now,
                                  source="reconciliation", created_by=actor_id)
            return EventCaptureResult("created" if outcome in {"new", "related"} else "uncertain", record)

    @staticmethod
    def _link_locked(connection: sqlite3.Connection, first_memory_id: str, second_memory_id: str,
                     relationship_type: str, now: datetime, *, source: str = "system",
                     conflict_id: int | None = None, created_by: str | None = None,
                     details: dict | None = None) -> None:
        """Create a stable, idempotent link for graph projection."""
        if relationship_type == "related":
            first_memory_id, second_memory_id = sorted((first_memory_id, second_memory_id))
        connection.execute(
            """INSERT OR IGNORE INTO memory_relationships
               (from_memory_id, to_memory_id, relationship_type, source, conflict_id, created_by, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (first_memory_id, second_memory_id, relationship_type, source, conflict_id, created_by,
             json.dumps(details or {}, sort_keys=True), now.isoformat()),
        )

    def list_graph_relationships(self, *, owner_id: str,
                                 memory_id: str | None = None) -> list[MemoryGraphRelationship]:
        """Return only authoritative links whose endpoint records can be traversed.

        Relationship rows remain authoritative lifecycle history; this method
        intentionally derives the smaller graph-safe view at read time.
        """
        clauses: list[str] = [
            "relationships.relationship_type IN ('related', 'contradiction', 'superseded_by')",
            "source.owner_id = ?",
        ]
        params: list[object] = [owner_id]
        if memory_id is not None:
            clauses.append("(relationships.from_memory_id = ? OR relationships.to_memory_id = ?)")
            params.extend([memory_id, memory_id])
        with self._connect() as connection:
            rows = connection.execute(f"""
                SELECT relationships.relationship_id, relationships.relationship_type,
                       relationships.created_at AS relationship_created_at,
                       source.*, target.memory_id AS target_memory_id,
                       target.owner_id AS target_owner_id, target.scope AS target_scope,
                       target.session_id AS target_session_id, target.project_id AS target_project_id,
                       target.project_scope AS target_project_scope, target.kind AS target_kind,
                       target.status AS target_status, target.confidence AS target_confidence,
                       target.user_confirmed AS target_user_confirmed,
                       target.evidence_refs_json AS target_evidence_refs_json,
                       target.source_turn_id AS target_source_turn_id,
                       target.extraction_model AS target_extraction_model,
                       target.extraction_version AS target_extraction_version,
                       target.identity_key AS target_identity_key, target.valid_from AS target_valid_from,
                       target.valid_to AS target_valid_to, target.superseded_by AS target_superseded_by,
                       target.payload_json AS target_payload_json, target.created_at AS target_created_at,
                       target.updated_at AS target_updated_at
                FROM memory_relationships relationships
                JOIN memory_records source ON source.memory_id = relationships.from_memory_id
                JOIN memory_records target ON target.memory_id = relationships.to_memory_id
                WHERE {' AND '.join(clauses)}
                ORDER BY relationships.relationship_id
            """, params).fetchall()
        relationships: list[MemoryGraphRelationship] = []
        for row in rows:
            source = self._from_row(row)
            target = MemoryRecord.model_validate({
                "memory_id": row["target_memory_id"], "owner_id": row["target_owner_id"],
                "scope": row["target_scope"], "session_id": row["target_session_id"],
                "project_id": row["target_project_id"], "project_scope": row["target_project_scope"],
                "kind": row["target_kind"], "status": row["target_status"],
                "confidence": row["target_confidence"], "user_confirmed": row["target_user_confirmed"],
                "evidence_refs": json.loads(row["target_evidence_refs_json"]),
                "source_turn_id": row["target_source_turn_id"],
                "extraction_model": row["target_extraction_model"],
                "extraction_version": row["target_extraction_version"],
                "identity_key": row["target_identity_key"], "valid_from": row["target_valid_from"],
                "valid_to": row["target_valid_to"], "superseded_by": row["target_superseded_by"],
                "payload": json.loads(row["target_payload_json"]),
                "created_at": row["target_created_at"], "updated_at": row["target_updated_at"],
            })
            relationship = MemoryGraphRelationship(
                relationship_id=str(row["relationship_id"]), relationship_type=row["relationship_type"],
                from_record=source, to_record=target,
                created_at=datetime.fromisoformat(row["relationship_created_at"]),
            )
            if not relationship.is_projectable:
                continue
            relationships.append(relationship)
        return relationships

    def list_relationships(self, memory_id: str, *, owner_id: str) -> list[dict]:
        if self.get_owned(memory_id, owner_id=owner_id) is None:
            raise KeyError(f"Memory '{memory_id}' not found")
        with self._connect() as connection:
            rows = connection.execute("""SELECT relationship_id, from_memory_id, to_memory_id,
                relationship_type, source, conflict_id, created_by, details_json, created_at
                FROM memory_relationships WHERE from_memory_id=? OR to_memory_id=? ORDER BY relationship_id DESC""",
                (memory_id, memory_id)).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"] or "{}"),
                 "peer_memory_id": row["to_memory_id"] if row["from_memory_id"] == memory_id else row["from_memory_id"]}
                for row in rows]

    @staticmethod
    def _enqueue_projection_locked(connection: sqlite3.Connection, memory_id: str,
                                   target: str, now: str) -> None:
        """Queue a repairable projection update in the same transaction as the record."""
        connection.execute(
            """
            INSERT INTO memory_projection_jobs (
                memory_id, target, status, attempts, error, created_at, updated_at
            ) VALUES (?, ?, 'pending', 0, NULL, ?, ?)
            ON CONFLICT(memory_id, target) DO UPDATE SET
                status='pending', error=NULL, updated_at=excluded.updated_at
            """,
            (memory_id, target, now, now),
        )

    def claim_projection_jobs(self, *, limit: int = 50,
                              targets: set[str] | None = None) -> list[dict]:
        """Claim pending projection work. Failed jobs are retried by re-enqueueing a record."""
        now = datetime.now(timezone.utc).isoformat()
        target_clause = ""
        target_params: list[object] = []
        if targets is not None:
            if not targets or not targets <= {"qdrant", "graph"}:
                raise ValueError("targets must contain qdrant and/or graph")
            placeholders = ", ".join("?" for _ in targets)
            target_clause = f" AND target IN ({placeholders})"
            target_params = sorted(targets)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_projection_jobs WHERE status = 'pending'{target_clause} ORDER BY updated_at LIMIT ?",
                [*target_params, limit],
            ).fetchall()
            jobs = [dict(row) for row in rows]
            for job in jobs:
                connection.execute(
                    "UPDATE memory_projection_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE job_id = ?",
                    (now, job["job_id"]),
                )
        return jobs

    def complete_projection_job(self, job_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE memory_projection_jobs SET status = 'completed', error = NULL, updated_at = ? WHERE job_id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )

    def fail_projection_job(self, job_id: int, error: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE memory_projection_jobs SET status = 'failed', error = ?, updated_at = ? WHERE job_id = ?",
                (error, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def requeue_failed_projections(self) -> int:
        """Make failed projection work retryable without mutating authoritative memories."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE memory_projection_jobs SET status = 'pending', error = NULL, updated_at = ? WHERE status = 'failed'",
                (now,),
            )
        return cursor.rowcount

    def expire_due(self, *, now: datetime | None = None, owner_id: str | None = None) -> list[MemoryRecord]:
        """Expire due active memories and enqueue projection removal atomically.

        Re-running this operation is safe: only active records whose validity
        deadline has passed are selected, and each receives one audit entry.
        """
        sweep_time = now or datetime.now(timezone.utc)
        clauses = ["status = 'active'", "valid_to IS NOT NULL", "valid_to <= ?"]
        params: list[object] = [sweep_time.isoformat()]
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} ORDER BY valid_to", params
            ).fetchall()
            expired: list[MemoryRecord] = []
            for row in rows:
                record = self._from_row(row)
                original_valid_to = record.valid_to
                record.status = "expired"
                record.updated_at = sweep_time
                self._upsert_locked(
                    connection, record, event_type="expired_by_sweep", actor_id="system",
                    details={"expired_at": sweep_time.isoformat(),
                             "valid_to": original_valid_to.isoformat() if original_valid_to else None},
                    now=sweep_time,
                )
                expired.append(record)
        return expired

    def enqueue_all_projections(self, *, targets: tuple[str, ...] = ("qdrant", "graph")) -> int:
        """Schedule one or more full derived-projection rebuilds."""
        if not targets or not set(targets) <= {"qdrant", "graph"}:
            raise ValueError("targets must contain qdrant and/or graph")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            memory_ids = [row["memory_id"] for row in connection.execute(
                "SELECT memory_id FROM memory_records"
            ).fetchall()]
            for memory_id in memory_ids:
                for target in targets:
                    self._enqueue_projection_locked(connection, memory_id, target, now)
        return len(memory_ids)

    def list_projection_jobs(self, *, owner_id: str, status: str | None = None,
                             limit: int = 100) -> list[dict]:
        clauses = ["records.owner_id = ?"]
        params: list[object] = [owner_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT jobs.* FROM memory_projection_jobs AS jobs
                    JOIN memory_records AS records ON records.memory_id = jobs.memory_id
                    {where.replace('status', 'jobs.status')}
                    ORDER BY jobs.updated_at DESC LIMIT ?""",
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def list_conflicts(self, *, owner_id: str, status: str | None = "open") -> list[dict]:
        clauses = ["incoming.owner_id = ?"]
        params: list[object] = [owner_id]
        if status is not None:
            clauses.append("conflicts.status = ?")
            params.append(status)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT conflicts.*, incoming.owner_id, incoming.project_scope FROM memory_conflicts AS conflicts
                    JOIN memory_records AS incoming ON incoming.memory_id = conflicts.incoming_memory_id
                    WHERE {' AND '.join(clauses)} ORDER BY conflicts.created_at DESC""", params
            ).fetchall()
        return [self._conflict_from_row(row) for row in rows]

    def get_conflict(self, conflict_id: int, *, owner_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT conflicts.*, incoming.owner_id, incoming.project_scope FROM memory_conflicts AS conflicts
                   JOIN memory_records AS incoming ON incoming.memory_id = conflicts.incoming_memory_id
                   WHERE conflicts.conflict_id = ? AND incoming.owner_id = ?""",
                (conflict_id, owner_id),
            ).fetchone()
        return self._conflict_from_row(row) if row else None

    def resolve_conflict(self, conflict_id: int, *, action: str, actor_id: str) -> dict:
        if action not in {"supersede_existing", "keep_existing", "expire_existing"}:
            raise ValueError("Unsupported conflict resolution")
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT conflicts.*, incoming.owner_id FROM memory_conflicts AS conflicts
                   JOIN memory_records AS incoming ON incoming.memory_id = conflicts.incoming_memory_id
                   WHERE conflicts.conflict_id = ? AND incoming.owner_id = ? AND conflicts.status = 'open'""",
                (conflict_id, actor_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Open conflict '{conflict_id}' not found")
            conflict = self._conflict_from_row(row)
            incoming = self._from_row(connection.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?", (conflict["incoming_memory_id"],)
            ).fetchone())
            existing = self._from_row(connection.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?", (conflict["existing_memory_id"],)
            ).fetchone())
            if action == "keep_existing":
                incoming.status = "rejected"
                self._upsert_locked(connection, incoming, event_type="conflict_rejected", actor_id=actor_id,
                                    details={"conflict_id": conflict_id}, now=now)
            else:
                incoming.status = "active"
                incoming.user_confirmed = True
                self._upsert_locked(connection, incoming, event_type="conflict_accepted", actor_id=actor_id,
                                    details={"conflict_id": conflict_id}, now=now)
                if action == "supersede_existing":
                    self._supersede_locked(connection, existing, incoming, actor_id=actor_id, now=now,
                                           source="conflict_resolution", conflict_id=conflict_id)
                else:
                    existing.status = "expired"
                    existing.valid_to = now
                    self._upsert_locked(connection, existing, event_type="expired", actor_id=actor_id,
                                        details={"conflict_id": conflict_id}, now=now)
            resolution = {"action": action, "incoming_memory_id": incoming.memory_id,
                          "existing_memory_id": existing.memory_id}
            connection.execute(
                """UPDATE memory_conflicts SET status = 'resolved', resolved_by = ?, resolution_json = ?,
                   resolved_at = ? WHERE conflict_id = ?""",
                (actor_id, json.dumps(resolution, sort_keys=True), now.isoformat(), conflict_id),
            )
        return self.get_conflict(conflict_id, owner_id=actor_id) or conflict

    def _supersede_locked(self, connection: sqlite3.Connection, existing: MemoryRecord,
                          replacement: MemoryRecord, *, actor_id: str, now: datetime,
                          source: str, conflict_id: int | None = None) -> None:
        existing.status, existing.superseded_by = "superseded", replacement.memory_id
        details = {"replacement_memory_id": replacement.memory_id, "relationship": "superseded_by"}
        if conflict_id is not None:
            details["conflict_id"] = conflict_id
        self._upsert_locked(connection, existing, event_type="superseded", actor_id=actor_id,
                            details=details, now=now)
        self._link_locked(connection, existing.memory_id, replacement.memory_id, "superseded_by", now,
                          source=source, conflict_id=conflict_id, created_by=actor_id, details=details)
        self._audit_locked(connection, replacement.memory_id, "replacement_linked", actor_id,
                           {"replaces_memory_id": existing.memory_id, **details}, now)

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
        return self._from_row(row) if row else None

    def get_owned(self, memory_id: str, *, owner_id: str) -> MemoryRecord | None:
        record = self.get(memory_id)
        return record if record and record.owner_id == owner_id else None

    @staticmethod
    def _candidate_scope_clause(*, session_id: str, project_id: str | None,
                                project_scope: str | None) -> tuple[str, list[object]]:
        clauses = ["(records.scope = 'session' AND records.session_id = ?)", "records.scope = 'user'"]
        params: list[object] = [session_id]
        if project_id:
            clauses.append("(records.scope = 'project' AND records.project_id = ?)")
            params.append(project_id)
        # Name matching is solely for legacy records that predate stable IDs.
        if project_scope:
            clauses.append("(records.scope = 'project' AND records.project_id IS NULL AND records.project_scope = ?)")
            params.append(project_scope)
        return f"({' OR '.join(clauses)})", params

    def find_memory_candidates(
        self,
        *,
        owner_id: str,
        session_id: str,
        project_id: str | None,
        project_scope: str | None,
        identifier_references: list[IdentifierReference],
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Find exact, currently eligible memory candidates by stable reference.

        Indexed explicit references are returned first. A bounded scan of
        legacy event entities preserves read compatibility without rewriting
        old records into the new identifier table.
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        reference_keys = sorted({(reference.scheme, reference.normalized_value) for reference in identifier_references})
        if not reference_keys:
            return []
        now = datetime.now(timezone.utc).isoformat()
        scope_clause, scope_params = self._candidate_scope_clause(
            session_id=session_id, project_id=project_id, project_scope=project_scope,
        )
        reference_match_clause = " OR ".join(
            "(refs.scheme = ? AND refs.normalized_value = ?)" for _ in reference_keys
        )
        reference_match_params = [value for key in reference_keys for value in key]
        base_where = f"""records.owner_id = ? AND records.status = 'active'
            AND records.user_confirmed = 1 AND records.valid_from <= ?
            AND (records.valid_to IS NULL OR records.valid_to > ?) AND {scope_clause}"""
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT records.*, COUNT(DISTINCT refs.normalized_value) AS match_count
                    FROM memory_identifier_references AS refs
                    JOIN memory_records AS records ON records.memory_id = refs.memory_id
                    WHERE {base_where} AND ({reference_match_clause})
                    GROUP BY records.memory_id
                    ORDER BY match_count DESC, records.updated_at DESC LIMIT ?""",
                [owner_id, now, now, *scope_params, *reference_match_params, limit],
            ).fetchall()
            candidates = [self._from_row(row) for row in rows]
            if len(candidates) >= limit:
                return candidates

            # Legacy rows have no explicit references. Their entity strings are
            # scanned only after indexed lookup and only within the same scope.
            legacy_rows = connection.execute(
                f"""SELECT records.* FROM memory_records AS records
                    WHERE {base_where} AND records.kind = 'event'
                    AND NOT EXISTS (
                        SELECT 1 FROM memory_identifier_references AS refs
                        WHERE refs.memory_id = records.memory_id
                    )
                    ORDER BY records.updated_at DESC LIMIT 500""",
                [owner_id, now, now, *scope_params],
            ).fetchall()
        known_ids = {record.memory_id for record in candidates}
        # Legacy entity strings do not carry a scheme. They are compatible only
        # with the default general-purpose external-reference scheme.
        wanted = {value for scheme, value in reference_keys if scheme == "external_reference"}
        if not wanted:
            return candidates
        for row in legacy_rows:
            record = self._from_row(row)
            if record.memory_id in known_ids or not isinstance(record.payload, EventMemory):
                continue
            if legacy_identifier_values(record.payload) & wanted:
                candidates.append(record)
                known_ids.add(record.memory_id)
                if len(candidates) == limit:
                    break
        return candidates

    def list(self, *, owner_id: str, scope: MemoryScope | None = None,
             session_id: str | None = None, project_id: str | None = None, project_scope: str | None = None,
             status: MemoryStatus | None = "active", kind: str | None = None,
             limit: int = 100) -> list[MemoryRecord]:
        clauses = ["owner_id = ?"]
        params: list[object] = [owner_id]
        for field, value in (("scope", scope), ("session_id", session_id),
                             ("project_id", project_id), ("project_scope", project_scope), ("status", status), ("kind", kind)):
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def backfill_project_ids(self, projects: dict[tuple[str, str], dict]) -> dict[str, int]:
        """Attach IDs only when a legacy owner/name match is unambiguous.

        Project creation is deliberately forbidden here: the session repository
        is authoritative for project entities.
        """
        updated = unmatched = 0
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            rows = connection.execute("""SELECT memory_id, owner_id, project_scope FROM memory_records
                WHERE scope = 'project' AND project_id IS NULL AND project_scope IS NOT NULL""").fetchall()
            for row in rows:
                normalized = " ".join(row["project_scope"].split()).casefold()
                project = projects.get((row["owner_id"], normalized))
                if project is None:
                    unmatched += 1
                    continue
                connection.execute("UPDATE memory_records SET project_id = ?, updated_at = ? WHERE memory_id = ?",
                                   (project["project_id"], now.isoformat(), row["memory_id"]))
                self._audit_locked(connection, row["memory_id"], "project_id_backfilled", "system",
                                   {"project_id": project["project_id"], "project_scope": row["project_scope"]}, now)
                self._enqueue_projection_locked(connection, row["memory_id"], "qdrant", now.isoformat())
                self._enqueue_projection_locked(connection, row["memory_id"], "graph", now.isoformat())
                updated += 1
        return {"updated": updated, "unmatched": unmatched}

    def list_audit_events(self, memory_id: str, *, owner_id: str, limit: int = 100) -> list[dict]:
        if self.get_owned(memory_id, owner_id=owner_id) is None:
            raise KeyError(f"Memory '{memory_id}' not found")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT event_id, event_type, actor_id, details_json, created_at
                   FROM memory_audit_events WHERE memory_id = ? ORDER BY event_id DESC LIMIT ?""",
                (memory_id, limit),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def list_access_events(self, memory_id: str, *, owner_id: str, limit: int = 100) -> list[dict]:
        if self.get_owned(memory_id, owner_id=owner_id) is None:
            raise KeyError(f"Memory '{memory_id}' not found")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT trace_id, session_id, message_id, event_type, prompt_label, rank, score,
                          details_json, created_at
                   FROM memory_access_events WHERE memory_id = ? ORDER BY access_event_id DESC LIMIT ?""",
                (memory_id, limit),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def transition(self, memory_id: str, status: MemoryStatus, *, actor_id: str | None = None,
                   superseded_by: str | None = None, event_type: str | None = None) -> MemoryRecord:
        record = self.get(memory_id)
        if record is None:
            raise KeyError(f"Memory '{memory_id}' not found")
        record.status = status
        record.superseded_by = superseded_by
        self.upsert(record, event_type=event_type or f"status_{status}", actor_id=actor_id)
        return record

    def review(self, memory_id: str, action: str, *, actor_id: str = "default") -> MemoryRecord:
        record = self._owned(memory_id, actor_id)
        if record.status not in {"candidate", "active"}:
            raise ValueError(f"Cannot review a {record.status} memory")
        if action == "confirm":
            record.user_confirmed = True
            status: MemoryStatus = "active"
        elif action == "reject":
            status: MemoryStatus = "rejected"
        elif action == "expire":
            status = "expired"
        else:
            raise ValueError(f"Unsupported memory review action: {action}")
        record.status = status
        if action == "expire":
            record.valid_to = datetime.now(timezone.utc)
        self.upsert(record, event_type=action, actor_id=actor_id)
        return record

    def forget(self, memory_id: str, *, actor_id: str = "default") -> MemoryRecord:
        """Soft-delete a memory while retaining provenance and lifecycle audit history."""
        record = self._owned(memory_id, actor_id)
        if record.status == "deleted":
            return record
        record.status = "deleted"
        self.upsert(record, event_type="deleted_by_user", actor_id=actor_id)
        return record

    def promote(self, memory_id: str, *, scope: MemoryScope, project_scope: str | None = None,
                actor_id: str = "default") -> MemoryRecord:
        record = self._owned(memory_id, actor_id)
        if record.status not in {"candidate", "active"}:
            raise ValueError(f"Cannot promote a {record.status} memory")
        if scope not in {"user", "project"}:
            raise ValueError("Promotion target must be 'user' or 'project'")
        if scope == "project" and not project_scope:
            raise ValueError("project promotion requires project_scope")
        previous_scope = record.scope
        record.scope = scope
        record.project_scope = project_scope if scope == "project" else None
        record.user_confirmed = True
        record.status = "active"
        self.upsert(record, event_type="promoted", actor_id=actor_id,
                    details={"from_scope": previous_scope, "to_scope": scope, "project_scope": project_scope})
        return record

    def edit(self, memory_id: str, payload: dict, *, actor_id: str = "default") -> MemoryRecord:
        record = self._owned(memory_id, actor_id)
        payload_types = {"knowledge": KnowledgeAtom, "preference": PreferenceMemory,
                         "solution": SolutionMemory, "entity": EntityMemory,
                         "event": EventMemory}
        parsed = payload_types[record.kind].model_validate(payload)
        record.payload = parsed
        if record.kind == "event":
            record.identity_key = event_identity_key(record)
        self.upsert(record, event_type="edited", actor_id=actor_id)
        return record

    def supersede(self, memory_id: str, replacement_id: str, *, actor_id: str = "default") -> MemoryRecord:
        record = self._owned(memory_id, actor_id)
        replacement = self._owned(replacement_id, actor_id)
        if record.memory_id == replacement.memory_id:
            raise ValueError("A memory cannot supersede itself")
        record.status = "superseded"
        record.superseded_by = replacement.memory_id
        self.upsert(record, event_type="superseded", actor_id=actor_id,
                    details={"replacement_memory_id": replacement.memory_id, "relationship": "superseded_by"})
        with self._lock, self._connect() as connection:
            self._link_locked(connection, record.memory_id, replacement.memory_id, "superseded_by", datetime.now(timezone.utc),
                              source="manual_supersede", created_by=actor_id,
                              details={"replacement_memory_id": replacement.memory_id})
        return record

    def _owned(self, memory_id: str, owner_id: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record is None:
            raise KeyError(f"Memory '{memory_id}' not found")
        if record.owner_id != owner_id:
            raise PermissionError("Memory does not belong to this owner")
        return record

    @staticmethod
    def _params(record: MemoryRecord) -> tuple[object, ...]:
        return (record.memory_id, record.owner_id, record.scope, record.session_id, record.project_id, record.project_scope,
                record.kind, record.status, record.confidence, int(record.user_confirmed),
                json.dumps(record.evidence_refs), record.source_turn_id, record.extraction_model,
                record.extraction_version, record.identity_key, record.valid_from.isoformat(),
                record.valid_to.isoformat() if record.valid_to else None, record.superseded_by,
                json.dumps(record.payload.model_dump(mode="json"), sort_keys=True),
                record.created_at.isoformat(), record.updated_at.isoformat())

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"], owner_id=row["owner_id"], scope=row["scope"],
            session_id=row["session_id"], project_id=row["project_id"], project_scope=row["project_scope"], kind=row["kind"],
            status=row["status"], confidence=row["confidence"], user_confirmed=bool(row["user_confirmed"]),
            evidence_refs=json.loads(row["evidence_refs_json"]), source_turn_id=row["source_turn_id"],
            extraction_model=row["extraction_model"], extraction_version=row["extraction_version"],
            identity_key=row["identity_key"],
            valid_from=datetime.fromisoformat(row["valid_from"]),
            valid_to=datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            superseded_by=row["superseded_by"], payload=json.loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _conflict_from_row(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        resolution = result.pop("resolution_json", None)
        if resolution:
            result["resolution"] = json.loads(resolution)
        result.pop("owner_id", None)
        return result


memory_store = MemoryStore()
