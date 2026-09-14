"""Versioned PostgreSQL schema for authoritative conversation records."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

PostgresMigrationFn = Callable[[Any], None]

@dataclass(frozen=True)
class PostgresMigration:
    version: int
    name: str
    apply: PostgresMigrationFn

class PostgresMigrationRunner:
    def __init__(self, component: str) -> None: self.component = component
    def apply(self, connection: Any, migrations: list[PostgresMigration]) -> list[int]:
        versions = [m.version for m in migrations]
        if versions != sorted(versions) or len(set(versions)) != len(versions):
            raise ValueError("migrations must have unique ascending versions")
        try:
            with connection.cursor() as cursor:
                cursor.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                component TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(component, version))""")
                cursor.execute("SELECT version FROM schema_migrations WHERE component = %s", (self.component,))
                # Repositories may opt into psycopg's ``dict_row`` factory.
                # Keep the migration runner usable with either tuple or mapping rows.
                applied = {
                    row["version"] if isinstance(row, dict) else row[0]
                    for row in cursor.fetchall()
                }
                completed = []
                for migration in migrations:
                    if migration.version in applied: continue
                    migration.apply(cursor)
                    cursor.execute("INSERT INTO schema_migrations (component, version, name) VALUES (%s, %s, %s)",
                                   (self.component, migration.version, migration.name))
                    completed.append(migration.version)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return completed

def session_migrations() -> list[PostgresMigration]:
    def initial(cursor: Any) -> None:
        cursor.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_id UUID PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
            project_scope TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS session_messages (
            message_id UUID PRIMARY KEY, session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            turn_index INTEGER NOT NULL, trace_id TEXT, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL, attachments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL, UNIQUE(session_id, turn_index))""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated ON sessions(owner_id, updated_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_messages_session_turn ON session_messages(session_id, turn_index)")
    def projects(cursor: Any) -> None:
        cursor.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS project_id UUID")
        cursor.execute("""CREATE TABLE IF NOT EXISTS projects (
            project_id UUID PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
            normalized_name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL, archived_at TIMESTAMPTZ,
            UNIQUE(owner_id, normalized_name))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS project_memberships (
            project_id UUID NOT NULL REFERENCES projects(project_id), owner_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')), created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(project_id, owner_id))""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_project_id ON sessions(owner_id, project_id, updated_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_memberships_owner ON project_memberships(owner_id, project_id)")
    return [PostgresMigration(1, "sessions_and_messages", initial),
            PostgresMigration(2, "projects_and_memberships", projects)]


def memory_migrations() -> list[PostgresMigration]:
    """Authoritative memory schema. Projection targets remain derived stores."""
    def initial(cursor: Any) -> None:
        cursor.execute("""CREATE TABLE IF NOT EXISTS memory_records (
            memory_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
            session_id TEXT, project_id TEXT, project_scope TEXT, kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('candidate','active','superseded','rejected','expired','deleted')),
            confidence DOUBLE PRECISION NOT NULL, user_confirmed BOOLEAN NOT NULL,
            evidence_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb, source_turn_id TEXT,
            extraction_model TEXT NOT NULL, extraction_version TEXT NOT NULL, identity_key TEXT,
            valid_from TIMESTAMPTZ NOT NULL, valid_to TIMESTAMPTZ, superseded_by TEXT,
            payload_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_records(owner_id, scope, session_id, project_scope, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_expiry ON memory_records(status, valid_to)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_event_identity ON memory_records(owner_id, scope, session_id, project_id, project_scope, identity_key)")
        cursor.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_active_event_identity
            ON memory_records(owner_id, scope, COALESCE(session_id, ''), COALESCE(project_id, project_scope, ''), identity_key)
            WHERE kind = 'event' AND status IN ('candidate','active') AND identity_key IS NOT NULL""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS memory_audit_events (
            event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            event_type TEXT NOT NULL, actor_id TEXT, details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL)""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_audit_memory ON memory_audit_events(memory_id, event_id DESC)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS memory_access_events (
            access_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, trace_id TEXT NOT NULL,
            session_id TEXT NOT NULL, message_id TEXT, memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            event_type TEXT NOT NULL, prompt_label TEXT, rank INTEGER, score DOUBLE PRECISION,
            details_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL)""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_memory ON memory_access_events(memory_id, access_event_id DESC)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS memory_extractions (
            source_turn_id TEXT NOT NULL, extraction_version TEXT NOT NULL, status TEXT NOT NULL,
            error TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(source_turn_id, extraction_version))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS memory_relationships (
            relationship_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            from_memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            to_memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            relationship_type TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS memory_conflicts (
            conflict_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            incoming_memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            existing_memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            conflict_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', details_json JSONB NOT NULL,
            resolved_by TEXT, resolution_json JSONB, created_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ,
            UNIQUE(incoming_memory_id, existing_memory_id, status))""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_conflicts_status ON memory_conflicts(status, created_at DESC)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS memory_projection_jobs (
            job_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            target TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
            locked_at TIMESTAMPTZ, locked_by TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(memory_id, target))""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_projection_jobs_pending ON memory_projection_jobs(status, updated_at)")
    return [PostgresMigration(1, "memory_records_lifecycle_and_outbox", initial)]


def evidence_migrations() -> list[PostgresMigration]:
    """Immutable source provenance plus durable ingestion/outbox state."""
    def initial(cursor: Any) -> None:
        cursor.execute("""CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY, filename TEXT NOT NULL, media_type TEXT NOT NULL,
            raw_file_uri TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(content_hash))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS asset_bindings (
            binding_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id), owner_id TEXT NOT NULL,
            project_id TEXT, project_scope TEXT, created_at TIMESTAMPTZ NOT NULL,
            UNIQUE NULLS NOT DISTINCT(asset_id, owner_id, project_id))""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_asset_bindings_owner_project ON asset_bindings(owner_id, project_id)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id), owner_id TEXT NOT NULL,
            project_id TEXT, project_scope TEXT, modality TEXT NOT NULL, status TEXT NOT NULL,
            parser_backend TEXT, parser_version TEXT, chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT,
            created_at TIMESTAMPTZ NOT NULL, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ)""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingestion_runs_asset ON ingestion_runs(asset_id, created_at DESC)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS evidence_segments (
            evidence_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id), modality TEXT NOT NULL,
            representation TEXT NOT NULL, content TEXT, media_uri TEXT, source_name TEXT,
            locator_json JSONB NOT NULL, parent_evidence_id TEXT, parser_backend TEXT NOT NULL,
            parser_version TEXT NOT NULL, retrieval_metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            content_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_evidence_segments_asset ON evidence_segments(asset_id)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS evidence_projection_jobs (
            job_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES evidence_segments(evidence_id),
            target TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
            locked_at TIMESTAMPTZ, locked_by TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(evidence_id, target))""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_evidence_projection_pending ON evidence_projection_jobs(status, updated_at)")
    return [PostgresMigration(1, "assets_evidence_ingestion_and_outbox", initial)]
