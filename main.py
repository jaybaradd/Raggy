"""
main.py — FastAPI application entry point.

Wires together all routers, configures structured JSON logging, and exposes
a /health endpoint for basic liveness checks.

Run with:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routers import documents, memories, messages, sessions
from api.schemas import HealthResponse
from core.storage.qdrant_store import qdrant_store

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Application ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Raggy",
    description="Multimodal RAG Chatbot — Phase 1 (Hybrid Retrieval + All Modalities)",
    version="0.2.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
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
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
