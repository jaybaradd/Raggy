"""
db/session_store.py — in-memory session store for Phase 0.

Phase 0 intentionally has no persistence — sessions and their message histories
are held in a dict and lost when the server restarts.  This is the seam Phase 2
replaces with Postgres.

The store is thread-safe for asyncio (single-threaded event loop) but not for
multi-process deployments (that is a Phase 5 concern).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TypedDict


class Message(TypedDict):
    message_id: str
    role: str        # "user" | "assistant"
    content: str
    created_at: str
    attachments: list[dict]  # [{type, filename, doc_id}] — populated by Phase 3


class Session(TypedDict):
    session_id: str
    title: str
    created_at: str
    messages: list[Message]


class SessionStore:
    """
    Simple in-memory dict-backed session store.

    All methods are synchronous — no I/O involved.  Async callers can call
    them directly without await.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create_session(self, title: str = "New chat") -> Session:
        session_id = str(uuid.uuid4())
        session: Session = {
            "session_id": session_id,
            "title": title,
            "created_at": _now_iso(),
            "messages": [],
        }
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[Session]:
        return sorted(
            self._sessions.values(),
            key=lambda s: s["created_at"],
            reverse=True,
        )

    def append_message(self, session_id: str, role: str, content: str, attachments: list[dict] | None = None) -> str:
        """Append a message to a session's history."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session '{session_id}' not found")
        message_id = str(uuid.uuid4())
        session["messages"].append({
            "message_id": message_id,
            "role": role,
            "content": content,
            "created_at": _now_iso(),
            "attachments": attachments or [],
        })
        return message_id

    def get_messages(self, session_id: str) -> list[Message]:
        """Return the full message history for a session (for LLM context)."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session '{session_id}' not found")
        return session["messages"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Module-level singleton
session_store = SessionStore()
