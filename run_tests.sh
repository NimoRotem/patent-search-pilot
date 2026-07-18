#!/usr/bin/env bash
# Milestone 7 §1 — one-command test suite. Hermetic (no paid APIs), reads the real Postgres,
# targets < ~2 min. Clear PASS/FAIL summary.
set -uo pipefail
cd "$(dirname "$0")"
export HF_HUB_DISABLE_PROGRESS_BARS=1 PYTHONWARNINGS="ignore::FutureWarning"
echo "== patent-pilot test suite =="
./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider --no-header \
  --disable-warnings -o addopts="" 2>&1 | grep -viE 'FutureWarning|warnings.warn|end of life|upgrade your Python'
rc=${PIPESTATUS[0]}
echo
[ "$rc" = 0 ] && echo "RESULT: ALL TESTS PASSED" || echo "RESULT: TESTS FAILED (rc=$rc)"
exit $rc
