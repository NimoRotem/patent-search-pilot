"""Size the acquisition cohort from CANDIDATE STATUS ALONE. No gold is read here, deliberately.

Plan rule R1: a cohort membership rule may not reference gold status. The plan's own wording for
rule (a) says "families where 3.2 found an eligible English-text family member", and 3.2 was run
over gold, so taking that literally would define the cohort by the answer key. The honest reading,
and the one implemented, is to apply the SAME family-substitution logic to the general candidate
pool: every family the dev runs actually surfaced, whether or not an examiner ever cited it.

Three general rules, each expressible without the word gold:

  A  a family in a dev run's SCREENED candidate set for which we hold no readable description,
     and whose DOCDB family contains a member with English full text we do not hold.
  B  an external candidate that survived rescore and is not held locally at all.
  C  a locally held stub that the screen scored at or above a threshold.

This script only measures how big each is. The cap and the thresholds are chosen from these
numbers and then frozen, before any gold overlap is computed.
"""
import collections
import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "eval")

import db  # noqa: E402
import textstate  # noqa: E402

TAG = os.environ.get("TAG", "v15")
subs = json.load(open("eval/benchmark_subjects.json"))["subjects"]
dev = [s["id"] for s in subs if s.get("split", "dev") == "dev"]

screened, ranked_only, screen_of = collections.Counter(), collections.Counter(), {}
n_reports = 0
for sid in dev:
    p = f"data/reports/bench-{sid}-{TAG}.json"
    if not os.path.exists(p):
        continue
    rep = json.load(open(p))
    dr = rep.get("deep_rank") or {}
    if not (dr.get("order") or dr.get("screen_scores")):
        continue
    n_reports += 1
    for f in (dr.get("candidate_families") or []):
        screened[str(f)] += 1
    for f in (rep.get("ranked_families") or []):
        ranked_only[str(f)] += 1
    by_pub = dr.get("by_pub") or {}
    for pub, sc in (dr.get("screen_scores") or {}).items():
        fam = str((by_pub.get(pub) or {}).get("family") or "")
        try:
            v = float(sc)
        except (TypeError, ValueError):
            continue
        if fam:
            screen_of[fam] = max(screen_of.get(fam, 0.0), v)

print(f"{n_reports} finished dev runs at tag {TAG}")
print(f"  distinct families ever RANKED   : {len(ranked_only):,}")
print(f"  distinct families SCREENED      : {len(screened):,}")
print(f"  families with a screen score    : {len(screen_of):,}")

#  How many screened families do we hold readable text for? Resolve each family to one
#  representative publication first: a family key is either a DOCDB id we hold, or (for external
#  candidates) the canonical publication number itself.
fams = list(screened)
rep_pub, unheld_keys = {}, []
CH = 800
for i in range(0, len(fams), CH):
    batch = fams[i:i + CH]
    with db.cursor() as cur:
        cur.execute("""
            SELECT coalesce(nullif(p.simple_family_id,''), p.publication_number) fam,
                   p.publication_number pub,
                   (SELECT coalesce(sum(length(ch.text)),0) FROM chunks ch
                     WHERE ch.publication_id = p.id AND ch.kind='paragraph') para,
                   (SELECT count(*) FROM chunks ch
                     WHERE ch.publication_id = p.id AND ch.kind LIKE 'claim%%') ncl
              FROM publications p
             WHERE coalesce(nullif(p.simple_family_id,''), p.publication_number) = ANY(%s)""",
                    (batch,))
        for r in cur.fetchall():
            cur_best = rep_pub.get(r["fam"])
            if not cur_best or (r["para"], r["ncl"]) > (cur_best["para"], cur_best["ncl"]):
                rep_pub[r["fam"]] = {"pub": r["pub"], "para": int(r["para"]),
                                     "ncl": int(r["ncl"])}
    for f in batch:
        if f not in rep_pub:
            unheld_keys.append(f)

READABLE_PARA = 6000
held_readable = [f for f, v in rep_pub.items() if v["para"] >= READABLE_PARA]
held_stub = [f for f, v in rep_pub.items() if v["para"] < READABLE_PARA]
print(f"\nof the {len(screened):,} SCREENED families:")
print(f"  held with a real description (>= {READABLE_PARA} chars) : {len(held_readable):,}"
      f"  {len(held_readable) / max(1, len(screened)):.1%}")
print(f"  held as a stub                                    : {len(held_stub):,}"
      f"  {len(held_stub) / max(1, len(screened)):.1%}")
print(f"  not held at all (external candidate keys)          : {len(unheld_keys):,}"
      f"  {len(unheld_keys) / max(1, len(screened)):.1%}")

print("\nrule C sizing: held stubs by screen score")
for thr in (0, 40, 50, 60, 70, 75, 80):
    n = sum(1 for f in held_stub if screen_of.get(f, -1) >= thr)
    print(f"  screen >= {thr:<3d}: {n:,}")

print("\nrule A/B sizing: how many distinct families appear in more than one subject's pool")
for k in (1, 2, 3):
    n = sum(1 for f, c in screened.items() if c >= k)
    print(f"  in >= {k} dev subject pools: {n:,}")

out = {"tag": TAG, "n_reports": n_reports,
       "screened_families": len(screened), "ranked_families": len(ranked_only),
       "held_readable": len(held_readable), "held_stub": len(held_stub),
       "not_held": len(unheld_keys),
       "stub_by_screen": {str(t): sum(1 for f in held_stub if screen_of.get(f, -1) >= t)
                          for t in (0, 40, 50, 60, 70, 75, 80)}}
os.makedirs("data/logs", exist_ok=True)
json.dump(out, open("data/logs/cohort_sizing.json", "w"), indent=1)
print("\nwritten data/logs/cohort_sizing.json  (no gold was read to produce this)")
