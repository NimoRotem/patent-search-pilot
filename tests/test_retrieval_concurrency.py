"""Fan-out, phase ordering and pre-cap family collapse.

Written before the implementation. Every concurrency assertion uses a real synchronization
primitive, a Barrier or an Event with a timeout, never a bare sleep: a sleep-based assertion
passes on a loaded box for the wrong reason and fails on a fast one for no reason.

No database. The channels are replaced with instrumented stubs and the Retriever is built with
`object.__new__`, so what is under test is the orchestration, not psycopg.
"""
import threading
import time

import pytest

from retrieval import Retriever
from retrieval import orchestrator as orch

BARRIER_TIMEOUT = 15.0

PHASE1 = ("dense", "claim_dense", "brief_dense", "bm25", "claim_bm25", "exact", "cpc",
          "crosslingual", "biblio")
PHASE2 = ("citation", "qbe")


def _double(**attrs):
    """A Retriever with no connection, no family map and no ANN profile."""
    r = object.__new__(Retriever)
    r._fam = {}
    r._wide = False
    r.scan_profile = lambda wide=False: None
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


def _hits(tag, n=3):
    return [(f"{tag}{i}", float(n - i)) for i in range(n)]


@pytest.fixture(autouse=True)
def no_embed(monkeypatch):
    import embed
    monkeypatch.setattr(embed, "embed_query", lambda *a, **k: [0.0] * 768)


# =========================================================================== timing overlap

def test_phase1_channels_genuinely_overlap():
    """A Barrier that every phase 1 channel must reach before any may leave. If they run one
    after another the first one blocks for ever and the barrier times out, so this cannot pass
    sequentially and cannot pass by luck."""
    names = ["dense", "bm25", "exact", "cpc"]
    barrier = threading.Barrier(len(names), timeout=BARRIER_TIMEOUT)

    def stub(tag):
        def run(*a, **k):
            barrier.wait()                  # BrokenBarrierError if the others never arrive
            return _hits(tag)
        return run

    r = _double(
        channel_dense=stub("d"), channel_bm25=stub("b"),
        channel_exact=stub("e"), channel_cpc=stub("c"),
        channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    res = r.search("q", config=names, phrases=["p"], db_concurrency=len(names))
    assert set(res.channel_hits) >= set(names)


def test_a_slow_channel_does_not_serialise_the_others():
    """Wall clock, but bounded by a barrier rather than asserted against a sleep. Three channels
    each block until all three have started; sequential execution cannot reach that state."""
    started = threading.Barrier(3, timeout=BARRIER_TIMEOUT)

    def stub(tag):
        def run(*a, **k):
            started.wait()
            return _hits(tag)
        return run

    r = _double(channel_dense=stub("d"), channel_bm25=stub("b"), channel_cpc=stub("c"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    t0 = time.monotonic()
    r.search("q", config=["dense", "bm25", "cpc"], db_concurrency=3)
    assert time.monotonic() - t0 < BARRIER_TIMEOUT


# =========================================================================== phase ordering

def test_phase2_starts_only_after_every_phase1_channel_finished():
    """Citation and QBE consume the fused strong seeds, so they must not start early. Recorded
    with an Event rather than timing."""
    phase1_done = threading.Event()
    finished = []
    lock = threading.Lock()
    order = []

    def p1(tag):
        def run(*a, **k):
            time.sleep(0.02)
            with lock:
                finished.append(tag)
                order.append(("p1", tag))
            return _hits(tag)
        return run

    def p2(tag):
        def run(seeds, *a, **k):
            with lock:
                order.append(("p2", tag))
            assert phase1_done.is_set(), f"{tag} started before phase 1 finished"
            assert len(finished) == 3, f"{tag} saw only {finished}"
            return _hits(tag)
        return run

    r = _double(channel_dense=p1("d"), channel_bm25=p1("b"), channel_cpc=p1("c"),
                channel_citation_family=p2("cit"), channel_qbe=p2("qbe"))
    orig = orch._run_phase

    def spy(*a, **k):
        out = orig(*a, **k)
        phase1_done.set()
        return out

    orch._run_phase = spy
    try:
        r.search("q", config=["dense", "bm25", "cpc", "citation", "qbe"], db_concurrency=4)
    finally:
        orch._run_phase = orig
    kinds = [k for k, _ in order]
    assert kinds.index("p2") > 2, f"a phase 2 channel ran inside phase 1: {order}"


def test_phase2_receives_the_seeds_fused_from_phase1():
    """Not merely 'runs later': it must get the strong seeds, or the ordering is decorative."""
    seen = {}

    def p2(tag):
        def run(seeds, *a, **k):
            seen[tag] = list(seeds)
            return []
        return run

    r = _double(channel_dense=lambda *a, **k: [("A", 9.0), ("B", 8.0)],
                channel_bm25=lambda *a, **k: [("B", 5.0), ("C", 4.0)],
                channel_citation_family=p2("cit"), channel_qbe=p2("qbe"))
    r.search("q", config=["dense", "bm25", "citation", "qbe"], db_concurrency=2)
    assert seen["cit"], "citation got no seeds"
    assert seen["qbe"] == seen["cit"], "the two phase 2 channels disagree on the seed set"
    assert "A" in seen["cit"] and "B" in seen["cit"]


def test_tasks_are_not_created_and_immediately_awaited():
    """Fake concurrency check: submitting a task then blocking on it before submitting the next
    is sequential execution wearing a thread pool. Proven by the barrier, which such an
    implementation can never satisfy."""
    names = ["dense", "bm25", "cpc"]
    barrier = threading.Barrier(len(names), timeout=3.0)

    def stub(tag):
        def run(*a, **k):
            barrier.wait()
            return _hits(tag)
        return run

    r = _double(channel_dense=stub("d"), channel_bm25=stub("b"), channel_cpc=stub("c"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    r.search("q", config=names, db_concurrency=3)


# =========================================================================== the bound

def test_the_concurrency_bound_is_hard_and_binds():
    """Eight channels, bound of three: at most three may be inside a channel at once, and three
    must actually be reached or the bound is not the thing limiting anything."""
    bound = 3
    names = ["dense", "claim_dense", "brief_dense", "bm25", "claim_bm25", "exact", "cpc",
             "biblio"]
    lock = threading.Lock()
    live = {"now": 0, "max": 0}
    at_bound = threading.Event()

    def stub(tag):
        def run(*a, **k):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
                if live["now"] >= bound:
                    at_bound.set()
            at_bound.wait(timeout=BARRIER_TIMEOUT)
            with lock:
                live["now"] -= 1
            return _hits(tag)
        return run

    r = _double(**{f"channel_{n}": stub(n) for n in
                   ("dense", "claim_dense", "brief_dense", "bm25", "claim_bm25", "cpc")},
                channel_exact=stub("exact"), channel_biblio=stub("biblio"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    r.search("q", config=names, phrases=["p"], assignee_hints=["acme"], db_concurrency=bound)
    assert live["max"] <= bound, f"{live['max']} channels ran at once, bound was {bound}"
    assert live["max"] == bound, f"bound never bound: peak {live['max']}"


def test_the_bound_cannot_exceed_the_connection_budget():
    """A bound larger than the connections available is not a bound, it is a queue at the
    database."""
    assert orch.DB_CONCURRENCY <= orch.DB_CONNECTION_BUDGET
    assert orch.MAX_DB_CONCURRENCY <= orch.DB_CONNECTION_BUDGET
    with pytest.raises(ValueError):
        orch.resolve_concurrency(orch.DB_CONNECTION_BUDGET + 1)


def test_the_bound_is_configurable_and_floors_at_one():
    assert orch.resolve_concurrency(1) == 1
    assert orch.resolve_concurrency(4) == 4
    with pytest.raises(ValueError):
        orch.resolve_concurrency(0)


def test_max_concurrent_db_operations_is_stated():
    """The number has to be written down somewhere a reader can find it."""
    assert isinstance(orch.MAX_CONCURRENT_DB_OPERATIONS, int)
    assert orch.MAX_CONCURRENT_DB_OPERATIONS == orch.DB_CONCURRENCY


# =========================================================================== determinism

def test_repeated_searches_are_identical_under_shuffled_completion_order():
    """Completion order must not reach the output. Channels finish in a different order each
    run; the fused result and the channel ordering must not move."""
    delays = [0.03, 0.0, 0.015, 0.005]

    def make(tag, d):
        def run(*a, **k):
            time.sleep(d)
            return _hits(tag, 4)
        return run

    outs = []
    for shift in range(4):
        ds = delays[shift:] + delays[:shift]
        r = _double(channel_dense=make("d", ds[0]), channel_bm25=make("b", ds[1]),
                    channel_cpc=make("c", ds[2]), channel_exact=make("e", ds[3]),
                    channel_citation_family=lambda *a, **k: [],
                    channel_qbe=lambda *a, **k: [])
        res = r.search("q", config=["dense", "bm25", "cpc", "exact"], phrases=["p"],
                       db_concurrency=4)
        outs.append((list(res.channel_hits), [p for p, _, _ in res.ranked_pubs]))
    assert len(set(map(str, outs))) == 1, f"output depends on completion order: {outs}"


def test_channel_order_follows_the_preset_not_completion():
    def make(tag, d):
        def run(*a, **k):
            time.sleep(d)
            return _hits(tag)
        return run

    r = _double(channel_dense=make("d", 0.03), channel_bm25=make("b", 0.0),
                channel_cpc=make("c", 0.01),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    res = r.search("q", config=["dense", "bm25", "cpc"], db_concurrency=3)
    assert list(res.channel_hits) == ["dense", "bm25", "cpc"]


# =========================================================================== failure isolation

def test_an_optional_channel_failure_is_recorded_and_isolated():
    """One optional channel raising must not cancel the healthy ones, and must not vanish."""
    import failclosed
    before = len(failclosed.used())

    def boom(*a, **k):
        raise RuntimeError("cpc exploded")

    reached = threading.Event()

    def slow(*a, **k):
        time.sleep(0.05)
        reached.set()
        return _hits("d")

    r = _double(channel_dense=slow, channel_cpc=boom, channel_bm25=lambda *a, **k: _hits("b"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    res = r.search("q", config=["dense", "bm25", "cpc"], db_concurrency=3)
    assert reached.is_set(), "a healthy channel was cancelled by a sibling's failure"
    assert res.channel_hits.get("dense"), "healthy channel lost its hits"
    assert res.channel_hits.get("cpc") == [], "failed channel must contribute an empty list"
    recorded = failclosed.used()[before:]
    assert any("cpc" in str(e) for e in recorded), f"failure not recorded: {recorded}"


def test_a_required_channel_failure_still_fails_closed():
    """Today every channel exception propagates. Optional channels become soft; the required
    ones must NOT quietly become soft with them."""
    def boom(*a, **k):
        raise RuntimeError("dense exploded")

    r = _double(channel_dense=boom, channel_bm25=lambda *a, **k: _hits("b"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="dense exploded"):
        r.search("q", config=["dense", "bm25"], db_concurrency=2)


def test_required_and_optional_sets_are_explicit_and_disjoint():
    assert orch.REQUIRED_CHANNELS
    assert orch.OPTIONAL_CHANNELS
    assert not (orch.REQUIRED_CHANNELS & orch.OPTIONAL_CHANNELS)
    assert "dense" in orch.REQUIRED_CHANNELS
    for n in ("cpc", "citation", "qbe", "biblio", "crosslingual", "exact"):
        assert n in orch.OPTIONAL_CHANNELS, n


# =========================================================================== connections

def test_no_two_channels_share_a_connection_at_the_same_time():
    """The real hazard: a psycopg connection cannot carry two statements at once."""
    import retrieval.base as base
    lock = threading.Lock()
    inuse, clash = {}, []
    barrier = threading.Barrier(3, timeout=BARRIER_TIMEOUT)

    class FakeConn:
        def __init__(self, tid):
            self.tid = tid
            self.closed = False

        def close(self):
            self.closed = True

    made = {}

    def fake_worker_conn(wide=False):
        tid = threading.get_ident()
        return made.setdefault(tid, FakeConn(tid))

    def stub(tag):
        def run(*a, **k):
            c = base.worker_conn()
            with lock:
                if id(c) in inuse:
                    clash.append(tag)
                inuse[id(c)] = tag
            barrier.wait()
            with lock:
                inuse.pop(id(c), None)
            return _hits(tag)
        return run

    orig = base.worker_conn
    base.worker_conn = fake_worker_conn
    try:
        r = _double(channel_dense=stub("d"), channel_bm25=stub("b"), channel_cpc=stub("c"),
                    channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
        r.search("q", config=["dense", "bm25", "cpc"], db_concurrency=3)
    finally:
        base.worker_conn = orig
    assert not clash, f"channels shared a connection: {clash}"
    assert len(made) == 3, f"expected one connection per worker thread, got {len(made)}"


def test_the_pool_closes_its_worker_connections():
    """A pool that leaks a connection per search exhausts max_connections in an afternoon."""
    assert hasattr(orch, "shutdown_pool")
    closed = []
    import retrieval.base as base
    orig = base.close_worker_conn
    base.close_worker_conn = lambda: closed.append(1)
    try:
        orch.shutdown_pool()
    finally:
        base.close_worker_conn = orig
    assert closed, "shutdown_pool did not close worker connections"


# =========================================================================== family collapse

def _rows(pairs):
    return [{"publication_id": p, "score": s} for p, s in pairs]


def test_collapse_happens_before_the_cap_not_after():
    """The whole point. Three members of family F plus two singletons, cap of 3. Collapsing
    first yields 3 DISTINCT FAMILIES; capping first yields F plus two of its own siblings."""
    r = _double()
    r._fam = {"a1": "F", "a2": "F", "a3": "F", "b": "B", "c": "C"}
    rows = _rows([("a1", 9), ("a2", 8), ("a3", 7), ("b", 6), ("c", 5)])
    out = r.collapse_rows(rows, cap=3)
    assert [p for p, _ in out] == ["a1", "b", "c"]
    assert len({r._fam[p] for p, _ in out}) == 3


@pytest.mark.parametrize("channel,method,cap", [
    ("dense", "channel_dense", 5),
    ("claim_dense", "channel_claim_dense", 5),
    ("brief_dense", "channel_brief_dense", 5),
    ("bm25", "channel_bm25", 5),
    # `exact` is deliberately narrow and caps at 300 per phrase, not at the channel cap: a phrase
    # matching thousands of documents is not a phrase worth searching.
    ("exact", "channel_exact", 300),
    ("cpc", "channel_cpc", 5),
    ("citation", "channel_citation_family", 5),
    ("qbe", "channel_qbe", 5),
])
def test_each_channel_caps_families_not_publications(channel, method, cap):
    """Every channel's cap must count distinct families. Driven through the real channel with a
    fake cursor, so a channel that forgets to collapse fails here rather than in production."""
    fam = {i: f"F{i // 4}" for i in range(40)}          # four members per family
    r = _double()
    r._fam = fam
    r._wide = False
    r._cap = lambda: 5
    r._fetch = lambda: 100

    rows = [{"publication_id": i, "score": float(40 - i)} for i in range(40)]

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            self.sql = a[0] if a else ""

        def fetchall(self):
            #  qbe first reads seed VECTORS ("SELECT embedding FROM chunks ..."); every other
            #  query, including the dense ones whose SQL merely mentions the embedding column,
            #  returns ranking rows.
            if getattr(self, "sql", "").strip().upper().startswith("SELECT EMBEDDING"):
                return [{"embedding": "[0]"}]
            return list(rows)

    class Conn:
        def cursor(self):
            return Cur()

    object.__setattr__(r, "_conn", Conn())
    r.__dict__["_conn_tid"] = threading.get_ident()
    r.channel_dense_raw = lambda *a, **k: [(i, float(40 - i)) for i in range(40)]

    fn = getattr(r, method)
    if method == "channel_exact":
        out = fn(["a phrase"])
    elif method == "channel_cpc":
        out = fn(None)
    elif method in ("channel_citation_family", "channel_qbe"):
        out = fn([1, 2, 3])
    elif method in ("channel_dense", "channel_claim_dense", "channel_brief_dense"):
        out = fn([0.0] * 768)
    else:
        out = fn("query text")

    pids = [p for p, _ in out]
    fams = [fam.get(p, p) for p in pids]
    assert len(fams) == len(set(fams)), f"{channel} returned two members of one family: {pids}"
    assert len(pids) <= cap, f"{channel} exceeded its cap of {cap} families: {len(pids)}"
    #  With 40 rows over 10 families the collapse must actually bind when the cap is below 10,
    #  otherwise the test would pass on a channel that simply returned nothing.
    assert len(pids) == min(cap, 10), f"{channel} returned {len(pids)} families, expected {min(cap, 10)}"


def test_collapse_keeps_the_best_scoring_member():
    """Which member survives is a retrieval decision here and a legal one later; it must be the
    member that carried the evidence, exactly as dedup_family would have chosen."""
    r = _double()
    r._fam = {"lo": "F", "hi": "F"}
    out = r.collapse_rows(_rows([("hi", 9.0), ("lo", 1.0)]), cap=5)
    assert [p for p, _ in out] == ["hi"]


# =========================================================================== review round 2

class _RecordingCur:
    """A cursor that remembers the SQL and params it was given."""

    def __init__(self, rows_for, log):
        self._rows_for = rows_for
        self._log = log
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = list(params or [])
        self._log.append((sql, self.params))

    def fetchall(self):
        return self._rows_for(self.sql, self.params)


def _conn_for(rows_for, log):
    class Conn:
        def cursor(self):
            return _RecordingCur(rows_for, log)
    return Conn()


def _attach(r, rows_for, log):
    object.__setattr__(r, "_conn", _conn_for(rows_for, log))
    r.__dict__["_conn_tid"] = threading.get_ident()


# --------------------------------------------------------------------- 1. exact over-fetch

def test_exact_overfetches_publications_before_capping_families():
    """The SQL still said LIMIT 300 literally, so the database truncated to 300 PUBLICATIONS and
    the family collapse could only ever return fewer. The limit must be a parameter, and it must
    be the over-fetched one."""
    from retrieval.base import FAMILY_OVERFETCH
    log = []
    r = _double()
    r._fam = {}
    _attach(r, lambda sql, params: [], log)
    r.channel_exact(["a phrase"])
    assert log, "no query issued"
    #  log[0] is the selectivity probe the channel runs first (see `phrase_is_affordable`); the
    #  ranking query is the one this test is about.
    ranking = [(s, p) for s, p in log if "ts_rank_cd" in s]
    assert ranking, f"the ranking query was never issued: {[s for s, _ in log]}"
    sql, params = ranking[0]
    assert "LIMIT 300" not in sql, "the literal cap is still in the SQL"
    assert "LIMIT %s" in sql, f"limit is not a parameter: {sql}"
    assert params[-1] == 300 * FAMILY_OVERFETCH, (
        f"expected an over-fetch of {300 * FAMILY_OVERFETCH}, got {params[-1]}")


def test_exact_pools_phrases_without_imposing_a_global_300_family_cap():
    """Each phrase is capped at 300 families. Pooling several phrases must remove duplicate
    families, not silently turn the channel into a global 300-family cap: two phrases that each
    fill their own budget legitimately yield more than 300 families between them."""
    fam = {}
    per_phrase = {}
    for phrase_no in range(2):
        ids = [f"p{phrase_no}_{i}" for i in range(300)]
        for i in ids:
            fam[i] = f"F{i}"
        per_phrase[phrase_no] = ids
    #  one family deliberately shared by both phrases
    fam["p1_0"] = fam["p0_0"]

    log = []
    r = _double()
    r._fam = fam
    calls = {"n": 0}

    def rows_for(sql, params):
        if "count(*)" in sql:          # the selectivity probe: both phrases are affordable
            return [{"n": 1}]
        ids = per_phrase[calls["n"]]
        calls["n"] += 1
        return [{"publication_id": p, "score": float(300 - i)} for i, p in enumerate(ids)]

    _attach(r, rows_for, log)
    out = r.channel_exact(["one", "two"])
    pids = [p for p, _ in out]
    fams = [fam[p] for p in pids]
    assert len(fams) == len(set(fams)), "a duplicate family survived the pool"
    assert len(pids) == 599, f"expected 600 minus the one shared family, got {len(pids)}"


# --------------------------------------------------------------------- 2. canonicalise

def test_two_channels_finding_different_members_of_one_family_combine_their_votes():
    """Per-channel collapse leaves a family able to arrive as pub A from dense and pub B from
    bm25. Without canonicalisation RRF splits the family's votes between two ids and neither
    wins."""
    r = _double(channel_dense=lambda *a, **k: [("A", 9.0), ("x1", 8.0), ("x2", 7.0)],
                channel_bm25=lambda *a, **k: [("B", 5.0), ("y1", 4.0), ("y2", 3.0)],
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    r._fam = {"A": "F", "B": "F", "x1": "X1", "x2": "X2", "y1": "Y1", "y2": "Y2"}
    res = r.search("q", config=["dense", "bm25"], db_concurrency=2)

    reps = {p for p, _s, _pr in res.ranked_pubs if r._fam.get(p) == "F"}
    assert len(reps) == 1, f"family F reached fusion as {reps}, it must be one representative"
    rep = reps.pop()
    prov = dict((p, pr) for p, _s, pr in res.ranked_pubs)[rep]
    channels = set(prov) if isinstance(prov, dict) else set()
    assert {"dense", "bm25"} <= channels, (
        f"the family's provenance lost a channel: {channels}")

    top = res.ranked_pubs[0][0]
    assert top == rep, f"the combined family should lead, got {top}"


def test_phase2_seeds_use_the_canonical_representative():
    """Seed selection happens on the fused phase 1 list, so it must see the canonical id too, or
    citation and qbe expand around the wrong member."""
    seen = {}

    def p2(tag):
        def run(seeds, *a, **k):
            seen[tag] = list(seeds)
            return []
        return run

    r = _double(channel_dense=lambda *a, **k: [("A", 9.0)],
                channel_bm25=lambda *a, **k: [("B", 5.0)],
                channel_citation_family=p2("cit"), channel_qbe=p2("qbe"))
    r._fam = {"A": "F", "B": "F"}
    r.search("q", config=["dense", "bm25", "citation", "qbe"], db_concurrency=2)
    assert len(seen["cit"]) == 1, f"family F seeded twice: {seen['cit']}"


def test_canonicalisation_is_deterministic_across_runs():
    outs = []
    for _ in range(5):
        r = _double(channel_dense=lambda *a, **k: [("A", 9.0)],
                    channel_bm25=lambda *a, **k: [("B", 5.0)],
                    channel_citation_family=lambda *a, **k: [],
                    channel_qbe=lambda *a, **k: [])
        r._fam = {"A": "F", "B": "F"}
        res = r.search("q", config=["dense", "bm25"], db_concurrency=2)
        outs.append([p for p, _, _ in res.ranked_pubs])
    assert len(set(map(str, outs))) == 1, f"representative is not stable: {outs}"


# --------------------------------------------------------------------- 3. draining

def test_no_task_is_still_running_when_search_raises():
    """A required channel failing must not leave siblings executing against the database after
    the caller has already seen the exception."""
    running = threading.Event()
    finished = threading.Event()
    started = threading.Event()

    def slow(*a, **k):
        started.set()
        running.set()
        time.sleep(0.25)
        running.clear()
        finished.set()
        return _hits("s")

    def boom(*a, **k):
        started.wait(timeout=BARRIER_TIMEOUT)
        raise RuntimeError("dense exploded")

    r = _double(channel_dense=boom, channel_bm25=slow,
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="dense exploded"):
        r.search("q", config=["dense", "bm25"], db_concurrency=2)
    assert finished.is_set(), "a sibling was still running when the exception propagated"
    assert not running.is_set()


def test_every_task_is_drained_when_search_returns():
    done = [threading.Event() for _ in range(3)]

    def stub(i):
        def run(*a, **k):
            time.sleep(0.02 * (3 - i))
            done[i].set()
            return _hits(f"c{i}")
        return run

    r = _double(channel_dense=stub(0), channel_bm25=stub(1), channel_cpc=stub(2),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    r.search("q", config=["dense", "bm25", "cpc"], db_concurrency=3)
    assert all(e.is_set() for e in done)


def test_an_unclassified_channel_failure_propagates():
    """Only names explicitly in OPTIONAL_CHANNELS may soft-degrade. A channel nobody classified
    must not quietly become optional by omission."""
    assert "mystery" not in orch.OPTIONAL_CHANNELS
    assert "mystery" not in orch.REQUIRED_CHANNELS
    with pytest.raises(RuntimeError, match="unclassified"):
        orch._run_phase([("mystery", lambda: (_ for _ in ()).throw(RuntimeError("unclassified")))],
                        2)


def test_the_propagated_failure_is_deterministic_when_several_fail():
    """Two required channels failing must always surface the same one, in task order, or the
    error a user sees depends on thread scheduling."""
    def boom(msg):
        def run():
            raise RuntimeError(msg)
        return run

    seen = set()
    for _ in range(5):
        with pytest.raises(RuntimeError) as e:
            orch._run_phase([("dense", boom("first")), ("claim_dense", boom("second"))], 2)
        seen.add(str(e.value))
    assert seen == {"first"}, f"the surfaced failure varies: {seen}"


def test_a_benchmark_optional_failure_is_raised_only_after_siblings_finish():
    """In benchmark mode failclosed RAISES on an optional channel too. That raise must happen
    after the phase has drained, not while a sibling is mid-query."""
    import failclosed
    sibling_done = threading.Event()

    def slow():
        time.sleep(0.2)
        sibling_done.set()
        return _hits("s")

    def boom():
        raise RuntimeError("cpc exploded")

    with failclosed.force(True):
        with pytest.raises(Exception):
            orch._run_phase([("cpc", boom), ("bm25", slow)], 2)
        assert sibling_done.is_set(), "the optional failure raised before the sibling finished"


# --------------------------------------------------------------------- 4. the bound boundary

def test_resolve_concurrency_rejects_above_the_pool_size():
    """The pool has MAX_DB_CONCURRENCY workers. A bound above that is not a bound, it is a queue
    inside the executor pretending to be parallelism."""
    assert orch.resolve_concurrency(orch.MAX_DB_CONCURRENCY) == orch.MAX_DB_CONCURRENCY
    with pytest.raises(ValueError):
        orch.resolve_concurrency(orch.MAX_DB_CONCURRENCY + 1)


def test_the_configured_default_is_within_the_pool_size():
    assert 1 <= orch.DB_CONCURRENCY <= orch.MAX_DB_CONCURRENCY
    assert orch.MAX_DB_CONCURRENCY <= orch.DB_CONNECTION_BUDGET


# =========================================================================== review round 3
# The scan profile race: one process-global Retriever, two overlapping searches.

def test_overlapping_wide_and_narrow_searches_keep_their_own_profile(monkeypatch):
    """webapp.retriever() hands every request the SAME Retriever. `search` set `self._wide` and
    each fan-out task read it again at EXECUTION time, so a wide seed pass and a narrow element
    pass overlapping on that object flipped each other's HNSW profile: the seed pass silently ran
    narrow, or the element pass ran wide and took the seed budget with it.

    Both searches are held at a barrier inside their channels, so they are provably in flight at
    the same moment; then each is asked what profile its own tasks observed.
    """
    import retrieval.base as base
    import embed

    both_in_flight = threading.Barrier(2, timeout=BARRIER_TIMEOUT)
    seen = {}
    conn_widths = {}
    lock = threading.Lock()

    class FakeConn:
        closed = False

        def close(self):
            pass

    def fake_worker_conn(wide=False):
        with lock:
            conn_widths.setdefault(threading.get_ident(), []).append(bool(wide))
        return FakeConn()

    monkeypatch.setattr(
        embed, "embed_query",
        lambda query, *args, **kwargs: [1.0] if query == "wide-query" else [0.0],
    )

    def stub(qvec, *args, **kwargs):
        request_name = "wide" if qvec[0] == 1.0 else "narrow"
        both_in_flight.wait()          # both searches are now inside a channel
        _ = r.conn                     # resolves through worker_conn with the live profile
        with lock:
            seen[request_name] = (base.active_wide(), r._fetch(), r._cap())
        return _hits(request_name)

    r = _double(channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    r.channel_dense = stub

    orig = base.worker_conn
    base.worker_conn = fake_worker_conn
    results = {}

    def go(key, wide):
        try:
            r.search(f"{key}-query", config=["dense"], wide=wide, db_concurrency=2)
            results[key] = "ok"
        except BaseException as exc:                                   # noqa: BLE001
            results[key] = exc

    try:
        ta = threading.Thread(target=go, args=("wide", True))
        tb = threading.Thread(target=go, args=("narrow", False))
        ta.start(); tb.start()
        ta.join(timeout=BARRIER_TIMEOUT + 5); tb.join(timeout=BARRIER_TIMEOUT + 5)
    finally:
        base.worker_conn = orig

    assert results.get("wide") == "ok", results.get("wide")
    assert results.get("narrow") == "ok", results.get("narrow")

    narrow_obs, wide_obs = seen["narrow"], seen["wide"]
    assert narrow_obs[0] is False and wide_obs[0] is True, seen
    assert narrow_obs[1] == base.CHUNK_FETCH and narrow_obs[2] == base.PUB_CAP, narrow_obs
    assert wide_obs[1] == base.SEED_CHUNK_FETCH and wide_obs[2] == base.SEED_PUB_CAP, wide_obs

    flat = [w for widths_ in conn_widths.values() for w in widths_]
    assert True in flat and False in flat, (
        f"both profiles must reach worker_conn, got {conn_widths}")


def test_the_profile_context_is_restored_after_a_successful_search():
    import retrieval.base as base
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    assert base.active_wide() is False
    r.search("q", config=["dense"], wide=True, db_concurrency=2)
    assert base.active_wide() is False, "a wide search leaked its profile to the caller"


def test_the_profile_context_is_restored_after_a_failed_search():
    """A later search must not inherit a stale wide setting because an earlier one raised."""
    import retrieval.base as base

    def boom(*a, **k):
        raise RuntimeError("dense exploded")

    r = _double(channel_dense=boom,
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    with pytest.raises(RuntimeError):
        r.search("q", config=["dense"], wide=True, db_concurrency=2)
    assert base.active_wide() is False, "the profile survived an exception"


def test_a_pool_thread_without_a_context_falls_back_to_the_instance_profile():
    """Sequential callers and the owning thread must behave exactly as before."""
    import retrieval.base as base
    r = _double()
    r._wide = True
    assert base.active_wide() is False           # no context on this thread
    assert r._fetch() == base.SEED_CHUNK_FETCH   # so the instance value is used
    r._wide = False
    assert r._fetch() == base.CHUNK_FETCH


# --------------------------------------------------------------------- preset validation

def test_an_unknown_channel_name_fails_before_any_task_is_submitted():
    """It used to disappear silently: the builder simply had no branch for it, so a typo in an
    explicit preset removed a channel and nothing said so."""
    ran = threading.Event()

    r = _double(channel_dense=lambda *a, **k: ran.set() or _hits("d"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    with pytest.raises(ValueError, match="dnese"):
        r.search("q", config=["dense", "dnese"], db_concurrency=2)
    assert not ran.is_set(), "work was submitted before the preset was validated"


def test_a_duplicate_explicit_channel_name_does_not_double_the_database_work():
    calls = {"n": 0}

    def once(*a, **k):
        calls["n"] += 1
        return _hits("d")

    r = _double(channel_dense=once,
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    res = r.search("q", config=["dense", "dense", "dense"], db_concurrency=2)
    assert calls["n"] == 1, f"dense ran {calls['n']} times"
    assert list(res.channel_hits) == ["dense"]


def test_every_builtin_preset_only_names_known_channels():
    for name, preset in orch.PRESETS.items():
        unknown = [c for c in preset if c not in orch.KNOWN_CHANNELS]
        assert not unknown, f"preset {name} names unknown channels {unknown}"


def test_known_channels_covers_required_and_optional():
    assert orch.REQUIRED_CHANNELS <= orch.KNOWN_CHANNELS
    assert orch.OPTIONAL_CHANNELS <= orch.KNOWN_CHANNELS


def test_an_unknown_preset_NAME_still_falls_back_as_before():
    """Behaviour preservation: a bad preset name is not the same thing as a bad channel name."""
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_bm25=lambda *a, **k: _hits("b"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    res = r.search("q", config="no_such_preset", db_concurrency=2)
    assert set(res.channel_hits) == {"bm25", "dense"}


def test_fork_and_shutdown_pool_are_still_present():
    assert callable(getattr(Retriever, "fork"))
    assert callable(orch.shutdown_pool)
    assert callable(getattr(Retriever, "scan_profile"))


def test_no_profile_residue_is_left_on_a_pool_thread():
    """The assertion that actually matters for requirement 3.

    Checking the CALLER thread proves little: it never had a context. The pool threads are the
    ones reused for the life of the process, so a leak there is what a later search would inherit.
    A probe is submitted straight to the pool, bypassing `_run_phase`, and asked what it sees.
    """
    import retrieval.base as base

    def probe():
        return base.active_wide()

    def residue():
        all_workers = threading.Barrier(orch.MAX_DB_CONCURRENCY, timeout=BARRIER_TIMEOUT)

        def probe_every_worker():
            all_workers.wait()
            return probe()

        futs = [orch._pool().submit(probe_every_worker)
                for _ in range(orch.MAX_DB_CONCURRENCY)]
        return [f.result(timeout=BARRIER_TIMEOUT) for f in futs]

    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    r.search("q", config=["dense"], wide=True, db_concurrency=2)
    assert not any(residue()), "a wide search left its profile on a pool thread"

    def boom(*a, **k):
        raise RuntimeError("dense exploded")

    r2 = _double(channel_dense=boom,
                 channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    with pytest.raises(RuntimeError):
        r2.search("q", config=["dense"], wide=True, db_concurrency=2)
    assert not any(residue()), "a wide search that raised left its profile on a pool thread"


def test_a_narrow_search_after_a_wide_one_is_not_widened():
    """End to end version of the same thing, through the real channel path."""
    import retrieval.base as base
    seen = []
    r = None

    def record(*a, **k):
        seen.append((base.active_wide(), r._fetch()))
        return _hits("d")

    r = _double(channel_dense=record,
                channel_citation_family=lambda *a, **k: [], channel_qbe=lambda *a, **k: [])
    r.search("q", config=["dense"], wide=True, db_concurrency=2)
    r.search("q", config=["dense"], wide=False, db_concurrency=2)
    assert seen[0] == (True, base.SEED_CHUNK_FETCH), seen
    assert seen[1] == (False, base.CHUNK_FETCH), seen
