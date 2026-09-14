"""PostgreSQL implementation of the authoritative memory repository.

The relational record, lifecycle state, audit trail, and outbox are committed
together. Vector and graph stores consume the outbox as rebuildable projections.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.memory.identity import (event_comparison_key, event_differences, event_identity_key,
                                  events_are_comparable, legacy_identifier_values)
from core.memory.models import (EntityMemory, EventMemory, IdentifierReference, KnowledgeAtom, MemoryRecord,
                                MemoryScope, MemoryStatus, PreferenceMemory, SolutionMemory)
from db.postgres_migrations import PostgresMigrationRunner, memory_migrations


@dataclass(frozen=True)
class EventCaptureResult:
    outcome: str
    record: MemoryRecord
    conflict_id: int | None = None


class PostgresMemoryRepository:
    """Postgres memory lifecycle repository; construct directly until full cutover."""

    def __init__(self, database_url: str, *, schema: str | None = None, worker_id: str | None = None) -> None:
        if not database_url:
            raise ValueError("database_url must not be blank")
        if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("schema must be a simple PostgreSQL identifier")
        self._database_url, self._schema = database_url, schema
        self._worker_id = worker_id or f"memory-worker-{uuid.uuid4()}"
        self._initialise()

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("psycopg is required for the PostgreSQL memory repository") from error
        connection = psycopg.connect(self._database_url, row_factory=dict_row)
        if self._schema:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('search_path', %s, false)", (f"{self._schema},public",))
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            PostgresMigrationRunner("memories").apply(connection, memory_migrations())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _event_domain_key(record: MemoryRecord) -> str:
        """Lock comparable events together, intentionally excluding temporal value."""
        key = event_comparison_key(record)
        return "\x1f".join(key or (record.owner_id, record.scope, record.project_id or record.project_scope or "", record.memory_id))

    @staticmethod
    def _record_from_row(row: dict[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"], owner_id=row["owner_id"], scope=row["scope"], session_id=row["session_id"],
            project_id=row["project_id"], project_scope=row["project_scope"], kind=row["kind"], status=row["status"],
            confidence=row["confidence"], user_confirmed=row["user_confirmed"], evidence_refs=row["evidence_refs_json"] or [],
            source_turn_id=row["source_turn_id"], extraction_model=row["extraction_model"], extraction_version=row["extraction_version"],
            identity_key=row["identity_key"], valid_from=row["valid_from"], valid_to=row["valid_to"],
            superseded_by=row["superseded_by"], payload=row["payload_json"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _params(record: MemoryRecord) -> tuple[Any, ...]:
        return (record.memory_id, record.owner_id, record.scope, record.session_id, record.project_id, record.project_scope,
                record.kind, record.status, record.confidence, record.user_confirmed, json.dumps(record.evidence_refs),
                record.source_turn_id, record.extraction_model, record.extraction_version, record.identity_key,
                record.valid_from, record.valid_to, record.superseded_by,
                json.dumps(record.payload.model_dump(mode="json"), sort_keys=True), record.created_at, record.updated_at)

    def _audit(self, cursor: Any, memory_id: str, event_type: str, actor_id: str | None,
               details: dict | None, now: datetime) -> None:
        cursor.execute("""INSERT INTO memory_audit_events(memory_id, event_type, actor_id, details_json, created_at)
            VALUES (%s, %s, %s, %s::jsonb, %s)""", (memory_id, event_type, actor_id, json.dumps(details or {}, sort_keys=True), now))

    def _enqueue(self, cursor: Any, memory_id: str, target: str, now: datetime) -> None:
        cursor.execute("""INSERT INTO memory_projection_jobs(memory_id, target, status, attempts, error, created_at, updated_at)
            VALUES (%s, %s, 'pending', 0, NULL, %s, %s)
            ON CONFLICT(memory_id, target) DO UPDATE SET status='pending', error=NULL, locked_at=NULL,
                locked_by=NULL, updated_at=EXCLUDED.updated_at""", (memory_id, target, now, now))

    @staticmethod
    def _sync_identifier_references(cursor: Any, record: MemoryRecord, now: datetime) -> None:
        """Replace explicit identifiers in the same transaction as the memory row."""
        cursor.execute("DELETE FROM memory_identifier_references WHERE memory_id=%s", (record.memory_id,))
        if record.kind != "event":
            return
        references: dict[tuple[str, str], IdentifierReference] = {}
        for reference in record.payload.identifier_references:
            key = (reference.scheme, reference.normalized_value)
            if key not in references or reference.confidence > references[key].confidence:
                references[key] = reference
        for reference in references.values():
            cursor.execute("""INSERT INTO memory_identifier_references(
                memory_id, scheme, normalized_value, raw_value, mention, confidence, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (record.memory_id, reference.scheme, reference.normalized_value, reference.value,
                 reference.mention, reference.confidence, now))

    def _upsert(self, cursor: Any, record: MemoryRecord, *, event_type: str, actor_id: str | None,
                details: dict | None, now: datetime) -> None:
        record.updated_at = now
        cursor.execute("""INSERT INTO memory_records(
            memory_id, owner_id, scope, session_id, project_id, project_scope, kind, status, confidence,
            user_confirmed, evidence_refs_json, source_turn_id, extraction_model, extraction_version, identity_key,
            valid_from, valid_to, superseded_by, payload_json, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT(memory_id) DO UPDATE SET owner_id=EXCLUDED.owner_id, scope=EXCLUDED.scope,
            session_id=EXCLUDED.session_id, project_id=EXCLUDED.project_id, project_scope=EXCLUDED.project_scope,
            status=EXCLUDED.status, confidence=EXCLUDED.confidence, user_confirmed=EXCLUDED.user_confirmed,
            evidence_refs_json=EXCLUDED.evidence_refs_json, identity_key=EXCLUDED.identity_key, valid_to=EXCLUDED.valid_to,
            superseded_by=EXCLUDED.superseded_by, payload_json=EXCLUDED.payload_json, updated_at=EXCLUDED.updated_at""",
            self._params(record))
        self._sync_identifier_references(cursor, record, now)
        self._audit(cursor, record.memory_id, event_type, actor_id, details, now)
        self._enqueue(cursor, record.memory_id, "qdrant", now)
        self._enqueue(cursor, record.memory_id, "graph", now)

    def upsert(self, record: MemoryRecord, *, event_type: str = "created", actor_id: str | None = None,
               details: dict | None = None) -> None:
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            self._upsert(cursor, record, event_type=event_type, actor_id=actor_id, details=details, now=now)

    def record_access_event(self, *, trace_id: str, session_id: str, memory_id: str, event_type: str,
                            message_id: str | None = None, prompt_label: str | None = None, rank: int | None = None,
                            score: float | None = None, details: dict | None = None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO memory_access_events(trace_id, session_id, message_id, memory_id, event_type,
                prompt_label, rank, score, details_json, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (trace_id, session_id, message_id, memory_id, event_type, prompt_label, rank, score,
                 json.dumps(details or {}, sort_keys=True), self._now()))

    def claim_extraction(self, source_turn_id: str, extraction_version: str) -> bool:
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO memory_extractions(source_turn_id, extraction_version, status, created_at, updated_at)
                VALUES (%s,%s,'running',%s,%s) ON CONFLICT(source_turn_id, extraction_version) DO NOTHING
                RETURNING source_turn_id""", (source_turn_id, extraction_version, now, now))
            if cursor.fetchone(): return True
            cursor.execute("""UPDATE memory_extractions SET status='running', error=NULL, updated_at=%s
                WHERE source_turn_id=%s AND extraction_version=%s AND status='failed' RETURNING source_turn_id""",
                (now, source_turn_id, extraction_version))
            return cursor.fetchone() is not None

    def complete_extraction(self, source_turn_id: str, extraction_version: str, *, status: str, error: str | None = None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE memory_extractions SET status=%s, error=%s, updated_at=%s WHERE source_turn_id=%s AND extraction_version=%s",
                           (status, error, self._now(), source_turn_id, extraction_version))

    def capture_event(self, record: MemoryRecord, *, event_type: str, details: dict | None = None,
                      actor_id: str | None = None) -> EventCaptureResult:
        if record.kind != "event": raise ValueError("capture_event requires an event memory")
        record.identity_key, now = event_identity_key(record), self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (self._event_domain_key(record),))
            session_clause = "AND COALESCE(session_id, '') = COALESCE(%s, '')" if record.scope == "session" else ""
            session_params: tuple[Any, ...] = (record.session_id,) if record.scope == "session" else ()
            cursor.execute(f"""SELECT * FROM memory_records WHERE owner_id=%s AND scope=%s {session_clause}
                AND COALESCE(project_id, project_scope, '') = COALESCE(%s, %s, '')
                AND kind='event' AND status IN ('candidate','active') FOR UPDATE""",
                (record.owner_id, record.scope, *session_params, record.project_id, record.project_scope))
            existing = [self._record_from_row(row) for row in cursor.fetchall()]
            exact = next((item for item in existing if event_identity_key(item) == record.identity_key), None)
            if exact:
                self._audit(cursor, exact.memory_id, "duplicate_detected", actor_id,
                            {"source_turn_id": record.source_turn_id, "identity_key": record.identity_key}, now)
                return EventCaptureResult("duplicate", exact)
            comparable = next((item for item in existing if events_are_comparable(item, record)), None)
            if comparable and (differences := event_differences(comparable, record)):
                record.status, record.user_confirmed = "candidate", False
                self._upsert(cursor, record, event_type="conflict_candidate_created", actor_id=actor_id, details=details, now=now)
                cursor.execute("""INSERT INTO memory_conflicts(incoming_memory_id, existing_memory_id, conflict_type, status, details_json, created_at)
                    VALUES (%s,%s,%s,'open',%s::jsonb,%s) RETURNING conflict_id""",
                    (record.memory_id, comparable.memory_id, "claim_mismatch", json.dumps({"changed_claims": differences}, sort_keys=True), now))
                conflict_id = cursor.fetchone()["conflict_id"]
                self._audit(cursor, comparable.memory_id, "conflict_detected", actor_id,
                            {"conflict_id": conflict_id, "incoming_memory_id": record.memory_id, "changed_claims": differences}, now)
                return EventCaptureResult("conflict", record, conflict_id)
            self._upsert(cursor, record, event_type=event_type, actor_id=actor_id, details=details, now=now)
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
        record.identity_key, now = event_identity_key(record), self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            existing = None
            if existing_memory_id:
                cursor.execute("SELECT * FROM memory_records WHERE memory_id=%s FOR UPDATE", (existing_memory_id,))
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"Memory '{existing_memory_id}' not found")
                existing = self._record_from_row(row)
                if existing.owner_id != record.owner_id:
                    raise PermissionError("Reconciliation candidate belongs to another owner")
            if outcome == "duplicate":
                assert existing is not None
                self._audit(cursor, existing.memory_id, "duplicate_detected", actor_id, details, now)
                return EventCaptureResult("duplicate", existing)
            if outcome == "update":
                assert existing is not None
                record.status, record.user_confirmed = "candidate", False
                self._upsert(cursor, record, event_type="conflict_candidate_created", actor_id=actor_id,
                             details=details, now=now)
                cursor.execute("""INSERT INTO memory_conflicts(
                    incoming_memory_id, existing_memory_id, conflict_type, status, details_json, created_at)
                    VALUES (%s,%s,'claim_mismatch','open',%s::jsonb,%s) RETURNING conflict_id""",
                    (record.memory_id, existing.memory_id, json.dumps(details, sort_keys=True), now))
                conflict_id = cursor.fetchone()["conflict_id"]
                self._audit(cursor, existing.memory_id, "conflict_detected", actor_id,
                            {"conflict_id": conflict_id, "incoming_memory_id": record.memory_id, **details}, now)
                return EventCaptureResult("conflict", record, conflict_id)
            if outcome == "uncertain":
                record.status, record.user_confirmed = "candidate", False
                event_type = "reconciliation_uncertain"
            else:
                event_type = "reconciliation_created" if outcome == "new" else "reconciliation_related"
            self._upsert(cursor, record, event_type=event_type, actor_id=actor_id, details=details, now=now)
            if outcome == "related":
                assert existing is not None
                self._link(cursor, record.memory_id, existing.memory_id, "related", now)
            return EventCaptureResult("created" if outcome in {"new", "related"} else "uncertain", record)

    @staticmethod
    def _link(cursor: Any, first_memory_id: str, second_memory_id: str,
              relationship_type: str, now: datetime) -> None:
        if relationship_type == "related":
            first_memory_id, second_memory_id = sorted((first_memory_id, second_memory_id))
        cursor.execute("""INSERT INTO memory_relationships(from_memory_id,to_memory_id,relationship_type,created_at)
            VALUES (%s,%s,%s,%s) ON CONFLICT(from_memory_id,to_memory_id,relationship_type) DO NOTHING""",
            (first_memory_id, second_memory_id, relationship_type, now))

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM memory_records WHERE memory_id=%s", (memory_id,)); row = cursor.fetchone()
        return self._record_from_row(row) if row else None

    def get_owned(self, memory_id: str, *, owner_id: str) -> MemoryRecord | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM memory_records WHERE memory_id=%s AND owner_id=%s", (memory_id, owner_id)); row = cursor.fetchone()
        return self._record_from_row(row) if row else None

    @staticmethod
    def _candidate_scope_clause(*, session_id: str, project_id: str | None,
                                project_scope: str | None) -> tuple[str, list[Any]]:
        clauses = ["(records.scope='session' AND records.session_id=%s)", "records.scope='user'"]
        params: list[Any] = [session_id]
        if project_id:
            clauses.append("(records.scope='project' AND records.project_id=%s)")
            params.append(project_id)
        if project_scope:
            clauses.append("(records.scope='project' AND records.project_id IS NULL AND records.project_scope=%s)")
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
        """Return exact, active identifier matches visible to the current chat."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        reference_keys = sorted({(reference.scheme, reference.normalized_value) for reference in identifier_references})
        if not reference_keys:
            return []
        now = self._now()
        scope_clause, scope_params = self._candidate_scope_clause(
            session_id=session_id, project_id=project_id, project_scope=project_scope,
        )
        base_where = f"""records.owner_id=%s AND records.status='active' AND records.user_confirmed=TRUE
            AND records.valid_from <= %s AND (records.valid_to IS NULL OR records.valid_to > %s)
            AND {scope_clause}"""
        reference_match_clause = " OR ".join(
            "(refs.scheme=%s AND refs.normalized_value=%s)" for _ in reference_keys
        )
        reference_match_params = [value for key in reference_keys for value in key]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT records.*, COUNT(DISTINCT refs.normalized_value) AS match_count
                    FROM memory_identifier_references refs
                    JOIN memory_records records ON records.memory_id=refs.memory_id
                    WHERE {base_where} AND ({reference_match_clause})
                    GROUP BY records.memory_id
                    ORDER BY match_count DESC, records.updated_at DESC LIMIT %s""",
                (owner_id, now, now, *scope_params, *reference_match_params, limit),
            )
            candidates = [self._record_from_row(row) for row in cursor.fetchall()]
            if len(candidates) >= limit:
                return candidates
            cursor.execute(
                f"""SELECT records.* FROM memory_records records
                    WHERE {base_where} AND records.kind='event' AND NOT EXISTS (
                        SELECT 1 FROM memory_identifier_references refs WHERE refs.memory_id=records.memory_id
                    ) ORDER BY records.updated_at DESC LIMIT 500""",
                (owner_id, now, now, *scope_params),
            )
            legacy_rows = cursor.fetchall()
        known_ids = {record.memory_id for record in candidates}
        wanted = {value for scheme, value in reference_keys if scheme == "external_reference"}
        if not wanted:
            return candidates
        for row in legacy_rows:
            record = self._record_from_row(row)
            if record.memory_id not in known_ids and isinstance(record.payload, EventMemory) and legacy_identifier_values(record.payload) & wanted:
                candidates.append(record)
                known_ids.add(record.memory_id)
                if len(candidates) == limit:
                    break
        return candidates

    def _owned_locked(self, cursor: Any, memory_id: str, owner_id: str) -> MemoryRecord:
        cursor.execute("SELECT * FROM memory_records WHERE memory_id=%s AND owner_id=%s FOR UPDATE", (memory_id, owner_id)); row = cursor.fetchone()
        if not row: raise KeyError(f"Memory '{memory_id}' not found")
        return self._record_from_row(row)

    def list(self, *, owner_id: str, scope: MemoryScope | None = None, session_id: str | None = None,
             project_id: str | None = None, project_scope: str | None = None, status: MemoryStatus | None = "active",
             kind: str | None = None, limit: int = 100) -> list[MemoryRecord]:
        clauses, params = ["owner_id=%s"], [owner_id]
        for field, value in (("scope",scope),("session_id",session_id),("project_id",project_id),("project_scope",project_scope),("status",status),("kind",kind)):
            if value is not None: clauses.append(f"{field}=%s"); params.append(value)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT %s", (*params, limit))
            return [self._record_from_row(row) for row in cursor.fetchall()]

    def expire_due(self, *, now: datetime | None = None, owner_id: str | None = None) -> list[MemoryRecord]:
        sweep_time = now or self._now(); clauses, params = ["status='active'", "valid_to IS NOT NULL", "valid_to <= %s"], [sweep_time]
        if owner_id: clauses.append("owner_id=%s"); params.append(owner_id)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} ORDER BY valid_to FOR UPDATE", params)
            expired = [self._record_from_row(row) for row in cursor.fetchall()]
            for record in expired:
                valid_to = record.valid_to; record.status = "expired"
                self._upsert(cursor, record, event_type="expired_by_sweep", actor_id="system",
                             details={"expired_at": sweep_time.isoformat(), "valid_to": valid_to.isoformat() if valid_to else None}, now=sweep_time)
        return expired

    def claim_projection_jobs(self, *, limit: int = 50, lease_seconds: int = 300) -> list[dict]:
        if not 1 <= limit <= 1000: raise ValueError("limit must be between 1 and 1000")
        now, stale = self._now(), self._now() - timedelta(seconds=lease_seconds)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""WITH candidates AS (
                SELECT job_id FROM memory_projection_jobs WHERE status='pending'
                   OR (status='running' AND locked_at < %s)
                ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE memory_projection_jobs jobs SET status='running', attempts=attempts+1, locked_at=%s,
                    locked_by=%s, updated_at=%s FROM candidates WHERE jobs.job_id=candidates.job_id RETURNING jobs.*""",
                (stale, limit, now, self._worker_id, now))
            return [dict(row) for row in cursor.fetchall()]

    def complete_projection_job(self, job_id: int) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE memory_projection_jobs SET status='completed', error=NULL, locked_at=NULL, locked_by=NULL, updated_at=%s
                WHERE job_id=%s AND status='running' AND locked_by=%s""", (self._now(), job_id, self._worker_id))

    def fail_projection_job(self, job_id: int, error: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE memory_projection_jobs SET status='failed', error=%s, locked_at=NULL, locked_by=NULL, updated_at=%s
                WHERE job_id=%s AND status='running' AND locked_by=%s""", (error, self._now(), job_id, self._worker_id))

    def requeue_failed_projections(self) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE memory_projection_jobs SET status='pending', error=NULL, updated_at=%s WHERE status='failed'", (self._now(),))
            return cursor.rowcount

    def enqueue_all_projections(self) -> int:
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT memory_id FROM memory_records"); ids = [row["memory_id"] for row in cursor.fetchall()]
            for memory_id in ids:
                self._enqueue(cursor, memory_id, "qdrant", now); self._enqueue(cursor, memory_id, "graph", now)
        return len(ids)

    def list_projection_jobs(self, *, owner_id: str, status: str | None = None, limit: int = 100) -> list[dict]:
        clauses, params = ["records.owner_id=%s"], [owner_id]
        if status: clauses.append("jobs.status=%s"); params.append(status)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"""SELECT jobs.* FROM memory_projection_jobs jobs JOIN memory_records records ON records.memory_id=jobs.memory_id
                WHERE {' AND '.join(clauses)} ORDER BY jobs.updated_at DESC LIMIT %s""", (*params, limit))
            return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _conflict(row: dict[str, Any]) -> dict:
        result = dict(row); result["details"] = result.pop("details_json") or {}
        if result.get("resolution_json") is not None: result["resolution"] = result.pop("resolution_json")
        else: result.pop("resolution_json", None)
        result.pop("owner_id", None); return result

    def list_conflicts(self, *, owner_id: str, status: str | None = "open") -> list[dict]:
        clauses, params = ["incoming.owner_id=%s"], [owner_id]
        if status: clauses.append("conflicts.status=%s"); params.append(status)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"""SELECT conflicts.*, incoming.owner_id, incoming.project_scope FROM memory_conflicts conflicts
                JOIN memory_records incoming ON incoming.memory_id=conflicts.incoming_memory_id
                WHERE {' AND '.join(clauses)} ORDER BY conflicts.created_at DESC""", params)
            return [self._conflict(row) for row in cursor.fetchall()]

    def get_conflict(self, conflict_id: int, *, owner_id: str) -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT conflicts.*, incoming.owner_id, incoming.project_scope FROM memory_conflicts conflicts
                JOIN memory_records incoming ON incoming.memory_id=conflicts.incoming_memory_id
                WHERE conflicts.conflict_id=%s AND incoming.owner_id=%s""", (conflict_id, owner_id)); row = cursor.fetchone()
        return self._conflict(row) if row else None

    def resolve_conflict(self, conflict_id: int, *, action: str, actor_id: str) -> dict:
        if action not in {"supersede_existing", "keep_existing", "expire_existing"}: raise ValueError("Unsupported conflict resolution")
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT conflicts.* FROM memory_conflicts conflicts JOIN memory_records incoming
                ON incoming.memory_id=conflicts.incoming_memory_id WHERE conflicts.conflict_id=%s AND incoming.owner_id=%s
                AND conflicts.status='open' FOR UPDATE""", (conflict_id, actor_id)); conflict = cursor.fetchone()
            if not conflict: raise KeyError(f"Open conflict '{conflict_id}' not found")
            incoming = self._owned_locked(cursor, conflict["incoming_memory_id"], actor_id)
            existing = self._owned_locked(cursor, conflict["existing_memory_id"], actor_id)
            if action == "keep_existing":
                incoming.status = "rejected"; self._upsert(cursor, incoming, event_type="conflict_rejected", actor_id=actor_id, details={"conflict_id": conflict_id}, now=now)
            else:
                incoming.status, incoming.user_confirmed = "active", True
                self._upsert(cursor, incoming, event_type="conflict_accepted", actor_id=actor_id, details={"conflict_id": conflict_id}, now=now)
                if action == "supersede_existing":
                    existing.status, existing.superseded_by = "superseded", incoming.memory_id; event = "superseded"
                else:
                    existing.status, existing.valid_to = "expired", now; event = "expired"
                self._upsert(cursor, existing, event_type=event, actor_id=actor_id,
                             details={"conflict_id": conflict_id, "replacement_memory_id": incoming.memory_id}, now=now)
            resolution = {"action": action, "incoming_memory_id": incoming.memory_id, "existing_memory_id": existing.memory_id}
            cursor.execute("""UPDATE memory_conflicts SET status='resolved', resolved_by=%s, resolution_json=%s::jsonb, resolved_at=%s
                WHERE conflict_id=%s""", (actor_id, json.dumps(resolution, sort_keys=True), now, conflict_id))
        return self.get_conflict(conflict_id, owner_id=actor_id) or self._conflict(conflict)

    def list_audit_events(self, memory_id: str, *, owner_id: str, limit: int = 100) -> list[dict]:
        if not self.get_owned(memory_id, owner_id=owner_id): raise KeyError(f"Memory '{memory_id}' not found")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT event_id,event_type,actor_id,details_json,created_at FROM memory_audit_events WHERE memory_id=%s ORDER BY event_id DESC LIMIT %s", (memory_id, limit))
            return [{**dict(row), "details": row["details_json"] or {}} for row in cursor.fetchall()]

    def list_access_events(self, memory_id: str, *, owner_id: str, limit: int = 100) -> list[dict]:
        if not self.get_owned(memory_id, owner_id=owner_id): raise KeyError(f"Memory '{memory_id}' not found")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT trace_id,session_id,message_id,event_type,prompt_label,rank,score,details_json,created_at
                FROM memory_access_events WHERE memory_id=%s ORDER BY access_event_id DESC LIMIT %s""", (memory_id, limit))
            return [{**dict(row), "details": row["details_json"] or {}} for row in cursor.fetchall()]

    def _mutate(self, memory_id: str, actor_id: str, mutation: Any, event_type: str, details: dict | None = None) -> MemoryRecord:
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            record = self._owned_locked(cursor, memory_id, actor_id); mutation(record)
            self._upsert(cursor, record, event_type=event_type, actor_id=actor_id, details=details, now=now)
            return record

    def transition(self, memory_id: str, status: MemoryStatus, *, actor_id: str | None = None,
                   superseded_by: str | None = None, event_type: str | None = None) -> MemoryRecord:
        actor = actor_id or "default"
        return self._mutate(memory_id, actor, lambda record: (setattr(record, "status", status), setattr(record, "superseded_by", superseded_by)), event_type or f"status_{status}")

    def review(self, memory_id: str, action: str, *, actor_id: str = "default") -> MemoryRecord:
        def mutate(record: MemoryRecord) -> None:
            if record.status not in {"candidate", "active"}: raise ValueError(f"Cannot review a {record.status} memory")
            if action == "confirm": record.user_confirmed, record.status = True, "active"
            elif action == "reject": record.status = "rejected"
            elif action == "expire": record.status, record.valid_to = "expired", self._now()
            else: raise ValueError(f"Unsupported memory review action: {action}")
        return self._mutate(memory_id, actor_id, mutate, action)

    def forget(self, memory_id: str, *, actor_id: str = "default") -> MemoryRecord:
        record = self.get_owned(memory_id, owner_id=actor_id)
        if not record: raise KeyError(f"Memory '{memory_id}' not found")
        if record.status == "deleted": return record
        return self._mutate(memory_id, actor_id, lambda item: setattr(item, "status", "deleted"), "deleted_by_user")

    def promote(self, memory_id: str, *, scope: MemoryScope, project_scope: str | None = None, actor_id: str = "default") -> MemoryRecord:
        if scope not in {"user", "project"}: raise ValueError("Promotion target must be 'user' or 'project'")
        if scope == "project" and not project_scope: raise ValueError("project promotion requires project_scope")
        previous = self.get_owned(memory_id, owner_id=actor_id)
        if not previous: raise KeyError(f"Memory '{memory_id}' not found")
        def mutate(record: MemoryRecord) -> None:
            if record.status not in {"candidate", "active"}: raise ValueError(f"Cannot promote a {record.status} memory")
            record.scope, record.project_scope, record.user_confirmed, record.status = scope, project_scope if scope == "project" else None, True, "active"
        return self._mutate(memory_id, actor_id, mutate, "promoted", {"from_scope": previous.scope, "to_scope": scope, "project_scope": project_scope})

    def edit(self, memory_id: str, payload: dict, *, actor_id: str = "default") -> MemoryRecord:
        kinds = {"knowledge": KnowledgeAtom, "preference": PreferenceMemory, "solution": SolutionMemory, "entity": EntityMemory, "event": EventMemory}
        def mutate(record: MemoryRecord) -> None:
            record.payload = kinds[record.kind].model_validate(payload)
            if record.kind == "event": record.identity_key = event_identity_key(record)
        return self._mutate(memory_id, actor_id, mutate, "edited")

    def supersede(self, memory_id: str, replacement_id: str, *, actor_id: str = "default") -> MemoryRecord:
        if memory_id == replacement_id: raise ValueError("A memory cannot supersede itself")
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            first, second = sorted((memory_id, replacement_id))
            records = {identifier: self._owned_locked(cursor, identifier, actor_id) for identifier in (first, second)}
            record, replacement = records[memory_id], records[replacement_id]
            record.status, record.superseded_by = "superseded", replacement.memory_id
            self._upsert(cursor, record, event_type="superseded", actor_id=actor_id,
                         details={"replacement_memory_id": replacement.memory_id, "relationship": "contradiction"}, now=now)
            self._link(cursor, record.memory_id, replacement.memory_id, "contradiction", now)
        return record

    def backfill_project_ids(self, projects: dict[tuple[str, str], dict]) -> dict[str, int]:
        updated = unmatched = 0; now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT * FROM memory_records WHERE scope='project' AND project_id IS NULL AND project_scope IS NOT NULL FOR UPDATE""")
            for row in cursor.fetchall():
                record = self._record_from_row(row); project = projects.get((record.owner_id, " ".join(record.project_scope.split()).casefold()))
                if not project: unmatched += 1; continue
                record.project_id = project["project_id"]
                self._upsert(cursor, record, event_type="project_id_backfilled", actor_id="system",
                             details={"project_id": record.project_id, "project_scope": record.project_scope}, now=now); updated += 1
        return {"updated": updated, "unmatched": unmatched}
