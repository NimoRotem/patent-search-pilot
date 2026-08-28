"""Compare the control and treatment arms, and refuse to report a number if they are not
comparable.

The gate is manifest.comparable(). For a corpus experiment exactly ONE field is allowed to differ,
corpus_snapshot, because that is the treatment. Any second difference means something else moved
between the arms and the delta cannot be attributed to acquisition. That is reported as a refusal
rather than as a result, per plan rule R3.

Reported per subject and in aggregate:
    delivered gold families        the headline, X/Y separately from A
    charted with evidence          where added description is EXPECTED to pay
    read in full                   whether deeper text changed the read set
    stage movement                 which references moved, and in which direction

    python eval/ab_compare.py --control abc2 --treatment abt2
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import manifest  # noqa: E402
import trace as tr  # noqa: E402
import webapp  # noqa: E402
from funnel import gold_by_subject  # noqa: E402

RANKING = {tr.CHANNEL_TRUNCATED, tr.FUSION_TRUNCATED, tr.DEDUPED, tr.SCREEN_REJECTED,
           tr.NOT_SELECTED_FOR_READING, tr.READ_NO_EVIDENCE, tr.CHARTING_FAILURE,
           tr.PORTFOLIO_EXCLUDED}


def load(sid, tag):
    p = os.path.join(ROOT, "data", "reports", f"bench-{sid}-{tag}.json")
    if not os.path.exists(p):
        return None, None, None
    rep = json.load(open(p))
    m = manifest.load(rep.get("run_id") or "") if rep.get("run_id") else None
    if (m or {}).get("completion_status") == "running":
        return None, None, None
    vp = p.replace(".json", ".view.json")
    if os.path.exists(vp):
        view = json.load(open(vp))
    else:
        try:
            view = webapp._build_view_cached(f"bench-{sid}-{tag}", rep) or {}
        except Exception:
            view = {}
    return rep, m, view


def code_class(c):
    c = (c or "").upper()
    return "X" if "X" in c else ("Y" if "Y" in c else "A")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True)
    ap.add_argument("--treatment", required=True)
    args = ap.parse_args()

    subs = {s["id"]: s for s in
            json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]}
    gold = gold_by_subject()

    rows, incomparable, both = [], [], 0
    agg = collections.Counter()
    moves = collections.Counter()

    for sid in sorted(gold):
        if (subs.get(sid) or {}).get("split", "dev") != "dev":
            continue
        cr, cm, cv = load(sid, args.control)
        tr_, tm, tv = load(sid, args.treatment)
        if not cr or not tr_:
            continue
        both += 1

        diffs = manifest.comparable(cm, tm)
        allowed = {"corpus_snapshot differs"}
        unexpected = [d for d in diffs if d not in allowed]
        if unexpected:
            incomparable.append((sid, unexpected))

        cits = gold[sid]
        fams = [c["gold_family_id"] for c in cits]
        cls = {c["gold_family_id"]: code_class(c["citation_code"]) for c in cits}

        cby, _ = tr.attribute(tr.from_report(cr, sid, f"bench-{sid}-{args.control}", cv), fams)
        tby, _ = tr.attribute(tr.from_report(tr_, sid, f"bench-{sid}-{args.treatment}", tv), fams)

        cd = sum(1 for f in fams if cby.get(f) == tr.TOP_50)
        td = sum(1 for f in fams if tby.get(f) == tr.TOP_50)
        cc = sum(1 for f in fams if cby.get(f) in (tr.TOP_50, tr.PORTFOLIO_EXCLUDED))
        tc = sum(1 for f in fams if tby.get(f) in (tr.TOP_50, tr.PORTFOLIO_EXCLUDED))
        agg["gold"] += len(fams)
        agg["c_delivered"] += cd
        agg["t_delivered"] += td
        agg["c_charted"] += cc
        agg["t_charted"] += tc
        agg["c_read"] += len((cr.get("deep_rank") or {}).get("order") or [])
        agg["t_read"] += len((tr_.get("deep_rank") or {}).get("order") or [])
        for f in fams:
            a, b = cby.get(f, tr.NOT_RETRIEVED), tby.get(f, tr.NOT_RETRIEVED)
            if a != b:
                moves[(a, b)] += 1
            if cls[f] in ("X", "Y"):
                agg["gold_xy"] += 1
                agg["c_delivered_xy"] += a == tr.TOP_50
                agg["t_delivered_xy"] += b == tr.TOP_50

        rows.append({"subject_id": sid, "n_gold": len(fams),
                     "control_delivered": cd, "treatment_delivered": td,
                     "control_charted": cc, "treatment_charted": tc,
                     "comparable": "no" if unexpected else "yes"})
        print(f"{sid:<24s} delivered {cd:>2d} -> {td:<2d}   charted {cc:>2d} -> {tc:<2d}"
              f"   {'INCOMPARABLE' if unexpected else ''}")

    print(f"\n{'=' * 74}")
    if incomparable:
        print("REFUSING TO REPORT A RESULT. These subjects differ by more than the treatment:")
        for sid, d in incomparable[:10]:
            print(f"  {sid}: {'; '.join(d)}")
        print("\nA delta measured across any of these cannot be attributed to acquisition.")
    print(f"{both} subjects ran in both arms, {both - len(incomparable)} comparable\n")
    g, gx = agg["gold"] or 1, agg["gold_xy"] or 1
    print(f"{'metric':<26s}{'control':>10s}{'treatment':>11s}{'delta':>8s}")
    for lab, a, b, d in (("delivered (all gold)", agg["c_delivered"], agg["t_delivered"], g),
                         ("delivered (X/Y only)", agg["c_delivered_xy"], agg["t_delivered_xy"], gx),
                         ("charted with evidence", agg["c_charted"], agg["t_charted"], g),
                         ("documents read in full", agg["c_read"], agg["t_read"], 0)):
        extra = f"  ({a / d:.1%} -> {b / d:.1%})" if d else ""
        print(f"{lab:<26s}{a:>10,}{b:>11,}{b - a:>+8,}{extra}")

    if moves:
        print("\nstage movement (control -> treatment), most common first:")
        for (a, b), n in moves.most_common(12):
            arrow = "gained" if b == tr.TOP_50 else ("lost" if a == tr.TOP_50 else "moved")
            print(f"  {n:>3d}  {a:<24s} -> {b:<24s} {arrow}")

    out = os.path.join(ROOT, "data", "logs", f"ab_{args.control}_vs_{args.treatment}.csv")
    if rows:
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwritten {out}")


if __name__ == "__main__":
    main()
