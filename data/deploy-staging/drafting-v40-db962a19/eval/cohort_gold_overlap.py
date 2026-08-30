"""How much gold does the frozen acquisition cohort happen to cover? An OUTCOME, not an input.

Order matters and is the point. eval/acquisition_cohort.py was written, run and COMMITTED before
this file existed, and its record carries gold_was_read: false with a content hash over the member
list. So this measurement cannot have influenced the cohort, and anyone can check that by the
commit order rather than by trusting the claim.

Two numbers matter and they pull in opposite directions:

  COVERAGE    of the gold references we currently cannot read, how many would receive text if the
              cohort were fetched? This is the ceiling on what the acquisition arm can possibly buy
              on this benchmark.
  DILUTION    what share of the cohort is gold at all? A cohort chosen honestly on candidate status
              should be overwhelmingly NOT gold. If most of it were gold, the rules would have
              been the answer key wearing a disguise, whatever the commit order says.

    python eval/cohort_gold_overlap.py
"""
from __future__ import annotations

import collections
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import trace as tr  # noqa: E402
from acquisition_cohort import FROZEN  # noqa: E402
from funnel import gold_by_subject  # noqa: E402


def code_class(code):
    c = (code or "").upper()
    return "X" if "X" in c else ("Y" if "Y" in c else "A")


def main():
    if not os.path.exists(FROZEN):
        raise SystemExit(f"cohort not frozen: {FROZEN}")
    rec = json.load(open(FROZEN))
    assert rec.get("gold_was_read") is False, "the cohort record claims gold was read"
    members = rec["members"]
    print(f"cohort {rec['cohort_version']}  hash {rec['content_hash'][:12]}  "
          f"{len(members):,} families to fetch\n")

    #  Match through the same key ladder the funnel uses. Gold records a familyless reference as
    #  ext:<pub> while the pipeline writes the bare canonical number, and comparing those with raw
    #  equality silently reports zero overlap.
    idx = {}
    for m in members:
        for k in tr.match_keys(m["family_id"]):
            idx[k] = m

    subs = {s["id"]: s for s in
            json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]}
    gold = gold_by_subject()

    #  The text state each gold reference is in today, from step 3.1's output.
    tsp = os.path.join(ROOT, "data", "logs", "funnel", "funnel_by_text_state.csv")
    state = {}
    if os.path.exists(tsp):
        for r in csv.DictReader(open(tsp)):
            state[(r["subject_id"], r["cited_pub"])] = r

    rows, hit_fams = [], set()
    for sid, cits in sorted(gold.items()):
        if (subs.get(sid) or {}).get("split", "dev") != "dev":
            continue
        for c in cits:
            fam = str(c["gold_family_id"])
            m = None
            for k in tr.match_keys(fam):
                m = idx.get(k)
                if m:
                    break
            pub = (c["cited_pub_resolved"] or c["citation_raw"] or "").strip()
            st = (state.get((sid, pub)) or {})
            rows.append({"subject_id": sid, "cited_pub": pub, "family_id": fam,
                         "code": code_class(c["citation_code"]),
                         "stratum": (subs[sid].get("strata") or {}).get("corpus") or "pinned",
                         "text_state": st.get("text_state", "?"),
                         "stage": st.get("stage", "?"),
                         "in_cohort": bool(m),
                         "fetch_pub": (m or {}).get("fetch_pub", ""),
                         "rules": (m or {}).get("rules", "")})
            if m:
                hit_fams.add(m["family_id"])

    n = len(rows) or 1
    covered = [r for r in rows if r["in_cohort"]]
    print(f"{n} eligible dev gold references")
    print(f"  in the frozen cohort: {len(covered)}  {len(covered) / n:.1%}\n")

    UNREADABLE = {"absent", "metadata_only", "title_only", "abstract_only", "claims_only",
                  "partial_description"}
    unread = [r for r in rows if r["text_state"] in UNREADABLE]
    uc = [r for r in unread if r["in_cohort"]]
    print(f"of the {len(unread)} gold references we currently CANNOT read:")
    print(f"  would receive text from the cohort: {len(uc)}  "
          f"{len(uc) / max(1, len(unread)):.1%}   <- ceiling for the acquisition arm\n")

    print("by citation class (X and Y are what the headline metric counts):")
    for k in ("X", "Y", "A"):
        sel = [r for r in rows if r["code"] == k]
        c = sum(1 for r in sel if r["in_cohort"])
        print(f"  {k}: {c:>3d} of {len(sel):>3d}  {c / max(1, len(sel)):>5.1%}")

    print("\nby corpus stratum:")
    for st in ("mostly_in", "mixed", "mostly_out", "pinned"):
        sel = [r for r in rows if r["stratum"] == st]
        if not sel:
            continue
        c = sum(1 for r in sel if r["in_cohort"])
        print(f"  {st:<12s} {c:>3d} of {len(sel):>3d}  {c / len(sel):>5.1%}")

    print("\nby the stage the reference died at today:")
    byst = collections.Counter((r["stage"], r["in_cohort"]) for r in rows)
    for stage in sorted({r["stage"] for r in rows}):
        yes, no = byst[(stage, True)], byst[(stage, False)]
        print(f"  {stage:<26s} in cohort {yes:>3d} / {yes + no:>3d}")

    print(f"\nDILUTION: {len(hit_fams)} of {len(members):,} cohort families are gold "
          f"({len(hit_fams) / max(1, len(members)):.2%}). "
          f"{100 - 100 * len(hit_fams) / max(1, len(members)):.1f}% of the fetch is art no "
          f"examiner in this benchmark ever cited, which is what an honest cohort looks like.")

    out = os.path.join(ROOT, "data", "logs", "cohort_gold_overlap.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwritten {out}")


if __name__ == "__main__":
    main()
