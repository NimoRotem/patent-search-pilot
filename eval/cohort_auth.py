"""Which authorities the acquisition cohort is made of, and therefore which sources can serve it.

No gold is read. This is candidate status and publication authority only.
"""
import collections
import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "eval")

import db  # noqa: E402

TAG = os.environ.get("TAG", "v15")
subs = json.load(open("eval/benchmark_subjects.json"))["subjects"]
dev = [s["id"] for s in subs if s.get("split", "dev") == "dev"]

screened, screen_of = collections.Counter(), {}
for sid in dev:
    p = f"data/reports/bench-{sid}-{TAG}.json"
    if not os.path.exists(p):
        continue
    rep = json.load(open(p))
    dr = rep.get("deep_rank") or {}
    if not (dr.get("order") or dr.get("screen_scores")):
        continue
    for f in (dr.get("candidate_families") or []):
        screened[str(f)] += 1
    by_pub = dr.get("by_pub") or {}
    for pub, sc in (dr.get("screen_scores") or {}).items():
        fam = str((by_pub.get(pub) or {}).get("family") or "")
        try:
            v = float(sc)
        except (TypeError, ValueError):
            continue
        if fam:
            screen_of[fam] = max(screen_of.get(fam, 0.0), v)

#  Candidate cohort under two general rules, then their union.
rule_a = {f for f, c in screened.items() if c >= 2}                 # recurring across subjects
rule_c = {f for f, v in screen_of.items() if v >= 75}               # screen thought it mattered
union = rule_a | rule_c
print(f"rule A (in >=2 dev pools)      : {len(rule_a):,}")
print(f"rule C (screen >= 75)          : {len(rule_c):,}")
print(f"union                          : {len(union):,}")

#  Which publications would we actually fetch, and from where. Take every family member we hold,
#  because the fetch target may be a sibling rather than the representative row.
fams = list(union)
by_auth = collections.Counter()
targets = collections.Counter()
CH = 800
rows = []
for i in range(0, len(fams), CH):
    batch = fams[i:i + CH]
    with db.cursor() as cur:
        cur.execute("""
            SELECT coalesce(nullif(p.simple_family_id,''), p.publication_number) fam,
                   p.publication_number pub, p.country,
                   (SELECT coalesce(sum(length(ch.text)),0) FROM chunks ch
                     WHERE ch.publication_id = p.id AND ch.kind='paragraph') para
              FROM publications p
             WHERE coalesce(nullif(p.simple_family_id,''), p.publication_number) = ANY(%s)""",
                    (batch,))
        rows.extend(dict(r) for r in cur.fetchall())

per_fam = collections.defaultdict(list)
for r in rows:
    per_fam[r["fam"]].append(r)

READABLE = 6000
need, already = [], 0
for fam, members in per_fam.items():
    if any(int(m["para"]) >= READABLE for m in members):
        already += 1
        continue
    #  Prefer a member whose full text is obtainable in English.
    pref = sorted(members, key=lambda m: (
        0 if (m["country"] or "")[:2] in ("US", "EP", "WO", "GB") else 1,
        -int(m["para"])))
    pick = pref[0]
    need.append(pick)
    by_auth[(pick["country"] or pick["pub"][:2] or "??")[:2]] += 1

print(f"\nfamilies in the union that ALREADY hold a readable member : {already:,}")
print(f"families that would need a fetch                          : {len(need):,}")
print("\nfetch targets by authority (the source that can serve them):")
FREE = {"US": "USPTO bulk / ODP, free",
        "EP": "EPO OPS full text, free tier",
        "WO": "EPO OPS / WIPO, free tier",
        "GB": "EPO OPS (GB in EPO full text is partial)",
        "DE": "DPMA backfile, or an EP/US sibling",
        "JP": "JPO bulk, or an EP/US sibling",
        "CN": "CNIPA / Google Patents",
        "FR": "EPO OPS / INPI"}
for a, n in by_auth.most_common(12):
    print(f"   {a:<4s} {n:>6,}   {FREE.get(a, 'commercial or sibling substitution')}")

covered = sum(n for a, n in by_auth.items() if a in ("US", "EP", "WO"))
print(f"\n=> {covered:,} of {len(need):,} ({covered / max(1, len(need)):.0%}) are US/EP/WO, "
      f"servable from free bulk or free-tier sources")
json.dump({"rule_a": len(rule_a), "rule_c": len(rule_c), "union": len(union),
           "already_readable": already, "need_fetch": len(need),
           "by_authority": dict(by_auth)},
          open("data/logs/cohort_authorities.json", "w"), indent=1)
print("written data/logs/cohort_authorities.json  (no gold was read)")
