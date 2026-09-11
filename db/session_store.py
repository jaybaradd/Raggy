"""Durable local session and message repository.

SQLite is the development implementation of the same repository boundary that
will move to Postgres. Sessions and messages are authoritative conversation
history; memory remains a separate, derived lifecycle system.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TypedDict

from config import settings


class Message(TypedDict):
    message_id: str
    trace_id: str | None
    role: str
    content: str
    created_at: str
    attachments: list[dict]


class Session(TypedDict):
    session_id: str
    owner_id: str
    title: str
    project_scope: str | None
    created_at: str
    updated_at: str


class SessionStore:
    """SQLite-backed session repository with a future Postgres-compatible API."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = Path(db_path or settings.session_db_path)
        if self._path.parent != Path("."):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialise(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    project_scope TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated
                    ON sessions(owner_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_owner_project
                    ON sessions(owner_id, project_scope, updated_at DESC);

                CREATE TABLE IF NOT EXISTS session_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    trace_id TEXT,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    attachments_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, turn_index),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_session_messages_session_turn
                    ON session_messages(session_id, turn_index);
            """)

    def create_session(self, title: str = "New chat", project_scope: str | None = None,
                       owner_id: str = "default") -> Session:
        now = _now_iso()
        session: Session = {
            "session_id": str(uuid.uuid4()),
            "owner_id": owner_id,
            "title": title,
            "project_scope": project_scope,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO sessions (
                    session_id, owner_id, title, project_scope, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (session["session_id"], session["owner_id"], session["title"],
                 session["project_scope"], session["created_at"], session["updated_at"]),
            )
        return session

    def get_session(self, session_id: str, owner_id: str = "default") -> Session | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND owner_id = ?", (session_id, owner_id)
            ).fetchone()
        return self._session_from_row(row) if row else None

    def update_project_scope(self, session_id: str, project_scope: str | None,
                             owner_id: str = "default") -> Session:
        now = _now_iso()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE sessions SET project_scope = ?, updated_at = ?
                   WHERE session_id = ? AND owner_id = ?""",
                (project_scope, now, session_id, owner_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Session '{session_id}' not found")
        session = self.get_session(session_id, owner_id)
        assert session is not None
        return session

    def list_sessions(self, owner_id: str = "default") -> list[Session]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE owner_id = ? ORDER BY updated_at DESC", (owner_id,)
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def append_message(self, session_id: str, role: str, content: str,
                       attachments: list[dict] | None = None, trace_id: str | None = None,
                       owner_id: str = "default") -> str:
        """Append one ordered message and advance the session's activity time."""
        if role not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        message_id = str(uuid.uuid4())
        now = _now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND owner_id = ?", (session_id, owner_id)
            ).fetchone()
            if session is None:
                raise KeyError(f"Session '{session_id}' not found")
            next_turn = connection.execute(
                "SELECT COALESCE(MAX(turn_index), 0) + 1 FROM session_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO session_messages (
                    message_id, session_id, turn_index, trace_id, role, content, attachments_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, session_id, next_turn, trace_id, role, content,
                 json.dumps(attachments or [], sort_keys=True), now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id)
            )
        return message_id

    def get_messages(self, session_id: str, owner_id: str = "default") -> list[Message]:
        with self._connect() as connection:
            allowed = connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND owner_id = ?", (session_id, owner_id)
            ).fetchone()
            if allowed is None:
                raise KeyError(f"Session '{session_id}' not found")
            rows = connection.execute(
                "SELECT * FROM session_messages WHERE session_id = ? ORDER BY turn_index", (session_id,)
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"], owner_id=row["owner_id"], title=row["title"],
            project_scope=row["project_scope"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        return Message(
            message_id=row["message_id"], trace_id=row["trace_id"], role=row["role"],
            content=row["content"], created_at=row["created_at"],
            attachments=json.loads(row["attachments_json"]),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


session_store = SessionStore()
