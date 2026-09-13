"""SQLite repository for Phase 2A memory records and audit history."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config import settings
from core.memory.models import (
    EventMemory, EntityMemory, KnowledgeAtom, MemoryRecord, MemoryScope, MemoryStatus,
    PreferenceMemory, SolutionMemory,
)
from core.memory.identity import events_are_comparable, event_differences, event_identity_key


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
                    session_id TEXT, project_scope TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
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
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(from_memory_id) REFERENCES memory_records(memory_id),
                    FOREIGN KEY(to_memory_id) REFERENCES memory_records(memory_id)
                );
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
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_records)")}
            if "identity_key" not in columns:
                connection.execute("ALTER TABLE memory_records ADD COLUMN identity_key TEXT")
            connection.execute("""CREATE INDEX IF NOT EXISTS idx_memory_event_identity
                                ON memory_records(owner_id, scope, session_id, project_scope, identity_key)""")
            for row in connection.execute("SELECT * FROM memory_records WHERE kind = 'event'").fetchall():
                record = self._from_row(row)
                connection.execute(
                    "UPDATE memory_records SET identity_key = ?, payload_json = ? WHERE memory_id = ?",
                    (event_identity_key(record), json.dumps(record.payload.model_dump(mode="json"), sort_keys=True),
                     record.memory_id),
                )
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
                    memory_id, owner_id, scope, session_id, project_scope, kind, status,
                    confidence, user_confirmed, evidence_refs_json, source_turn_id,
                    extraction_model, extraction_version, identity_key, valid_from, valid_to, superseded_by,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    owner_id=excluded.owner_id, scope=excluded.scope,
                    session_id=excluded.session_id, project_scope=excluded.project_scope,
                    status=excluded.status, confidence=excluded.confidence,
                    user_confirmed=excluded.user_confirmed, evidence_refs_json=excluded.evidence_refs_json,
                    identity_key=excluded.identity_key, valid_to=excluded.valid_to, superseded_by=excluded.superseded_by,
                    payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """, self._params(record))
        self._audit_locked(connection, record.memory_id, event_type, actor_id, details, now)
        self._enqueue_projection_locked(connection, record.memory_id, "qdrant", now.isoformat())
        self._enqueue_projection_locked(connection, record.memory_id, "graph", now.isoformat())

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
                    conflict_type = "temporal_mismatch" if "temporal_scope" in differences else "location_mismatch"
                    cursor = connection.execute(
                        """INSERT INTO memory_conflicts (
                            incoming_memory_id, existing_memory_id, conflict_type, status, details_json, created_at
                        ) VALUES (?, ?, ?, 'open', ?, ?)""",
                        (record.memory_id, comparable.memory_id, conflict_type,
                         json.dumps({"differences": differences}, sort_keys=True), now.isoformat()),
                    )
                    self._audit_locked(connection, comparable.memory_id, "conflict_detected", actor_id,
                                       {"conflict_id": cursor.lastrowid, "incoming_memory_id": record.memory_id,
                                        "differences": differences}, now)
                    return EventCaptureResult("conflict", record, cursor.lastrowid)

            record.updated_at = now
            self._upsert_locked(connection, record, event_type=event_type, actor_id=actor_id,
                                details=details, now=now)
        return EventCaptureResult("created", record)

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

    def claim_projection_jobs(self, *, limit: int = 50) -> list[dict]:
        """Claim pending projection work. Failed jobs are retried by re-enqueueing a record."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_projection_jobs WHERE status = 'pending' ORDER BY updated_at LIMIT ?", (limit,)
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

    def enqueue_all_projections(self) -> int:
        """Schedule a full rebuild of derived graph and vector projections."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            memory_ids = [row["memory_id"] for row in connection.execute(
                "SELECT memory_id FROM memory_records"
            ).fetchall()]
            for memory_id in memory_ids:
                self._enqueue_projection_locked(connection, memory_id, "qdrant", now)
                self._enqueue_projection_locked(connection, memory_id, "graph", now)
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
                    existing.status = "superseded"
                    existing.superseded_by = incoming.memory_id
                    self._upsert_locked(connection, existing, event_type="superseded", actor_id=actor_id,
                                        details={"conflict_id": conflict_id, "replacement_memory_id": incoming.memory_id}, now=now)
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

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
        return self._from_row(row) if row else None

    def get_owned(self, memory_id: str, *, owner_id: str) -> MemoryRecord | None:
        record = self.get(memory_id)
        return record if record and record.owner_id == owner_id else None

    def list(self, *, owner_id: str, scope: MemoryScope | None = None,
             session_id: str | None = None, project_scope: str | None = None,
             status: MemoryStatus | None = "active", kind: str | None = None,
             limit: int = 100) -> list[MemoryRecord]:
        clauses = ["owner_id = ?"]
        params: list[object] = [owner_id]
        for field, value in (("scope", scope), ("session_id", session_id),
                             ("project_scope", project_scope), ("status", status), ("kind", kind)):
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._from_row(row) for row in rows]

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
                    details={"replacement_memory_id": replacement.memory_id, "relationship": "contradiction"})
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO memory_relationships (from_memory_id, to_memory_id, relationship_type, created_at) VALUES (?, ?, ?, ?)",
                (record.memory_id, replacement.memory_id, "contradiction", datetime.now(timezone.utc).isoformat()),
            )
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
        return (record.memory_id, record.owner_id, record.scope, record.session_id, record.project_scope,
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
            session_id=row["session_id"], project_scope=row["project_scope"], kind=row["kind"],
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
