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
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api.schemas import DocumentStatusResponse, ParseDocumentResponse, UploadDocumentResponse, YouTubeIngestRequest
from config import settings
from core.ingestion.chunker import chunk_parsed_chunks
from core.ingestion.models import AssetRecord, EvidenceSegment
from core.ingestion.parser import compute_doc_id, get_parser
from core.evidence.projections import sync_pending_evidence_projections
from db.repository_factory import repositories

logger = logging.getLogger(__name__)
evidence_store = repositories.evidence
router = APIRouter(prefix="/documents", tags=["documents"])

_doc_registry: dict[str, DocumentStatusResponse] = {}

_SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx",
    ".xlsx", ".csv",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mov", ".avi", ".webm",
}


def _upload_path(doc_id: str, filename: str) -> Path:
    """Return a stable, traversal-safe path for a raw uploaded file."""
    dest = Path(settings.upload_dir) / doc_id
    dest.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name or "upload"
    return dest / safe_name


async def _run_ingestion_job(
    file_path: Path | str,
    doc_id: str,
    modality: str,
    filename: str,
    run_id: str | None = None,
) -> None:
    """Run heavy parsing off the event loop and update the in-memory status."""
    try:
        chunk_count = await asyncio.to_thread(_ingest, file_path, doc_id, modality, filename)
    except Exception as exc:
        logger.exception("Ingestion failed for '%s'", filename)
        _doc_registry[doc_id] = DocumentStatusResponse(
            doc_id=doc_id, modality=modality, status="error", chunk_count=0, message=str(exc)
        )
        if run_id:
            evidence_store.complete_ingestion(run_id, chunk_count=0, error=str(exc))
        return

    _doc_registry[doc_id] = DocumentStatusResponse(
        doc_id=doc_id, modality=modality, status="done", chunk_count=chunk_count
    )
    if run_id:
        evidence_store.complete_ingestion(run_id, chunk_count=chunk_count)


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

    if doc_id in _doc_registry and _doc_registry[doc_id].status in {"processing", "done"}:
        cached = _doc_registry[doc_id]
        return UploadDocumentResponse(
            doc_id=doc_id, filename=filename, modality=ext.lstrip("."), status=cached.status,
            chunk_count=cached.chunk_count,
            message="Already indexed." if cached.status == "done" else "Ingestion already processing.",
        )

    modality = ext.lstrip(".")
    _doc_registry[doc_id] = DocumentStatusResponse(
        doc_id=doc_id, modality=modality, status="processing", chunk_count=0
    )

    # Persist raw file to disk so raw_file_uri is a real path
    file_path = _upload_path(doc_id, filename)
    file_path.write_bytes(file_bytes)
    evidence_store.upsert_asset(AssetRecord(
        asset_id=doc_id,
        filename=filename,
        media_type=file.content_type or "application/octet-stream",
        raw_file_uri=file_path.resolve().as_uri(),
        content_hash=doc_id,
    ))
    run = evidence_store.start_ingestion(doc_id, modality=modality)
    asyncio.create_task(_run_ingestion_job(file_path, doc_id, modality, filename, run["run_id"]))
    return UploadDocumentResponse(
        doc_id=doc_id, filename=filename, modality=modality, status="processing",
        chunk_count=0, message="Ingestion started. Poll the document status endpoint.",
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

    if doc_id in _doc_registry and _doc_registry[doc_id].status in {"processing", "done"}:
        cached = _doc_registry[doc_id]
        return UploadDocumentResponse(
            doc_id=doc_id, filename=url, modality="youtube", status=cached.status,
            chunk_count=cached.chunk_count,
            message="Already indexed." if cached.status == "done" else "Ingestion already processing.",
        )

    _doc_registry[doc_id] = DocumentStatusResponse(
        doc_id=doc_id, modality="youtube", status="processing", chunk_count=0
    )
    evidence_store.upsert_asset(AssetRecord(
        asset_id=doc_id,
        filename=url,
        media_type="video/x-youtube",
        raw_file_uri=url,
        content_hash=doc_id,
    ))

    # Pass the complete URL as a string; YouTubeParser preserves it for yt-dlp.
    run = evidence_store.start_ingestion(doc_id, modality="youtube")
    asyncio.create_task(_run_ingestion_job(url, doc_id, "youtube", url, run["run_id"]))
    return UploadDocumentResponse(
        doc_id=doc_id, filename=url, modality="youtube", status="processing",
        chunk_count=0, message="Ingestion started. Poll the document status endpoint.",
    )


@router.get("/{doc_id}/status", response_model=DocumentStatusResponse)
def document_status(doc_id: str) -> DocumentStatusResponse:
    record = _doc_registry.get(doc_id)
    if record is None:
        stored = evidence_store.get_document_status(doc_id)
        if stored is not None:
            return DocumentStatusResponse(**stored)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return record


@router.get("/{doc_id}/source")
def document_source(doc_id: str) -> FileResponse:
    """Serve an uploaded source through the API, never exposing file:// URIs."""
    directory = Path(settings.upload_dir) / doc_id
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail="Source file not found")
    files = [path for path in directory.iterdir() if path.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="Source file not found")
    source = files[0]
    return FileResponse(source, filename=source.name)


# ── Internal ingestion pipeline ───────────────────────────────────────────────

def _ingest(file_path: Path | str, doc_id: str, modality: str, filename: str) -> int:
    logger.info("Parsing '%s' (doc_id=%s, modality=%s) …", filename, doc_id[:8], modality)
    parser = get_parser(modality)
    raw_chunks = parser.parse(file_path, doc_id)
    logger.info("Parsed %d raw blocks; chunking …", len(raw_chunks))

    chunks = chunk_parsed_chunks(raw_chunks)
    if not chunks:
        logger.warning("No chunks produced for '%s'", filename)
        return 0

    for chunk in chunks:
        chunk.embedding_model = settings.local_embed_model
        chunk.source_name = Path(filename).name if modality != "youtube" else filename

    evidence_store.upsert_evidence([
        EvidenceSegment.from_chunk(chunk) for chunk in chunks
    ])
    projection_result = sync_pending_evidence_projections(store=evidence_store)
    if projection_result["failed"]:
        raise RuntimeError(f"Evidence projection failed for {projection_result['failed']} chunk(s)")
    logger.info("Stored and projected %d chunks for doc_id=%s", len(chunks), doc_id[:8])
    return len(chunks)
