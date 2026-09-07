"""Stable Phase 1 asset/evidence contracts.

Parsers currently emit :class:`ParsedChunk`; these models provide the durable
identity and provenance envelope that later knowledge-atom extraction depends
on without forcing an all-at-once parser rewrite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from core.ingestion.parser import ParsedChunk, SourceLocator


class AssetRecord(BaseModel):
    asset_id: str
    owner_id: str | None = None
    project_scope: str | None = None
    filename: str
    media_type: str
    raw_file_uri: str
    content_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceSegment(BaseModel):
    evidence_id: str
    asset_id: str
    modality: Literal["text", "table", "image", "video", "audio"]
    representation: Literal["text", "ocr", "caption", "transcript", "frame", "layout"]
    content: str | None = None
    media_uri: str | None = None
    source_name: str | None = None
    locator: SourceLocator
    parent_evidence_id: str | None = None
    parser_backend: str
    parser_version: str = "1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_chunk(cls, chunk: ParsedChunk) -> "EvidenceSegment":
        modality = "video" if chunk.modality == "video_segment" else (
            "audio" if chunk.modality == "audio_segment" else chunk.modality
        )
        return cls(
            evidence_id=chunk.evidence_id or chunk.chunk_id,
            asset_id=chunk.doc_id,
            modality=modality,
            representation=chunk.representation,
            content=chunk.content,
            media_uri=chunk.raw_file_uri,
            source_name=chunk.source_name,
            locator=chunk.source_locator,
            parser_backend=chunk.parser_backend,
            created_at=chunk.created_at,
        )
