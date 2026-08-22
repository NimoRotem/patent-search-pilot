"""Synthetic shards and a synthetic global backend, behind the real seams.

WHY THIS IS IN `src/` AND NOT IN `tests/`. Three workstreams need it. Workstream G wires the cold
and global tiers against it, and workstream E and workstream F must be able to run the SAME
assertions against their real implementations: if the synthetic backend and the real one are
tested by different harnesses, the seam is only tested on one side of itself.

    from retrieval import testing
    with testing.installed(shards={"B66C": testing.shard("B66C", docs=[(1, "F1"), (2, "F2")])}):
        ...                                   # retrieval now has a cold tier

WHAT IT SIMULATES, because these are the failures that matter and the happy path is not a test:

    hot_from_start=False        a shard that must be woken, and takes `wake_after` seconds
    never_wake={"B65G"}         a shard that is still "waking" when the 20 s budget runs out
    connect_error={"B25J"}      `connection()` raising instead of returning
    fail_on={"dense"}           a channel that raises MID-QUERY, after the shard was hot
    latency=0.4                 a slow shard, for budget and overlap assertions
    Global(error=...)           a global backend whose search raises
    Global(available=False)     a backend that is mid-build and says so, which is REQUIRED of one:
                                an empty result and a genuine miss are indistinguishable
                                downstream, and a miss is scored as a recall failure

WHAT IT DOES NOT SIMULATE. Postgres. The fake connection answers the channel SQL by recognising
which channel is asking, so it proves the WIRING: routing, waking, parallelism, family collapse
across tiers, caps, pooling and degradation. It cannot prove that `to_tsvector` or `<=>` behave on
a shard. Those need a real shard, which is workstream E's, and the point of this double is that E
can swap it out under the same tests.
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field

from . import global_search, shard_manager, shard_router


# ---- what a fake shard holds -------------------------------------------------------------------
@dataclass
class ShardDoc:
    """One publication on a shard. `family` is what the cold tier must learn from the shard, and
    the reason it can dedup against a hot hit of the same disclosure."""
    publication_id: object
    family: str = ""
    score: float = 1.0
    publication_number: str = ""

    def key(self):
        return self.family or self.publication_number or str(self.publication_id)


def shard(domain, docs=(), channels=None, fail_on=(), latency=0.0, hot=True):
    """Build a `SyntheticShard`. `docs` is [(pid, family)] or [(pid, family, score)] or ShardDocs."""
    out = []
    for i, d in enumerate(docs):
        if isinstance(d, ShardDoc):
            out.append(d)
        elif isinstance(d, (tuple, list)):
            pid, fam = d[0], (d[1] if len(d) > 1 else "")
            score = float(d[2]) if len(d) > 2 else float(len(docs) - i)
            out.append(ShardDoc(pid, fam, score))
        else:
            out.append(ShardDoc(d, "", float(len(docs) - i)))
    return SyntheticShard(domain, out, channels=channels, fail_on=fail_on, latency=latency,
                          hot=hot)


class SyntheticShard:
    """The corpus of one domain shard, plus the failures it can be asked to perform."""

    def __init__(self, domain, docs, channels=None, fail_on=(), latency=0.0, hot=True):
        self.domain = domain
        self.docs = list(docs)
        self.channels = dict(channels or {})       # kind -> [ShardDoc], overriding `docs`
        self.fail_on = set(fail_on)
        self.latency = float(latency)
        self.hot = bool(hot)
        self.sql_log = []                          # [(kind, sql, params)]
        self.opened = 0
        self.released = 0

    def rows_for(self, kind):
        docs = self.channels.get(kind, self.docs)
        return [{"publication_id": d.publication_id, "score": float(d.score)} for d in docs]

    def doc(self, pid):
        for d in self.docs:
            if d.publication_id == pid:
                return d
        for docs in self.channels.values():
            for d in docs:
                if d.publication_id == pid:
                    return d
        return None


# ---- the fake connection -----------------------------------------------------------------------
#  Markers that identify which channel is asking. Ordered: the first match wins, so the narrow
#  patterns come before the broad ones (`claim_dense` before `dense`, and `qbe`'s seed-vector read
#  before anything else that mentions `chunks`).
def classify(sql):
    """Which channel issued this SQL. The one place the double has to know the queries."""
    s = " ".join(str(sql or "").split())
    low = s.lower()
    if low.startswith("set "):
        return "set"
    if "from publications where id = any" in low:
        return "families"
    if "select distinct cl.symbol" in low:
        return "subject_cpc"
    if low.startswith("select embedding from chunks"):
        return "qbe_seeds"
    if "phraseto_tsquery" in low:
        return "exact"
    if "from parties" in low:
        return "biblio"
    if "with seed as" in low or "from citations" in low:
        return "citation"
    if "from classifications" in low:
        return "cpc"
    if "c.tsv @@ tq.q" in low:
        if "count(*)" in low:
            return "bm25"
        return "claim_bm25"
    if "embedding <=>" in low:
        if "'claim_own','claim_resolved'" in low.replace(" ", ""):
            return "claim_dense"
        if "'abstract','whole'" in low.replace(" ", ""):
            return "brief_dense"
        return "dense"
    return "unknown"


class FakeCursor:
    def __init__(self, shard):
        self.shard = shard
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        return None

    def execute(self, sql, params=None):
        kind = classify(sql)
        self.shard.sql_log.append((kind, " ".join(str(sql).split()), list(params or [])))
        if kind == "set":
            self.rows = []
            return
        if kind in self.shard.fail_on:
            raise RuntimeError(f"synthetic shard {self.shard.domain} failed on {kind}")
        if self.shard.latency:
            time.sleep(self.shard.latency)
        if kind == "families":
            wanted = list((params or [[]])[0] or [])
            self.rows = [{"id": d.publication_id, "k": d.key()}
                         for d in self.shard.docs if d.publication_id in wanted]
            return
        if kind == "qbe_seeds":
            self.rows = [{"embedding": "[" + ",".join(["0.0"] * 8) + "]"}]
            return
        if kind == "subject_cpc":
            self.rows = []
            return
        if kind == "unknown":
            self.rows = []
            return
        self.rows = self.shard.rows_for(kind)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, shard):
        self.shard = shard
        self.closed = False
        self.autocommit = True

    def cursor(self):
        return FakeCursor(self.shard)

    def close(self):
        self.closed = True


# ---- the hot connection the ROUTER reads its evidence on ---------------------------------------
#  `shard_router.route` is real code and is not stubbed: it reads the corpus-wide domain prior, the
#  subject's own symbols and the domains of the candidate publications. This double answers those
#  four queries so a routing decision can be asserted without touching Postgres.
class RoutingCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        return None

    def execute(self, sql, params=None):
        low = " ".join(str(sql or "").split()).lower()
        self.conn.log.append(low)
        if self.conn.error:
            raise RuntimeError("synthetic routing connection is down")
        if "substr(symbol,1," in low and "group by" in low:
            self.rows = [{"d": d, "n": n} for d, n in self.conn.prior.items()
                         if d != shard_router.UNCLASSIFIED]
        elif "not exists" in low and "from publications" in low:
            self.rows = [{"n": self.conn.prior.get(shard_router.UNCLASSIFIED, 0)}]
        elif low.startswith("select distinct cl.symbol"):
            self.rows = [{"symbol": s} for s in self.conn.symbols]
        elif "select publication_id, symbol from classifications" in low:
            wanted = list((params or [[]])[0] or [])
            self.rows = [{"publication_id": p, "symbol": s}
                         for p in wanted for s in self.conn.classified.get(p, ())]
        else:
            self.rows = []

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class RoutingConnection:
    """`prior` is {domain: row count} including `unclassified`; `symbols` are the subject's own;
    `classified` is {publication_id: (symbol, ...)} for the candidate and citation votes."""

    def __init__(self, prior=None, symbols=(), classified=None, error=False):
        self.prior = dict(prior or {})
        self.symbols = list(symbols)
        self.classified = dict(classified or {})
        self.error = bool(error)
        self.log = []
        self.closed = False

    def cursor(self):
        return RoutingCursor(self)

    def close(self):
        self.closed = True


def routing_connection(**kw):
    return RoutingConnection(**kw)


# ---- the backends ------------------------------------------------------------------------------
class SyntheticShardManager(shard_manager.ShardManagerBackend):
    """A shard fleet that wakes on demand, or refuses to.

    `ensure` honours its timeout, which is the property the whole cold tier rests on: a shard that
    is not hot within `SHARD_WAKE_TIMEOUT` is not waited for, so a cold miss costs the art it would
    have added and nothing else.
    """

    def __init__(self, shards=None, wake_after=0.0, never_wake=(), connect_error=(),
                 ensure_error=False):
        self.shards = dict(shards or {})
        self.wake_after = float(wake_after)
        self.never_wake = set(never_wake)
        self.connect_error = set(connect_error)
        self.ensure_error = bool(ensure_error)
        self._woke_at = {}
        self._lock = threading.Lock()
        self.ensure_calls = []
        self.open_connections = 0
        self.max_open_connections = 0

    # -- lifecycle ---------------------------------------------------------------------------
    def state(self, domain):
        sh = self.shards.get(domain)
        if sh is None:
            return "unknown"
        if sh.hot:
            return "hot"
        if domain in self.never_wake:
            return "waking"
        started = self._woke_at.get(domain)
        if started is None:
            return "cold"
        if time.monotonic() - started >= self.wake_after:
            return "hot"
        return "waking"

    def ensure(self, domains, timeout=shard_manager.WAKE_TIMEOUT):
        self.ensure_calls.append((list(domains or []), timeout))
        if self.ensure_error:
            raise RuntimeError("synthetic shard manager cannot reach the fleet")
        now = time.monotonic()
        for d in domains or ():
            if d in self.shards and not self.shards[d].hot:
                self._woke_at.setdefault(d, now)
        deadline = now + float(timeout or 0.0)
        while True:
            states = {d: self.state(d) for d in (domains or ())}
            if all(s != "waking" for s in states.values()):
                return states
            if time.monotonic() >= deadline:
                return states
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def connection(self, domain):
        if domain in self.connect_error:
            raise RuntimeError(f"synthetic shard {domain} refused a connection")
        sh = self.shards.get(domain)
        if sh is None or self.state(domain) != "hot":
            return None
        with self._lock:
            sh.opened += 1
            self.open_connections += 1
            self.max_open_connections = max(self.max_open_connections, self.open_connections)
        return FakeConnection(sh)

    def release(self, domain, conn):
        sh = self.shards.get(domain)
        #  A CONNECTION THIS FLEET NEVER HANDED OUT IS NOT THIS FLEET'S TO COUNT. `cold.run`
        #  abandons a task that blew the tier budget without cancelling it, on purpose, because
        #  the task owns the connection and abandoning that would leak it on the real shard. The
        #  abandoned task finishes seconds later and runs its own `finally: release(...)`, by
        #  which time the NEXT test has installed a different fleet under the same domain name.
        #  Counting the straggler there made `leaked_connections()` report `{'B66C': -1}` for a
        #  shard that had handed out and taken back exactly one connection, in whichever run the
        #  two happened to overlap. Identity, not the domain name, says whose connection this is.
        if sh is not None and conn is not None and getattr(conn, "shard", sh) is not sh:
            conn.close()
            return
        with self._lock:
            if sh is not None:
                sh.released += 1
            self.open_connections = max(0, self.open_connections - 1)
        if conn is not None:
            conn.close()

    # -- assertions the other workstreams will want -------------------------------------------
    def leaked_connections(self):
        """Every connection handed out must come back. A shard that keeps its connections runs
        out of them long before anything notices, because nothing here fails loudly."""
        return {d: sh.opened - sh.released for d, sh in self.shards.items()
                if sh.opened != sh.released}


class SyntheticRouter(shard_router.ShardRouterBackend):
    """Records what it was asked to wake. `route()` itself is real and is not stubbed here."""

    def __init__(self, error=False):
        self.error = bool(error)
        self.calls = []

    def wake(self, routes):
        self.calls.append([r["domain"] for r in (routes or [])])
        if self.error:
            raise RuntimeError("synthetic router cannot wake anything")
        return {"woken": [r["domain"] for r in (routes or [])], "state": "ok"}


class SyntheticGlobal(global_search.GlobalBackend):
    """The 170M catalog, faked. `hits` is [(publication_id, score)] best-first."""

    name = "global:synthetic"

    def __init__(self, hits=(), families=None, records=None, available=True, error=None,
                 latency=0.0):
        self.hits = list(hits)
        self.families = dict(families or {})
        self._records = dict(records or {})
        self._available = bool(available)
        self.error = error
        self.latency = float(latency)
        self.calls = []

    def available(self):
        return self._available

    def search(self, query, *, subject=None, mode=None, limit=2500, qvec=None):
        self.calls.append({"query": query, "limit": limit, "mode": mode,
                           "has_qvec": qvec is not None})
        if self.error:
            raise self.error if isinstance(self.error, BaseException) else RuntimeError(self.error)
        if self.latency:
            time.sleep(self.latency)
        return list(self.hits)[:limit]

    def family_keys(self, publication_ids):
        return {p: self.families[p] for p in publication_ids if p in self.families}

    def records(self, publication_ids):
        return {p: self._records[p] for p in publication_ids if p in self._records}


@dataclass
class GlobalRecord:
    """The minimum a display record needs: `fusion.best_text` reads title and abstract."""
    pub_number: str = ""
    title: str = ""
    abstract: str = ""
    date: str = ""
    assignee: str = ""
    url: str = ""
    members: list = field(default_factory=list)


# ---- installation ------------------------------------------------------------------------------
@contextlib.contextmanager
def installed(shards=None, manager=None, router=None, global_backend=None, **manager_kw):
    """Register synthetic backends for the duration of a block, then put back what was there.

    Restores on the way out of an exception as well as a return: a test that leaves a synthetic
    shard registered turns every later test in the process into a cold-tier test.
    """
    mgr = manager if manager is not None else SyntheticShardManager(shards or {}, **manager_kw)
    rtr = router if router is not None else SyntheticRouter()
    prev = (shard_manager.backend(), shard_router.backend(), global_search.backend())
    shard_manager.register_backend(mgr)
    shard_router.register_backend(rtr)
    if global_backend is not None:
        global_search.register_backend(global_backend)
    try:
        yield mgr, rtr, global_backend
    finally:
        shard_manager.register_backend(prev[0])
        shard_router.register_backend(prev[1])
        global_search.register_backend(prev[2])


def reset():
    """Unregister everything. `register_backend(None)` restores each seam's inert default."""
    shard_manager.register_backend(None)
    shard_router.register_backend(None)
    global_search.register_backend(None)
