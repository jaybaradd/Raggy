"""PostgreSQL implementation of the durable session repository contract.

This is intentionally constructible directly for migration and integration
testing.  The application factory continues to use SQLite until every
authoritative repository has a PostgreSQL implementation.
"""
from __future__ import annotations

import re
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from db.postgres_migrations import PostgresMigrationRunner, session_migrations


class PostgresSessionRepository:
    """Durable sessions, projects, memberships, and messages in PostgreSQL."""

    def __init__(self, database_url: str, *, schema: str | None = None) -> None:
        if not database_url:
            raise ValueError("database_url must not be blank")
        if schema is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("schema must be a simple PostgreSQL identifier")
        self._database_url = database_url
        self._schema = schema
        self._initialise()

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - exercised in deployment configuration
            raise RuntimeError("psycopg is required for the PostgreSQL session repository") from error
        connection = psycopg.connect(self._database_url, row_factory=dict_row)
        if self._schema:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('search_path', %s, false)", (f"{self._schema},public",))
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            PostgresMigrationRunner("sessions").apply(connection, session_migrations())

    @staticmethod
    def _normalise_project_name(name: str) -> str:
        return " ".join(name.split()).casefold()

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @classmethod
    def _session_from_row(cls, row: dict[str, Any]) -> dict:
        return {
            "session_id": str(row["session_id"]), "owner_id": row["owner_id"], "title": row["title"],
            "project_scope": row["project_scope"], "project_id": str(row["project_id"]) if row["project_id"] else None,
            "project_name": row["project_scope"], "created_at": cls._timestamp(row["created_at"]),
            "updated_at": cls._timestamp(row["updated_at"]),
        }

    @classmethod
    def _project_from_row(cls, row: dict[str, Any]) -> dict:
        return {
            "project_id": str(row["project_id"]), "owner_id": row["owner_id"], "name": row["name"],
            "normalized_name": row["normalized_name"], "created_at": cls._timestamp(row["created_at"]),
            "updated_at": cls._timestamp(row["updated_at"]),
            "archived_at": cls._timestamp(row["archived_at"]) if row["archived_at"] else None,
        }

    @classmethod
    def _message_from_row(cls, row: dict[str, Any]) -> dict:
        return {
            "message_id": str(row["message_id"]), "trace_id": row["trace_id"], "role": row["role"],
            "content": row["content"], "created_at": cls._timestamp(row["created_at"]),
            "attachments": row["attachments_json"] or [],
        }

    def _get_project(self, cursor: Any, project_id: str, owner_id: str) -> dict | None:
        cursor.execute("""SELECT projects.* FROM projects JOIN project_memberships
            ON project_memberships.project_id = projects.project_id
            WHERE projects.project_id = %s AND project_memberships.owner_id = %s
              AND projects.archived_at IS NULL""", (project_id, owner_id))
        row = cursor.fetchone()
        return self._project_from_row(row) if row else None

    def _resolve_or_create_project(self, cursor: Any, owner_id: str, name: str) -> dict:
        clean_name = " ".join(name.split())
        if not clean_name:
            raise ValueError("project name must not be blank")
        normalized = self._normalise_project_name(clean_name)
        cursor.execute("SELECT * FROM projects WHERE owner_id = %s AND normalized_name = %s", (owner_id, normalized))
        existing = cursor.fetchone()
        if existing:
            return self._project_from_row(existing)
        now = datetime.now(timezone.utc)
        project_id = uuid.uuid4()
        cursor.execute("""INSERT INTO projects (
            project_id, owner_id, name, normalized_name, created_at, updated_at, archived_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NULL)""", (project_id, owner_id, clean_name, normalized, now, now))
        cursor.execute("""INSERT INTO project_memberships (project_id, owner_id, role, created_at)
            VALUES (%s, %s, 'owner', %s)""", (project_id, owner_id, now))
        return {"project_id": str(project_id), "owner_id": owner_id, "name": clean_name,
                "normalized_name": normalized, "created_at": self._timestamp(now),
                "updated_at": self._timestamp(now), "archived_at": None}

    def create_project(self, name: str, owner_id: str = "default") -> dict:
        with self._connect() as connection, connection.cursor() as cursor:
            return self._resolve_or_create_project(cursor, owner_id, name)

    def get_project(self, project_id: str, owner_id: str = "default") -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            return self._get_project(cursor, project_id, owner_id)

    def list_projects(self, owner_id: str = "default") -> list[dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT projects.* FROM projects JOIN project_memberships
                ON project_memberships.project_id = projects.project_id
                WHERE project_memberships.owner_id = %s AND projects.archived_at IS NULL
                ORDER BY projects.name""", (owner_id,))
            return [self._project_from_row(row) for row in cursor.fetchall()]

    def project_name_mapping(self) -> dict[tuple[str, str], dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM projects WHERE archived_at IS NULL")
            projects = [self._project_from_row(row) for row in cursor.fetchall()]
        return {(project["owner_id"], project["normalized_name"]): project for project in projects}

    def create_session(self, title: str = "New chat", project_scope: str | None = None,
                       project_id: str | None = None, owner_id: str = "default") -> dict:
        now = datetime.now(timezone.utc)
        session_id = uuid.uuid4()
        with self._connect() as connection, connection.cursor() as cursor:
            if project_id:
                project = self._get_project(cursor, project_id, owner_id)
                if project is None:
                    raise KeyError("Project not found or not accessible")
            elif project_scope and project_scope.strip():
                project = self._resolve_or_create_project(cursor, owner_id, project_scope)
            else:
                project = None
            cursor.execute("""INSERT INTO sessions (
                session_id, owner_id, title, project_scope, project_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)""", (
                session_id, owner_id, title, project["name"] if project else None,
                project["project_id"] if project else None, now, now,
            ))
        return {"session_id": str(session_id), "owner_id": owner_id, "title": title,
                "project_scope": project["name"] if project else None,
                "project_id": project["project_id"] if project else None,
                "project_name": project["name"] if project else None,
                "created_at": self._timestamp(now), "updated_at": self._timestamp(now)}

    def get_session(self, session_id: str, owner_id: str = "default") -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM sessions WHERE session_id = %s AND owner_id = %s", (session_id, owner_id))
            row = cursor.fetchone()
        return self._session_from_row(row) if row else None

    def update_project_scope(self, session_id: str, project_scope: str | None,
                             project_id: str | None = None, owner_id: str = "default") -> dict:
        now = datetime.now(timezone.utc)
        with self._connect() as connection, connection.cursor() as cursor:
            if project_id:
                project = self._get_project(cursor, project_id, owner_id)
                if project is None:
                    raise KeyError("Project not found or not accessible")
            elif project_scope:
                project = self._resolve_or_create_project(cursor, owner_id, project_scope)
            else:
                project = None
            cursor.execute("""UPDATE sessions SET project_scope = %s, project_id = %s, updated_at = %s
                WHERE session_id = %s AND owner_id = %s RETURNING *""", (
                project["name"] if project else None, project["project_id"] if project else None,
                now, session_id, owner_id,
            ))
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"Session '{session_id}' not found")
            return self._session_from_row(row)

    def list_sessions(self, owner_id: str = "default") -> list[dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM sessions WHERE owner_id = %s ORDER BY updated_at DESC", (owner_id,))
            return [self._session_from_row(row) for row in cursor.fetchall()]

    def append_message(self, session_id: str, role: str, content: str,
                       attachments: list[dict] | None = None, trace_id: str | None = None,
                       owner_id: str = "default") -> str:
        if role not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        message_id, now = uuid.uuid4(), datetime.now(timezone.utc)
        # Locking the session row serializes turn-index allocation per chat.
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM sessions WHERE session_id = %s AND owner_id = %s FOR UPDATE", (session_id, owner_id))
            if cursor.fetchone() is None:
                raise KeyError(f"Session '{session_id}' not found")
            cursor.execute("SELECT COALESCE(MAX(turn_index), 0) + 1 AS next_turn FROM session_messages WHERE session_id = %s", (session_id,))
            next_turn = cursor.fetchone()["next_turn"]
            cursor.execute("""INSERT INTO session_messages (
                message_id, session_id, turn_index, trace_id, role, content, attachments_json, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)""", (
                message_id, session_id, next_turn, trace_id, role, content,
                json.dumps(attachments or [], sort_keys=True), now,
            ))
            cursor.execute("UPDATE sessions SET updated_at = %s WHERE session_id = %s", (now, session_id))
        return str(message_id)

    def get_messages(self, session_id: str, owner_id: str = "default") -> list[dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM sessions WHERE session_id = %s AND owner_id = %s", (session_id, owner_id))
            if cursor.fetchone() is None:
                raise KeyError(f"Session '{session_id}' not found")
            cursor.execute("SELECT * FROM session_messages WHERE session_id = %s ORDER BY turn_index", (session_id,))
            return [self._message_from_row(row) for row in cursor.fetchall()]
