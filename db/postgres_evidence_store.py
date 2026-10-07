"""PostgreSQL authority for immutable assets, scoped bindings, and evidence."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from core.ingestion.models import AssetRecord, EvidenceSegment
from core.ingestion.parser import SourceLocator
from db.postgres_migrations import PostgresMigrationRunner, evidence_migrations


class PostgresEvidenceRepository:
    def __init__(self, database_url: str, *, schema: str | None = None, worker_id: str | None = None) -> None:
        if not database_url:
            raise ValueError("database_url must not be blank")
        if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("schema must be a simple PostgreSQL identifier")
        self._database_url, self._schema = database_url, schema
        self._worker_id = worker_id or f"evidence-worker-{uuid.uuid4()}"
        with self._connect() as connection:
            PostgresMigrationRunner("evidence").apply(connection, evidence_migrations())

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("psycopg is required for the PostgreSQL evidence repository") from error
        connection = psycopg.connect(self._database_url, row_factory=dict_row)
        if self._schema:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('search_path', %s, false)", (f"{self._schema},public",))
        return connection

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _evidence_hash(segment: EvidenceSegment) -> str:
        payload = {
            "asset_id": segment.asset_id, "modality": segment.modality,
            "representation": segment.representation, "content": segment.content,
            "media_uri": segment.media_uri, "source_name": segment.source_name,
            "locator": segment.locator.model_dump(mode="json"), "parent": segment.parent_evidence_id,
            "parser_backend": segment.parser_backend, "parser_version": segment.parser_version,
            "retrieval_metadata": segment.retrieval_metadata,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _segment(row: dict) -> EvidenceSegment:
        return EvidenceSegment(
            evidence_id=row["evidence_id"], asset_id=row["asset_id"], modality=row["modality"],
            representation=row["representation"], content=row["content"], media_uri=row["media_uri"],
            source_name=row["source_name"], locator=SourceLocator.model_validate(row["locator_json"]),
            parent_evidence_id=row["parent_evidence_id"], parser_backend=row["parser_backend"],
            parser_version=row["parser_version"], retrieval_metadata=row["retrieval_metadata_json"],
            created_at=row["created_at"],
        )

    def upsert_asset(self, asset: AssetRecord, *, owner_id: str, scope: str,
                     session_id: str | None, project_id: str | None) -> int:
        if scope not in {"session", "project"}:
            raise ValueError("scope must be 'session' or 'project'")
        if scope == "session" and (not session_id or project_id is not None):
            raise ValueError("session scope requires session_id and forbids project_id")
        if scope == "project" and (not project_id or session_id is not None):
            raise ValueError("project scope requires project_id and forbids session_id")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM assets WHERE asset_id=%s FOR UPDATE", (asset.asset_id,))
            existing = cursor.fetchone()
            if existing and existing["content_hash"] != asset.content_hash:
                raise ValueError("asset ID already exists with different content")
            if not existing:
                cursor.execute("""INSERT INTO assets(asset_id,filename,media_type,raw_file_uri,content_hash,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (asset.asset_id, asset.filename, asset.media_type, asset.raw_file_uri,
                     asset.content_hash, asset.created_at))
            cursor.execute("""INSERT INTO asset_bindings(asset_id,owner_id,scope,session_id,project_id,created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT ON CONSTRAINT uq_asset_binding_scope DO UPDATE SET asset_id=EXCLUDED.asset_id
                RETURNING binding_id""",
                (asset.asset_id, owner_id, scope, session_id, project_id, self._now()))
            return int(cursor.fetchone()["binding_id"])

    def get_binding(self, binding_id: int, *, owner_id: str, session_id: str,
                    project_id: str | None) -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT b.*,a.filename,a.media_type,a.raw_file_uri,a.content_hash,
                    a.created_at AS asset_created_at FROM asset_bindings b JOIN assets a USING(asset_id)
                WHERE b.binding_id=%s AND b.owner_id=%s AND
                  ((b.scope='session' AND b.session_id=%s) OR (b.scope='project' AND b.project_id=%s))""",
                (binding_id, owner_id, session_id, project_id))
            row = cursor.fetchone()
        return dict(row) if row else None

    def get_asset(self, binding_id: int, *, owner_id: str, session_id: str,
                  project_id: str | None) -> AssetRecord | None:
        row = self.get_binding(binding_id, owner_id=owner_id, session_id=session_id, project_id=project_id)
        if not row:
            return None
        return AssetRecord(asset_id=row["asset_id"], owner_id=row["owner_id"], project_id=row["project_id"],
                           filename=row["filename"], media_type=row["media_type"], raw_file_uri=row["raw_file_uri"],
                           content_hash=row["content_hash"], created_at=row["asset_created_at"])

    def start_ingestion(self, asset_id: str, *, binding_id: int, modality: str,
                        parser_backend: str | None = None, parser_version: str | None = None) -> dict:
        run_id, now = str(uuid.uuid4()), self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM asset_bindings WHERE binding_id=%s AND asset_id=%s", (binding_id, asset_id))
            if not cursor.fetchone():
                raise KeyError("Asset binding not found")
            cursor.execute("""INSERT INTO ingestion_runs(run_id,asset_id,binding_id,modality,status,
                    parser_backend,parser_version,created_at,started_at)
                VALUES (%s,%s,%s,%s,'processing',%s,%s,%s,%s)""",
                (run_id, asset_id, binding_id, modality, parser_backend, parser_version, now, now))
        return {"run_id": run_id, "binding_id": binding_id, "asset_id": asset_id,
                "modality": modality, "status": "processing", "chunk_count": 0, "message": ""}

    def complete_ingestion(self, run_id: str, *, chunk_count: int, error: str | None = None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE ingestion_runs SET status=%s,chunk_count=%s,error=%s,completed_at=%s
                WHERE run_id=%s""", ("failed" if error else "done", chunk_count, error, self._now(), run_id))

    def get_document_status(self, binding_id: int, *, owner_id: str, session_id: str,
                            project_id: str | None) -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT r.* FROM ingestion_runs r JOIN asset_bindings b USING(binding_id)
                WHERE r.binding_id=%s AND b.owner_id=%s AND
                  ((b.scope='session' AND b.session_id=%s) OR (b.scope='project' AND b.project_id=%s))
                ORDER BY r.created_at DESC LIMIT 1""", (binding_id, owner_id, session_id, project_id))
            row = cursor.fetchone()
        if not row:
            return None
        return {"doc_id": row["asset_id"], "binding_id": int(row["binding_id"]), "run_id": row["run_id"],
                "modality": row["modality"], "status": row["status"], "chunk_count": row["chunk_count"],
                "message": row["error"] or ""}

    @staticmethod
    def _enqueue(cursor: Any, evidence_id: str, binding_id: int, now: datetime) -> None:
        cursor.execute("""INSERT INTO evidence_projection_jobs(evidence_id,binding_id,target,status,attempts,created_at,updated_at)
            VALUES (%s,%s,'qdrant','pending',0,%s,%s)
            ON CONFLICT(evidence_id,binding_id,target) DO UPDATE SET status='pending',error=NULL,
                locked_at=NULL,locked_by=NULL,updated_at=EXCLUDED.updated_at""", (evidence_id, binding_id, now, now))

    def upsert_evidence(self, segments: list[EvidenceSegment], *, binding_id: int) -> None:
        if not segments:
            return
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT asset_id FROM asset_bindings WHERE binding_id=%s", (binding_id,))
            binding = cursor.fetchone()
            if not binding:
                raise KeyError("Asset binding not found")
            if any(segment.asset_id != binding["asset_id"] for segment in segments):
                raise ValueError("Evidence and binding must belong to the same asset")
            for segment in segments:
                digest = self._evidence_hash(segment)
                cursor.execute("SELECT content_hash FROM evidence_segments WHERE evidence_id=%s FOR UPDATE", (segment.evidence_id,))
                old = cursor.fetchone()
                if old and old["content_hash"] != digest:
                    raise ValueError("evidence ID already exists with different immutable provenance")
                if not old:
                    cursor.execute("""INSERT INTO evidence_segments(evidence_id,asset_id,modality,representation,content,
                        media_uri,source_name,locator_json,parent_evidence_id,parser_backend,parser_version,
                        retrieval_metadata_json,content_hash,created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s)""",
                        (segment.evidence_id, segment.asset_id, segment.modality, segment.representation, segment.content,
                         segment.media_uri, segment.source_name, json.dumps(segment.locator.model_dump(mode="json"), sort_keys=True),
                         segment.parent_evidence_id, segment.parser_backend, segment.parser_version,
                         json.dumps(segment.retrieval_metadata, sort_keys=True), digest, segment.created_at))
                self._enqueue(cursor, segment.evidence_id, binding_id, now)

    def enqueue_asset_projections(self, binding_id: int) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT e.evidence_id FROM evidence_segments e JOIN asset_bindings b ON b.asset_id=e.asset_id
                WHERE b.binding_id=%s""", (binding_id,))
            rows, now = cursor.fetchall(), self._now()
            for row in rows:
                self._enqueue(cursor, row["evidence_id"], binding_id, now)
        return len(rows)

    def get_projection_input(self, evidence_id: str, *, binding_id: int) -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT e.*,b.owner_id,b.scope,b.session_id,b.project_id,b.binding_id
                FROM evidence_segments e JOIN asset_bindings b ON b.asset_id=e.asset_id
                WHERE e.evidence_id=%s AND b.binding_id=%s""", (evidence_id, binding_id))
            row = cursor.fetchone()
        if not row:
            return None
        return {"segment": self._segment(row), "binding_id": int(row["binding_id"]), "owner_id": row["owner_id"],
                "scope": row["scope"], "session_id": row["session_id"], "project_id": row["project_id"]}

    def revalidate_evidence(self, references: list[tuple[str, int]], *, owner_id: str,
                            session_id: str, project_id: str | None) -> dict[tuple[str, int], dict]:
        if not references:
            return {}
        unique = list(dict.fromkeys(references))
        values = ",".join(["(%s,%s)"] * len(unique))
        params: list[Any] = [value for pair in unique for value in pair] + [owner_id, session_id, project_id]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"""WITH requested(evidence_id,binding_id) AS (VALUES {values})
                SELECT e.*,b.binding_id,b.owner_id,b.scope,b.session_id,b.project_id
                FROM requested JOIN evidence_segments e USING(evidence_id)
                JOIN asset_bindings b ON b.binding_id=requested.binding_id AND b.asset_id=e.asset_id
                WHERE b.owner_id=%s AND ((b.scope='session' AND b.session_id=%s)
                    OR (b.scope='project' AND b.project_id=%s))""", params)
            rows = cursor.fetchall()
        return {(row["evidence_id"], int(row["binding_id"])): dict(row) for row in rows}

    def enqueue_all_evidence_projections(self) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT e.evidence_id,b.binding_id FROM evidence_segments e
                JOIN asset_bindings b ON b.asset_id=e.asset_id""")
            rows, now = cursor.fetchall(), self._now()
            for row in rows:
                self._enqueue(cursor, row["evidence_id"], int(row["binding_id"]), now)
        return len(rows)

    def requeue_failed_projections(self) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE evidence_projection_jobs SET status='pending',error=NULL,updated_at=%s WHERE status='failed'", (self._now(),))
            return cursor.rowcount

    def claim_projection_jobs(self, *, limit: int = 50, lease_seconds: int = 300,
                              binding_id: int | None = None) -> list[dict]:
        now, stale = self._now(), self._now() - timedelta(seconds=lease_seconds)
        clause = "AND binding_id=%s" if binding_id is not None else ""
        params: list[Any] = [stale] + ([binding_id] if binding_id is not None else []) + [limit, now, self._worker_id, now]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"""WITH candidates AS (SELECT job_id FROM evidence_projection_jobs
                    WHERE (status='pending' OR (status='running' AND locked_at<%s)) {clause}
                    ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE evidence_projection_jobs jobs SET status='running',attempts=attempts+1,
                    locked_at=%s,locked_by=%s,updated_at=%s FROM candidates
                WHERE jobs.job_id=candidates.job_id RETURNING jobs.*""", params)
            return [dict(row) for row in cursor.fetchall()]

    def complete_projection_job(self, job_id: int) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE evidence_projection_jobs SET status='completed',error=NULL,locked_at=NULL,
                locked_by=NULL,updated_at=%s WHERE job_id=%s AND locked_by=%s""", (self._now(), job_id, self._worker_id))

    def fail_projection_job(self, job_id: int, error: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE evidence_projection_jobs SET status='failed',error=%s,locked_at=NULL,
                locked_by=NULL,updated_at=%s WHERE job_id=%s AND locked_by=%s""", (error, self._now(), job_id, self._worker_id))
