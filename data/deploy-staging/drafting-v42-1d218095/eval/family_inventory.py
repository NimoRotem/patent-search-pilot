"""Plan step 3.2: for art we cannot read, does its FAMILY contain something we could read?

WHY THIS IS THE DIAGNOSTIC THAT SIZES THE ACQUISITION BUILD
-----------------------------------------------------------
A DOCDB simple family is the same invention filed in several offices. If we hold only a German
abstract of a reference, but its US sibling has a full description sitting in a public bulk source,
then the "we cannot read this document" problem is a JOIN we are not doing, not a corpus we have to
buy. If instead the family is German only, no amount of wiring helps and the work is translation
and foreign-language acquisition. Those two conclusions imply completely different projects, and
nothing measured so far separates them.

Output, one row per unreadable gold reference:

    none_needed          a member with readable text is ALREADY in our corpus. Retrieval wiring.
    fetch_english_member a member with English full text exists in a public source. Acquisition.
    foreign_only         members exist, none with English full text. Translation project.
    no_family_data       the family could not be expanded at all.

HARD RULE CARRIED FORWARD. A family member is a RETRIEVAL PROXY. Claims are amended between
offices, so evidence displayed to a user must cite an eligible publication or a text verified
equivalent. `eligibility_check` records whether the chosen member is itself citable at the
subject's priority date; where it is not, the member may be used to FIND the family but not to
prove the disclosure. See `evidence_publication_id` on the chart schema.

COST. Family expansion over patents-public-data is about $0.04. Adding description and claims
lengths scans roughly 1.26 TB, about $7.85, because those columns cannot be pruned by a WHERE.
The expensive pass therefore runs once and materialises to data/families/member_text.csv; later
runs read the cache unless --refresh is given.

    python eval/family_inventory.py --split dev
    python eval/family_inventory.py --split dev --refresh --confirm-spend
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
from funnel import gold_by_subject  # noqa: E402

CACHE = os.path.join(ROOT, "data", "families", "member_text.csv")
#  Languages we can read without a translation project.
ENGLISH = {"en", "EN", "eng"}
#  Below this a "description" is a stub in any language.
DESC_MIN = int(os.environ.get("FAMILY_DESC_MIN", "3000"))

#  Resolve BY PUBLICATION NUMBER as well as by family id. A reference the corpus never held has no
#  DOCDB family locally, so eval/benchmark_gold.py records the placeholder `ext:<pub>`. Expanding
#  only real family ids silently drops exactly the references this diagnostic exists to size: on
#  the first run that was 75 of 309, and 36 of the 47 in the mostly_out stratum.
MEMBER_Q = """
WITH targets AS (
  SELECT DISTINCT CAST(family_id AS STRING) fid
  FROM `patents-public-data.patents.publications`
  WHERE publication_number IN UNNEST(@pubs)
     OR CAST(family_id AS STRING) IN UNNEST(@fams)
)
SELECT
  CAST(p.family_id AS STRING) family_id,
  p.publication_number,
  p.country_code,
  CAST(p.publication_date AS STRING) publication_date,
  (SELECT STRING_AGG(CONCAT(language, ':', CAST(LENGTH(text) AS STRING)), '|')
     FROM UNNEST(p.description_localized)) desc_langs,
  (SELECT STRING_AGG(CONCAT(language, ':', CAST(LENGTH(text) AS STRING)), '|')
     FROM UNNEST(p.claims_localized)) claim_langs
FROM `patents-public-data.patents.publications` p
JOIN targets t ON CAST(p.family_id AS STRING) = t.fid
"""


def bq_members(family_ids, pubs=(), confirm_spend=False):
    from google.cloud import bigquery
    c = bigquery.Client(project="nimo-gpt")
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("fams", "STRING", sorted(family_ids)),
        bigquery.ArrayQueryParameter("pubs", "STRING", sorted(set(pubs)))])
    dry = bigquery.QueryJobConfig(query_parameters=cfg.query_parameters,
                                  dry_run=True, use_query_cache=False)
    est = c.query(MEMBER_Q, job_config=dry).total_bytes_processed
    usd = est / 1e12 * 6.25
    print(f"[bq] {len(family_ids)} families; would scan {est / 1e9:,.0f} GB, about ${usd:,.2f}")
    if usd > 25 and not confirm_spend:
        raise SystemExit("refusing to spend over $25 without --confirm-spend")
    rows = [dict(r) for r in c.query(MEMBER_Q, job_config=cfg).result()]
    print(f"[bq] {len(rows)} family members returned")
    return rows


def best_lang_len(spec):
    """'de:41000|en:120' -> {'de': 41000, 'en': 120}"""
    out = {}
    for part in (spec or "").split("|"):
        if ":" in part:
            lang, _, n = part.rpartition(":")
            try:
                out[lang.strip().lower()] = max(out.get(lang.strip().lower(), 0), int(n))
            except ValueError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=("all", "dev", "holdout"))
    ap.add_argument("--refresh", action="store_true", help="re-run the expensive BigQuery pass")
    ap.add_argument("--confirm-spend", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "logs", "family_inventory.csv"))
    args = ap.parse_args()

    subs = {s["id"]: s for s in
            json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]}
    gold = gold_by_subject()

    pending = []
    for sid, cits in sorted(gold.items()):
        sub = subs.get(sid) or {}
        if args.split != "all" and sub.get("split", "dev") != args.split:
            continue
        for c in cits:
            pending.append((sid, c, (c["cited_pub_resolved"] or c["citation_raw"] or "").strip()))

    local = textstate.fetch([p for _, _, p in pending])
    #  Target: everything we cannot already read in full. A reference we hold complete needs no
    #  family work, and including it would flatter the result.
    targets = [(sid, c, p) for sid, c, p in pending
               if (local.get(p) or {}).get("state") != "full_description_and_claims"]
    fams = {c["gold_family_id"] for _, c, _ in targets
            if c["gold_family_id"] and not c["gold_family_id"].startswith("ext:")}
    #  Placeholders carry no family, so they must be resolved by publication number instead.
    ext_pubs = {p for _, c, p in targets if (c["gold_family_id"] or "").startswith("ext:")}
    print(f"{len(pending)} gold references, {len(targets)} not fully readable, "
          f"{len(fams)} real families + {len(ext_pubs)} placeholder references to resolve by "
          f"publication number")

    if args.refresh or not os.path.exists(CACHE):
        rows = bq_members(fams, pubs=ext_pubs, confirm_spend=args.confirm_spend)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["family_id", "publication_number", "country_code",
                                               "publication_date", "desc_langs", "claim_langs"])
            w.writeheader()
            w.writerows(rows)
        print(f"[bq] cached to {CACHE}")
    else:
        rows = list(csv.DictReader(open(CACHE)))
        print(f"[bq] {len(rows)} members read from cache {CACHE} (use --refresh to re-query)")

    by_fam = collections.defaultdict(list)
    for r in rows:
        by_fam[str(r["family_id"])].append(r)

    #  Do we already HOLD a sibling with readable text? That is the cheapest possible fix and it
    #  needs no acquisition at all, so it has to be checked before concluding anything is missing.
    local_members = textstate.fetch([r["publication_number"] for r in rows])

    out_rows, fixes = [], collections.Counter()
    #  publication -> family, so a placeholder row can find the family it belongs to.
    fam_of_pub = {}
    for r in rows:
        fam_of_pub[textstate._norm(r["publication_number"])] = str(r["family_id"])

    for sid, c, pub in targets:
        fam = c["gold_family_id"]
        if str(fam).startswith("ext:"):
            fam = fam_of_pub.get(textstate._norm(pub), fam)
        members = by_fam.get(str(fam), [])
        efd = (c.get("subject_efd") or "").strip()

        held, english, foreign = [], [], []
        for m in members:
            mp = m["publication_number"]
            st = (local_members.get(mp) or {}).get("state", "absent")
            d = best_lang_len(m.get("desc_langs"))
            cl = best_lang_len(m.get("claim_langs"))
            en_chars = max(d.get("en", 0), 0)
            any_chars = max(list(d.values()) or [0])
            rec = {"pub": mp, "auth": (m.get("country_code") or mp[:2]),
                   "date": (m.get("publication_date") or "")[:10],
                   "local_state": st, "en_desc": en_chars, "any_desc": any_chars,
                   "langs": ",".join(sorted(d)) or "-", "claims": max(list(cl.values()) or [0])}
            if st in textstate.READABLE:
                held.append(rec)
            elif en_chars >= DESC_MIN:
                english.append(rec)
            elif any_chars >= DESC_MIN:
                foreign.append(rec)

        if held:
            pick, fix = max(held, key=lambda r: r["any_desc"]), "none_needed"
        elif english:
            pick, fix = max(english, key=lambda r: r["en_desc"]), "fetch_english_member"
        elif foreign:
            pick, fix = max(foreign, key=lambda r: r["any_desc"]), "foreign_only"
        elif members:
            pick, fix = None, "foreign_only"
        else:
            pick, fix = None, "no_family_data"
        fixes[fix] += 1

        #  Citable only if the member published on or before the subject's earliest filing date.
        elig = "unknown"
        if pick and efd:
            elig = "citable" if pick["date"] and pick["date"] <= efd else "NOT_citable_as_art"

        out_rows.append({
            "subject_id": sid, "cited_pub": pub,
            "cited_text_state": (local.get(pub) or {}).get("state", "absent"),
            "family_id": fam, "n_members": len(members),
            "best_member": (pick or {}).get("pub", ""),
            "member_authority": (pick or {}).get("auth", ""),
            "member_date": (pick or {}).get("date", ""),
            "member_text_state": (pick or {}).get("local_state", ""),
            "member_en_desc_chars": (pick or {}).get("en_desc", 0),
            "member_any_desc_chars": (pick or {}).get("any_desc", 0),
            "member_langs": (pick or {}).get("langs", ""),
            "eligibility_check": elig,
            "estimated_fix": fix,
        })

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)

    n = len(out_rows) or 1
    print(f"\n{n} unreadable gold references, by what would fix them\n")
    for k in ("none_needed", "fetch_english_member", "foreign_only", "no_family_data"):
        print(f"  {k:<22s} {fixes[k]:>4d}  {fixes[k] / n:>5.1%}")

    print("\nby corpus stratum:")
    strat = collections.defaultdict(collections.Counter)
    for r in out_rows:
        strat[(subs[r["subject_id"]].get("strata") or {}).get("corpus") or "pinned"][
            r["estimated_fix"]] += 1
    for st in ("mostly_in", "mixed", "mostly_out", "pinned"):
        c = strat.get(st)
        if not c:
            continue
        tot = sum(c.values())
        print(f"  {st:<12s} " + "  ".join(
            f"{k.split('_')[0]}={c[k]:>3d}" for k in
            ("none_needed", "fetch_english_member", "foreign_only", "no_family_data"))
            + f"   n={tot}")

    elig = collections.Counter(r["eligibility_check"] for r in out_rows)
    print(f"\neligibility of the chosen member: {dict(elig)}")
    print("  a member that is NOT citable may be used to FIND the family, never as displayed "
          "evidence (see evidence_publication_id).")
    print(f"\nwritten {args.out}")


if __name__ == "__main__":
    main()
