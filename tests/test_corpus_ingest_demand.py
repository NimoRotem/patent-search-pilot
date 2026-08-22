"""The demand loop's guard: a claim must not be able to take rows it was not asking for.

WHY THIS FILE EXISTS. `runstore.claim_ingest()` used to take no filter, so
`claim_ingest(limit=50)` claimed the top fifty PENDING rows of whatever database the test process
was pointed at, which is the live one. `tests/test_durable_runs.py` called exactly that and then
cleaned up only the single row it had created. MEASURED 2026-08-22 on the live queue: 549 rows in
state='claimed', `corpus_release=''`, `ingested_at IS NULL`, with no release process running
anywhere. Eleven runs of that file, fifty rows each, less the one row it did delete.

Those rows are invisible to `pending_ingest`, so they are invisible to the release builder, so the
demand signal the whole loop is supposed to rank by was being consumed by the test suite.

These tests run against the real Postgres like the rest of the suite and delete what they make.
"""
import uuid

import pytest

import runstore
from corpus import demand


def _pub():
    return f"US-{uuid.uuid4().hex[:10].upper()}-A1"


def _cleanup(pubs):
    import db
    with db.cursor() as cur:
        cur.execute("DELETE FROM corpus_ingest_queue WHERE publication_number = ANY(%s)",
                    (list(pubs),))


def test_an_unscoped_claim_is_refused():
    """The defect, stated as a test. Remove the guard in `claim_ingest` and this goes green while
    the queue quietly loses fifty rows."""
    with pytest.raises(runstore.UnscopedClaim) as exc:
        runstore.claim_ingest(limit=50)
    assert "scoped" in str(exc.value)


def test_a_scoped_claim_leaves_its_neighbours_pending():
    """The consequence the guard exists to prevent, measured directly: a claim for one publication
    must not remove a second, higher-priority one from `pending_ingest`."""
    mine, neighbour = _pub(), _pub()
    try:
        #  The neighbour outranks `mine` on every ordering key `claim_ingest` uses, so an unscoped
        #  claim would take it first. That is the point: it stands in for the fetcher's rows.
        runstore.queue_for_ingest(neighbour, reason="fetcher", priority=1)
        runstore.queue_for_ingest(mine, reason="test", priority=100)

        claimed = runstore.claim_ingest(limit=50, publication_numbers=[mine])
        assert [r["publication_number"] for r in claimed] == [mine]

        still = {r["publication_number"] for r in runstore.pending_ingest(limit=5000)}
        assert neighbour in still, "a scoped claim took a row it was not asking for"
        assert mine not in still
    finally:
        _cleanup([mine, neighbour])


def test_a_named_claimant_is_recorded_so_a_stale_claim_is_attributable():
    pub = _pub()
    try:
        runstore.queue_for_ingest(pub, reason="test")
        rows = runstore.claim_ingest(limit=1, publication_numbers=[pub],
                                     claimant="corpus-release-builder")
        assert rows and rows[0]["note"] == "corpus-release-builder"
        assert rows[0]["state"] == "claimed"
    finally:
        _cleanup([pub])


def test_a_claim_can_be_given_back():
    """A build that dies between claiming and sealing must not eat the request for ever."""
    pub = _pub()
    try:
        runstore.queue_for_ingest(pub, reason="test")
        assert runstore.claim_ingest(limit=1, publication_numbers=[pub])
        assert runstore.release_ingest([pub]) == 1
        assert any(r["publication_number"] == pub
                   for r in runstore.pending_ingest(limit=5000))
    finally:
        _cleanup([pub])


def test_release_ingest_also_refuses_to_run_unscoped():
    with pytest.raises(runstore.UnscopedClaim):
        runstore.release_ingest()


def test_the_reaper_refuses_to_sweep_rows_it_was_not_given():
    """Found by writing this file: the first draft called `reap_ingest_claims(3600)` with no
    filter and reset every abandoned claim on the live queue. Sweeping is an operator action."""
    with pytest.raises(runstore.UnscopedClaim):
        runstore.reap_ingest_claims(older_than_seconds=3600)
    assert runstore.reap_ingest_claims(older_than_seconds=3600, publication_numbers=[]) == []


def test_reaping_returns_abandoned_claims_and_leaves_ingested_ones_alone():
    """`ingested_at IS NULL AND corpus_release = ''` is the test for 'never actually released'.
    A row that reached a release must survive the reaper, or activation would re-request it."""
    abandoned, done = _pub(), _pub()
    try:
        for pn in (abandoned, done):
            runstore.queue_for_ingest(pn, reason="test")
            assert runstore.claim_ingest(limit=1, publication_numbers=[pn], claimant="ghost")
        runstore.mark_ingested([done], corpus_release="hot_v1")

        import db
        with db.cursor() as cur:
            cur.execute("UPDATE corpus_ingest_queue SET last_requested_at = now() - "
                        "interval '48 hours' WHERE publication_number = ANY(%s)",
                        ([abandoned, done],))

        reaped = set(runstore.reap_ingest_claims(older_than_seconds=3600,
                                                 publication_numbers=[abandoned, done]))
        assert abandoned in reaped
        assert done not in reaped, "a released row must not be re-queued by the reaper"

        pending = {r["publication_number"] for r in runstore.pending_ingest(limit=5000)}
        assert abandoned in pending and done not in pending
    finally:
        _cleanup([abandoned, done])


def test_demand_claim_names_itself_by_default():
    """`corpus.demand.claim` is the release builder's door onto the queue. It must not be able to
    make the anonymous claim `runstore` now refuses."""
    calls = {}

    class FakeRunstore:
        def claim_ingest(self, limit=100, **kw):
            calls.update({"limit": limit, **kw})
            return []

    demand.claim(limit=7, runstore=FakeRunstore())
    assert calls["claimant"] == demand.CLAIMANT
    assert calls["limit"] == 7
