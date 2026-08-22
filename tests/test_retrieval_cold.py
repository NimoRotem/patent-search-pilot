"""The cold tier: parallel with the hot one, degrading rather than raising, and free when absent.

Every assertion here injects the failure it is about. A shard that wakes, connects and answers
proves nothing: the tier exists to be correct when a shard does none of those things.

No database. The hot channels are instrumented stubs, the shards are `retrieval.testing` doubles
and the routing evidence comes from `testing.routing_connection`, so what is under test is the
orchestration.
"""
import threading
import time

import pytest

import retrieval
from retrieval import channels, cold, fusion, shard_manager, shard_router, testing
from retrieval import orchestrator as orch
from retrieval import Retriever

BARRIER_TIMEOUT = 15.0
PRIOR = {"B66C": 1000, "B65G": 500, "B25J": 250, shard_router.UNCLASSIFIED: 200}


def _double(**attrs):
    r = object.__new__(Retriever)
    r._fam = {}
    r._wide = False
    r.scan_profile = lambda wide=False: None
    r.channel_citation_family = lambda *a, **k: []
    r.channel_qbe = lambda *a, **k: []
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


def _hits(tag, n=3):
    return [(f"{tag}{i}", float(n - i)) for i in range(n)]


@pytest.fixture(autouse=True)
def clean_seams(monkeypatch):
    """No synthetic backend may survive a test, and the corpus prior must not leak between them."""
    shard_router.reset_prior()
    testing.reset()
    monkeypatch.setattr(cold, "route_connection",
                        lambda: testing.routing_connection(prior=PRIOR))
    yield
    testing.reset()
    shard_router.reset_prior()


def _search(r, shards, config=("dense", "bm25", "cold"), **kw):
    with testing.installed(shards=shards, **kw.pop("manager_kw", {})) as (mgr, rtr, _g):
        res = r.search("q", config=list(config), db_concurrency=2, **kw)
    return res, mgr, rtr


# =========================================================================== free when absent

def test_no_shard_backend_issues_no_query_and_creates_no_channel(monkeypatch):
    """"Identical to today when the backends are absent" has to include COST. `route()` reads the
    database and the corpus-wide prior is a 7.9 s GROUP BY; paying it on every search to discover
    that no shard exists would be a regression nobody would attribute to this change."""
    opened = []
    monkeypatch.setattr(cold, "route_connection",
                        lambda: opened.append(1) or testing.routing_connection(prior=PRIOR))
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_bm25=lambda *a, **k: _hits("b"))
    res = r.search("q", config=["dense", "bm25", "cold", "global"], db_concurrency=2)
    assert not opened, "the cold tier routed with no shard backend registered"
    assert list(res.channel_hits) == ["dense", "bm25"], res.channel_hits
    assert not any(cold.is_cold(k) for k in res.channel_hits)
    assert res.tiers == {}


def test_a_preset_naming_cold_and_global_is_still_a_valid_preset():
    for name, preset in orch.PRESETS.items():
        unknown = [c for c in preset if c not in orch.KNOWN_CHANNELS]
        assert not unknown, f"preset {name} names unknown channels {unknown}"
    assert "cold" in orch.PRESETS["agentic"]
    assert "global" in orch.PRESETS["agentic"]


# =========================================================================== parallel, not after

def test_the_cold_tier_runs_in_parallel_with_the_hot_tier():
    """A Barrier both tiers must reach before either may leave. If the cold tier ran after the hot
    one the barrier times out, so this cannot pass sequentially and cannot pass by luck."""
    both = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def hot(*a, **k):
        both.wait()
        return _hits("d")

    class BarrierShard(testing.SyntheticShard):
        def rows_for(self, kind):
            both.wait()
            return super().rows_for(kind)

    sh = BarrierShard("B66C", [testing.ShardDoc(101, "FC1", 9.0)])
    r = _double(channel_dense=hot, channel_bm25=lambda *a, **k: _hits("b"))
    res, _mgr, _rtr = _search(r, {"B66C": sh}, config=("dense", "cold"))
    assert res.channel_hits.get("cold:dense"), "the cold tier contributed nothing"


def test_two_shards_are_queried_concurrently():
    """Shards are different hosts. Querying them one after another makes the tier as slow as the
    sum of the fleet, which is what the per-domain fan-out exists to avoid."""
    both = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    class BarrierShard(testing.SyntheticShard):
        def rows_for(self, kind):
            both.wait()
            return super().rows_for(kind)

    shards = {"B66C": BarrierShard("B66C", [testing.ShardDoc(101, "FC1", 9.0)]),
              "B65G": BarrierShard("B65G", [testing.ShardDoc(201, "FC2", 8.0)])}
    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    res, _mgr, _rtr = _search(r, shards, config=("dense", "cold"))
    assert len(res.channel_hits.get("cold:dense") or []) == 2


def test_a_slow_shard_does_not_delay_the_hot_answer():
    """Wall clock, bounded by an Event rather than asserted against a sleep: the hot channels must
    have finished while the cold tier was still inside its shard."""
    hot_done = threading.Event()
    release = threading.Event()

    class SlowShard(testing.SyntheticShard):
        def rows_for(self, kind):
            hot_done.wait(timeout=BARRIER_TIMEOUT)
            release.set()
            return super().rows_for(kind)

    def hot(*a, **k):
        hot_done.set()
        return _hits("d")

    r = _double(channel_dense=hot)
    res, _mgr, _rtr = _search(r, {"B66C": SlowShard("B66C", [testing.ShardDoc(1, "F", 1.0)])},
                              config=("dense", "cold"))
    assert release.is_set(), "the shard was queried before the hot tier had even started"
    assert res.channel_hits.get("dense")


# =========================================================================== degrade, never raise

def test_a_shard_that_never_wakes_is_not_waited_for(monkeypatch):
    """SHARD_WAKE_TIMEOUT is a budget, not a hope. A shard still waking when it expires is not
    waited for: a cold miss costs the art it would have added and nothing else."""
    monkeypatch.setattr(shard_manager, "WAKE_TIMEOUT", 0.2)
    sh = testing.shard("B66C", docs=[(101, "FC1")], hot=False)
    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    t0 = time.monotonic()
    res, mgr, _rtr = _search(r, {"B66C": sh}, config=("dense", "cold"),
                             manager_kw={"never_wake": {"B66C"}})
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"a shard that will not wake held the search for {elapsed:.1f}s"
    assert res.channel_hits.get("dense"), "the hot answer was lost with the cold one"
    assert not any(cold.is_cold(k) for k in res.channel_hits)
    assert res.tiers["cold"]["hot"] == []
    assert sh.opened == 0, "a shard that never woke was connected to anyway"


def test_a_shard_that_refuses_a_connection_degrades():
    sh = testing.shard("B66C", docs=[(101, "FC1")])
    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    res, _mgr, _rtr = _search(r, {"B66C": sh}, config=("dense", "cold"),
                              manager_kw={"connect_error": {"B66C"}})
    assert res.channel_hits.get("dense")
    assert not any(cold.is_cold(k) for k in res.channel_hits)
    assert "B66C" in res.tiers["cold"]["errors"]


def test_a_channel_that_raises_on_a_shard_loses_only_that_channel():
    """One channel failing on one shard must not take the shard's other channels with it, and must
    not take the search with it either."""
    sh = testing.shard("B66C", docs=[(101, "FC1"), (102, "FC2")], fail_on={"dense"})
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_bm25=lambda *a, **k: _hits("b"))
    res, _mgr, _rtr = _search(r, {"B66C": sh}, config=("dense", "bm25", "cold"))
    assert "cold:dense" not in res.channel_hits
    assert res.channel_hits.get("cold:bm25"), "a sibling channel died with the failing one"
    assert any("dense" in k for k in res.tiers["cold"]["errors"])


def test_a_shard_manager_that_cannot_reach_the_fleet_degrades():
    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    res, _mgr, _rtr = _search(r, {"B66C": testing.shard("B66C", docs=[(1, "F")])},
                              config=("dense", "cold"), manager_kw={"ensure_error": True})
    assert res.channel_hits.get("dense")
    assert not any(cold.is_cold(k) for k in res.channel_hits)


def test_a_router_that_cannot_wake_anything_degrades():
    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    with testing.installed(shards={"B66C": testing.shard("B66C", docs=[(1, "F")])},
                           router=testing.SyntheticRouter(error=True)):
        res = r.search("q", config=["dense", "cold"], db_concurrency=2)
    assert res.channel_hits.get("dense")


def test_a_routing_query_that_fails_still_emits_the_unclassified_route(monkeypatch):
    """1,024,320 publications carry no classification at all and skew old and foreign, which is
    exactly the population the gold citation lists are drawn from. Losing that route because the
    router could not read the corpus would make 20.6% of it unreachable, silently."""
    monkeypatch.setattr(cold, "route_connection",
                        lambda: testing.routing_connection(prior=PRIOR, error=True))
    sh = testing.shard(shard_router.UNCLASSIFIED, docs=[(9001, "FU1")])
    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    res, _mgr, _rtr = _search(r, {shard_router.UNCLASSIFIED: sh}, config=("dense", "cold"))
    assert res.tiers["cold"]["routes"] == [shard_router.UNCLASSIFIED]
    assert res.channel_hits.get("cold:dense"), "the unclassified shard was never queried"


def test_the_tier_budget_stops_a_hung_shard_from_hanging_the_search(monkeypatch):
    """The wake has a budget; a shard that hangs AFTER waking needs one too, or a single stuck
    host makes every search as slow as it is."""
    monkeypatch.setattr(cold, "TIER_BUDGET", 0.3)
    stuck = threading.Event()

    class HungShard(testing.SyntheticShard):
        def rows_for(self, kind):
            stuck.wait(timeout=3.0)
            return super().rows_for(kind)

    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    t0 = time.monotonic()
    res, _mgr, _rtr = _search(r, {"B66C": HungShard("B66C", [testing.ShardDoc(1, "F", 1.0)])},
                              config=("dense", "cold"))
    elapsed = time.monotonic() - t0
    stuck.set()
    assert elapsed < 3.0, f"the search waited {elapsed:.1f}s on a hung shard"
    assert res.channel_hits.get("dense")
    assert "B66C" in res.tiers["cold"]["errors"]


def test_every_shard_connection_is_handed_back():
    """Nothing here fails loudly when a connection is kept, so a leak is invisible until the shard
    runs out. Checked on the success path and on the failing one."""
    ok = testing.shard("B66C", docs=[(101, "FC1")])
    bad = testing.shard("B65G", docs=[(201, "FC2")], fail_on={"dense", "bm25"})
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_bm25=lambda *a, **k: _hits("b"))
    res, mgr, _rtr = _search(r, {"B66C": ok, "B65G": bad}, config=("dense", "bm25", "cold"))
    assert mgr.leaked_connections() == {}, mgr.leaked_connections()
    assert mgr.open_connections == 0
    assert res is not None


# =========================================================================== one implementation

def test_a_cold_channel_issues_exactly_the_same_sql_as_its_hot_counterpart():
    """The seam hands back a CONNECTION and not a `search()` so that there is one implementation of
    each channel. This is the assertion that keeps it that way: the SQL the shard was given must be
    the SQL the hot channel issues, character for character."""
    log = []

    class RecordingCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            log.append(" ".join(str(sql).split()))

        def fetchall(self):
            return []

    class RecordingConn:
        def cursor(self):
            return RecordingCur()

    hot = _double()
    object.__setattr__(hot, "_conn", RecordingConn())
    hot.__dict__["_conn_tid"] = threading.get_ident()
    hot._cap = lambda: 5
    hot._fetch = lambda: 10
    hot.channel_dense([0.0] * 8)
    hot.channel_bm25("query text")
    hot_sql = list(log)

    sh = testing.shard("B66C", docs=[(101, "FC1")])
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_bm25=lambda *a, **k: _hits("b"))
    r._cap = lambda: 5
    r._fetch = lambda: 10
    _search(r, {"B66C": sh}, config=("dense", "bm25", "cold"))
    cold_sql = [sql for kind, sql, _p in sh.sql_log if kind in ("dense", "bm25")]
    assert cold_sql == hot_sql, f"cold SQL has drifted from hot SQL\n{cold_sql}\n{hot_sql}"


def test_a_cold_channel_carries_the_weight_of_its_hot_counterpart():
    """The same query against a different host is the same evidence. Two weight tables drift, and
    the day one is retuned and the other is not, the ranking depends on which VM is awake."""
    for kind in ("dense", "claim_dense", "brief_dense", "bm25", "cpc", "citation", "qbe"):
        assert fusion.channel_weight(cold.cold_name(kind)) == fusion.CHANNEL_WEIGHTS[kind], kind
    assert fusion.channel_weight("cold:nonsense") == 0.5


# =========================================================================== family identity

def _family_shards():
    #  Publication 900 lives on the shard and belongs to family F-SHARED, which the HOT corpus
    #  already knows under publication 1. Same disclosure, two ids, two tiers.
    return {"B66C": testing.shard("B66C", docs=[(900, "F-SHARED"), (901, "F-COLD")])}


def _family_retriever():
    r = _double(channel_dense=lambda *a, **k: [(1, 9.0)],
                channel_bm25=lambda *a, **k: [(1, 5.0)])
    r._fam = {1: "F-SHARED"}
    return r


def test_a_cold_hit_of_a_hot_family_collapses_to_one_row():
    r = _family_retriever()
    res, _mgr, _rtr = _search(r, _family_shards(), config=("dense", "bm25", "cold"))
    fams = [fk for fk, _pid, _s, _pr in res.family_ranked]
    assert len(fams) == len(set(fams)), f"one disclosure reached the answer twice: {fams}"
    assert "F-SHARED" in fams and "F-COLD" in fams
    assert r._fam.get(900) == "F-SHARED", "the shard's family key was never learned"


def test_without_the_shard_family_lookup_the_same_disclosure_appears_twice(monkeypatch):
    """The defect injection for the test above: remove the hydration and the collapse fails. A
    guard that is only ever green proves nothing about what it is guarding."""
    monkeypatch.setattr(cold, "_hydrator", lambda r: (lambda pids: None))
    r = _family_retriever()
    res, _mgr, _rtr = _search(r, _family_shards(), config=("dense", "bm25", "cold"))
    fams = [fk for fk, _pid, _s, _pr in res.family_ranked]
    assert "F-SHARED" in fams and "900" in fams, (
        f"expected the un-hydrated cold hit to survive as its own family: {fams}")


def test_hydration_never_relabels_a_publication_the_hot_corpus_already_knows():
    """Gaps only. A shard that renumbered its publications would otherwise silently move a hot
    document into a cold family, which is a wrong answer rather than a missing one."""
    r = _double(channel_dense=lambda *a, **k: [(1, 9.0)])
    r._fam = {1: "HOT-FAMILY"}
    shards = {"B66C": testing.shard("B66C", docs=[(1, "COLD-FAMILY")])}
    _search(r, shards, config=("dense", "cold"))
    assert r._fam[1] == "HOT-FAMILY"


def test_cold_hits_are_capped_at_families_across_shards():
    """Each shard collapses its own hits; two shards can still hold two members of one family."""
    shards = {"B66C": testing.shard("B66C", docs=[(101, "F1", 9.0), (102, "F2", 8.0)]),
              "B65G": testing.shard("B65G", docs=[(201, "F1", 7.0), (202, "F3", 6.0)])}
    r = _double(channel_dense=lambda *a, **k: _hits("d"))
    res, _mgr, _rtr = _search(r, shards, config=("dense", "cold"))
    pids = res.channel_hits["cold:dense"]
    fams = [r.family_key(p) for p in pids]
    assert len(fams) == len(set(fams)), f"a family survived twice across shards: {fams}"
    assert set(fams) == {"F1", "F2", "F3"}


# =========================================================================== routing and phases

def test_phase_two_reroutes_with_the_candidates_and_catches_a_newly_woken_domain(monkeypatch):
    """The candidate distribution is 50% of the documented routing mix and does not exist until
    the cheap tiers have answered. A domain only that evidence indicates is woken in phase 2, and
    must then get the phase 1 channels run against it too: it was not reachable when they ran."""
    conns = []

    def route_conn():
        #  Publication 7 is a phase 1 candidate, and it is classified in B25J. B25J is otherwise
        #  the weakest domain in the prior, so nothing but the candidate vote can reach it.
        c = testing.routing_connection(prior={"B66C": 10000, "B25J": 1},
                                       classified={7: ("B25J1/00",)})
        conns.append(c)
        return c

    monkeypatch.setattr(cold, "route_connection", route_conn)
    monkeypatch.setattr(shard_router, "MAX_ROUTES", 1)
    b25j = testing.shard("B25J", docs=[(701, "F-B25J")])
    shards = {"B66C": testing.shard("B66C", docs=[(101, "F-B66C")]), "B25J": b25j}
    r = _double(channel_dense=lambda *a, **k: [(7, 9.0)])
    res, _mgr, _rtr = _search(r, shards, config=("dense", "cold"))
    assert len(conns) == 2, "the tier routed once; the candidate evidence was never used"
    assert "B25J" in res.tiers["cold"]["queried"], res.tiers["cold"]
    assert b25j.sql_log, "the newly woken shard was never queried"
    assert any(kind == "dense" for kind, _s, _p in b25j.sql_log), (
        "a domain that only woke in phase 2 never ran the phase 1 channels")


def test_the_tier_mirrors_the_presets_own_channels_and_nothing_else():
    sh = testing.shard("B66C", docs=[(101, "F1")])
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_bm25=lambda *a, **k: _hits("b"),
                channel_cpc=lambda *a, **k: _hits("c"))
    _search(r, {"B66C": sh}, config=("dense", "bm25", "cold"))
    kinds = {kind for kind, _s, _p in sh.sql_log if kind not in ("set", "families")}
    assert kinds == {"dense", "bm25"}, kinds


def test_channel_order_is_the_preset_then_the_cold_mirror_then_global():
    """Channel order reaches the output through `channel_hits` and through RRF's iteration, so it
    must be a property of the preset and not of which shard answered first."""
    shards = {"B66C": testing.shard("B66C", docs=[(101, "F1")], latency=0.05),
              "B65G": testing.shard("B65G", docs=[(201, "F2")])}
    r = _double(channel_dense=lambda *a, **k: _hits("d"),
                channel_bm25=lambda *a, **k: _hits("b"))
    seen = []
    for _ in range(3):
        res, _mgr, _rtr = _search(r, shards, config=("dense", "bm25", "cold"))
        seen.append(list(res.channel_hits))
    assert seen[0] == ["dense", "bm25", "cold:dense", "cold:bm25"], seen[0]
    assert len({tuple(s) for s in seen}) == 1, f"channel order is not stable: {seen}"


def test_the_remote_lane_has_its_own_bound_and_its_own_pool():
    """A shard wake must never occupy a slot the dense channel needed. Separate lane, separate
    semaphore, separate pool."""
    assert orch.REMOTE_LANE != orch.DB_LANE
    assert orch._remote_pool() is not orch._pool()
    assert 1 <= orch.REMOTE_CONCURRENCY <= orch.MAX_REMOTE_CONCURRENCY


def test_a_remote_task_does_not_consume_the_database_lane():
    """Eight hot channels at a bound of two, plus a cold tier that blocks: the hot channels must
    still reach their own bound, which they cannot if the cold task took a db slot."""
    bound = 2
    live = {"now": 0, "max": 0}
    lock = threading.Lock()
    at_bound = threading.Event()
    cold_started = threading.Event()

    class BlockingShard(testing.SyntheticShard):
        def rows_for(self, kind):
            cold_started.set()
            at_bound.wait(timeout=BARRIER_TIMEOUT)
            return super().rows_for(kind)

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

    r = _double(channel_dense=stub("d"), channel_bm25=stub("b"), channel_cpc=stub("c"))
    with testing.installed(shards={"B66C": BlockingShard("B66C",
                                                         [testing.ShardDoc(1, "F", 1.0)])}):
        r.search("q", config=["dense", "bm25", "cpc", "cold"], db_concurrency=bound)
    assert cold_started.is_set(), "the cold tier never started"
    assert live["max"] == bound, f"the db lane never reached its bound: {live['max']}"


def test_retrieval_exports_the_new_seams():
    assert retrieval.cold is cold
    assert retrieval.channels is channels
    assert callable(retrieval.channel_weight)


def test_a_straggler_from_an_earlier_fleet_is_not_counted_against_this_one():
    """REGRESSION. `test_the_tier_budget_stops_a_hung_shard_from_hanging_the_search` abandons a
    task on purpose: the task owns its connection and cancelling it would leak the connection on a
    real shard, so it is left to finish and release in its own `finally`. It finishes seconds
    later, when the NEXT test has installed a different fleet under the same domain name, and the
    straggler's release used to be counted there: `leaked_connections()` reported `{'B66C': -1}`
    for a fleet that had handed out and taken back exactly one connection. Observed in a full-suite
    run on 2026-08-22 and not reproducible on its own, which is what makes it worth a test."""
    old_fleet = testing.shard("B66C", docs=[(1, "F1")])
    new_fleet = testing.shard("B66C", docs=[(2, "F2")])
    mgr = testing.SyntheticShardManager(shards={"B66C": new_fleet})

    conn = mgr.connection("B66C")                      # this fleet's own, opened and released
    mgr.release("B66C", conn)
    assert mgr.leaked_connections() == {}

    straggler = testing.FakeConnection(old_fleet)      # the abandoned task, finishing late
    mgr.release("B66C", straggler)
    assert straggler.closed, "the straggler's connection was not closed"
    assert mgr.leaked_connections() == {}, mgr.leaked_connections()
    assert mgr.open_connections == 0
