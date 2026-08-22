"""CJK acquisition: HimmPat barred from bulk, and the BigQuery rung that replaces the plan for it.

Two things are proved here and both are guards, so both are defect injected: the test that says
the guard works is followed by a test that removes the guard and asserts the call goes through.
A green test beside a guard that was never exercised is worth nothing.

    1. HIMMPAT IS FOR A LIVE SEARCH ONLY. 250 units a day, about 48 for one deep search, so the
       whole allowance is roughly five searches. `src/realtime_only.py` defaults to DENY and
       refuses at the adapter's own HTTP boundary, and `acquire.providers.BARRED` refuses to put
       a rung on it into any cascade whatever `FULLTEXT_CASCADE` says. Two independent doors.

    2. `bq_cjk` IS WHAT THE BULK PLAN ROUTES CJK ABSTRACTS TO. It reads a clustered cache of
       `patents-public-data` and it can never satisfy the completeness floor, because BigQuery
       holds no CJK claims or description at all (measured 2026-08-22 across every snapshot from
       201710 to 202511). BigQuery is mocked in every test in this file; `ops/bq_cjk_cache.py
       probe` is the unmocked version and it costs about a tenth of a cent.

No test here makes a network call, spends a HimmPat unit or queries BigQuery.
"""
from __future__ import annotations

import asyncio

import pytest

import realtime_only
from acquire import providers, tasks, worker

PUBS = [f"ZZ{n:07d}A" for n in range(90, 96)]
#  A manifest name of this file's own. The pool is shared with the production worker AND with
#  `tests/test_fulltext_acquire.py`, which leases on manifest 'unit'; `pt` runs three suites at
#  once, so a shared manifest name is a shared work list.
MANIFEST = "unit-cjk"
UNIT = [MANIFEST]


@pytest.fixture(autouse=True)
def denied_by_default():
    """Every test starts in the default state: this process is NOT a live search."""
    realtime_only.disable()
    yield
    realtime_only.disable()


@pytest.fixture(autouse=True)
def clean_pool():
    from acquire import ledger
    tasks.require_schema()
    tasks.delete(PUBS)
    ledger.delete_events(PUBS)
    yield
    tasks.delete(PUBS)
    ledger.delete_events(PUBS)


# =============================================================================================
# 1. HimmPat is barred from bulk
# =============================================================================================
def test_himmpat_is_not_a_rung_of_the_default_cascade():
    assert "himmpat" not in providers.DEFAULT_ORDER
    assert "himmpat" not in providers.DEFAULT_CAPS, \
        "a cap on a rung that must not exist reads as permission to spend it"
    assert not any(p.name == "himmpat" for p in providers.build())


def test_build_refuses_a_himmpat_rung_even_when_an_operator_asks_for_one(monkeypatch):
    """`FULLTEXT_CASCADE` is the documented way to change the cascade without a deploy, so
    dropping the name from DEFAULT_ORDER on its own would leave the rung one env var away.

    Defect injection: delete the `if n in BARRED` clause from `providers.build` and this goes red.
    """
    monkeypatch.setenv("FULLTEXT_CASCADE", "corpus,himmpat,serpapi")
    with pytest.raises(ValueError) as exc:
        providers.build()
    assert "himmpat" in str(exc.value)
    assert "real-time" in str(exc.value)
    #  and naming it directly is refused too, not just via the environment
    with pytest.raises(ValueError):
        providers.build(["himmpat"])


class _Transport:
    """Counts the HTTP calls that reach it. Nothing here talks to himmpat.com."""

    def __init__(self):
        self.calls = 0

    async def post(self, url, **kw):
        self.calls += 1
        raise AssertionError("a HimmPat HTTP call was made from a bulk process")


def _himmpat_adapter(monkeypatch=None):
    """A HimmPat adapter with a fake key and its SPEND LEDGER STUBBED OUT.

    `_affordable` reads `~/.patents/himmpat_usage.json`, which is the real rolling ledger the
    production key is charged against. A test whose result depended on how many units a live
    search spent this afternoon is not a test.
    """
    from sources import himmpat as H
    a = H.HimmPat()
    a.key = "test-key-not-real"
    if monkeypatch is not None:
        monkeypatch.setattr(H, "_affordable", lambda units: True)
        monkeypatch.setattr(H, "MIN_INTERVAL", 0.0)
    return a


def _post(adapter, transport):
    return asyncio.run(adapter._post(transport, "https://example.invalid/x", {}, 1))


def test_a_bulk_process_cannot_reach_the_himmpat_http_boundary():
    """The guard is at `_post`, the ONE boundary every billed HimmPat call passes through, so a
    bulk path written next year is refused without having to know this module exists."""
    transport = _Transport()
    with pytest.raises(realtime_only.BulkUseBlocked) as exc:
        _post(_himmpat_adapter(), transport)
    assert transport.calls == 0, "the call reached the network before the guard"
    assert "real-time" in str(exc.value)


def test_defect_injection_without_the_guard_the_same_bulk_call_goes_through(monkeypatch):
    """THE PROOF THAT THE TEST ABOVE MEANS SOMETHING.

    Remove the check and the identical call now reaches the transport, which raises to say so.
    If this test ever passes with the guard in place, the guard is not on the call path.
    """
    adapter = _himmpat_adapter(monkeypatch)
    monkeypatch.setattr(realtime_only, "check", lambda provider: None)
    transport = _Transport()
    with pytest.raises(AssertionError) as exc:
        _post(adapter, transport)
    assert "bulk process" in str(exc.value)
    assert transport.calls == 1, "the guarded path is not the path the call takes"


def test_a_live_search_process_may_still_call_himmpat(monkeypatch):
    """The other half of the rule. Barring it from bulk must not disable it for a search."""
    adapter = _himmpat_adapter(monkeypatch)
    realtime_only.enable("unit test: live search")
    transport = _Transport()
    with pytest.raises(AssertionError):
        _post(adapter, transport)
    assert transport.calls == 1, "a live search was refused the provider reserved for it"


def test_the_adapter_reports_the_bar_rather_than_looking_broken():
    a = _himmpat_adapter()
    assert a.enabled() is False
    assert "real-time" in a.disabled_reason()
    realtime_only.enable("unit test: live search")
    assert a.enabled() is True
    #  Whatever it says now is about the key or the vendor ledger, not about this guard.
    assert "real-time" not in a.disabled_reason()


def test_the_niche_manifest_routes_cjk_to_google_not_to_himmpat():
    """`best_source` produced the "900,463 families, about 3,600 days" figure by naming a rung
    the cascade never asks first. Measured on the live pool 2026-08-22, Google Patents answered
    9,359 of 9,360 CN publications; HimmPat answered 45 in the whole run."""
    import corpus_niche
    assert "himmpat" not in [name for name, _ in corpus_niche.SOURCE_LADDER]
    assert corpus_niche.best_source(["CN"], False) == "gpatents_direct"
    assert corpus_niche.best_source(["JP", "KR"], False) == "gpatents_direct"
    assert corpus_niche.best_source(["CN", "US"], False) == "pqai"
    assert corpus_niche.best_source(["CN", "EP"], False) == "epo_ops"


# =============================================================================================
# 2. the BigQuery CJK rung
# =============================================================================================
class _FakeJob:
    def __init__(self, rows, billed):
        self._rows, self.total_bytes_billed = rows, billed

    def result(self):
        return list(self._rows)


class _FakeBq:
    """Stands in for `google.cloud.bigquery.Client`. Records every query it is asked to run."""

    def __init__(self, table_rows, billed=10_000_000):
        self.rows = dict(table_rows)
        self.billed = billed
        self.queries: list = []

    def query(self, sql, job_config=None):
        keys = list(job_config.query_parameters[0].values)
        self.queries.append(keys)
        return _FakeJob([self.rows[k] for k in keys if k in self.rows], self.billed)


def _row(pn_key, country="CN", title="A gripper", abstract="x" * 300, src="zh"):
    return {"pn_key": pn_key, "publication_number": pn_key, "country": country,
            "title_en": title, "abstract_en": abstract, "src_lang": src}


def _bq_provider(rows, billed=10_000_000):
    p = providers.BigQueryCjkProvider(table="t")
    fake = _FakeBq({r["pn_key"]: r for r in rows}, billed)
    p._client = fake
    return p, fake


def test_bq_cjk_covers_only_the_four_cjk_offices():
    p, _ = _bq_provider([])
    assert [p.covers(x) for x in ("CN1A", "JP2A", "KR3A", "TW4A")] == [True] * 4
    assert [p.covers(x) for x in ("US1A", "EP1A", "WO1A", "DE1A")] == [False] * 4


def test_bq_cjk_normalises_the_publication_number_both_ways():
    """BigQuery publishes `CN-101234567-A`; the fetch pool holds `CN101234567A`. One key."""
    k = providers.BigQueryCjkProvider._key
    assert k("CN-101234567-A") == k("cn101234567a") == "CN101234567A"
    assert k("JP-S59163237-A") == "JPS59163237A"


def test_bq_cjk_prefetches_a_whole_batch_in_one_query():
    """BigQuery bills a 10 MB minimum per query however few keys it carries, so 24 point lookups
    cost 24 minimums for the answer one 24-key lookup gives.

    Defect injection: make `prefetch` a no-op and this goes red on the query count, because every
    `fetch` then falls back to warming itself one key at a time.
    """
    pubs = [f"CN{n}A" for n in range(24)]
    p, fake = _bq_provider([_row(f"CN{n}A") for n in range(24)])
    asyncio.run(p.prefetch(pubs, None))
    assert len(fake.queries) == 1, fake.queries
    assert sorted(fake.queries[0]) == sorted(pubs)
    for pub in pubs:
        asyncio.run(p.fetch(pub, None))
    assert len(fake.queries) == 1, "a warmed batch went back to BigQuery per publication"


def test_bq_cjk_caches_an_absent_key_so_a_retry_does_not_buy_the_same_nothing():
    p, fake = _bq_provider([])
    asyncio.run(p.prefetch(["CN1A"], None))
    res = asyncio.run(p.fetch("CN1A", None))
    assert res.reached is True and not res.title and not res.abstract
    asyncio.run(p.prefetch(["CN1A"], None))
    assert len(fake.queries) == 1, "an absent key was looked up twice"


def test_bq_cjk_can_never_claim_full_text():
    """There is no CJK full text in BigQuery to return. Measured 2026-08-22 across every snapshot
    from `publications_201710` to `publications_202511`: 21,993,541 US descriptions and
    18,760,680 US claims, and exactly zero for CN, JP, KR, TW, EP, WO and DE.

    So this rung must always fall through to the rung that does have full text, and asserting it
    here is what stops a later change quietly promoting an abstract to a document."""
    p, _ = _bq_provider([_row("CN1A", abstract="y" * 4000, title="t" * 500)])
    res = asyncio.run(p.fetch("CN1A", None))
    assert res.abstract and res.title
    assert res.claims == "" and res.description == ""
    assert res.complete() is False, "an abstract was accepted as a document"
    assert res.meta["text_is_machine_translation"] is True
    assert res.meta["source_language"] == "zh"


def test_bq_cjk_unescapes_the_jpo_markup():
    """Google ships JPO abstracts as `&lt;P&gt;PROBLEM TO BE SOLVED: ...`. Stored raw, every
    Japanese abstract in the corpus carries the literal tokens `lt` and `gt`."""
    p, _ = _bq_provider([_row("JP1A", country="JP", src="ja",
                              abstract="&lt;P&gt;PROBLEM TO BE SOLVED: lift it. "
                                       "&lt;P&gt;SOLUTION: a suction cup.")])
    res = asyncio.run(p.fetch("JP1A", None))
    assert "&lt;" not in res.abstract and "<P>" not in res.abstract
    assert res.abstract == "PROBLEM TO BE SOLVED: lift it.\nSOLUTION: a suction cup."


def test_bq_cjk_charges_the_batch_cost_over_the_keys_it_carried():
    """The ledger records megabytes, because megabytes are what BigQuery bills. A per-publication
    figure that ignored the shared query would report a cost nobody was charged."""
    pubs = [f"CN{n}A" for n in range(10)]
    p, _ = _bq_provider([_row(k) for k in pubs], billed=50_000_000)
    asyncio.run(p.prefetch(pubs, None))
    res = asyncio.run(p.fetch(pubs[0], None))
    assert res.credits == pytest.approx(5.0), "50 MB over 10 keys is 5 MB a key"
    assert p.bytes_billed == 50_000_000


def test_bq_cjk_is_inert_rather_than_fatal_without_a_client():
    """The documented fail-soft contract: a rung that cannot start is a rung that misses."""
    p = providers.BigQueryCjkProvider(table="t")
    p._client_err = "no credentials"
    ok, why = p.available()
    assert ok is False and "no credentials" in why
    assert asyncio.run(p.fetch("CN1A", None)).reached is True


# =============================================================================================
# 3. the worker: one warm-up per batch, and a partial answer that is kept
# =============================================================================================
class _CountingProvider(providers.Provider):
    def __init__(self, name, result=None):
        self.name = name
        self.timeout = 5.0
        super().__init__()
        self._result = result
        self.prefetches: list = []

    async def prefetch(self, pubs, client):
        self.prefetches.append(list(pubs))

    async def fetch(self, pub, client):
        return self._result


def test_the_worker_warms_every_rung_once_per_leased_batch(monkeypatch):
    """Defect injection: delete the `await self.prefetch_batch(...)` line from `Worker.run` and
    this goes red, and the BigQuery rung silently starts paying a per-query minimum per
    publication instead of per batch."""
    from sources import docstore
    import runstore
    monkeypatch.setattr(docstore, "_put_sync", lambda pn, rec: None)
    monkeypatch.setattr(runstore, "queue_for_ingest", lambda pn, **kw: {"id": 1})
    monkeypatch.setattr(worker.blobstore, "enabled", lambda: False)
    monkeypatch.setattr(worker, "BATCH", 3)

    tasks.seed([{"publication_number": p, "family_id": p, "priority": 100000}
                for p in PUBS[:3]], manifest=MANIFEST)
    prov = _CountingProvider("counting", providers.FetchResult(
        provider="counting", description="d" * 2000))
    parts = sorted({tasks.partition_of(p) for p in PUBS[:3]})
    w = worker.Worker(0, 1, cascade=[prov], partitions=parts, manifests=UNIT)
    asyncio.run(w.run(max_publications=3, max_batches=1))
    assert len(prov.prefetches) == 1, prov.prefetches
    assert sorted(prov.prefetches[0]) == sorted(PUBS[:3])


def test_a_rung_that_cannot_warm_itself_still_lets_the_batch_run(monkeypatch):
    """A renamed cache table must cost one rung its answers, not the whole partition."""
    class _Broken(_CountingProvider):
        async def prefetch(self, pubs, client):
            raise RuntimeError("cache table not found")

    prov = _Broken("broken", providers.FetchResult(provider="broken", description="d" * 2000))
    w = worker.Worker(0, 1, cascade=[prov], dry_run=True)
    asyncio.run(w.prefetch_batch([{"publication_number": PUBS[0]}], None))
    res = asyncio.run(w.cascade_for(PUBS[0], {"publication_number": PUBS[0], "partition_id": 0},
                                    None, []))
    assert res is not None and res.provider == "broken"


def test_the_best_partial_answer_is_kept_rather_than_discarded(monkeypatch):
    """For 55.9% of the niche a machine-translated English abstract is the only text that will
    ever exist: measured 2026-08-22, 97.5% of the CJK-only niche families have no non-CJK member
    anywhere in the world DOCDB family, so there is no original to fall back on.

    Before this the cascade returned None whenever nothing cleared the completeness floor, and
    every one of those publications went to `missing` holding literally nothing: 1,341 of them on
    the live pool. Defect injection: make `Worker.cascade_for` return None instead of `best` and
    this goes red on both the docstore write and the recorded provider.
    """
    from sources import docstore
    import runstore
    stored: dict = {}
    monkeypatch.setattr(docstore, "_put_sync", lambda pn, rec: stored.update({pn: rec}))
    monkeypatch.setattr(runstore, "queue_for_ingest", lambda pn, **kw: {"id": 1})
    monkeypatch.setattr(worker.blobstore, "enabled", lambda: False)

    nothing = _CountingProvider("empty", providers.FetchResult(provider="empty", reached=True))
    stub = _CountingProvider("bq_cjk", providers.FetchResult(
        provider="bq_cjk", title="Suction gripper", abstract="a" * 400))
    tasks.seed([{"publication_number": PUBS[0], "family_id": PUBS[0], "priority": 100000}],
               manifest=MANIFEST)
    w = worker.Worker(0, 1, cascade=[nothing, stub], manifests=UNIT)
    leased = tasks.lease(list(range(tasks.PARTITIONS)), w.id, limit=1, manifests=UNIT)
    assert [r["publication_number"] for r in leased] == [PUBS[0]]
    w.held = {PUBS[0]}
    asyncio.run(w.handle(leased[0], None, []))

    assert stored.get(PUBS[0], {}).get("abstract") == "a" * 400
    assert stored[PUBS[0]]["title"] == "Suction gripper"
    import db
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute("SELECT state, provider, last_error FROM fulltext_fetch_task "
                    "WHERE publication_number=%s", (PUBS[0],))
        row = cur.fetchone()
    assert row["state"] == "missing", "an abstract must not be recorded as full text"
    assert row["provider"] == "bq_cjk"
    assert "title/abstract" in row["last_error"]


def test_a_complete_answer_still_beats_a_partial_one(monkeypatch):
    """The other half: keeping the stub must not stop the cascade reaching the real text."""
    stub = _CountingProvider("bq_cjk", providers.FetchResult(
        provider="bq_cjk", title="t", abstract="a" * 400))
    full = _CountingProvider("serp_self", providers.FetchResult(
        provider="serp_self", description="d" * 2000, claims="c" * 400))
    w = worker.Worker(0, 1, cascade=[stub, full], dry_run=True)
    res = asyncio.run(w.cascade_for(PUBS[0],
                                    {"publication_number": PUBS[0], "partition_id": 0}, None, []))
    assert res.provider == "serp_self" and res.complete()
