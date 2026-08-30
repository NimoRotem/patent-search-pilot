#!/bin/bash
#  The acquisition A/B, end to end and unattended.
#
#  ARM A, control    : corpus 5433, the untouched clone source. REPLAY_MODE=record, so this arm
#                      DEFINES the external world the treatment will then reuse.
#  ARM B, treatment  : corpus 5434, same corpus plus the batch-one description text.
#                      REPLAY_MODE=record, not replay, on purpose: a strict replay miss kills a
#                      whole subject, and the query plan is only frozen if the upstream brief is
#                      byte-identical. Record degrades to a live call instead of losing the run.
#                      The guarantee is then MEASURED rather than assumed: the cache entry count
#                      is captured before and after, and if it grew, live calls happened and the
#                      report says exactly how many.
#
#  Both arms run the same 26 dev subjects, same frozen disclosure lists, same code (src_tree_hash
#  is recorded per run and manifest.comparable refuses a mismatch).
set -u
cd /home/nimrod_rotem/patent-search-pilot
PY=.venv/bin/python
LOG=data/logs

echo "=== waiting for the embed to finish ==="
while pgrep -f acquire_load >/dev/null; do sleep 120; done
echo "embed process gone at $(date +%H:%M)"
tail -3 "$LOG/acquire_load.log" | grep -viE "warnings.warn|FutureWarning"

echo
echo "=== treatment corpus state (5434) vs control (5433) ==="
PGPORT=5433 $PY -c "
import sys; sys.path.insert(0,'src'); import db
with db.cursor() as c:
    c.execute(\"select count(*) n from chunks where kind='paragraph'\"); a=c.fetchone()['n']
    c.execute('select count(*) n from chunks where embedding is null'); b=c.fetchone()['n']
print(f'control  : {a:,} paragraph chunks, {b:,} unembedded')" 2>/dev/null | grep -v Warning
PGPORT=5434 $PY -c "
import sys; sys.path.insert(0,'src'); import db
with db.cursor() as c:
    c.execute(\"select count(*) n from chunks where kind='paragraph'\"); a=c.fetchone()['n']
    c.execute('select count(*) n from chunks where embedding is null'); b=c.fetchone()['n']
print(f'treatment: {a:,} paragraph chunks, {b:,} unembedded')" 2>/dev/null | grep -v Warning

CACHE_BEFORE_ALL=$(find data/replay -name '*.json' 2>/dev/null | wc -l)

echo
echo "=== ARM A: control on 5433, replay=record  ($(date +%H:%M)) ==="
TAG=abc2 SPLIT=dev PGPORT=5433 REPLAY_MODE=record $PY -u eval/run_split.py \
  > "$LOG/ab_control.log" 2>&1
echo "control finished $(date +%H:%M)"
tail -2 "$LOG/ab_control.log"

CACHE_AFTER_CONTROL=$(find data/replay -name '*.json' 2>/dev/null | wc -l)

echo
echo "=== ARM B: treatment on 5434, replay=record  ($(date +%H:%M)) ==="
TAG=abt2 SPLIT=dev PGPORT=5434 REPLAY_MODE=record $PY -u eval/run_split.py \
  > "$LOG/ab_treatment.log" 2>&1
echo "treatment finished $(date +%H:%M)"
tail -2 "$LOG/ab_treatment.log"

CACHE_AFTER_TREAT=$(find data/replay -name '*.json' 2>/dev/null | wc -l)

echo
echo "=== external-world isolation, measured ==="
echo "replay cache entries: start $CACHE_BEFORE_ALL -> after control $CACHE_AFTER_CONTROL -> after treatment $CACHE_AFTER_TREAT"
GREW=$((CACHE_AFTER_TREAT - CACHE_AFTER_CONTROL))
if [ "$GREW" -eq 0 ]; then
  echo "the treatment arm made NO live external call: every request was served from the control's recording."
else
  echo "WARNING: the treatment arm made $GREW live external calls. Those requests differed from the control's, so the external world was NOT held constant for them."
fi

echo
echo "=== comparison ==="
$PY -u eval/ab_compare.py --control abc2 --treatment abt2 2>&1 | grep -v Warning
echo "=== A/B COMPLETE $(date +%H:%M) ==="
