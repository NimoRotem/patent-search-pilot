"""How big the niche is IN THE WORLD, from patents-public-data. The only part not answerable here.

Three cached answers, all under `data/niche_cache/`:

    world_cpc_l5.json       distinct publications and families per CPC main group, worldwide
    world_cpc_l4.json       the same per CPC subclass
    world_universe.json     the boundary's own total, and its country x decade breakdown

`world_cpc_l5.json` is what `ops/niche_boundary.py` divides the evidence by, so it has to exist
before a boundary can be re-derived. `world_universe.json` is the denominator of the completeness
statement.

MEASURED 2026-08-22: 17.3 GB, 19.4 GB and 17.3 GB scanned, about $0.35 for all three at the US
on-demand price. Every query dry-runs first and refuses above `--ceiling-gb`, because this table is
3.1 TB and a careless join is a real bill.

    python ops/niche_world.py --dry-run
    python ops/niche_world.py --run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

CACHE = os.path.join(ROOT, "data", "niche_cache")
CONFIG = os.path.join(ROOT, "config", "niche_boundary.json")
USD_PER_TB = 6.25

L5 = """
SELECT SUBSTR(REPLACE(c.code,' ',''),1,4) AS l4,
       SPLIT(REPLACE(c.code,' ',''),'/')[OFFSET(0)] AS l5,
       COUNT(DISTINCT family_id) AS families,
       COUNT(DISTINCT publication_number) AS pubs
FROM `patents-public-data.patents.publications`, UNNEST(cpc) AS c
WHERE c.code IS NOT NULL AND c.code != ''
GROUP BY 1, 2
"""

#  A subclass total is NOT the sum of its main groups: a publication carrying B65G47 and B65G49 is
#  one publication in B65G and two rows in the L5 table. Summing the L5 rows reported B65G at 55%
#  held when the true figure is 100%, which is exactly the kind of error that funds an ingest
#  nobody needed. So the subclass counts get their own DISTINCT.
L4 = """
WITH x AS (
  SELECT publication_number, family_id, SUBSTR(REPLACE(c.code,' ',''),1,4) AS l4
  FROM `patents-public-data.patents.publications`, UNNEST(cpc) AS c
  WHERE c.code IS NOT NULL AND c.code != ''
)
SELECT l4, COUNT(DISTINCT publication_number) AS pubs, COUNT(DISTINCT family_id) AS families
FROM x GROUP BY l4
"""

#  Three groupings, not one cross-tab. A family filed in five offices across two decades appears in
#  five country rows and one decade row, so summing a (country, decade) cross-tab over either axis
#  double counts it. The country breakdown is deliberately a per-office count of the SAME family;
#  the decade breakdown dates a family by its earliest publication, which is the definition
#  `ops/niche_report.py` uses on the local side so the two columns can sit next to each other.
UNIVERSE = """
WITH x AS (
  SELECT DISTINCT publication_number, family_id, country_code, publication_date
  FROM `patents-public-data.patents.publications`, UNNEST(cpc) AS c
  WHERE SUBSTR(REPLACE(c.code,' ',''),1,4) IN UNNEST(@core)
     OR SPLIT(REPLACE(c.code,' ',''),'/')[OFFSET(0)] IN UNNEST(@adj)
),
f AS (
  SELECT family_id,
         CAST(FLOOR(MIN(NULLIF(publication_date, 0)) / 100000) AS INT64) * 10 AS decade
  FROM x GROUP BY family_id
)
SELECT 'TOTAL' AS bucket, 'total' AS kind,
       COUNT(DISTINCT family_id) AS families, COUNT(DISTINCT publication_number) AS pubs FROM x
UNION ALL
SELECT country_code, 'country', COUNT(DISTINCT family_id), COUNT(DISTINCT publication_number)
FROM x GROUP BY 1
UNION ALL
SELECT CAST(IFNULL(decade, 0) AS STRING), 'decade', COUNT(DISTINCT family_id), 0
FROM f GROUP BY 1
"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--ceiling-gb", type=float, default=60.0)
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args(argv)

    from google.cloud import bigquery
    from config import GCP_PROJECT

    spec = json.load(open(args.config))
    params = [bigquery.ArrayQueryParameter("core", "STRING", list(spec["core_subclasses"])),
              bigquery.ArrayQueryParameter("adj", "STRING", list(spec["adjacent_groups"]))]
    client = bigquery.Client(project=GCP_PROJECT)
    os.makedirs(args.cache, exist_ok=True)

    jobs = [("world_cpc_l5.json", L5, None),
            ("world_cpc_l4.json", L4, None),
            ("world_universe.json", UNIVERSE, params)]
    total_gb = 0.0
    for name, sql, p in jobs:
        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=p or [])
        gb = client.query(sql, job_config=cfg).total_bytes_processed / 1e9
        total_gb += gb
        print(f"{name:24} dry run {gb:8,.1f} GB  ~${gb / 1000 * USD_PER_TB:,.2f}")
        if gb > args.ceiling_gb:
            raise SystemExit(f"refusing {name}: {gb:,.1f} GB over the {args.ceiling_gb:,.1f} GB "
                             f"ceiling. Raise it deliberately if that is what you meant.")
        if not args.run:
            continue
        rows = [dict(r) for r in
                client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=p or [])
                             ).result()]
        with open(os.path.join(args.cache, name), "w") as fh:
            json.dump(rows, fh)
        print(f"{name:24} {len(rows):,} rows")
    print(f"total {total_gb:,.1f} GB ~${total_gb / 1000 * USD_PER_TB:,.2f}"
          f"{'' if args.run else '  (dry run only, pass --run)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
