"""
main.py — FastAPI application entry point.

Wires together all routers, configures structured JSON logging, and exposes
a /health endpoint for basic liveness checks.

Run with:
    uvicorn main:app --port 8000

The local Qdrant backend uses a filesystem lock. Do not run this entrypoint
with Uvicorn's reload mode: the reload supervisor and worker are separate
processes and would both try to open the same local Qdrant directory.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routers import documents, memories, messages, projects, sessions
from api.schemas import HealthResponse
from core.evidence.projections import sync_pending_evidence_projections
from core.memory.expiry import expiry_sweep_loop, run_expiry_sweep
from core.memory.projections import sync_pending_projections
from core.storage.qdrant_store import qdrant_store
from db.repository_factory import repositories

memory_store = repositories.memories
session_store = repositories.sessions
evidence_store = repositories.evidence

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Application ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    result = memory_store.backfill_project_ids(session_store.project_name_mapping())
    if result["updated"] or result["unmatched"]:
        logger.info("Project memory backfill: %s", result)
    # Bounded repair closes the normal migration/write path without making a
    # full rebuild part of application startup.
    try:
        projection_result = await asyncio.to_thread(sync_pending_projections, store=memory_store, limit=100)
        if projection_result["completed"] or projection_result["failed"]:
            logger.info("Startup projection sync: %s", projection_result)
    except Exception:
        logger.exception("Startup projection sync failed")
    try:
        evidence_projection_result = await asyncio.to_thread(
            sync_pending_evidence_projections, store=evidence_store, limit=100
        )
        if evidence_projection_result["completed"] or evidence_projection_result["failed"]:
            logger.info("Startup evidence projection sync: %s", evidence_projection_result)
    except Exception:
        logger.exception("Startup evidence projection sync failed")
    try:
        await run_expiry_sweep()
    except Exception:
        logger.exception("Startup expiry sweep failed")
    expiry_task = asyncio.create_task(expiry_sweep_loop(), name="memory-expiry-sweep")
    try:
        yield
    finally:
        expiry_task.cancel()
        with suppress(asyncio.CancelledError):
            await expiry_task


app = FastAPI(
    title="Raggy",
    description="Multimodal RAG Chatbot — Phase 1 (Hybrid Retrieval + All Modalities)",
    version="0.2.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

# Allow the frontend (served from a different port in dev) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(sessions.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(messages.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(memories.router, prefix="/api")


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness check — returns Qdrant collection stats."""
    return HealthResponse(
        status="ok",
        qdrant=qdrant_store.collection_info(),
    )


# ── Static frontend ───────────────────────────────────────────────────────────
# Serve uploaded raw files (used by raw_file_uri references)
from pathlib import Path as _Path
from config import settings as _settings
_Path(_settings.upload_dir).mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_settings.upload_dir), name="uploads")

# Frontend — must be mounted LAST so /api/* and /uploads/* are matched first
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Keep one process when using file-backed Qdrant. Reload mode creates a
    # second interpreter on macOS and contends for qdrant_data/.lock.
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
