#!/usr/bin/env bash
# Wait for the sharded embedding to drain, then measure Stage A on both gold sets.
#
# Chained rather than run by hand because the embed tail is hours long and the two evaluations
# must run against a FULLY embedded corpus to be quotable — a partially-embedded measurement
# understates recall and would be a misleading "final" number. This script is the thing that
# guarantees the eval happens the moment the corpus is ready, not whenever someone next looks.
#
# Idempotent: both evaluators append a labelled entry to their JSON, so a re-run overwrites its
# own label and nothing else.
set -uo pipefail
ROOT=/home/nimrod_rotem/patent-search-pilot
PY=$ROOT/.venv/bin/python
LOG=$ROOT/data/stage_a_finish.log
LABEL="${LABEL:-stageA_final}"
cd "$ROOT/src" || exit 1

log(){ echo "$(date -Is) $*" >> "$LOG"; }

pending(){ "$PY" -c "
import sys; sys.path.insert(0,'.')
import db; print(db.scalar('SELECT count(*) FROM chunks WHERE embedding IS NULL') or 0)
" 2>/dev/null | tail -1; }

log "=== waiting for embedding to drain ==="
STALL=0
LAST=$(pending)
while :; do
  N=$(pending)
  [ -z "$N" ] && N=$LAST
  if [ "$N" = "0" ]; then log "embedding complete"; break; fi
  # A stalled queue must not be mistaken for a slow one: if nothing moves across ~30 min the
  # shard workers have died and waiting forever would silently never produce the numbers.
  if [ "$N" = "$LAST" ]; then
    STALL=$((STALL+1))
    if [ "$STALL" -ge 6 ]; then
      log "ABORT: pending stuck at $N across ~30 min — shard workers appear dead"
      exit 1
    fi
  else
    STALL=0
  fi
  log "pending $N"
  LAST=$N
  sleep 300
done

log "=== frozen 11-query gold set ==="
"$PY" eval_frozen.py "$LABEL" >> "$LOG" 2>&1
log "=== widened 36-query gold set ==="
"$PY" eval_wide.py "$LABEL" >> "$LOG" 2>&1
log "=== stage A measurement complete ==="
