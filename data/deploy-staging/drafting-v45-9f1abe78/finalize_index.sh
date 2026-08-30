#!/usr/bin/env bash
# Wait for the search-critical (non-paragraph) embedding tier, then build the HNSW index.
cd "$(dirname "$0")"
PY=./.venv/bin/python
q() { PGPASSWORD=patents_pilot_local psql -h 127.0.0.1 -p 5433 -U patents -d patents -tAc "$1"; }

echo "$(date +%T) waiting for non-paragraph embedding tier..."
while true; do
  N=$(q "SELECT count(*) FROM chunks WHERE embedding IS NULL AND kind NOT IN ('paragraph','figure_caption')")
  echo "$(date +%T) non-paragraph pending: $N"
  if [ "$N" -le 300 ] 2>/dev/null; then break; fi
  sleep 30
done

echo "$(date +%T) tier embedded; stopping embedder to free CPU for the HNSW build"
pkill -f "embed.py run" 2>/dev/null || true
sleep 4

EMB=$(q "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
echo "$(date +%T) building HNSW over $EMB embedded vectors (parallel)..."
PGPASSWORD=patents_pilot_local psql -h 127.0.0.1 -p 5433 -U patents -d patents -v ON_ERROR_STOP=1 <<'SQL'
SET max_parallel_maintenance_workers = 3;
SET maintenance_work_mem = '1500MB';
CREATE INDEX IF NOT EXISTS ix_chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
ANALYZE chunks;
SQL

echo "$(date +%T) HNSW build complete"
touch data/INDEX_DONE
