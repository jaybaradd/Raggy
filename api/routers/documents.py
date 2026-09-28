"""Scoped document upload, ingestion status, retry, and source access."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from api.schemas import DocumentStatusResponse, ParseDocumentResponse, UploadDocumentResponse, YouTubeIngestRequest
from config import settings
from core.evidence.projections import sync_pending_evidence_projections
from core.ingestion.chunker import chunk_parsed_chunks
from core.ingestion.models import AssetRecord, EvidenceSegment
from core.ingestion.parser import compute_doc_id, get_parser
from core.latency import elapsed_ms, latency_span, log_latency, now_ns
from db.repository_factory import repositories

logger = logging.getLogger(__name__)
evidence_store = repositories.evidence
session_store = repositories.sessions
router = APIRouter(prefix="/documents", tags=["documents"])

_SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".jpg", ".jpeg", ".png",
    ".webp", ".gif", ".mp4", ".mov", ".avi", ".webm",
}


def _session_scope(session_id: str, scope: str) -> tuple[dict, str | None, str | None]:
    session = session_store.get_session(session_id, owner_id="default")
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    project_id = session.get("project_id")
    if scope == "project":
        if not project_id:
            raise HTTPException(status_code=422, detail="Project scope requires a project session")
        return session, None, project_id
    return session, session_id, None


def _upload_path(doc_id: str, filename: str) -> Path:
    destination = Path(settings.upload_dir) / doc_id
    destination.mkdir(parents=True, exist_ok=True)
    return destination / (Path(filename).name or "upload")


async def _run_projection_job(run_id: str, binding_id: int, chunk_count: int) -> None:
    try:
        result = await asyncio.to_thread(
            sync_pending_evidence_projections, store=evidence_store,
            binding_id=binding_id, limit=max(chunk_count, 1), run_id=run_id,
        )
        if result["failed"]:
            raise RuntimeError(f"Evidence projection failed for {result['failed']} chunk(s)")
    except Exception as exc:
        logger.exception("Projection failed for binding %s", binding_id)
        evidence_store.complete_ingestion(run_id, chunk_count=0, error=str(exc))
    else:
        evidence_store.complete_ingestion(run_id, chunk_count=chunk_count)


async def _run_ingestion_job(file_path: Path | str, doc_id: str, binding_id: int,
                             modality: str, filename: str, run_id: str) -> None:
    try:
        chunk_count = await asyncio.to_thread(
            _ingest, file_path, doc_id, binding_id, modality, filename, run_id,
        )
    except Exception as exc:
        logger.exception("Ingestion failed for '%s'", filename)
        evidence_store.complete_ingestion(run_id, chunk_count=0, error=str(exc))
    else:
        evidence_store.complete_ingestion(run_id, chunk_count=chunk_count)


def _start(asset: AssetRecord, *, scope: str, session_id: str, project_id: str | None,
           modality: str, source: Path | str) -> UploadDocumentResponse:
    binding_id = evidence_store.upsert_asset(
        asset, owner_id="default", scope=scope,
        session_id=session_id if scope == "session" else None,
        project_id=project_id if scope == "project" else None,
    )
    current = evidence_store.get_document_status(
        binding_id, owner_id="default", session_id=session_id, project_id=project_id,
    )
    if current and current["status"] in {"processing", "done"}:
        return UploadDocumentResponse(**{
            **current,
            "filename": asset.filename,
            "message": "Already indexed." if current["status"] == "done" else "Ingestion already processing.",
        })
    run = evidence_store.start_ingestion(asset.asset_id, binding_id=binding_id, modality=modality)
    existing = evidence_store.enqueue_asset_projections(binding_id)
    if existing:
        asyncio.create_task(_run_projection_job(run["run_id"], binding_id, existing))
    else:
        asyncio.create_task(_run_ingestion_job(source, asset.asset_id, binding_id, modality, asset.filename, run["run_id"]))
    return UploadDocumentResponse(
        doc_id=asset.asset_id, binding_id=binding_id, run_id=run["run_id"], filename=asset.filename,
        modality=modality, status="processing", chunk_count=0,
        message="Ingestion started. Poll the document status endpoint.",
    )


@router.post("", response_model=UploadDocumentResponse, status_code=202)
async def upload_document(file: UploadFile, session_id: str = Query(...),
                          scope: Literal["session", "project"] = Query(...)) -> UploadDocumentResponse:
    _, scoped_session, project_id = _session_scope(session_id, scope)
    filename = file.filename or "unknown"
    extension = Path(filename).suffix.lower()
    if extension not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{extension}'")
    content = await file.read()
    doc_id = compute_doc_id(content)
    path = _upload_path(doc_id, filename)
    path.write_bytes(content)
    return _start(
        AssetRecord(asset_id=doc_id, filename=filename,
                    media_type=file.content_type or "application/octet-stream",
                    raw_file_uri=path.resolve().as_uri(), content_hash=doc_id),
        scope=scope, session_id=session_id, project_id=project_id,
        modality=extension.lstrip("."), source=path,
    )


@router.post("/parse", response_model=ParseDocumentResponse)
async def parse_document(file: UploadFile) -> ParseDocumentResponse:
    total_started = now_ns()
    filename = file.filename or "unknown"
    extension = Path(filename).suffix.lower()
    if extension not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{extension}'")
    import tempfile
    content = await file.read()
    doc_id = compute_doc_id(content)
    with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as temporary:
        temporary.write(content)
        path = Path(temporary.name)
    try:
        common = {"doc_id": doc_id, "modality": extension.lstrip("."), "mode": "inline"}
        with latency_span("ingestion", "parser_setup", **common):
            parser = get_parser(extension.lstrip("."))
        with latency_span("ingestion", "parse", **common) as fields:
            chunks = parser.parse(path, doc_id)
            fields["raw_block_count"] = len(chunks)
    except Exception as exc:
        log_latency(
            "ingestion", "total", elapsed_ms(total_started), outcome="error",
            doc_id=doc_id, modality=extension.lstrip("."), mode="inline",
            error_type=type(exc).__name__,
        )
        logger.exception("Inline parsing failed for '%s'", filename)
        raise HTTPException(status_code=422, detail=f"Document parsing failed: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)
    parsed_text = "\n\n".join(c.content for c in chunks if c.content.strip())
    log_latency(
        "ingestion", "total", elapsed_ms(total_started), doc_id=doc_id,
        modality=extension.lstrip("."), mode="inline", raw_block_count=len(chunks),
        output_chars=len(parsed_text),
    )
    return ParseDocumentResponse(filename=filename, modality=extension.lstrip("."), parsed_text=parsed_text)


@router.post("/youtube", response_model=UploadDocumentResponse, status_code=202)
async def ingest_youtube(body: YouTubeIngestRequest) -> UploadDocumentResponse:
    _, _, project_id = _session_scope(body.session_id, body.scope)
    doc_id = compute_doc_id(body.url.encode())
    return _start(
        AssetRecord(asset_id=doc_id, filename=body.url, media_type="video/x-youtube",
                    raw_file_uri=body.url, content_hash=doc_id),
        scope=body.scope, session_id=body.session_id, project_id=project_id,
        modality="youtube", source=body.url,
    )


@router.get("/bindings/{binding_id}/status", response_model=DocumentStatusResponse)
def document_status(binding_id: int, session_id: str = Query(...)) -> DocumentStatusResponse:
    try:
        session = session_store.get_session(session_id, owner_id="default")
    except Exception as exc:
        logger.exception("Document status database lookup failed")
        raise HTTPException(status_code=503, detail="Document status is temporarily unavailable") from exc
    if not session:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        record = evidence_store.get_document_status(
            binding_id, owner_id="default", session_id=session_id, project_id=session.get("project_id"),
        )
    except Exception as exc:
        logger.exception("Document status evidence lookup failed")
        raise HTTPException(status_code=503, detail="Document status is temporarily unavailable") from exc
    if not record:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentStatusResponse(**record)


@router.post("/bindings/{binding_id}/retry", response_model=UploadDocumentResponse, status_code=202)
async def retry_document_ingestion(binding_id: int, session_id: str = Query(...)) -> UploadDocumentResponse:
    session = session_store.get_session(session_id, owner_id="default")
    if not session:
        raise HTTPException(status_code=404, detail="Document not found")
    context = {"owner_id": "default", "session_id": session_id, "project_id": session.get("project_id")}
    current = evidence_store.get_document_status(binding_id, **context)
    asset = evidence_store.get_asset(binding_id, **context)
    if not current or not asset:
        raise HTTPException(status_code=404, detail="Document not found")
    if current["status"] in {"processing", "done"}:
        raise HTTPException(status_code=409, detail=f"Document is already {current['status']}")
    source: Path | str = asset.raw_file_uri
    if current["modality"] != "youtube":
        parsed = urlparse(asset.raw_file_uri)
        path = Path(unquote(parsed.path)).resolve()
        try:
            path.relative_to(Path(settings.upload_dir).resolve())
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Stored source is outside the upload directory") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Stored source file is unavailable")
        source = path
    run = evidence_store.start_ingestion(asset.asset_id, binding_id=binding_id, modality=current["modality"])
    count = evidence_store.enqueue_asset_projections(binding_id)
    if count:
        asyncio.create_task(_run_projection_job(run["run_id"], binding_id, count))
    else:
        asyncio.create_task(_run_ingestion_job(source, asset.asset_id, binding_id, current["modality"], asset.filename, run["run_id"]))
    return UploadDocumentResponse(doc_id=asset.asset_id, binding_id=binding_id, run_id=run["run_id"],
                                  filename=asset.filename, modality=current["modality"], status="processing")


@router.get("/bindings/{binding_id}/source")
def document_source(binding_id: int, session_id: str = Query(...)) -> FileResponse:
    session = session_store.get_session(session_id, owner_id="default")
    if not session:
        raise HTTPException(status_code=404, detail="Source file not found")
    asset = evidence_store.get_asset(binding_id, owner_id="default", session_id=session_id,
                                     project_id=session.get("project_id"))
    if not asset:
        raise HTTPException(status_code=404, detail="Source file not found")
    parsed = urlparse(asset.raw_file_uri)
    if parsed.scheme != "file":
        raise HTTPException(status_code=404, detail="Source file is not stored locally")
    source = Path(unquote(parsed.path)).resolve()
    try:
        source.relative_to(Path(settings.upload_dir).resolve())
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Source file not found") from error
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Source file not found")
    return FileResponse(source, filename=asset.filename)


def _ingest(file_path: Path | str, doc_id: str, binding_id: int, modality: str,
            filename: str, run_id: str | None = None) -> int:
    total_started = now_ns()
    common = {"run_id": run_id, "binding_id": binding_id, "doc_id": doc_id, "modality": modality}
    try:
        with latency_span("ingestion", "parser_setup", **common):
            parser = get_parser(modality)
        with latency_span("ingestion", "parse", **common) as fields:
            parsed = parser.parse(file_path, doc_id)
            fields["raw_block_count"] = len(parsed)
        logger.info("Parsed '%s' into %d raw blocks", filename, len(parsed))
        with latency_span("ingestion", "chunk", **common) as fields:
            chunks = chunk_parsed_chunks(parsed)
            for chunk in chunks:
                chunk.embedding_model = settings.local_embed_model
                chunk.source_name = filename if modality == "youtube" else Path(filename).name
            fields["raw_block_count"] = len(parsed)
            fields["chunk_count"] = len(chunks)
        with latency_span("ingestion", "evidence_persist", **common, chunk_count=len(chunks)):
            evidence_store.upsert_evidence(
                [EvidenceSegment.from_chunk(chunk) for chunk in chunks], binding_id=binding_id,
            )
        with latency_span("ingestion", "projection", **common, chunk_count=len(chunks)) as fields:
            result = sync_pending_evidence_projections(
                store=evidence_store, binding_id=binding_id, limit=max(len(chunks), 1), run_id=run_id,
            )
            fields.update(result)
        if result["failed"]:
            raise RuntimeError(f"Evidence projection failed for {result['failed']} chunk(s)")
    except Exception as exc:
        log_latency(
            "ingestion", "total", elapsed_ms(total_started), outcome="error",
            error_type=type(exc).__name__, **common,
        )
        raise
    log_latency("ingestion", "total", elapsed_ms(total_started), chunk_count=len(chunks), **common)
    logger.info("Ingested '%s' into %d chunks", filename, len(chunks))
    return len(chunks)
