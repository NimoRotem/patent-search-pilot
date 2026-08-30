#!/usr/bin/env bash
# Runs after the HNSW index is built: bench embeddings, 5-config ablation, dimension benchmark,
# and a demo grounded report. (spec §8, §7)
cd "$(dirname "$0")/src"
PY=../.venv/bin/python
export HF_HUB_DISABLE_PROGRESS_BARS=1

echo "$(date +%T) waiting for HNSW index (data/INDEX_DONE)..."
while [ ! -f ../data/INDEX_DONE ]; do sleep 20; done
echo "$(date +%T) index ready."

echo "$(date +%T) bench embeddings (gold-relevant subset @ 1024/3072)..."
$PY -c "import evaluate, embed; embed.run_bench(pub_ids=evaluate.bench_targets())" 2>&1 | grep -viE 'FutureWarning|warnings.warn'

echo "$(date +%T) 5-config ablation (§8)..."
$PY evaluate.py 2>&1 | grep -viE 'FutureWarning|warnings.warn'

echo "$(date +%T) dimension benchmark 768 vs 1024 vs 3072..."
$PY evaluate.py bench 2>&1 | grep -viE 'FutureWarning|warnings.warn'

echo "$(date +%T) demo grounded prior-art report (§7)..."
$PY report.py grabo_gripper_novelty 2>&1 | grep -viE 'FutureWarning|warnings.warn' | tail -40

touch ../data/EVAL_DONE
echo "$(date +%T) EVAL COMPLETE"
