"""
api/routers/documents.py — document upload and ingestion.

Routes
------
POST /documents                 Upload a file (any supported modality)
POST /documents/youtube         Ingest a YouTube URL
GET  /documents/{doc_id}/status Ingestion status
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from api.schemas import DocumentStatusResponse, ParseDocumentResponse, UploadDocumentResponse, YouTubeIngestRequest
from config import settings
from core.embeddings import embedder
from core.ingestion.chunker import chunk_parsed_chunks
from core.ingestion.parser import compute_doc_id, get_parser
from core.storage.qdrant_store import qdrant_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

_doc_registry: dict[str, DocumentStatusResponse] = {}

_SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx",
    ".xlsx", ".csv",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mov", ".avi", ".webm",
}


def _upload_path(doc_id: str, filename: str) -> Path:
    """Return (and create) a stable path for a raw uploaded file."""
    dest = Path(settings.upload_dir) / doc_id
    dest.mkdir(parents=True, exist_ok=True)
    return dest / filename


@router.post("", response_model=UploadDocumentResponse, status_code=202)
async def upload_document(file: UploadFile) -> UploadDocumentResponse:
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()

    if ext not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ext}'. Supported: {sorted(_SUPPORTED_EXTENSIONS)}",
        )

    file_bytes = await file.read()
    doc_id = compute_doc_id(file_bytes)

    if doc_id in _doc_registry and _doc_registry[doc_id].status == "done":
        cached = _doc_registry[doc_id]
        return UploadDocumentResponse(
            doc_id=doc_id, filename=filename, status="done",
            chunk_count=cached.chunk_count, message="Already indexed.",
        )

    _doc_registry[doc_id] = DocumentStatusResponse(doc_id=doc_id, status="processing", chunk_count=0)

    # Persist raw file to disk so raw_file_uri is a real path
    file_path = _upload_path(doc_id, filename)
    file_path.write_bytes(file_bytes)
    modality = ext.lstrip(".")

    # Run the heavy parsing/embedding work off the event loop
    loop = asyncio.get_event_loop()
    try:
        chunk_count = await loop.run_in_executor(None, _ingest, file_path, doc_id, modality, filename)
    except Exception as exc:
        logger.exception("Ingestion failed for '%s'", filename)
        _doc_registry[doc_id] = DocumentStatusResponse(
            doc_id=doc_id, status="error", chunk_count=0, message=str(exc)
        )
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc

    _doc_registry[doc_id] = DocumentStatusResponse(doc_id=doc_id, status="done", chunk_count=chunk_count)
    return UploadDocumentResponse(
        doc_id=doc_id, filename=filename, status="done",
        chunk_count=chunk_count, message=f"Ingested {chunk_count} chunks.",
    )


@router.post("/parse", response_model=ParseDocumentResponse)
async def parse_document(file: UploadFile) -> ParseDocumentResponse:
    """Parse a file and return its text content — no Qdrant storage.

    Used when a file is attached inline to a chat message. The parsed text is
    sent back to the frontend and injected directly into that turn's prompt.
    """
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()

    if ext not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ext}'. Supported: {sorted(_SUPPORTED_EXTENSIONS)}",
        )

    file_bytes = await file.read()
    modality = ext.lstrip(".")

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        parser = get_parser(modality)
        doc_id = compute_doc_id(file_bytes)
        raw_chunks = parser.parse(tmp_path, doc_id)
    finally:
        tmp_path.unlink(missing_ok=True)

    # Concatenate all parsed chunk content into one string
    parsed_text = "\n\n".join(c.content for c in raw_chunks if c.content.strip())
    return ParseDocumentResponse(filename=filename, modality=modality, parsed_text=parsed_text)


@router.post("/youtube", response_model=UploadDocumentResponse, status_code=202)
async def ingest_youtube(body: YouTubeIngestRequest) -> UploadDocumentResponse:
    """Ingest a YouTube video by URL — yt-dlp download + Whisper transcription."""
    url = body.url
    doc_id = compute_doc_id(url.encode())

    if doc_id in _doc_registry and _doc_registry[doc_id].status == "done":
        cached = _doc_registry[doc_id]
        return UploadDocumentResponse(
            doc_id=doc_id, filename=url, status="done",
            chunk_count=cached.chunk_count, message="Already indexed.",
        )

    _doc_registry[doc_id] = DocumentStatusResponse(doc_id=doc_id, status="processing", chunk_count=0)

    # YouTubeParser reads the URL from file_path.name — pass it as a fake Path
    fake_path = Path(url)

    # yt-dlp + Whisper can take several minutes — run off the event loop
    loop = asyncio.get_event_loop()
    try:
        chunk_count = await loop.run_in_executor(None, _ingest, fake_path, doc_id, "youtube", url)
    except Exception as exc:
        logger.exception("YouTube ingestion failed for '%s'", url)
        _doc_registry[doc_id] = DocumentStatusResponse(
            doc_id=doc_id, status="error", chunk_count=0, message=str(exc)
        )
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc

    _doc_registry[doc_id] = DocumentStatusResponse(doc_id=doc_id, status="done", chunk_count=chunk_count)
    return UploadDocumentResponse(
        doc_id=doc_id, filename=url, status="done",
        chunk_count=chunk_count, message=f"Ingested {chunk_count} chunks from YouTube.",
    )


@router.get("/{doc_id}/status", response_model=DocumentStatusResponse)
def document_status(doc_id: str) -> DocumentStatusResponse:
    record = _doc_registry.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return record


# ── Internal ingestion pipeline ───────────────────────────────────────────────

def _ingest(file_path: Path, doc_id: str, modality: str, filename: str) -> int:
    logger.info("Parsing '%s' (doc_id=%s, modality=%s) …", filename, doc_id[:8], modality)
    parser = get_parser(modality)
    raw_chunks = parser.parse(file_path, doc_id)
    logger.info("Parsed %d raw blocks; chunking …", len(raw_chunks))

    chunks = chunk_parsed_chunks(raw_chunks)
    if not chunks:
        logger.warning("No chunks produced for '%s'", filename)
        return 0

    logger.info("Embedding %d chunks …", len(chunks))
    texts = [c.content for c in chunks]
    vectors = embedder.encode(texts)

    for chunk in chunks:
        chunk.embedding_model = settings.local_embed_model

    qdrant_store.upsert(chunks, vectors)
    logger.info("Stored %d chunks for doc_id=%s", len(chunks), doc_id[:8])
    return len(chunks)
