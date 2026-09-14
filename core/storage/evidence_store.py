"""Durable local metadata store for Phase 1 assets and evidence.

SQLite is deliberately used as a local implementation detail for the current
single-user prototype. The interface is shaped so it can be backed by Postgres
later without changing parser or retrieval code.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config import settings
from db.migrations import Migration, MigrationRunner
from core.ingestion.models import AssetRecord, EvidenceSegment


class EvidenceStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._path = Path(db_path or settings.metadata_db_path)
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
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    project_id TEXT,
                    project_scope TEXT,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    raw_file_uri TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_segments (
                    evidence_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    representation TEXT NOT NULL,
                    content TEXT,
                    media_uri TEXT,
                    source_name TEXT,
                    locator_json TEXT NOT NULL,
                    parent_evidence_id TEXT,
                    parser_backend TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    retrieval_metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES assets(asset_id)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_asset
                    ON evidence_segments(asset_id);
                CREATE TABLE IF NOT EXISTS ingestion_runs (
                    run_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    modality TEXT NOT NULL, status TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_ingestion_runs_asset ON ingestion_runs(asset_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS evidence_projection_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL REFERENCES evidence_segments(evidence_id),
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(evidence_id, target)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_projection_jobs_pending
                    ON evidence_projection_jobs(status, updated_at);
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(evidence_segments)")
            }
            if "source_name" not in columns:
                connection.execute("ALTER TABLE evidence_segments ADD COLUMN source_name TEXT")
            if "retrieval_metadata_json" not in columns:
                connection.execute(
                    "ALTER TABLE evidence_segments ADD COLUMN retrieval_metadata_json TEXT NOT NULL DEFAULT '{}'"
                )
            asset_columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
            if "project_id" not in asset_columns:
                connection.execute("ALTER TABLE assets ADD COLUMN project_id TEXT")
            MigrationRunner("evidence").apply(connection, [
                Migration(1, "legacy_evidence_schema_baseline", lambda _: None),
                Migration(2, "asset_project_id", lambda _: None),
            ])

    def upsert_asset(self, asset: AssetRecord) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assets (
                    asset_id, owner_id, project_id, project_scope, filename, media_type,
                    raw_file_uri, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    filename=excluded.filename,
                    media_type=excluded.media_type,
                    raw_file_uri=excluded.raw_file_uri
                """,
                (
                    asset.asset_id,
                    asset.owner_id,
                    asset.project_id,
                    asset.project_scope,
                    asset.filename,
                    asset.media_type,
                    asset.raw_file_uri,
                    asset.content_hash,
                    asset.created_at.isoformat(),
                ),
            )

    def upsert_evidence(self, segments: list[EvidenceSegment]) -> None:
        if not segments:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO evidence_segments (
                    evidence_id, asset_id, modality, representation, content,
                    media_uri, source_name, locator_json, parent_evidence_id,
                    parser_backend, parser_version, retrieval_metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    content=excluded.content,
                    media_uri=excluded.media_uri,
                    source_name=excluded.source_name,
                    locator_json=excluded.locator_json,
                    parser_backend=excluded.parser_backend,
                    parser_version=excluded.parser_version,
                    retrieval_metadata_json=excluded.retrieval_metadata_json
                """,
                [
                    (
                        item.evidence_id,
                        item.asset_id,
                        item.modality,
                        item.representation,
                        item.content,
                        item.media_uri,
                        item.source_name,
                        json.dumps(item.locator.model_dump(mode="json"), sort_keys=True),
                        item.parent_evidence_id,
                        item.parser_backend,
                        item.parser_version,
                        json.dumps(item.retrieval_metadata, sort_keys=True),
                        item.created_at.isoformat(),
                    )
                    for item in segments
                ],
            )
            connection.executemany(
                """
                INSERT INTO evidence_projection_jobs (
                    evidence_id, target, status, attempts, error, created_at, updated_at
                ) VALUES (?, 'qdrant', 'pending', 0, NULL, ?, ?)
                ON CONFLICT(evidence_id, target) DO UPDATE SET
                    status='pending', error=NULL, updated_at=excluded.updated_at
                """,
                [(item.evidence_id, now, now) for item in segments],
            )

    @staticmethod
    def _segment_from_row(row: sqlite3.Row) -> EvidenceSegment:
        from core.ingestion.parser import SourceLocator

        return EvidenceSegment(
            evidence_id=row["evidence_id"],
            asset_id=row["asset_id"],
            modality=row["modality"],
            representation=row["representation"],
            content=row["content"],
            media_uri=row["media_uri"],
            source_name=row["source_name"],
            locator=SourceLocator.model_validate(json.loads(row["locator_json"])),
            parent_evidence_id=row["parent_evidence_id"],
            parser_backend=row["parser_backend"],
            parser_version=row["parser_version"],
            retrieval_metadata=json.loads(row["retrieval_metadata_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_evidence_for_projection(self, evidence_id: str) -> EvidenceSegment | None:
        """Load evidence without a visibility filter for a trusted local worker."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_segments WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        return self._segment_from_row(row) if row else None

    def claim_projection_jobs(self, *, limit: int = 50) -> list[dict]:
        """Claim a bounded batch of pending evidence-to-Qdrant work."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM evidence_projection_jobs
                   WHERE status = 'pending' ORDER BY updated_at LIMIT ?""",
                (limit,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE evidence_projection_jobs
                       SET status = 'running', attempts = attempts + 1, updated_at = ?
                       WHERE job_id = ?""",
                    (now, row["job_id"]),
                )
        return [dict(row) for row in rows]

    def complete_projection_job(self, job_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE evidence_projection_jobs
                   SET status = 'completed', error = NULL, updated_at = ? WHERE job_id = ?""",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )

    def fail_projection_job(self, job_id: int, error: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE evidence_projection_jobs
                   SET status = 'failed', error = ?, updated_at = ? WHERE job_id = ?""",
                (error, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def requeue_failed_projections(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE evidence_projection_jobs
                   SET status = 'pending', error = NULL, updated_at = ? WHERE status = 'failed'""",
                (datetime.now(timezone.utc).isoformat(),),
            )
        return cursor.rowcount

    def enqueue_all_evidence_projections(self) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT evidence_id FROM evidence_segments").fetchall()
            now = datetime.now(timezone.utc).isoformat()
            connection.executemany(
                """INSERT INTO evidence_projection_jobs (
                    evidence_id, target, status, attempts, error, created_at, updated_at
                ) VALUES (?, 'qdrant', 'pending', 0, NULL, ?, ?)
                ON CONFLICT(evidence_id, target) DO UPDATE SET
                    status='pending', error=NULL, updated_at=excluded.updated_at""",
                [(row["evidence_id"], now, now) for row in rows],
            )
        return len(rows)

    def start_ingestion(self, asset_id: str, *, modality: str, owner_id: str = "default", **_: object) -> dict:
        run_id, now = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("INSERT INTO ingestion_runs VALUES (?, ?, ?, ?, 'processing', 0, NULL, ?, NULL)",
                               (run_id, asset_id, owner_id, modality, now))
        return {"run_id": run_id, "asset_id": asset_id, "modality": modality, "status": "processing", "chunk_count": 0, "message": ""}

    def complete_ingestion(self, run_id: str, *, chunk_count: int, error: str | None = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE ingestion_runs SET status = ?, chunk_count = ?, error = ?, completed_at = ? WHERE run_id = ?",
                               ("failed" if error else "done", chunk_count, error, datetime.now(timezone.utc).isoformat(), run_id))

    def get_document_status(self, asset_id: str, *, owner_id: str = "default") -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ingestion_runs WHERE asset_id = ? AND owner_id = ? ORDER BY created_at DESC LIMIT 1",
                                     (asset_id, owner_id)).fetchone()
        if row is None: return None
        return {"doc_id": row["asset_id"], "modality": row["modality"], "status": row["status"],
                "chunk_count": row["chunk_count"], "message": row["error"] or ""}


evidence_store = EvidenceStore()
