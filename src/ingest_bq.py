"""BigQuery bootstrap ingest (spec §2.2). ONE expensive full-text scan extracts the pilot
slice into staging tables; gold-set + Postgres load then read cheaply from staging.

Dates in patents-public-data.patents.publications are INT64 YYYYMMDD -> converted here.
Core = seed-CPC matches (full text). Expanded = families + 1-hop citations (biblio+claims).
"""
from __future__ import annotations
import sys, json
import bqclient
from config import GCP_PROJECT

DATASET = f"{GCP_PROJECT}.patent_pilot"
CORE_TBL = f"{DATASET}.core"
EXPANDED_TBL = f"{DATASET}.expanded"
SRC = "`patents-public-data.patents.publications`"

_DATE = lambda col: f"SAFE.PARSE_DATE('%Y%m%d', CAST(NULLIF({col},0) AS STRING))"

# en-preferred + original-language localized picks
def _loc(field, lang_pref="en"):
    return f"""(SELECT x.text FROM UNNEST({field}) x ORDER BY CASE WHEN x.language='{lang_pref}' THEN 0 ELSE 1 END, LENGTH(x.text) DESC LIMIT 1)"""

def _loc_orig(field):
    # original (non-en) text + its language, for cross-lingual DE
    return (f"""(SELECT AS STRUCT x.text AS text, x.language AS lang FROM UNNEST({field}) x
                 ORDER BY CASE WHEN x.language='en' THEN 1 ELSE 0 END, LENGTH(x.text) DESC LIMIT 1)""")

CORE_EXTRACT_SQL = f"""
CREATE OR REPLACE TABLE {CORE_TBL}
CLUSTER BY publication_number AS
SELECT
  publication_number, country_code, kind_code, application_number,
  {_DATE('publication_date')} AS publication_date,
  {_DATE('filing_date')}      AS filing_date,
  {_DATE('priority_date')}    AS priority_date,
  CAST(family_id AS STRING)   AS family_id,
  {_loc('title_localized')}       AS title_en,
  {_loc('abstract_localized')}    AS abstract_en,
  {_loc_orig('abstract_localized')} AS abstract_orig,
  {_loc('claims_localized')}      AS claims_en,
  {_loc_orig('claims_localized')}   AS claims_orig,
  {_loc('description_localized')} AS description_en,
  {_loc_orig('description_localized')} AS description_orig,
  ARRAY(SELECT AS STRUCT c.code AS code, c.first AS first, c.inventive AS inventive
        FROM UNNEST(cpc) c) AS cpc,
  ARRAY(SELECT AS STRUCT i.code AS code FROM UNNEST(ipc) i) AS ipc,
  ARRAY(SELECT AS STRUCT ci.publication_number AS pub, ci.category AS category, ci.type AS type
        FROM UNNEST(citation) ci
        WHERE ci.publication_number IS NOT NULL AND ci.publication_number != '') AS cites,
  ARRAY(SELECT AS STRUCT a.name AS name FROM UNNEST(assignee_harmonized) a) AS assignees,
  ARRAY(SELECT AS STRUCT iv.name AS name FROM UNNEST(inventor_harmonized) iv) AS inventors
FROM {SRC}
WHERE country_code IN ('US','EP','WO','DE')
  AND EXISTS (SELECT 1 FROM UNNEST(cpc) c WHERE {bqclient.cpc_like_clause()})
"""


def extract_core(max_gb=2500.0):
    bqclient.ensure_dataset()
    try:
        est = bqclient.dry_run_gb(CORE_EXTRACT_SQL)
        print(f"[core] extraction dry-run ~{est:.0f} GB (${est/1000*6.25:.2f}); running...")
    except Exception as e:
        print(f"[core] dry-run skipped ({e}); running with {max_gb:.0f} GB cap...")
    job = bqclient.client().query(
        CORE_EXTRACT_SQL,
        job_config=bqclient.bigquery.QueryJobConfig(maximum_bytes_billed=int(max_gb*1e9)),
    )
    job.result()
    n = bqclient.client().get_table(CORE_TBL).num_rows
    print(f"[core] staging table {CORE_TBL} = {n:,} rows")
    return n


# Expanded set: family members of core + 1-hop backward/forward citations (biblio + claims).
EXPANDED_EXTRACT_SQL = f"""
CREATE OR REPLACE TABLE {EXPANDED_TBL}
CLUSTER BY publication_number AS
WITH core_fams AS (SELECT DISTINCT family_id FROM {CORE_TBL} WHERE family_id IS NOT NULL),
-- backward: docs cited BY core (prior art). Forward-citation expansion is intentionally
-- omitted for the pilot: it requires scanning every row's citation array (costly/slow) and
-- forward citations post-date the subject, so they don't affect prior-art recall. (spec §0)
cited AS (SELECT DISTINCT c.pub AS publication_number FROM {CORE_TBL}, UNNEST(cites) c),
wanted AS (
  SELECT publication_number FROM {SRC} p
  WHERE country_code IN ('US','EP','WO','DE') AND (
    CAST(family_id AS STRING) IN (SELECT family_id FROM core_fams)
    OR publication_number IN (SELECT publication_number FROM cited)
  )
)
SELECT
  publication_number, country_code, kind_code, application_number,
  {_DATE('publication_date')} AS publication_date,
  {_DATE('filing_date')}      AS filing_date,
  {_DATE('priority_date')}    AS priority_date,
  CAST(family_id AS STRING)   AS family_id,
  {_loc('title_localized')}       AS title_en,
  {_loc('abstract_localized')}    AS abstract_en,
  {_loc_orig('abstract_localized')} AS abstract_orig,
  {_loc('claims_localized')}      AS claims_en,
  {_loc_orig('claims_localized')}   AS claims_orig,
  ARRAY(SELECT AS STRUCT c.code AS code, c.first AS first, c.inventive AS inventive
        FROM UNNEST(cpc) c) AS cpc,
  ARRAY(SELECT AS STRUCT ci.publication_number AS pub, ci.category AS category, ci.type AS type
        FROM UNNEST(citation) ci
        WHERE ci.publication_number IS NOT NULL AND ci.publication_number != '') AS cites,
  ARRAY(SELECT AS STRUCT a.name AS name FROM UNNEST(assignee_harmonized) a) AS assignees
FROM {SRC}
WHERE publication_number IN (SELECT publication_number FROM wanted)
  AND publication_number NOT IN (SELECT publication_number FROM {CORE_TBL})
"""


def extract_expanded(max_gb=3000.0):
    print("[expanded] running family + 1-hop citation extraction...")
    job = bqclient.client().query(
        EXPANDED_EXTRACT_SQL,
        job_config=bqclient.bigquery.QueryJobConfig(maximum_bytes_billed=int(max_gb*1e9)),
    )
    job.result()
    n = bqclient.client().get_table(EXPANDED_TBL).num_rows
    print(f"[expanded] staging table {EXPANDED_TBL} = {n:,} rows")
    return n


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "core"
    if cmd == "core":
        extract_core()
    elif cmd == "expanded":
        extract_expanded()
