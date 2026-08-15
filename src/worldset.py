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

import hashlib
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
#  How many publications a working set may hold. A 40-subclass, all-era set is a few million rows;
#  the bound exists so a careless class list cannot write a 100M-row table.
MAX_ROWS = int(os.environ.get("WORLDSET_MAX_ROWS", "8000000"))
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
    if TTL_DAYS and t.modified is not None:
        age = (time.time() - t.modified.timestamp()) / 86400.0
        if age > TTL_DAYS:
            return False
    return True


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
        return {"table": table, "rows": None, "gb": 0.0, "cached": True, "cpc": cpc}

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
        rows = bqclient.client().get_table(table).num_rows
    except Exception:
        pass
    log(f"[worldset] built {table}: {rows if rows is not None else '?'} publications across "
        f"{len(cpc)} classes ({gb:.0f} GB scanned, ${bqclient.usd(gb):.2f}, "
        f"{time.time() - t0:.0f}s) — {', '.join(cpc[:12])}")
    return {"table": table, "rows": rows, "gb": gb, "cached": False, "cpc": cpc}


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


def lexical(table, must_any=(), must_all=(), cpc=(), limit=400, log=print):
    """Full-text search over the working set. -> [{pub, title, score, ...}] best first.

    Scored by how many of the `must_any` terms appear and how early — crude, and deliberately so:
    this is a RECALL stage feeding a reader, and a clever lexical score here would only be a worse
    version of the judgement the reader makes from the full text.
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
