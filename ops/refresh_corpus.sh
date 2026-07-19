#!/usr/bin/env bash
# Weekly corpus refresh for the patent-search pilot.
#
# Installed as a cron entry for user nimrod_rotem (see ops/patent-refresh.cron).
# Safe to run by hand at any time: an flock inside incremental_ingest.py guarantees two
# runs never overlap, and every stage is idempotent.
#
# Normal week: BigQuery has published nothing new -> the ~2 GB freshness probe short-circuits
# the run and it costs about $0.01. Only when BigQuery actually refreshes (roughly quarterly)
# does the ~1.5 TB / ~$9 full-text delta extract fire.
set -uo pipefail

APP=/home/nimrod_rotem/patent-search-pilot
PY="$APP/.venv/bin/python"
LOG="$APP/data/incremental_ingest.log"

cd "$APP" || exit 1

# Cost ceilings. The extract ceiling sits below a full 3.1 TB table scan so an accidentally
# unfiltered query can never sail through. Override here, not in the source.
export DELTA_MAX_GB="${DELTA_MAX_GB:-1800}"
export DELTA_PROBE_MAX_GB="${DELTA_PROBE_MAX_GB:-50}"
export DELTA_LOOKBACK_DAYS="${DELTA_LOOKBACK_DAYS:-90}"
# Throttle: keep the live app responsive while HNSW inserts churn the page cache.
export DELTA_EMBED_BATCH="${DELTA_EMBED_BATCH:-4000}"
export DELTA_EMBED_THROTTLE_S="${DELTA_EMBED_THROTTLE_S:-2.0}"
# We redirect stdout+stderr into $LOG below, so the logger must NOT also write to it
# directly -- otherwise every line is duplicated. This way the log captures the logger,
# embed.py's progress output and any traceback, each exactly once.
export DELTA_LOG_TO_FILE=0

{
  echo "=============================================================================="
  echo "cron refresh starting $(date -Is) (uid=$(id -un))"
} >> "$LOG"

# nice/ionice: this box is memory constrained and serves live traffic; the refresh is
# strictly lower priority than the web app.
nice -n 15 ionice -c2 -n7 "$PY" -u "$APP/src/incremental_ingest.py" "$@" >> "$LOG" 2>&1
rc=$?

echo "cron refresh finished $(date -Is) rc=$rc" >> "$LOG"

# rc 2 = cost ceiling exceeded, rc 3 = lock held by another run. Both are deliberate
# refusals, not failures worth alerting on repeatedly, but they must stay visible.
if [ $rc -eq 2 ]; then
  echo "WARNING: refresh refused - BigQuery cost ceiling exceeded. Investigate before raising DELTA_MAX_GB." >> "$LOG"
fi

# Keep the log from growing without bound (weekly job, keep ~1 year of runs).
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 20000000 ]; then
  tail -c 5000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit $rc
