#!/bin/bash
#  Treatment arm: same 28 dev subjects, same frozen disclosure lists, same source tree, on the
#  corpus that received batch one of the acquisition. REUSE_META_FROM_TAG makes it reuse the
#  control's ingested subject verbatim, so the two arms share a byte-identical brief and document
#  and the corpus is the only intended difference.
cd /home/nimrod_rotem/patent-search-pilot
export PGPORT=5434
export REPLAY_MODE=record
export REUSE_META_FROM_TAG=abc2
export TAG=abt2
export SPLIT=dev

echo "start $(date +%H:%M)  port=$PGPORT reuse=$REUSE_META_FROM_TAG"
BEFORE=$(find data/replay -name '*.json' | wc -l)
echo "replay entries before: $BEFORE"

.venv/bin/python -u eval/run_split.py > data/logs/ab_treatment.log 2>&1
echo "treatment finished $(date +%H:%M)"
tail -2 data/logs/ab_treatment.log

AFTER=$(find data/replay -name '*.json' | wc -l)
echo "replay entries after: $AFTER  (grew by $((AFTER - BEFORE)))"

echo "=== comparison ==="
.venv/bin/python -u eval/ab_compare.py --control abc2 --treatment abt2 > data/logs/ab_result.log 2>&1
cat data/logs/ab_result.log
echo "=== DONE $(date +%H:%M) ==="
