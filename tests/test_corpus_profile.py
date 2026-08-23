"""The corpus page: what it is allowed to claim, and what it must not do on a request.

The out-of-domain notice was the only place the scope was ever stated, and it says what the corpus
is NOT. This page says what it is. Two rules hold it up: every number is measured or declared
derived, and nothing that takes minutes of I/O runs while somebody is waiting for a page.
"""
import json
import re
import time

import corpus_profile as cp


def test_the_expensive_counts_are_snapshotted_not_computed_on_view():
    """`classifications` took 183 seconds and `cpc_top` 74 on the real database. A page that runs
    those on view is a page that times out."""
    src = open(cp.__file__.replace(".pyc", ".py")).read()
    body = src[src.index("def profile("):]
    for banned in ("count(*)", "count(DISTINCT"):
        assert banned not in body, "profile() runs an aggregate over the corpus on the request path"
    assert "_load_snapshot()" in body and "_fast_sizes()" in body


def test_the_snapshot_records_how_long_each_answer_took():
    """So the next person wondering whether this can move to the request path can read it."""
    src = open(cp.__file__.replace(".pyc", ".py")).read()
    assert '"seconds"' in src and "time.time() - t0" in src


def test_the_snapshot_is_written_atomically(tmp_path, monkeypatch):
    src = open(cp.__file__.replace(".pyc", ".py")).read()
    assert "os.replace(tmp, str(SNAPSHOT))" in src


def test_a_missing_or_broken_snapshot_does_not_break_the_page(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "SNAPSHOT", tmp_path / "nope.json")
    assert cp._load_snapshot() == {}
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    monkeypatch.setattr(cp, "SNAPSHOT", p)
    assert cp._load_snapshot() == {}


#  Measured on the real database on 2026-08-21. The projections are arithmetic on these.
FACTS = {"publications": 4_983_354, "chunks": 27_621_981}
SIZES = {"database": {"bytes": 293 * 1024 ** 3},
         "indexes": [{"idx": "ix_chunks_hnsw", "bytes": 94 * 1024 ** 3}],
         "extensions": {"vector": "0.8.5"}}


def test_the_projection_is_arithmetic_on_measured_ratios():
    pr = cp.projections(FACTS, SIZES)
    assert 5.4 < pr["chunks_per_pub"] < 5.7
    per_m = pr["per_million_publications"]
    #  5.54 chunks per publication, so a million publications is about 5.5 million chunks
    assert 5_400_000 < per_m["chunks"] < 5_700_000
    #  and the index grows by that many chunks times the measured bytes per chunk
    assert 17 < per_m["hnsw_gb"] < 21
    #  the ratios travel with the answer so the sum can be checked
    assert pr["hnsw_bytes_per_chunk"] > 0 and pr["db_bytes_per_pub"] > 0


def test_the_index_not_fitting_in_ram_is_stated_as_a_fact():
    """The one number the whole expansion question turns on."""
    pr = cp.projections(FACTS, SIZES)
    assert pr["index_fits_in_ram"] is False
    assert pr["hnsw_gb"] > pr["ram_gb"]
    assert pr["halfvec_hnsw_gb"] < pr["ram_gb"], (
        "halving the vectors has to be shown as the thing that makes it fit")


def test_halfvec_is_only_offered_when_pgvector_has_it():
    assert cp.projections(FACTS, SIZES)["halfvec_available"] is True
    old = dict(SIZES, extensions={"vector": "0.6.2"})
    assert cp.projections(FACTS, old)["halfvec_available"] is False


def test_the_assumed_input_is_named_and_adjustable():
    """Tokens per chunk is the one figure here that is assumed rather than measured, so it has to
    be visible and changeable rather than baked into a dollar number."""
    pr = cp.projections(FACTS, SIZES)
    assert pr["tokens_per_chunk"] and pr["usd_per_mtok"]
    assert pr["per_million_publications"]["embed_usd"] == round(
        pr["per_million_publications"]["embed_tokens"] / 1e6 * pr["usd_per_mtok"], 2)


def test_no_projection_without_the_numbers_behind_it():
    assert cp.projections({}, SIZES) == {}
    assert cp.projections({"publications": 0, "chunks": 0}, SIZES) == {}


def test_the_seed_branches_are_the_field_definition_not_the_ingest():
    """Widening the ingest is additive; moving the field definition re-points a calibrated router.
    They are separate constants for that reason and the page has to show both."""
    p = {"seed_cpc": [{"code": c} for c in cp.SEED_CPC], "ingest_cpc": list(cp.INGEST_CPC)}
    assert len(p["seed_cpc"]) == 8
    html = open("templates/corpus.html").read()
    assert "field definition" in html
    assert "ingest_is_seed" in html, "the page never says when the ingest is wider than the field"


def test_the_page_says_when_the_snapshot_was_taken():
    """A two-day-old count must not read as a live one.

    This used to require a `snapshot` chip on each of the four snapshot-derived numbers. The page
    is customer-facing now and four chips of our jargon in the middle of the figures is worse than
    one plain sentence, so the PROPERTY is asserted instead of that mechanism: the page names when
    the slow counts were taken, and says which figures are live so the reader can tell them apart.
    A chip on every row would satisfy this too.
    """
    html = open("templates/corpus.html").read()
    assert "snapshot_age_days" in html, "the page never says how old the counted figures are"
    assert "recomputed" in html
    #  and it distinguishes them from the ones read live, or "last recomputed" reads as if it
    #  applied to every number on the page
    assert "read live" in html or 'class="cc">snapshot<' in html, (
        "the page does not say which figures are live and which are counted periodically")
