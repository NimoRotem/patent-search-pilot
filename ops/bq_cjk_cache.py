#!/usr/bin/env python3
"""Build and inspect the clustered CJK text cache the `bq_cjk` acquisition rung reads.

WHY THE CACHE EXISTS. `patents-public-data.patents.publications` is neither partitioned nor
clustered on the publication number, so looking up ONE publication dry-runs at 228 GB, which is
$1.43 to read one abstract. This script copies the CJK slice into
`nimo-gpt.patents_cache.cjk_text`, CLUSTERED BY the normalised publication number, after which a
lookup prunes to the blocks that can hold the keys. MEASURED 2026-08-22 against the live work
list: a 24-key batch bills BigQuery's 10 MB minimum ($0.00007, $0.000003 a publication) and a
2,000-key CN batch bills 211 MB and returns 2,000 of 2,000.

WHAT IT HOLDS. English title and abstract, the source language, the publication date and the
DOCDB family id, for CN / JP / KR / TW publications that have an English abstract. Censused over
the whole public table on 2026-08-22:

    CN  54,729,311 of 54,743,394   (100.0%)      JP  12,770,474 of 28,175,668   (45.3%)
    KR   3,227,226 of  8,027,935   ( 40.2%)      TW   1,682,074 of  2,595,882   (64.8%)

WHAT IT DOES NOT HOLD, AND WHY THIS SCRIPT WILL NOT PRETEND OTHERWISE. There is no CJK full text
in BigQuery to copy. `claims_localized` and `description_localized` are populated for the United
States and for nobody else: 21,993,541 US descriptions and 18,760,680 US claims against exactly
zero for CN, JP, KR, TW, EP, WO and DE, and the same census against the `publications_201710`
snapshot returns zero too, so it is not a regression that reading an older release would undo.
CJK full text comes from Google Patents (`serp_self`), which is the only rung that has it at
volume. See docs/cjk_acquisition.md.

MONEY. `build` prints the dry-run estimate and the dollar figure and refuses to run without
`--yes`. The build measured on 2026-08-22 scanned 231 GB, which is $1.44 at $6.25/TB and free
inside BigQuery's 1 TB monthly allowance; the resulting table is 72,194,695 rows and 83.5 GB,
about $1.67 a month of active storage. Nothing here writes a Postgres table of any kind.

    ops/bq_cjk_cache.py status
    ops/bq_cjk_cache.py build --yes
    ops/bq_cjk_cache.py probe CN101234567A JP2005312821A
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

PROJECT = os.environ.get("GCP_PROJECT", "nimo-gpt")
TABLE = os.environ.get("BQ_CJK_TABLE", "nimo-gpt.patents_cache.cjk_text")
SOURCE = os.environ.get("BQ_CJK_SOURCE", "patents-public-data.patents.publications")
COUNTRIES = ("CN", "JP", "KR", "TW")
USD_PER_TB = float(os.environ.get("BQ_USD_PER_TB", "6.25"))

BUILD_SQL = """
CREATE OR REPLACE TABLE `{table}`
CLUSTER BY pn_key AS
SELECT
  UPPER(REGEXP_REPLACE(publication_number, r'[^A-Za-z0-9]', '')) AS pn_key,
  publication_number,
  country_code AS country,
  (SELECT text     FROM UNNEST(title_localized)    WHERE language =  'en' LIMIT 1) AS title_en,
  (SELECT text     FROM UNNEST(abstract_localized) WHERE language =  'en' LIMIT 1) AS abstract_en,
  (SELECT language FROM UNNEST(abstract_localized) WHERE language <> 'en' LIMIT 1) AS src_lang,
  publication_date,
  family_id
FROM `{source}`
WHERE country_code IN ({countries})
  AND (SELECT COUNT(*) FROM UNNEST(abstract_localized) WHERE language = 'en') > 0
"""


def _client():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


def _build_sql() -> str:
    return BUILD_SQL.format(table=TABLE, source=SOURCE,
                            countries=", ".join(f"'{c}'" for c in COUNTRIES))


def cmd_status(_args) -> int:
    from google.cloud import bigquery
    cli = _client()
    try:
        t = cli.get_table(TABLE)
    except Exception as exc:
        print(f"{TABLE}: NOT BUILT ({type(exc).__name__}: {str(exc)[:120]})")
        print("run: ops/bq_cjk_cache.py build --yes")
        return 1
    clustering = list(t.clustering_fields or [])
    print(f"{TABLE}")
    print(f"  rows        {t.num_rows:,}")
    print(f"  size        {t.num_bytes / 1e9:,.1f} GB "
          f"(~${t.num_bytes / 1e9 * 0.02:,.2f}/month active storage)")
    print(f"  clustered   {clustering or '(NONE - lookups will scan the whole table)'}")
    if clustering != ["pn_key"]:
        print("  WARNING: a lookup on an unclustered copy of this table scans every byte of it.")
    job = cli.query(f"SELECT country, COUNT(*) n FROM `{TABLE}` GROUP BY 1 ORDER BY n DESC")
    for r in job.result():
        print(f"  {r['country']:2s}          {r['n']:,}")
    print(f"  census cost {job.total_bytes_billed / 1e6:,.1f} MB billed")
    return 0


def cmd_build(args) -> int:
    from google.cloud import bigquery
    cli = _client()
    sql = _build_sql()
    dry = cli.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    gb = dry.total_bytes_processed / 1e9
    print(f"build {TABLE} from {SOURCE}")
    print(f"  dry run   {gb:,.1f} GB  (~${gb / 1000 * USD_PER_TB:,.2f} at ${USD_PER_TB}/TB)")
    if not args.yes:
        print("  refusing to run without --yes. Nothing was spent.")
        return 2
    job = cli.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=int(args.max_gb * 1e9)))
    job.result()
    billed = (job.total_bytes_billed or 0) / 1e9
    print(f"  built     {billed:,.1f} GB billed (~${billed / 1000 * USD_PER_TB:,.2f})")
    return cmd_status(args)


def cmd_probe(args) -> int:
    """A real lookup against the real table, printing what it cost. No mocks anywhere."""
    from google.cloud import bigquery
    from acquire.providers import BigQueryCjkProvider
    keys = [BigQueryCjkProvider._key(p) for p in args.publications]
    cli = _client()
    job = cli.query(
        f"SELECT pn_key, country, src_lang, title_en, abstract_en FROM `{TABLE}` "
        f"WHERE pn_key IN UNNEST(@k)",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("k", "STRING", keys)]))
    rows = list(job.result())
    mb = (job.total_bytes_billed or 0) / 1e6
    print(f"{len(keys)} key(s) -> {len(rows)} hit(s); {mb:,.1f} MB billed "
          f"(${mb / 1e6 * USD_PER_TB:,.6f}, ${mb / 1e6 * USD_PER_TB / max(1, len(keys)):,.7f} "
          f"a publication)")
    for r in rows:
        print(f"  {r['pn_key']:18s} {r['country']} src={r['src_lang'] or '-':4s} "
              f"{(r['title_en'] or '')[:56]}")
        print(f"     {(r['abstract_en'] or '')[:150]}")
    missing = [k for k in keys if k not in {r["pn_key"] for r in rows}]
    if missing:
        print(f"  absent: {', '.join(missing[:12])}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    b = sub.add_parser("build")
    b.add_argument("--yes", action="store_true", help="actually spend the dry-run estimate")
    b.add_argument("--max-gb", type=float, default=400.0,
                   help="hard maximum_bytes_billed for the build query")
    b.set_defaults(fn=cmd_build)
    p = sub.add_parser("probe")
    p.add_argument("publications", nargs="+")
    p.set_defaults(fn=cmd_probe)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
