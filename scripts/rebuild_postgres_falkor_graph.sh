#!/usr/bin/env zsh
# Rebuild the derived Falkor graph from authoritative Postgres records.
# Run while the app and other projection workers are stopped.

set -euo pipefail

source "${0:A:h}/postgres_falkor_env.sh"
exec "${PYTHON_BIN:-python}" scripts/rebuild_graph_projection.py "$@"
