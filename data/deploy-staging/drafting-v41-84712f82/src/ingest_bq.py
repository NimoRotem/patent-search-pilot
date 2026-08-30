"""BigQuery bootstrap ingest (spec §2.2). ONE expensive full-text scan extracts the pilot
slice into staging tables; gold-set + Postgres load then read cheaply from staging.

Dates in patents-public-data.patents.publications are INT64 YYYYMMDD -> converted here.
Core = seed-CPC matches (full text). Expanded = families + 1-hop citations (biblio+claims).

The core SELECT list is factored into `_CORE_COLUMNS` so the incremental/delta job
(src/incremental_ingest.py) extracts *exactly* the same column shape into its own staging
table and can therefore be loaded by the existing ingest_pg.load_table() path unchanged.
"""
from __future__ import annotations
import sys, json
import bqclient
from config import GCP_PROJECT

DATASET = f"{GCP_PROJECT}.patent_pilot"
CORE_TBL = f"{DATASET}.core"
EXPANDED_TBL = f"{DATASET}.expanded"
DELTA_TBL = f"{DATASET}.delta"
SRC = "`patents-public-data.patents.publications`"

_DATE = lambda col: f"SAFE.PARSE_DATE('%Y%m%d', CAST(NULLIF({col},0) AS STRING))"

# en-preferred + original-language localized picks
def _loc(field, lang_pref="en"):
    return f"""(SELECT x.text FROM UNNEST({field}) x ORDER BY CASE WHEN x.language='{lang_pref}' THEN 0 ELSE 1 END, LENGTH(x.text) DESC LIMIT 1)"""

def _loc_orig(field):
    # original (non-en) text + its language, for cross-lingual DE
    return (f"""(SELECT AS STRUCT x.text AS text, x.language AS lang FROM UNNEST({field}) x
                 ORDER BY CASE WHEN x.language='en' THEN 1 ELSE 0 END, LENGTH(x.text) DESC LIMIT 1)""")


# --- shared core projection -------------------------------------------------------------
# Single source of truth for the "core tier" column shape. Both the bootstrap extract and
# the weekly delta extract use it, so a delta staging table is drop-in loadable by
# ingest_pg.load_table(tbl, tier="core", ...).
_CORE_COLUMNS = f"""
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
"""

_SEED_CPC_PREDICATE = f"EXISTS (SELECT 1 FROM UNNEST(cpc) c WHERE {bqclient.cpc_like_clause()})"

_CORE_WHERE = f"""WHERE {bqclient.juris_predicate()}
  AND {_SEED_CPC_PREDICATE}
"""

CORE_EXTRACT_SQL = f"""
CREATE OR REPLACE TABLE {CORE_TBL}
CLUSTER BY publication_number AS{_CORE_COLUMNS}{_CORE_WHERE}"""


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


# --- incremental / delta extraction -----------------------------------------------------
# NOTE on BigQuery economics: patents-public-data.patents.publications is NOT partitioned and
# NOT clustered (verified via get_table: partitioning=None, clustering=None). BigQuery bills
# the *full* referenced columns regardless of the WHERE clause, so narrowing the date window
# does NOT reduce cost -- a 7-day window and a 365-day window bill identically. The cost is
# governed entirely by WHICH COLUMNS are referenced. That is why:
#   * the freshness probe (publication_date + country_code only) is ~2 GB, and
#   * the full-text delta extract is ~1.5 TB, the same as a bootstrap core extract.
# Consequence: use a *generous* safety-lookback window (it is free), and gate the expensive
# extract behind the cheap freshness probe so the weekly cron costs ~$0.01 when BigQuery has
# published nothing new.

def _date_int(d):
    """date/datetime -> BigQuery's INT64 YYYYMMDD encoding."""
    return int(d.strftime("%Y%m%d"))


FRESHNESS_SQL = f"""
SELECT MAX(publication_date) AS max_pub_date,
       COUNT(*) AS n_rows
FROM {SRC}
WHERE {bqclient.juris_predicate()}
"""


def delta_count_sql(since, until=None):
    """Cheap-ish probe: how many seed-CPC publications exist in the window? (~18 GB)

    References only publication_number/country_code/publication_date/cpc, so it costs a
    fraction of the full-text extract and tells us whether the extract is worth running.
    """
    upper = f"\n  AND publication_date <= {_date_int(until)}" if until else ""
    return f"""
SELECT COUNT(*) AS n, MIN(publication_date) AS mn, MAX(publication_date) AS mx
FROM {SRC}
WHERE {bqclient.juris_predicate()}
  AND publication_date >= {_date_int(since)}{upper}
  AND {_SEED_CPC_PREDICATE}
"""


def delta_extract_sql(since, until=None, dest=DELTA_TBL):
    """Full core-shaped extract restricted to a publication-date window.

    Identical column shape to CORE_EXTRACT_SQL (see _CORE_COLUMNS) so the resulting staging
    table is loadable by ingest_pg.load_table(dest, tier="core", ...) with no changes.
    """
    upper = f"\n  AND publication_date <= {_date_int(until)}" if until else ""
    where = f"""WHERE {bqclient.juris_predicate()}
  AND publication_date >= {_date_int(since)}{upper}
  AND {_SEED_CPC_PREDICATE}
"""
    return f"""
CREATE OR REPLACE TABLE {dest}
CLUSTER BY publication_number AS{_CORE_COLUMNS}{where}"""


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
  WHERE {bqclient.juris_predicate()} AND (
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
