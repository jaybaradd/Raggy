"""PostgreSQL authority for immutable source assets and evidence provenance."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from core.ingestion.models import AssetRecord, EvidenceSegment
from db.postgres_migrations import PostgresMigrationRunner, evidence_migrations


class PostgresEvidenceRepository:
    def __init__(self, database_url: str, *, schema: str | None = None, worker_id: str | None = None) -> None:
        if not database_url: raise ValueError("database_url must not be blank")
        if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema): raise ValueError("schema must be a simple PostgreSQL identifier")
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
    def _now() -> datetime: return datetime.now(timezone.utc)

    @staticmethod
    def _evidence_hash(segment: EvidenceSegment) -> str:
        payload = {"asset_id": segment.asset_id, "modality": segment.modality, "representation": segment.representation,
                   "content": segment.content, "media_uri": segment.media_uri, "source_name": segment.source_name,
                   "locator": segment.locator.model_dump(mode="json"), "parent": segment.parent_evidence_id,
                   "parser_backend": segment.parser_backend, "parser_version": segment.parser_version,
                   "retrieval_metadata": segment.retrieval_metadata}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def upsert_asset(self, asset: AssetRecord) -> None:
        """Create immutable source metadata and an owner/project visibility binding."""
        now = self._now()
        owner = asset.owner_id or "default"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM assets WHERE asset_id=%s FOR UPDATE", (asset.asset_id,)); existing = cursor.fetchone()
            if existing:
                immutable = ("content_hash", "filename", "media_type", "raw_file_uri")
                proposed = {"content_hash": asset.content_hash, "filename": asset.filename, "media_type": asset.media_type, "raw_file_uri": asset.raw_file_uri}
                if any(existing[field] != proposed[field] for field in immutable):
                    raise ValueError("asset ID already exists with different immutable source metadata")
            else:
                cursor.execute("""INSERT INTO assets(asset_id,filename,media_type,raw_file_uri,content_hash,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s)""", (asset.asset_id, asset.filename, asset.media_type, asset.raw_file_uri, asset.content_hash, asset.created_at))
            cursor.execute("""INSERT INTO asset_bindings(asset_id,owner_id,project_id,project_scope,created_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(asset_id,owner_id,project_id) DO NOTHING""",
                (asset.asset_id, owner, asset.project_id, asset.project_scope, now))

    def start_ingestion(self, asset_id: str, *, modality: str, owner_id: str = "default", project_id: str | None = None,
                        project_scope: str | None = None, parser_backend: str | None = None, parser_version: str | None = None) -> dict:
        run_id, now = str(uuid.uuid4()), self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM asset_bindings WHERE asset_id=%s AND owner_id=%s AND project_id IS NOT DISTINCT FROM %s", (asset_id, owner_id, project_id))
            if not cursor.fetchone(): raise KeyError("Asset not found or not accessible")
            cursor.execute("""INSERT INTO ingestion_runs(run_id,asset_id,owner_id,project_id,project_scope,modality,status,parser_backend,parser_version,created_at,started_at)
                VALUES (%s,%s,%s,%s,%s,%s,'processing',%s,%s,%s,%s)""",
                (run_id, asset_id, owner_id, project_id, project_scope, modality, parser_backend, parser_version, now, now))
        return {"run_id": run_id, "asset_id": asset_id, "modality": modality, "status": "processing", "chunk_count": 0, "message": ""}

    def complete_ingestion(self, run_id: str, *, chunk_count: int, error: str | None = None) -> None:
        now, status = self._now(), "failed" if error else "done"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE ingestion_runs SET status=%s, chunk_count=%s, error=%s, completed_at=%s WHERE run_id=%s", (status, chunk_count, error, now, run_id))

    def get_document_status(self, asset_id: str, *, owner_id: str = "default") -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT runs.* FROM ingestion_runs runs JOIN asset_bindings bindings ON bindings.asset_id=runs.asset_id
                WHERE runs.asset_id=%s AND bindings.owner_id=%s ORDER BY runs.created_at DESC LIMIT 1""", (asset_id, owner_id)); row = cursor.fetchone()
        if not row: return None
        return {"doc_id": row["asset_id"], "modality": row["modality"], "status": row["status"], "chunk_count": row["chunk_count"], "message": row["error"] or ""}

    def upsert_evidence(self, segments: list[EvidenceSegment]) -> None:
        if not segments: return
        now = self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            for segment in segments:
                digest = self._evidence_hash(segment)
                cursor.execute("SELECT content_hash FROM evidence_segments WHERE evidence_id=%s FOR UPDATE", (segment.evidence_id,)); old = cursor.fetchone()
                if old:
                    if old["content_hash"] != digest: raise ValueError("evidence ID already exists with different immutable provenance")
                    continue
                cursor.execute("""INSERT INTO evidence_segments(evidence_id,asset_id,modality,representation,content,media_uri,source_name,
                    locator_json,parent_evidence_id,parser_backend,parser_version,retrieval_metadata_json,content_hash,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s)""",
                    (segment.evidence_id, segment.asset_id, segment.modality, segment.representation, segment.content, segment.media_uri,
                     segment.source_name, json.dumps(segment.locator.model_dump(mode="json"), sort_keys=True), segment.parent_evidence_id,
                     segment.parser_backend, segment.parser_version, json.dumps(segment.retrieval_metadata, sort_keys=True), digest, segment.created_at))
                cursor.execute("""INSERT INTO evidence_projection_jobs(evidence_id,target,status,attempts,created_at,updated_at)
                    VALUES (%s,'qdrant','pending',0,%s,%s) ON CONFLICT(evidence_id,target) DO UPDATE SET status='pending',error=NULL,
                    locked_at=NULL,locked_by=NULL,updated_at=EXCLUDED.updated_at""", (segment.evidence_id, now, now))

    def get_evidence(self, evidence_id: str, *, owner_id: str = "default", project_id: str | None = None) -> EvidenceSegment | None:
        with self._connect() as connection, connection.cursor() as cursor:
            if project_id is None:
                cursor.execute("""SELECT evidence.* FROM evidence_segments evidence JOIN asset_bindings bindings ON bindings.asset_id=evidence.asset_id
                    WHERE evidence.evidence_id=%s AND bindings.owner_id=%s LIMIT 1""", (evidence_id, owner_id))
            else:
                cursor.execute("""SELECT evidence.* FROM evidence_segments evidence JOIN asset_bindings bindings ON bindings.asset_id=evidence.asset_id
                    WHERE evidence.evidence_id=%s AND bindings.owner_id=%s AND bindings.project_id=%s LIMIT 1""", (evidence_id, owner_id, project_id))
            row = cursor.fetchone()
        if not row: return None
        from core.ingestion.parser import SourceLocator
        return EvidenceSegment(evidence_id=row["evidence_id"], asset_id=row["asset_id"], modality=row["modality"], representation=row["representation"], content=row["content"], media_uri=row["media_uri"], source_name=row["source_name"], locator=SourceLocator.model_validate(row["locator_json"]), parent_evidence_id=row["parent_evidence_id"], parser_backend=row["parser_backend"], parser_version=row["parser_version"], retrieval_metadata=row["retrieval_metadata_json"], created_at=row["created_at"])

    def list_evidence(self, *, limit: int = 10_000) -> list[EvidenceSegment]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM evidence_segments ORDER BY created_at LIMIT %s", (limit,)); rows = cursor.fetchall()
        from core.ingestion.parser import SourceLocator
        return [EvidenceSegment(evidence_id=row["evidence_id"], asset_id=row["asset_id"], modality=row["modality"], representation=row["representation"], content=row["content"], media_uri=row["media_uri"], source_name=row["source_name"], locator=SourceLocator.model_validate(row["locator_json"]), parent_evidence_id=row["parent_evidence_id"], parser_backend=row["parser_backend"], parser_version=row["parser_version"], retrieval_metadata=row["retrieval_metadata_json"], created_at=row["created_at"]) for row in rows]

    def get_evidence_for_projection(self, evidence_id: str) -> EvidenceSegment | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM evidence_segments WHERE evidence_id=%s", (evidence_id,)); row = cursor.fetchone()
        if not row: return None
        from core.ingestion.parser import SourceLocator
        return EvidenceSegment(evidence_id=row["evidence_id"], asset_id=row["asset_id"], modality=row["modality"], representation=row["representation"], content=row["content"], media_uri=row["media_uri"], source_name=row["source_name"], locator=SourceLocator.model_validate(row["locator_json"]), parent_evidence_id=row["parent_evidence_id"], parser_backend=row["parser_backend"], parser_version=row["parser_version"], retrieval_metadata=row["retrieval_metadata_json"], created_at=row["created_at"])

    def enqueue_all_evidence_projections(self) -> int:
        items, now = self.list_evidence(), self._now()
        with self._connect() as connection, connection.cursor() as cursor:
            for item in items:
                cursor.execute("""INSERT INTO evidence_projection_jobs(evidence_id,target,status,attempts,created_at,updated_at)
                    VALUES (%s,'qdrant','pending',0,%s,%s) ON CONFLICT(evidence_id,target) DO UPDATE SET status='pending',error=NULL,locked_at=NULL,locked_by=NULL,updated_at=EXCLUDED.updated_at""", (item.evidence_id,now,now))
        return len(items)

    def requeue_failed_projections(self) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE evidence_projection_jobs SET status='pending',error=NULL,updated_at=%s WHERE status='failed'", (self._now(),)); return cursor.rowcount

    def claim_projection_jobs(self, *, limit: int = 50, lease_seconds: int = 300) -> list[dict]:
        now, stale = self._now(), self._now() - timedelta(seconds=lease_seconds)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""WITH candidates AS (SELECT job_id FROM evidence_projection_jobs WHERE status='pending' OR (status='running' AND locked_at<%s)
                ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT %s) UPDATE evidence_projection_jobs jobs SET status='running',attempts=attempts+1,
                locked_at=%s,locked_by=%s,updated_at=%s FROM candidates WHERE jobs.job_id=candidates.job_id RETURNING jobs.*""", (stale,limit,now,self._worker_id,now))
            return [dict(row) for row in cursor.fetchall()]

    def complete_projection_job(self, job_id: int) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE evidence_projection_jobs SET status='completed',error=NULL,locked_at=NULL,locked_by=NULL,updated_at=%s WHERE job_id=%s AND locked_by=%s", (self._now(),job_id,self._worker_id))

    def fail_projection_job(self, job_id: int, error: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE evidence_projection_jobs SET status='failed',error=%s,locked_at=NULL,locked_by=NULL,updated_at=%s WHERE job_id=%s AND locked_by=%s", (error,self._now(),job_id,self._worker_id))
