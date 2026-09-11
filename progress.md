# Raggy — Progress Log

---

### ✅ [2026-09-11] Event deduplication and conflict review

- Added repository-enforced deterministic identity keys for event memories, scoped by owner, session/project, event type, entities, location, and temporal marker.
- Repeated event captures now retain one authoritative memory and append a `duplicate_detected` audit event instead of creating duplicate shipments.
- Material event updates create a reviewable candidate plus an auditable conflict record; they never overwrite the existing event automatically.
- Added conflict inspection and resolution APIs. Resolution can retain the existing event, supersede it, or expire it while activating the reviewed incoming event and synchronizing projections.
- Added deterministic coverage for duplicate events, temporal conflicts, conflict resolution, project isolation, and expiration followed by a new event.

### 📝 [2026-09-10] Hybrid memory architecture decision recorded

- Documented the SQLite/Postgres + Qdrant + graph strategy, its advantages, and mitigations for graph noise, hallucinated relationships, duplication, contradictions, stale data, privacy leakage, projection inconsistency, and operational complexity in `Roadmap.md`.
- Established that relational memory records remain authoritative while vector and graph stores remain scoped, specialized, rebuildable projections.

### 🚧 [2026-09-10] Phase 2 graph-foundation implementation started

- Added graph-compatible SQLite node, alias, and edge projections for active, confirmed memories.
- Added conservative exact canonical/alias resolution, open normalized predicates, and broad relation-family classification while preserving the original atom semantics.
- Added provenance kind, evidence references, confidence, scope, validity, and qualifiers to projected graph edges.
- Added a durable SQLite projection outbox for Qdrant and graph synchronization, plus a rebuild script for derived projections.
- Added graph inspection endpoints and deterministic tests for alias resolution, edge provenance, lifecycle deactivation, and outbox completion.
- Deferred graph traversal/fusion, automatic contradiction decisions, durable sessions, and the Postgres migration to their planned follow-on slices.

### ✅ [2026-09-10] Automatic project-event capture

- Added `EventMemory` for user-stated operational facts such as shipments, deliveries, deadlines, meetings, and incidents.
- Added deterministic project-capture policy: high-confidence events in a named project become active project memories automatically; generic knowledge, entities, solutions, and ambiguous claims remain candidates.
- Added conservative event validity windows using the original user wording, with a default review window when no relative time is available.
- Active project events now enter the existing Qdrant and graph projections; their graph edge retains source-turn provenance and temporal qualifiers.

### ✅ [2026-09-11] Durable conversations

- Replaced the in-memory session store with a SQLite-backed session/message repository, using a Postgres-compatible boundary and schema shape.
- Added durable session ownership, project membership, ordered message history, trace IDs, and attachment metadata.
- Added `GET /api/sessions/{session_id}/messages`; opening a chat in the frontend now restores its stored messages.
- Added persistence and owner-isolation tests, including recreation of the repository from disk to simulate a process restart.

### ✅ [2026-09-10] Knowledge-base retrieval control

- Added a per-message `Search knowledge base` toggle, disabled by default, so general and memory-oriented questions do not receive unrelated document context.
- Added a matching API request flag; disabled retrieval bypasses document Qdrant search entirely while keeping scoped memory retrieval available.
- Saving an attachment to the knowledge base enables the toggle for that message, preserving the expected indexed-document workflow.
- Made local-Qdrant memory deletion idempotent so candidate-memory projection cleanup no longer fails when a point was never indexed.
- Tightened post-chat extraction: assistant-only document summaries and citations are no longer eligible to become personal memory candidates.

### ✅ [2026-09-10] Project membership clarity

- Added a session project-update endpoint and made the sidebar's current-project control update the active chat explicitly.
- New chats inherit the visible current project; the sidebar now groups chats under `Project · <name>` headings rather than only appending a project label to each title.
- Passed session project context into post-turn candidate extraction so candidate provenance no longer loses the project that produced it.

---

### ✅ [2026-09-08] Attachment ingestion choice

- Added a `Save to knowledge base` option to the file attachment preview.
- Attachments remain available for immediate inline Q&A; when selected, the same file is also sent through asynchronous chunking, embedding, evidence persistence, and Qdrant indexing for future retrieval.
- Kept the two paths explicit so one-off attachments do not become durable knowledge unintentionally.
- Saved attachments now use their indexed chunks for the current question, avoiding a second concurrent Docling parse of the same file.

### ✅ [2026-09-08] Local Qdrant startup lock fix

- Disabled Uvicorn reload in the `python main.py` entrypoint because its macOS supervisor/worker processes contend for the file-backed Qdrant lock.
- Updated startup documentation to use a single-process server for `QDRANT_URL=./qdrant_data`.

### ✅ [2026-09-09] Phase 2E minimal memory acceptance slice

- Added `evals/phase2e_conversations.json` with preference, session isolation, solution, contradiction, and provenance/expiration conversation scenarios.
- Added deterministic lifecycle tests in `tests/test_phase2e_memory.py`; they run without Gemini, embeddings, or Qdrant.
- Added opt-in real-API test `tests/test_phase2e_api_e2e.py` covering extraction, confirmation, project promotion, cross-session retrieval, SSE memory metadata, access events, and project isolation.
- Fixed SQLite memory upserts so promotion correctly persists owner, scope, session, and project fields.
- All four deterministic Phase 2E lifecycle tests pass; the real-API test runs when `RUN_PHASE2E_E2E=1` against a started server.
- Verified the real API test against the running server: 1 end-to-end test passed in 12.037 seconds, covering extraction, confirmation, project promotion, cross-session retrieval, SSE memory metadata, access events, and project isolation.

### ✅ Phase 2 exit criteria reached

The narrow Phase 2 vertical slice is operational: a user preference becomes a candidate, is explicitly confirmed and promoted, is retrieved in a permitted later session, carries provenance/trace metadata, and is isolated from another project. Broader atom coverage and automated contradiction resolution remain future extensions rather than blockers for this phase.

---

## Phase 2 — Knowledge atoms and durable memory

### ✅ [2026-09-08] Phase 2A memory foundation

- Added typed `KnowledgeAtom`, `PreferenceMemory`, `SolutionMemory`, and `EntityMemory` payload models with a shared scoped/lifecycle-aware `MemoryRecord` envelope.
- Added local SQLite `MemoryStore` with durable records, scope/status indexes, safe upsert, lifecycle transitions, and append-only audit events.
- Added `MEMORY_DB_PATH` configuration; the repository remains replaceable by Postgres in a later Phase 2 slice.

### ✅ [2026-09-08] Phase 2B structured extraction worker

- Added a provider-neutral structured JSON extraction contract with typed payload validation and normalization.
- Added an asynchronous post-turn extraction job; SSE completion is not blocked by memory extraction.
- Added idempotency claims keyed by `source_turn_id` and extraction version, including failed/completed job status.
- Persisted candidate memories with session scope, confidence, source turn, extractor version, and retrieved evidence references.
- Added stable message IDs so completed assistant turns can be used as extraction provenance.
- Memory retrieval remains planned for the next Phase 2 slice.

### ✅ [2026-09-08] Phase 2C review and promotion APIs

- Added candidate and filtered memory listing endpoints with owner isolation via `X-Owner-ID`.
- Added confirm, reject, edit, expire, and explicit user/project promotion operations.
- Added append-only audit details for review, edit, promotion, and supersession actions.
- Added contradiction relationship records linking a superseded memory to its replacement; old records remain auditable and are excluded from active retrieval.
- Expiration sets `status=expired` and `valid_to` rather than deleting source history.

### ✅ [2026-09-08] Phase 2D memory retrieval projection

- Added deterministic typed-memory text projection and a dedicated `kb_memory_records` Qdrant collection.
- Active, confirmed memories are indexed; candidates, rejected, expired, and superseded records are removed from the projection.
- Added owner, scope, session/project, status, confirmation, and validity-window filters before semantic memory search.
- Added separate memory context and `memories` SSE metadata so memory citations remain distinct from document sources.
- Added frontend memory cards showing memory kind, scope, confidence, and memory ID separately from source citations.
- Added turn-level trace IDs, durable retrieval/injection access events, stable `M1`/`M2` prompt labels, and trace-aware memory SSE metadata. Model-use attribution remains intentionally deferred.
- Fixed Gemini Developer API incompatibility with Pydantic `additionalProperties` schemas by using JSON MIME mode plus post-response validation; failed extraction jobs are now retryable.
- Normalized common preference field variants into canonical `preferred_behavior` and isolated candidate validation so malformed document/entity candidates no longer discard valid preferences from the same turn.
- Normalized nested preference payloads and human-readable values such as string conditions, `high` strength, and explicit consent.
- Strengthened the extraction prompt with generic per-kind output examples, exact field contracts, atomic knowledge rules, evidence-ID constraints, and an explicit empty-result shape.

### ✅ [2026-09-08] Session project context

- Added optional `project_scope` to session creation and session responses.
- Passed session project context into memory retrieval so project-scoped memories are isolated to matching sessions.
- Added conservative retrieval limits; graph storage and broad document-atom indexing remain deferred.

## Phase 1 — Evidence contracts and asynchronous ingestion

### ✅ [2026-09-07] Phase 1 foundation slice

- Added forward-compatible `AssetRecord` and `EvidenceSegment` models in `core/ingestion/models.py`.
- Added stable evidence IDs, representation metadata, and per-instance timestamps to `ParsedChunk`.
- Preserved evidence/provenance fields in Qdrant payloads (timestamp, cell range, bounding box, and representation).
- Changed document and YouTube ingestion to return immediately with `processing` status and complete in a background thread; the status endpoint now reports terminal state.
- Fixed YouTube URL handling so the complete URL reaches yt-dlp instead of being reduced to a `Path.name`.
- Sanitized uploaded filenames to prevent path traversal.
- Updated the frontend to poll asynchronous ingestion status and display completion/errors.

### ✅ [2026-09-07] Durable evidence metadata and modality routing

- Added a local SQLite `EvidenceStore` for restart-safe asset and evidence metadata; its interface is intentionally replaceable by Postgres in Phase 2.
- Persisted parsed evidence segments with modality, representation, source locator, parser provenance, and media URI.
- Added lazy modality-specific Qdrant collections (`kb_table_chunks`, `kb_image_chunks`, and `kb_video_segments`) while retaining `kb_text_chunks` for compatibility.
- Added cross-collection retrieval with initial modality weights and preserved collection/evidence identifiers in results.
- Deferred contextual retrieval prefixes as a future adaptive optimization; Phase 1 will proceed using structural metadata, evidence locators, and retrieval evaluation first.
- Scoped Phase 1 image work to separate OCR and caption evidence. Visual embeddings, image-region search, video keyframes, and frame/transcript fusion are deferred to future scope.

### ✅ [2026-09-07] Image evidence separation and citation metadata

- Updated `ImageParser` to emit distinct caption and OCR evidence units with separate evidence IDs and OCR bounding-box provenance.
- Added source filenames and representation metadata to indexed chunks.
- Added a structured `sources` SSE event before assistant tokens, carrying evidence IDs, modality, representation, and page/time/cell/bbox locators.
- Added frontend source cards for assistant responses, including page, spreadsheet range, image-region, timestamp, and external URL details where available.
- Added `/api/documents/{doc_id}/source` so local citations resolve through the API instead of exposing `file://` paths.

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
