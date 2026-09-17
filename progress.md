# Raggy — Progress Log

---

### ✅ [2026-09-17] Phase 3 foundation — LiteLLM SDK and bounded decomposition

- Replaced the unused `LLMProvider`/Gemini inheritance seam with one concise in-process `LiteLLMClient`. It preserves the existing Gemini key and model by deriving `gemini/<GEMINI_CHAT_MODEL>` unless `LITELLM_MODEL` is set, and exposes only streamed chat plus JSON generation.
- Chose the LiteLLM SDK rather than a LiteLLM Proxy Compose service: one local app and one Gemini key do not need a network hop or gateway configuration. A proxy becomes appropriate when several applications/providers/users need shared virtual keys, budgets, rate limiting, or observability.
- Added an opt-in, maximum-three-subquery decomposition path for clearly compound document questions. It merges evidence by durable identity and performs one final rerank against the original question; malformed, unavailable, or disabled decomposition uses the original question unchanged.
- Added focused unit coverage for the LiteLLM boundary and decomposition behavior. Query decomposition defaults off until real complex-query evaluation data is available.

### ✅ [2026-09-17] Reliable conversational event-update targeting

- Added a trace-backed update resolver that runs before ordinary memory retrieval. It follows the immediately preceding assistant reply's `trace_id` to its `memory_access_events` rows and uses real `memory_id` values, never the per-response `M1` display label.
- The resolver rehydrates and scope-validates one active confirmed event, retains up to ten recent messages only as non-factual reference, and makes a generic constrained `update`/`not_update`/`ambiguous` decision against that event. Multiple eligible prior-response events remain ambiguous.
- Targeted extraction must preserve an event's subject and change one of its existing claim attributes; unrelated generic events are rejected. A valid target update becomes an existing review conflict instead of silently replacing the old record.
- A resolved update replaces ordinary memory retrieval for that turn, so a semantically retrieved but unrelated event cannot reach the answer or extraction prompt. The answer describes the change as pending confirmation; ambiguous decisions ask a concise clarification instead of claiming a memory was changed.

---

### ✅ [2026-09-15] Containerized local stack implementation

- Added a single default Podman Compose stack for the Raggy app, PostgreSQL, Qdrant, and FalkorDB. The app container uses internal service DNS (`postgres`, `qdrant`, `falkordb`) while the browser continues to use `localhost:8000`.
- Added named volumes for authoritative Postgres data, derived Qdrant/Falkor state, uploads, and reusable local-model caches. The app runs the existing `main.py` after a small dependency-wait entrypoint.
- Added `Dockerfile`, `.dockerignore`, and `CONTAINERS.md`. The first startup downloads local embedding/BM25 assets into the model-cache volume; later starts reuse it.
- Initial image intentionally matches the existing host runtime and adds no unverified Linux media/GUI packages. Add a system package only if an exercised container ingestion path demonstrates it is needed.
- The current Podman machine was unavailable while this slice was implemented, so the rendered Compose configuration and full image build remain a manual acceptance check.

### ✅ [2026-09-15] FalkorDB Phase G — Postgres development cutover

- `scripts/run_postgres_dev.sh` now selects PostgreSQL authority and FalkorDB graph projection as one explicit development profile. Plain `python main.py` retains SQLite authority and the SQLite graph default.
- Added one shared profile script for the app launcher and `scripts/rebuild_postgres_falkor_graph.sh`, preventing maintenance commands from accidentally rebuilding an archived SQLite graph.
- Startup logs the selected authority, graph backend, and Falkor graph name. Repository construction remains the preflight: unavailable Postgres or FalkorDB aborts startup before requests are served, without fallback.
- Added a repository-factory cutover test proving the Postgres/Falkor profile never constructs SQLite repository implementations.

### ✅ [2026-09-15] FalkorDB Phase F — reliability verification

- Extended the fake-client contract suite to prove parameterized Cypher writes, owner/project filtering, idempotent graph writes, inactive lifecycle handling, and rejection of cross-project edges.
- Extended the opt-in Postgres/Falkor suite with isolated random schemas and graph names. It now verifies active-to-expired projection, related-edge idempotency, owner/project isolation, authoritative rebuild, and reading the rebuilt graph through a fresh adapter instance.
- Verified locally with `RUN_FALKOR_INTEGRATION_TESTS=1` against Podman PostgreSQL and FalkorDB. Each test removes only its own random graph and schema.
- FalkorDB remains derived and opt-in; `GRAPH_PROJECTION_BACKEND=sqlite` stays the default pending Phase G cutover.

### ✅ [2026-09-15] FalkorDB Phase E — durable correction lineage

- Accepted conflict resolutions and manual supersedes now persist an idempotent, owner-scoped `superseded_by` relationship with source, actor, optional conflict ID, details, and audit history.
- Replaced the former manual-supersede `contradiction` write with precise `SUPERSEDED_BY` lifecycle semantics. Legacy contradiction rows remain readable but no new correction creates one.
- Graph projection retains superseded source nodes as inactive historical context and projects `SUPERSEDED_BY` to the active replacement. Phase D traversal explicitly follows only `RELATED` edges, so superseded records cannot re-enter prompt context.
- Added `GET /api/memories/{memory_id}/relationships` and relationship history in the memory browser.

### ✅ [2026-09-15] FalkorDB Phase D — graph-assisted memory discovery

- Added optional, one-hop graph expansion to the pre-response memory planner. It starts only from exact/Postgres or Qdrant candidates, returns relationship provenance rather than graph payloads, and rehydrates every graph ID from the authoritative memory repository before planner selection.
- Graph candidates are never included in exact-match fallback context. Planner approval, owner isolation, project/session scope, active status, confirmation, and validity windows remain mandatory.
- Added `GRAPH_MEMORY_EXPANSION_ENABLED=false` and a bounded `GRAPH_MEMORY_EXPANSION_LIMIT=4`; graph expansion is opt-in and failure-safe.
- Added graph candidate provenance and expansion status to the existing memory SSE payload and access-event details.
- Repository field access is now method-lazy, so router imports no longer establish optional backend connections. Application startup remains the first explicit repository construction point.

### ✅ [2026-09-15] FalkorDB graph projection hardening

- Scoped authoritative relationship reads by `owner_id` before graph-edge projection, closing the last cross-owner graph discovery seam.
- Added an explicit, backend-neutral graph rebuild service and `scripts/rebuild_graph_projection.py`. It clears only the selected derived graph, re-enqueues only graph jobs, and never initializes, consumes, or modifies Qdrant work.
- Made the repository provider lazy. Optional FalkorDB connectivity is now resolved during FastAPI startup rather than while importing routers or utility modules.
- Added both fake-client contract coverage and opt-in real Postgres-to-Falkor integration coverage. The integration test uses a random schema and graph name and removes both afterward.

### ✅ [2026-09-15] Multi-event correction reconciliation

- A correction turn with multiple extracted events now associates each event independently with one high-confidence planner-selected memory using conservative entity-subject overlap.
- This opens separate review conflicts for independent changes in the same turn while excluding time/status-only matching and retaining ambiguous cases as safe review candidates.

### ✅ [2026-09-14] FalkorDB graph projection adapter and local runtime profile

- Added the optional, pinned `FalkorDB` Python client and `FalkorGraphStore`, which implements the same conservative graph repository contract as SQLite with idempotent memory-node and durable relationship-edge writes.
- Added an opt-in Podman Compose `falkor` profile using the FalkorDB development image, persistent AOF-backed storage, localhost-only server/browser ports (`6380`/`3001`), and a health check.
- Added explicit `GRAPH_PROJECTION_BACKEND`, `FALKORDB_URL`, and `FALKORDB_GRAPH_NAME` settings. SQLite remains the default; choosing Falkor makes repository initialization fail clearly if its service is unavailable.
- The Podman profile pins the multi-architecture `falkordb/falkordb:v4.20.4` image after validating its published tag; the earlier unprefixed `4.0.0` tag was not available.
- Validated the installed FalkorDB client against the local container and corrected its connection path to `FalkorDB.from_url(...)`; empty named-graph reads now return an empty projection result until the first memory is written.

### ✅ [2026-09-14] FalkorDB preparation — conservative memory graph model

- Replaced the SQLite projection's extracted entity/concept graph with a lossless memory-node model: one node per authoritative memory, retaining lifecycle fields and serialized payload details.
- Durable `memory_relationships` now provide the only graph edges. `RELATED` edges project only while both endpoint memories are active, confirmed, and in their validity window; open conflicts never invent a contradiction edge.
- Added backend-neutral graph node/relationship contracts, PostgreSQL and SQLite relationship readers, exact `project_id`-first filtering, and lifecycle-safe removal of stale incident edges.

### ✅ [2026-09-14] FalkorDB preparation — explicit graph projection seam

- Memory projection dispatch now requires an explicit `GraphRepository`; it no longer silently imports the module-level SQLite graph singleton.
- Startup repair, post-turn extraction, expiry, memory lifecycle APIs, and projection/rebuild scripts all forward the repository factory's selected graph implementation.
- Added regression coverage proving the explicit graph target receives queued graph work, making a future FalkorDB adapter a normal repository substitution rather than a special path.

### ✅ [2026-09-14] Deterministic high-confidence correction fallback

- A single selected, scope-eligible memory with planner relation `updates` and confidence at least 0.90 now creates a generic review conflict directly from claim differences; it no longer depends on a second structured LLM call succeeding.
- The old active fact still remains unchanged until the existing conflict-resolution action is chosen. The response policy now treats new user operational facts as proposed corrections rather than dismissing them because older memory disagrees.

### ✅ [2026-09-14] Implicit-reference reconciliation and truthful memory responses

- The post-turn reconciler can now consider only event memories selected for the current turn's context, allowing high-confidence references such as “the meeting with my manager” to open a reviewable update conflict without a synthetic identifier.
- Implicit matches require high reconciliation confidence and are scope-revalidated from the authoritative store; low-confidence or failed decisions remain reviewable candidates without a conflict or lifecycle mutation.
- Memory extraction is grounded in the user turn alone, and the chat system prompt prohibits claims that a memory has already been updated or resolved before the lifecycle system confirms it.

### ✅ [2026-09-14] Generalist memory reconciliation — Slice 4 lifecycle decisions

- Post-turn event extraction now discovers exact identifier candidates before writing an incoming event, and uses a constrained reconciler to choose `new`, `duplicate`, `update`, `related`, or `uncertain`.
- Material updates are stored as reviewable candidates with generic `claim_mismatch` conflicts; active memories are never overwritten automatically. Planner failures also retain a candidate rather than making a destructive guess.
- Duplicate observations are auditable without duplicate records; related records create idempotent authoritative relationship links for later graph projection. SQLite and PostgreSQL share the lifecycle contract.

### ✅ [2026-09-14] Generalist memory reconciliation — Slice 3 pre-response planner

- Added a bounded pre-response planner that combines deterministic identifier candidates with scoped Qdrant semantic suggestions, then accepts only structured, candidate-set-bounded selections for prompt injection.
- Semantic hits are revalidated against the authoritative memory repository before planning; unavailable or invalid planning safely falls back to high-confidence exact identifier matches only.
- Memory SSE/access provenance now includes candidate source, planner selection relation/confidence, planner status, and a short rationale for inspection.

### ✅ [2026-09-14] Generalist memory reconciliation — Slice 2 identifier candidates

- Added a normalized, authoritative `memory_identifier_references` index in SQLite and PostgreSQL, synchronized atomically with every memory write.
- Added exact, active, confirmed, unexpired candidate lookup across the current session, user scope, and project scope; stable project IDs are authoritative while name matching is legacy-only.
- Preserved a bounded read-only fallback for legacy event entity strings without rewriting archived records.

### ✅ [2026-09-14] Generalist memory reconciliation — Slice 1 contract foundation

- Replaced the shipping-specific event alias taxonomy with lossless label normalization; descriptive event labels no longer gate project-memory capture.
- Added additive `identifier_references` and explicit `claims` to event payloads, retaining raw mentions and normalized values for the next identifier-index and reconciliation slices.
- Updated extraction guidance and semantic projection text while retaining existing event fields and storage compatibility.

### ✅ [2026-09-14] Project-memory retrieval filter correction

- Corrected the Qdrant eligibility filter so project memories retain `scope=project` while project ID/name are applied as separate constraints.
- This restores active project-memory retrieval across different chats in the same project; the prior filter incorrectly searched for nonexistent `scope=project_id` and `scope=project_scope` values.
- Added a regression test for the exact cross-chat project-memory scenario.

### ✅ [2026-09-14] One-command local PostgreSQL launcher

- Added `scripts/run_postgres_dev.sh`, which loads the existing `.env`, selects Postgres authority, and uses separate fresh Qdrant, upload, and graph paths without storing credentials in code.
- The normal `python main.py` path remains unchanged for archived SQLite compatibility; the launcher makes the intended fresh-Postgres UI path explicit and repeatable.

### ✅ [2026-09-14] Postgres HTTP restart E2E

- Added an opt-in black-box test that starts real Uvicorn processes against an isolated Postgres schema, local Qdrant directory, graph projection file, and upload directory.
- The test creates a project and chat, streams deterministic test-provider turns through the public API, uploads and indexes a CSV, restarts the server, and verifies persisted history, ingestion status, and raw-source access through HTTP.
- The test provider is confined to the test Uvicorn entry point; production Gemini behavior and configuration are unchanged. It uses cached local models only and never triggers a model download.

### ✅ [2026-09-14] Durable evidence-to-Qdrant projection path

- Evidence persistence now queues Qdrant projection work in the same durable store transaction; ingestion no longer writes vectors before the evidence record exists.
- The projection worker reconstructs a retrieval chunk from durable evidence provenance, embeds it, writes it to Qdrant, and records completion or a retryable failure in the outbox.
- SQLite now has the same evidence projection-outbox contract as PostgreSQL, including failed-job requeue and full rebuild enqueue support. Startup performs a bounded recovery pass.
- Added deterministic worker/retry coverage and a separately opt-in test that uses a fresh Qdrant collection with the production Qdrant payload implementation.

### ✅ [2026-09-14] Fresh PostgreSQL cutover decision

- Confirmed that the current SQLite records are disposable test/development data; no SQLite-to-Postgres data migration will be performed.
- SQLite databases, uploaded files, and current Qdrant data remain untouched as an archive and are not deleted.
- The planned cutover initializes an empty Postgres authority, uses fresh Qdrant collections, and rebuilds derived graph/vector state only from new Postgres records.
- FalkorDB remains a later rebuildable graph projection after Postgres authority, restart, backup, and projection-operation checks are complete.

### ✅ [2026-09-14] Atomic PostgreSQL repository factory

- The repository factory now constructs PostgreSQL sessions, memories, and evidence together or aborts startup without a SQLite fallback.
- Added `POSTGRES_SCHEMA` for isolated tests and `GRAPH_DB_PATH` so Postgres-derived graph state never writes into the archived SQLite memory database.

### ✅ [2026-09-14] Fresh Postgres backend E2E foundation

- Added an opt-in, isolated-schema authority test that constructs the atomic factory, persists sessions/messages, evidence/ingestion status, and memories, then recreates the factory to verify Postgres-backed restart persistence.

### ✅ [2026-09-14] Optional Postgres development foundation

- Added an opt-in Docker Compose Postgres 17 service with a persistent volume and health check, plus local Postgres connection configuration and the optional Psycopg driver dependency.
- Added transactional Postgres logical migrations for sessions/messages and projects/memberships, mirroring the first authoritative SQLite schema versions without enabling Postgres as the live application backend.
- SQLite remains the supported default while the remaining Postgres repositories are implemented and contract-tested.

### ✅ [2026-09-14] PostgreSQL session repository (opt-in)
- Added a PostgreSQL session/project/membership/message repository that preserves the SQLite contract, including owner isolation, attachment metadata, trace IDs, stable project IDs, and ordered turns.
- Session turn allocation locks the parent session row to prevent concurrent writers from assigning the same turn index.
- Added opt-in, isolated-schema integration coverage for a local Podman PostgreSQL service. The application factory intentionally remains SQLite-only until every authoritative repository has moved.

### ✅ [2026-09-14] PostgreSQL memory repository (opt-in)
- Added versioned PostgreSQL memory lifecycle, audit, conflict, extraction, access-history, relationship, and projection-outbox schema.
- Added atomic lifecycle writes, event-domain advisory locks, conflict resolution locks, idempotent expiry transitions, and `FOR UPDATE SKIP LOCKED` projection claims with stale-worker lease recovery.
- Added isolated-schema Podman integration coverage. The production repository factory remains SQLite-only pending the evidence and graph migration slices.

### ✅ [2026-09-14] PostgreSQL evidence repository (opt-in)
- Added immutable asset metadata, owner/project bindings, durable ingestion runs, immutable evidence segments, and Qdrant projection-outbox schema.
- Evidence upserts are idempotent only when provenance is identical; a reused evidence ID with changed content/provenance fails instead of overwriting source history.
- Added isolated-schema Podman tests for restart-safe ingestion status, owner/project evidence access, immutability, and outbox claims. SQLite remains the live default pending the explicit full-authority cutover.

### ✅ [2026-09-14] Cutover prerequisite: durable ingestion status
- Document ingestion now records processing/completed/failed runs through the evidence repository, allowing a status lookup to survive an application restart once the selected repository is durable.
- Added a read-only PostgreSQL preflight command for connectivity and required schema-version checks.

### ✅ [2026-09-14] Repository contracts and backend-selection seam

- Added backend-neutral contracts for session/project, memory, evidence, and graph repositories plus a single repository container/factory.
- API routers and application startup now depend on the container rather than importing concrete SQLite stores directly.
- Added explicit SQLite/Postgres configuration. SQLite remains the default; a Postgres selection requires a URL and fails clearly until its repositories are implemented.
- Added factory safety coverage for default selection and Postgres misconfiguration.

### ✅ [2026-09-14] Versioned SQLite migration foundation

- Added a shared transactional migration runner and per-component `schema_migrations` ledger for session/project, memory, graph, and evidence repositories.
- Existing self-healing schemas are recorded as explicit compatibility baselines, avoiding a destructive rewrite of deployed SQLite files; future changes now have ordered migration versions.
- Added migration-status operations tooling and deterministic coverage for ordering, idempotency, and rollback on failure.

### ✅ [2026-09-14] Projection-sync reliability patch

- Startup now performs one bounded synchronization of existing pending graph/Qdrant projection jobs after authoritative project-memory backfill.
- Added `scripts/sync_pending_projections.py` for safe manual recovery; it processes existing jobs only and can explicitly requeue failed jobs. Full rebuild remains a separate deliberate script.

### ✅ [2026-09-14] Project-ID propagation and legacy-memory migration

- Added an owner-scoped, idempotent memory backfill that maps legacy project names only through the canonical session-project mapping, audits each change, and queues projection refreshes. Unmatched records remain safely name-scoped.
- Made new event identity/comparison, memory retrieval, Qdrant payloads, graph records, memory APIs, browser filters, and conflict polling project-ID-aware, with legacy name fallback during transition.
- Project-ID/name API inputs are validated against membership and rejected if mismatched. Graph identity now uses project IDs, preventing same-name scopes from colliding after project naming evolves.
- Added nullable project ownership metadata to evidence assets without guessing historical asset assignment.
- Added deterministic tests for authoritative backfill audit behavior and graph project-ID projections.

### ✅ [2026-09-14] Normalized project membership foundation

- Added durable `projects` and `project_memberships` tables, with stable project IDs, normalized per-owner names, and owner roles ready for later multi-user authorization.
- Sessions now retain both a stable `project_id` and compatibility/display `project_scope`; legacy named sessions are idempotently backfilled on repository startup.
- Added project list/create APIs and project-ID-aware session create/update responses while preserving existing name-based clients.
- New extracted memories and Qdrant projections carry project IDs. Retrieval queries the stable ID plus the legacy display-name projection during transition, so older project memories remain discoverable.
- Added migration, name-normalization, restart, and cross-owner membership-isolation tests.

### ✅ [2026-09-13] Durable attachment metadata in chat history

- Replaced the loose chat-attachment dictionaries with a validated API contract: attachment ID, filename, MIME type, byte size, source mode, and optional document/evidence references.
- The frontend now sends attachment metadata with the user turn, retaining an indexed document ID when the file enters the knowledge base; inline-only attachments deliberately retain no durable binary or document reference.
- User messages persist this metadata in the existing SQLite `attachments_json` column, and restored conversations render non-interactive attachment chips with their source mode.
- Added validation, API-contract serialization, and restart-persistence coverage. Raw file bytes remain in the existing upload/ingestion subsystem rather than being copied into conversation storage.

### ✅ [2026-09-13] Memory browser and project-memory controls

- Added a project-aware Memories drawer with scope/status filters, payload inspection, lifecycle metadata, audit timeline, and retrieval/injection usage counts.
- Added owner-scoped memory detail, audit, and access-event APIs, plus `status=all` and bounded list results for browser use.
- Added soft-forget behavior: deleted memories remain auditable but are excluded from default listing and retrieval, with graph/vector cleanup queued through the existing projection outbox.
- Added browser controls for confirm, reject, expire, project promotion, edit, and forget; expired, superseded, and forgotten records are inspection-only.
- Added deterministic coverage for browser owner isolation, audit/access inspection, soft-forget idempotency, and projection cleanup scheduling.

### ✅ [2026-09-13] Expiry sweep lifecycle hardening

- Added an idempotent repository sweep that formally transitions due active memories to `expired`, preserves their original validity deadline, and appends an `expired_by_sweep` audit event.
- Expiry changes enqueue Qdrant and graph projection updates in the same transaction; derived stores are synchronized immediately after the sweep.
- The server runs one sweep at startup and repeats it on a configurable interval (`MEMORY_EXPIRY_SWEEP_INTERVAL_SECONDS`, default five minutes).
- Added `POST /api/memories/expiry-sweep` for scoped manual verification and operations.
- Added deterministic coverage for expiry, audit/projection updates, repeat safety, future deadlines, and owner isolation.

### ✅ [2026-09-11] Event deduplication and conflict review

- Added repository-enforced deterministic identity keys for event memories, scoped by owner, session/project, event type, entities, location, and temporal marker.
- Canonicalized event types through a backend taxonomy (for example, `shipment arrival` and `shipment_arrival` both become `shipment_arrival`) while retaining the original extracted label for provenance.
- Event comparison now uses canonical type plus stable identifier overlap (for example, `AC-42`), so incidental extracted entities do not suppress a legitimate conflict.
- Repeated event captures now retain one authoritative memory and append a `duplicate_detected` audit event instead of creating duplicate shipments.
- Material event updates create a reviewable candidate plus an auditable conflict record; they never overwrite the existing event automatically.
- Added conflict inspection and resolution APIs. Resolution can retain the existing event, supersede it, or expire it while activating the reviewed incoming event and synchronizing projections.
- Added deterministic coverage for duplicate events, temporal conflicts, conflict resolution, project isolation, and expiration followed by a new event.

### ✅ [2026-09-11] Inline conflict resolution

- Added a chat-level review card for open event conflicts in the active project, with clear existing-versus-updated values.
- Users can accept the update, keep the existing memory, or defer review without using a terminal command.
- The card polls briefly after streaming completes because event extraction and conflict creation run asynchronously after the assistant response.

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
