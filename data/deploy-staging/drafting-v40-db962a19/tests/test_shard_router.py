"""The routing distribution, and the one route that can never be dropped.

MEASURED: 1,024,320 publications, 20.6% of the corpus, carry no classification at all, and they
skew old and foreign, which is exactly the population the gold citation lists are drawn from. A
router that only wakes classified shards makes that share unreachable, and unreachable art is
indistinguishable from art that does not exist.
"""
import pytest

from retrieval import shard_router, testing
from retrieval.shard_router import UNCLASSIFIED


@pytest.fixture(autouse=True)
def no_cached_prior():
    shard_router.reset_prior()
    yield
    shard_router.reset_prior()


def _conn(**kw):
    kw.setdefault("prior", {"B66C": 4000, "B65G": 3000, "B25J": 2000, UNCLASSIFIED: 1000})
    return testing.routing_connection(**kw)


# =========================================================================== the domain
def test_a_domain_is_a_cpc_subclass():
    assert shard_router.domain_of("B66C1/0225") == "B66C"
    assert shard_router.domain_of("b66c 1/02") == "B66C"
    assert shard_router.domain_of(" B25J ") == "B25J"
    assert shard_router.DOMAIN_LEN == 4


def test_a_symbol_too_short_to_name_a_subclass_routes_to_unclassified():
    for bad in (None, "", "B", "B66"):
        assert shard_router.domain_of(bad) == UNCLASSIFIED


def test_a_publication_spreads_one_vote_across_its_domains():
    """A heavily classified document must not out-vote a sparsely classified one. That miscount is
    exactly what makes `channel_cpc`'s count(*) ranking meaningless, and it must not be repeated
    in the thing that decides which VMs to start."""
    #  Publication 1 carries three symbols in TWO domains. It gets one vote, split by domain and
    #  not by symbol, so its two B66C symbols do not buy it twice the say of its one B25J symbol.
    conn = _conn(classified={1: ("B66C1/02", "B66C1/03", "B25J15/06"), 2: ("B65G47/91",)})
    dist = shard_router.domains_of_publications(conn, [1, 2])
    assert pytest.approx(sum(dist.values())) == 1.0
    assert dist["B65G"] == pytest.approx(0.5), dist
    assert dist["B66C"] == pytest.approx(0.25), dist
    assert dist["B25J"] == pytest.approx(0.25), dist


def test_a_publication_with_no_classification_votes_for_the_unclassified_route():
    conn = _conn(classified={1: ("B66C1/02",)})
    dist = shard_router.domains_of_publications(conn, [1, 2])
    assert dist[UNCLASSIFIED] == pytest.approx(0.5), dist


# =========================================================================== the mix
def test_route_never_returns_an_empty_list_even_with_no_evidence_at_all():
    conn = _conn(prior={})
    routes = shard_router.route(conn)
    assert routes
    assert routes[0]["domain"] == UNCLASSIFIED
    assert routes[0]["weight"] == 1.0


def test_the_unclassified_route_survives_a_cut_it_did_not_make():
    """It is appended at whatever weight it earned, or at its corpus share if it earned none, and
    `max_routes` is a ceiling on the CLASSIFIED routes, not a reason to lose 20.6% of the corpus."""
    conn = _conn(prior={"B66C": 9000, "B65G": 8000, "B25J": 7000, "B23Q": 6000, "F16B": 5000,
                        "H01L": 4000, UNCLASSIFIED: 1})
    routes = shard_router.route(conn, max_routes=3)
    doms = [r["domain"] for r in routes]
    assert len(doms) == 4, doms
    assert doms[-1] == UNCLASSIFIED
    assert doms[:3] == ["B66C", "B65G", "B25J"]


def test_weights_are_normalised_over_the_routes_returned():
    routes = shard_router.route(_conn(), max_routes=2)
    assert pytest.approx(sum(r["weight"] for r in routes), abs=1e-5) == 1.0


def test_every_route_carries_the_sources_that_produced_it():
    """A routing decision that cannot be explained cannot be debugged: 'why did this search wake
    B23Q' has to have an answer that is not 'read the code'."""
    conn = _conn(classified={7: ("B25J15/06",)})
    routes = shard_router.route(conn, candidate_pids=[7], predicted_cpc=["B66C1/0225"])
    by = {r["domain"]: r["sources"] for r in routes}
    assert "candidates" in by["B25J"], by
    assert "predicted_cpc" in by["B66C"], by
    assert "prior" in by["B65G"], by


def test_a_source_that_produced_nothing_has_its_weight_redistributed():
    """The mix is 50/25/15/10. With only the subject's own symbols and the prior, those two must
    still add up to a whole answer rather than to 35% of one."""
    conn = _conn(prior={"B66C": 1})
    routes = shard_router.route(conn, predicted_cpc=["B66C1/0225"])
    assert pytest.approx(sum(r["weight"] for r in routes), abs=1e-5) == 1.0
    b66c = [r for r in routes if r["domain"] == "B66C"][0]
    #  0.25 of the mix and 0.10 of it are live, so the symbol source carries 0.25/0.35 of the
    #  weight, not 0.25 of it.
    assert b66c["sources"]["predicted_cpc"] == pytest.approx(0.25 / 0.35, abs=1e-4)


def test_the_subjects_own_symbols_are_weighted_most_specific_first():
    conn = _conn(prior={})
    routes = shard_router.route(conn, predicted_cpc=["B66C1/0225", "B25J15/06", "B65G47/91"],
                                include_prior=False)
    weights = {r["domain"]: r["weight"] for r in routes}
    assert weights["B66C"] > weights["B25J"] > weights["B65G"], weights


def test_one_family_votes_once():
    """Rank-decayed, one vote per family: a family with six members in the candidate list must not
    move the routing six times as far as a family with one.

    THE BUG THIS CAUGHT. `_rank_weighted` drops every member of a family after the first, and
    `domains_of_publications` then defaulted a pid that was missing from the weights map to a FULL
    vote of 1.0. So the five suppressed members voted 1.0 each and the one that survived voted
    1/41: the dedup did not merely fail, it inverted. Measured before the fix, this same case:
    B25J 0.986, B66C 0.004.
    """
    conn = _conn(classified={i: ("B25J15/06",) for i in range(1, 7)} | {9: ("B66C1/02",)})
    fam = {i: "F-ONE" for i in range(1, 7)}
    fam[9] = "F-TWO"
    routes = shard_router.route(conn, candidate_pids=[1, 2, 3, 4, 5, 6, 9],
                                family_key=lambda p: fam[p], include_prior=False)
    weights = {r["domain"]: r["weight"] for r in routes}
    assert 0.40 < weights["B66C"] < 0.50, weights
    assert 0.45 < weights["B25J"] < 0.60, weights


def test_a_pid_absent_from_a_supplied_weights_map_does_not_vote():
    """The direct form of the same defect. A weights map is the electoral roll, not a set of
    adjustments, and `w.get(pid, 1.0)` made every abstention a maximal vote."""
    conn = _conn(classified={1: ("B66C1/02",), 2: ("B25J15/06",)})
    dist = shard_router.domains_of_publications(conn, [1, 2], {1: 0.5})
    assert dist == {"B66C": 1.0}, dist


def test_the_corpus_prior_is_computed_once_per_process():
    """7.9 s for the GROUP BY over 53,473,700 classification rows, measured. Once per process is
    fine and once per search is not."""
    conn = _conn()
    shard_router.route(conn)
    first = len(conn.log)
    shard_router.route(conn)
    assert len(conn.log) == first, "the corpus prior was recomputed"


def test_a_prior_that_cannot_be_read_is_a_missing_source_and_not_a_failed_route():
    routes = shard_router.route(_conn(error=True))
    assert routes and routes[0]["domain"] == UNCLASSIFIED


# =========================================================================== the wake seam
def test_wake_never_raises():
    shard_router.register_backend(testing.SyntheticRouter(error=True))
    try:
        out = shard_router.wake([{"domain": "B66C", "weight": 1.0, "sources": {}}])
        assert out["state"] == "failed"
        assert out["woken"] == []
    finally:
        shard_router.register_backend(None)


def test_with_no_backend_wake_reports_that_nothing_was_woken():
    shard_router.register_backend(None)
    out = shard_router.wake([{"domain": "B66C", "weight": 1.0, "sources": {}}])
    assert out == {"woken": [], "state": "not_implemented", "routes": ["B66C"]}
    assert shard_router.available() is False
