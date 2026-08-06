"""Where do the gold references die, across the whole benchmark, with one cause each.

Plan step 5. Reads the canonical gold set and every finished report, joins them through
trace.from_report, and reports the stage each cited family reached.

    python eval/funnel.py --tag v13
    python eval/funnel.py --tag v13 --split dev --out data/logs/funnel

Writes funnel_by_subject.csv and funnel_aggregate.csv, and prints the stage-loss table that
decides the next engineering workstream (plan section 14).

Exit criterion: at least 95% of misses assigned to a specific stage, and no UNKNOWN without a
recorded pipeline failure.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import oracle  # noqa: E402
import trace as tr  # noqa: E402

#  Which stage each terminal state blames, for the decision table in plan section 14.
BLAME = {
    tr.INELIGIBLE: "not a defect (outside the date/jurisdiction window)",
    tr.SOURCE_UNAVAILABLE: "repair or replace adapters; add source coverage",
    tr.NOT_RETRIEVED: "disclosure search, learned CPC transitions, graph expansion",
    tr.CHANNEL_TRUNCATED: "per-disclosure quotas and learned fusion",
    tr.FUSION_TRUNCATED: "per-disclosure quotas and learned fusion",
    tr.DEDUPED: "repair identifier and family normalisation",
    tr.SCREEN_REJECTED: "disclosure-conditioned screening",
    tr.NOT_SELECTED_FOR_READING: "improve allocation of the reading budget",
    tr.READ_NO_EVIDENCE: "passage retrieval, segmentation, or reading depth",
    tr.CHARTING_FAILURE: "repair grounding, charting or refutation",
    tr.PORTFOLIO_EXCLUDED: "constrained portfolio construction",
    tr.TOP_50: "delivered",
    tr.UNKNOWN: "PIPELINE DEFECT: no terminal stage recorded",
}


def gold_by_subject(path=None):
    path = path or os.path.join(HERE, "benchmark_gold.csv")
    out = {}
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if r["eligible"] == "true":
                out.setdefault(r["subject_id"], []).append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--split", default="all", choices=("all", "dev", "holdout"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "logs", "funnel"))
    args = ap.parse_args()

    subs = {s["id"]: s for s in
            json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]}
    gold = gold_by_subject()
    rows, grand, missing_reports, injected = [], {}, [], []

    for sid, cits in sorted(gold.items()):
        sub = subs.get(sid) or {}
        if args.split != "all" and sub.get("split", "dev") != args.split:
            continue
        path = os.path.join(ROOT, "data", "reports", f"bench-{sid}-{args.tag}.json")
        if not os.path.exists(path):
            missing_reports.append(sid)
            continue
        rep = json.load(open(path))
        if oracle.is_injected(rep):
            #  An injected run has seen the answer key. Counting it here would report the upper
            #  bound as the result, which is the single most misleading thing this tool could do.
            injected.append(sid)
            continue
        vp = path.replace(".json", ".view.json")
        view = json.load(open(vp)) if os.path.exists(vp) else {}
        t = tr.from_report(rep, subject_id=sid, slug=f"bench-{sid}-{args.tag}", view=view)
        by, tally = tr.attribute(t, [c["gold_family_id"] for c in cits])
        for c in cits:
            fam = c["gold_family_id"]
            rows.append({"subject_id": sid, "split": sub.get("split", "dev"),
                         "field": sub.get("field", ""),
                         "corpus_stratum": (sub.get("strata") or {}).get("corpus", ""),
                         "gold_family_id": fam,
                         "cited_pub": c["cited_pub_resolved"] or c["citation_raw"],
                         "in_corpus": c["in_corpus"],
                         "stage": by.get(fam, tr.NOT_RETRIEVED)})
        for k, v in tally.items():
            grand[k] = grand.get(k, 0) + v

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "funnel_by_subject.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["subject_id"])
        w.writeheader()
        w.writerows(rows)

    total = sum(grand.values()) or 1
    print(f"tag {args.tag}  split {args.split}  "
          f"{len({r['subject_id'] for r in rows})} subjects, {total} eligible gold families")
    if missing_reports:
        print(f"  no report for {len(missing_reports)} subject(s): "
              f"{', '.join(missing_reports[:6])}"
              + (" ..." if len(missing_reports) > 6 else ""))
    if injected:
        print(f"  EXCLUDED {len(injected)} oracle-injected run(s): {', '.join(injected[:6])}")
    print(f"\n{'stage':28s} {'n':>5s} {'share':>7s}  next action (plan section 14)")
    print("-" * 100)
    with open(os.path.join(args.out, "funnel_aggregate.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["stage", "n", "share", "next_action"])
        for stage, n in sorted(grand.items(), key=lambda kv: -kv[1]):
            print(f"{stage:28s} {n:>5d} {n / total:>6.1%}  {BLAME.get(stage, '?')}")
            w.writerow([stage, n, f"{n / total:.4f}", BLAME.get(stage, "")])

    unknown = grand.get(tr.UNKNOWN, 0)
    print(f"\nattributed to a specific stage: {total - unknown}/{total} = "
          f"{(total - unknown) / total:.0%}   (exit criterion 95%)")

    #  The same table split by how much of a subject's art the corpus holds. A change that helps
    #  in-corpus subjects and does nothing for out-of-corpus ones is invisible in the aggregate.
    print(f"\n{'corpus stratum':16s} " + " ".join(f"{s[:12]:>13s}" for s in
          (tr.NOT_RETRIEVED, tr.FUSION_TRUNCATED, tr.PORTFOLIO_EXCLUDED, tr.TOP_50)))
    for strat in ("mostly_in", "mixed", "mostly_out", ""):
        sel = [r for r in rows if r["corpus_stratum"] == strat]
        if not sel:
            continue
        c = {}
        for r in sel:
            c[r["stage"]] = c.get(r["stage"], 0) + 1
        n = len(sel)
        print(f"{(strat or 'pinned'):16s} " + " ".join(
            f"{c.get(s, 0):>5d} {c.get(s, 0) / n:>6.0%}" for s in
            (tr.NOT_RETRIEVED, tr.FUSION_TRUNCATED, tr.PORTFOLIO_EXCLUDED, tr.TOP_50)))
    print(f"\nwritten {args.out}/funnel_by_subject.csv and funnel_aggregate.csv")


if __name__ == "__main__":
    main()
