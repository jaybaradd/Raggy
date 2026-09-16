# Raggy

Raggy is a local-first multimodal RAG chat application with durable chats,
project-scoped memory, document ingestion, hybrid retrieval, and a rebuildable
memory graph.

It is designed around one safety rule: **PostgreSQL is authoritative.** Qdrant
and FalkorDB are derived indexes that can be rebuilt without losing chats,
memories, evidence, or audit history.

## What it does

- Chats persist across application restarts.
- Chats can belong to named projects; projects isolate sessions, memories, and
  retrieval context.
- Documents and YouTube sources can be ingested, indexed, and cited in chat.
- High-confidence operational memories can be captured from conversation.
- Corrections become reviewable memory updates instead of silently overwriting
  old facts.
- Memory records support confirmation, rejection, expiry, deletion,
  supersession, audit history, and inspection in the UI.
- Evidence and memory retrieval use Qdrant hybrid dense/BM25 search.
- Durable memory relationships are projected to FalkorDB for graph inspection
  and bounded one-hop related-memory discovery.

## Architecture

```text
Browser
  │  http://localhost:8000
  ▼
FastAPI app + static frontend
  ├── PostgreSQL  authoritative sessions, projects, messages, evidence,
  │               memories, lifecycle, audit history, and projection outbox
  ├── Qdrant      derived semantic indexes for evidence and active memories
  └── FalkorDB    derived graph of durable memory records and relationships
```

### Storage responsibilities

| Store | Responsibility | Authoritative? |
| --- | --- | --- |
| PostgreSQL | chats, project membership, messages, attachments metadata, evidence metadata, memories, conflicts, audits, outbox jobs | Yes |
| Object/upload storage | original uploaded files | Yes for raw files |
| Qdrant | semantic evidence and active-memory retrieval | No; rebuildable |
| FalkorDB | memory nodes and durable relationship edges | No; rebuildable |
| SQLite | local compatibility mode and archived development data | No for the Postgres runtime |

## Memory lifecycle

Memory capture is intentionally conservative.

1. A high-confidence user-stated operational event is captured in the current
   project or session scope.
2. The next turn uses identifier lookup, semantic retrieval, and optional
   one-hop graph expansion to assemble bounded candidate context.
3. A constrained planner selects only candidate-set-bounded memories for the
   answer prompt.
4. A reconciler classifies a new event as `new`, `duplicate`, `update`,
   `related`, or `uncertain`.
5. Material updates remain reviewable candidates. Accepting one supersedes the
   old memory and creates durable `SUPERSEDED_BY` lineage.

`RELATED` links can be used for one-hop graph candidate expansion.
`SUPERSEDED_BY` is historical lineage only and is never traversed into answer
context. Open conflicts do not invent graph edges.

## Run with Podman Compose

The container stack is the simplest way to run the full application:

```bash
cp .env.example .env
# Set GEMINI_API_KEY and choose a non-default POSTGRES_PASSWORD.
podman compose up -d --build
```

Open <http://localhost:8000>.

Useful endpoints:

| Service | Host address |
| --- | --- |
| Raggy UI and API | <http://localhost:8000> |
| Falkor Browser | <http://localhost:3001> |
| Qdrant, for debugging | <http://localhost:6333> |
| PostgreSQL, for debugging | `localhost:5433` |
| Falkor Redis, for debugging | `localhost:6380` |

Inside the Compose network, the application uses service DNS names rather than
host `localhost` addresses:

```text
postgres:5432
qdrant:6333
falkordb:6379
```

The app container waits for those services, then runs the normal `main.py`
entrypoint.

### First startup

The first start downloads the configured local embedding model and BM25
resources. Model caches are stored in the `raggy-model-cache` named volume, so
later starts reuse them. Postgres, Qdrant, FalkorDB, and uploads also use named
volumes.

```bash
podman compose ps
podman compose logs -f raggy-app
curl http://localhost:8000/api/health
```

`podman compose down` preserves data. The following command is destructive and
removes all container volumes:

```bash
podman compose down -v
```

See [CONTAINERS.md](CONTAINERS.md) for container-specific recovery details.

## Run on the host during development

The original host workflow remains available. Start dependencies first:

```bash
podman compose up -d postgres qdrant falkordb
./scripts/run_postgres_dev.sh
```

The launcher explicitly selects PostgreSQL authority and FalkorDB graph
projection. Plain `python main.py` remains the SQLite compatibility mode.

The host launcher expects Postgres on `localhost:5433`; the containerized app
uses `postgres:5432` internally.

## Configuration

Copy `.env.example` to `.env`. At minimum configure:

```dotenv
GEMINI_API_KEY=your_key_here
POSTGRES_PASSWORD=choose_a_local_password
GEMINI_CHAT_MODEL=gemini-3.1-flash-lite
LOCAL_EMBED_MODEL=cnmoro/snowflake-arctic-embed-m-v2.0-cpu
LOCAL_EMBED_DIM=256
```

The Compose application service explicitly supplies its Postgres, Qdrant, and
FalkorDB addresses. Do not put container service URLs in the host-only
Postgres launcher configuration.

To enable optional one-hop related-memory expansion:

```dotenv
GRAPH_MEMORY_EXPANSION_ENABLED=true
GRAPH_MEMORY_EXPANSION_LIMIT=4
```

## Operations and recovery

Synchronize existing memory projection jobs:

```bash
python scripts/sync_pending_projections.py
```

Rebuild only the derived graph from Postgres authority. Stop the app first:

```bash
podman compose stop raggy-app
./scripts/rebuild_postgres_falkor_graph.sh
podman compose start raggy-app
```

The graph rebuild never changes Postgres or Qdrant.

## Testing

Focused offline tests:

```bash
GRAPH_PROJECTION_BACKEND=sqlite \
python -m unittest tests.test_repository_factory tests.test_falkor_graph_store -v
```

Real Postgres/Falkor integration coverage is opt-in:

```bash
RUN_FALKOR_INTEGRATION_TESTS=1 \
GRAPH_PROJECTION_BACKEND=falkor \
python -m unittest tests.test_postgres_falkor_graph -v
```

The integration suite creates a random Postgres schema and Falkor graph name
per run, then removes only those test resources.

## Current status

The Postgres/Falkor development cutover is complete:

- Postgres repositories exist for sessions, messages, memories, evidence,
  lifecycle, conflicts, audit records, and projection jobs.
- The Falkor adapter projects active memory nodes plus `RELATED`, legacy
  `CONTRADICTION`, and `SUPERSEDED_BY` edges.
- Projection, lifecycle, isolation, rebuild, and adapter-restart behavior have
  fake-client and real Podman integration coverage.
- The Postgres launcher selects FalkorDB by default, while SQLite remains an
  explicit compatibility and rollback path.

## Roadmap

The detailed plan is in [Roadmap.md](Roadmap.md) and completed slices are
tracked in [progress.md](progress.md).

Near-term maintenance:

- Polish correction UI copy and ensure responses describe unaccepted updates
  as proposed corrections rather than completed changes.
- Remove or modernize remaining archive-only operational scripts.
- Complete a clean full-container build and restart acceptance run on the
  target Podman machine.

Future product work:

- Graph/vector fused retrieval with explicit ranking and provenance.
- Carefully bounded multi-hop graph planning.
- A richer canonical entity graph only after robust entity resolution and a
  demonstrated product need.
- Relationship-management UI for deliberate `RELATED` links.
- Multi-provider reasoning, query decomposition, and long-context handling
  described in Phase 3 of the roadmap.

## Design principles

- Relational data is authoritative; derived indexes are disposable.
- Scope and lifecycle checks apply before vector and graph retrieval.
- Corrections are reviewable; facts are never silently overwritten.
- Graph edges come from durable relationships, not unverified LLM guesses.
- New capabilities preserve restart durability and project isolation.
