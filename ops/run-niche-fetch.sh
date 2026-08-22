#!/bin/bash
set -euo pipefail

FACTORY_ROOT="${NICHE_FACTORY_ROOT:-/home/nimrod_rotem/niche-factory}"
NICHE_DB_HOST="${NICHE_DB_HOST:-10.128.0.4}"
NICHE_DB_PASSWORD_FILE="${NICHE_DB_PASSWORD_FILE:-${FACTORY_ROOT}/.niche-db-password}"

set -a
. "${FACTORY_ROOT}/.env"
set +a

test -r "${NICHE_DB_PASSWORD_FILE}"
NICHE_DB_PASSWORD="$(tr -d '\r\n' < "${NICHE_DB_PASSWORD_FILE}")"
test -n "${NICHE_DB_PASSWORD}"

export NICHE_FACTORY_ISOLATED=1
export NICHE_PARSE_DATABASE_URL="host=${NICHE_DB_HOST} port=5432 dbname=niche_full_v1 user=niche_factory password=${NICHE_DB_PASSWORD}"
export NICHE_EXPECTED_DATABASE="niche_full_v1"
export NICHE_DATABASE_FINGERPRINT="niche-full-v1-20260822"
export FULLTEXT_FIRECRAWL_BUDGET="${FULLTEXT_FIRECRAWL_BUDGET:-50000}"

cd "${FACTORY_ROOT}"
exec .venv/bin/python -u ops/fulltext_acquire.py run --shard "${1:?shard}" --of "${2:?shard count}"
