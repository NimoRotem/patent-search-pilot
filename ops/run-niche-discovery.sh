#!/bin/bash
set -euo pipefail

FACTORY_ROOT="${NICHE_FACTORY_ROOT:-/home/nimrod_rotem/niche-factory}"
NICHE_DB_HOST="${NICHE_DB_HOST:-10.128.0.4}"
NICHE_DB_PASSWORD_FILE="${NICHE_DB_PASSWORD_FILE:-${FACTORY_ROOT}/.niche-db-password}"
NICHE_SOURCE_ENV_FILE="${NICHE_SOURCE_ENV_FILE:-${FACTORY_ROOT}/.source.env}"

test -r "${NICHE_SOURCE_ENV_FILE}"
set -a
. "${NICHE_SOURCE_ENV_FILE}"
set +a

test -r "${NICHE_DB_PASSWORD_FILE}"
NICHE_DB_PASSWORD="$(tr -d '\r\n' < "${NICHE_DB_PASSWORD_FILE}")"
test -n "${NICHE_DB_PASSWORD}"

export NICHE_DATABASE_URL="host=${NICHE_DB_HOST} port=5432 dbname=niche_full_v1 user=niche_factory password=${NICHE_DB_PASSWORD}"
export NICHE_SOURCE_DATABASE_URL="host=${PGHOST} port=${PGPORT} dbname=${PGDATABASE} user=${PGUSER} password=${PGPASSWORD} options='-cdefault_transaction_read_only=on -cstatement_timeout=60000'"
export NICHE_EXPECTED_DATABASE="niche_full_v1"
export NICHE_DATABASE_FINGERPRINT="niche-full-v1-20260822"
export NICHE_DISCOVERY_ID_WINDOW="${NICHE_DISCOVERY_ID_WINDOW:-50000}"
export NICHE_DISCOVERY_GRAPH_LIMIT="${NICHE_DISCOVERY_GRAPH_LIMIT:-5000}"

case "${1:-}" in
  lower)
    ID_START=0
    ID_END=3218196
    ;;
  upper)
    ID_START=3218196
    ID_END=6436391
    ;;
  tail)
    ID_START="${NICHE_DISCOVERY_TAIL_START:-6436391}"
    ID_END=0
    if [[ ! "${ID_START}" =~ ^[0-9]+$ ]]; then
      echo "invalid discovery tail start" >&2
      exit 64
    fi
    ;;
  [0-9]*-[0-9]*)
    if [[ ! "${1}" =~ ^[0-9]+-[0-9]+$ ]]; then
      echo "invalid discovery range" >&2
      exit 64
    fi
    ID_START="${1%%-*}"
    ID_END="${1#*-}"
    if (( ID_END <= ID_START )); then
      echo "discovery range end must exceed its start" >&2
      exit 64
    fi
    ;;
  *)
    echo "unknown discovery range" >&2
    exit 64
    ;;
esac

cd "${FACTORY_ROOT}"
if [ "${1:-}" = "tail" ]; then
  while true; do
    .venv/bin/python -u -m src.corpus.niche.discover \
      --id-start "${ID_START}" \
      --id-end 0 \
      --batch-size "${NICHE_DISCOVERY_BATCH_SIZE:-1000}" \
      --max-batches 0 \
      --db-read-delay "${NICHE_DISCOVERY_DB_READ_DELAY:-1.0}"
    if [ "${NICHE_DISCOVERY_TAIL_ONCE:-0}" = "1" ]; then
      exit 0
    fi
    sleep "${NICHE_DISCOVERY_TAIL_INTERVAL_SECONDS:-300}"
  done
fi
exec .venv/bin/python -u -m src.corpus.niche.discover \
  --id-start "${ID_START}" \
  --id-end "${ID_END}" \
  --batch-size "${NICHE_DISCOVERY_BATCH_SIZE:-1000}" \
  --max-batches 0 \
  --db-read-delay "${NICHE_DISCOVERY_DB_READ_DELAY:-1.0}"
