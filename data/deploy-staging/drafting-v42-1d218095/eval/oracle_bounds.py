"""Upper bounds per stage: if everything above stage X were perfect, what would reach the page?

WHY THIS AND NOT MORE FUNNEL ANALYSIS
-------------------------------------
Funnel attribution says where a reference died. It cannot say what would have happened if it had
not, and that is the question that decides where to spend the next month. "39 of 82 gold families
are never retrieved" is a fact. "Fixing retrieval would put them on the page" is an ASSUMPTION: if
the screen would have rejected them anyway, or the portfolio would not have selected them, then
perfect retrieval buys far less than the funnel implies and the work belongs downstream.

Injection settles it. Hand a stage the gold it never received and measure how much survives to the
delivered portfolio. Each arm is an upper bound on what fixing EVERYTHING ABOVE it could be worth:

    control              what the pipeline does today
    before_screen        retrieval is perfect. Screening, reading, portfolio on trial.
    before_read          retrieval and screening perfect. Reading and portfolio on trial.
    before_portfolio     everything perfect except selection.

Reading the result:
    before_screen close to control      -> retrieval is NOT the binding constraint
    before_screen much higher           -> retrieval is worth fixing, up to that ceiling
    before_portfolio well below 100%    -> selection is discarding gold it was handed

DIAGNOSTIC ONLY. Every report produced here is stamped, eval/funnel.py and the coverage metric
refuse to score stamped reports, and the numbers below are ceilings rather than measurements.

    python eval/oracle_bounds.py --subjects ep3707092,suction_chuck --tag ob1
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import oracle  # noqa: E402
import trace as tr  # noqa: E402
from funnel import gold_by_subject  # noqa: E402

ARMS = ["control", "before_screen", "before_read", "before_portfolio"]


def run_one(subject_id, tag, stage, gold):
    """Generate one report with the oracle armed at `stage`. -> slug or None."""
    slug = f"bench-{subject_id}-{tag}-{stage}"
    env = dict(os.environ)
    env["ORACLE_INJECTION_ENABLED"] = "0" if stage == "control" else "1"
    env["ORACLE_STAGE"] = "" if stage == "control" else stage
    env["ORACLE_GOLD"] = "" if stage == "control" else ",".join(gold)
    env["ORACLE_SLUG"] = slug
    r = subprocess.run([os.path.join(ROOT, ".venv", "bin", "python"),
                        os.path.join(HERE, "run_one_oracle.py")],
                       capture_output=True, text=True, env=env, timeout=5400)
    ok = os.path.exists(os.path.join(ROOT, "data", "reports", f"{slug}.json"))
    if not ok:
        print(f"    FAILED rc={r.returncode}: {(r.stderr or r.stdout or '')[-400:]}", flush=True)
    return slug if ok else None


def score(slug, gold_families):
    """(delivered, charted_with_evidence, stage tally) for one finished report."""
    path = os.path.join(ROOT, "data", "reports", f"{slug}.json")
    if not os.path.exists(path):
        return None
    rep = json.load(open(path))
    #  The view decides which families were DELIVERED. _generate does not write it: it is a
    #  render-time cache built on first page load, so a report produced headlessly has no view and
    #  every arm would score delivered=0. That failure is silent and it would look exactly like
    #  "injection buys nothing", which is the conclusion this whole experiment exists to test.
    #  Build it on demand, and say so loudly if it cannot be built.
    vp = path.replace(".json", ".view.json")
    if os.path.exists(vp):
        view = json.load(open(vp))
    else:
        try:
            import webapp
            view = webapp._build_view_cached(slug, rep) or {}
        except Exception as e:
            view = {}
            print(f"    VIEW BUILD FAILED ({type(e).__name__}: {e}); "
                  f"delivered is NOT measurable for this arm", flush=True)
    if not (view.get("cards") or []):
        print("    WARNING: view has no cards; delivered will read 0 for a reason that is not "
              "the pipeline", flush=True)
    t = tr.from_report(rep, subject_id="", slug=slug, view=view)
    by, tally = tr.attribute(t, gold_families)
    return {"delivered": tally.get(tr.TOP_50, 0),
            "charted": tally.get(tr.TOP_50, 0) + tally.get(tr.PORTFOLIO_EXCLUDED, 0),
            "tally": tally, "n_gold": len(gold_families),
            "injected": bool(rep.get(oracle.REPORT_KEY))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects", required=True, help="comma-separated benchmark subject ids")
    ap.add_argument("--tag", default="ob1")
    ap.add_argument("--arms", default=",".join(ARMS))
    args = ap.parse_args()

    gold = gold_by_subject()
    ids = [s.strip() for s in args.subjects.split(",") if s.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    out_rows, totals = [], {a: {"delivered": 0, "charted": 0, "gold": 0} for a in arms}

    for sid in ids:
        cits = gold.get(sid) or []
        fams = [c["gold_family_id"] for c in cits]
        if not fams:
            print(f"{sid}: no eligible gold, skipped")
            continue
        print(f"\n{sid}: {len(fams)} eligible gold families")
        for arm in arms:
            t0 = time.time()
            slug = run_one(sid, args.tag, arm, fams)
            res = score(slug, fams) if slug else None
            if not res:
                print(f"  {arm:18s} FAILED")
                continue
            totals[arm]["delivered"] += res["delivered"]
            totals[arm]["charted"] += res["charted"]
            totals[arm]["gold"] += len(fams)
            out_rows.append({"subject_id": sid, "arm": arm, "n_gold": len(fams),
                             "delivered": res["delivered"], "charted": res["charted"],
                             "seconds": round(time.time() - t0)})
            print(f"  {arm:18s} delivered {res['delivered']:>2d}/{len(fams):<3d} "
                  f"charted {res['charted']:>2d}  ({time.time() - t0:.0f}s)")

    d = os.path.join(ROOT, "data", "logs", "oracle_bounds")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{args.tag}.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["subject_id", "arm", "n_gold", "delivered",
                                           "charted", "seconds"])
        w.writeheader()
        w.writerows(out_rows)

    print(f"\n{'=' * 72}\nUPPER BOUNDS (ceilings, not measurements)\n{'=' * 72}")
    print(f"{'arm':20s} {'delivered':>12s} {'charted':>12s}  reading")
    for arm in arms:
        t = totals[arm]
        g = t["gold"] or 1
        note = {"control": "what the pipeline does today",
                "before_screen": "ceiling if retrieval were perfect",
                "before_read": "ceiling if retrieval and screening were perfect",
                "before_portfolio": "ceiling if only selection could fail"}.get(arm, "")
        print(f"{arm:20s} {f'{t[chr(100)+chr(101)+chr(108)+chr(105)+chr(118)+chr(101)+chr(114)+chr(101)+chr(100)]}/{g}':>12s} "
              f"{f'{t[chr(99)+chr(104)+chr(97)+chr(114)+chr(116)+chr(101)+chr(100)]}/{g}':>12s}  {note}")
    print(f"\nwritten {d}/{args.tag}.csv")


if __name__ == "__main__":
    main()
