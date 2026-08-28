"""The acquisition cohort, defined by candidate status alone and frozen before any gold is read.

PLAN RULE R1, AND WHY IT IS NOT PEDANTRY
----------------------------------------
"Never fetch, ingest or prioritise a document BECAUSE it is in the gold set." The gold set is
examiner citations we already know. A cohort chosen with one eye on it would produce a treatment
arm that improves on the benchmark and nowhere else, and the improvement would be indistinguishable
from a real one until the holdout ran and failed. Every rule below is expressible without the word
gold, and this module never imports the gold set. The overlap with gold is computed by a SEPARATE
tool, after this file is frozen and committed, and it is reported as an outcome.

The plan's own wording for rule A ("families where 3.2 found an eligible English-text family
member") could not be used literally: step 3.2 ran over gold, so that phrasing defines the cohort
by the answer key. Implemented instead as the same family-substitution logic applied to the general
candidate pool, which is what it was meant to test.

THE RULES

  A  RECURRING CANDIDATE. The family appeared in the screened candidate set of at least
     MIN_SUBJECT_POOLS distinct dev subjects. A document several unrelated searches surface is
     load-bearing in this field regardless of whether anyone cited it.

  C  THE SCREEN THOUGHT IT MATTERED. The family's best screen score is at least MIN_SCREEN. The
     screen is a cheap read of the document against the subject and is computed on every run, so
     it is available for every candidate without new work.

  B  is deliberately EMPTY. The plan anticipated "unique, eligible, not-held external candidates",
     and measurement says there are none: all 51,950 screened families resolve to a publication we
     already hold. External candidates are materialised as stub rows before screening, so this is
     an ENRICHMENT problem, not an insertion one. Recorded rather than quietly dropped.

  EXCLUSION. A family that already holds a member with a real description is removed. Nothing to
  acquire.

Sizing at freeze time (from data/logs/cohort_sizing.json, no gold read): 51,950 screened families,
of which 95.3% are stubs and 4.7% hold a real description.

    python eval/acquisition_cohort.py --freeze
    python eval/acquisition_cohort.py --show
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db  # noqa: E402

COHORT_VERSION = "2026-08-06.1"
FROZEN = os.path.join(ROOT, "data", "cohort", f"cohort_{COHORT_VERSION}.json")

#  Thresholds. Chosen from the sizing run above to give a cohort that free sources can actually
#  serve, and fixed here. They are properties of candidate status, not of the answer key.
MIN_SUBJECT_POOLS = 2      # rule A
MIN_SCREEN = 75.0          # rule C
READABLE_PARA = 6000       # a family holding this much description needs no acquisition
#  Batch one is limited by SOURCE AVAILABILITY, not by relevance: these authorities have free bulk
#  or free-tier full text. The rest are deferred to the cross-lingual and commercial tracks.
BATCH1_AUTHORITIES = ("US", "EP", "WO")


def build(tag="v15"):
    """-> the cohort record. Reads only run artefacts and corpus state."""
    subs = json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]
    dev = [s["id"] for s in subs if s.get("split", "dev") == "dev"]

    pools, screen_of, used = collections.Counter(), {}, []
    for sid in dev:
        p = os.path.join(ROOT, "data", "reports", f"bench-{sid}-{tag}.json")
        if not os.path.exists(p):
            continue
        rep = json.load(open(p))
        dr = rep.get("deep_rank") or {}
        if not (dr.get("order") or dr.get("screen_scores")):
            continue
        used.append(sid)
        for f in (dr.get("candidate_families") or []):
            pools[str(f)] += 1
        by_pub = dr.get("by_pub") or {}
        for pub, sc in (dr.get("screen_scores") or {}).items():
            fam = str((by_pub.get(pub) or {}).get("family") or "")
            try:
                v = float(sc)
            except (TypeError, ValueError):
                continue
            if fam:
                screen_of[fam] = max(screen_of.get(fam, 0.0), v)

    rule_a = {f for f, c in pools.items() if c >= MIN_SUBJECT_POOLS}
    rule_c = {f for f, v in screen_of.items() if v >= MIN_SCREEN}
    union = sorted(rule_a | rule_c)

    members, CH = [], 800
    rows = []
    for i in range(0, len(union), CH):
        batch = union[i:i + CH]
        with db.cursor() as cur:
            cur.execute("""
                SELECT coalesce(nullif(p.simple_family_id,''), p.publication_number) fam,
                       p.publication_number pub, p.country, p.publication_date,
                       (SELECT coalesce(sum(length(ch.text)),0) FROM chunks ch
                         WHERE ch.publication_id = p.id AND ch.kind='paragraph') para
                  FROM publications p
                 WHERE coalesce(nullif(p.simple_family_id,''), p.publication_number) = ANY(%s)""",
                        (batch,))
            rows.extend(dict(r) for r in cur.fetchall())

    per_fam = collections.defaultdict(list)
    for r in rows:
        per_fam[str(r["fam"])].append(r)

    already = 0
    for fam in union:
        ms = per_fam.get(fam) or []
        if not ms:
            continue
        if any(int(m["para"]) >= READABLE_PARA for m in ms):
            already += 1
            continue
        pick = sorted(ms, key=lambda m: (
            0 if (m["country"] or "")[:2] in BATCH1_AUTHORITIES else 1, -int(m["para"])))[0]
        auth = (pick["country"] or (pick["pub"] or "??")[:2])[:2]
        members.append({"family_id": fam, "fetch_pub": pick["pub"], "authority": auth,
                        "in_pools": pools.get(fam, 0),
                        "screen": round(screen_of.get(fam, -1.0), 1),
                        "rules": ("A" if fam in rule_a else "") + ("C" if fam in rule_c else ""),
                        "batch": 1 if auth in BATCH1_AUTHORITIES else 2})

    members.sort(key=lambda m: (m["batch"], -m["screen"], -m["in_pools"], m["family_id"]))
    digest = hashlib.sha256(
        json.dumps([m["family_id"] for m in members], separators=(",", ":")).encode()).hexdigest()
    return {
        "cohort_version": COHORT_VERSION, "tag": tag, "subjects_used": used,
        "rules": {"A_min_subject_pools": MIN_SUBJECT_POOLS, "C_min_screen": MIN_SCREEN,
                  "B": "empty by measurement: every screened family is already held as a row",
                  "exclusion_readable_para_chars": READABLE_PARA,
                  "batch1_authorities": list(BATCH1_AUTHORITIES)},
        "counts": {"rule_a": len(rule_a), "rule_c": len(rule_c), "union": len(union),
                   "already_readable": already, "to_fetch": len(members),
                   "batch1": sum(1 for m in members if m["batch"] == 1),
                   "batch2": sum(1 for m in members if m["batch"] == 2)},
        "by_authority": dict(collections.Counter(m["authority"] for m in members).most_common()),
        "content_hash": digest,
        "gold_was_read": False,
        "members": members,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--refreeze", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--tag", default="v15")
    args = ap.parse_args()

    if args.show:
        if not os.path.exists(FROZEN):
            raise SystemExit(f"not frozen yet: {FROZEN}")
        rec = json.load(open(FROZEN))
        print(json.dumps({k: v for k, v in rec.items() if k != "members"}, indent=1))
        return

    if os.path.exists(FROZEN) and not args.refreeze:
        raise SystemExit(f"already frozen at {FROZEN}. A cohort that moves is not a cohort; "
                         f"use --refreeze deliberately and bump COHORT_VERSION.")

    rec = build(tag=args.tag)
    os.makedirs(os.path.dirname(FROZEN), exist_ok=True)
    with open(FROZEN, "w") as fh:
        json.dump(rec, fh, indent=1)
    print(json.dumps({k: v for k, v in rec.items() if k != "members"}, indent=1))
    print(f"\nfrozen {len(rec['members'])} families to {FROZEN}")
    print("NO GOLD WAS READ. Overlap with gold is an outcome, measured by "
          "eval/cohort_gold_overlap.py after this file is committed.")


if __name__ == "__main__":
    main()
