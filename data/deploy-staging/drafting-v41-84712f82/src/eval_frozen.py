"""Re-baseline the frozen 11-query gold set, stamped with the code and corpus that produced it.

WHY THIS EXISTS
---------------
`evaluate.py` runs the full five-config ablation and writes `eval_results.json` stamped with a
DATE and nothing else. That is not enough to compare two runs, and the gap caused a real
misattribution on 2026-07-29:

  * `eval_results.json` was generated 2026-07-19 (vector recall@100 = 0.1697).
  * Eight commits touching retrieval landed on 2026-07-20 — parallel multi-channel fan-out,
    family dedup, listwise rerank, OOD de-dilution — and the ablation was never re-run.
  * A corpus ingest on 07-29 measured 0.1658, and the 0.0039 difference was initially read as
    dilution caused by the new documents.
  * It was not. Restricting the dense channel to the pre-ingest office set reproduces 0.1658
    exactly, so the new publications contribute nothing to the delta. The difference is the
    unmeasured 07-20 retrieval work.

The lesson is not "be careful", it is that a recall number without a commit is not a baseline.
This module runs the deterministic configs only (so two runs of the same code and corpus give the
same number) and records commit + corpus size beside the result.
"""
from __future__ import annotations

import json
import statistics
import sys

import goldset
from config import DATA
from evaluate import recall, subject_from
from eval_wide import provenance
from retrieval import Retriever

CONFIGS = ("vector", "hybrid")
KS = (10, 100, 500)
OUT = DATA / "eval" / "frozen_baseline.json"


def run(label="current", configs=CONFIGS):
    R = Retriever()
    with R.conn.cursor() as c:
        c.execute("SELECT DISTINCT COALESCE(NULLIF(simple_family_id,''), publication_number) f "
                  "FROM publications")
        corpus = {r["f"] for r in c.fetchall()}

    rows = []
    for e in goldset.load()["entries"]:
        gold = set(e["gold_families"])
        reach = gold & corpus
        rec = {"id": e["id"], "n_gold": len(gold), "n_reachable": len(reach),
               "reachability": round(len(reach) / len(gold), 4) if gold else None, "configs": {}}
        for cfg in configs:
            res = R.search(e["query_text"], subject=subject_from(e), mode=e["mode"],
                           config=cfg, topk=1000)
            fams = [fk for fk, *_ in res.family_ranked]
            rec["configs"][cfg] = {
                **{f"recall@{k}": recall(fams, gold, k) for k in KS},
                **{f"reachable@{k}": (recall(fams, reach, k) if reach else None) for k in KS},
            }
        rows.append(rec)
        print(f"  {e['id']:32s} gold={len(gold):>3} reach={len(reach):>3} "
              f"r@100={rec['configs'][configs[0]]['recall@100']:.4f}", flush=True)

    def macro(cfg, key):
        v = [r["configs"][cfg][key] for r in rows if r["configs"][cfg].get(key) is not None]
        return round(statistics.fmean(v), 4) if v else None

    summary = {
        "label": label,
        "provenance": provenance(),
        "n_queries": len(rows),
        "reachability_macro": round(statistics.fmean([r["reachability"] for r in rows]), 4),
        "configs": {c: {f"recall@{k}": macro(c, f"recall@{k}") for k in KS} |
                       {f"reachable@{k}": macro(c, f"reachable@{k}") for k in KS}
                    for c in configs},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    prev[label] = {"summary": summary, "rows": rows}
    OUT.write_text(json.dumps(prev, indent=1))

    p = summary["provenance"]
    print(f"\n=== frozen gold set — {label} ===")
    print(f"  code {p.get('commit')}{' (DIRTY)' if p.get('worktree_dirty') else ''} · "
          f"corpus {p.get('publications'):,} pubs / {p.get('chunks'):,} chunks")
    print(f"  reachability {summary['reachability_macro']:.1%}")
    for c in configs:
        s = summary["configs"][c]
        print(f"  {c:14s} recall@100 {s['recall@100']:.4f}   reachable@100 {s['reachable@100']:.4f}")
    print(f"  -> {OUT}")
    return summary


if __name__ == "__main__":
    run(label=sys.argv[1] if len(sys.argv) > 1 else "current")
