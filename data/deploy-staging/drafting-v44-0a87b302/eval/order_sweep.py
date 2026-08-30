"""Does comparing references ADD anything on top of the evidence score? Sweep, offline.

Replacing the pointwise score with a tournament was measured first and is worse: 10 cited families
in the top 50 became 6, with the comparisons demonstrably working (25 of 25 groups judged per
round). The pointwise score is grounded in measured feature coverage with located quotes; a
comparison is an impression, and an impression favours documents that LOOK like the invention.

So the real question is whether the impression adds anything ON TOP. This runs the tournament ONCE
per criterion set and then evaluates every blend offline, so the sweep costs one tournament, not
one per variant.

    python eval/order_sweep.py --tag v13
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db  # noqa: E402
import tournament  # noqa: E402
from order_ab import gold, members, hits  # noqa: E402

SHARES = [0.0, 0.15, 0.30, 0.50, 0.75, 1.0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v13")
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--rounds", type=int, default=None)
    args = ap.parse_args()

    subs = json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]
    con = db.connect()
    con.autocommit = True
    cur = con.cursor()

    #  criterion sets: the mixed panel, and each question alone, to see whether asking DIFFERENT
    #  questions helps or whether one question asked three times is steadier.
    sets = {
        "mixed": tournament.CRITERIA,
        "coverage-only": [tournament.CRITERIA[0]],
        "novelty-only": [tournament.CRITERIA[1]],
    }
    totals = {(k, s): 0 for k in sets for s in SHARES}
    tot_gold = tot_base = 0

    for sub in subs:
        path = os.path.join(ROOT, "data", "reports", f"bench-{sub['id']}-{args.tag}.json")
        if not os.path.exists(path):
            continue
        rep = json.load(open(path))
        dr = rep.get("deep_rank") or {}
        by, order = dr.get("by_pub") or {}, dr.get("order") or []
        if not order:
            continue
        feats = rep.get("elements") or []
        g = gold(cur, sub["citations"])
        mem = members(cur, set(g))
        base, _ = hits(order, g, mem)
        tot_gold += len(g)
        tot_base += base
        print(f"\n{sub['id']}: {len(g)} cited families, pointwise {base}")

        for key, crit in sets.items():
            t0 = time.time()
            head, pts = tournament.rank_with_points(
                feats, by, order, top=args.top, rounds=args.rounds, criteria=crit)
            row = []
            for s in SHARES:
                n, _ = hits(tournament.blend(order, head, pts, share=s), g, mem)
                totals[(key, s)] += n
                row.append(f"{s:.2f}:{n}")
            print(f"   {key:14s} {' '.join(row)}   ({time.time() - t0:.0f}s)")

    print(f"\n{'=' * 62}\nTOTAL over {tot_gold} cited families "
          f"(pointwise baseline {tot_base})\n{'=' * 62}")
    print(f"{'criteria':16s} " + " ".join(f"{s:>5.2f}" for s in SHARES))
    for key in sets:
        print(f"{key:16s} " + " ".join(f"{totals[(key, s)]:>5d}" for s in SHARES))
    print("\nshare 0.00 = pointwise order untouched; 1.00 = tournament alone.")


if __name__ == "__main__":
    main()
