"""Widened-field evaluation gold set — the instrument that makes corpus expansion measurable.

WHY A SECOND GOLD SET
---------------------
`goldset.py` covers ONE field: vacuum gripping, eight CPC subgroups, eleven queries. It cannot
measure a corpus widened to whole subclasses. A change that improved B65G retrieval while diluting
B66C would score *identically* on it, and the M9 lesson in this repo is precisely that a
retrieval-layer change can look like an improvement and be a 26% recall regression. Widening the
corpus without widening the benchmark means flying blind.

This set is built the same way — CLEF-IP methodology: an anchor's citations resolved to DOCDB
simple families are the relevance judgments, and the citation edges are recorded so retrieval can
HIDE them and cannot read the answer key. Two things are deliberately different.

1. ANCHORS ARE SELECTED, NOT CURATED
   `goldset.py`'s anchors are hand-picked GRABO / Schmalz / Probst patents. That is right for a
   case-specific benchmark and wrong for measuring general retrieval, because whoever picks them
   knows what the engine is good at. Here every publication meeting fixed, stated criteria is a
   candidate, and anchors are drawn by a deterministic hash of the publication number, stratified
   across the six Tier-1 subclasses. Same input, same anchors, forever — and no judgement of mine
   in the loop.

   The selection is also BLIND TO OUR CORPUS on purpose. Choosing anchors whose cited art we
   happen to hold would manufacture a good score; it is the teaching-to-the-test failure
   `REACHABILITY.md` warns about. Expect the first measured recall on this set to be LOW.

2. GOLD IS EXAMINER CITATIONS ONLY
   `goldset.py` counts every citation edge. Measured on `patents-public-data` (2026-07-29), the
   category mix of all citation rows is:

       APP  47.7%   applicant-submitted (IDS dumps — assert nothing about relevance)
       SEA  24.1%   search report
       PRS  11.5%   prosecution
       ISR   5.4%   international search report
       EXA   0.4%   examiner

   So roughly half of the existing set's "gold" is an attorney's disclosure list. Reproducing that
   is a different task from finding the art an examiner judged relevant, and only the second one is
   what this tool claims to do. Only SEA / ISR / EXA count here.

COST
----
One BigQuery query, ~404 GB ≈ $2.52. It stays cheap by never touching `description_localized`,
which is what makes the full core extract $9.57. Run once; the output is then frozen exactly like
`goldset.json`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import bqclient
from config import DATA

OUT = DATA / "goldset"
OUT.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT / "goldset_wide.json"

# The Tier-1 subclasses the corpus expansion targets. One stratum each, so no single busy
# subclass (F16B is 4.6x B66C) can dominate the macro-average.
SUBCLASSES = ["B25J", "B65G", "B66C", "B66F", "B25B", "F16B"]
PER_SUBCLASS = 6

# Only these citation categories are examiner assertions of relevance. See the module docstring.
EXAMINER_CATS = ("SEA", "ISR", "EXA")

# Anchor admission criteria, all stated up front so the set is reproducible and arguable:
#   * US/EP/WO — the offices that actually publish a search report to score against
#   * 2005-2023 — recent enough to have one, old enough to be fully prosecuted and cited
#   * 8-60 examiner citations — fewer makes recall too coarse to read (one hit = 12.5%), more
#     makes the anchor a landscape rather than a search
#   * title + abstract + claims present — the query is built by example from the anchor's own
#     text, so an anchor without text is not a query
MIN_CITES, MAX_CITES = 8, 60
YEAR_FROM, YEAR_TO = 20050101, 20230101

_CATS_SQL = ",".join(f"'{c}'" for c in EXAMINER_CATS)


def _select_sql() -> str:
    """One query: pick the anchors AND pull their text, stratified, deterministically.

    QUALIFY + ROW_NUMBER does the stratification server-side, so exactly PER_SUBCLASS anchors come
    back per subclass and nothing is filtered client-side (which would make the draw depend on how
    much we happened to fetch). The ordering key is FARM_FINGERPRINT of the publication number:
    stable across runs, uncorrelated with anything we care about, and not my opinion.
    """
    subs = ",".join(f"'{s}'" for s in SUBCLASSES)
    return f"""
WITH cand AS (
  SELECT
    publication_number,
    country_code,
    kind_code,
    --  Dates in this table are INT64 YYYYMMDD, not DATE. goldset.py never hits this because it
    --  reads the `patent_pilot.core` staging table, where ingest_bq already parsed them. Parsing
    --  here keeps both gold sets writing ISO dates, which is what the date engine expects — the
    --  raw integer form reaches Subject() as the string "20171222" and blows up date parsing.
    SAFE.PARSE_DATE('%Y%m%d', CAST(NULLIF(publication_date,0) AS STRING)) AS publication_date,
    SAFE.PARSE_DATE('%Y%m%d', CAST(NULLIF(filing_date,0)      AS STRING)) AS filing_date,
    SAFE.PARSE_DATE('%Y%m%d', CAST(NULLIF(priority_date,0)    AS STRING)) AS priority_date,
    CAST(family_id AS STRING) AS family_id,
    (SELECT x.text FROM UNNEST(title_localized) x
      ORDER BY CASE WHEN x.language='en' THEN 0 ELSE 1 END, LENGTH(x.text) DESC LIMIT 1) AS title,
    (SELECT x.text FROM UNNEST(abstract_localized) x
      ORDER BY CASE WHEN x.language='en' THEN 0 ELSE 1 END, LENGTH(x.text) DESC LIMIT 1) AS abstract,
    (SELECT x.text FROM UNNEST(claims_localized) x
      ORDER BY CASE WHEN x.language='en' THEN 0 ELSE 1 END, LENGTH(x.text) DESC LIMIT 1) AS claims_text,
    ARRAY(SELECT AS STRUCT ci.publication_number AS pub, ci.category AS category, ci.type AS type
          FROM UNNEST(citation) ci
          WHERE ci.category IN ({_CATS_SQL})
            AND ci.publication_number IS NOT NULL AND ci.publication_number != '') AS ex_cites,
    -- The subclass this anchor represents. A publication can carry several of our six; the
    -- lowest-sorting one is taken so each anchor belongs to exactly one stratum and cannot be
    -- drawn twice.
    (SELECT MIN(SUBSTR(c.code, 1, 4)) FROM UNNEST(cpc) c
      WHERE SUBSTR(c.code, 1, 4) IN ({subs})) AS stratum
  FROM `patents-public-data.patents.publications`
  WHERE country_code IN ('US','EP','WO')
    AND publication_date BETWEEN {YEAR_FROM} AND {YEAR_TO}
    AND EXISTS (SELECT 1 FROM UNNEST(cpc) c WHERE SUBSTR(c.code, 1, 4) IN ({subs}))
)
SELECT * FROM cand
WHERE stratum IS NOT NULL
  AND title IS NOT NULL AND abstract IS NOT NULL AND claims_text IS NOT NULL
  AND LENGTH(abstract) > 200 AND LENGTH(claims_text) > 400
  AND ARRAY_LENGTH(ex_cites) BETWEEN {MIN_CITES} AND {MAX_CITES}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY stratum ORDER BY FARM_FINGERPRINT(publication_number)
) <= {PER_SUBCLASS}
"""


# Cited documents are usually outside any corpus we hold, so their families come from a separate
# small-column lookup (cheap: publication_number + family_id only).
_RESOLVE_SQL = """
SELECT publication_number, CAST(family_id AS STRING) AS family_id, country_code
FROM `patents-public-data.patents.publications`
WHERE publication_number IN UNNEST(@pubs)
"""


def _q(sql, **params):
    from google.cloud import bigquery
    qp = [bigquery.ArrayQueryParameter(k, "STRING", v) if isinstance(v, list)
          else bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()]
    cfg = bigquery.QueryJobConfig(query_parameters=qp, maximum_bytes_billed=int(600e9))
    job = bqclient.client().query(sql, job_config=cfg)
    rows = [dict(r) for r in job.result()]
    return rows, (job.total_bytes_billed or 0) / 1e9


def first_claim(claims_text: str) -> str:
    """Claim 1 only. Identical rule to goldset.first_claim so a query built here has the same
    shape as one built for the frozen set — otherwise the two benchmarks would not be comparable
    even on a query they share."""
    if not claims_text:
        return ""
    import re
    return re.split(r"\n\s*2\s*[\.\)]", claims_text, maxsplit=1)[0].strip()[:1500]


def build(dry_run: bool = False):
    sql = _select_sql()
    est = bqclient.dry_run_gb(sql)
    print(f"[wide] anchor selection dry-run ~{est:,.0f} GB (${est / 1000 * 6.25:,.2f})")
    if dry_run:
        return None

    anchors, billed = _q(sql)
    print(f"[wide] BILLED {billed:,.1f} GB — {len(anchors)} anchors "
          f"across {len({a['stratum'] for a in anchors})} subclasses")

    # resolve every examiner-cited publication -> DOCDB simple family
    cited = sorted({c["pub"] for a in anchors for c in a["ex_cites"]})
    fam, tot = {}, 0.0
    for i in range(0, len(cited), 5000):
        rows, b = _q(_RESOLVE_SQL, pubs=cited[i:i + 5000])
        tot += b
        for r in rows:
            fam[r["publication_number"]] = r
    print(f"[wide] resolved {len(fam):,}/{len(cited):,} cited pubs to families "
          f"({tot:,.1f} GB, ${tot / 1000 * 6.25:,.2f})")

    entries = []
    for a in sorted(anchors, key=lambda r: (r["stratum"], r["publication_number"])):
        anchor_fam = str(a["family_id"]) if a["family_id"] else None
        gold, hidden = set(), []
        for c in a["ex_cites"]:
            f = fam.get(c["pub"], {}).get("family_id")
            #  A citation to a sibling in the anchor's OWN family is not prior art against it;
            #  counting it would hand the engine a free hit for retrieving the anchor itself.
            if f and str(f) != anchor_fam:
                gold.add(str(f))
            hidden.append({"src": a["publication_number"], "dst": c["pub"],
                           "category": c.get("category"), "type": c.get("type")})
        if not gold:
            print(f"[wide] skip {a['publication_number']}: no gold family survived", file=sys.stderr)
            continue

        c1 = first_claim(a.get("claims_text") or "")
        query_text = "\n".join(p for p in [a.get("title"), a.get("abstract"),
                                           (f"Claim 1: {c1}" if c1 else "")] if p)
        entries.append(dict(
            id=f"wide_{a['stratum'].lower()}_{a['publication_number'].replace('-', '')}",
            category=f"wide_{a['stratum']}",
            stratum=a["stratum"],
            mode="novelty",
            anchor_publication=a["publication_number"],
            anchor_family=anchor_fam,
            title=a.get("title"),
            notes=(f"Auto-selected anchor for {a['stratum']}; gold = "
                   f"{len(gold)} families from {len(a['ex_cites'])} examiner citations "
                   f"({'/'.join(EXAMINER_CATS)} only)."),
            query_text=query_text,
            subject=dict(
                number=a["publication_number"],
                efd=str(a["priority_date"] or a["filing_date"] or a["publication_date"]),
                filing_date=str(a["filing_date"]) if a["filing_date"] else None,
                publication_date=str(a["publication_date"]) if a["publication_date"] else None,
                jurisdiction=a["country_code"],
                has_claims=bool(c1),
            ),
            gold_families=sorted(gold),
            n_gold_families=len(gold),
            hidden_edges=hidden,
        ))

    doc = {
        "version": "wide-1",
        "built_from": "patents-public-data.patents.publications",
        "method": ("CLEF-IP: examiner citations (SEA/ISR/EXA only) resolved to DOCDB simple "
                   "families. Anchors auto-selected by FARM_FINGERPRINT hash, stratified over "
                   "the Tier-1 subclasses, blind to the local corpus."),
        "subclasses": SUBCLASSES,
        "per_subclass": PER_SUBCLASS,
        "examiner_categories": list(EXAMINER_CATS),
        "criteria": {"jurisdictions": ["US", "EP", "WO"],
                     "publication_date_between": [YEAR_FROM, YEAR_TO],
                     "examiner_citations_between": [MIN_CITES, MAX_CITES]},
        "entries": entries,
    }
    OUT_FILE.write_text(json.dumps(doc, indent=1))
    n_gold = sum(e["n_gold_families"] for e in entries)
    print(f"[wide] wrote {OUT_FILE} — {len(entries)} queries, {n_gold:,} gold families "
          f"({n_gold / max(1, len(entries)):.1f} per query)")
    return doc


def load():
    """Same shape goldset.load() returns, so evaluate.py can score either set unchanged."""
    return json.loads(OUT_FILE.read_text())


if __name__ == "__main__":
    build(dry_run="--dry-run" in sys.argv)
