"""Does ranking by CONTRIBUTION beat ranking by similarity? Offline sweep over finished reports.

Two metrics, because they answer different questions and a change must not quietly trade one away:

  cited@50   how many of the examiner's cited families land in the top 50. The recall metric the
             benchmark has used throughout.
  covered    how much of the invention's rarity-weighted disclosure mass the top 50 covers, and
             how many of those fifty slots add NOTHING. This is the product metric: a report that
             attacks more of the invention with the same fifty slots is more useful whatever it
             does to recall, because that is what the reader is building an argument out of.

Runs entirely on saved reports: deep_rank.by_pub holds every charted reference's verdicts and
feature_idf holds the rarity weights, so a new ORDER costs no search and no LLM call.

    python eval/coverage_sweep.py --tag v13
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import coverage_rank as CVR  # noqa: E402
import db  # noqa: E402
from order_ab import gold, members, hits  # noqa: E402

GRID = [
    #  (corroboration, score_weight)
    (0.00, 0.00), (0.00, 0.35), (0.25, 0.35), (0.25, 0.15),
    (0.50, 0.35), (0.50, 0.15), (1.00, 0.35),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v13")
    ap.add_argument("--depth", type=int, default=None)
    args = ap.parse_args()

    subs = json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]
    con = db.connect()
    con.autocommit = True
    cur = con.cursor()

    tot = {g: [0, 0.0, 0, 0] for g in GRID}      # cited, covered_frac, dead slots, n
    base = [0, 0.0, 0, 0]
    n_gold = 0

    for sub in subs:
        path = os.path.join(ROOT, "data", "reports", f"bench-{sub['id']}-{args.tag}.json")
        if not os.path.exists(path):
            continue
        rep = json.load(open(path))
        dr = rep.get("deep_rank") or {}
        by, order = dr.get("by_pub") or {}, dr.get("order") or []
        idf = dr.get("feature_idf") or {}
        if not order or not idf:
            continue
        g = gold(cur, sub["citations"])
        mem = members(cur, set(g))
        n_gold += len(g)

        b_cited, _ = hits(order, g, mem)
        b_cov, b_tot, b_dead = CVR.covered_mass(order, by, idf)
        base[0] += b_cited
        base[1] += b_cov / (b_tot or 1)
        base[2] += b_dead
        base[3] += 1
        print(f"\n{sub['id']}: {len(g)} cited families | pointwise "
              f"cited@50={b_cited} covered={b_cov / (b_tot or 1):.0%} dead-slots={b_dead}/50")

        for grid in GRID:
            corr, sw = grid
            new, _gains = CVR.rank(order, by, idf, depth=args.depth,
                                   corroboration=corr, score_weight=sw)
            c, _ = hits(new, g, mem)
            cov, t, dead = CVR.covered_mass(new, by, idf)
            tot[grid][0] += c
            tot[grid][1] += cov / (t or 1)
            tot[grid][2] += dead
            tot[grid][3] += 1
            print(f"   corr={corr:.2f} score_w={sw:.2f}   cited@50={c:<3d} "
                  f"covered={cov / (t or 1):.0%}  dead-slots={dead}/50")

    n = base[3] or 1
    print(f"\n{'=' * 70}\nTOTAL over {n_gold} cited families, {n} subjects\n{'=' * 70}")
    print(f"{'variant':22s} {'cited@50':>9s} {'covered':>9s} {'dead slots':>11s}")
    print(f"{'pointwise (current)':22s} {base[0]:>9d} {base[1] / n:>8.0%} "
          f"{base[2] / n:>10.0f}/50")
    for grid in GRID:
        c, cov, dead, k = tot[grid]
        k = k or 1
        print(f"{f'corr={grid[0]} score_w={grid[1]}':22s} {c:>9d} {cov / k:>8.0%} "
              f"{dead / k:>10.0f}/50")


if __name__ == "__main__":
    main()
