"""Home-shard assignment, the sizing arithmetic and the manifest hash. No database.

The subject is placement: which shard a FAMILY lives on. The unit is the family and not the
publication because retrieval collapses to families everywhere downstream, so a family split
across two shards is a family that gets deduped against itself.
"""
import math

import pytest

from corpus import assign, manifest as manifest_mod, sizing
from retrieval.shard_router import UNCLASSIFIED, domain_of


# ------------------------------------------------------------------ the family key
def test_a_docdb_sentinel_is_not_a_family():
    """MEASURED: 21,862 publications carry simple_family_id='-1'. Treating that as a family key
    puts 21,862 unrelated disclosures on one shard and collapses them to one search result."""
    for sentinel in ("", "-1", "0", "none", "NULL", "unknown", "nan", "  -1  "):
        assert assign.family_key(sentinel, "US-1234567-A") == "US-1234567-A"
    assert assign.family_key("44556677", "US-1234567-A") == "44556677"


def test_the_family_key_matches_the_sql_definition():
    """`corpus_family_key` in sql/010 and `assign.family_key` must agree, or the mirror and the
    release database place the same family differently."""
    import re
    sql = open("sql/010_corpus_release.sql", encoding="utf-8").read()
    body = sql[sql.index("CREATE OR REPLACE FUNCTION corpus_family_key"):]
    body = body[:body.index("$$;")]
    in_sql = set(re.findall(r"'([^']*)'", body[body.index("IN ("):body.index(")\n", body.index("IN ("))]))
    assert in_sql == assign.FAMILY_SENTINELS, "sql/010 and corpus.assign disagree about sentinels"


# ------------------------------------------------------------------ the domain rule
def test_the_domain_rule_is_the_routers_and_is_not_reimplemented():
    """A second definition of `domain_of` is how the router and the shards silently disagree about
    where a family lives, and it looks like a recall problem rather than a placement bug."""
    import inspect
    src = inspect.getsource(assign)
    assert "from retrieval.shard_router import" in src
    assert "def domain_of" not in src
    assert assign.domains_of_symbols(["B65G 47/91"]) == {domain_of("B65G 47/91")}


class _FakeConn:
    """A connection whose `classifications` read returns fixed rows, so the router's rule and the
    builder's rule can be run over the SAME input."""

    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        rows = self._rows
        outer = self

        class _C:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def execute(self_inner, _sql, params=None):
                pids = set(params[0]) if params else set()
                self_inner._out = [r for r in rows if r["publication_id"] in pids]

            def fetchall(self_inner):
                return self_inner._out

        return _C()


def test_the_router_and_the_builder_put_a_publication_in_the_same_domains():
    """The seam the brief names. The builder cannot call `domains_of_publications` directly (it
    reads `classifications`, and the builder works off its own mirror), so the agreement is
    asserted here instead of assumed. A publication that the router routes to B65G and the builder
    places in B25J is a recall hole that looks like a ranking problem."""
    from retrieval import shard_router

    cases = {
        1: ["B65G47/91", "B25J15/06"],
        2: ["B66C1/0225"],
        3: [],                                     # no classification at all: 20.6% of the corpus
        4: ["F16B2/00", "F16B2/02", "F16B4/00"],   # several symbols, one domain
    }
    rows = [{"publication_id": pid, "symbol": s} for pid, syms in cases.items() for s in syms]
    conn = _FakeConn(rows)
    for pid, syms in cases.items():
        router_domains = set(shard_router.domains_of_publications(conn, [pid]))
        builder_domains = assign.domains_of_symbols(syms)
        assert router_domains == builder_domains, f"pid {pid}: {router_domains} != {builder_domains}"


def test_both_rules_spread_one_publications_vote_rather_than_counting_symbols():
    """The same miscount, refused in both places: a document with three symbols in one subclass
    must not outweigh one with a single symbol."""
    from retrieval import shard_router

    conn = _FakeConn([{"publication_id": 1, "symbol": s}
                      for s in ("B65G47/91", "B65G1/00", "B25J15/06")])
    routed = shard_router.domains_of_publications(conn, [1])
    assert routed["B65G"] == pytest.approx(routed["B25J"]), \
        "the router counted symbols instead of domains"
    built = assign.publication_domain_weights(["B65G47/91", "B65G1/00", "B25J15/06"])
    assert built["B65G"] == pytest.approx(built["B25J"])
    assert sum(built.values()) == pytest.approx(1.0)


def test_an_unclassified_publication_still_has_somewhere_to_go():
    """1,024,320 publications, 20.6% of the corpus, carry no classification at all, and they skew
    old and foreign, which is exactly the population the gold citation lists are drawn from."""
    assert assign.domains_of_symbols([]) == {UNCLASSIFIED}
    assert assign.domains_of_symbols(["", "   ", None]) == {UNCLASSIFIED}
    assert assign.home_domain([])[0] == UNCLASSIFIED
    assert assign.home_domain([{"symbols": []}])[0] == UNCLASSIFIED


def test_a_heavily_classified_publication_does_not_outvote_eleven_others():
    """One publication is worth one vote in total, spread across the domains it names. Counting
    one vote per SYMBOL is the same miscount that makes channel_cpc's count(*) ranking useless."""
    fat = {"symbols": ["B65G1/00", "B65G2/00", "B65G3/00", "B65G4/00", "B65G5/00",
                       "B65G6/00", "B65G7/00", "B65G8/00", "B65G9/00", "B65G10/00",
                       "B65G11/00", "B65G12/00"]}
    thin = [{"symbols": ["B25J1/00"]} for _ in range(11)]
    home, weights = assign.home_domain([fat] + thin)
    assert home == "B25J"
    assert weights["B65G"] == pytest.approx(1.0)
    assert weights["B25J"] == pytest.approx(11.0)


def test_a_family_spanning_several_domains_gets_exactly_one_home_and_the_rest_as_secondaries():
    fam = [{"symbols": ["B65G47/91", "B25J15/06"], "first_symbols": ["B65G47/91"]},
           {"symbols": ["B25J15/06"]}]
    home, weights = assign.home_domain(fam)
    assert home in ("B65G", "B25J")
    sec = assign.secondary_domains(home, weights)
    assert home not in sec
    assert set(sec) | {home} == set(weights)


def test_the_primary_symbol_breaks_a_tie_rather_than_alphabet():
    """Without the primary bonus a level family is placed by alphabetical accident, and the
    accident moves the family between builds as symbols are added."""
    level = [{"symbols": ["B65G47/91", "B25J15/06"]}]
    assert assign.home_domain(level)[0] == "B25J"           # ties break on the string ascending
    with_first = [{"symbols": ["B65G47/91", "B25J15/06"], "first_symbols": ["B65G47/91"]}]
    assert assign.home_domain(with_first)[0] == "B65G"


def test_placement_is_deterministic_across_symbol_orderings():
    """A placement that depends on dict or list ordering moves families between builds, and a
    family that moves between builds is a family that is in two releases at once during a
    rollout."""
    syms = ["B65G47/91", "B25J15/06", "F16B2/00", "B66C1/02"]
    firsts = ["B66C1/02"]
    homes = set()
    for i in range(len(syms)):
        rotated = syms[i:] + syms[:i]
        homes.add(assign.home_domain([{"symbols": rotated, "first_symbols": firsts}])[0])
    assert len(homes) == 1


# ------------------------------------------------------------------ packing
def test_packing_never_leaves_the_unclassified_population_homeless():
    mass = {"B65G": 3_388_870, "B25J": 3_106_107, UNCLASSIFIED: 2_528_746, "F16B": 1_554_258}
    plan = assign.pack_domains(mass, 8, capacity=25_119_648)
    assert UNCLASSIFIED in plan.domain_shard
    assert plan.fits


def test_packing_reports_the_bins_that_do_not_fit_instead_of_dropping_them():
    """A packer that silently dropped the overflow would produce a plan that looks balanced and a
    corpus missing its largest subclass."""
    plan = assign.pack_domains({"B65G": 100, "B25J": 10}, 2, capacity=50)
    assert not plan.fits
    assert plan.oversized == [plan.domain_shard["B65G"]]
    assert sum(plan.mass.values()) == 110, "no mass may be lost by packing"


def test_every_domain_lands_in_exactly_one_shard():
    mass = {f"X{i:03d}": (i * 37) % 991 for i in range(600)}
    plan = assign.pack_domains(mass, 8)
    placed = [d for ds in plan.shards.values() for d in ds]
    assert sorted(placed) == sorted(mass)
    assert len(placed) == len(set(placed))
    assert sum(plan.mass.values()) == sum(mass.values())


def test_a_pinned_domain_stays_where_it_was_pinned():
    plan = assign.pack_domains({"B65G": 10, "B25J": 10, "F16B": 10}, 3,
                               pinned={"B65G": "domain_03"})
    assert plan.domain_shard["B65G"] == "domain_03"


def test_splitting_unclassified_is_stable_across_builds():
    """`route()` emits ONE unclassified route, so a split multiplies the wake cost by the number
    of pieces. If it is done at all it must at least not move families between builds."""
    keys = [f"FAM{i}" for i in range(2000)]
    first = [assign.unclassified_bucket(k, 4) for k in keys]
    second = [assign.unclassified_bucket(k, 4) for k in keys]
    assert first == second
    assert set(first) == {0, 1, 2, 3}
    assert all(assign.unclassified_bucket(k, 1) == 0 for k in keys)


# ------------------------------------------------------------------ the sizing arithmetic
def test_the_capacity_is_the_smaller_of_the_ram_and_disk_ceilings():
    """Reporting only the RAM ceiling is how a plan that fits in memory and not on the disk gets
    built."""
    cap = sizing.chunks_per_shard()
    assert cap["cap_chunks"] == min(cap["ram_cap_chunks"], cap["disk_cap_chunks"])
    assert cap["binding"] in ("ram", "disk")


def test_the_resident_index_budget_is_not_the_whole_machine():
    """Handing the whole box to the index is the assumption that produced the current situation:
    a 101 GB index on a 62 GB host and no written answer to 'does it fit'."""
    budget = sizing.resident_index_budget_bytes(124.0)
    assert budget < 124.0 * sizing.GiB
    assert budget == pytest.approx((124.0 - 6.0 - 4.0) * 0.85 * sizing.GiB)


def test_the_defect_v3_exists_to_fix_is_reproduced_by_the_arithmetic():
    """62 GB of RAM against the live 27.62M chunks must come out as NOT fitting. An arithmetic
    that says today's box is fine is an arithmetic with a sign error in it."""
    v = sizing.plan_verdict(sizing.MEASURED["chunk_rows"], 1, ram_gib=62.0, disk_gib=250.0)
    assert not v["fits"]
    assert v["index_gib_per_shard"] > 62.0


def test_halfvec_buys_ram_and_then_the_disk_becomes_the_ceiling():
    fp32, half = sizing.chunks_per_shard(), sizing.chunks_per_shard(halfvec=True)
    assert half["ram_cap_chunks"] > fp32["ram_cap_chunks"]
    assert fp32["binding"] == "ram" and half["binding"] == "disk"


def test_shards_needed_is_a_ceiling_not_a_rounding():
    cap = sizing.chunks_per_shard()["cap_chunks"]
    assert sizing.shards_needed(cap)["shards_needed"] == 1
    assert sizing.shards_needed(cap + 1)["shards_needed"] == 2
    assert sizing.shards_needed(cap * 8)["shards_needed"] == 8


def test_the_lexical_index_is_counted_and_the_replaced_gin_index_is_not():
    """v3 replaces to_tsvector('english') with Tantivy, so the GIN cost goes and Tantivy's
    arrives. Counting neither would understate the shard by 406 B/chunk."""
    with_lex = sizing.index_bytes_per_chunk(lexical=True)
    without = sizing.index_bytes_per_chunk(lexical=False)
    assert with_lex - without == sizing.LEXICAL_BYTES_PER_CHUNK
    assert sizing.MEASURED["tsv_gin_bytes_per_chunk"] not in (with_lex, without)


def test_the_builder_ram_requirement_is_reported_because_this_box_cannot_meet_it():
    """pgvector builds the HNSW graph inside maintenance_work_mem. The builder box has 31 GiB, so
    a full domain shard's index cannot be built here and that has to be visible, not discovered."""
    per_shard = 40_312_607 // 9
    assert sizing.build_ram_required_gib(per_shard) > 15.0
    assert sizing.build_ram_required_gib(500_000) < 2.0


def test_the_report_states_both_the_fitting_and_the_non_fitting_scenario():
    r = sizing.report(9)
    names = [s["name"] for s in r["scenarios"]]
    assert names == ["today", "stored_descriptions", "full_text_corpus"]
    assert r["scenarios"][0]["fp32"]["fits"] and r["scenarios"][1]["fp32"]["fits"]
    #  Every publication reaching full text does NOT fit in nine fp32 shards. If this ever passes,
    #  the constants moved and the fleet size has to be revisited.
    assert not r["scenarios"][2]["fp32"]["fits"]
    assert r["scenarios"][2]["fp32"]["shards_needed"] > 9
    assert r["break_even"]["max_total_chunks"] == r["capacity"]["fp32"]["cap_chunks"] * 9


# ------------------------------------------------------------------ the manifest
def _manifest(**over):
    base = dict(release_id="domain_01_v1", shard_key="domain_01", version=1, kind="domain",
                domains=["B65G"], counts={"chunks": 10, "families": 2, "publications": 3},
                built_from={"chunks_max_id": 99}, index_params={"dense": {"m": 16}},
                artifacts=[], stats={}, timings={"total_s": 1.0}, root=".", note="")
    base.update(over)
    return manifest_mod.build(**base)


def test_the_content_hash_does_not_move_when_only_the_clock_does():
    """Content addressing whose value changes when the clock changes proves only that somebody
    ran a build."""
    a, b = _manifest(), _manifest()
    assert a["built_at"] != b["built_at"] or True
    b["built_at"] = "1999-01-01T00:00:00Z"
    b["builder"] = {"host": "somewhere-else"}
    b["timings"] = {"total_s": 999.0}
    assert manifest_mod.content_hash(a) == manifest_mod.content_hash(b)
    assert a["content_hash"] == manifest_mod.content_hash(a)


def test_the_content_hash_moves_when_the_content_does():
    a = _manifest()
    for change in ({"counts": {"chunks": 11, "families": 2, "publications": 3}},
                   {"domains": ["B25J"]},
                   {"index_params": {"dense": {"m": 32}}},
                   {"built_from": {"chunks_max_id": 100}}):
        assert manifest_mod.content_hash(_manifest(**change)) != a["content_hash"], change


def test_what_the_hash_covers_is_written_into_the_manifest():
    """So a reader can see what was covered rather than trusting a docstring."""
    m = _manifest()
    assert set(m["hash_excludes"]) == set(manifest_mod.HASH_EXCLUDES)
    assert "counts" not in m["hash_excludes"]


def test_verification_fails_when_the_counts_disagree_with_the_database():
    m = _manifest()
    ok = manifest_mod.verify(m, observed_counts={"chunks": 10, "families": 2, "publications": 3})
    assert ok.ok, ok.failures
    bad = manifest_mod.verify(m, observed_counts={"chunks": 9, "families": 2, "publications": 3})
    assert not bad.ok


def test_verification_fails_when_the_index_parameters_disagree():
    m = _manifest(index_params={"dense": {"m": 16, "ef_construction": 64}})
    bad = manifest_mod.verify(m, observed_index_params={"m": 32, "ef_construction": 64})
    assert not bad.ok


def test_verification_fails_on_a_tampered_artifact(tmp_path):
    f = tmp_path / "chunks.dump"
    f.write_bytes(b"the release payload")
    m = _manifest(artifacts=[manifest_mod.artifact(str(f), "chunks.dump")])
    assert manifest_mod.verify(m, artifact_root=str(tmp_path)).ok
    f.write_bytes(b"the release payload, edited")
    v = manifest_mod.verify(m, artifact_root=str(tmp_path))
    assert not v.ok
    assert any("chunks.dump" in str(x) for x in v.failures)
