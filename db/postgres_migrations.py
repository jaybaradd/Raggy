"""Postgres migration runner and logical schema equivalents for sessions."""
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
                applied = {row[0] for row in cursor.fetchall()}
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
            turn_index INTEGER NOT NULL, trace_id UUID, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL, attachments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL, UNIQUE(session_id, turn_index))""")
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
    return [PostgresMigration(1, "sessions_and_messages", initial),
            PostgresMigration(2, "projects_and_memberships", projects)]
