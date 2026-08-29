#!/usr/bin/env bash
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
export FM_DATA_DIR="${FM_DATA_DIR:-$HOME/figuresmaker-data}"
export PATENTS_LOGIN_URL="${PATENTS_LOGIN_URL:-https://nimo.iptorch.com/login}"
export PYTHONUNBUFFERED=1

mkdir -p "$FM_DATA_DIR/jobs"

# One worker, threads inside it. Jobs are held in this process's memory while they run and their
# artefacts go to disk as they go, so a second worker would only fragment the job store.
exec "$HERE/.venv/bin/gunicorn" \
  --bind 127.0.0.1:"${PORT:-8639}" \
  --workers 1 --threads 8 \
  --timeout 1800 --graceful-timeout 60 --keep-alive 75 \
  --access-logfile - --error-logfile - \
  app:app
