"""Does comparing references beat scoring them? Offline A/B over finished reports.

The whole comparison runs on saved data: `deep_rank.by_pub` already holds, for every reference the
pipeline read in full, its charted features, verdicts, quotes and character count. So a new
ORDERING can be measured without re-running a 20-minute search, which is what makes it possible to
tune this at all.

Measures the only thing that matters at this stage: how many cited families land in the top 50.

    python eval/order_ab.py --tag v13                     # every subject
    python eval/order_ab.py --tag v13 --subject schmalz
    python eval/order_ab.py --tag v13 --rounds 1 --group 8 --top 100
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
import citation_recall as CR  # noqa: E402


def gold(cur, citations):
    out = {}
    for p, r in CR.resolve(cur, citations).items():
        if r:
            out.setdefault(r["fam"], r["pub"])
    return out


def members(cur, fams):
    if not fams:
        return {}
    cur.execute("""SELECT COALESCE(NULLIF(simple_family_id,''), publication_number) fam,
                          publication_number pn FROM publications
                   WHERE COALESCE(NULLIF(simple_family_id,''), publication_number) = ANY(%s)""",
                (list(fams),))
    out = {}
    for r in cur.fetchall():
        out.setdefault(r["fam"], set()).add(r["pn"])
    return out


def hits(order, g, mem, cut=50):
    """(cited families in the top `cut`, their positions)."""
    pos = {p: i + 1 for i, p in enumerate(order)}
    got = {}
    for fam, pub in g.items():
        rs = [pos[m] for m in mem.get(fam, {pub}) if m in pos]
        if rs:
            got[pub] = min(rs)
    return sum(1 for r in got.values() if r <= cut), got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v13")
    ap.add_argument("--subject", default="all")
    ap.add_argument("--rounds", type=int, default=None)
    ap.add_argument("--group", type=int, default=None)
    ap.add_argument("--top", type=int, default=None)
    args = ap.parse_args()

    subs = json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]
    if args.subject != "all":
        subs = [s for s in subs if s["id"] == args.subject]

    con = db.connect()
    con.autocommit = True
    cur = con.cursor()
    tot_base = tot_new = tot_gold = 0
    print(f"{'subject':16s} {'gold':>5s} {'pointwise':>10s} {'tournament':>11s} {'secs':>6s}")
    print("-" * 56)
    detail = []
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
        base, base_pos = hits(order, g, mem)

        t0 = time.time()
        new_order = tournament.rank(feats, by, order, top=args.top,
                                    rounds=args.rounds, group=args.group)
        new, new_pos = hits(new_order, g, mem)
        el = time.time() - t0

        tot_base += base
        tot_new += new
        tot_gold += len(g)
        print(f"{sub['id']:16s} {len(g):>5d} {base:>10d} {new:>11d} {el:>6.0f}")
        moved = [(p, base_pos.get(p), new_pos.get(p)) for p in base_pos
                 if base_pos.get(p) != new_pos.get(p)]
        detail.append((sub["id"], sorted(moved, key=lambda t: t[2] or 10 ** 9)))
    print("-" * 56)
    print(f"{'TOTAL':16s} {tot_gold:>5d} {tot_base:>10d} {tot_new:>11d}")

    print("\nwhere each cited reference moved (pointwise -> tournament):")
    for sid, moved in detail:
        if not moved:
            continue
        print(f"  {sid}: " + ", ".join(
            f"{p.split('-')[1]} {a}->{b}" for p, a, b in moved[:10]))


if __name__ == "__main__":
    main()
