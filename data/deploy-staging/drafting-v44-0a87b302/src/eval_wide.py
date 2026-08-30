"""Score the widened-field gold set — the before/after ruler for corpus expansion.

Deliberately NOT the full five-config ablation in `evaluate.py`. That runs keyword + vector +
hybrid + hybrid_rerank + agentic over every query; on 36 queries with an LLM in the loop that is
well over an hour and its agentic row is non-deterministic, which is the wrong instrument for
answering "did adding N publications move retrieval". This runs the deterministic configs only, so
two runs of the same corpus produce the same number and any movement is the corpus.

Reports three things per config, and the third is the one that matters:

  recall@k            of ALL gold families, how many we surfaced
  reachable@k         of the gold families actually PRESENT in the corpus, how many we surfaced
  reachability        what share of gold is in the corpus at all — the hard ceiling on recall@k

Separating them stops the classic misreading. A low recall can mean the retriever is weak OR that
the art simply is not in the corpus, and those call for opposite responses: tune ranking, or buy
more corpus. Reachability tells you which.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import goldset_wide
from config import DATA
from retrieval import Retriever
#  Reuse evaluate.subject_from rather than rebuilding a Subject here: the date engine's
#  novelty/secret-art boundaries are decided by these fields, so two constructors that drift apart
#  would silently score the two gold sets under different legal rules.
from evaluate import subject_from as _subject

KS = (10, 100, 500)
CONFIGS = ("vector", "hybrid")
OUT = DATA / "eval" / "wide_baseline.json"


def _recall(ranked, gold, k):
    if not gold:
        return None
    seen = []
    for f in ranked:
        if f not in seen:
            seen.append(f)
        if len(seen) >= k:
            break
    return len(set(seen) & gold) / len(gold)


def provenance():
    """What produced this number: code commit + corpus size.

    `eval_results.json` records only a date. That is not enough to compare two runs, and it cost
    a real misattribution: the 0.1697 baseline was generated 2026-07-19, eight commits touching
    retrieval landed on 07-20 (parallel multi-channel fan-out, family dedup, listwise rerank,
    OOD de-dilution) with no re-run, and the next measurement — after a corpus ingest — read the
    whole difference as an effect of the ingest. It was not: restricting the dense channel to the
    pre-ingest offices reproduced the post-ingest number exactly.

    A recall figure is only comparable against another figure from the same code AND the same
    corpus, so both are stamped here and any comparison that ignores them is unsound.
    """
    import subprocess
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, cwd=str(Path(__file__).resolve().parent.parent),
                             timeout=10).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "-uno"], capture_output=True,
                                    text=True, cwd=str(Path(__file__).resolve().parent.parent),
                                    timeout=10).stdout.strip())
    except Exception:
        sha, dirty = None, None
    import db
    pubs = chunks = None
    try:
        with db.cursor() as c:
            c.execute("SELECT count(*) n FROM publications")
            pubs = c.fetchone()["n"]
            c.execute("SELECT count(*) n FROM chunks")
            chunks = c.fetchone()["n"]
    except Exception:
        pass
    return {"commit": sha, "worktree_dirty": dirty, "publications": pubs, "chunks": chunks}


def corpus_families(R):
    with R.conn.cursor() as cur:
        cur.execute("SELECT DISTINCT COALESCE(NULLIF(simple_family_id,''), publication_number) f "
                    "FROM publications")
        return {r[0] if not isinstance(r, dict) else r["f"] for r in cur.fetchall()}


def run(limit=None, configs=CONFIGS, label="baseline"):
    R = Retriever()
    corpus = corpus_families(R)
    entries = goldset_wide.load()["entries"][:limit]
    rows = []
    for i, e in enumerate(entries, 1):
        gold = set(e["gold_families"])
        reach = gold & corpus
        rec = {"id": e["id"], "stratum": e["stratum"], "n_gold": len(gold),
               "n_reachable": len(reach),
               "reachability": round(len(reach) / len(gold), 4) if gold else None,
               "configs": {}}
        for cfg in configs:
            res = R.search(e["query_text"], subject=_subject(e), mode=e["mode"],
                           config=cfg, topk=1000)
            fams = [fk for fk, *_ in res.family_ranked]
            rec["configs"][cfg] = {
                **{f"recall@{k}": _recall(fams, gold, k) for k in KS},
                **{f"reachable@{k}": _recall(fams, reach, k) for k in KS},
            }
        rows.append(rec)
        v = rec["configs"][configs[0]]
        print(f"  [{i:>2}/{len(entries)}] {e['stratum']} {e['anchor_publication']:20s} "
              f"gold={len(gold):>3} reach={len(reach):>3} "
              f"r@100={v['recall@100']:.3f} reach@100={(v['reachable@100'] or 0):.3f}", flush=True)

    def macro(cfg, key):
        vals = [r["configs"][cfg][key] for r in rows if r["configs"][cfg].get(key) is not None]
        return round(statistics.fmean(vals), 4) if vals else None

    summary = {
        "label": label,
        "provenance": provenance(),
        "n_queries": len(rows),
        "reachability_macro": round(statistics.fmean([r["reachability"] for r in rows]), 4),
        "configs": {c: {f"recall@{k}": macro(c, f"recall@{k}") for k in KS} |
                       {f"reachable@{k}": macro(c, f"reachable@{k}") for k in KS}
                    for c in configs},
        "by_stratum": {},
    }
    per = defaultdict(list)
    for r in rows:
        per[r["stratum"]].append(r)
    for s, rs in sorted(per.items()):
        summary["by_stratum"][s] = {
            "n": len(rs),
            "reachability": round(statistics.fmean([x["reachability"] for x in rs]), 4),
            "recall@100": round(statistics.fmean(
                [x["configs"][configs[0]]["recall@100"] for x in rs]), 4),
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    prev[label] = {"summary": summary, "rows": rows}
    OUT.write_text(json.dumps(prev, indent=1))

    print(f"\n=== widened gold set — {label} ({len(rows)} queries) ===")
    print(f"  reachability (share of gold present in the corpus at all): "
          f"{summary['reachability_macro']:.1%}  <-- hard ceiling on recall")
    for c in configs:
        s = summary["configs"][c]
        print(f"  {c:14s} recall@100 {s['recall@100']:.4f}   "
              f"reachable@100 {s['reachable@100']:.4f}   recall@500 {s['recall@500']:.4f}")
    print("\n  by subclass (reachability / recall@100):")
    for s, v in summary["by_stratum"].items():
        print(f"    {s:6s} n={v['n']}  reach {v['reachability']:.1%}   r@100 {v['recall@100']:.4f}")
    print(f"\n  -> {OUT}")
    return summary


if __name__ == "__main__":
    lab = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
    run(limit=lim, label=lab)
