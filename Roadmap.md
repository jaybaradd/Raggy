# Raggy — Product and Technical Roadmap

This roadmap defines Raggy as a multimodal evidence, retrieval, knowledge-atom, and memory system. Raw assets remain immutable; every parsed segment, extracted atom, and durable memory record must retain provenance, scope, confidence, and lifecycle history.

## Architecture principles

1. A retrieval chunk is not a knowledge atom. Chunks support search; atoms support facts, preferences, solutions, entities, temporal reasoning, and contradiction handling.
2. Multimodal inputs retain modality-specific evidence: OCR, captions, transcripts, frames, tables, layout, timestamps, and bounding boxes.
3. Session memory is private by default. Promotion to a project is explicit, policy-driven, or user-confirmed. Global user memory is deferred until it has an explicit opt-in product design.
4. Postgres/object storage is authoritative for records and audit history. Qdrant and Graphiti/FalkorDB are rebuildable projections.
5. Every durable claim has evidence references, confidence, extraction metadata, owner, scope, and lifecycle status.
6. Original assets and evidence are never silently overwritten; derived indexes must be reproducible.

## Current baseline

- Phase 0 core loop is substantially implemented: FastAPI, PDF parsing, local embeddings, Qdrant, and Gemini SSE chat through LiteLLM.
- Hybrid dense/BM25 retrieval and cross-encoder reranking are substantially implemented.
- Multimodal parser coverage is partial: image OCR/captioning, spreadsheets, video transcription, and YouTube paths exist, but complete provenance remains. Visual embeddings and video keyframe evidence are intentionally deferred as future scope. Contextual retrieval is also deferred as a later optimization.
- Memory, knowledge atoms, web research, workflows, and observability are planned work.

## Phase 0 — Foundations and evidence identity (weeks 1–2)

### Build

- FastAPI session CRUD and SSE streaming chat.
- PDF ingestion with Docling.
- Local dense embeddings and one Qdrant text collection.
- Gemini-backed streaming chat and structured JSON generation.
- Token-aware structural chunking.
- Content-addressed immutable `Asset` records: asset ID, owner, project, filename, MIME type, storage URI, hash, and timestamps.
- Stable `EvidenceSegment` records and provenance fields from the beginning.
- Structured JSON logging and deterministic request/session/message IDs.

### Evidence model

```python
class EvidenceSegment:
    evidence_id: str
    asset_id: str
    modality: Literal["text", "table", "image", "video", "audio"]
    representation: Literal["text", "ocr", "caption", "transcript", "frame", "layout"]
    content: str | None
    media_uri: str | None
    locator: SourceLocator       # page, timestamp, cell range, bbox
    parent_evidence_id: str | None
    parser_backend: str
    parser_version: str
    created_at: datetime
```

### Exit criteria

Upload a PDF, retrieve grounded evidence, stream an answer, and display a citation resolving to the original asset and locator. Cross-session memory is not required yet.

## Phase 1 — Multimodal evidence and retrieval (weeks 3–6)

### Build

- Parser abstraction for PDF, DOCX/PPTX, XLSX/CSV, images, video, audio, and YouTube.
- Raw asset storage plus immutable evidence segments.
- Image pipeline: separate OCR evidence and caption evidence, each linked to the original asset and locator.
- Video pipeline: transcript windows and timestamps. Keyframe extraction and frame/transcript links are deferred.
- Spreadsheet pipeline: typed cells, headers, row groups, sheet names, and valid cell ranges.
- Per-modality Qdrant collections or named vector spaces: text, image, video, table, and later memory.
- Dense plus sparse retrieval where applicable, modality-aware fusion, and reranking.
- Preserve all locator fields in retrieval payloads.
- Background ingestion jobs with explicit status transitions; long jobs must not block API requests.

### Retrieval contract

Retrieval returns evidence references, not only strings:

```text
EvidenceResult:
  evidence_id, asset_id, content/representation,
  locator, modality, score, rerank_score
```

### Exit criteria

- Each supported modality produces inspectable evidence with correct locators.
- A video answer can cite a transcript timestamp; frame citations are deferred with keyframe extraction.
- A spreadsheet answer can cite sheet and cell range.
- Hybrid/reranked retrieval beats dense-only retrieval on a labeled evaluation set.

### Deferred optimization

Contextual retrieval prefixes are intentionally not part of the Phase 1 completion gate. Revisit them after the evidence model, modality-specific retrieval, and evaluation set are stable. When revisited, implement them as an adaptive, cached feature with metadata-only and LLM-enhanced modes; never replace or overwrite original evidence.

### Deferred visual scope

Visual image embeddings, image-region similarity search, video keyframe extraction, and frame/transcript fusion are intentionally deferred. Phase 1 will use caption text and OCR text as the searchable image/video representations while retaining the original media URI for future expansion.

## Phase 2 — Knowledge atoms and durable memory (weeks 7–10)

### Atom extraction pipeline

Run asynchronously after ingestion and after relevant session turns:

```text
evidence segments + conversation turn
  → candidate atom extraction
  → schema validation
  → deterministic retention policy
  → entity resolution / normalization
  → duplicate and contradiction detection
  → confidence and provenance attachment
  → session candidate or active project/user memory
```

Model-generated claims are candidates until supported by evidence or explicit user confirmation. Extraction never replaces source evidence.

### Common memory envelope

```python
class MemoryRecord:
    memory_id: str
    owner_id: str
    scope: Literal["session", "project", "user", "organization"]
    session_id: str | None
    project_scope: str | None
    kind: Literal["knowledge", "preference", "solution", "entity", "event"]
    status: Literal["candidate", "active", "superseded", "rejected", "expired"]
    confidence: float
    user_confirmed: bool
    evidence_refs: list[str]
    source_turn_id: str | None
    extraction_model: str
    extraction_version: str
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: str | None
    created_at: datetime
    updated_at: datetime
```

Use typed payloads rather than one nullable schema:

- `KnowledgeAtom`: subject, predicate, object, qualifiers, and temporal scope.
- `PreferenceMemory`: preferred behavior, scope, strength, consent, and applicability conditions.
- `SolutionMemory`: problem signature, environment, steps, outcome, and verification evidence.
- `EntityMemory`: canonical entity ID, type, aliases, and graph links.
- `EventMemory`: user-stated operational event type, summary, entities, locations, and temporal scope.

### Memory lifecycle

- Session records are private by default.
- Candidate records require evidence and confidence thresholds.
- Promotion to project/user scope is explicit, policy-driven, or user-confirmed.
- Named projects use a transparent automatic-capture policy for high-confidence, user-stated operational events and explicit project preferences. Generic facts, entities, solutions, questions, rumours, assistant output, and document-only claims remain reviewable candidates until their dedicated policies exist.
- Time-bound events receive a deterministic validity/review window derived from the user wording when possible; otherwise they receive a conservative default review window.
- New claims supersede old claims only when subject, predicate, scope, and temporal context match.
- Contradictions remain auditable; they are not silently deleted.
- Preferences are injected through a relevance/scope policy, not dumped into every prompt.
- Users can inspect, correct, reject, expire, or delete durable memories.

### Storage responsibilities

- Postgres: authoritative atoms, memory lifecycle, scopes, audit events, and promotion decisions.
- Object storage: original assets and immutable extracted artifacts.
- Qdrant: semantic projections of evidence and active memory records.
- Graphiti/FalkorDB: entity nodes and temporal relationships, rebuildable from authoritative records.

### Fresh PostgreSQL cutover decision (2026-09-14)

The current SQLite data is disposable development/test data, not production data. Raggy will therefore **start fresh in PostgreSQL** rather than perform a SQLite-to-Postgres data import.

- Existing SQLite database files, uploaded files, and Qdrant data are retained untouched as an archive and rollback reference; they are not deleted or imported.
- PostgreSQL schema migrations remain required and will initialize an empty authoritative database.
- PostgreSQL becomes selectable only when the session, memory, and evidence repositories can be constructed together as one authority.
- A fresh Qdrant collection will be used for Postgres-backed evidence and memory projections; the existing collection is retained unchanged.
- The local graph projection starts empty and is rebuilt solely from new Postgres memory records. FalkorDB is introduced only after the Postgres cutover and operational checks succeed.

### Hybrid memory architecture decision

Keep the memory system hybrid rather than selecting one storage mechanism for every workload:

- SQLite during local development, replaceable by Postgres for durable authoritative memory records, scopes, lifecycle, ACLs, promotion decisions, contradictions, and audit history.
- Qdrant for semantic projections of active, permitted memories and evidence; it is a retrieval accelerator, never the source of truth.
- A graph projection for durable concept/entity nodes and typed relationships; it is rebuildable from authoritative memory and evidence records.

Advantages:

- Relational storage provides transactions, exact filtering, lifecycle control, provenance, and predictable permission checks.
- Qdrant provides fuzzy natural-language recall when the user query does not match stored wording.
- The graph provides multi-hop relationships, temporal structure, dependency traversal, and explainable connected context.
- Each projection can be repaired or rebuilt independently without losing the underlying memory.

Risks and mitigations:

- Hallucinated or noisy relationships: retain raw claims and evidence in the authoritative store; project only validated/active memories; preserve confidence and review state.
- Graph bloat: graph durable atoms and relationships, not every raw chunk or ephemeral conversation statement.
- Duplicate entities/concepts: normalize aliases and external IDs; use conservative merges; retain unresolved candidates instead of silently merging.
- Contradictory or stale facts: preserve both claims, add temporal validity and contradiction/supersession relationships, and exclude inactive projections from retrieval.
- Privacy leakage: apply owner, project, session, status, and validity filters before both vector and graph retrieval; candidates remain private by default.
- Projection inconsistency or graph-service failure: write the relational record first, synchronize projections asynchronously, retry failures, and support full rebuilds.
- Operational complexity: begin with SQLite plus a local/rebuildable graph-compatible projection; introduce Postgres and Graphiti/FalkorDB only when scale or traversal needs justify them.

Design rule: relational memory records remain authoritative; Qdrant and the graph are specialized, scoped, rebuildable projections.

### Gap-bridging implementation sequence

| Gap | Bridge | Delivery boundary |
| --- | --- | --- |
| SQLite is development-only | Keep repository interfaces and migrations SQLite-compatible; move the same schema to Postgres before multi-user deployment. | Postgres migration, not a blocker for local graph foundation. |
| Sessions are in memory | Use the new SQLite session/message repository locally; migrate the same repository contract and schema to Postgres before multi-user deployment. | Durable local sessions complete; Postgres migration remains pending. |
| No graph projection | Add graph-compatible node, alias, edge, and projection-job tables to the authoritative store. | Phase 2 graph foundation. |
| Basic normalization only | Add conservative exact canonical/alias/external-ID resolution; ambiguous names remain separate candidates. | Phase 2 graph foundation. |
| Atoms are disconnected strings | Project active knowledge atoms into provenance-linked graph edges between resolved nodes. | Phase 2 graph foundation. |
| No generic relationship model | Preserve an open normalized predicate plus a controlled broad relation family and qualifiers. | Phase 2 graph foundation. |
| Event conflicts | Deterministic event identity deduplicates repeats; material changes create reviewable conflict candidates with explicit resolution. General knowledge-atom conflict detection remains future work. | Event lifecycle slice complete; general contradiction handling later. |
| Projection sync is best effort | Persist projection jobs in the same transaction as memory changes; retry and rebuild projections independently. | Phase 2 graph foundation. |
| Incomplete provenance | Record provenance kind (`conversation`, `evidence`, `inferred`) and require source turn and/or evidence references. | Phase 2 graph foundation. |
| Vector-only retrieval | Retain vector retrieval; add graph neighborhood/traversal and fusion only after the graph is populated. | Phase 3 retrieval and reasoning. |

Implementation order:

1. Add graph-compatible authoritative schema and a deterministic resolver.
2. Project active memory records into nodes and generic edges, including provenance and temporal qualifiers.
3. Add a durable projection outbox, retries, status inspection, and rebuild support.
4. Add contradiction candidate detection and review APIs.
5. Initialize a fresh Postgres authority, run Postgres-backed UI/restart checks, and rebuild fresh vector/graph projections.
6. Add FalkorDB as a rebuildable graph projection and validate it against the local graph projection. **Completed locally:** contract, lifecycle, isolation, rebuild, and adapter-restart checks run against isolated Postgres/Falkor test resources; Phase G remains the safe-default cutover decision.
7. Add graph/vector fused retrieval and multi-hop planning in Phase 3.

### Exit criteria

- A fact learned in session A is recalled in session B only when scope permits.
- Preferences apply only to the relevant user/project/task context.
- A successful solution is retrievable by a similar future problem with environment and evidence.
- Contradicted facts are superseded or marked uncertain, never silently overwritten.
- Every recalled memory links to originating evidence and session.

## Phase 3 — Multi-provider reasoning and long-context handling (weeks 11–13)

### Current implementation scope

- Use the LiteLLM **Python SDK in-process**, with Gemini as the only configured model. Keep one concrete client for streamed chat and JSON generation; do not add a provider registry, role tiers, or a proxy container yet.
- Preserve `GEMINI_CHAT_MODEL` by deriving `gemini/<model>` when `LITELLM_MODEL` is unset. A second provider can later be introduced through that one model setting before any routing abstraction is warranted.
- Add bounded query decomposition only for clearly compound retrieval questions (maximum three subqueries). Merge and deduplicate candidates, then perform one final rerank against the original question.
- Build one merged memory candidate set across the original query and its subqueries, then make exactly one constrained memory-selection call using the original user question.
- Resolve a possible event change before ordinary memory retrieval: constrain one generic `update`/`not_update`/`ambiguous` decision to the immediately preceding assistant response's durable trace-linked event. Use the bounded recent-chat window only as reference; an ambiguous result asks for clarification rather than guessing.
- Keep source evidence, confirmed memory, and prompt assembly separate, with deterministic context limits and safe fallbacks to the original query.
- Expose durable post-turn memory-processing status in the chat UI, and make failed document/YouTube ingestion explicitly retryable from its immutable source. Cover these lifecycle paths with deterministic local regression tests; live-provider and network behavior remain manual smoke checks.

### Deferred deliberately

- LiteLLM Proxy/Compose service: useful for multiple applications, providers, virtual keys, centralized budgets/rate limits, and shared observability; unnecessary overhead for one local app and one Gemini key.
- Cheap/frontier model tiers, RLM context traversal, prompt caching, and LangGraph orchestration. Revisit them once evaluation data demonstrates a concrete need.
- Atom-aware synthesis remains a follow-on refinement after the retrieval and context path is measured.

### Exit criteria

Provider switching requires configuration changes only. Large evidence-plus-memory queries produce coherent answers with citations and confidence indicators.

## Phase 4 — Web search and deep research (weeks 14–17)

- Tavily default and Brave fallback behind a search interface.
- Trust tiers, domain policy, retrieval-time source scoring, and cross-source corroboration.
- Research evidence and research atoms use the same provenance model as user documents.
- Planner, parallel searchers, full-text fetch, RLM synthesis, independent citation verification, and report assembly.
- Research-derived atoms cannot silently become personal user facts without an explicit promotion rule.

### Exit criteria

Every report claim resolves to fetched source text and every durable research memory preserves source, date, scope, and provenance.

## Phase 5 — Workflows, observability, and trust UX (weeks 18–22)

- Durable scheduler for reminders, reports, and digests.
- Pause/resume generation with persisted partial output.
- OpenTelemetry and Langfuse traces across parsing, retrieval, extraction, memory decisions, and generation.
- Per-message trace showing evidence, atoms, memories, tool calls, latency, cost, and model versions.
- Memory browser with inspect, confirm, edit, reject, expire, delete, and provenance navigation.
- Evaluation and load-testing harness for retrieval, extraction precision/recall, contradiction handling, and scope isolation.

### Exit criteria

Responses are traceable end to end; workflows survive restarts; users can understand and control durable memory; isolation tests show no cross-user or cross-project leakage.

## API additions

- `POST /assets` and `GET /assets/{id}/status`
- `GET /evidence/{id}`
- `POST /memory/candidates/{id}/confirm`
- `POST /memory/{id}/reject`
- `POST /memory/{id}/supersede`
- `GET /memory?scope=&kind=&status=`
- `POST /memory/search`
- `GET /observability/traces/{message_id}`

Message and retrieval responses carry `evidence_refs`, `memory_refs`, and scope metadata so the frontend can render provenance and explain why a memory was used.

## Non-functional requirements

- Authorize every asset, evidence, session, and memory query by tenant/user/project.
- Sanitize filenames and keep object storage private; never expose raw upload directories publicly.
- Make ingestion and extraction idempotent using content/version hashes.
- Version parsers, embedding models, extraction prompts, and atom schemas.
- Keep raw evidence immutable and make all derived indexes rebuildable.
- Maintain golden sets for each modality and memory kind before declaring a phase complete.
