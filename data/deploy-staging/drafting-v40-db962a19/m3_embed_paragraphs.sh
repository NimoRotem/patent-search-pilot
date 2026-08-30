#!/usr/bin/env bash
# Milestone 3 §3: embed the remaining ~571k description-paragraph + figure-caption chunks,
# then rebuild HNSW fresh (best graph quality for the recall milestone).
set -uo pipefail
ROOT=/home/nimrod_rotem/patent-search-pilot
PY=$ROOT/.venv/bin/python
PSQL(){ PGPASSWORD=patents_pilot_local psql -h 127.0.0.1 -p 5433 -U patents -d patents "$@"; }
log(){ echo "$(date +%T) $*"; }

rm -f $ROOT/data/M3_INDEX_DONE
N0=$(PSQL -tAc "SELECT count(*) FROM chunks WHERE embedding IS NULL")
log "start: $N0 chunks unembedded"

# 1) ensure HNSW dropped so bulk UPDATEs don't pay per-row index maintenance
log "dropping HNSW index for bulk embed…"
PSQL -c "DROP INDEX IF EXISTS ix_chunks_hnsw;" 2>&1 | tail -1

# 2) embed all remaining NULL (resumable: WHERE embedding IS NULL). Run from src/ with abs python.
log "embedding remaining chunks (Vertex gemini-embedding-001 768d)…"
cd $ROOT/src && "$PY" embed.py run 2>&1 | grep -viE 'FutureWarning|warnings.warn' | tail -3
NLEFT=$(PSQL -tAc "SELECT count(*) FROM chunks WHERE embedding IS NULL")
NEMB=$(PSQL -tAc "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
log "embed done: $NEMB embedded, $NLEFT still NULL"

# 3) rebuild HNSW fresh, single-threaded in-RAM (avoids /dev/shm; 6GB fits the ~5.7GB graph)
log "rebuilding HNSW over $NEMB vectors (single-threaded, in-RAM)…"
PSQL -v ON_ERROR_STOP=1 <<'SQL'
SET max_parallel_maintenance_workers = 0;
SET maintenance_work_mem = '6GB';
CREATE INDEX ix_chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
ANALYZE chunks;
SQL
rc=$?
IDX=$(PSQL -tAc "SELECT indexname FROM pg_indexes WHERE indexname='ix_chunks_hnsw'")
SZ=$(PSQL -tAc "SELECT pg_size_pretty(pg_relation_size('ix_chunks_hnsw'))" 2>/dev/null)
log "rebuild rc=$rc index=$IDX size=$SZ"
[ "$IDX" = "ix_chunks_hnsw" ] && touch $ROOT/data/M3_INDEX_DONE && log "M3_INDEX_DONE"
