# Container development

Raggy's default container stack runs the application, PostgreSQL, Qdrant, and
FalkorDB together. PostgreSQL is authoritative; Qdrant and FalkorDB are
rebuildable projections.

## Start

```bash
cp .env.example .env
# Add GEMINI_API_KEY to .env and choose a non-default POSTGRES_PASSWORD.
podman compose up -d --build
```

Open <http://localhost:8000>. Falkor Browser is available at
<http://localhost:3001>.

The first image build downloads large ML and document-parsing wheels. The
Dockerfile uses a 180-second pip read timeout and retries for slower networks.
If a build fails with a PyPI read timeout, run the same command again; it is a
network transfer failure, not a database reset or application-data failure.

The browser talks to the app at `localhost:8000`. Inside the Compose network,
the app uses `postgres:5432`, `qdrant:6333`, and `falkordb:6379`; those values
are set explicitly in `compose.yaml` and do not use host `localhost` addresses.

## First startup and model cache

The first app startup downloads the configured local embedding model and BM25
resources. They are stored in the `raggy-model-cache` named volume, along with
other standard model caches. Later starts reuse that volume.

Original uploads, Postgres data, Qdrant data, and FalkorDB data each have their
own named volume. `podman compose down` preserves them. Use the destructive
command below only when intentionally resetting all local container data:

```bash
podman compose down -v
```

## Verify and recover

```bash
podman compose ps
curl http://localhost:8000/api/health
podman compose logs -f raggy-app
```

To rebuild only the derived graph, stop the app first and run the existing
host-side Postgres/Falkor rebuild command:

```bash
podman compose stop raggy-app
./scripts/rebuild_postgres_falkor_graph.sh
podman compose start raggy-app
```

The initial container image intentionally adds no extra Linux media or GUI
packages. Current chat, document, memory, Qdrant, and Falkor flows do not
require them. If video or YouTube transcription is exercised and reports a
missing system binary, add only that confirmed runtime dependency (for example
`ffmpeg`) to the image.
