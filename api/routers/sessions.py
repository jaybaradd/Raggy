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
)
from db.session_store import session_store

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(body: CreateSessionRequest) -> SessionResponse:
    session = session_store.create_session(title=body.title)
    return SessionResponse(
        session_id=session["session_id"],
        title=session["title"],
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
        created_at=session["created_at"],
    )
