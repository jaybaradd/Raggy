"""
api/routers/sessions.py — session CRUD endpoints.

Routes
------
POST   /sessions          Create a new session.
GET    /sessions          List all sessions.
GET    /sessions/{id}     Get a single session.
"""

from fastapi import APIRouter, HTTPException

from api.schemas import (
    CreateSessionRequest,
    SessionListResponse,
    SessionResponse,
    UpdateSessionRequest,
)
from db.session_store import session_store

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(body: CreateSessionRequest) -> SessionResponse:
    session = session_store.create_session(title=body.title, project_scope=body.project_scope)
    return SessionResponse(
        session_id=session["session_id"],
        title=session["title"],
        project_scope=session["project_scope"],
        created_at=session["created_at"],
    )


@router.get("", response_model=SessionListResponse)
def list_sessions() -> SessionListResponse:
    sessions = session_store.list_sessions()
    return SessionListResponse(
        sessions=[
            SessionResponse(
                session_id=s["session_id"],
                title=s["title"],
                project_scope=s["project_scope"],
                created_at=s["created_at"],
            )
            for s in sessions
        ]
    )


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: str) -> SessionResponse:
    session = session_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionResponse(
        session_id=session["session_id"],
        title=session["title"],
        project_scope=session["project_scope"],
        created_at=session["created_at"],
    )


@router.patch("/{session_id}", response_model=SessionResponse)
def update_session(session_id: str, body: UpdateSessionRequest) -> SessionResponse:
    """Move an existing chat into a project, or remove it from one."""
    project_scope = body.project_scope.strip() if body.project_scope else None
    try:
        session = session_store.update_project_scope(session_id, project_scope)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return SessionResponse(
        session_id=session["session_id"],
        title=session["title"],
        project_scope=session["project_scope"],
        created_at=session["created_at"],
    )
