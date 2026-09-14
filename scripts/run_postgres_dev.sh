#!/usr/bin/env zsh
# Start Raggy with a fresh, local PostgreSQL authority and separate projections.
#
# Credentials remain in .env; this script deliberately contains no secrets.
set -euo pipefail

cd "${0:A:h}/.."

if [[ ! -f .env ]]; then
  print -u2 "Missing .env. Copy .env.example to .env and set POSTGRES_PASSWORD."
  exit 1
fi

set -a
source .env
set +a

: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}"
# The launcher defaults to Podman's host port (5433), even if an archived .env
# still contains an older 5432 connection string. Custom remote/local targets
# can opt in explicitly with these RAGGY_POSTGRES_* overrides.
export POSTGRES_PORT="${RAGGY_POSTGRES_PORT:-5433}"
export POSTGRES_DATABASE_URL="${RAGGY_POSTGRES_DATABASE_URL:-postgresql://raggy:${POSTGRES_PASSWORD}@localhost:${POSTGRES_PORT}/raggy}"
export POSTGRES_SCHEMA="${POSTGRES_SCHEMA:-public}"
export AUTHORITATIVE_DB_BACKEND=postgres

# These derived stores intentionally do not share the archived SQLite/Qdrant
# paths. Override POSTGRES_* values in .env if another location is needed.
export QDRANT_URL="${POSTGRES_QDRANT_URL:-./qdrant_postgres_data}"
export QDRANT_COLLECTION="${POSTGRES_QDRANT_COLLECTION:-raggy_postgres_evidence}"
export MEMORY_COLLECTION="${POSTGRES_MEMORY_COLLECTION:-raggy_postgres_memories}"
export UPLOAD_DIR="${POSTGRES_UPLOAD_DIR:-./uploaded_files_postgres}"
export GRAPH_DB_PATH="${POSTGRES_GRAPH_DB_PATH:-./raggy_graph_postgres.sqlite3}"

exec "${PYTHON_BIN:-python}" main.py
