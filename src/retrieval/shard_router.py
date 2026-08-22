"""SEAM: which dormant domain shards a query should wake. Workstream E owns the VMs.

WHAT THIS PRODUCES. A ranked list of domain ids with weights, plus an always-present
unclassified/multidomain route:

    [{"domain": "B66C", "weight": 0.31, "sources": {...}}, ..., {"domain": "unclassified", ...}]

A DOMAIN IS A CPC SUBCLASS (four characters, e.g. `B66C`). That is the granularity the corpus
actually splits on: the eight seed subgroups sit inside five subclasses, and the neighbouring art
the benchmark subjects need (`B25J` robot grippers, `B65G` conveyors, `B23Q` machine tools) is one
subclass away, not one group away. Finer than a subclass and the router wakes a shard per query
with no reuse; coarser than a subclass (a three-character class, or a one-letter section) and
`B65G`'s 6.8M classification rows are one route and the routing decision carries no information.

THE MIX, as specified:

      50%  global candidate family distribution   what the wide, cheap tiers already found
      25%  predicted CPC / domain                 the subject's own symbols, most specific first
      15%  citation neighbour distribution        where this subject's citation graph lives
      10%  historical domain prior                corpus-wide frequency

The first three are query evidence and the last is a floor. A source that produced nothing does
not silently vanish: its weight is redistributed across the sources that DID produce something,
and the source breakdown is returned on every route, so a route can be explained.

THE UNCLASSIFIED ROUTE IS NEVER DROPPED. MEASURED: 1,024,320 publications in this corpus carry no
classification at all. A router that only ever wakes classified shards makes 20.6% of the corpus
unreachable, and unclassified documents skew old and foreign, which is exactly the population the
gold citation lists are drawn from. It is emitted with at least its corpus share, always, and it
is the reason `route()` can never return an empty list.

UNTIL WORKSTREAM E LANDS. `wake()` is a no-op that reports that no shard was woken, and the
orchestrator's cold channels return []. `route()` is NOT a no-op: it is real, it is measurable, and
E consumes it.
"""
from __future__ import annotations

import os
import threading

#  CPC subclass. See the module docstring for why four characters.
DOMAIN_LEN = 4
UNCLASSIFIED = "unclassified"

SOURCE_WEIGHTS = {
    "candidates": 0.50,
    "predicted_cpc": 0.25,
    "citations": 0.15,
    "prior": 0.10,
}

#  How many shards a router may recommend waking. The architecture says 2-5 dormant domain VMs;
#  this is the ceiling on the list, not a promise that E wakes all of them.
MAX_ROUTES = int(os.environ.get("SHARD_MAX_ROUTES", "5"))

_PRIOR_LOCK = threading.Lock()
_PRIOR = None


# ---- domain lookups --------------------------------------------------------------------------
def domain_of(symbol):
    """CPC/IPC symbol -> domain id. '' / None -> the unclassified route."""
    s = str(symbol or "").strip().upper().replace(" ", "")
    return s[:DOMAIN_LEN] if len(s) >= DOMAIN_LEN else UNCLASSIFIED


def domains_of_publications(conn, pids, weights=None):
    """{domain: weight} for a set of local publication ids.

    A publication with several symbols spreads its weight across their domains rather than voting
    once per symbol, so a heavily classified document does not out-vote a sparsely classified one
    (that miscount is exactly what makes `channel_cpc`'s count(*) ranking meaningless). A
    publication with NO classification votes for the unclassified route, which is how 20.6% of the
    corpus keeps a way in.
    """
    pids = [p for p in (pids or []) if not isinstance(p, str)]
    if not pids:
        return {}
    w = dict(weights or {})
    rows = {}
    with conn.cursor() as c:
        c.execute("SELECT publication_id, symbol FROM classifications "
                  "WHERE publication_id = ANY(%s) AND symbol IS NOT NULL", (list(pids),))
        for r in c.fetchall():
            rows.setdefault(r["publication_id"], set()).add(domain_of(r["symbol"]))
    out = {}
    for pid in pids:
        doms = rows.get(pid) or {UNCLASSIFIED}
        share = float(w.get(pid, 1.0)) / len(doms)
        for d in doms:
            out[d] = out.get(d, 0.0) + share
    return _normalise(out)


def historical_prior(conn, refresh=False):
    """{domain: share} over the whole corpus, computed once per process.

    MEASURED 2026-08-22 on the live corpus: 53,473,700 classification rows, 7.9 s for the GROUP BY.
    Paying that once per process is fine; paying it per search is not, which is why it is cached
    rather than recomputed. The unclassified share is counted from `publications`, not inferred.
    """
    global _PRIOR
    if _PRIOR is not None and not refresh:
        return _PRIOR
    with _PRIOR_LOCK:
        if _PRIOR is not None and not refresh:
            return _PRIOR
        counts = {}
        try:
            with conn.cursor() as c:
                c.execute("SELECT substr(symbol,1,%s) d, count(*) n FROM classifications "
                          "WHERE symbol IS NOT NULL GROUP BY 1", (DOMAIN_LEN,))
                for r in c.fetchall():
                    d = domain_of(r["d"])
                    counts[d] = counts.get(d, 0) + int(r["n"])
                c.execute("SELECT count(*) n FROM publications p WHERE NOT EXISTS "
                          "(SELECT 1 FROM classifications cl WHERE cl.publication_id = p.id)")
                n_unclassified = int((c.fetchone() or {}).get("n") or 0)
            if n_unclassified:
                counts[UNCLASSIFIED] = counts.get(UNCLASSIFIED, 0) + n_unclassified
        except Exception:
            #  A prior that cannot be read is a missing source, not a failed search: its 10% is
            #  redistributed by `route`.
            counts = {}
        _PRIOR = _normalise(counts)
        return _PRIOR


def _normalise(d):
    total = float(sum(v for v in d.values() if v > 0)) or 0.0
    if total <= 0:
        return {}
    return {k: v / total for k, v in d.items() if v > 0}


def _rank_weighted(pids, family_key=None):
    """A rank-decayed vote per publication: rank 1 counts most, rank 500 barely at all.

    1/(40 + rank) is the same shape as fusion's RRF term, deliberately: the router should believe
    the candidate list for the same reason the ranking does.
    """
    out, seen = {}, set()
    for rank, pid in enumerate(pids or []):
        fk = family_key(pid) if family_key else pid
        if fk in seen:
            continue
        seen.add(fk)
        out[pid] = 1.0 / (40.0 + rank + 1.0)
    return out


# ---- the routing distribution ------------------------------------------------------------------
def route(conn, *, candidate_pids=(), predicted_cpc=(), citation_pids=(), family_key=None,
          max_routes=MAX_ROUTES, include_prior=True):
    """-> [{"domain", "weight", "sources"}] best-first, always including an unclassified route.

    `candidate_pids`   local publication ids already found, best-first (the cheap tiers' answer).
    `predicted_cpc`    the subject's own symbols, most specific first (`Retriever.subject_cpc`).
    `citation_pids`    publication ids reached along the citation graph, best-first.
    `family_key`       optional pid -> family key, so one family votes once.
    """
    parts = {}
    if candidate_pids:
        parts["candidates"] = domains_of_publications(
            conn, list(candidate_pids), _rank_weighted(candidate_pids, family_key))
    if predicted_cpc:
        #  Most specific first: symbol i gets 1/(i+1), so the subject's primary classification
        #  dominates and its twelfth symbol is a whisper.
        pc = {}
        for i, s in enumerate(predicted_cpc):
            d = domain_of(s)
            pc[d] = pc.get(d, 0.0) + 1.0 / (i + 1.0)
        parts["predicted_cpc"] = _normalise(pc)
    if citation_pids:
        parts["citations"] = domains_of_publications(
            conn, list(citation_pids), _rank_weighted(citation_pids, family_key))
    if include_prior:
        p = historical_prior(conn)
        if p:
            parts["prior"] = p

    parts = {k: v for k, v in parts.items() if v}
    if not parts:
        return [{"domain": UNCLASSIFIED, "weight": 1.0, "sources": {}}]
    #  Redistribute the weight of any source that produced nothing, so the mix stays a mix.
    live = sum(SOURCE_WEIGHTS[k] for k in parts)
    scale = 1.0 / live if live else 0.0

    merged, sources = {}, {}
    for name, dist in parts.items():
        w = SOURCE_WEIGHTS[name] * scale
        for dom, share in dist.items():
            merged[dom] = merged.get(dom, 0.0) + w * share
            sources.setdefault(dom, {})[name] = round(w * share, 6)

    ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[:max(1, int(max_routes))]
    #  THE UNCLASSIFIED ROUTE IS NEVER DROPPED (see the module docstring). If it did not make the
    #  cut on its own weight, it is appended at whatever weight it earned, or at the corpus share
    #  if it earned none.
    if not any(d == UNCLASSIFIED for d, _ in top):
        w = merged.get(UNCLASSIFIED)
        if w is None:
            w = (historical_prior(conn) or {}).get(UNCLASSIFIED, 0.0) * SOURCE_WEIGHTS["prior"]
        top = top + [(UNCLASSIFIED, w)]
    total = sum(w for _, w in top) or 1.0
    return [{"domain": d, "weight": round(w / total, 6), "sources": sources.get(d, {})}
            for d, w in top]


# ---- the wake seam ---------------------------------------------------------------------------
class ShardRouterBackend:
    """Workstream E implements this; `register_backend` installs it."""

    def wake(self, routes) -> dict:
        """Wake the shards named by `routes` (the output of `route`). -> a status dict."""
        raise NotImplementedError


class _NoWake(ShardRouterBackend):
    def wake(self, routes):
        return {"woken": [], "state": "not_implemented",
                "routes": [r["domain"] for r in (routes or [])]}


_BACKEND = _NoWake()


def register_backend(backend):
    global _BACKEND
    _BACKEND = backend or _NoWake()


def wake(routes):
    """Ask the shard manager to wake these domains. Never raises: a shard that will not wake is a
    channel that returns nothing, not a failed search."""
    try:
        return dict(_BACKEND.wake(list(routes or [])) or {})
    except Exception as e:
        return {"woken": [], "state": "failed", "note": str(e)[:200]}
