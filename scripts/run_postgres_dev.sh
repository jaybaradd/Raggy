#!/usr/bin/env zsh
# Start Raggy with PostgreSQL authority and the FalkorDB graph projection.
#
# Credentials remain in .env; this script deliberately contains no secrets.
set -euo pipefail

source "${0:A:h}/postgres_falkor_env.sh"

exec "${PYTHON_BIN:-python}" main.py
