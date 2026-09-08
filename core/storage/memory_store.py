"""SQLite repository for Phase 2A memory records and audit history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config import settings
from core.memory.models import (
    EntityMemory, KnowledgeAtom, MemoryRecord, MemoryScope, MemoryStatus,
    PreferenceMemory, SolutionMemory,
)


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
                    valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_scope
                    ON memory_records(owner_id, scope, session_id, project_scope, status);
                CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_records(kind, status);
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
            """)

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
        """Claim a turn/version once; return False for an already completed/running job."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO memory_extractions (source_turn_id, extraction_version, status, created_at, updated_at) VALUES (?, ?, 'running', ?, ?)",
                (source_turn_id, extraction_version, now, now),
            )
            return cursor.rowcount == 1

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
            connection.execute("""
                INSERT INTO memory_records (
                    memory_id, owner_id, scope, session_id, project_scope, kind, status,
                    confidence, user_confirmed, evidence_refs_json, source_turn_id,
                    extraction_model, extraction_version, valid_from, valid_to, superseded_by,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    status=excluded.status, confidence=excluded.confidence,
                    user_confirmed=excluded.user_confirmed, evidence_refs_json=excluded.evidence_refs_json,
                    valid_to=excluded.valid_to, superseded_by=excluded.superseded_by,
                    payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """, self._params(record))
            connection.execute(
                "INSERT INTO memory_audit_events (memory_id, event_type, actor_id, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (record.memory_id, event_type, actor_id, json.dumps(details or {}, sort_keys=True), now.isoformat()),
            )

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
        return self._from_row(row) if row else None

    def list(self, *, owner_id: str, scope: MemoryScope | None = None,
             session_id: str | None = None, project_scope: str | None = None,
             status: MemoryStatus | None = "active", kind: str | None = None) -> list[MemoryRecord]:
        clauses = ["owner_id = ?"]
        params: list[object] = [owner_id]
        for field, value in (("scope", scope), ("session_id", session_id),
                             ("project_scope", project_scope), ("status", status), ("kind", kind)):
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC", params
            ).fetchall()
        return [self._from_row(row) for row in rows]

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
                         "solution": SolutionMemory, "entity": EntityMemory}
        parsed = payload_types[record.kind].model_validate(payload)
        record.payload = parsed
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
                record.extraction_version, record.valid_from.isoformat(),
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
            valid_from=datetime.fromisoformat(row["valid_from"]),
            valid_to=datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            superseded_by=row["superseded_by"], payload=json.loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        )


memory_store = MemoryStore()
