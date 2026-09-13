"""
api/schemas.py — Pydantic request / response models for all API endpoints.

Keeping schemas in one file makes it easy to see the full API surface at a
glance and avoids circular imports between routers.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from uuid import UUID


# ── Sessions ──────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: str = Field(default="New chat", max_length=200)
    project_scope: str | None = Field(default=None, max_length=200)


class UpdateSessionRequest(BaseModel):
    project_scope: str | None = Field(default=None, max_length=200)


class SessionResponse(BaseModel):
    session_id: str
    title: str
    project_scope: str | None = None
    created_at: str


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


# ── Messages ──────────────────────────────────────────────────────────────────

class AttachmentMetadata(BaseModel):
    """Durable metadata for a file associated with one chat turn.

    The conversation store deliberately retains references and descriptive
    metadata only. Raw bytes remain owned by the upload/ingestion subsystem.
    """

    model_config = ConfigDict(extra="forbid")

    attachment_id: UUID
    filename: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(min_length=1, max_length=255, pattern=r"^[^/\s]+/[^/\s]+$")
    size_bytes: int = Field(ge=0, le=2_147_483_647)
    source_mode: Literal["inline", "knowledge_base"]
    document_id: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_id: str | None = Field(default=None, min_length=1, max_length=200)


class SessionMessageResponse(BaseModel):
    message_id: str
    trace_id: str | None = None
    role: Literal["user", "assistant"]
    content: str
    created_at: str
    attachments: list[AttachmentMetadata] = Field(default_factory=list)


class SessionMessagesResponse(BaseModel):
    messages: list[SessionMessageResponse]


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=32_000)
    inline_context: str = Field(default="")  # parsed content of a chat-attached file (bypasses retrieval)
    use_knowledge_base: bool = Field(default=False)
    attachments: list[AttachmentMetadata] = Field(default_factory=list)


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


class MemoryConflictResolutionRequest(BaseModel):
    action: Literal["supersede_existing", "keep_existing", "expire_existing"]
