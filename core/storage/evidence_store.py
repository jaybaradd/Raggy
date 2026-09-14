"""Durable local metadata store for Phase 1 assets and evidence.

SQLite is deliberately used as a local implementation detail for the current
single-user prototype. The interface is shaped so it can be backed by Postgres
later without changing parser or retrieval code.
"""

from __future__ import annotations

import json
import sqlite3
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
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES assets(asset_id)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_asset
                    ON evidence_segments(asset_id);
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(evidence_segments)")
            }
            if "source_name" not in columns:
                connection.execute("ALTER TABLE evidence_segments ADD COLUMN source_name TEXT")
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
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO evidence_segments (
                    evidence_id, asset_id, modality, representation, content,
                    media_uri, source_name, locator_json, parent_evidence_id,
                    parser_backend, parser_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    content=excluded.content,
                    media_uri=excluded.media_uri,
                    source_name=excluded.source_name,
                    locator_json=excluded.locator_json,
                    parser_backend=excluded.parser_backend,
                    parser_version=excluded.parser_version
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
                        item.created_at.isoformat(),
                    )
                    for item in segments
                ],
            )


evidence_store = EvidenceStore()
