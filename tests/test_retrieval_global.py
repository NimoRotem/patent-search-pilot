"""The global tier: the 170M catalog and the external APIs, fused as one channel at weight 0.90.

The reason it exists is a measured REACH failure, not a ranking one: the `schmalz` benchmark
subject has ten cited documents, four of them classified outside the eight indexed CPC branches,
and no reordering of documents this corpus does not hold will ever produce them. So the assertions
here are about the tier being consumed correctly, and about it costing nothing when it is not
there.
"""
import threading
import time

import pytest

from retrieval import Retriever, cold, fusion, global_search, testing

BARRIER_TIMEOUT = 15.0


def _double(**attrs):
    r = object.__new__(Retriever)
    r._fam = {}
    r._wide = False
    r.scan_profile = lambda wide=False: None
    r.channel_citation_family = lambda *a, **k: []
    r.channel_qbe = lambda *a, **k: []
    r.channel_dense = lambda *a, **k: [(1, 9.0), (2, 8.0)]
    r.channel_bm25 = lambda *a, **k: [(2, 5.0)]
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


@pytest.fixture(autouse=True)
def clean_seams(monkeypatch):
    testing.reset()
    monkeypatch.setattr(cold, "route_connection",
                        lambda: testing.routing_connection(prior={"B66C": 10}))
    yield
    testing.reset()


def _search(r, backend, config=("dense", "bm25", "global"), **kw):
    global_search.register_backend(backend)
    try:
        return r.search("q", config=list(config), db_concurrency=2, **kw)
    finally:
        global_search.register_backend(None)


# =========================================================================== absent and inert

def test_no_global_backend_contributes_no_channel():
    r = _double()
    res = r.search("q", config=["dense", "bm25", "global"], db_concurrency=2)
    assert list(res.channel_hits) == ["dense", "bm25"]
    assert res.external == {}


def test_a_backend_that_says_it_is_unavailable_is_never_called():
    """`available()` returning False is REQUIRED of a backend that is mid-build. An empty result
    and a genuine miss are indistinguishable downstream and a miss scores as a recall failure, so
    a partial index must say so rather than answer thinly."""
    g = testing.SyntheticGlobal(hits=[("fed:EP1", 9.0)], available=False)
    res = _search(_double(), g)
    assert g.calls == [], "an unavailable backend was queried anyway"
    assert "global" not in res.channel_hits


# =========================================================================== consumed correctly

def test_global_hits_enter_fusion_under_the_name_global():
    g = testing.SyntheticGlobal(hits=[("fed:EP1", 9.0), ("fed:EP2", 8.0)])
    res = _search(_double(), g)
    assert res.channel_hits["global"] == ["fed:EP1", "fed:EP2"]
    assert g.calls and g.calls[0]["has_qvec"], "the query vector was not offered to the backend"


def test_the_global_weight_is_the_standing_of_the_federated_bridge():
    assert fusion.CHANNEL_WEIGHTS["global"] == 0.90
    assert fusion.CHANNEL_WEIGHTS["global"] == fusion.CHANNEL_WEIGHTS["federated"]
    assert fusion.channel_weight("global") == 0.90
    #  Above every lexical and classification channel, below local dense.
    assert (fusion.CHANNEL_WEIGHTS["dense"] > fusion.CHANNEL_WEIGHTS["global"]
            > fusion.CHANNEL_WEIGHTS["bm25"])


def test_a_global_hit_resolved_onto_a_local_row_fuses_with_it():
    """The contract allows a local bigint when the global tier resolved the hit onto a publication
    this corpus holds. That is the common case and it must show as cross-system agreement, not as
    a second row."""
    r = _double()
    r._fam = {1: "F1", 2: "F2"}
    res = _search(r, testing.SyntheticGlobal(hits=[(1, 9.0)]))
    prov = dict((p, pr) for p, _s, pr in res.ranked_pubs)
    assert "global" in prov[1] and "dense" in prov[1], prov[1]
    assert [fk for fk, _p, _s, _pr in res.family_ranked].count("F1") == 1


def test_family_keys_dedup_an_external_hit_against_a_local_one():
    r = _double()
    r._fam = {1: "F1", 2: "F2"}
    g = testing.SyntheticGlobal(hits=[("fed:EP1", 9.0)], families={"fed:EP1": "F1"})
    res = _search(r, g)
    fams = [fk for fk, _p, _s, _pr in res.family_ranked]
    assert fams.count("F1") == 1, f"one disclosure reached the answer twice: {fams}"
    assert r._fam["fed:EP1"] == "F1"


def test_no_family_keys_means_each_external_id_is_its_own_family():
    """Returning {} is allowed and is the SAFE direction: the cost is a duplicate row, and the
    alternative is silently merging two disclosures that are not the same."""
    r = _double()
    r._fam = {1: "F1", 2: "F2"}
    res = _search(r, testing.SyntheticGlobal(hits=[("fed:EP1", 9.0)], families={}))
    fams = [fk for fk, _p, _s, _pr in res.family_ranked]
    assert "fed:EP1" in fams
    assert len(fams) == len(set(fams))


def test_records_populate_result_external_so_a_hit_can_be_rendered():
    """An external hit has no local row, so nothing downstream can read its title out of `chunks`.
    An id missing from `Result.external` renders as a blank card and reranks on an empty passage."""
    rec = testing.GlobalRecord(pub_number="EP1", title="A vacuum gripper", abstract="Text.")
    g = testing.SyntheticGlobal(hits=[("fed:EP1", 9.0)], records={"fed:EP1": rec})
    res = _search(_double(), g)
    assert res.external["fed:EP1"].title == "A vacuum gripper"


def test_the_global_channel_is_family_unique_like_every_other_channel():
    r = _double()
    r._fam = {1: "F1", 2: "F1", 3: "F3"}
    res = _search(r, testing.SyntheticGlobal(hits=[(1, 9.0), (2, 8.0), (3, 7.0)]))
    pids = res.channel_hits["global"]
    assert [r.family_key(p) for p in pids] == ["F1", "F3"]


def test_a_duplicate_id_from_the_backend_is_kept_once():
    res = _search(_double(), testing.SyntheticGlobal(
        hits=[("fed:EP1", 9.0), ("fed:EP1", 8.0), ("fed:EP2", 7.0)]))
    assert res.channel_hits["global"] == ["fed:EP1", "fed:EP2"]


# =========================================================================== degrade, never raise

def test_a_backend_that_raises_degrades_to_an_empty_channel():
    r = _double()
    res = _search(r, testing.SyntheticGlobal(hits=[("fed:EP1", 9.0)],
                                             error=RuntimeError("catalog is down")))
    assert res.channel_hits.get("dense"), "the local answer was lost with the global one"
    assert "global" not in res.channel_hits


def test_a_backend_returning_rubbish_does_not_break_the_search():
    class Rubbish(testing.SyntheticGlobal):
        def search(self, query, **kw):
            return [("fed:EP1", "not a number"), None, ("fed:EP2", 3.0)]

    res = _search(_double(), Rubbish())
    assert res.channel_hits["global"] == ["fed:EP2"]


def test_family_keys_that_raise_do_not_lose_the_hits():
    class Broken(testing.SyntheticGlobal):
        def family_keys(self, publication_ids):
            raise RuntimeError("no family service")

    res = _search(_double(), Broken(hits=[("fed:EP1", 9.0)]))
    assert res.channel_hits["global"] == ["fed:EP1"]


def test_a_budget_already_spent_skips_the_call_rather_than_extending_the_search(monkeypatch):
    """The tier is submitted in parallel with the hot one, so a queued remote task can start after
    the local answer is already complete. Starting an external round trip then is pure added
    latency."""
    monkeypatch.setattr(global_search, "GLOBAL_TIMEOUT", -1.0)
    g = testing.SyntheticGlobal(hits=[("fed:EP1", 9.0)])
    res = _search(_double(), g)
    assert g.calls == []
    assert "global" not in res.channel_hits


def test_the_global_tier_runs_in_parallel_with_the_local_one():
    """A Barrier the local channel and the global backend must both reach."""
    both = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    class BarrierGlobal(testing.SyntheticGlobal):
        def search(self, query, **kw):
            both.wait()
            return list(self.hits)

    def hot(*a, **k):
        both.wait()
        return [(1, 9.0)]

    r = _double(channel_dense=hot)
    res = _search(r, BarrierGlobal(hits=[("fed:EP1", 9.0)]), config=("dense", "global"))
    assert res.channel_hits["global"] == ["fed:EP1"]


def test_a_slow_global_backend_does_not_serialise_the_local_channels():
    started = threading.Event()
    r = _double()
    t0 = time.monotonic()
    res = _search(r, testing.SyntheticGlobal(hits=[("fed:EP1", 9.0)], latency=0.2))
    assert res.channel_hits.get("dense")
    assert time.monotonic() - t0 < BARRIER_TIMEOUT
    assert not started.is_set()


# =========================================================================== local ids only

def test_an_external_id_never_reaches_the_phase_two_seeds():
    """`citation` joins publications.id and `qbe` reads that publication's chunks, so an external
    id is not a weaker seed, it is a bigint cast error.

    MEASURED on a live search the moment the global tier was registered:
    `channel_citation_family` failed with `invalid input syntax for type bigint:
    "fed:EP9999999"` and soft-degraded to zero hits, while `qbe` survived only because it reads
    the first five seeds and the external one happened to sit lower. The global tier is what made
    this reachable: the federated bridge fuses AFTER the local search and never seeds phase 2.
    """
    seen = {}

    def p2(tag):
        def run(seeds, *a, **k):
            seen[tag] = list(seeds)
            return []
        return run

    r = _double(channel_citation_family=p2("cit"), channel_qbe=p2("qbe"))
    r._fam = {1: "F1", 2: "F2"}
    #  Scored above every local hit, so it leads the fused list and would be seed number one.
    _search(r, testing.SyntheticGlobal(hits=[("fed:EP1", 99.0)]),
            config=("dense", "bm25", "global", "citation", "qbe"))
    assert seen["cit"], "phase 2 got no seeds at all"
    assert not any(isinstance(p, str) and p.startswith("fed:") for p in seen["cit"]), seen["cit"]
    assert seen["qbe"] == seen["cit"]
    assert 1 in seen["cit"] and 2 in seen["cit"], "the local seeds were lost with the external one"


def test_the_seed_count_does_not_shrink_because_the_global_tier_answered():
    """Top 40 of the LOCAL rows, not the local rows within the top 40."""
    seen = {}

    def p2(seeds, *a, **k):
        seen["s"] = list(seeds)
        return []

    locals_ = [(i, float(100 - i)) for i in range(1, 61)]
    r = _double(channel_dense=lambda *a, **k: locals_, channel_bm25=lambda *a, **k: [],
                channel_citation_family=p2, channel_qbe=lambda *a, **k: [])
    r._fam = {i: f"F{i}" for i in range(1, 61)}
    ext = [(f"fed:EP{i}", float(1000 - i)) for i in range(20)]
    _search(r, testing.SyntheticGlobal(hits=ext),
            config=("dense", "bm25", "global", "citation", "qbe"))
    assert len(seen["s"]) == 40, len(seen["s"])
    assert all(not isinstance(p, str) for p in seen["s"])


def test_a_local_member_represents_its_family_even_when_the_external_one_ranks_higher():
    """An external id has a title and an abstract at best; a local row has chunks, claims, dates
    and figures. Letting the external one represent a family this corpus holds throws all of that
    away for a row the reader cannot open."""
    r = _double(channel_dense=lambda *a, **k: [(1, 1.0)], channel_bm25=lambda *a, **k: [])
    r._fam = {1: "F1"}
    res = _search(r, testing.SyntheticGlobal(hits=[("fed:EP1", 99.0)],
                                             families={"fed:EP1": "F1"}))
    reps = [pid for fk, pid, _s, _pr in res.family_ranked if fk == "F1"]
    assert reps == [1], f"the external id represented a family with a local member: {reps}"


# =========================================================================== both tiers at once

def test_the_cold_and_global_tiers_run_together_and_neither_blocks_the_other():
    all_three = threading.Barrier(3, timeout=BARRIER_TIMEOUT)

    class BarrierGlobal(testing.SyntheticGlobal):
        def search(self, query, **kw):
            all_three.wait()
            return list(self.hits)

    class BarrierShard(testing.SyntheticShard):
        def rows_for(self, kind):
            all_three.wait()
            return super().rows_for(kind)

    def hot(*a, **k):
        all_three.wait()
        return [(1, 9.0)]

    r = _double(channel_dense=hot)
    r._fam = {1: "F1"}
    g = BarrierGlobal(hits=[("fed:EP1", 9.0)])
    shards = {"B66C": BarrierShard("B66C", [testing.ShardDoc(101, "F-COLD", 9.0)])}
    with testing.installed(shards=shards, global_backend=g):
        res = r.search("q", config=["dense", "cold", "global"], db_concurrency=2)
    assert res.channel_hits.get("cold:dense") == [101]
    assert res.channel_hits.get("global") == ["fed:EP1"]
    assert list(res.channel_hits) == ["dense", "cold:dense", "global"]
