"""BigQuery Google Patents adapter — the always-on recall backbone.

Queries OUR OWN clustered cache `nimo-gpt.patents_cache.pubs` (a one-time ETL
from `patents-public-data.patents.publications`, denormalized one row per
(publication, cpc4), CLUSTER BY cpc4, country). Clustering means a query
filtering on cpc4 scans only the relevant blocks — measured ~10-40 MB billed per
query vs 60-175 GB for a naive scan of the public tables.

COVERAGE, measured rather than assumed (this docstring used to say "since 2000",
which is wrong by 24 years and would have ruled the source out for exactly the
old art examiners cite most):

    32,409,744 publications, 73,550,167 rows
    1976-01-06 to 2026-04-21
    US 17.2M, EP 9.1M, WO 6.1M — no CN, JP, KR or DE national publications

Cost guards (belt + suspenders):
  * maximum_bytes_billed = 2 GB hard cap (fail-soft if a query would exceed it).
  * only runs when the caller supplies CPC hints (so we never full-scan title).
  * matches keywords in the small `title` column only (never `abstract`).
  * SELECT only small columns; results cached 24h (patents don't change).

Auth: Application Default Credentials / the GCE service account (project nimo-gpt),
the same SA that powers Vertex Gemini. No per-search quota — search_available()
is always true; the only limit is the byte cap.

Ported from patents-app `adapters/bigquery_gpatents.py` unchanged in behavior.
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx

from .schema import Candidate, SubQuery, canonical_pub, google_url
from .base import Adapter, CACHE

PROJECT = os.environ.get("GCP_PROJECT", "nimo-gpt")
TABLE = os.environ.get("BQ_PATENTS_TABLE", "nimo-gpt.patents_cache.pubs")
MAX_BYTES = int(os.environ.get("BQ_MAX_BYTES_BILLED", str(2_000_000_000)))  # 2 GB
COUNTRIES = ["US", "EP", "WO"]
#  Rows RETURNED, not rows scanned: the byte cap bills the columns the WHERE touches, and LIMIT
#  is applied after, so raising this is close to free. At 100 it was the tightest bottleneck in
#  the keyword channel -- a 0.1% sample of the match set.
ROW_LIMIT = int(os.environ.get("BQ_ROW_LIMIT", "400"))
#  Title terms OR-ed into one query. At 4 it discarded most of a caller's keyword
#  set; the ORDER BY now ranks by how many matched, so a wider OR costs precision
#  nothing and buys the long tail.
KW_LIMIT = int(os.environ.get("BQ_KW_LIMIT", "8"))

_STOP = set("a an the of and or for to in on with without by from as at is are be this that "
            "using use used method system apparatus device having comprising said wherein which "
            "these those means configured able about into via per not no non more most any".split())

_client = None
_client_err = ""


def _get_client():
    global _client, _client_err
    if _client is not None or _client_err:
        return _client
    try:
        from google.cloud import bigquery
        _client = bigquery.Client(project=PROJECT)
    except Exception as e:  # pragma: no cover
        _client_err = str(e)[:200]
        _client = None
    return _client


def _keywords(text: str, k: int = 4) -> list:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", (text or "").lower())
    seen, out = set(), []
    for w in words:
        if w in _STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:k]


def _cpc4s(sq: SubQuery) -> list:
    codes = list(sq.cpc or [])
    # sq.native may also carry cpc-ish tokens; but rely on structured sq.cpc
    out, seen = [], set()
    for c in codes:
        c4 = re.sub(r"[^A-Z0-9]", "", str(c).upper())[:4]
        if len(c4) == 4 and c4 not in seen:
            seen.add(c4)
            out.append(c4)
    return out[:8]


def _fmt_date(v) -> str:
    s = re.sub(r"\D", "", str(v or ""))
    if len(s) == 8:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s[:4] if s else ""


class BigQueryGPatents(Adapter):
    name = "bigquery_gpatents"

    def enabled(self) -> bool:
        return _get_client() is not None

    def disabled_reason(self) -> str:
        return f"BigQuery client unavailable ({_client_err or 'google-cloud-bigquery not importable'})"

    def search_available(self) -> bool:
        # no per-search quota — only the byte cap. Always on when reachable.
        return self.enabled()

    def search_note(self) -> str:
        return "cost-capped (2 GB/query)"

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        cli = _get_client()
        if cli is None:
            return []
        cpc4 = _cpc4s(sq)
        if not cpc4:
            # no CPC hint → would force a full title scan; skip to protect the byte cap
            return []
        kws = _keywords(str(sq.native), KW_LIMIT)
        ck = f"bq:{','.join(sorted(cpc4))}:{','.join(kws)}"
        hit = await CACHE.get(ck)
        if hit is None:
            try:
                hit = await asyncio.to_thread(self._run_query, cli, cpc4, kws)
            except Exception:
                return []
            await CACHE.set(ck, hit, 86400)  # patents don't change
        out: list[Candidate] = []
        for i, r in enumerate(hit):
            # canonical_pub repairs the missing leading zero the dataset drops from
            # US pre-grant numbers (US-2019183307-A1 -> US20190183307A1)
            pub = canonical_pub(r.get("publication_number", ""))
            if not pub:
                continue
            out.append(Candidate(
                pub_number=pub,
                source=self.name,
                source_rank=i + 1,
                title=r.get("title", "") or "",
                assignee=r.get("assignee", "") or "",
                date=_fmt_date(r.get("publication_date")),
                priority_date=_fmt_date(r.get("priority_date")),
                cpc=[c for c in (r.get("cpc_codes") or [])][:12],
                url=google_url(pub),
                found_by=[sq.rationale or sq.element],
                extra={"country": r.get("country", "")},
            ))
        return out

    def _run_query(self, cli, cpc4: list, kws: list) -> list:
        from google.cloud import bigquery
        params = [
            bigquery.ArrayQueryParameter("cpc4", "STRING", cpc4),
            bigquery.ArrayQueryParameter("countries", "STRING", COUNTRIES),
        ]
        # keyword match in the small title column only (never abstract) — OR of terms.
        #
        # WORD BOUNDARIES, not substrings. `LOWER(title) LIKE '%air%'` also matches chair,
        # repair, hair, airbag and staircase, which turned a term meant to be selective into
        # one matching a large fraction of every subclass. The match set then overflowed the
        # row limit with documents that do not contain the word at all.
        #
        # WEIGHTED by position, not a flat count. The caller orders its terms most-specific
        # first, and a flat count collapses everything into a handful of tiers -- in a real
        # subclass the "matched exactly one term" tier runs to tens of thousands of documents,
        # so which of them came back was decided by RAND(). Weighting means a title carrying
        # the rare word (silencer) outranks one carrying the common word (air) instead of
        # tying with it.
        kw_clause = ""
        rank_expr = "0"
        if kws:
            ors, hits = [], []
            n = len(kws)
            for j, w in enumerate(kws):
                params.append(bigquery.ScalarQueryParameter(
                    f"kw{j}", "STRING", r"\b" + re.escape(w) + r"\b"))
                pred = f"REGEXP_CONTAINS(LOWER(title), @kw{j})"
                ors.append(pred)
                hits.append(f"{n - j} * CAST({pred} AS INT64)")
            kw_clause = " AND (" + " OR ".join(ors) + ")"
            rank_expr = " + ".join(hits)
        sql = (
            "SELECT publication_number, country, title, assignee, "
            f"publication_date, priority_date, cpc_codes, ({rank_expr}) AS kw_hits "
            f"FROM `{TABLE}` "
            "WHERE cpc4 IN UNNEST(@cpc4) AND country IN UNNEST(@countries)"
            f"{kw_clause} "
            # An OR over several terms inside a whole CPC subclass matches a very large set, and
            # `ORDER BY RAND() LIMIT 100` returned a ~0.1% RANDOM SAMPLE of it: the same query
            # twice gave different documents, and finding any particular reference was luck. Rank
            # by HOW MANY of the terms the title matched first — a title carrying three of the
            # four query words is the one an examiner would reach for — and keep the fingerprint
            # only as the tie-break inside a match-count tier, which is what de-biases the
            # (cpc4,country) clustering.
            # DETERMINISTIC. RAND() meant the same query returned different documents on every
            # run, so a result could not be replayed and every A/B carried avoidable noise on top
            # of the effect being measured. FARM_FINGERPRINT of the publication number is
            # uncorrelated with the (cpc4, country) clustering, which is what the de-biasing
            # actually needs, and is stable across runs.
            f"ORDER BY kw_hits DESC, FARM_FINGERPRINT(publication_number) LIMIT {ROW_LIMIT}"
        )
        cfg = bigquery.QueryJobConfig(
            query_parameters=params,
            maximum_bytes_billed=MAX_BYTES,
            use_query_cache=True,
        )
        job = cli.query(sql, job_config=cfg)
        rows = list(job.result())
        self._last_bytes_billed = getattr(job, "total_bytes_billed", 0) or 0
        # dedup by publication_number (denormalized table has one row per cpc4)
        seen, out = set(), []
        for r in rows:
            pn = r.get("publication_number")
            if pn in seen:
                continue
            seen.add(pn)
            out.append({
                "publication_number": pn,
                "country": r.get("country"),
                "title": r.get("title"),
                "assignee": r.get("assignee"),
                "publication_date": r.get("publication_date"),
                "priority_date": r.get("priority_date"),
                "cpc_codes": list(r.get("cpc_codes") or []),
            })
        return out

    async def similar_pull(self, cpc4: list, limit: int = 300,
                           keywords=None) -> list:
        """Candidate pool for semantic recall / more-like-this: patents in the seed's
        cpc4 + our countries, WITH abstract (for embedding). Cost-capped ~1 GB.

        CRITICAL: the cache is clustered by (cpc4, country), so a plain LIMIT returns
        a single-country block (all EP or all US) — useless. We order by a hash of the
        publication number to get a country-mixed representative sample that is the SAME
        sample every run, and (when keywords are given) keyword-filter title+abstract
        first so the pool is RELEVANT across all countries."""
        cli = _get_client()
        cpc4 = [c for c in (cpc4 or []) if c and not c.startswith("Y")][:3]
        if cli is None or not cpc4:
            return []
        kws = [w for w in (keywords or []) if w][:6]
        ck = f"bqsim:{','.join(sorted(cpc4))}:{limit}:{','.join(sorted(kws))}"
        hit = await CACHE.get(ck)
        if hit is not None:
            return hit

        def _q():
            from google.cloud import bigquery
            params = [bigquery.ArrayQueryParameter("cpc4", "STRING", cpc4),
                      bigquery.ArrayQueryParameter("countries", "STRING", COUNTRIES)]
            kw_clause = ""
            if kws:
                ors = []
                for j, w in enumerate(kws):
                    params.append(bigquery.ScalarQueryParameter(f"k{j}", "STRING", f"%{w.lower()}%"))
                    ors.append(f"LOWER(title) LIKE @k{j} OR LOWER(abstract) LIKE @k{j}")
                kw_clause = " AND (" + " OR ".join(ors) + ")"
            sql = ("SELECT publication_number, country, title, abstract, assignee, "
                   "publication_date "
                   f"FROM `{TABLE}` "
                   "WHERE cpc4 IN UNNEST(@cpc4) AND country IN UNNEST(@countries)"
                   f"{kw_clause} "
                   "ORDER BY FARM_FINGERPRINT(publication_number) LIMIT " + str(limit))
            cfg = bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=1_000_000_000, use_query_cache=True)
            job = cli.query(sql, job_config=cfg)
            rows = list(job.result())
            seen, out = set(), []
            for r in rows:
                pn = r.get("publication_number")
                if pn in seen:
                    continue
                seen.add(pn)
                out.append({"publication_number": pn, "country": r.get("country"),
                            "title": r.get("title") or "", "abstract": r.get("abstract") or "",
                            "assignee": r.get("assignee") or "",
                            "publication_date": r.get("publication_date")})
            return out
        try:
            res = await asyncio.to_thread(_q)
        except Exception:
            return []
        await CACHE.set(ck, res, 86400)
        return res
