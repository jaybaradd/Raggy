"""
core/ingestion/parser.py — Parser abstraction + PDF parser (Phase 0).

The Parser ABC defines the contract every modality parser must fulfil.
PdfParser implements it using Docling.

Phase 1 will add: DocxParser, XlsxParser, ImageParser, VideoParser, YouTubeParser.
All of them write into the same ParsedChunk schema so the rest of the pipeline
is completely modality-agnostic.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


# ── Canonical schema ──────────────────────────────────────────────────────────

class SourceLocator(BaseModel):
    """Pinpoints where inside the source document a chunk came from."""

    page: int | None = None
    time_range: tuple[float, float] | None = None  # video / audio
    cell_range: str | None = None                  # spreadsheets, e.g. "Sheet1!A2:D8"
    bbox: tuple[float, float, float, float] | None = None  # images


class ParsedChunk(BaseModel):
    """
    Atomic unit that every parser produces and every downstream stage consumes.

    Fields
    ------
    chunk_id        : Stable UUID derived from (doc_id, chunk_index).
    doc_id          : SHA-256 of the raw file bytes — same file always yields
                      the same doc_id regardless of filename.
    modality        : Discriminator used to route into the right Qdrant collection.
    content         : The text the embedding model will see (extracted text or
                      caption/transcript for non-text modalities).
    context_prefix  : Contextual-retrieval sentence (Phase 1+ — None in Phase 0).
    source_locator  : Provenance inside the source document.
    raw_file_uri    : Where the original file is stored (local path in Phase 0;
                      S3/GCS URI in Phase 1+).
    parser_backend  : Which parser produced this chunk (for debugging + auditing).
    embedding_model : Filled in by the embedding layer, not the parser.
    created_at      : UTC timestamp.
    """

    chunk_id: str
    doc_id: str
    modality: Literal["text", "table", "image", "video_segment", "audio_segment"]
    content: str
    context_prefix: str | None = None
    source_locator: SourceLocator
    raw_file_uri: str
    parser_backend: str
    embedding_model: str = ""   # set later by the embedding layer
    created_at: datetime = datetime.now(timezone.utc)


# ── Abstract Parser ───────────────────────────────────────────────────────────

class Parser(ABC):
    """
    Every modality parser must implement ``parse``.

    The return value is always a list of ParsedChunks — even if the modality
    is a single image, it returns a one-element list.
    """

    @abstractmethod
    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        """
        Parse *file_path* and return its chunks.

        Parameters
        ----------
        file_path : Path to the uploaded file on disk.
        doc_id    : Caller-supplied doc identifier (SHA-256 of file bytes).
        """
        ...


# ── PDF Parser (Phase 0) ──────────────────────────────────────────────────────

class PdfParser(Parser):
    """
    Parses PDF files using Docling.

    Docling handles text extraction, table detection, and figure captioning
    internally.  In Phase 0 we only expose the text chunks; other modalities
    (tables, images) will be wired in Phase 1.

    The raw text from Docling is NOT chunked here — chunking is a separate
    concern handled by core.ingestion.chunker.  This parser only extracts
    page-level text blocks and records their page number in SourceLocator.
    """

    BACKEND = "docling-pdf"

    def __init__(self) -> None:
        # Lazy import so tests that don't need Docling don't pay the import cost.
        from docling.document_converter import DocumentConverter

        self._converter = DocumentConverter()

    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        """
        Convert a PDF to a list of ParsedChunks (one per paragraph/block).

        Docling's DocumentConverter returns a DoclingDocument whose ``export_to_dict``
        gives us structured blocks.  We iterate over text items and record the
        page number from each item's provenance.
        """
        result = self._converter.convert(str(file_path))
        doc = result.document

        raw_file_uri = file_path.resolve().as_uri()
        chunks: list[ParsedChunk] = []

        # export_to_markdown gives a clean, linearised representation;
        # we also iterate structured items to get per-page provenance.
        for idx, (item, _level) in enumerate(doc.iterate_items()):
            # Only process text-bearing items in Phase 0
            item_type = type(item).__name__
            if item_type not in ("TextItem", "SectionHeaderItem", "ListItem"):
                continue

            text = item.text.strip() if hasattr(item, "text") else ""
            if not text:
                continue

            # Extract page number from provenance if available
            page_no: int | None = None
            if hasattr(item, "prov") and item.prov:
                prov = item.prov[0]
                if hasattr(prov, "page_no"):
                    page_no = prov.page_no

            chunk_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{idx}")
            )

            chunks.append(
                ParsedChunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    modality="text",
                    content=text,
                    source_locator=SourceLocator(page=page_no),
                    raw_file_uri=raw_file_uri,
                    parser_backend=self.BACKEND,
                )
            )

        return chunks


# ── Helpers ───────────────────────────────────────────────────────────────────

def compute_doc_id(file_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of *file_bytes* (used as stable doc_id)."""
    return hashlib.sha256(file_bytes).hexdigest()


def get_parser(modality: str) -> Parser:
    """
    Factory that returns the right Parser for a given modality string.

    Phase 0 only supports "pdf".  Phase 1 will extend this map.
    """
    parsers: dict[str, type[Parser]] = {
        "pdf": PdfParser,
    }
    if modality not in parsers:
        raise ValueError(
            f"Unsupported modality '{modality}'. "
            f"Supported in this phase: {list(parsers.keys())}"
        )
    return parsers[modality]()
