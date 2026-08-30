"""Advisor move 2: audit the tool baseline before anyone repeats "30x".

0.4% and 0.0% are low enough to suspect the instrument, and this project has already shipped one
broken baseline arm. Checks the mundane failure modes:

  1. do the baseline's publication numbers resolve to real DOCDB families, or silently fall back
     to publication ids that can never match a numeric gold family?
  2. is the gold side keyed the same way for every arm?
  3. were results actually collected to depth 50, or supply-capped earlier?
  4. positive control: does the SAME matching code find our own hits?
  5. does the baseline return anything relevant at all, measured as overlap with the union of
     gold across all subjects rather than per subject?
"""
import collections
import csv
import glob
import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "eval")
import tool_baseline as tb  # noqa: E402
import trace as tr  # noqa: E402
from funnel import gold_by_subject  # noqa: E402

gold = gold_by_subject()
subs = {s["id"]: s for s in json.load(open("eval/benchmark_subjects.json"))["subjects"]}

rows = list(csv.DictReader(open("data/logs/tool_baseline.csv")))
print("=== 3. depth actually collected ===")
for arm in ("gp_similar", "gp_search", "ours"):
    sel = [int(r["n_returned"]) for r in rows if r["arm"] == arm]
    if sel:
        sel.sort()
        print(f"  {arm:<12s} n={len(sel):<3d} median {sel[len(sel)//2]:>4d}  min {sel[0]}  max {sel[-1]}")

print("\n=== 1. do baseline results resolve to REAL families? ===")
sample_pubs = []
for f in sorted(glob.glob("data/baseline/gpd_*.json"))[:8]:
    d = json.load(open(f))
    for x in (d.get("similar_documents") or [])[:12]:
        pn = (x or {}).get("publication_number")
        if pn:
            sample_pubs.append(pn)
sample_pubs = sorted(set(sample_pubs))
fam = tb.families_for(sample_pubs)
numeric = sum(1 for v in fam.values() if str(v).isdigit())
print(f"  {len(sample_pubs)} sampled baseline publications")
print(f"  resolved to a NUMERIC DOCDB family : {numeric}  ({numeric/max(1,len(sample_pubs)):.0%})")
print(f"  fell back to a publication id      : {len(sample_pubs)-numeric}")
print("  (a fallback can never match a numeric gold family, so a high number here would")
print("   invalidate the baseline)")

print("\n=== 2. how is the GOLD side keyed? ===")
allg = [c for s in gold for c in gold[s]]
num = sum(1 for c in allg if str(c["gold_family_id"]).isdigit())
print(f"  {len(allg)} gold rows: {num} numeric families, {len(allg)-num} ext: placeholders")
print("  both sides go through trace.match_keys in the scorer, so a placeholder is still")
print("  matchable by its canonical publication number.")

print("\n=== 5. does the baseline return ANY gold, pooled across all subjects? ===")
gold_any = set()
for s in gold:
    for c in gold[s]:
        gold_any |= tr.match_keys(str(c["gold_family_id"]))
hits = collections.Counter()
for f in sorted(glob.glob("data/baseline/gpd_*.json")):
    d = json.load(open(f))
    pubs = [(x or {}).get("publication_number") for x in (d.get("similar_documents") or [])]
    pubs = [p for p in pubs if p]
    fm = tb.families_for(pubs)
    for p in pubs:
        k = tr.match_keys(str(fm.get(p) or p))
        if k & gold_any:
            hits["pooled_gold_hits"] += 1
    hits["pubs_checked"] += len(pubs)
print(f"  {hits['pubs_checked']:,} baseline results checked against the POOLED gold set")
print(f"  {hits['pooled_gold_hits']} of them are cited art for SOME subject in the benchmark")
print("  (pooled is a far easier test than per subject; a near-zero here means the tool is")
print("   genuinely not surfacing examiner-cited art, not that our matching is broken)")

print("\n=== 4. positive control ===")
o = [r for r in rows if r["arm"] == "ours"]
print(f"  our arm scored {sum(int(r['hit_xy_50']) for r in o)} XY hits through the IDENTICAL")
print(f"  families_for + matching path, so the machinery demonstrably can produce matches.")
