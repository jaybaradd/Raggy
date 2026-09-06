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
- Initial Docling & EasyOCR model weights (~500MB) are cached locally in `~/.cache/` on first run; subsequent runs load models instantly from disk.
- Added local disk persistence option (`QDRANT_URL=./qdrant_data`) so vector indices survive server restarts without re-uploading PDFs.
- Enhanced `PdfParser` in `core/ingestion/parser.py` with **Section-Block Aggregation** (`[Section: <Header>]`), grouping lines under headings into full ~400-token context blocks and eliminating over-granular line-by-line chunking.
- Increased default `RETRIEVAL_TOP_K=10` and generalized system prompt in `core/retrieval/engine.py` to ensure broad multi-item queries retrieve all relevant document sections in a single turn.
- Upgraded embedding backend to `cnmoro/snowflake-arctic-embed-m-v2.0-cpu` with Matryoshka dimension truncation (`256` dimensions), replacing `all-MiniLM-L6-v2`.

---

## Phase 1A — Hybrid Retrieval + Reranking

### ✅ [2026-09-05] BM25 + Dense Hybrid Search + Cross-Encoder Reranking

**Features shipped:**
- `core/storage/qdrant_store.py` — Upgraded to dual-vector collection (dense `snowflake-arctic-256d` + BM25 sparse via `fastembed`). `search()` runs two parallel `qdrant_client.search()` calls and fuses results via manual RRF (k=60), compatible with `qdrant-client==1.9.2`.
- `core/retrieval/reranker.py` — New `CrossEncoderReranker` wrapping `cross-encoder/ms-marco-MiniLM-L-6-v2` (CPU, via `sentence-transformers`). Takes ~20 hybrid candidates → reranked top-6.
- `core/retrieval/engine.py` — `retrieve()` pipeline: embed query → hybrid RRF search (20 candidates) → cross-encoder rerank (top 6) → format context.
- `config.py` — Added `HYBRID_CANDIDATES=20`, `RERANKER_MODEL`, `RERANKER_TOP_N=6`.
- `requirements.txt` — Added `fastembed==0.3.6`.

**Decisions:**
- Used manual RRF fusion instead of Qdrant's native `query_points` prefetch — `query_points` requires `qdrant-client>=1.10.0` (pinned to `1.9.2` due to hardware constraints).
- Cross-encoder reranker is self-hosted (no API key), runs on CPU in ~50–200 ms for 20 candidates.

---

## Bugs / Errors

- **`docling-core` version mismatch**: `docling==2.7.0` failed with `cannot import name 'BoundingBox'` due to `docling-core` auto-upgrading to v2.84+. **Fix**: Pinned `docling-core==2.8.0` in `requirements.txt`.
- **Retrieval Disambiguation & Over-Granular Chunking**: Asking *"Which college did I go to?"* retrieved `"Collegestreet.tech"` experience item due to isolated single-line chunks. **Fix**: Implemented Section-Block Aggregation in `PdfParser` to keep section headings and their child entries together in rich context blocks.
- **Sub-Header Dropping & Partial Item Retrieval**: Multiple consecutive sub-headers under `EXPERIENCE` (`InSync Analytics`, `DataEngite`, `Collegestreet.tech`) were overwriting `current_section` and dropping header text. **Fix**: Implemented hierarchical section tracking (`top_section > sub_section`) in `PdfParser`, ensuring all 3 internship entries retain `EXPERIENCE` section context and body text.

---

## Phase 1B — Multimodal Ingestion

### ✅ [2026-09-06] All Modalities (Image, Video, YouTube, Spreadsheets) & Segment Merging

**Features shipped & modifications:**
- `core/ingestion/parser.py`:
  - **`ImageParser` (BLIP + RapidOCR)**: Dual-pass image processing — BLIP captioner for visual scene descriptions combined with RapidOCR (PP-OCRv4 via ONNX) for verbatim text extraction from cards, screenshots, slides, and receipts.
  - **`XlsxParser` (Pandas)**: Multi-sheet Excel (`.xlsx`) and CSV parsing with header-preserving row chunking (`table_chunk_rows=50`). Repeats the header row on every chunk so column-value context is maintained across Qdrant vectors and LLM context.
  - **`YouTubeParser` & `VideoParser`**: Captions-first strategy (yt-dlp VTT download in ~5 seconds) with `faster-whisper` fallback. Added `_merge_segments()` helper to aggregate raw 1–3 word VTT cues / Whisper outputs into **~120-word / 60-second contextual windows**, eliminating micro-chunks.
  - **CPU Whisper Optimization**: Set `beam_size=1` (greedy decoding), `vad_filter=True` (skips silence), and `condition_on_previous_text=False` for 4–5x CPU speedup.
- `core/retrieval/engine.py`:
  - Added `_clean_query()` to strip raw URLs (e.g. YouTube links) from user queries before dense embedding, BM25, and cross-encoder reranking.
- `api/routers/documents.py`:
  - Offloaded sync ingestion work to `asyncio.to_thread` / thread pool executor so heavy YouTube/video/image parsing never blocks the FastAPI event loop.
- `frontend/app.js`:
  - Added YouTube URL auto-detection banner and automatic URL stripping from chat input box upon ingestion.
- `requirements.txt`:
  - Added `rapidocr-onnxruntime>=1.3.0` and `pytesseract>=0.3.10`.

**Decisions & Fixes:**
- **BLIP-only limitation**: Initial image ingestion missed business card text because BLIP only generated visual captions ("picture of a man"). Resolved by pairing BLIP with RapidOCR.
- **YouTube Ingestion Latency**: Whisper inference took ~1 hour for 5-min video. Resolved by using YouTube auto-captions first (seconds) and optimizing Whisper CPU inference.
- **YouTube Micro-Chunking & Retrieval Failure**: VTT cues were stored as isolated 1-3 word Qdrant points (`>> Yes, sir.`), yielding poor rerank scores (-10.5). Resolved by merging adjacent segments into 60s/120-word windows.
