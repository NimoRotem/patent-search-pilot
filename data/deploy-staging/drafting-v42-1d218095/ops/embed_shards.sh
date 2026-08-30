#!/usr/bin/env bash
# Drain the pending-embedding backlog with N parallel shard workers.
#
# WHY SHARD
# ---------
# embed.run() is I/O-bound on Vertex round-trips, not CPU-bound, so a single process with 14
# concurrent calls tops out around 68 chunks/sec against a live HNSW index — roughly 25 hours for
# a 6M-chunk backlog. shard=(k,n) partitions by `id % n`, so n processes cover disjoint rows with
# no coordination and no double work.
#
# WHY NOT THE FASTER PATTERN
# --------------------------
# The M3 bulk pattern (DROP INDEX -> embed at ~376/sec -> CREATE INDEX) is faster at the embed
# step, but the rebuild of ~11.5M vectors costs hours on its own and vector search is DOWN for the
# entire embed+rebuild window. Sharding reaches comparable wall-clock with the app serving
# throughout, which is the right trade for a live corpus.
#
# CONCURRENCY IS ACCOUNT-WIDE. The Vertex quota does not care how many processes we run: what
# matters is SHARDS x EMBED_WORKERS. 3 x 16 = 48 tripped 429 RESOURCE_EXHAUSTED historically, so
# the default here is 3 x 12 = 36. Raise only while watching for 429s in the shard logs.
set -uo pipefail
ROOT=/srv/patents/app
PY=$ROOT/.venv/bin/python
SHARDS="${SHARDS:-3}"
export EMBED_WORKERS="${EMBED_WORKERS:-12}"
cd "$ROOT/src" || exit 1

for k in $(seq 0 $((SHARDS-1))); do
  LOG="$ROOT/data/embed_shard_${k}.log"
  echo "$(date -Is) shard $k/$SHARDS starting (EMBED_WORKERS=$EMBED_WORKERS)" >> "$LOG"
  # Resumable by construction: the work queue IS `WHERE embedding IS NULL AND id % n = k`, so a
  # killed shard simply leaves its rows pending for the next run.
  nohup "$PY" -u -c "
import sys; sys.path.insert(0,'.')
import embed
embed.run(shard=($k, $SHARDS))
" >> "$LOG" 2>&1 &
  echo "  shard $k -> pid $! log $LOG"
done
wait
