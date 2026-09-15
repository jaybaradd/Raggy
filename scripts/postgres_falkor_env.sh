#!/usr/bin/env zsh
# Shared Postgres + FalkorDB development profile.
#
# Source this file from a launcher or maintenance wrapper. It intentionally
# selects Falkor only for the Postgres profile; plain `python main.py` retains
# the SQLite graph default from .env.

set -euo pipefail

script_path="${(%):-%N}"
cd "${script_path:A:h}/.."

if [[ ! -f .env ]]; then
  print -u2 "Missing .env. Copy .env.example to .env and set POSTGRES_PASSWORD."
  return 1
fi

set -a
source .env
set +a

: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}"

# Podman's host port is 5433 by default. RAGGY_* variables deliberately scope
# overrides to this profile rather than changing the ordinary SQLite runtime.
export POSTGRES_PORT="${RAGGY_POSTGRES_PORT:-5433}"
export POSTGRES_DATABASE_URL="${RAGGY_POSTGRES_DATABASE_URL:-postgresql://raggy:${POSTGRES_PASSWORD}@localhost:${POSTGRES_PORT}/raggy}"
export POSTGRES_SCHEMA="${POSTGRES_SCHEMA:-public}"
export AUTHORITATIVE_DB_BACKEND=postgres

export QDRANT_URL="${POSTGRES_QDRANT_URL:-./qdrant_postgres_data}"
export QDRANT_COLLECTION="${POSTGRES_QDRANT_COLLECTION:-raggy_postgres_evidence}"
export MEMORY_COLLECTION="${POSTGRES_MEMORY_COLLECTION:-raggy_postgres_memories}"
export UPLOAD_DIR="${POSTGRES_UPLOAD_DIR:-./uploaded_files_postgres}"
# Used only by the explicit SQLite-graph rollback path; it must not share the
# archived local graph file configured for ordinary SQLite development.
export GRAPH_DB_PATH="${POSTGRES_GRAPH_DB_PATH:-./raggy_graph_postgres.sqlite3}"

# Phase G cutover: the Postgres development profile always selects Falkor.
# Use RAGGY_GRAPH_PROJECTION_BACKEND=sqlite only as an explicit rollback.
export GRAPH_PROJECTION_BACKEND="${RAGGY_GRAPH_PROJECTION_BACKEND:-falkor}"
if [[ "${GRAPH_PROJECTION_BACKEND}" != "falkor" && "${GRAPH_PROJECTION_BACKEND}" != "sqlite" ]]; then
  print -u2 "RAGGY_GRAPH_PROJECTION_BACKEND must be 'falkor' or 'sqlite'."
  return 1
fi
export FALKORDB_URL="${RAGGY_FALKORDB_URL:-${FALKORDB_URL:-redis://127.0.0.1:6380}}"
export FALKORDB_GRAPH_NAME="${RAGGY_FALKORDB_GRAPH_NAME:-${FALKORDB_GRAPH_NAME:-raggy_memory_projection_v1}}"

if [[ "${GRAPH_PROJECTION_BACKEND}" == "falkor" ]]; then
  : "${FALKORDB_URL:?Set FALKORDB_URL or RAGGY_FALKORDB_URL}"
  : "${FALKORDB_GRAPH_NAME:?Set FALKORDB_GRAPH_NAME or RAGGY_FALKORDB_GRAPH_NAME}"
fi
