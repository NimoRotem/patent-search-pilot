"""Emit the before/after comparison tables for eval_report.md.

Reads the committed pre-OPS baseline and the new run and prints markdown. Metric definitions are
untouched -- this only formats what evaluate.py already computed, side by side.
"""
from __future__ import annotations
import json, statistics, sys
sys.path.insert(0, 'src')
from config import DATA

OUT = DATA / "eval"
CFGS = ["keyword", "vector", "hybrid", "hybrid_rerank", "agentic"]
KEYS = ["family_recall@100", "family_recall@500", "family_recall@1000", "reachable_recall@100"]


def agg(rows, cfg, key):
    vals = [r["configs"][cfg][key] for r in rows
            if cfg in r["configs"] and r["configs"][cfg].get(key) is not None]
    return round(statistics.mean(vals), 4) if vals else None


def earliest(rows, cfg):
    ys = sum(1 for r in rows if r["configs"].get(cfg, {}).get("earliest_recovered"))
    tot = sum(1 for r in rows if "earliest_recovered" in r["configs"].get(cfg, {}))
    return f"{ys}/{tot}"


def d(a, b):
    if a is None or b is None:
        return "—"
    x = round(b - a, 4)
    return f"{x:+.4f}" if x else "0"


def main():
    before = json.loads((OUT / "eval_results.PRE_OPS.json").read_text())["results"]
    after = json.loads((OUT / "eval_results.json").read_text())["results"]

    print("### Headline — mean family recall@k (macro-avg, 11 frozen gold searches)\n")
    print("| Config | r@100 before | r@100 after | Δ | r@500 before | r@500 after | Δ "
          "| r@1000 before | r@1000 after | Δ | earliest before | after |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|:--:|:--:|")
    for c in CFGS:
        row = [c]
        for k in ["family_recall@100", "family_recall@500", "family_recall@1000"]:
            a, b = agg(before, c, k), agg(after, c, k)
            row += [f"{a}", f"{b}", d(a, b)]
        row += [earliest(before, c), earliest(after, c)]
        print("| " + " | ".join(str(x) for x in row) + " |")

    print("\n### reachable_recall@100\n")
    print("| Config | before | after | Δ |")
    print("|---|--:|--:|--:|")
    for c in CFGS:
        a, b = agg(before, c, "reachable_recall@100"), agg(after, c, "reachable_recall@100")
        print(f"| {c} | {a} | {b} | {d(a,b)} |")

    print("\n### Per-query family recall@100 (before → after)\n")
    ob = {r["id"]: r for r in before}
    depth = json.loads((OUT / "depth_snapshot.json").read_text())["per_query"]
    print("| query | gold | new-text families | " + " | ".join(CFGS) + " |")
    print("|---|--:|--:|" + "--:|" * len(CFGS))
    for r in after:
        b = ob.get(r["id"], {"configs": {}})
        cells = []
        for c in CFGS:
            x = b["configs"].get(c, {}).get("family_recall@100")
            y = r["configs"].get(c, {}).get("family_recall@100")
            cells.append(f"{x} → {y}" + ("" if x == y else " *"))
        nnew = depth.get(r["id"], {}).get("deepened_NEW", 0)
        print(f"| `{r['id']}` | {r['n_gold']} | {nnew} | " + " | ".join(cells) + " |")
    print("\n`*` = changed. Rows with no `*` are bit-identical to the pre-OPS baseline.")


if __name__ == "__main__":
    main()
