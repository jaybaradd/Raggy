# Raggy — Progress Log

---

## Phase 0 — Foundations

### ✅ [2026-09-04] Initial scaffold & local embedding cleanup

**Features shipped & modifications:**
- `config.py` — pydantic-settings singleton; all env config in one place
- `core/llm/base.py` — `LLMProvider` abstract interface (Phase 3 seam)
- `core/llm/gemini.py` — `GeminiProvider` implementation via `google-genai` async streaming
- `core/ingestion/parser.py` — `Parser` ABC + `PdfParser` (Docling), `ParsedChunk` canonical schema
- `core/ingestion/chunker.py` — token-based recursive chunker (paragraph → sentence → word), tiktoken BPE, configurable overlap
- `core/embeddings.py` — Local sentence-transformers embedding backend (`all-MiniLM-L6-v2`)
- `core/storage/qdrant_store.py` — Qdrant wrapper, in-memory + server mode, upsert + dense search (dynamically configured vector dimensions)
- `core/retrieval/engine.py` — Phase 0 RAG pipeline: embed query → dense search → format context → build prompt
- `db/session_store.py` — in-memory session + message history store (Phase 2 will replace with Postgres)
- `api/schemas.py` — Pydantic request/response models for all endpoints
- `api/routers/sessions.py` — `POST/GET /api/sessions`, `GET /api/sessions/{id}`
- `api/routers/messages.py` — `POST /api/sessions/{id}/messages` (SSE streaming chat)
- `api/routers/documents.py` — `POST /api/documents` (upload + synchronous ingestion), `GET /api/documents/{id}/status`
- `main.py` — FastAPI entry point, CORS, static frontend, `/api/health`
- `frontend/` — dark-mode single-page UI (session list, SSE streaming chat, PDF upload, live cursor)
- `requirements.txt` — all deps pinned
- `.env.example` — documented config template

**Refinements & Decisions:**
- Removed Gemini embeddings backend entirely per user constraint (exclusively using local `sentence-transformers`).
- `docling==2.7.0` verified working alongside pinned `torch==2.2.2`, `numpy==1.26.4`, `transformers==4.40.2`.

---

## Bugs / Errors

*None encountered so far.*
