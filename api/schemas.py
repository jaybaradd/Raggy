"""
api/schemas.py — Pydantic request / response models for all API endpoints.

Keeping schemas in one file makes it easy to see the full API surface at a
glance and avoids circular imports between routers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Sessions ──────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: str = Field(default="New chat", max_length=200)


class SessionResponse(BaseModel):
    session_id: str
    title: str
    created_at: str


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


# ── Messages ──────────────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=32_000)
    # Phase 1+ will add: retrieval_modes, doc_id_filter, etc.


# ── Documents ─────────────────────────────────────────────────────────────────

class UploadDocumentResponse(BaseModel):
    doc_id: str
    filename: str
    status: str   # "processing" | "done" | "error"
    chunk_count: int = 0
    message: str = ""


class DocumentStatusResponse(BaseModel):
    doc_id: str
    status: str
    chunk_count: int
    message: str = ""


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    qdrant: dict
