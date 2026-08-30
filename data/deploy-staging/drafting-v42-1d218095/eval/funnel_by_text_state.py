"""Plan step 3.1: where gold references die, CROSSED WITH the text we hold for them.

The funnel says 44% of gold families die at NOT_RETRIEVED and reads as a search failure. The text
coverage measurement says we hold enough text to read 31% of the gold set. This joins the two, so
the stage a reference died at can be read against what the pipeline had to work with.

THIS IS AN ANNOTATION, NOT A RECLASSIFICATION. No terminal stage is renamed and no reference is
moved. A reference that died at NOT_RETRIEVED while we held only its abstract is still recorded as
NOT_RETRIEVED; the table simply lets a reader see that retrieval was asked to find a document that
was, in our index, two sentences long.

    python eval/funnel_by_text_state.py --tag v15
    python eval/funnel_by_text_state.py --tag v15 --split dev

Writes data/logs/funnel/funnel_by_text_state.csv, one row per gold reference, joinable to
funnel_by_subject.csv on (subject_id, gold_family_id).
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

import textstate  # noqa: E402
import trace as tr  # noqa: E402
from funnel import gold_by_subject  # noqa: E402

#  Stage groups, for the summary line only. The per-reference rows keep the exact stage.
REACH = {tr.NOT_RETRIEVED, tr.SOURCE_UNAVAILABLE}
RANKING = {tr.CHANNEL_TRUNCATED, tr.FUSION_TRUNCATED, tr.DEDUPED, tr.SCREEN_REJECTED,
           tr.NOT_SELECTED_FOR_READING, tr.READ_NO_EVIDENCE, tr.CHARTING_FAILURE,
           tr.PORTFOLIO_EXCLUDED}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--split", default="dev", choices=("all", "dev", "holdout"))
    ap.add_argument("--funnel", default=os.path.join(ROOT, "data", "logs", "funnel",
                                                     "funnel_by_subject.csv"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "logs", "funnel",
                                                  "funnel_by_text_state.csv"))
    args = ap.parse_args()

    if not os.path.exists(args.funnel):
        raise SystemExit(f"no funnel at {args.funnel}; run eval/funnel.py --tag {args.tag} first")
    staged = {}
    for r in csv.DictReader(open(args.funnel)):
        staged[(r["subject_id"], r["gold_family_id"])] = r

    subs = {s["id"]: s for s in
            json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]}
    gold = gold_by_subject()

    #  Collect every cited publication first so the DB is hit once rather than per reference.
    pending = []
    for sid, cits in sorted(gold.items()):
        sub = subs.get(sid) or {}
        if args.split != "all" and sub.get("split", "dev") != args.split:
            continue
        for c in cits:
            pending.append((sid, c, (c["cited_pub_resolved"] or c["citation_raw"] or "").strip()))
    states = textstate.fetch([p for _, _, p in pending])

    rows = []
    for sid, c, pub in pending:
        st = states.get(pub) or {}
        key = (sid, c["gold_family_id"])
        f = staged.get(key)
        rows.append({
            "subject_id": sid,
            "corpus_stratum": (subs[sid].get("strata") or {}).get("corpus") or "pinned",
            "gold_family_id": c["gold_family_id"],
            "cited_pub": pub,
            "authority": st.get("authority") or textstate.authority_of(pub),
            "in_corpus_flag": c["in_corpus"],
            "text_state": st.get("state", "absent"),
            "para_chars": st.get("para_chars", 0),
            "total_chars": st.get("total_chars", 0),
            "n_claim_chunks": st.get("n_claim", 0),
            #  A reference with no report is not attributed; say so rather than defaulting it into
            #  NOT_RETRIEVED, which would inflate the very cell this table exists to interrogate.
            "stage": (f or {}).get("stage", "NO_RUN"),
        })

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    scored = [r for r in rows if r["stage"] != "NO_RUN"]
    print(f"tag {args.tag}  split {args.split}  {len(rows)} gold references, "
          f"{len(scored)} attributed by a finished run\n")

    #  The matrix. Stages down, text state across, because the question is "given what we held,
    #  where did it die".
    states_seen = [s for s in textstate.ORDER
                   if any(r["text_state"] == s for r in scored)]
    stages_seen = sorted({r["stage"] for r in scored},
                         key=lambda s: -sum(1 for r in scored if r["stage"] == s))
    w1 = max(len(s) for s in stages_seen) + 1
    print(" " * w1 + "".join(f"{s[:13]:>14s}" for s in states_seen) + f"{'total':>9s}")
    grid = collections.Counter((r["stage"], r["text_state"]) for r in scored)
    for stg in stages_seen:
        n = sum(1 for r in scored if r["stage"] == stg)
        print(f"{stg:<{w1}s}" + "".join(f"{grid[(stg, s)]:>14d}" for s in states_seen)
              + f"{n:>9d}")
    print(" " * w1 + "".join(
        f"{sum(1 for r in scored if r['text_state'] == s):>14d}" for s in states_seen)
        + f"{len(scored):>9d}")

    #  The two readings that decide what this table means.
    print("\nreadable = partial_description or full_description_and_claims")
    for lab, sel in (("READABLE text held", [r for r in scored
                                             if r["text_state"] in textstate.READABLE]),
                     ("NOT readable", [r for r in scored
                                       if r["text_state"] not in textstate.READABLE])):
        n = len(sel) or 1
        d = sum(1 for r in sel if r["stage"] == tr.TOP_50)
        rc = sum(1 for r in sel if r["stage"] in REACH)
        rk = sum(1 for r in sel if r["stage"] in RANKING)
        print(f"  {lab:<20s} n={len(sel):<4d} delivered {d:>3d} ({d / n:>4.0%})   "
              f"lost to reach {rc:>3d} ({rc / n:>4.0%})   lost to ranking {rk:>3d} ({rk / n:>4.0%})")

    #  THE SPLIT THAT DECIDES WHAT ACQUISITION IS WORTH. NOT_RETRIEVED is the largest stage and it
    #  is two different failures wearing one name: art we do not hold at all, and art we DO hold
    #  and did not find. Only the first is an acquisition problem.
    nr = [r for r in scored if r["stage"] == tr.NOT_RETRIEVED]
    have_nothing = [r for r in nr if r["text_state"] in ("absent", "metadata_only")]
    have_something = [r for r in nr if r["text_state"] not in ("absent", "metadata_only")]
    n = len(nr) or 1
    print(f"\nNOT_RETRIEVED ({len(nr)}) is two different failures:")
    print(f"  we hold NOTHING for it          {len(have_nothing):>4d}  {len(have_nothing) / n:>4.0%}"
          f"   an ACQUISITION failure")
    print(f"  we hold it and did not find it  {len(have_something):>4d}  {len(have_something) / n:>4.0%}"
          f"   a RETRIEVAL failure over text we already have")
    byst = collections.Counter(r["text_state"] for r in have_something)
    print("     of which: " + ", ".join(f"{k} {v}" for k, v in byst.most_common()))

    #  Reconciling with gold_text_coverage.py, which reports 31% readable on the same set. That
    #  tool floors on TOTAL characters and this one on DESCRIPTION characters, so a document with
    #  substantial claims and no description is readable there and claims_only here. Both are
    #  right for their question; printing the claims-inclusive cut so neither has to be inferred.
    incl = textstate.READABLE | {"claims_only"}
    sel = [r for r in scored if r["text_state"] in incl]
    ns = len(sel) or 1
    d = sum(1 for r in sel if r["stage"] == tr.TOP_50)
    rk = sum(1 for r in sel if r["stage"] in RANKING)
    print(f"\nclaims-inclusive cut (claims are groundable evidence even with no description):")
    print(f"  n={len(sel)} of {len(scored)} ({len(sel) / len(scored):.0%})   delivered {d} ({d / ns:.0%})"
          f"   lost to ranking {rk} ({rk / ns:.0%})")
    print("  gold_text_coverage.py reports 31% on this set; it floors on TOTAL characters, this "
          "table on DESCRIPTION characters.")

    print(f"\nwritten {args.out}")


if __name__ == "__main__":
    main()
