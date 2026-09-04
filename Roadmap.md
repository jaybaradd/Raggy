# Multimodal RAG Chatbot — Product & Technical Roadmap

This document is the execution plan: **Part 1** is the phased build order (what ships when, and what "done" means for each phase). **Part 2** is the detailed spec for every layer — ingestion, data, API, retrieval, web search, deep research, frontend — that Part 1's phases draw from.

---

## Part 1 — Phased Roadmap

Six phases, roughly 19 weeks for a small team. Each phase is meant to be shippable and testable on its own before the next one starts — nothing here assumes you build all seven layers in parallel.

| Phase | Weeks | Goal | Ships |
|---|---|---|---|
| **0 — Foundations** | 1–2 | Prove the core loop end to end, one modality only | FastAPI skeleton, PDF-only ingestion, dense-only Qdrant, Gemini behind an interface, SSE streaming chat, minimal frontend |
| **1 — Hybrid Retrieval + All Modalities** | 3–5 | Make retrieval good, support every file type | BM25 + rerank, contextual retrieval, all 6 modalities, parser abstraction, canonical schema, object storage |
| **2 — Memory Layer** | 6–8 | Cross-session recall | FalkorDB + Graphiti, background extraction job, memory enums, project scoping |
| **3 — Multi-Provider + RLM** | 9–11 | Model-agnostic chat, handle context overflow | LiteLLM gateway, model routing, query decomposition, RLM synthesis path, prompt caching |
| **4 — Web Search + Deep Research** | 12–15 | Ground answers in live web content, long-form research | Tavily + Brave, domain trust tiering, orchestrator-worker deep research, citation-check subagent |
| **5 — Workflows + Observability + Polish** | 16–19 | Reminders/reports/emails, pause/resume, full tracing | Scheduler, SSE pause/resume, OTel + Langfuse, observability panel, load testing |

### Phase 0 — Foundations

**Build:**
- FastAPI app with `/sessions` and `/sessions/{id}/messages` (SSE streaming)
- Docling for PDF parsing only (defer every other modality)
- One Qdrant collection, dense vectors only, recursive token-based chunking (512 tokens / ~50 overlap), no reranking yet
- `LLMProvider` abstract interface with a single `GeminiProvider` implementation — **build the interface even though you only have one backend**, this is the seam Phase 3 plugs into
- Naive RAG: embed query → top-k dense search → stuff into prompt → stream response
- No memory, no web search, no deep research
- Structured JSON logging only (real observability comes in Phase 5)
- Frontend: one chat window, upload button, streamed response

**Exit criteria:** upload a PDF, ask a grounded question about it, get a streamed answer, in one session. Nothing persists across sessions yet — that's intentional, it's Phase 2's job.

### Phase 1 — Hybrid Retrieval + All Modalities

**Build:**
- Add BM25 sparse vectors to Qdrant (native `bm25_sparse_vector` support), fuse with dense via Qdrant's built-in RRF `prefetch`
- Add contextual retrieval: before embedding, prepend an LLM-generated 1–2 sentence description of what the chunk is and where it sits in the source document
- Add a reranking step (cross-encoder — self-hosted BGE reranker or Cohere Rerank) narrowing ~20–30 candidates to top 5–8
- Build the `Parser` abstraction (see Part 2 §1) and implement all six modality parsers
- Finalize the canonical `ParsedChunk` schema every parser writes into
- Wire raw-file object storage (S3/GCS/local disk) alongside parsed content, keyed by `doc_id`
- Split Qdrant into per-modality collections (see Part 2 §2) rather than one collection for everything

**Exit criteria:** all six modalities ingest successfully; a side-by-side eval shows hybrid+rerank materially outperforms Phase 0's dense-only retrieval on a small labeled test set (even 20 hand-written Q&A pairs is enough to catch regressions).

### Phase 2 — Memory Layer

**Build:**
- Deploy FalkorDB + Graphiti
- Background extraction job (post-session, or on a rolling schedule): reads new messages, extracts facts/solutions/preferences/entities per the enums in Part 2 §2
- `MemoryItem` schema and both `ResearchMemoryType`/`SessionMemoryType` enums finalized and enforced
- Project/session scoping via Graphiti `group_id` + a `project_scope` payload filter in Qdrant
- An explicit "search memory" tool the agent calls on demand, separate from always-inject — mirrors how Claude's own memory system separates topic-based facts from an explicit "search past chats" retrieval tool

**Exit criteria:** a fact/preference/solution learned in session A is correctly recalled in session B; a contradicted fact is invalidated (not deleted) and the newer version is what gets served.

### Phase 3 — Multi-Provider + Query Decomposition + RLM

**Build:**
- Stand up LiteLLM as a self-hosted gateway behind your existing `LLMProvider` interface; add OpenAI, Anthropic, Groq
- Model routing policy: cheap/fast tier for extraction, classification, chunk-context generation; frontier tier for synthesis and final generation
- Query decomposition step in the retrieval pipeline (skip it for queries that are already atomic — don't pay the extra call every time)
- RLM integration point: triggered when aggregated retrieved+memory context crosses a token-budget threshold (e.g. 60–70% of the active model's context window), instead of silently truncating
- Prompt caching wired per-provider (static system prompt/tools first, variable content last, so cache hit rate is actually high)

**Exit criteria:** switching the configured provider doesn't change any application code; a query needing synthesis across 15+ retrieved items returns a coherent answer instead of a truncated one.

### Phase 4 — Web Search + Deep Research

**Build:**
- Tavily as default web search tool (LLM-ready output, native domain filtering); Brave Search API as fallback/secondary for cost control and index independence
- Domain trust-tiering layer (Part 2 §5) applied on top of provider results
- Deep research orchestrator: planner → parallel searcher subagents → full-text fetch → RLM synthesis over the aggregated corpus → independent citation-check subagent → report assembly
- Frontend: retrieval-mode toggle (KB / Web / Deep Research, multi-select)

**Exit criteria:** deep research mode produces a cited, multi-source report for an open-ended query, and every citation is checked against the actual fetched source text, not the synthesizer's recollection of it.

### Phase 5 — Workflows + Observability + Polish

**Build:**
- Scheduler for durable/delayed jobs (reminders, periodic reports, email digests) — Temporal if you want durability guarantees across restarts, APScheduler/a cron table if you want something simpler to operate at small scale (see Part 2 note)
- SSE pause/resume: a pause flag checked between streamed tokens, resume re-issues generation using the partial assistant message as prefix
- OpenTelemetry instrumentation across every layer → Langfuse
- Frontend observability panel: per-message trace (retrieved chunks + scores, memory items injected, tool calls, latency/cost breakdown, model used)
- Load test Qdrant at your real target scale, tune HNSW params

**Exit criteria:** any response is fully traceable end to end in the UI; a reminder scheduled days out fires correctly; pausing and resuming preserves partial output correctly.

---

## Part 2 — Detailed Layer Specs

### 1. Ingestion Layer

**Modality routing:**

| Modality | Default parser | Fallback |
|---|---|---|
| PDF | Docling | LlamaParse (messy scans) |
| DOCX / PPTX | Docling | Unstructured |
| XLSX / CSV | pandas + row-group serializer | — |
| Images | VLM captioning + multimodal embedding | Tesseract OCR (text-heavy images) |
| Video | Keyframe extraction + Whisper ASR, segmented by scene/time | — |
| YouTube | Caption/transcript API first | yt-dlp download + Whisper if no captions |

**Chunking strategy — the actual recommendation, not "it depends":**

Use **token-based recursive chunking with structural awareness** as the default, not character-count, not pure sentence-level, not pure semantic/concept-level:

- **Not character-limit** — character count doesn't map to tokens consistently across scripts/languages; token budget is what the embedding model actually sees, so that's the correct unit.
- **Not pure sentence-level** — a single sentence in isolation ("it grew 3%") is usually context-free and useless; sentence boundaries are a *fallback* the recursive splitter uses when a paragraph exceeds budget, not the primary granularity.
- **Not pure paragraph-level** — paragraph length varies too much (one line vs. 2,000 words) to use as the sole unit; use paragraph boundaries as a splitting *preference* inside the recursive splitter.
- **Not pure semantic/concept-level as the default** — embedding-similarity boundary detection tends to over-split into fragments too small to be useful in practice, and it's the most expensive option to compute at ingestion scale. Reserve it for document types that genuinely lack structure (raw meeting transcripts), not as the default path.
- **Target:** ~400–512 tokens per chunk, ~10–15% overlap, never splitting mid-table/mid-code-block/mid-list-item.
- **Add contextual retrieval on top, always**: prepend a short LLM-generated sentence describing what the chunk is and where it sits in the document, before embedding. This is the single highest-leverage addition and doesn't change your chunk boundaries — cheap to do with your fast/cheap model tier.
- **Tables (Excel/CSV):** row-group chunking that repeats the header row in every chunk — never flatten a whole sheet into prose, never split by raw token count (it severs the row/header alignment).
- **Video/audio:** the natural chunk unit is a time segment (one keyframe + its transcript window), not a token count.

**Models used at ingestion:**
- Parsing/OCR: Docling's built-in models (self-hosted)
- ASR: Whisper
- Chunk-context generation: your cheap/fast LLM tier
- Text embeddings: one fixed model across the whole system — changing this later means re-embedding everything, so commit early
- Visual embeddings: a separate multimodal/CLIP-family embedding, its own vector space, its own collection

**Canonical schema every parser writes into** (this is what makes the parser backend swappable — see the diagram from earlier in this conversation):

```python
class SourceLocator(BaseModel):
    page: int | None = None
    time_range: tuple[float, float] | None = None   # video/audio segments
    cell_range: str | None = None                    # e.g. "Sheet1!A2:D8"
    bbox: tuple[float, float, float, float] | None = None  # images

class ParsedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    modality: Literal["text", "table", "image", "video_segment", "audio_segment"]
    content: str                # extracted text OR caption/transcript
    context_prefix: str | None  # contextual-retrieval sentence
    source_locator: SourceLocator
    raw_file_uri: str           # pointer into object storage
    parser_backend: str         # provenance: which parser produced this
    embedding_model: str
    created_at: datetime
```

---

### 2. Data Layer

**Qdrant collections — one per modality, not one giant collection.** Payload shapes differ enough (an image chunk needs a bbox, a video chunk needs a time range) that separate collections are cleaner to reason about and scale independently. Query them in parallel at retrieval time and fuse with modality weights (Part 2 §4).

| Collection | Vectors | Notes |
|---|---|---|
| `kb_text_chunks` | dense (text embed) + sparse (BM25) | primary document corpus |
| `kb_image_chunks` | dense (multimodal/CLIP) | payload includes caption + bbox |
| `kb_video_segments` | dense (multimodal, frame+transcript fusion) | payload includes time range |
| `memory_items` | dense (text embed) | session **and** research memory — one collection, see below |

**Why one `memory_items` collection instead of two:** a fact learned during research and a fact learned about the user's stack are the same data shape, just different provenance. Splitting them into separate indices means every "what do I know about X" query has to hit two collections and merge — a `category` discriminator field does the same job with less overhead.

**Enums:**

```python
class ResearchMemoryType(str, Enum):
    FACT = "fact"
    STATISTIC = "statistic"
    QUOTE = "quote"
    ANALYSIS = "analysis"
    DEFINITION = "definition"

class SessionMemoryType(str, Enum):
    SOLUTION = "solution"      # episodic — a specific solved-problem instance
    KNOWLEDGE = "knowledge"    # semantic — generalized fact about the user's domain/stack
    PREFERENCE = "preference"  # procedural — behavioral rule, NOT retrieved, injected directly
    ENTITY = "entity"          # pointer to a Graphiti node, not memory content itself

class MemoryCategory(str, Enum):
    SESSION = "session"
    RESEARCH = "research"

class SolutionOutcome(str, Enum):
    WORKED = "worked"
    FAILED = "failed"
    PARTIAL = "partial"
    SUPERSEDED = "superseded"
```

**`MemoryItem` schema:**

```python
class MemoryItem(BaseModel):
    memory_id: str
    category: MemoryCategory
    memory_type: ResearchMemoryType | SessionMemoryType
    content: str
    embedding_model: str
    source_session_id: str | None
    source_locator: SourceLocator | None    # mandatory for QUOTE — exact span, not just "it was in there somewhere"
    confidence: float
    outcome: SolutionOutcome | None         # only set when memory_type == SOLUTION
    valid_from: datetime
    valid_to: datetime | None               # null = still valid; set = superseded, not deleted
    superseded_by: str | None               # memory_id of whatever replaced this
    tags: list[str]
    project_scope: str | None
```

Note on `PREFERENCE`: this type is stored here for auditability, but at inference time it should **not** go through retrieval — pull it directly into the system prompt every turn, same as a small rules file. Treating preference as "just another retrievable fact" is the mistake that makes procedural memory invisible when it matters most.

**Graphiti / FalkorDB (temporal graph, session-scoped):**
- Ingested unit: an **Episode** (Graphiti's atomic ingestion primitive) — a session's raw exchange gets ingested as an episode, then Graphiti extracts entity nodes and edges from it
- Node type: `Entity` (name, entity_type: person / project / tool / concept / organization, first_seen, last_seen)
- Edge types: `RELATES_TO`, `WORKED_ON`, `PREFERS`, `SOLVED_WITH`, `MENTIONED_IN` — every edge carries `valid_from`/`valid_to` (Graphiti's native temporal validity) and a `source_session_id` for provenance
- Isolation: `group_id` per user/project, Graphiti's native multi-tenancy mechanism

**Postgres (relational, for what graph/vector don't fit):**
- `sessions` (session_id, user_id, project_scope, created_at, title)
- `documents` (doc_id, filename, modality, raw_file_uri, parsed_at, parser_backend, status)
- `preferences` (small table, loaded directly into system prompt every turn)

---

### 3. API Layer (FastAPI)

| Endpoint | Purpose |
|---|---|
| `POST /sessions` / `GET /sessions` / `GET /sessions/{id}` | session CRUD |
| `POST /sessions/{id}/messages` | SSE streaming chat (`StreamingResponse`, `media_type="text/event-stream"`) |
| `POST /sessions/{id}/messages/{message_id}/pause` | sets a flag checked between streamed tokens |
| `POST /sessions/{id}/messages/{message_id}/resume` | re-issues generation using the partial assistant message as prefix |
| `POST /documents` | upload, returns `202 Accepted` + `doc_id`, ingestion runs in a background worker |
| `GET /documents/{id}/status` | poll ingestion progress |
| `POST /memory/search` | explicit memory query tool |
| `POST /research` | kick off a deep-research job (async; poll or SSE for progress) |
| `GET /observability/traces/{message_id}` | full trace for a given response |

**Retrieval-mode toggle** is a request param on `/messages`: `retrieval_modes: list[Literal["kb", "web", "deep_research"]]` — multi-select, so a user can combine KB + web in one turn.

**Background jobs:** ingestion and deep-research are long-running — put them on a task queue (ARQ/Celery, or Temporal once Phase 5 lands) so they don't block the request cycle.

**SSE pause/resume, concretely:**
- Generation loop yields tokens from the LLM stream; between yields it checks a cheap flag (in-memory dict for single-worker, Redis key for multi-worker deployments)
- On pause: stop yielding, persist `partial_content` + `finish_reason="paused"`
- On resume: call the LLM again with `[...original messages, {role: "assistant", content: partial_content}]` and ask it to continue — this is a normal multi-turn continuation for every major provider, nothing exotic needed

---

### 4. Retrieval Engine

Pipeline, in order:

1. **Query decomposition** (cheap-tier LLM call): break a complex query into 1–4 sub-questions; skip entirely for queries that are already atomic — don't pay the extra call on every turn.
2. **Hybrid retrieval per sub-question**, run in parallel across whichever collections the retrieval-mode toggle enables: Qdrant `prefetch` with dense + sparse BM25 sub-queries, fused via Qdrant's native Reciprocal Rank Fusion.
3. **Modality weighting**: apply a multiplier to each collection's fused score before merging across collections — e.g. text 1.0, image 0.8, video 0.7, memory 1.1 as a starting point, tunable, and bumpable per query (a query with clear visual intent can raise the image/video weight).
4. **Merge** all weighted candidates, take the top ~20–30.
5. **Rerank** with a cross-encoder down to the top 5–8.
6. **RLM check**: if reranked context + injected memory + system prompt crosses your token-budget threshold, route the aggregation through an RLM call instead of truncating.
7. **Generate.**

---

### 5. Web Search Engine

**Provider choice:** Tavily as default — LLM-native output, `Search`/`Extract`/`Crawl`/`Map`/`Research` endpoints in one API, native `include_domains`/`exclude_domains` filtering. Brave Search API as a secondary/fallback specifically because it runs its own independent index (not reselling Google/Bing) and has a dedicated LLM-context endpoint — worth having a second provider behind your abstraction the same way you're keeping LLM providers swappable, since Tavily's ownership already changed once this year.

**Reliable sources over fluff — three layers, not one:**
1. Provider-native domain filtering: a versioned allowlist (wire services, .gov/.edu, standards bodies, established mastheads, peer-reviewed publishers) and blocklist (known content farms), passed as `include_domains`/`exclude_domains`.
2. A trust-score field computed per result at retrieval time — tiered: primary/official > established journalism > reputable vendor docs/blogs > forums/UGC > unknown — used to re-rank before results reach the synthesizer.
3. Cross-reference requirement: any claim used in a deep-research output needs corroboration from ≥2 independent sources before it's stated without a hedge.

**Do you need RLM here?** Not for the search/ranking step itself — that's a provider-infrastructure concern, not a context-length problem. You need it one stage later: once you fetch **full-text** content from multiple sources for a deep-research pass (not just snippets), the aggregated corpus routinely exceeds a normal context window across 10–20 sources. That's a genuine overflow problem, and that's where RLM belongs — in the Deep Research Engine's synthesis step, not inside the plain web-search tool a normal chat turn calls for a quick lookup.

---

### 6. Deep Research Engine

1. **Planner** — decomposes the research question into sub-questions (reuses the retrieval engine's decomposition utility).
2. **Parallel searcher subagents** — one per sub-question, each choosing web search, internal KB retrieval, or both.
3. **Full-text fetch** — pull complete content for the most promising sources, not just snippets (Tavily `Extract` or `web_fetch`).
4. **RLM synthesizer** — hands the entire aggregated full-text corpus to a recursive call rather than pre-summarizing lossily or truncating.
5. **Citation-check subagent** — independently re-verifies every claim in the draft against the actual fetched source text, never against the synthesizer's summary of it. This is the detail that prevents a "game of telephone" where facts drift through re-summarization.
6. **Report assembly** — executive summary, findings per sub-question, citation list.

---

### 7. Frontend

- **Session list**: new chat, past sessions grouped by project scope
- **Chat window**: SSE message stream, retrieval-mode toggle chips (KB / Web / Deep Research, multi-select), drag-drop upload with per-file ingestion status, pause button during generation → resume if paused
- **Observability panel** (expandable per message): retrieved chunks + scores, memory items injected, tool calls made, latency/cost breakdown, model used
- **Memory browser** (Phase 5+): view/edit/delete extracted memory items — important for trust, and the same pattern Claude's own memory feature exposes to users
- **Tech**: plain SSE (`EventSource`/fetch-stream) is enough — pause is a separate REST call, so you don't need bidirectional WebSocket infrastructure for this

---

## Appendix — Full Stack Summary

| Layer | Choice |
|---|---|
| Parsing | Docling + Whisper + yt-dlp, LLM-based captioning for images/video |
| Chunking | Recursive token-based (~450 tokens, structural boundaries) + contextual retrieval |
| Vector DB | Qdrant, per-modality collections, dense+sparse hybrid, native RRF |
| Graph/temporal memory | FalkorDB + Graphiti |
| Relational | Postgres (sessions, documents, preferences) |
| Object storage | S3/GCS/local disk |
| LLM gateway | `google-genai` direct (Phase 0–2) → LiteLLM (Phase 3+) |
| Web search | Tavily default, Brave fallback |
| Long-context overflow | RLM, triggered by token-budget threshold |
| Deep research | Custom orchestrator-worker + citation-check subagent |
| Scheduler | APScheduler (small scale) or Temporal (durability across restarts) |
| Observability | OpenTelemetry → Langfuse |
| API | FastAPI, SSE streaming, background task queue for long jobs |