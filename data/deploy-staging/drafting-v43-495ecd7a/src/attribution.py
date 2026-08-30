"""Attribution: did DEEPENING THE TEXT improve recall, for the families that were actually deepened?

Two independent views, neither of which changes the metric definition:

  (A) RANK-LEVEL, post-OPS only. Re-runs the deterministic `hybrid` config and records the rank of
      every gold family, then stratifies recall@k by whether that family was text-deepened. If
      deepening works, deepened families should rank materially better than non-deepened ones.

  (B) CANDIDATE-LEVEL, before vs after. Both eval_results JSONs store
      `channel_gold_contribution` = which gold families each channel actually retrieved. Union over
      channels = "was this gold family retrieved at all". Compares that set before vs after,
      split by deepened / not-deepened. This is the attributable 2x2: if the corpus change caused
      the gain, newly-found families should be concentrated in the deepened stratum.
"""
from __future__ import annotations
import json, sys, statistics
sys.path.insert(0, 'src')

import goldset, evaluate
from retrieval import Retriever
from config import DATA

OUT = DATA / "eval"
KS = [100, 500, 1000]


STRATA = ["deepened_NEW", "deepened_pre", "bq_deep", "thin", "absent"]


def load_depth():
    d = json.loads((OUT / "depth_snapshot.json").read_text())
    fams = d["families"]
    return fams, {f: r["stratum"] for f, r in fams.items()}


def found_sets(results_json, cfg):
    """gold families that appeared in ANY retrieval channel, per query."""
    out = {}
    for r in results_json["results"]:
        c = r["configs"].get(cfg)
        if not c:
            continue
        cg = c.get("channel_gold_contribution") or {}
        s = set()
        for v in cg.values():
            s |= set(v)
        out[r["id"]] = s
    return out


def rank_level():
    R = Retriever()
    gs = goldset.load()
    fams, strat = load_depth()
    per_family = []
    for e in gs["entries"]:
        subj = evaluate.subject_from(e)
        gold = set(e["gold_families"])
        res = R.search(e["query_text"], subject=subj, mode=e["mode"], config="hybrid", topk=1000)
        ranked = [fk for fk, _, _, _ in res.family_ranked]
        pos = {f: i + 1 for i, f in enumerate(ranked)}
        for f in gold:
            per_family.append({"query": e["id"], "family": f, "stratum": strat.get(f, "absent"),
                               "rank": pos.get(f)})
        print(f"  {e['id']:32s} ranked={len(ranked)}", flush=True)
    # stratified recall@k: fraction of gold families in that stratum reaching top-k
    table = {}
    for st in STRATA:
        rows = [x for x in per_family if x["stratum"] == st]
        if not rows:
            continue
        table[st] = {"n": len(rows)}
        for k in KS:
            hit = sum(1 for x in rows if x["rank"] and x["rank"] <= k)
            table[st][f"recall@{k}"] = round(hit / len(rows), 4)
        got = [x["rank"] for x in rows if x["rank"]]
        table[st]["median_rank_when_found"] = int(statistics.median(got)) if got else None
        table[st]["found_at_all"] = round(len(got) / len(rows), 4)
    return per_family, table


def candidate_level():
    before = json.loads((OUT / "eval_results.PRE_OPS.json").read_text())
    after = json.loads((OUT / "eval_results.json").read_text())
    fams, strat = load_depth()
    gs = {e["id"]: set(e["gold_families"]) for e in goldset.load()["entries"]}
    out = {}
    for cfg in ["vector", "hybrid", "agentic"]:
        b, a = found_sets(before, cfg), found_sets(after, cfg)
        cells = {}
        for st in STRATA:
            gained = lost = kept = never = 0
            for q, gold in gs.items():
                if q not in b or q not in a:
                    continue
                for f in gold:
                    if strat.get(f) != st:
                        continue
                    fb, fa = f in b[q], f in a[q]
                    if fa and not fb:
                        gained += 1
                    elif fb and not fa:
                        lost += 1
                    elif fb and fa:
                        kept += 1
                    else:
                        never += 1
            cells[st] = {"gained": gained, "lost": lost, "kept": kept, "never": never,
                         "net": gained - lost}
        out[cfg] = cells
    return out


if __name__ == "__main__":
    print("[A] rank-level stratified recall (hybrid, post-OPS)", flush=True)
    per_family, table = rank_level()
    print(json.dumps(table, indent=2))
    print("\n[B] candidate-level before/after 2x2", flush=True)
    cand = candidate_level()
    print(json.dumps(cand, indent=2))
    (OUT / "attribution.json").write_text(json.dumps(
        {"rank_level": table, "per_family": per_family, "candidate_level": cand}, indent=1))
    print("\n[attr] wrote", OUT / "attribution.json")
