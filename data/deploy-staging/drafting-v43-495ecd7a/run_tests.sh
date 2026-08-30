#!/usr/bin/env bash
# Milestone 7 §1 — one-command test suite. Hermetic (no paid APIs), reads the real Postgres,
# targets < ~2 min. Clear PASS/FAIL summary.
set -uo pipefail
cd "$(dirname "$0")"
export HF_HUB_DISABLE_PROGRESS_BARS=1 PYTHONWARNINGS="ignore::FutureWarning"
#  A second checkout of this repo (the fable bench) shares the primary checkout's venv rather
#  than building its own, so a hard-coded ./.venv path makes the suite unrunnable there. PY
#  overrides; the default is unchanged.
if [ ! -x "${PY:-./.venv/bin/python}" ] && [ -x ../patent-search-pilot/.venv/bin/python ]; then
  PY=../patent-search-pilot/.venv/bin/python
fi
echo "== patent-pilot test suite =="
"${PY:-./.venv/bin/python}" -m pytest tests/ -q -p no:cacheprovider --no-header \
  --disable-warnings -o addopts="" 2>&1 | grep -viE 'FutureWarning|warnings.warn|end of life|upgrade your Python'
rc=${PIPESTATUS[0]}
echo
[ "$rc" = 0 ] && echo "RESULT: ALL TESTS PASSED" || echo "RESULT: TESTS FAILED (rc=$rc)"
exit $rc
