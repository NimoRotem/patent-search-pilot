"""Fetch full text for the frozen acquisition cohort, batch one, and publish the funnel.

Plan step 4 asked for per-stage counters on the acquisition path. This is that funnel for a batch
fetch rather than the live materialisation path, and every stage is counted so a shortfall names
itself instead of showing up later as "acquisition did not help".

    cohort families (batch 1)
      -> fetch target publications          one per family, English-authority preferred
      -> requested from the source
      -> returned by the source
      -> carries a description at all
      -> carries an ENGLISH description
      -> description >= MIN_CHARS           below this it is an excerpt, not a document
      -> written to disk

SOURCE. BigQuery patents-public-data, which holds description_localized and claims_localized. One
query costs about $8 because the description column cannot be pruned by a WHERE, and it replaces
several thousand rate-limited HTTP calls. Plan step 5.1 says to MEASURE what fraction of our need
that mirror actually serves rather than assume it, so the funnel reports coverage per authority.

Writes data/acquire/batch1.jsonl (one document per line) and data/logs/acquisition_funnel.json.
Nothing is written to any database here: ops/acquire_load.py does that, into 5434 only.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "eval"))

from acquisition_cohort import FROZEN  # noqa: E402

OUT = os.path.join(ROOT, "data", "acquire")
MIN_CHARS = int(os.environ.get("ACQUIRE_MIN_CHARS", "3000"))

Q = """
SELECT
  publication_number,
  country_code,
  CAST(family_id AS STRING) family_id,
  (SELECT text FROM UNNEST(description_localized)
    WHERE language = 'en' LIMIT 1) desc_en,
  (SELECT text FROM UNNEST(description_localized) ORDER BY LENGTH(text) DESC LIMIT 1) desc_best,
  (SELECT language FROM UNNEST(description_localized)
    ORDER BY LENGTH(text) DESC LIMIT 1) desc_best_lang,
  (SELECT text FROM UNNEST(claims_localized) WHERE language = 'en' LIMIT 1) claims_en,
  (SELECT text FROM UNNEST(claims_localized) ORDER BY LENGTH(text) DESC LIMIT 1) claims_best
FROM `patents-public-data.patents.publications`
WHERE publication_number IN UNNEST(@pubs)
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="0 = the whole batch")
    ap.add_argument("--confirm-spend", action="store_true")
    args = ap.parse_args()

    rec = json.load(open(FROZEN))
    members = [m for m in rec["members"] if m["batch"] == args.batch]
    if args.limit:
        members = members[:args.limit]
    pubs = sorted({m["fetch_pub"] for m in members if m["fetch_pub"]})
    fam_of = {m["fetch_pub"]: m["family_id"] for m in members}
    auth_of = {m["fetch_pub"]: m["authority"] for m in members}

    funnel = collections.Counter()
    funnel["cohort_families"] = len(members)
    funnel["fetch_target_publications"] = len(pubs)
    print(f"cohort {rec['cohort_version']} batch {args.batch}: "
          f"{len(members):,} families -> {len(pubs):,} target publications")

    from google.cloud import bigquery
    c = bigquery.Client(project="nimo-gpt")
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("pubs", "STRING", pubs)])
    dry = bigquery.QueryJobConfig(query_parameters=cfg.query_parameters,
                                  dry_run=True, use_query_cache=False)
    est = c.query(Q, job_config=dry).total_bytes_processed
    usd = est / 1e12 * 6.25
    print(f"[bq] would scan {est / 1e9:,.0f} GB, about ${usd:,.2f}")
    if usd > 25 and not args.confirm_spend:
        raise SystemExit("refusing to spend over $25 without --confirm-spend")

    funnel["requested"] = len(pubs)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"batch{args.batch}.jsonl")
    by_auth = collections.defaultdict(collections.Counter)
    lens = []

    with open(path, "w") as fh:
        for r in c.query(Q, job_config=cfg).result():
            pub = r["publication_number"]
            a = auth_of.get(pub) or (r["country_code"] or "??")
            funnel["returned"] += 1
            by_auth[a]["returned"] += 1

            desc = r["desc_en"] or ""
            lang = "en"
            if not desc:
                desc = r["desc_best"] or ""
                lang = r["desc_best_lang"] or "?"
            claims = r["claims_en"] or r["claims_best"] or ""

            if desc:
                funnel["has_any_description"] += 1
                by_auth[a]["has_any_description"] += 1
            if r["desc_en"]:
                funnel["has_english_description"] += 1
                by_auth[a]["has_english_description"] += 1
            if len(desc) >= MIN_CHARS:
                funnel["description_over_min"] += 1
                by_auth[a]["description_over_min"] += 1
                lens.append(len(desc))
                fh.write(json.dumps({
                    "publication_number": pub, "family_id": fam_of.get(pub) or r["family_id"],
                    "authority": a, "description": desc, "description_lang": lang,
                    "claims": claims, "n_desc_chars": len(desc),
                    "n_claim_chars": len(claims)}) + "\n")
                funnel["written"] += 1

    print(f"\n{'stage':<28s}{'n':>9s}{'of requested':>14s}")
    req = funnel["requested"] or 1
    for k in ("cohort_families", "fetch_target_publications", "requested", "returned",
              "has_any_description", "has_english_description", "description_over_min",
              "written"):
        print(f"{k:<28s}{funnel[k]:>9,}{funnel[k] / req:>13.1%}")

    print(f"\nby authority (coverage of the mirror, measured not assumed):")
    print(f"{'auth':<7s}{'targets':>9s}{'returned':>10s}{'any desc':>10s}{'english':>9s}"
          f"{'>= min':>9s}")
    tgt = collections.Counter(auth_of.values())
    for a in sorted(by_auth, key=lambda x: -tgt[x]):
        b = by_auth[a]
        print(f"{a:<7s}{tgt[a]:>9,}{b['returned']:>10,}{b['has_any_description']:>10,}"
              f"{b['has_english_description']:>9,}{b['description_over_min']:>9,}")

    if lens:
        lens.sort()
        print(f"\ndescription length: median {lens[len(lens) // 2]:,} chars, "
              f"p10 {lens[len(lens) // 10]:,}, p90 {lens[9 * len(lens) // 10]:,}, "
              f"max {lens[-1]:,}")

    os.makedirs(os.path.join(ROOT, "data", "logs"), exist_ok=True)
    json.dump({"cohort_version": rec["cohort_version"], "batch": args.batch,
               "min_chars": MIN_CHARS, "funnel": dict(funnel),
               "by_authority": {a: dict(b) for a, b in by_auth.items()},
               "bytes_scanned": est, "usd": round(usd, 2), "output": path},
              open(os.path.join(ROOT, "data", "logs", "acquisition_funnel.json"), "w"), indent=1)
    print(f"\nwritten {path}")
    print("nothing was loaded into any database; ops/acquire_load.py does that, into 5434 only.")


if __name__ == "__main__":
    main()
