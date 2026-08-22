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
export NICHE_CORPUS_RELEASE="${NICHE_CORPUS_RELEASE:-niche_full_v1}"
export GEMINI_EMBED_MODEL="${GEMINI_EMBED_MODEL:-gemini-embedding-001}"
export GEMINI_EMBED_DIMENSION="${GEMINI_EMBED_DIMENSION:-768}"
export GEMINI_EMBED_TASK_TYPE="${GEMINI_EMBED_TASK_TYPE:-RETRIEVAL_DOCUMENT}"
export GEMINI_EMBED_BUDGET_KEY="${GEMINI_EMBED_BUDGET_KEY:-niche_full_v1}"
export MAX_GEMINI_EMBED_USD_TOTAL="${MAX_GEMINI_EMBED_USD_TOTAL:-400}"
export GEMINI_EMBED_PRICE_USD_PER_MTOK="${GEMINI_EMBED_PRICE_USD_PER_MTOK:-0.12}"
export GEMINI_BATCH_BUCKET="${GEMINI_BATCH_BUCKET:-nimo-patents-v3}"
export GCP_PROJECT="${GCP_PROJECT:-nimo-gpt}"

cd "${FACTORY_ROOT}"
exec .venv/bin/python -u -m src.corpus.niche.parse \
  --stream \
  --workers "${PARSE_WORKERS:-4}" \
  --lease-seconds "${PARSE_LEASE_SECONDS:-600}" \
  --heartbeat-seconds "${PARSE_HEARTBEAT_SECONDS:-30}" \
  --poll-seconds "${PARSE_POLL_SECONDS:-5}" \
  --object-root "${NICHE_OBJECT_ROOT:-gs://nimo-patents-v3/niche_full_v1}"
