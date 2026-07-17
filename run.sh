#!/usr/bin/env bash
# Evaluation-first build order (spec §9). Idempotent; re-run any step.
set -euo pipefail
cd "$(dirname "$0")"
PY=./.venv/bin/python
export HF_HUB_DISABLE_PROGRESS_BARS=1

step() { echo; echo "==== $* ===="; }

step "1-2. Schema + date/status engine (§3,§5)"
PGPASSWORD=patents_pilot_local psql -h 127.0.0.1 -p 5433 -U patents -d patents -v ON_ERROR_STOP=1 -f sql/001_schema.sql >/dev/null
( cd src && $PY -c "import search_modes; print('date/status modes:', [m.value for m in search_modes.Mode])" )

step "3. Profile BigQuery coverage (§2.1)"
( cd src && $PY coverage_profile.py )

step "4. Frozen evaluation gold set (§8) — BEFORE the index"
( cd src && $PY goldset.py )

step "5. Ingest core+expanded (§2.2)"
( cd src && $PY ingest_bq.py core && $PY ingest_bq.py expanded )
( cd src && $PY ingest_pg.py all )

step "6. Chunk + embed (every claim, hierarchical) (§4)"
( cd src && $PY chunker.py )
( cd src && $PY embed.py run )
( cd src && $PY -c "import evaluate,embed; embed.run_bench(pub_ids=evaluate.bench_targets())" )   # multi-dim bench

step "7. Heavy indexes: HNSW + FTS/BM25 (§4)"
PGPASSWORD=patents_pilot_local psql -h 127.0.0.1 -p 5433 -U patents -d patents -v ON_ERROR_STOP=1 -f sql/002_indexes.sql

step "9. 5-config ablation + metrics + dimension benchmark (§8)"
( cd src && $PY evaluate.py && $PY evaluate.py bench )

step "Report demo (§7): grounded element-by-element prior-art report"
( cd src && $PY report.py grabo_gripper_novelty )

echo; echo "Done. Artifacts: data/coverage, data/goldset, data/eval, data/reports"
