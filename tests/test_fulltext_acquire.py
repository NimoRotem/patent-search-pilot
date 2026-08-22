"""Full-text acquisition: the cascade, the lease, the dedup, the budget and the parser.

Every provider is mocked. The database is the real one, but every row these tests create carries a
publication number in the ZZ office, which does not exist, and every test deletes its own rows by
EXACT publication number: a prefix or a LIKE in a cleanup is how a test takes real rows with it.

Where a guard is the point of the test, the test is defect injected: `test_budget_is_a_hard_cap`
fails if the cap is dropped from the reserving UPDATE, `test_complete_refuses_a_lost_lease` fails
if the lease check is dropped, and `test_worker_run_arms_the_corpus_guard` fails if `arm()` is
removed from `worker.run`.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from acquire import ledger, manifest, providers, tasks, worker

PUBS = [f"ZZ{n:07d}A" for n in range(1, 13)]
#  The pool is SHARED with the running production worker. Every lease in this file is
#  pinned to this manifest name so a test can never take, or be beaten to, a real row.
UNIT = ["unit"]
TEST_PERIOD = "1999-01"


def _cleanup():
    tasks.delete(PUBS)
    ledger.delete_events(PUBS)
    for p in ("zztest", "zztest2"):
        ledger.delete_budget(p, TEST_PERIOD)


@pytest.fixture(autouse=True)
def clean_pool():
    tasks.require_schema()
    _cleanup()
    yield
    _cleanup()


# ---------------------------------------------------------------------------------------------
# partitions: the thing that makes several workers safe
# ---------------------------------------------------------------------------------------------
def test_partition_is_stable_and_family_scoped():
    #  Stable across calls, and derived from the FAMILY, so siblings land on one worker.
    assert tasks.partition_of("12345678") == tasks.partition_of("12345678")
    assert 0 <= tasks.partition_of("12345678") < tasks.PARTITIONS
    #  md5, not hash(): PYTHONHASHSEED would move the partition between processes.
    import hashlib
    want = int.from_bytes(hashlib.md5(b"12345678").digest()[:2], "big") % tasks.PARTITIONS
    assert tasks.partition_of("12345678") == want


def test_partitions_for_are_disjoint_and_complete():
    for of in (1, 2, 3, 4):
        sets = [set(tasks.partitions_for(i, of)) for i in range(of)]
        union = set()
        for s in sets:
            assert not (s & union), "two workers would share a partition"
            union |= s
        assert union == set(range(tasks.PARTITIONS)), "a partition would never be worked"
    with pytest.raises(ValueError):
        tasks.partitions_for(2, 2)


def test_seed_dedups_by_publication_number():
    entries = [{"publication_number": PUBS[0], "family_id": "F1"},
               {"publication_number": PUBS[0], "family_id": "F1"},
               {"publication_number": PUBS[1], "family_id": "F1"}]
    first = tasks.seed(entries, manifest="unit")
    assert first["added"] == 2, first
    again = tasks.seed(entries, manifest="unit")
    assert again["added"] == 0, "re-seeding must not duplicate a publication"
    assert tasks.counts()["pending"] >= 2


def test_seed_puts_one_family_in_one_partition():
    tasks.seed([{"publication_number": p, "family_id": "FAM-42"} for p in PUBS[:5]],
               manifest="unit")
    import db
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute("SELECT DISTINCT partition_id FROM fulltext_fetch_task "
                    "WHERE publication_number = ANY(%s)", (PUBS[:5],))
        parts = [r["partition_id"] for r in cur.fetchall()]
    assert len(parts) == 1, f"a family was split across partitions {parts}"


# ---------------------------------------------------------------------------------------------
# leases
# ---------------------------------------------------------------------------------------------
def test_two_workers_never_lease_the_same_publication():
    tasks.seed([{"publication_number": p, "family_id": p} for p in PUBS[:8]], manifest="unit")
    parts = list(range(tasks.PARTITIONS))
    a = tasks.lease(parts, "worker-a", limit=8, manifests=UNIT)
    b = tasks.lease(parts, "worker-b", limit=8, manifests=UNIT)
    got_a = {r["publication_number"] for r in a}
    got_b = {r["publication_number"] for r in b}
    assert got_a & got_b == set(), "the same publication was leased twice"
    assert set(PUBS[:8]) <= (got_a | got_b)


def test_a_second_worker_does_not_wait_on_the_first_workers_row_locks():
    """SKIP LOCKED, and it has to be tested CONCURRENTLY.

    Two sequential leases in one process agree even without SKIP LOCKED, because the first has
    already committed `state='leased'`. The property SKIP LOCKED buys is that a worker whose scan
    reaches a row another worker is holding steps over it instead of blocking on it. Defect
    injection: drop SKIP LOCKED from tasks.lease and this test goes red on the elapsed time.
    """
    import threading
    import db
    tasks.seed([{"publication_number": p, "family_id": p} for p in PUBS[:8]], manifest="unit")
    parts = list(range(tasks.PARTITIONS))
    holding = threading.Event()
    done = threading.Event()

    def hold():
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """WITH picked AS (
                           SELECT publication_number FROM fulltext_fetch_task
                            WHERE state='pending' AND partition_id = ANY(%s)
                              AND manifest = 'unit'
                            ORDER BY priority, created_at FOR UPDATE SKIP LOCKED LIMIT 4)
                       UPDATE fulltext_fetch_task t SET state='leased', lease_owner='holder'
                         FROM picked p WHERE t.publication_number = p.publication_number""",
                    (parts,))
            holding.set()
            done.wait(4.0)
            conn.rollback()
        finally:
            conn.close()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert holding.wait(10), "the holding transaction never started"
    t0 = time.monotonic()
    got = tasks.lease(parts, "worker-b", limit=8, manifests=UNIT)
    elapsed = time.monotonic() - t0
    done.set()
    t.join(timeout=10)
    assert elapsed < 1.5, (f"the second worker waited {elapsed:.1f}s on the first worker's row "
                           f"locks: SKIP LOCKED is not doing its job")
    assert got, "the second worker got nothing at all"


def test_a_dead_workers_rows_come_back_to_the_pool():
    tasks.seed([{"publication_number": PUBS[0], "family_id": PUBS[0]}], manifest="unit")
    leased = tasks.lease(list(range(tasks.PARTITIONS)), "worker-dead", limit=1, manifests=UNIT)
    assert [r["publication_number"] for r in leased] == [PUBS[0]]
    #  The worker dies. Its lease expires.
    import db
    with db.cursor() as cur:
        cur.execute("UPDATE fulltext_fetch_task SET lease_expires_at = now() - interval '1 hour' "
                    "WHERE publication_number=%s", (PUBS[0],))
    out = tasks.reap()
    assert out["requeued"] >= 1, out
    assert tasks.lease(list(range(tasks.PARTITIONS)), "worker-b", limit=1,
                       manifests=UNIT)[0]["publication_number"] == PUBS[0]


def test_reap_retires_a_publication_that_keeps_killing_its_worker():
    tasks.seed([{"publication_number": PUBS[0], "family_id": PUBS[0]}], manifest="unit")
    import db
    tasks.lease(list(range(tasks.PARTITIONS)), "worker-dead", limit=1, manifests=UNIT)
    with db.cursor() as cur:
        cur.execute("UPDATE fulltext_fetch_task SET attempts=%s, "
                    "lease_expires_at = now() - interval '1 hour' WHERE publication_number=%s",
                    (tasks.MAX_ATTEMPTS, PUBS[0]))
    out = tasks.reap()
    assert out["failed"] >= 1, out
    import db as _db
    with _db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute("SELECT state, lease_owner FROM fulltext_fetch_task "
                    "WHERE publication_number=%s", (PUBS[0],))
        row = cur.fetchone()
    assert row["state"] == "failed" and row["lease_owner"] is None, row


def test_complete_refuses_a_lost_lease():
    """Defect injection: drop `AND lease_owner=%s` from tasks.complete and this goes green."""
    tasks.seed([{"publication_number": PUBS[0], "family_id": PUBS[0]}], manifest="unit")
    tasks.lease(list(range(tasks.PARTITIONS)), "worker-a", limit=1, manifests=UNIT)
    assert tasks.complete(PUBS[0], "worker-b", state="done", provider="x") is False
    assert tasks.complete(PUBS[0], "worker-a", state="done", provider="x") is True
    assert tasks.counts()["done"] >= 1


def test_renew_reports_the_rows_this_worker_still_owns():
    tasks.seed([{"publication_number": p, "family_id": p} for p in PUBS[:3]], manifest="unit")
    tasks.lease(list(range(tasks.PARTITIONS)), "worker-a", limit=3, manifests=UNIT)
    still = tasks.renew(PUBS[:3], "worker-a")
    assert set(still) == set(PUBS[:3])
    assert tasks.renew(PUBS[:3], "worker-b") == [], "renewing somebody else's lease must fail"


# ---------------------------------------------------------------------------------------------
# the budget: a cap that four workers cannot each spend
# ---------------------------------------------------------------------------------------------
def test_budget_is_a_hard_cap():
    """Defect injection: remove `AND spent + %s <= %s` from ledger.reserve and this goes red."""
    for i in range(5):
        got = ledger.reserve("zztest", 1.0, cap=5.0, period=TEST_PERIOD)
        assert got["granted"], f"reservation {i} should have been granted: {got}"
    refused = ledger.reserve("zztest", 1.0, cap=5.0, period=TEST_PERIOD)
    assert refused["granted"] is False
    assert refused["spent"] == 5.0 and refused["cap"] == 5.0


def test_budget_refuses_a_reservation_larger_than_the_room_left():
    ledger.reserve("zztest", 4.0, cap=5.0, period=TEST_PERIOD)
    assert ledger.reserve("zztest", 15.0, cap=5.0, period=TEST_PERIOD)["granted"] is False
    assert ledger.reserve("zztest", 1.0, cap=5.0, period=TEST_PERIOD)["granted"] is True


def test_budget_refund_puts_the_reservation_back():
    ledger.reserve("zztest2", 3.0, cap=3.0, period=TEST_PERIOD)
    assert ledger.reserve("zztest2", 1.0, cap=3.0, period=TEST_PERIOD)["granted"] is False
    ledger.refund("zztest2", 2.0, period=TEST_PERIOD)
    assert ledger.reserve("zztest2", 1.0, cap=3.0, period=TEST_PERIOD)["granted"] is True


# ---------------------------------------------------------------------------------------------
# the cascade
# ---------------------------------------------------------------------------------------------
class FakeProvider(providers.Provider):
    def __init__(self, name, *, result=None, exc=None, hang=False, covers=True,
                 available=(True, ""), credits=0.0, budget_key=""):
        self.name = name
        self.credits = credits
        self.budget_key = budget_key
        self.timeout = 0.3
        self.min_interval = 0.0
        self.concurrency = 4
        super().__init__()
        self._result, self._exc, self._hang = result, exc, hang
        self._covers, self._available = covers, available
        self.calls = 0

    def available(self):
        return self._available

    def covers(self, pub):
        return self._covers

    async def fetch(self, pub, client):
        self.calls += 1
        if self._hang:
            await asyncio.sleep(30)
        if self._exc:
            raise self._exc
        return self._result


def _full(provider="p", n=2000):
    return providers.FetchResult(provider=provider, description="x" * n, claims="c" * 400)


def _stub(provider="p"):
    return providers.FetchResult(provider=provider, description="short", claims="")


def _run_cascade(cascade, pub="ZZ0000001A"):
    w = worker.Worker(0, 1, cascade=cascade, dry_run=True)
    events: list = []
    task = {"publication_number": pub, "partition_id": 0, "manifest": "unit"}
    res = asyncio.run(w.cascade_for(pub, task, None, events))
    return res, events


def test_cascade_stops_at_the_first_complete_answer():
    a = FakeProvider("cheap", result=_full("cheap"))
    b = FakeProvider("expensive", result=_full("expensive"))
    res, events = _run_cascade([a, b])
    assert res.provider == "cheap"
    assert b.calls == 0, "an expensive rung was called after a cheap one had answered"
    assert [e["outcome"] for e in events] == ["hit"]


def test_cascade_falls_through_a_stub_answer():
    """A source that returns an abstract and calls it a document is a miss, not a hit: this is
    the exact failure that let a reference be 'read' from its title."""
    a = FakeProvider("stubby", result=_stub("stubby"))
    b = FakeProvider("real", result=_full("real"))
    res, events = _run_cascade([a, b])
    assert res.provider == "real"
    assert [e["outcome"] for e in events] == ["miss", "hit"]


def test_cascade_skips_a_provider_that_does_not_cover_the_jurisdiction():
    a = FakeProvider("us_only", result=_full("us_only"), covers=False)
    b = FakeProvider("worldwide", result=_full("worldwide"))
    res, _ = _run_cascade([a, b])
    assert a.calls == 0 and res.provider == "worldwide"


def test_cascade_records_an_unavailable_provider_rather_than_going_dark():
    a = FakeProvider("nokey", result=_full(), available=(False, "KEY not set"))
    b = FakeProvider("real", result=_full("real"))
    res, events = _run_cascade([a, b])
    assert a.calls == 0 and res.provider == "real"
    assert events[0]["outcome"] == "skipped" and "KEY not set" in events[0]["detail"]


def test_a_hanging_provider_does_not_stall_the_partition():
    """The rung's own timeout is ours, not the provider's promise."""
    a = FakeProvider("hangs", hang=True)
    b = FakeProvider("real", result=_full("real"))
    t0 = time.monotonic()
    res, events = _run_cascade([a, b])
    assert res.provider == "real"
    assert events[0]["outcome"] == "timeout"
    assert time.monotonic() - t0 < 5, "the hang was not bounded by the gate timeout"


def test_an_erroring_provider_is_a_miss_not_a_crash():
    a = FakeProvider("boom", exc=RuntimeError("upstream 500"))
    b = FakeProvider("real", result=_full("real"))
    res, events = _run_cascade([a, b])
    assert res.provider == "real"
    assert events[0]["outcome"] == "error" and "upstream 500" in events[0]["detail"]


def test_the_breaker_opens_after_consecutive_failures():
    a = FakeProvider("flaky", exc=RuntimeError("nope"))
    a.gate.breaker_after = 3
    for _ in range(3):
        _run_cascade([a])
    assert a.gate.open(), "the breaker never opened"
    before = a.calls
    _, events = _run_cascade([a])
    assert a.calls == before, "a rung with an open breaker was still called"
    assert events[0]["outcome"] == "breaker"


def test_a_settled_upstream_is_not_bought_twice():
    """The single most expensive defect this ledger has caught.

    serp_self, scrapingbee and serpapi all read the Google Patents index. When the free rung
    fetches the page and the page carries no claims and no description section, the two paid rungs
    fetch the identical page and produce the identical nothing, at 15 credits and $0.0092 a go.
    Measured on 2026-08-22 before this existed: 1,018 old FR / SE / GB / NL / AT documents cost
    15,480 ScrapingBee credits and the whole $4.58 SerpApi budget confirming what the free rung
    had already established.

    Defect injection: delete the `settled` set from Worker.cascade_for and this goes red.
    """
    free = FakeProvider("serp_self", result=providers.FetchResult(provider="serp_self",
                                                                  reached=True))
    free.upstream = "google_patents"
    paid = FakeProvider("scrapingbee", result=_full("scrapingbee"), credits=15.0)
    paid.upstream = "google_patents"
    other = FakeProvider("himmpat", result=_full("himmpat"))
    res, events = _run_cascade([free, paid, other])
    assert paid.calls == 0, "the paid rung bought a page the free rung had already read as empty"
    assert res.provider == "himmpat", "a rung on a DIFFERENT upstream must still be tried"
    outcomes = {e["provider"]: e["outcome"] for e in events}
    assert outcomes["serp_self"] == "miss"
    assert outcomes["scrapingbee"] == "settled"


def test_an_unreached_upstream_still_falls_through_to_the_paid_rung():
    """The other half of the rule, and the reason `reached` is a flag rather than an assumption.

    Google answering 404 and Google answering 503 look identical to a caller that only asks "did
    I get a document". They are opposites. 404 is Google saying it does not hold the publication,
    which settles it for the two rungs that resell the same index. 503 is Google refusing us,
    which is exactly the outage the paid rungs exist for. Defect injection: drop `res.reached`
    from the settling condition in Worker.cascade_for and this goes red.
    """
    refused = FakeProvider("serp_self",
                           result=providers.FetchResult(provider="serp_self", reached=False))
    refused.upstream = "google_patents"
    paid = FakeProvider("scrapingbee", result=_full("scrapingbee"), credits=15.0)
    paid.upstream = "google_patents"
    res, _ = _run_cascade([refused, paid])
    assert paid.calls == 1 and res.provider == "scrapingbee"

    #  and a rung that raised outright settles nothing either
    boom = FakeProvider("serp_self", exc=RuntimeError("HTTP 503 from patents.google.com"))
    boom.upstream = "google_patents"
    paid2 = FakeProvider("scrapingbee", result=_full("scrapingbee"), credits=15.0)
    paid2.upstream = "google_patents"
    res, _ = _run_cascade([boom, paid2])
    assert paid2.calls == 1 and res.provider == "scrapingbee"


def test_a_spent_budget_skips_the_rung_without_calling_it(monkeypatch):
    paid = FakeProvider("paid", result=_full("paid"), credits=1.0, budget_key="zztest")
    free = FakeProvider("free", result=_full("free"))
    monkeypatch.setitem(providers.DEFAULT_CAPS, "zztest", 1.0)
    monkeypatch.setattr(ledger, "month_period", lambda when=None: TEST_PERIOD)
    res, events = _run_cascade([paid, free])
    assert res.provider == "paid" and paid.calls == 1
    res, events = _run_cascade([paid, free])
    assert res.provider == "free", "the cap did not bind on the second publication"
    assert paid.calls == 1, "a rung was called after its budget was spent"
    assert events[0]["outcome"] == "budget"


def test_a_timeout_refunds_its_reservation(monkeypatch):
    paid = FakeProvider("paid", hang=True, credits=1.0, budget_key="zztest")
    monkeypatch.setitem(providers.DEFAULT_CAPS, "zztest", 1.0)
    monkeypatch.setattr(ledger, "month_period", lambda when=None: TEST_PERIOD)
    _run_cascade([paid])
    state = {b["provider"]: b for b in ledger.budget_state(period=TEST_PERIOD)}
    assert state["zztest"]["spent"] == 0.0, "a call that never happened kept its credit"


# ---------------------------------------------------------------------------------------------
# storage: only the three permitted places
# ---------------------------------------------------------------------------------------------
def test_store_writes_the_docstore_and_the_ingest_queue(monkeypatch):
    from sources import docstore
    import runstore
    put, queued = {}, {}
    monkeypatch.setattr(docstore, "_put_sync", lambda pn, rec: put.update({pn: rec}))
    monkeypatch.setattr(runstore, "queue_for_ingest",
                        lambda pn, **kw: queued.update({pn: kw}) or {"id": 1})
    monkeypatch.setattr(worker.blobstore, "enabled", lambda: False)
    w = worker.Worker(0, 1, cascade=[])
    events: list = []
    res = _full("epo_ops")
    asyncio.run(w.store(PUBS[0], res, None, events,
                        {"partition_id": 0, "manifest": "unit"}))
    assert put[PUBS[0]]["description"] == res.description
    assert queued[PUBS[0]]["scratch_ref"] == f"sources_docstore:{PUBS[0]}"
    assert queued[PUBS[0]]["source"] == "epo_ops"


def test_bulk_demand_queues_behind_a_live_search_request(monkeypatch):
    """Defect injection: put FULLTEXT_INGEST_PRIORITY back to 80 and this goes red.

    `runstore.pending_ingest(limit=N)` returns the top N by priority, and a search-time request
    takes corpus_ingest_queue's default of 100. At 80 this fetcher put tens of thousands of rows
    in front of every live request and pushed them out of the window, which is exactly how it
    broke test_durable_runs.py::test_repeat_demand_for_one_publication_bumps_the_count.
    """
    from sources import docstore
    import runstore
    captured = {}
    monkeypatch.setattr(docstore, "_put_sync", lambda pn, rec: None)
    monkeypatch.setattr(runstore, "queue_for_ingest",
                        lambda pn, **kw: captured.update(kw) or {"id": 1})
    monkeypatch.setattr(worker.blobstore, "enabled", lambda: False)
    w = worker.Worker(0, 1, cascade=[])
    asyncio.run(w.store(PUBS[0], _full("x"), None, [],
                        {"partition_id": 0, "manifest": "unit"}))
    assert captured["priority"] > 100, (
        "bulk niche acquisition must queue BEHIND search-time demand")


def test_a_gcs_failure_does_not_lose_the_text(monkeypatch):
    from sources import docstore
    import runstore
    put = {}
    monkeypatch.setattr(docstore, "_put_sync", lambda pn, rec: put.update({pn: rec}))
    monkeypatch.setattr(runstore, "queue_for_ingest", lambda pn, **kw: {"id": 1})
    monkeypatch.setattr(worker.blobstore, "enabled", lambda: True)

    async def boom(*a, **k):
        raise RuntimeError("bucket on fire")
    monkeypatch.setattr(worker.blobstore, "put_raw", boom)
    monkeypatch.setattr(worker.blobstore, "put_parsed", boom)
    w = worker.Worker(0, 1, cascade=[])
    events: list = []
    uris = asyncio.run(w.store(PUBS[0], _full("epo_ops"), None, events,
                               {"partition_id": 0, "manifest": "unit"}))
    assert uris == {"raw_uri": "", "parsed_uri": ""}
    assert put, "the text was lost because GCS was unhappy"
    assert any(e["provider"] == "gcs" and e["outcome"] == "error" for e in events)


# ---------------------------------------------------------------------------------------------
# the corpus write prohibition
# ---------------------------------------------------------------------------------------------
def test_worker_run_arms_the_corpus_guard(monkeypatch):
    """Defect injection: delete `corpus_guard.arm(...)` from worker.run and this goes red."""
    import corpus_guard
    import db
    seen = []

    async def fake_run(self, **kw):
        seen.append(corpus_guard.armed())
        with db.cursor(autocommit=True) as cur:
            with pytest.raises(corpus_guard.CorpusWriteBlocked):
                cur.execute("INSERT INTO chunks (publication_id, text) VALUES (-1, 'x')")
            with pytest.raises(corpus_guard.CorpusWriteBlocked):
                cur.execute("UPDATE publications SET abstract='x' WHERE id=-1")
        return {}

    monkeypatch.setattr(worker.Worker, "run", fake_run)
    monkeypatch.setattr(worker.providers, "build", lambda *a, **k: [])
    try:
        worker.run(0, 1)
    finally:
        corpus_guard.disarm()
    assert seen == [True]


def test_the_docstore_and_the_ingest_queue_are_not_protected_tables():
    """The permitted destinations must stay writable under the ARMED guard, or the worker would
    refuse its own output. `check()` returns early when the process is not armed, so this has to
    arm to mean anything."""
    import corpus_guard
    corpus_guard.arm("test: the permitted destinations")
    try:
        for t in ("sources_docstore", "corpus_ingest_queue", "fulltext_fetch_task",
                  "fulltext_fetch_event", "fulltext_budget"):
            assert t not in corpus_guard.PROTECTED_TABLES
            corpus_guard.check(f"INSERT INTO {t} (x) VALUES (1)")
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            corpus_guard.check("INSERT INTO chunks (publication_id) VALUES (1)")
    finally:
        corpus_guard.disarm()


# ---------------------------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------------------------
ST36 = b"""<?xml version="1.0"?>
<patent-document xmlns="http://www.wipo.int/standards/XMLSchema/ST36">
  <bibliographic-data>
    <invention-title lang="EN">Vacuum lifting device with a sealing lip</invention-title>
  </bibliographic-data>
  <abstract><p>A suction cup with a flexible sealing lip.</p></abstract>
  <description>
    <p>The invention relates to a vacuum lifting device.</p>
    <p>A pump generates a partial vacuum in the chamber.</p>
  </description>
  <claims>
    <claim><claim-text>1. A vacuum lifting device comprising a suction cup.</claim-text></claim>
    <claim><claim-text>2. The device of claim 1, further comprising a pump.</claim-text></claim>
  </claims>
</patent-document>"""


def test_parse_st36_extracts_title_claims_and_description():
    out = providers.parse_st36(ST36)
    assert out["title"] == "Vacuum lifting device with a sealing lip"
    assert "suction cup" in out["claims"] and "further comprising a pump" in out["claims"]
    assert out["claims"].count("\n") == 1, "claims must stay one per line"
    assert out["description"].count("\n") == 1, "description paragraphs must stay separate"
    assert "partial vacuum" in out["description"]
    assert "sealing lip" in out["abstract"]


def test_parse_st36_ignores_the_namespace():
    """The same office changes its namespace between DTD versions. A namespace-exact match is how
    a parser silently returns nothing for a whole decade of documents."""
    other = ST36.replace(b"http://www.wipo.int/standards/XMLSchema/ST36", b"urn:us:gov:doc:uspto")
    assert providers.parse_st36(other)["claims"] == providers.parse_st36(ST36)["claims"]


def test_parse_st36_survives_rubbish():
    assert providers.parse_st36(b"not xml at all") == {}
    assert providers.parse_st36(b"") == {}


def test_bulk_xml_provider_is_inert_without_a_mirror(monkeypatch):
    monkeypatch.delenv("MAREC_ROOT", raising=False)
    p = providers.BulkXmlProvider("marec", "MAREC_ROOT", countries=("EP", "WO"))
    ok, why = p.available()
    assert ok is False and "MAREC_ROOT" in why


def test_bulk_xml_provider_reads_a_local_mirror(tmp_path, monkeypatch):
    (tmp_path / "EP").mkdir()
    (tmp_path / "EP" / "EP1234567A1.xml").write_bytes(ST36)
    monkeypatch.setenv("MAREC_ROOT", str(tmp_path))
    p = providers.BulkXmlProvider("marec", "MAREC_ROOT", countries=("EP", "WO"))
    assert p.available()[0] is True
    assert p.covers("EP1234567A1") and not p.covers("US1234567B2")
    res = asyncio.run(p.fetch("EP1234567A1", None))
    assert res is not None and "suction cup" in res.claims
    assert res.raw_ext == "xml" and res.raw == ST36


# ---------------------------------------------------------------------------------------------
# the manifest seam
# ---------------------------------------------------------------------------------------------
def test_jsonl_manifest_reader_is_incremental(tmp_path):
    f = tmp_path / "niche.jsonl"
    f.write_text("\n".join(json.dumps({"publication_number": p, "family_id": f"F{i}"})
                           for i, p in enumerate(PUBS[:6])))
    r = manifest.JsonlManifestReader(str(f))
    a, cur, done = r.read("", 4)
    assert [e.publication_number for e in a] == PUBS[:4] and not done
    b, cur, done = r.read(cur, 4)
    assert [e.publication_number for e in b] == PUBS[4:6] and done


def test_jsonl_manifest_reader_accepts_the_other_field_names():
    e = manifest.JsonlManifestReader.entry_from(
        {"pub": "US-9,876,543-B2", "simple_family_id": 77, "priority": 5}, "m")
    assert e.publication_number == "US9876543B2" and e.family_id == "77" and e.priority == 5
    assert manifest.JsonlManifestReader.entry_from({"nothing": 1}, "m") is None


def test_open_reader_defaults_to_the_provisional_corpus_niche_reader():
    assert isinstance(manifest.open_reader("corpus-niche"), manifest.CorpusNicheReader)
    assert isinstance(manifest.open_reader("/tmp/whatever.jsonl"), manifest.JsonlManifestReader)


def test_corpus_niche_reader_returns_real_starved_publications():
    """Not a mock: the provisional manifest must actually name publications this corpus holds
    with no text at all, or the worker would start with nothing to do."""
    r = manifest.CorpusNicheReader()
    entries, cursor, exhausted = r.read("", 25)
    assert entries, "the provisional manifest is empty"
    assert not exhausted
    assert all(e.publication_number and e.publication_number[:2].isalpha() for e in entries)
    assert all(e.priority in (manifest.PRIORITY_NO_SIBLING_TEXT,
                              manifest.PRIORITY_HAS_SIBLING_TEXT) for e in entries)
    #  Every one of them must genuinely hold no text: a manifest that names publications the
    #  corpus already has is a fetcher spending its budget on work already done. Defect
    #  injection: drop either NOT EXISTS from the reader and this goes red.
    import db
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute(
            """SELECT count(*) n FROM publications p
                WHERE upper(regexp_replace(p.publication_number,'[^A-Za-z0-9]','','g'))
                      = ANY(%s)
                  AND (EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=p.id)
                    OR EXISTS (SELECT 1 FROM paragraphs g WHERE g.publication_id=p.id))""",
            ([e.publication_number for e in entries],))
        already_texted = int(cur.fetchone()["n"])
    assert already_texted == 0, (
        f"{already_texted} of {len(entries)} manifest entries already hold text")
    nxt, _, _ = r.read(cursor, 5)
    assert {e.publication_number for e in nxt} & {e.publication_number for e in entries} == set()


# ---------------------------------------------------------------------------------------------
# the corpus rung, against the real corpus
# ---------------------------------------------------------------------------------------------
def test_corpus_provider_answers_from_a_family_sibling():
    """22,099 of the 52,176 starved niche publications have a texted family sibling. This is the
    rung that makes those free."""
    import db
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute(
            """SELECT p.publication_number FROM publications p
                WHERE p.simple_family_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=p.id)
                  AND NOT EXISTS (SELECT 1 FROM paragraphs g WHERE g.publication_id=p.id)
                  AND EXISTS (SELECT 1 FROM publications q JOIN claims c2 ON c2.publication_id=q.id
                               WHERE q.simple_family_id=p.simple_family_id AND q.id<>p.id)
                LIMIT 1""")
        row = cur.fetchone()
    if row is None:
        pytest.skip("no starved publication with a texted sibling in this corpus")
    from sources.schema import canonical_pub
    pub = canonical_pub(row["publication_number"])
    p = providers.CorpusProvider(allow_family_donor=True)
    res = asyncio.run(p.fetch(pub, None))
    assert res is not None, f"the corpus rung missed {pub}"
    assert res.provider == "corpus:family"
    assert res.meta.get("donor_publication"), "a donor's text must name the donor"
    assert res.claims or res.description


def test_corpus_provider_can_be_told_not_to_borrow_from_the_family(monkeypatch):
    p = providers.CorpusProvider(allow_family_donor=False)
    assert p.allow_family_donor is False
    assert p._lookup("ZZ9999999A") is None


# ---------------------------------------------------------------------------------------------
# end to end, with every provider mocked
# ---------------------------------------------------------------------------------------------
def test_worker_leases_fetches_stores_and_marks_done(monkeypatch):
    from sources import docstore
    import runstore
    stored = {}
    monkeypatch.setattr(docstore, "_put_sync", lambda pn, rec: stored.update({pn: rec}))
    monkeypatch.setattr(runstore, "queue_for_ingest", lambda pn, **kw: {"id": 1})
    monkeypatch.setattr(worker.blobstore, "enabled", lambda: False)
    monkeypatch.setattr(worker, "BATCH", 3)

    #  ONLY our own rows. The pool is SHARED with the running production worker, so two things
    #  keep this hermetic in both directions: the test worker is pinned to manifest='unit', and
    #  the fixture rows are seeded at priority 100000, behind every real row, so production will
    #  not reach them before the test has deleted them.
    tasks.seed([{"publication_number": p, "family_id": p, "priority": 100000}
                for p in PUBS[:3]], manifest="unit")
    parts = sorted({tasks.partition_of(p) for p in PUBS[:3]})
    w = worker.Worker(0, 1, cascade=[FakeProvider("fake", result=_full("fake"))],
                      partitions=parts, manifests=["unit"])
    out = asyncio.run(w.run(max_publications=3, max_batches=1))
    assert out["fetched"] == 3, out
    import db
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute("SELECT publication_number, state, provider, desc_chars "
                    "FROM fulltext_fetch_task WHERE publication_number = ANY(%s)", (PUBS[:3],))
        rows = {r["publication_number"]: r for r in cur.fetchall()}
    assert len(rows) == 3
    assert all(r["state"] == "done" and r["provider"] == "fake" and r["desc_chars"] == 2000
               for r in rows.values()), rows
    assert set(stored) == set(PUBS[:3])


def test_a_finished_publication_leaves_the_held_set(monkeypatch):
    """Defect injection: delete `self.held.discard(pub)` from Worker.handle and this goes red.

    A finished row left in `held` is renewed by the next heartbeat, comes back absent because
    `renew()` only matches `state='leased'`, and is logged as a LOST LEASE. That reads as a
    stalled worker to an operator and it is nothing of the kind: it was observed on both
    production shards at 15:29:51 on 2026-08-22, on rows that had completed normally.
    """
    from sources import docstore
    import runstore
    monkeypatch.setattr(docstore, "_put_sync", lambda pn, rec: None)
    monkeypatch.setattr(runstore, "queue_for_ingest", lambda pn, **kw: {"id": 1})
    monkeypatch.setattr(worker.blobstore, "enabled", lambda: False)

    tasks.seed([{"publication_number": PUBS[0], "family_id": PUBS[0], "priority": 100000}],
               manifest="unit")
    w = worker.Worker(0, 1, cascade=[FakeProvider("fake", result=_full("fake"))],
                      manifests=UNIT)
    leased = tasks.lease(list(range(tasks.PARTITIONS)), w.id, limit=1, manifests=UNIT)
    assert [r["publication_number"] for r in leased] == [PUBS[0]]
    w.held = {PUBS[0]}
    asyncio.run(w.handle(leased[0], None, []))
    assert w.held == set(), "a completed publication was left in the held set"
    #  and the heartbeat's own view agrees: the row is done, so renew reports nothing.
    assert tasks.renew([PUBS[0]], w.id) == []


def test_progress_reports_the_pool_and_the_spend():
    p = ledger.progress(minutes=5)
    assert set(p) >= {"pool", "providers", "credits_total", "usd_total", "hits_per_hour",
                      "budgets"}
    assert isinstance(p["pool"], dict)
