#!/usr/bin/env bash
# EPO OPS full-text backfill — the single command that fills the EP/WO full-text hole.
#
#   ./ops_backfill.sh                # full run: every claimless EP/WO core pub, then embed
#   ./ops_backfill.sh --limit 200    # bounded run (use this first on a fresh credential)
#   ./ops_backfill.sh --status       # credential + weekly-budget check, no network writes
#   ./ops_backfill.sh --legal-sweep  # DE/national INPADOC legal status only (no full text)
#
# PREREQUISITE — credentials in .env (EPO developer portal -> My Apps -> <app> -> Keys):
#     OPS_CONSUMER_KEY=...
#     OPS_CONSUMER_SECRET=...
# Registration is per-account at https://developers.epo.org (free "Non-paying" tier, 4 GB/week).
#
# WHAT IT DOES
#   1. Selects claimless tier=core publications, gold-relevant families first.
#   2. Fetches claims + description + drawings + INPADOC legal status from OPS 3.2.
#   3. Writes claims/paragraphs/legal_events with field_provenance rows
#      (ops_fulltext = full text retrieved; ops_legal = legal status only).
#   4. Creates claim_own / claim_resolved / paragraph chunks with embedding = NULL.
#   5. Runs embed.py, which fills them with Vertex gemini-embedding-001 @768d,
#      task_type RETRIEVAL_DOCUMENT — identical settings to the rest of the corpus.
#
# COVERAGE CAVEAT: the OPS full-text service serves EP and WO only. Every national DE
# publication returns 404 on /claims and /description, so DE is routed to legal+images only.
# German full text needs a different source (see enrich_de_batch.py).
#
# SAFETY
#   * Resumable — provenance-gated, so re-running skips what already landed. Safe to Ctrl-C.
#   * Throttled from the live X-Throttling-Control header, per OPS service.
#   * Weekly byte budget persisted in data/ops_budget.json; the run stops itself at
#     OPS_BUDGET_SOFT_FRAC (default 0.80) of the 4 GB/week free-tier allowance.
#   * Commits per publication — never holds a long transaction against the live app's DB.
set -euo pipefail

cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "no venv at $PY" >&2; exit 1; }

case "${1:-}" in
  --status)      exec "$PY" src/ops.py --status ;;
  --legal-sweep) shift; exec "$PY" src/ops.py --legal-sweep "$@" ;;
esac

LOG="data/ops_backfill.$(date +%Y%m%d-%H%M%S).log"
echo "[ops] logging to $LOG"
"$PY" src/ops.py --status
"$PY" src/ops.py --backfill-core "$@" 2>&1 | tee "$LOG"
echo "[ops] done. Verify with:"
echo "  $PY src/ops.py --status"
echo "  psql -c \"SELECT count(*) FROM chunks WHERE embedding IS NULL;\"   # expect 0"
