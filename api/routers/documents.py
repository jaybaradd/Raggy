"""
api/routers/documents.py — document upload and ingestion endpoint.

Routes
------
POST /documents               Upload a file; ingestion runs synchronously in Phase 0.
GET  /documents/{doc_id}/status  Return ingestion status for a document.

Phase 0 note: ingestion is synchronous (blocks the request) because we have no
task queue yet.  Phase 1 will move this to a background worker (ARQ/Celery).
The response shape and status polling endpoint are already in place so the
frontend code doesn't need to change in Phase 1.
"""

from __future__ import annotations

import logging
import mimetypes
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from api.schemas import DocumentStatusResponse, UploadDocumentResponse
from core.embeddings import embedder
from core.ingestion.chunker import chunk_parsed_chunks
from core.ingestion.parser import compute_doc_id, get_parser
from core.storage.qdrant_store import qdrant_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

# In-memory document registry (Phase 0 — no Postgres yet)
_doc_registry: dict[str, DocumentStatusResponse] = {}

_SUPPORTED_EXTENSIONS = {".pdf"}


@router.post("", response_model=UploadDocumentResponse, status_code=202)
async def upload_document(file: UploadFile) -> UploadDocumentResponse:
    """
    Accept a file upload, parse it, chunk it, embed it, and store in Qdrant.

    Returns 202 Accepted immediately in the response shape; the body includes
    the final status since Phase 0 runs ingestion synchronously.
    """
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()

    if ext not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type '{ext}'. "
                f"Supported in Phase 0: {sorted(_SUPPORTED_EXTENSIONS)}"
            ),
        )

    # Read file bytes once
    file_bytes = await file.read()
    doc_id = compute_doc_id(file_bytes)

    # Idempotency: if we've already processed this exact file, return cached result
    if doc_id in _doc_registry and _doc_registry[doc_id].status == "done":
        cached = _doc_registry[doc_id]
        return UploadDocumentResponse(
            doc_id=doc_id,
            filename=filename,
            status="done",
            chunk_count=cached.chunk_count,
            message="Already indexed (cached result).",
        )

    # Register as processing
    _doc_registry[doc_id] = DocumentStatusResponse(
        doc_id=doc_id,
        status="processing",
        chunk_count=0,
    )

    try:
        chunk_count = _ingest(file_bytes, doc_id, ext.lstrip("."), filename)
    except Exception as exc:
        logger.exception("Ingestion failed for '%s'", filename)
        _doc_registry[doc_id] = DocumentStatusResponse(
            doc_id=doc_id,
            status="error",
            chunk_count=0,
            message=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc

    _doc_registry[doc_id] = DocumentStatusResponse(
        doc_id=doc_id,
        status="done",
        chunk_count=chunk_count,
    )

    return UploadDocumentResponse(
        doc_id=doc_id,
        filename=filename,
        status="done",
        chunk_count=chunk_count,
        message=f"Ingested {chunk_count} chunks successfully.",
    )


@router.get("/{doc_id}/status", response_model=DocumentStatusResponse)
def document_status(doc_id: str) -> DocumentStatusResponse:
    """Return the current ingestion status for a document."""
    record = _doc_registry.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return record


# ── Internal ingestion pipeline ───────────────────────────────────────────────

def _ingest(file_bytes: bytes, doc_id: str, modality: str, filename: str) -> int:
    """
    Full ingestion pipeline:  parse → chunk → embed → upsert.

    Returns the number of chunks stored.
    """
    # Write bytes to a temp file so Docling can open it by path
    with tempfile.NamedTemporaryFile(suffix=f".{modality}", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        logger.info("Parsing '%s' (doc_id=%s) …", filename, doc_id[:8])
        parser = get_parser(modality)
        raw_chunks = parser.parse(tmp_path, doc_id)

        logger.info("Parsed %d raw blocks; chunking …", len(raw_chunks))
        chunks = chunk_parsed_chunks(raw_chunks)

        if not chunks:
            logger.warning("No chunks produced for '%s'", filename)
            return 0

        logger.info("Embedding %d chunks …", len(chunks))
        texts = [c.content for c in chunks]
        vectors = embedder.encode(texts)

        # Tag each chunk with the embedding model used
        model_name = (
            settings_embed_model_name()
        )
        for chunk in chunks:
            chunk.embedding_model = model_name

        qdrant_store.upsert(chunks, vectors)
        logger.info("Stored %d chunks for doc_id=%s", len(chunks), doc_id[:8])
        return len(chunks)

    finally:
        tmp_path.unlink(missing_ok=True)


def settings_embed_model_name() -> str:
    """Return the active embedding model name for provenance tagging."""
    from config import settings

    return settings.local_embed_model
