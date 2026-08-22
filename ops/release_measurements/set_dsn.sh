#!/bin/bash
set -e
PW="relbuild_v3_$(python3 -c 'import secrets;print(secrets.token_hex(12))')"
sudo -u postgres psql -p 5544 -tAc "ALTER ROLE relbuild WITH PASSWORD '$PW' LOGIN"
DSN="host=127.0.0.1 port=5544 dbname=relbuild user=relbuild password=$PW"
ENV=/home/nimrod_rotem/patent-search-pilot/.env
if grep -q '^CORPUS_RELEASE_DSN=' "$ENV"; then
  echo "ALREADY PRESENT, not touching .env"
else
  cp "$ENV" "$ENV.bak-corpusdsn-$(date +%Y%m%d%H%M%S)"
  printf '\n# The offline corpus release builder database (workstream O, v3).\n# PostgreSQL 17 cluster "relbuild" on this VM, 127.0.0.1:5544, pgvector 0.8.6.\n# Never production: HNSW builds happen here. See docs/corpus_release.md.\nCORPUS_RELEASE_DSN=%s\n' "$DSN" >> "$ENV"
fi
echo "=== written ==="
grep -n 'CORPUS_RELEASE_DSN' "$ENV"
echo "=== connect test ==="
PGPASSWORD="$PW" psql "host=127.0.0.1 port=5544 dbname=relbuild user=relbuild" -tAc "select current_user||' @ '||current_database()||' pgvector='||(select extversion from pg_extension where extname='vector')"
