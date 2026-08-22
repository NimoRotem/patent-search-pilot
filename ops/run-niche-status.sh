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

cd "${FACTORY_ROOT}"
if [ "${1:-}" = "once" ]; then
  exec .venv/bin/python -u -m src.corpus.niche.status \
    --artifacts-dir "${FACTORY_ROOT}/artifacts"
fi
while true; do
  .venv/bin/python -u -m src.corpus.niche.status \
    --artifacts-dir "${FACTORY_ROOT}/artifacts"
  sleep "${NICHE_STATUS_INTERVAL_SECONDS:-300}"
done
