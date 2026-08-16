"""Every patent on Earth, with its full text, as the retrieval universe.

WHY THIS REPLACES THE SEEDED CORPUS
-----------------------------------
The local corpus holds ~5M publications seeded from eight vacuum-gripping CPC branches, and it has
been the retrieval universe: nothing outside it can be found by any amount of re-ranking or query
rewriting. Measured against the ten references a patent attorney filed against US 2026/0109053 A1,
three were absent from the corpus entirely and three more were text-less citation stubs, and every
one of those six is classified OUTSIDE the seeded branches — F01N mufflers, G10K sound absorption,
B25F portable tools, A47L blowers — because the invention's point is exhaust noise damping.

Meanwhile:

    patents-public-data.patents.publications                170,418,479 rows · 3.09 TB
      claims_localized / description_localized / abstract_localized     full text, all languages
    patents-public-data.google_patents_research.publications  170,418,479 rows · 0.51 TB
      embedding_v1   a precomputed BERT vector for every patent ever published
      similar        a precomputed nearest-neighbour graph over all of them
      top_terms      the distinctive terms of each document
      cited_by, cpc, cpc_low, abstract_translated

We had been using that first table for TITLE SUBSTRING MATCHING, ranked by how many query terms hit
and tie-broken with RAND(). The scraping chain, the ScrapingBee credits, the Google Patents IP
blocks and the SerpApi quota were all us rebuilding, badly, something we already have free access
to.

So: BigQuery is the index and the local corpus is a warm cache of what we have read.

THE COST SHAPE, WHICH DICTATES THE DESIGN
-----------------------------------------
BigQuery bills for columns scanned, not rows matched, so any query touching description_localized
scans ~2.5 TB whatever the WHERE clause says. Running 1,800 per-limitation queries that way is not
expensive so much as absurd — three hours of pure I/O to answer questions a local table answers in
milliseconds.

Hence the WORKING SET: one guarded scan materialises every publication in the target classes and
eras, WITH its full text, into a project table. Everything after that queries the working set for
pennies. The table is keyed by a hash of (classes, date bound) and reused, so the second search in
a field costs nothing at all.

Nothing here writes to the local corpus by itself; `ingest` does that for the documents a search
actually selects, and it writes TEXT, never a bare title. A row the reader cannot read is the
defect this module exists to end.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time

import bqclient

GP = "patents-public-data.patents.publications"
GPR = "patents-public-data.google_patents_research.publications"

DATASET = os.environ.get("WORLDSET_DATASET", "patent_pilot")
#  Ceiling for the one big materialisation, in GB scanned. The full-text columns are ~2.5 TB, so
#  this has to clear it; bqclient.estimate_and_guard dry-runs first and refuses above the ceiling.
BUILD_CEILING_GB = float(os.environ.get("WORLDSET_BUILD_CEILING_GB", "4000"))
#  Per-query ceiling. The similar-graph join touches the 3 TB source table for dates, so 60
#  was below the cost of the cheapest useful question and simply refused every time.
QUERY_CEILING_GB = float(os.environ.get("WORLDSET_QUERY_CEILING_GB", "400"))
#  How many publications a working set may hold. The bound exists so a careless class list cannot
#  write a 100M-row table.
#
#  IT IS AN UNORDERED `LIMIT`, SO REACHING IT DISCARDS AN ARBITRARY PART OF THE UNIVERSE, and the
#  universe is the one thing in this pipeline nothing downstream can recover from. Measured
#  2026-08-15: a working set built from 20 classes came back at exactly 8,000,000 rows and did not
#  contain US-9107549-B2, one of the ten references a patent attorney filed — while two other
#  references classified in the same neighbourhood were in it. The log said "built ... 8000000
#  publications" and read like success. A round number is a truncation, never a finding.
#
#  Raised, and `build` now says so loudly when it binds. The cost of a bigger set is storage, not
#  scan: the build scans ~1,500 GB of text columns whatever the row count, and the table expires
#  after TTL_DAYS.
MAX_ROWS = int(os.environ.get("WORLDSET_MAX_ROWS", "16000000"))
#  Working sets older than this are rebuilt. Patent data updates weekly.
TTL_DAYS = float(os.environ.get("WORLDSET_TTL_DAYS", "30"))

_CPC = re.compile(r"^[A-HY]\d{2}[A-Z]")


def valid_cpc(sym) -> str:
    """A CPC prefix we will let into a LIKE clause, or "".

    Subclass ("F01N") or group ("F01N1/24"). Anything else is a model inventing a symbol, and a
    malformed prefix silently matches nothing — which reads as "there is no art here".
    """
    s = re.sub(r"\s+", "", str(sym or "")).upper()
    return s if _CPC.match(s) else ""


def key_for(cpc_prefixes, date_max=None) -> str:
    payload = "|".join(sorted(set(cpc_prefixes))) + "@" + str(date_max or "")
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def table_for(cpc_prefixes, date_max=None) -> str:
    return f"{bqclient.GCP_PROJECT}.{DATASET}.ws_{key_for(cpc_prefixes, date_max)}"


def _exists(table) -> bool:
    from google.cloud import bigquery                                    # noqa: F401
    try:
        t = bqclient.client().get_table(table)
    except Exception:
        return False
    return _fresh(t)


def _fresh(t) -> bool:
    if TTL_DAYS and getattr(t, "modified", None) is not None:
        return (time.time() - t.modified.timestamp()) / 86400.0 <= TTL_DAYS
    return True


# ---------------------------------------------------------------------------
# REUSING A WORKING SET THAT IS WIDER THAN THE ONE ASKED FOR
#
# The exact-key cache above is correct and it almost never hits, for two reasons that are both
# properties of the caller rather than bugs:
#
#   * the class list comes from `limitation_query.facets_for`, which is a MODEL call, so it wobbles
#     between runs on the same subject; and
#   * `date_max` is the subject's own effective filing date, so two different subjects can never
#     share a table however identical their classes are.
#
# Measured: two searches a day apart built ws_36208e56133485ea and ws_126282588b746b0b, each
# scanning ~1,500 GB for $9.38 and ~18 minutes, and each was then asked eight questions that
# returned 1,812 candidates of which 130 were read.
#
# A working set is a SUPERSET CACHE, though, not an exact one. A table built over classes H ⊇ the
# classes we want, with a date bound at least as late as ours, contains every row our own build
# would have produced and some extra. The extra is harmless as long as we filter it out at query
# time, which `limitation_query.build_sql` already did and `lexical` now does too.
#
# So: prefer the NARROWEST superset. Widest would maximise future reuse but every query then scans
# a bigger table, and query cost is what we are protecting downstream.
_MANIFEST_KIND = "patent-pilot-worldset"


def _dnorm(d):
    """A date bound as an int yyyymmdd, or None for "no bound"."""
    s = str(d or "").replace("-", "")[:8]
    return int(s) if s.isdigit() and len(s) == 8 else None


def _date_ok(table_bound, want_bound) -> bool:
    """Can a table built with `table_bound` serve a request that needs `want_bound`?

    Only if the table holds everything we do: unbounded, or bounded no earlier than we need. The
    CALLER must then apply `want_bound` itself, or a reused table hands back art published after
    the subject was filed, which is not prior art under any mode.
    """
    tb, rb = _dnorm(table_bound), _dnorm(want_bound)
    if tb is None:
        return True                     # the table has no date bound: it holds everything
    if rb is None:
        return False                    # we need everything; a bounded table is short
    return tb >= rb


def _manifest(t):
    """What a table says it was built from, or None if it does not say."""
    try:
        d = json.loads((getattr(t, "description", "") or "").strip())
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("kind") == _MANIFEST_KIND else None


def _write_manifest(table, cpc, date_max, truncated=False, log=print):
    """Record (classes, date bound, truncation) ON the table, so a later search can judge it.

    On the table rather than in a local file on purpose: four hosts run this pipeline against the
    same BigQuery project, and a cache only one of them can see is a cache three of them pay for.

    `truncated` is the load-bearing field. A table that hit MAX_ROWS is missing an arbitrary slice
    of the universe (see the note on MAX_ROWS), and reusing one would spread a silent truncation
    from the single search that built it to every later search that matched it.
    """
    try:
        t = bqclient.client().get_table(table)
        t.description = json.dumps({"kind": _MANIFEST_KIND, "cpc": sorted(cpc),
                                    "date_max": str(date_max or ""),
                                    "truncated": bool(truncated)}, sort_keys=True)
        bqclient.client().update_table(t, ["description"])
    except Exception as e:
        #  Never fatal. A table with no manifest is simply invisible to superset reuse; it still
        #  serves the exact-key path, and the next build rewrites one.
        log(f"[worldset] could not record the manifest on {table}: {str(e)[:120]}")


def find_reusable(cpc_prefixes, date_max=None, log=print):
    """The narrowest cached working set that already contains everything asked for, or None."""
    want = {c for c in (valid_cpc(x) for x in (cpc_prefixes or [])) if c}
    if not want:
        return None
    try:
        listed = list(bqclient.client().list_tables(f"{bqclient.GCP_PROJECT}.{DATASET}"))
    except Exception as e:
        log(f"[worldset] could not list {DATASET}: {str(e)[:120]}")
        return None
    best = None
    for ref in listed:
        if not ref.table_id.startswith("ws_"):
            continue
        try:
            t = bqclient.client().get_table(ref)
        except Exception:
            continue
        if not _fresh(t):
            continue
        m = _manifest(t)
        if not m:
            continue
        #  NEVER REUSE A TRUNCATED TABLE. It hit MAX_ROWS, so an arbitrary part of its classes is
        #  simply absent, and nothing downstream can tell that from "there is no such art". One
        #  search paying for that mistake is bad; every later search inheriting it is worse.
        if m.get("truncated"):
            continue
        have = {c for c in (valid_cpc(x) for x in (m.get("cpc") or [])) if c}
        if not want.issubset(have) or not _date_ok(m.get("date_max"), date_max):
            continue
        rows = t.num_rows or 0
        if best is None or rows < best["rows"]:
            best = {"table": f"{ref.project}.{ref.dataset_id}.{ref.table_id}",
                    "rows": rows, "cpc": sorted(have), "date_max": m.get("date_max") or ""}
    return best


def build(cpc_prefixes, date_max=None, log=print, force=False):
    """Materialise the working set. -> {"table", "rows", "gb", "cached", "cpc"}.

    `date_max` is the subject's effective filing date: art published on or after it is not prior
    art under any mode, and excluding it here keeps the whole downstream pipeline from having to
    remember. `None` builds without a date bound (Type A, no subject).
    """
    cpc = sorted({c for c in (valid_cpc(x) for x in (cpc_prefixes or [])) if c})
    if not cpc:
        return {"table": "", "rows": 0, "gb": 0.0, "cached": False, "cpc": [],
                "error": "no valid CPC prefixes"}
    table = table_for(cpc, date_max)
    if not force and _exists(table):
        log(f"[worldset] reusing {table} ({len(cpc)} classes)")
        return {"table": table, "rows": None, "gb": 0.0, "cached": True, "cpc": cpc,
                "date_max": str(date_max or "")}

    #  NOT AN EXACT MATCH, BUT MAYBE A WIDER ONE. See the note above _MANIFEST_KIND: the exact key
    #  is a hash of (classes, date) and both wobble per search, so without this the $9.38 build runs
    #  every single time.
    if not force:
        try:
            reuse = find_reusable(cpc, date_max, log=log)
        except Exception:
            reuse = None
        if reuse:
            extra = len(set(reuse["cpc"]) - set(cpc))
            log(f"[worldset] reusing the wider {reuse['table']}: it already covers all "
                f"{len(cpc)} classes asked for (plus {extra} more) with a date bound of "
                f"{reuse['date_max'] or 'none'}, {reuse['rows']:,} rows — no build, $0.00")
            return {"table": reuse["table"], "rows": reuse["rows"], "gb": 0.0, "cached": True,
                    "cpc": reuse["cpc"], "date_max": reuse["date_max"], "superset": True}

    bqclient.ensure_dataset(DATASET)
    like = " OR ".join([f"c.code LIKE '{c}%'" for c in cpc])
    date_clause = ""
    if date_max:
        d = str(date_max).replace("-", "")[:8]
        if d.isdigit() and len(d) == 8:
            #  publication_date is an INT64 yyyymmdd in this table.
            date_clause = f"AND p.publication_date > 0 AND p.publication_date < {int(d)}"
    sql = f"""
      SELECT
        p.publication_number,
        p.country_code,
        p.publication_date,
        p.filing_date,
        p.priority_date,
        (SELECT t.text FROM UNNEST(p.title_localized) t
          WHERE t.language IN ('en','EN') LIMIT 1) AS title_en,
        (SELECT t.text FROM UNNEST(p.title_localized) t LIMIT 1) AS title_any,
        (SELECT t.text FROM UNNEST(p.abstract_localized) t
          WHERE t.language IN ('en','EN') LIMIT 1) AS abstract_en,
        (SELECT t.text FROM UNNEST(p.abstract_localized) t LIMIT 1) AS abstract_any,
        (SELECT t.text FROM UNNEST(p.claims_localized) t
          WHERE t.language IN ('en','EN') LIMIT 1) AS claims_en,
        (SELECT t.text FROM UNNEST(p.claims_localized) t LIMIT 1) AS claims_any,
        (SELECT t.text FROM UNNEST(p.description_localized) t
          WHERE t.language IN ('en','EN') LIMIT 1) AS description_en,
        (SELECT t.text FROM UNNEST(p.description_localized) t LIMIT 1) AS description_any,
        ARRAY(SELECT c.code FROM UNNEST(p.cpc) c) AS cpc,
        (SELECT STRING_AGG(a.name, '; ') FROM UNNEST(p.assignee_harmonized) a) AS assignee,
        p.family_id
      FROM `{GP}` p
      WHERE EXISTS (SELECT 1 FROM UNNEST(p.cpc) c WHERE {like})
        {date_clause}
      LIMIT {MAX_ROWS}
    """
    gb = bqclient.estimate_and_guard(sql, BUILD_CEILING_GB, label="worldset build", log=log)
    t0 = time.time()
    bqclient.run_to_table(sql, table, max_gb_billed=BUILD_CEILING_GB,
                          cluster=["publication_number"])
    rows = None
    try:
        t = bqclient.client().get_table(table)
        rows = t.num_rows
        #  KEEP IT AS LONG AS WE SAY WE DO. `ensure_dataset` gives the dataset a 3-day default
        #  table expiration — right for a scratch table, wrong for this one, which costs $9.38 and
        #  145 seconds to build. TTL_DAYS said 30 and `_exists` believed it, so from day 4 every
        #  search rebuilt the working set from scratch while logging that it was reusing one.
        if TTL_DAYS:
            t.expires = (datetime.datetime.now(datetime.timezone.utc)
                         + datetime.timedelta(days=float(TTL_DAYS)))
            bqclient.client().update_table(t, ["expires"])
    except Exception as e:
        log(f"[worldset] could not set the expiry on {table}: {str(e)[:120]}")
    truncated = rows is not None and rows >= MAX_ROWS
    #  What it was built from, so the NEXT search can reuse it without an exact key match. Written
    #  AFTER `truncated` is known, because that flag is what keeps a capped table out of the cache.
    _write_manifest(table, cpc, date_max, truncated=truncated, log=log)
    log(f"[worldset] built {table}: {rows if rows is not None else '?'} publications across "
        f"{len(cpc)} classes ({gb:.0f} GB scanned, ${bqclient.usd(gb):.2f}, "
        f"{time.time() - t0:.0f}s) — {', '.join(cpc[:12])}")
    if truncated:
        #  Never silently. This is the retrieval universe: what the LIMIT dropped cannot be
        #  recovered by any amount of ranking, screening or re-reading downstream, and the miss
        #  looks exactly like "no such art exists".
        log(f"[worldset] *** TRUNCATED at WORLDSET_MAX_ROWS={MAX_ROWS:,}. These {len(cpc)} classes "
            f"hold more than that, so an ARBITRARY part of them is missing from the working set "
            f"and nothing downstream can find it. Narrow the class list or raise the cap: "
            f"{', '.join(cpc)}")
    return {"table": table, "rows": rows, "gb": gb, "cached": False, "cpc": cpc,
            "date_max": str(date_max or ""), "truncated": truncated}


# ---------------------------------------------------------------------------
# asking the working set
# ---------------------------------------------------------------------------
def _rows(result):
    """bqclient.run_guarded returns (rows, estimated_gb, billed_gb), not rows.

    Iterating the tuple yields the row LIST as its first element, so every consumer here silently
    saw one "row" that was a list and failed on the first field access. Unpack in one place.
    """
    if isinstance(result, tuple) and result and isinstance(result[0], list):
        return result[0]
    return list(result or [])


def _terms_clause(terms, col_expr, mode="any"):
    """A case-insensitive containment clause over the concatenated text of a row."""
    safe = [re.sub(r"[^\w %+-]", " ", str(t or "")).strip().lower() for t in (terms or [])]
    safe = [t for t in safe if len(t) >= 3][:40]
    if not safe:
        return "", []
    joiner = " OR " if mode == "any" else " AND "
    return "(" + joiner.join([f"STRPOS({col_expr}, '{t}') > 0" for t in safe]) + ")", safe


def lexical(table, must_any=(), must_all=(), cpc=(), limit=400, log=print, date_max=None):
    """Full-text search over the working set. -> [{pub, title, score, ...}] best first.

    Scored by how many of the `must_any` terms appear and how early — crude, and deliberately so:
    this is a RECALL stage feeding a reader, and a clever lexical score here would only be a worse
    version of the judgement the reader makes from the full text.

    `date_max` IS NOT OPTIONAL IN PRACTICE, even though the signature allows it. The working set may
    now be a SUPERSET table built for a different subject with a later date bound (see
    `find_reusable`), so the bound the caller needs can no longer be assumed to have been applied at
    build time. Omit it and a reused table quietly returns art published after the subject was
    filed, which is not prior art under any mode and which nothing downstream re-checks.
    """
    if not table:
        return []
    body = ("LOWER(CONCAT(IFNULL(title_en, IFNULL(title_any,'')), ' ', "
            "IFNULL(abstract_en, IFNULL(abstract_any,'')), ' ', "
            "IFNULL(claims_en, IFNULL(claims_any,'')), ' ', "
            "IFNULL(description_en, IFNULL(description_any,''))))")
    any_c, any_terms = _terms_clause(must_any, "body", "any")
    all_c, _ = _terms_clause(must_all, "body", "all")
    where = [c for c in (any_c, all_c) if c]
    if cpc:
        syms = [c for c in (valid_cpc(x) for x in cpc) if c]
        if syms:
            where.append("EXISTS (SELECT 1 FROM UNNEST(cpc) c WHERE "
                         + " OR ".join([f"c LIKE '{s}%'" for s in syms]) + ")")
    if not where:
        return []
    #  AFTER the guard above, never as part of it: a date bound is not a search, and letting it
    #  stand alone would turn "no usable terms" into a full scan of the working set.
    d = _dnorm(date_max)
    if d:
        where.append(f"publication_date > 0 AND publication_date < {d}")
    hits = " + ".join([f"CAST(STRPOS(body, '{t}') > 0 AS INT64)" for t in any_terms]) or "0"
    sql = f"""
      WITH b AS (SELECT *, {body} AS body FROM `{table}`)
      SELECT publication_number AS pub,
             IFNULL(title_en, title_any) AS title,
             IFNULL(abstract_en, abstract_any) AS abstract,
             publication_date, family_id, cpc, assignee,
             ({hits}) AS n_terms
      FROM b
      WHERE {' AND '.join(where)}
      ORDER BY n_terms DESC, publication_date ASC
      LIMIT {int(limit)}
    """
    try:
        rows = _rows(bqclient.run_guarded(sql, QUERY_CEILING_GB,
                                          label="worldset lexical", log=log))
    except Exception as e:
        log(f"[worldset] lexical failed: {str(e)[:200]}")
        return []
    return [dict(r) for r in rows]


def similar_to(pubs, limit=200, date_max=None, log=print):
    """Google's own precomputed nearest-neighbour graph, over all 170M publications.

    Free query-by-example against every patent ever published, with no embedding of our own and no
    index to maintain. This is the single cheapest way out of the neighbourhood a seeded corpus
    traps a search in.
    """
    pubs = [p for p in (pubs or []) if p][:60]
    if not pubs:
        return []
    lst = ",".join("'" + re.sub(r"[^A-Za-z0-9-]", "", p) + "'" for p in pubs)
    date_clause = ""
    if date_max:
        d = str(date_max).replace("-", "")[:8]
        if d.isdigit():
            date_clause = f"AND p.publication_date < {int(d)} AND p.publication_date > 0"
    #  The `similar` struct carries no similarity NUMBER — it is
    #  STRUCT<publication_number, application_number, npl_text, type, category, filing_date>, and
    #  Google's own ordering is the signal. So rank by array position, best (lowest) first, and
    #  let a document found near the front of several seeds beat one found deep in a single list.
    sql = f"""
      SELECT s.publication_number AS pub,
             1.0 / (1 + MIN(off)) AS score,
             MIN(off) AS best_offset, COUNT(*) AS n_seeds,
             ANY_VALUE(p.publication_date) AS publication_date
      FROM `{GPR}` r, UNNEST(r.similar) s WITH OFFSET off
      LEFT JOIN `{GP}` p ON p.publication_number = s.publication_number
      WHERE r.publication_number IN ({lst})
        AND s.publication_number IS NOT NULL AND s.publication_number != ''
        {date_clause}
      GROUP BY pub
      ORDER BY n_seeds DESC, best_offset ASC LIMIT {int(limit)}
    """
    try:
        return [dict(r) for r in _rows(bqclient.run_guarded(
            sql, QUERY_CEILING_GB, label="worldset similar", log=log))]
    except Exception as e:
        log(f"[worldset] similar failed: {str(e)[:200]}")
        return []


def top_terms(pubs, limit=400, log=print):
    """The distinctive terms Google already extracted for these documents.

    THE VOCABULARY LOOP. A model asked what else a thing might be called will not produce Blatt's
    actual words, "porous frequency-distorter insert". The documents that DO disclose a limitation
    will, and this is where they say it. Feed these back into `lexical` and the search stops
    matching our vocabulary and starts matching theirs.
    """
    pubs = [p for p in (pubs or []) if p][:200]
    if not pubs:
        return []
    lst = ",".join("'" + re.sub(r"[^A-Za-z0-9-]", "", p) + "'" for p in pubs)
    sql = f"""
      SELECT t AS term, COUNT(*) n
      FROM `{GPR}`, UNNEST(top_terms) t
      WHERE publication_number IN ({lst})
      GROUP BY term ORDER BY n DESC, term LIMIT {int(limit)}
    """
    try:
        return [dict(r) for r in _rows(bqclient.run_guarded(
            sql, QUERY_CEILING_GB, label="worldset top_terms", log=log))]
    except Exception as e:
        log(f"[worldset] top_terms failed: {str(e)[:200]}")
        return []


def classes_of(pubs, limit=60, log=print):
    """Which CPC groups the art that actually answers us lives in.

    Self-correcting class targeting: whatever the model guessed, the documents that turned out to
    disclose the thing carry the real answer, and the next round searches there.
    """
    pubs = [p for p in (pubs or []) if p][:200]
    if not pubs:
        return []
    lst = ",".join("'" + re.sub(r"[^A-Za-z0-9-]", "", p) + "'" for p in pubs)
    sql = f"""
      SELECT c.code AS code, COUNT(*) n
      FROM `{GP}`, UNNEST(cpc) c
      WHERE publication_number IN ({lst}) AND c.code IS NOT NULL
      GROUP BY code ORDER BY n DESC LIMIT {int(limit)}
    """
    try:
        return [dict(r) for r in _rows(bqclient.run_guarded(
            sql, QUERY_CEILING_GB, label="worldset classes", log=log))]
    except Exception as e:
        log(f"[worldset] classes failed: {str(e)[:200]}")
        return []


def fetch_text(pubs, table="", log=print):
    """Full text for specific publications. -> {pub: {title, abstract, claims, description, ...}}

    Prefers the working set (a small table, pennies). Falls back to the source table, which scans
    the description column and therefore costs real money — guarded, and logged when it happens.
    """
    pubs = [re.sub(r"[^A-Za-z0-9-]", "", str(p)) for p in (pubs or []) if p][:2000]
    if not pubs:
        return {}
    lst = ",".join("'" + p + "'" for p in pubs)
    src = table or GP
    if table:
        sql = f"""SELECT publication_number AS pub,
                         IFNULL(title_en, title_any) AS title,
                         IFNULL(abstract_en, abstract_any) AS abstract,
                         IFNULL(claims_en, claims_any) AS claims,
                         IFNULL(description_en, description_any) AS description,
                         publication_date, family_id, cpc, assignee
                  FROM `{table}` WHERE publication_number IN ({lst})"""
    else:
        log(f"[worldset] fetching text for {len(pubs)} publications from the SOURCE table "
            f"(scans the description column — this is the expensive path)")
        sql = f"""
          SELECT p.publication_number AS pub,
                 (SELECT t.text FROM UNNEST(p.title_localized) t LIMIT 1) AS title,
                 (SELECT t.text FROM UNNEST(p.abstract_localized) t LIMIT 1) AS abstract,
                 (SELECT t.text FROM UNNEST(p.claims_localized) t LIMIT 1) AS claims,
                 (SELECT t.text FROM UNNEST(p.description_localized) t LIMIT 1) AS description,
                 p.publication_date, p.family_id,
                 ARRAY(SELECT c.code FROM UNNEST(p.cpc) c) AS cpc,
                 (SELECT STRING_AGG(a.name,'; ') FROM UNNEST(p.assignee_harmonized) a) AS assignee
          FROM `{GP}` p WHERE p.publication_number IN ({lst})"""
    try:
        rows = _rows(bqclient.run_guarded(
            sql, BUILD_CEILING_GB if not table else QUERY_CEILING_GB,
            label="worldset fetch_text", log=log))
    except Exception as e:
        log(f"[worldset] fetch_text failed against {src}: {str(e)[:200]}")
        return {}
    return {r["pub"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# into the local corpus, WITH TEXT
# ---------------------------------------------------------------------------
_CLAIM_SPLIT = re.compile(r"(?m)^\s*(?:\(|\[)?(\d{1,3})[.)\]]\s+")


def _split_claims(text):
    """A claims blob -> [claim text]. Numbered claims, else paragraph-split.

    Google's claims_localized is one string per language. The corpus stores claims individually
    because that is what a chart cites, so the split happens here rather than being lost.
    """
    t = (text or "").strip()
    if not t:
        return []
    parts = _CLAIM_SPLIT.split(t)
    if len(parts) >= 3:
        out = []
        for i in range(1, len(parts) - 1, 2):
            body = " ".join(parts[i + 1].split())
            if body:
                out.append(body)
        if out:
            return out[:200]
    return [" ".join(p.split()) for p in t.split("\n\n") if p.strip()][:200]


def _split_paras(text, min_chars=80):
    t = (text or "").strip()
    if not t:
        return []
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", t)]
    return [p for p in paras if len(p) >= min_chars][:600]


def ingest(pubs, table="", reembed=True, log=print):
    """Write these publications into the local corpus WITH THEIR TEXT. -> {"rows", "with_text"}.

    NEVER a bare title. Adding a row the reader cannot read is the defect measured on the live run
    that prompted this module: 300 publications acquired, 0 of 40 readable, so every one of them
    charted as "no text" and the acquisition was theatre. A publication with no text in BigQuery is
    skipped and counted, not inserted.
    """
    got = fetch_text(pubs, table=table, log=log)
    if not got:
        return {"rows": 0, "with_text": 0, "skipped_no_text": len(pubs or [])}
    import enrich
    import external
    rows = with_text = skipped = 0
    for pub, rec in got.items():
        claims = _split_claims(rec.get("claims"))
        paras = _split_paras(rec.get("description"))
        if not claims and not paras:
            skipped += 1
            continue
        d = rec.get("publication_date")
        iso = ""
        if d and str(d).isdigit() and len(str(d)) == 8:
            s = str(d)
            iso = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        try:
            external.materialise({external._norm(pub): {
                "pub_number": pub,
                "title": (rec.get("title") or "")[:500],
                "abstract": (rec.get("abstract") or "")[:8000],
                "family_id": str(rec.get("family_id") or ""),
                "publication_date": iso,
                "cpc": list(rec.get("cpc") or [])[:20],
                "assignee": (rec.get("assignee") or "")[:300],
            }})
        except Exception as e:
            log(f"[worldset] materialise {pub} failed: {str(e)[:120]}")
            continue
        rows += 1
        try:
            r = enrich._persist_full_text(
                pub, {"claims": claims, "description": paras, "sources": ["bigquery"]},
                reembed=reembed)
            if r and (r.get("added_claims") or r.get("added_paragraphs")):
                with_text += 1
        except Exception as e:
            log(f"[worldset] persist {pub} failed: {str(e)[:120]}")
    log(f"[worldset] ingested {rows} publications, {with_text} with full text, "
        f"{skipped} skipped for having none in BigQuery either")
    return {"rows": rows, "with_text": with_text, "skipped_no_text": skipped}
