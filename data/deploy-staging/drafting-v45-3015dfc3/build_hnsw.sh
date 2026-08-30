#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "$(date +%T) building HNSW single-threaded (no /dev/shm)..."
PGPASSWORD=patents_pilot_local psql -h 127.0.0.1 -p 5433 -U patents -d patents -v ON_ERROR_STOP=1 <<'SQL'
SET max_parallel_maintenance_workers = 0;
SET maintenance_work_mem = '5GB';
CREATE INDEX IF NOT EXISTS ix_chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
ANALYZE chunks;
SQL
rc=$?
echo "$(date +%T) psql rc=$rc"
[ $rc -eq 0 ] && touch data/INDEX_DONE && echo "$(date +%T) HNSW READY"
