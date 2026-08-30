"""Advisor move 1: of the documents we READ and CHART, how many ran on a stub?

Charting is where all evidence and all ranking signal come from, and it needs RAW text at read
time, not embedded text. That makes it separable from the corpus problem: only the ~395 documents
per search that reach the read stage need full text, and only at that moment.

This measures the size of the prize before any code changes:
   how many read documents ran on under 3,000 characters
   of those, how many have a public full text we could have fetched
   what that would have cost per search
"""
import collections
import glob
import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "eval")
import textstate  # noqa: E402

TAG = "abc2"
MIN = 3000

reads = []
for p in sorted(glob.glob(f"data/reports/bench-*-{TAG}.json")):
    sid = os.path.basename(p)[len("bench-"):-(len(TAG) + 6)]
    d = json.load(open(p))
    dr = d.get("deep_rank") or {}
    by_pub = dr.get("by_pub") or {}
    for pub in (dr.get("order") or []):
        e = by_pub.get(pub) or {}
        cov = [c for c in (e.get("covered") or [])
               if c.get("verdict") in ("disclosed", "partial")]
        reads.append({"sid": sid, "pub": pub, "chars": int(e.get("chars_read") or 0),
                      "grounded": len(cov)})

n_subj = len({r["sid"] for r in reads})
n = len(reads)
stubs = [r for r in reads if r["chars"] < MIN]
print(f"=== {n:,} documents read in full across {n_subj} searches "
      f"({n/max(1,n_subj):.0f} per search) ===\n")

buckets = [(0, 500), (500, 1500), (1500, 3000), (3000, 10000), (10000, 40000), (40000, 10**9)]
print(f"{'chars read':<22s}{'docs':>8s}{'share':>8s}{'grounded/doc':>14s}")
for lo, hi in buckets:
    sel = [r for r in reads if lo <= r["chars"] < hi]
    if not sel:
        continue
    g = sum(r["grounded"] for r in sel) / len(sel)
    lab = f"{lo:,} to {hi:,}" if hi < 10**9 else f"{lo:,}+"
    print(f"{lab:<22s}{len(sel):>8,}{len(sel)/n:>7.1%}{g:>14.2f}")

print(f"\nread on a STUB (<{MIN:,} chars): {len(stubs):,} of {n:,} = {len(stubs)/n:.1%}"
      f"  ({len(stubs)/max(1,n_subj):.0f} per search)")
gs = sum(r["grounded"] for r in stubs) / max(1, len(stubs))
gf = sum(r["grounded"] for r in reads if r["chars"] >= MIN) / max(1, n - len(stubs))
print(f"  disclosures grounded per document: stub {gs:.2f}  vs  full text {gf:.2f}"
      f"   ({gf/max(gs,0.01):.1f}x)")

#  Of the stubs, how many could we actually have fetched?
uniq = sorted({r["pub"] for r in stubs})
print(f"\nof {len(uniq):,} distinct stub-read publications, checking public availability...")
st = textstate.fetch(uniq)
auth = collections.Counter()
for p in uniq:
    a = (st.get(p) or {}).get("authority") or textstate.authority_of(p)
    auth[a] += 1
fetchable = sum(v for k, v in auth.items() if k in ("US", "EP", "WO"))
print(f"{'authority':<10s}{'docs':>8s}   full text source")
for a, c in auth.most_common(10):
    src = {"US": "BigQuery mirror, measured 99% coverage",
           "EP": "EPO OPS (BigQuery has none)",
           "WO": "EPO OPS (BigQuery has none)"}.get(a, "sibling substitution or commercial")
    print(f"{a:<10s}{c:>8,}   {src}")
print(f"\n=> {fetchable:,} of {len(uniq):,} ({fetchable/max(1,len(uniq)):.0%}) are US/EP/WO")
print(f"=> about {fetchable/max(1,n_subj):.0f} fetches per search to eliminate stub charting "
      f"for those authorities")
