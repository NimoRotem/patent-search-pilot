"""Federated patent sources — in-process port of App A's adapter stack.

The pilot used to reach the external sources over HTTP (`external.bulk()` ->
App A's /api/bulk_search on :8630). This package is those adapters ported into
the pilot itself, behind a SYNC facade, so the fan-out is one process with no
network hop and no second service to keep alive:

    import sources
    cands  = sources.search([{"source": "serpapi_gpatents", "q": '"vacuum lifter"',
                              "element": "gripper", "why": "head term"}])
    texts  = sources.fetch_fulltext(["US9876543B2", "EP1234567A1"])
    status = sources.health()

THREADING MODEL. App A is asyncio end-to-end; the pilot is threads (Flask +
gunicorn workers). The adapters stay async internally — their pacing, latches
and semaphores are asyncio constructs whose measured behavior we do not want to
re-derive — and this module owns ONE long-lived background event loop in a
daemon thread. Every facade call submits a coroutine to that loop and blocks on
the result. One loop for the process is load-bearing, not a convenience: the
adapters' asyncio primitives (pacing locks, semaphores, the TTL cache lock)
bind to the loop that first uses them, so a loop-per-call design would crash on
the second call, and per-call loops would also discard the shared TTL cache's
loop affinity guarantees.

BUDGETS. Every call to search()/bulk() gets a fresh Budget mirroring App A's
per-run caps: 12 queries per source (PATENTS_PER_SOURCE_CAP), SerpApi 40
(PATENTS_SERPAPI_CAP — carries both planner queries and keyword probes),
HimmPat 3 (HIMMPAT_QUERIES_PER_RUN — its hydrate bills PER RESULT), PQAI 6
(PATENTS_PQAI_CAP — the mediator route is capped 1,000/24h host-wide).
Queries over a cap are reported in the envelope's `skipped`, never silently
dropped — a cap that binds looks exactly like a source with nothing to say
unless it is named.

CREDENTIALS (env, never committed — the repo is public):
    SERPAPI_KEY (alias SERPAPI_API_KEY)
    SCRAPINGBEE_API_KEY, FIRECRAWL_API_KEY, TAVILY_API_KEY
    PQAI_TOKEN (+ optional PQAI_BASE_URL, default https://api.projectpq.ai)
    EPO_OPS_KEY / EPO_OPS_SECRET (aliases OPS_CONSUMER_KEY / OPS_CONSUMER_SECRET)
    USPTO_ODP_KEY (alias ODP_API_KEY)
    HIMMPAT_API_KEY
    IPA_CLIENT_ID / IPA_CLIENT_SECRET
    BigQuery uses Application Default Credentials (GCP_PROJECT, default nimo-gpt).

Quota ledgers persist under ~/.patents/ in App A's exact files
(himmpat_usage.json, pqai_mediator_calls.json, pqai_search_quota.json), so the
limits are shared per-host between App A and the pilot.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Optional

import httpx

from .schema import Candidate, SubQuery, dataclass_to_dict  # noqa: F401 (re-export)
from .base import Budget, CACHE  # noqa: F401 (re-export)
from .registry import all_adapters, registry, reset_adapters  # noqa: F401 (re-export)
from .fulltext import Acquisition

# Per-source timeout for one search call; the whole fan-out shares one client.
FANOUT_TIMEOUT = float(os.environ.get("PATENTS_FANOUT_TIMEOUT", "45"))
# Overall wall-clock ceiling for one bulk()/search() call.
BULK_TIMEOUT = float(os.environ.get("PATENTS_BULK_TIMEOUT", "75"))
MAX_BULK_QUERIES = int(os.environ.get("PATENTS_MAX_BULK_QUERIES", "80"))
# Concurrent facade calls actually fanning out (App A held this at 2).
BULK_CONCURRENCY = int(os.environ.get("PATENTS_BULK_CONCURRENCY", "2"))

PER_SOURCE_CAP = int(os.environ.get("PATENTS_PER_SOURCE_CAP", "12"))


def budget_caps() -> dict:
    """Fresh per-run caps, App A's numbers (pipeline.py lines 314-327)."""
    caps = {a.name: PER_SOURCE_CAP for a in all_adapters()}
    # token-free mediator route (1000/24h host-wide) — cast wider; still well under cap
    caps["pqai"] = int(os.environ.get("PATENTS_PQAI_CAP", "6"))
    # HimmPat is a low-allowance trial key whose bibliographic hydrate bills PER
    # RESULT, so a wide fan-out here costs real money for little marginal recall.
    # Three searches per run (one semantic + two expressions) is the whole budget.
    caps["himmpat"] = int(os.environ.get("HIMMPAT_QUERIES_PER_RUN", "3"))
    # SerpApi carries both the planner's boolean queries and the deterministic
    # keyword probes, so it needs room for both. Sized against the actual plan
    # rather than caution: ~40 searches plus ~120 detail fetches is ~160 credits a
    # run, against 30,000/month running at ~1,000. A cap of 26 was binding from
    # round 3 onwards in App A, which is why the probe count read 0 for the back
    # half of the search — the source had simply gone quiet without saying so.
    caps["serpapi_gpatents"] = int(os.environ.get("PATENTS_SERPAPI_CAP", "40"))
    return caps


# ---------------------------------------------------------------------------
# the one background loop (see module docstring)
# ---------------------------------------------------------------------------
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOOP_LOCK = threading.Lock()
_BULK_SEM: Optional[asyncio.Semaphore] = None    # created lazily ON the loop


def _loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, name="sources-loop", daemon=True)
            t.start()
            _LOOP = loop
    return _LOOP


def _submit(coro, timeout: Optional[float] = None):
    """Run a coroutine on the shared background loop and wait for its result.
    This is the only bridge between the pilot's threads and the async adapters
    (tests use it too, so every asyncio primitive lives on one loop)."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop())
    try:
        return fut.result(timeout)
    except Exception:
        fut.cancel()
        raise


def _bulk_sem() -> asyncio.Semaphore:
    global _BULK_SEM
    if _BULK_SEM is None:
        _BULK_SEM = asyncio.Semaphore(BULK_CONCURRENCY)
    return _BULK_SEM


def _new_client(timeout: float) -> httpx.AsyncClient:
    """One shared client per facade call (tests monkeypatch this)."""
    limits = httpx.Limits(max_connections=24, max_keepalive_connections=12)
    return httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True)


def _candidate_dict(c: Candidate, query_i: int, element: str) -> dict:
    """The exact row shape App A's /api/bulk_search returned, so external.py's
    best_records()/materialise() consume this without change."""
    return {
        "pub_number": c.pub_number, "source": c.source,
        "source_rank": c.source_rank, "title": c.title,
        "abstract": c.abstract, "snippet": c.snippet,
        "assignee": c.assignee, "date": c.date,
        "priority_date": c.priority_date, "kind": c.kind,
        "cpc": list(c.cpc or []), "url": c.url,
        "family_id": c.family_id,
        "query_i": query_i, "element": element,
    }


async def _bulk_async(queries: list, timeout: float) -> dict:
    """Fan a list of sub-query dicts across the sources, fail-soft per source.
    Mirrors App A's /api/bulk_search handler plus the pipeline's Budget filter."""
    adapters = registry()
    budget = Budget(budget_caps())

    subs: list = []
    skipped: list = []
    for i, q in enumerate(queries[:MAX_BULK_QUERIES]):
        q = dict(q or {})
        source = str(q.get("source") or "")
        a = adapters.get(source)
        if a is None or not a.search_available():
            skipped.append({"i": i, "source": source,
                            "why": "unknown source" if a is None else
                                   (a.disabled_reason() or a.search_note()
                                    or "search unavailable")})
            continue
        if not budget.allow(source):
            # A per-source cap that binds looks exactly like a source with nothing
            # left to say. Name it, or the back half of a run quietly fans out over
            # fewer sources than the front half and the caller never learns it.
            skipped.append({"i": i, "source": source,
                            "why": f"budget cap reached ({budget.caps.get(source)})"})
            continue
        budget.charge(source)
        subs.append((i, SubQuery(source=source, native=q.get("q", ""),
                                 rationale=str(q.get("why", "") or ""),
                                 element=str(q.get("element", "") or ""),
                                 cpc=list(q.get("cpc") or []),
                                 date_from=q.get("date_from") or None,
                                 date_to=q.get("date_to") or None)))

    t0 = time.time()
    stats: dict = {}
    errors: list = []
    out: list = []

    async with _bulk_sem():
        client = _new_client(timeout)
        try:
            async def _run(i, sq):
                try:
                    return i, sq, await asyncio.wait_for(
                        adapters[sq.source].search(sq, client), timeout=timeout)
                except Exception as e:
                    return i, sq, e

            for i, sq, res in await asyncio.gather(*[_run(i, s) for i, s in subs]):
                st = stats.setdefault(sq.source, {"queries": 0, "hits": 0, "errors": 0})
                st["queries"] += 1
                if isinstance(res, Exception):
                    st["errors"] += 1
                    errors.append({"i": i, "source": sq.source,
                                   "q": str(sq.native)[:120],
                                   "error": f"{type(res).__name__}: {str(res)[:160]}"})
                    continue
                st["hits"] += len(res)
                for c in res:
                    out.append(_candidate_dict(c, i, sq.element))
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    # Drain adapter warning buffers (contract drift, truncation notices) so a
    # source that degraded without raising is still VISIBLE to the caller.
    warnings: list = []
    for a in adapters.values():
        pop = getattr(a, "pop_warnings", None)
        if callable(pop):
            for w in pop():
                warnings.append({"source": a.name, "warning": w})

    return {"ok": True, "candidates": out, "stats": stats, "errors": errors,
            "skipped": skipped, "warnings": warnings,
            "elapsed": round(time.time() - t0, 2),
            "n_queries": len(subs), "n_candidates": len(out),
            "budget_used": dict(budget.used)}


def bulk(queries: list, timeout: Optional[float] = None) -> dict:
    """Full fan-out envelope: {ok, candidates, stats, errors, skipped, warnings,
    elapsed, n_queries, n_candidates, budget_used} — the same contract App A's
    /api/bulk_search returned, so external.py can swap its HTTP call for this."""
    if not queries:
        return {"ok": False, "candidates": [], "stats": {}, "errors": [],
                "skipped": [], "warnings": [], "error": "no queries",
                "elapsed": 0.0, "n_queries": 0, "n_candidates": 0, "budget_used": {}}
    t = max(5.0, min(float(timeout or BULK_TIMEOUT), 180.0))
    return _submit(_bulk_async(list(queries), t), timeout=t + 60)


def search(queries: list) -> list:
    """Fan the sub-query dicts ({source, q, cpc, element, why, date_from, date_to})
    across the sources and return the raw candidate dicts. Fail-soft: a dead
    source contributes nothing (its error is in bulk()'s envelope), it never
    raises. Callers who need the per-source stats/errors should use bulk()."""
    return bulk(queries).get("candidates", [])


# ---------------------------------------------------------------------------
# full text
# ---------------------------------------------------------------------------
async def _fulltext_async(pubs: list, want, timeout: float) -> dict:
    acq = Acquisition(registry())
    client = _new_client(timeout)
    try:
        recs = await acq.fetch(list(pubs), client, want=want)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
    out: dict = {}
    for pn, rec in recs.items():
        out[pn] = {
            "claims": rec.get("claims", "") or "",
            "description": rec.get("description", "") or "",
            "abstract": rec.get("abstract", "") or "",
            "title": rec.get("title", "") or "",
            "source": (rec.get("source") or rec.get("fulltext_source") or ""),
        }
    out["_summary"] = acq.summary()
    return out


def fetch_fulltext(pubs: list, want: Optional[int] = None,
                   timeout: Optional[float] = None) -> dict:
    """Full text for as many of `pubs` as the acquisition ladder can supply:
    {pub_number: {claims, description, abstract, title, source}}, plus a
    "_summary" key with the per-source tally / spend / warnings. Everything
    acquired is persisted to the Postgres docstore, so the ladder gets shorter
    every time it runs."""
    if not pubs:
        return {"_summary": {"by_source": {}, "paid_documents": 0, "paid_usd": 0.0,
                             "scrapingbee_pages": 0, "scrapingbee_credits": 0,
                             "warnings": []}}
    t = max(30.0, float(timeout or (BULK_TIMEOUT * 4)))
    return _submit(_fulltext_async(list(pubs), want, FANOUT_TIMEOUT), timeout=t)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def health() -> dict:
    """Per-source availability including the quota latches, plus the ledgers the
    vendors do not expose (HimmPat has no balance endpoint: our own ledger is
    the only place that spend is visible)."""
    out: dict = {"sources": [], "ok": True}
    for a in all_adapters():
        try:
            row = {"name": a.name,
                   "enabled": bool(a.enabled()),
                   "search_available": bool(a.search_available()),
                   "in_report_fanout": bool(getattr(a, "in_report_fanout", True)),
                   "note": a.search_note(),
                   "reason": a.disabled_reason() if not a.enabled() else ""}
        except Exception as exc:  # a health probe must never take health() down
            row = {"name": getattr(a, "name", "?"), "enabled": False,
                   "search_available": False, "note": "",
                   "reason": f"health probe failed: {str(exc)[:120]}"}
        out["sources"].append(row)
    try:
        from .himmpat import usage as _himm_usage
        out["himmpat_usage"] = _himm_usage()
    except Exception as exc:
        out["himmpat_usage"] = {"error": str(exc)[:120]}
    try:
        from .pqai import mediator_note, quota_note
        out["pqai"] = {"mediator": mediator_note(), "token_quota": quota_note()}
    except Exception as exc:
        out["pqai"] = {"error": str(exc)[:120]}
    try:
        from . import gpatents_direct as G
        out["gpatents_direct"] = {"available": G.available(),
                                  "block_reason": G.block_reason()}
    except Exception as exc:
        out["gpatents_direct"] = {"error": str(exc)[:120]}
    # The corpus is what makes a second search in a field nearly free, so its
    # size is an operational number, not a curiosity. Fail-soft: health() must
    # answer even where Postgres is unreachable.
    try:
        from . import docstore
        out["docstore"] = docstore.stats_sync()
    except Exception as exc:
        out["docstore"] = {"error": str(exc)[:160]}
    return out
