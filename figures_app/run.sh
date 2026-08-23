#!/bin/bash
# Serve the patent figure compiler on :8637, behind nginx at /figures.
#
# One gunicorn worker with a generous thread pool, not several workers: a compilation runs on a
# background thread and holds its job state in that process, so a second worker would answer
# status polls for a job it has never heard of. Concurrency is bounded inside the app by
# PFC_MAX_CONCURRENT instead.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ -f "$HERE/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$HERE/.env"
  set +a
fi

export PILOT_ROOT="${PILOT_ROOT:-$HOME/patent-search-pilot}"
export PFC_DATA_DIR="${PFC_DATA_DIR:-$HOME/patent-figures-data}"
export PATENTS_LOGIN_URL="${PATENTS_LOGIN_URL:-https://nimo.iptorch.com/login}"
export PYTHONUNBUFFERED=1

mkdir -p "$PFC_DATA_DIR/jobs"

exec "$HERE/.venv/bin/gunicorn" \
  --bind 127.0.0.1:"${PORT:-8637}" \
  --workers 1 --threads 8 \
  --timeout 1800 --graceful-timeout 60 --keep-alive 75 \
  --access-logfile - --error-logfile - \
  app:app
