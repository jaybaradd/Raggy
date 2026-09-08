"""
api/schemas.py — Pydantic request / response models for all API endpoints.

Keeping schemas in one file makes it easy to see the full API surface at a
glance and avoids circular imports between routers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal


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
    inline_context: str = Field(default="")  # parsed content of a chat-attached file (bypasses retrieval)
    attachments: list[dict] = Field(default_factory=list)  # stub for Phase 3 inline multimodal


# ── Documents ─────────────────────────────────────────────────────────────────

class UploadDocumentResponse(BaseModel):
    doc_id: str
    filename: str
    modality: str = ""
    status: str
    chunk_count: int = 0
    message: str = ""


class DocumentStatusResponse(BaseModel):
    doc_id: str
    modality: str = ""
    status: str
    chunk_count: int
    message: str = ""


class ParseDocumentResponse(BaseModel):
    filename: str
    modality: str
    parsed_text: str  # raw parsed content, injected as inline_context in the chat turn


class YouTubeIngestRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    qdrant: dict


# ── Memories ─────────────────────────────────────────────────────────────────

class MemoryListResponse(BaseModel):
    memories: list[dict]


class MemoryPromotionRequest(BaseModel):
    scope: Literal["user", "project"]
    project_scope: str | None = Field(default=None, max_length=200)


class MemoryEditRequest(BaseModel):
    payload: dict


class MemorySupersedeRequest(BaseModel):
    replacement_memory_id: str
