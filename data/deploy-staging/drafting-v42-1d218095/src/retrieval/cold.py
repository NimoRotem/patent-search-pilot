"""The cold tier: every channel this package already runs, pointed at a woken domain shard.

WHAT THIS IS. `shard_router.route` says which CPC-domain shards a query needs, `shard_router.wake`
asks for them, `shard_manager.hot_domains` says which of them are actually queryable, and
`shard_manager.connection` hands back a psycopg connection to one. This module is what turns that
connection into results: it binds the SAME `Retriever` to the shard and runs the SAME channel code
against it (`retrieval.channels`), so `cold:dense` cannot drift from `dense`.

THREE PROPERTIES THAT ARE NOT NEGOTIABLE, each of which has a test that injects the failure rather
than asserting the happy path (`tests/test_retrieval_cold.py`):

* PARALLEL, NEVER AFTER. The cold tier is submitted in the same phase as the hot one, on its own
  lane, so a shard that takes the full 20 s wake budget costs the search nothing the hot tier was
  not already spending. Waiting for the hot answer first and then going cold would add the wake to
  every search that needs one.
* DEGRADE, NEVER RAISE. A shard that will not wake, will not connect, or raises mid-query
  contributes nothing and is recorded in `Result.tiers["cold"]`. It never reaches the caller as an
  exception, because the local answer is complete without it.
* NOTHING WHEN NOTHING IS REGISTERED. With no shard backend registered this module issues ZERO
  queries and creates ZERO threads: `shard_manager.available()` is checked first, before routing,
  because `shard_router.route` reads the database and the corpus-wide prior is a 7.9 s GROUP BY.
  "Identical to today when the new backends are absent" has to mean identical, including cost.

WHY A COLD HIT NEEDS ITS FAMILY KEY FROM THE SHARD. `RetrieverBase` preloads publication -> family
for the HOT corpus. A shard holds publications that map has never seen, so `family_key` would fall
back to `str(pid)`, every cold hit would be its own family, and a cold hit and a hot hit of one
disclosure would both survive to fusion, where RRF splits the family's votes between two ids and
neither wins. The bound retriever therefore hydrates the families of the ids it is about to
collapse, in one batched query against the shard, through the `hydrate_families` hook.

REQUIRED OF WORKSTREAM E, and stated in `docs/shard_and_global_seams.md`:

* A publication id on a shard is the SAME id as in the hot corpus. Hydration fills gaps in the hot
  family map and never overwrites an entry, so a shard that renumbers its publications would
  silently attribute a hot family to a cold document.
* `release(domain, conn)` must reset or discard session state if connections are pooled. The cold
  tier applies the ANN scan profile (`hnsw.ef_search` and friends) to the connection it is handed,
  exactly as the hot path does to its own, because a dense channel at the shard's defaults is a
  different query.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import db

from . import base as _base
from . import channels, shard_manager, shard_router
from .family import family_key_sql

#  The channel-name prefix that marks a hit as coming from a cold shard. `fusion.channel_weight`
#  resolves `cold:dense` to the weight of `dense`: the same query against a different host is the
#  same evidence.
PREFIX = "cold:"

#  A backstop on the whole tier, in seconds, checked when the tier task STARTS and again between
#  collecting one shard's results and the next. It is not a way to interrupt a query in flight (no
#  such thing exists); it is what stops a tier that queued behind other work, or a shard that hangs
#  after waking, from becoming the critical path of a search whose local answer is already done.
TIER_BUDGET = float(os.environ.get("SHARD_TIER_BUDGET", "90"))

#  Shards queried at once. One connection per shard, and a psycopg connection carries one statement
#  at a time, so this is also the number of concurrent shard queries. Defaults to the router's own
#  ceiling: routing never recommends more domains than this anyway.
MAX_FANOUT = int(os.environ.get("SHARD_MAX_FANOUT", str(shard_router.MAX_ROUTES)))


def available() -> bool:
    """True only when a shard backend is registered. The gate on every query this module makes."""
    return shard_manager.available()


def cold_name(kind):
    return PREFIX + kind


def is_cold(name):
    return isinstance(name, str) and name.startswith(PREFIX)


def route_connection():
    """The connection `ColdTier.routes` reads the routing evidence on.

    A function and not an inline `db.connect` so it is a seam: `retrieval.testing` hands it a
    double, and a deployment that wants routing to read a replica rather than the live corpus can
    replace it without touching the tier.
    """
    return db.connect(autocommit=True, readonly=True)


# ---- binding a retriever to another corpus -----------------------------------------------------
def bind(retriever, conn, *, domain=None, wide=False, foreign=True):
    """A retriever of the SAME class, running the SAME channels, against `conn`.

    Must be called ON THE THREAD that will run the queries: `RetrieverBase.conn` hands the owning
    thread its own connection and every other thread a pool connection, and "owning" is recorded
    as the thread that assigned it.

    `_fam` is SHARED with the parent, deliberately. Family keys hydrated from a shard become
    visible to the parent's fusion and family dedup, which is the only way a cold hit and a hot hit
    of one disclosure can collapse into one row. Dict item assignment is atomic under the GIL and
    the map is otherwise immutable during a search, which is the same assumption `fork()` and
    `register_external` already make.

    `foreign=False` binds to a HOT connection (the routing probe does this) and leaves the family
    map alone.

    Only the state that is CORPUS-INDEPENDENT is carried over, named field by field rather than by
    copying `__dict__`. Everything else on a retriever is tied to the connection it was built for:
    the lexical backend is resolved against a specific retriever, and a cached anything is a cache
    of the hot corpus. Copying the lot would make "the same SQL against a different host" quietly
    untrue the first time somebody adds a per-connection cache.
    """
    r = object.__new__(type(retriever))
    r.__dict__["_fam"] = retriever._fam
    r.__dict__["_force_xlingual"] = retriever.__dict__.get("_force_xlingual", False)
    r.__dict__["_wide"] = bool(wide)
    r.__dict__["_conn"] = conn
    r.__dict__["_conn_tid"] = threading.get_ident()
    r.__dict__["_shard_domain"] = domain
    if foreign:
        r.__dict__["_needs_hydration"] = True
        r.__dict__["_hydrated"] = set()
        r.__dict__["hydrate_families"] = _hydrator(r)
        try:
            _base._apply_scan_profile(conn, bool(wide))
        except Exception:                                              # noqa: BLE001
            #  A shard that will not take the ANN profile still answers; it answers at its own
            #  defaults. A narrower scan is thinner recall, not a failed search.
            pass
    return r


def _hydrator(r):
    """`hydrate_families` for a shard-bound retriever: one batched lookup, gaps only."""
    def hydrate_families(pids):
        fam, done = r.__dict__["_fam"], r.__dict__["_hydrated"]
        todo = [p for p in dict.fromkeys(pids)
                if not isinstance(p, str) and p not in fam and p not in done]
        if not todo:
            return
        done.update(todo)
        try:
            with r.conn.cursor() as c:
                c.execute(f"SELECT id, {family_key_sql()} k "
                          "FROM publications WHERE id = ANY(%s)", (list(todo),))
                rows = c.fetchall()
        except Exception:                                              # noqa: BLE001
            #  No family keys means each cold hit is its own family. That is the SAFE direction:
            #  nothing is merged that should not have been, and the cost is a duplicate row.
            return
        for row in rows:
            pid = row["id"]
            #  Gaps only. Never relabel a publication the hot corpus already has an opinion about.
            if pid not in fam:
                fam[pid] = row["k"]
    return hydrate_families


# ---- the tier ----------------------------------------------------------------------------------
class ColdTier:
    """Per-search cold state: routes, which shards were woken, which were already queried.

    One instance per `search()`, consulted once in phase 1 and once in phase 2. Phase 2 re-routes,
    because the candidate distribution is 50% of the routing mix and it does not exist until the
    cheap tiers have answered; a domain that only phase 2's evidence indicates is woken then, and
    gets the phase 1 channels run against it as well, since it was not reachable earlier.
    """

    def __init__(self, retriever, args, wide=False):
        self.r = retriever
        self.args = args
        self.wide = bool(wide)
        self._lock = threading.Lock()
        self._pcpc = None
        self.status = {"routes": [], "hot": [], "queried": [], "errors": {}, "waits": [],
                       "wake": [], "available": available()}
        self._queried = set()

    # -- routing ---------------------------------------------------------------------------
    def _subject_cpc(self, conn):
        """The subject's own symbols, most specific first. Cached: it is the same every phase."""
        if self._pcpc is None:
            try:
                probe = bind(self.r, conn, wide=self.wide, foreign=False)
                self._pcpc = list(probe.subject_cpc(self.args.subject) or ())
            except Exception:                                          # noqa: BLE001
                self._pcpc = []
        return self._pcpc

    def routes(self, candidate_pids=(), citation_pids=()):
        """`shard_router.route` on a short-lived read-only connection of this tier's own.

        Its own connection, and not a pool one, because this runs on the REMOTE lane: a lane whose
        whole point is that it does not spend the hot database's bounded connections. One transient
        connection per search that actually has shards is a smaller cost than a permanent one per
        remote worker thread.

        The `citations` source of the routing mix has no input here. The citation neighbours are
        produced by the citation channel, which runs in the same phase this routing decision
        starts, so consulting them would mean waiting for the hot tier: exactly the serialisation
        the architecture forbids. `route()` redistributes that 15% across the sources that did
        produce something, which is the documented behaviour and not a silent loss.
        """
        conn = None
        try:
            conn = route_connection()
            routes = shard_router.route(
                conn,
                candidate_pids=list(candidate_pids or ()),
                predicted_cpc=self._subject_cpc(conn),
                citation_pids=list(citation_pids or ()),
                family_key=self.r.family_key)
        except Exception as e:                                         # noqa: BLE001
            self.status["errors"]["route"] = f"{type(e).__name__}: {str(e)[:120]}"
            #  THE UNCLASSIFIED ROUTE IS NEVER DROPPED, not even by a failure to route: 20.6% of
            #  the corpus carries no classification and it skews to exactly the old, foreign art
            #  the gold lists are drawn from.
            routes = [{"domain": shard_router.UNCLASSIFIED, "weight": 1.0, "sources": {}}]
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:                                      # noqa: BLE001
                    pass
        return routes

    # -- one pass ---------------------------------------------------------------------------
    def run(self, kinds, *, catch_up=(), candidate_pids=(), citation_pids=(), seeds=(),
            deadline=None):
        """Query every hot shard and return {cold:<kind>: [(pid, score)]} pooled across shards.

        `catch_up` are the kinds a domain that is being queried for the FIRST time should also run,
        so a shard that only woke in phase 2 still contributes its dense and lexical evidence.
        """
        if not available():
            return {}
        if deadline is not None and time.monotonic() >= deadline:
            self.status["errors"]["tier"] = "budget spent before the tier started"
            return {}

        routes = self.routes(candidate_pids, citation_pids)
        self.status["routes"] = [r.get("domain") for r in routes]
        self.status["wake"].append((shard_router.wake(routes) or {}).get("state", "unknown"))

        t0 = time.monotonic()
        wake_budget = shard_manager.WAKE_TIMEOUT
        if deadline is not None:
            wake_budget = min(wake_budget, max(0.0, deadline - t0))
        hot = shard_manager.hot_domains(routes, timeout=wake_budget)
        self.status["waits"].append(round(time.monotonic() - t0, 3))
        self.status["hot"] = list(hot)
        if not hot:
            return {}

        plans = []
        for d in hot:
            with self._lock:
                first = d not in self._queried
                self._queried.add(d)
            ks = (tuple(catch_up) + tuple(kinds)) if first else tuple(kinds)
            ks = tuple(k for k in dict.fromkeys(ks) if channels.has_input(k, self.args))
            if ks:
                plans.append((d, ks))
        if not plans:
            return {}
        self.status["queried"] = sorted(set(self.status["queried"]) | {d for d, _ in plans})

        args = self.args.with_seeds(seeds) if seeds else self.args
        pool = ThreadPoolExecutor(max_workers=min(len(plans), max(1, MAX_FANOUT)),
                                  thread_name_prefix="cold")
        try:
            futures = [(d, pool.submit(self._query_domain, d, ks, args)) for d, ks in plans]
            merged = {}
            for d, fut in futures:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    part = fut.result(timeout=remaining)
                except FutureTimeout:
                    #  Not cancelled: the task owns the connection and releases it in its own
                    #  `finally`. Abandoning the RESULT is what the budget buys; abandoning the
                    #  connection would leak it on the shard.
                    self.status["errors"][d] = "budget exhausted"
                    continue
                except Exception as e:                                 # noqa: BLE001
                    self.status["errors"][d] = f"{type(e).__name__}: {str(e)[:120]}"
                    continue
                for kind, rows in part.items():
                    bucket = merged.setdefault(kind, {})
                    for pid, sc in rows:
                        if sc > bucket.get(pid, float("-inf")):
                            bucket[pid] = sc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        #  Pooled across shards, then collapsed to families ONCE, at the tier's cap. Each shard
        #  already collapsed its own hits; two shards can still hold two members of one family,
        #  which is the same problem `canonicalise` solves between channels.
        cap = _base.SEED_PUB_CAP if self.wide else _base.PUB_CAP
        out = {}
        for kind, bucket in merged.items():
            pooled = sorted(bucket.items(), key=lambda t: t[1], reverse=True)
            rows = self.r.collapse_pairs(pooled, cap)
            if rows:
                out[cold_name(kind)] = rows
        return out

    def _query_domain(self, domain, kinds, args):
        """One shard, one connection, its channels run sequentially on it.

        Sequentially and not concurrently because a psycopg connection carries one statement at a
        time, and the seam hands back one connection per domain. The parallelism that matters here
        is ACROSS shards, which is what the caller's pool provides.
        """
        conn = shard_manager.connection(domain)
        if conn is None:
            self.status["errors"][domain] = "no connection"
            return {}
        out = {}
        try:
            r = bind(self.r, conn, domain=domain, wide=self.wide)
            with _base.profile_context(self.wide):
                for kind in kinds:
                    try:
                        rows = channels.call(r, kind, args)
                    except Exception as e:                             # noqa: BLE001
                        self.status["errors"][f"{domain}:{kind}"] = (
                            f"{type(e).__name__}: {str(e)[:120]}")
                        continue
                    if rows:
                        out[kind] = list(rows)
        finally:
            shard_manager.release(domain, conn)
        return out
