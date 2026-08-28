"""Hermetic tests for the src/sources package (App A adapter port).

No paid API is ever hit: the facade's HTTP client factory is replaced with a
handler-driven fake, and no test requires a real credential (keys are set to
dummies via monkeypatch where an adapter checks enabled()).

The Postgres-backed docstore tests use the real PG like the rest of the suite,
but only where it is reachable: they auto-skip when the pilot database cannot
be reached (e.g. on the builder box), and SOURCES_TEST_PG=0 / =1 forces skip /
run. Test rows use the ZZTESTSRC pub-number prefix and are deleted afterwards.
"""
import os
import sys
import time
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

import httpx

import sources
from sources import base as sbase
from sources import gpatents_direct as G
from sources import himmpat as H
from sources import openalex as OA
from sources import pqai as PQ
from sources import serpapi_gpatents as SP


@pytest.fixture(autouse=True)
def no_paid_apis():
    """Override the suite-wide autouse fixture of the same name (conftest.py),
    which monkeypatches embed/enrich/llm — heavyweight imports this hermetic
    module never touches. The HTTP layer here is replaced wholesale by
    _FakeClient, so no paid API can be reached either way."""
    yield


@pytest.fixture(autouse=True)
def isolate_sources_state(monkeypatch, tmp_path):
    """Fresh latches, caches, ledgers and adapter singletons for every test —
    and keep the REAL ~/.patents ledgers out of reach so tests never spend or
    corrupt the shared per-host quota files."""
    monkeypatch.setattr(H, "_STATE_FILE", str(tmp_path / "himmpat_usage.json"))
    monkeypatch.setattr(PQ, "_MED_CALLS_FILE", str(tmp_path / "pqai_mediator_calls.json"))
    monkeypatch.setattr(PQ, "_QUOTA_FILE", str(tmp_path / "pqai_search_quota.json"))
    monkeypatch.setattr(PQ, "_QUOTA_STATE", {})
    monkeypatch.setattr(SP, "_BLOCKED_UNTIL", 0.0)
    monkeypatch.setattr(SP, "_BLOCK_REASON", "")
    monkeypatch.setattr(OA, "_EXHAUSTED_UTC_DATE", "")
    G.reset_cooldown()
    monkeypatch.setattr(G, "_LAST_CALL", 0.0)
    sbase.CACHE.clear()
    sources.reset_adapters()
    yield
    G.reset_cooldown()
    sbase.CACHE.clear()
    sources.reset_adapters()


# ---------------------------------------------------------------------------
# fake HTTP layer
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None, url=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else ""
        self._url = url

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", self._url or "http://fake/")
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=req, response=self)


class FakeClient:
    """Handler-driven stand-in for httpx.AsyncClient. handler(method, url, kw)
    -> FakeResponse (or raises). Records every call."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, url, **kw):
        self.calls.append((method.upper(), url, kw))
        return self.handler(method.upper(), url, kw)

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)

    async def aclose(self):
        pass


def use_fake_client(monkeypatch, handler):
    client = FakeClient(handler)
    monkeypatch.setattr(sources, "_new_client", lambda timeout: client)
    return client


def run_async(coro, timeout=30):
    """Run a coroutine on the facade's shared background loop, so every asyncio
    primitive the adapters build binds to ONE loop (the production topology)."""
    return sources._submit(coro, timeout=timeout)


# ---------------------------------------------------------------------------
# facade shape
# ---------------------------------------------------------------------------
def _serp_handler(rows):
    def handler(method, url, kw):
        if "serpapi.com" in url:
            return FakeResponse(200, {"organic_results": rows}, url=url)
        raise AssertionError(f"unexpected call: {method} {url}")
    return handler


def test_facade_shape(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    sources.reset_adapters()
    rows = [
        {"publication_number": "US-9987654-B2", "title": "Vacuum lifter",
         "snippet": "a lifter", "assignee": "ACME", "inventor": "A. Person",
         "publication_date": "2018-05-01", "priority_date": "2016-01-02",
         "patent_id": "patent/US9987654B2/en"},
        {"publication_number": "US-9987654-B2"},   # duplicate — must dedup
        {"patent_id": "patent/EP1234567A1/en"},
    ]
    use_fake_client(monkeypatch, _serp_handler(rows))
    q = f"vacuum lifter {uuid.uuid4().hex[:8]}"
    env = sources.bulk([{"source": "serpapi_gpatents", "q": q,
                         "element": "gripper body", "why": "head term",
                         "date_from": "2010-01-01", "date_to": "2020-01-01"}])
    assert env["ok"] is True
    assert env["n_queries"] == 1 and env["errors"] == []
    cands = env["candidates"]
    assert [c["pub_number"] for c in cands] == ["US9987654B2", "EP1234567A1"]
    c = cands[0]
    # the exact row shape App A's /api/bulk_search returned
    for key in ("pub_number", "source", "source_rank", "title", "abstract", "snippet",
                "assignee", "date", "priority_date", "kind", "cpc", "url",
                "family_id", "query_i", "element"):
        assert key in c
    assert c["source"] == "serpapi_gpatents"
    assert c["element"] == "gripper body"
    assert c["date"] == "2018-05-01" and c["priority_date"] == "2016-01-02"
    # search() is the thin candidates-only wrapper
    assert sources.search([]) == []


def test_unknown_source_is_skipped_not_fatal(monkeypatch):
    use_fake_client(monkeypatch, _serp_handler([]))
    env = sources.bulk([{"source": "nope", "q": "x"}])
    assert env["candidates"] == [] and env["n_queries"] == 0
    assert env["skipped"] and env["skipped"][0]["why"] == "unknown source"


def test_dead_source_degrades_never_raises(monkeypatch):
    """Fail-soft: an adapter that raises contributes an error entry, not an
    exception through the facade. (uspto lets transport errors propagate by
    design — "a broken key or a 5xx is visible rather than silent".)"""
    monkeypatch.setenv("USPTO_ODP_KEY", "test-key")
    sources.reset_adapters()

    def handler(method, url, kw):
        raise RuntimeError("connection reset by peer")
    use_fake_client(monkeypatch, handler)
    env = sources.bulk([{"source": "uspto",
                         "q": f"vacuum lifter boom{uuid.uuid4().hex[:8]}"}], timeout=8)
    assert env["ok"] is True and env["candidates"] == []
    assert env["stats"]["uspto"]["errors"] == 1
    assert "RuntimeError" in env["errors"][0]["error"]


# ---------------------------------------------------------------------------
# budget caps
# ---------------------------------------------------------------------------
def test_budget_caps_bind_and_are_named(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    sources.reset_adapters()
    client = use_fake_client(monkeypatch, _serp_handler([]))
    queries = [{"source": "serpapi_gpatents", "q": f"probe {i} {uuid.uuid4().hex[:6]}"}
               for i in range(45)]
    env = sources.bulk(queries, timeout=30)
    # cap 40 (PATENTS_SERPAPI_CAP default): 40 executed, 5 named in skipped
    assert env["n_queries"] == 40
    over = [s for s in env["skipped"] if "budget cap" in s["why"]]
    assert len(over) == 5
    assert env["budget_used"]["serpapi_gpatents"] == 40
    assert len(client.calls) == 40      # the cap bounds real HTTP, not just bookkeeping


def test_default_per_source_cap(monkeypatch):
    def handler(method, url, kw):
        if "openalex.org" in url:
            return FakeResponse(200, {"results": []}, url=url)
        raise AssertionError(f"unexpected call: {url}")
    client = use_fake_client(monkeypatch, handler)
    queries = [{"source": "openalex", "q": f"paper {i} {uuid.uuid4().hex[:6]}"}
               for i in range(15)]
    env = sources.bulk(queries, timeout=30)
    assert env["n_queries"] == 12       # PATENTS_PER_SOURCE_CAP default
    assert len([s for s in env["skipped"] if "budget cap" in s["why"]]) == 3
    assert len(client.calls) == 12


def test_himmpat_query_cap_is_3(monkeypatch):
    monkeypatch.setenv("HIMMPAT_API_KEY", "test-key")
    sources.reset_adapters()

    def handler(method, url, kw):
        # expression search: no results (201) — cheapest way to count queries
        return FakeResponse(200, {"code": 201, "message": "no results", "data": None}, url=url)
    client = use_fake_client(monkeypatch, handler)
    queries = [{"source": "himmpat", "q": f"suction gripper {i} x{uuid.uuid4().hex[:6]}"}
               for i in range(5)]
    env = sources.bulk(queries, timeout=30)
    assert env["n_queries"] == 3        # HIMMPAT_QUERIES_PER_RUN default
    assert len(client.calls) == 3


# ---------------------------------------------------------------------------
# himmpat envelope: HTTP 200 is NOT success
# ---------------------------------------------------------------------------
def test_himmpat_http200_business_error_is_an_error_not_empty(monkeypatch):
    monkeypatch.setenv("HIMMPAT_API_KEY", "test-key")
    sources.reset_adapters()

    def handler(method, url, kw):
        return FakeResponse(200, {"code": 103, "message": "quota gone", "data": None}, url=url)
    use_fake_client(monkeypatch, handler)
    env = sources.bulk([{"source": "himmpat",
                         "q": f"vacuum lifter y{uuid.uuid4().hex[:6]}"}], timeout=15)
    # An exhausted key must surface as a source error, never as "0 matches".
    assert env["stats"]["himmpat"]["errors"] == 1
    assert "HimmPat code 103" in env["errors"][0]["error"]
    assert env["candidates"] == []
    # ...and code 103 latches the adapter off (persisted block), with the reason named
    h = sources.registry()["himmpat"]
    assert not h.search_available()
    assert "paused" in h.disabled_reason()


def test_himmpat_code_201_is_a_clean_empty(monkeypatch):
    monkeypatch.setenv("HIMMPAT_API_KEY", "test-key")
    sources.reset_adapters()

    def handler(method, url, kw):
        return FakeResponse(200, {"code": 201, "message": "no results", "data": None}, url=url)
    use_fake_client(monkeypatch, handler)
    env = sources.bulk([{"source": "himmpat",
                         "q": f"nonexistent widget z{uuid.uuid4().hex[:6]}"}], timeout=15)
    assert env["stats"]["himmpat"] == {"queries": 1, "hits": 0, "errors": 0}
    assert env["errors"] == [] and env["candidates"] == []


def test_himmpat_search_hydrates_and_charges_units(monkeypatch):
    monkeypatch.setenv("HIMMPAT_API_KEY", "test-key")
    sources.reset_adapters()
    biblio = {
        "a1": {"publicationReferenceModel": {"pn": "CN107776963B", "pd": "20200304"},
               "applicationReferenceModel": {"apd": "20170815"},
               "inventionTitleModel": {"tie": "Vacuum gripping device", "tio": "真空抓取装置"},
               "abstractModel": {"abe": "An English abstract."},
               "partiesModel": {"assignee": [{"as": "Test Co"}]},
               "classificationModel": {"cpc": ["B66C1/02"]}},
        "b2": {"publicationReferenceModel": {"pn": "JP2019183307A", "pd": "20191024"}},
    }

    def handler(method, url, kw):
        if url.endswith("query_patent_ids_by_query_expression"):
            return FakeResponse(200, {"code": 200, "message": "ok",
                                      "data": {"ids": ["a1", "b2"], "total": 2}}, url=url)
        if url.endswith("get_patent_publication_by_patent_ids"):
            assert kw["json"]["ids"] == ["a1", "b2"]
            return FakeResponse(200, {"code": 200, "message": "ok", "data": biblio}, url=url)
        raise AssertionError(f"unexpected call: {url}")
    use_fake_client(monkeypatch, handler)
    env = sources.bulk([{"source": "himmpat",
                         "q": f'"vacuum gripper"/tac w{uuid.uuid4().hex[:6]}'}], timeout=15)
    pubs = [c["pub_number"] for c in env["candidates"]]
    assert pubs == ["CN107776963B", "JP2019183307A"]
    assert env["candidates"][0]["title"] == "Vacuum gripping device"   # English wins
    # ledger charged in vendor units: 1 (search) + 2 (hydrate per id) = 3
    assert H.usage()["day_units"] == 3


def test_himmpat_envelope_shape_drift_is_loud(monkeypatch):
    monkeypatch.setenv("HIMMPAT_API_KEY", "test-key")
    sources.reset_adapters()

    def handler(method, url, kw):
        return FakeResponse(200, {"weird": True}, url=url)   # no "code" key
    use_fake_client(monkeypatch, handler)
    env = sources.bulk([{"source": "himmpat",
                         "q": f"drifted v{uuid.uuid4().hex[:6]}"}], timeout=15)
    assert env["stats"]["himmpat"]["errors"] == 1
    assert "HimmPat code 199" in env["errors"][0]["error"]


# ---------------------------------------------------------------------------
# gpatents_direct: pacing + cooldown latch
# ---------------------------------------------------------------------------
def test_gpatents_direct_paces_calls(monkeypatch):
    monkeypatch.setattr(G, "MIN_INTERVAL", 0.2)
    client = FakeClient(lambda m, u, kw: FakeResponse(200, {"results": {}}, url=u))

    async def two():
        t0 = time.monotonic()
        await G._get(client, G.XHR)
        await G._get(client, G.XHR)
        return time.monotonic() - t0

    elapsed = run_async(two())
    assert len(client.calls) == 2
    # the second call waited out the minimum spacing (first is free: no prior call)
    assert elapsed >= 0.18, f"calls were not paced: {elapsed:.3f}s"


def test_gpatents_direct_503_trips_cooldown_latch(monkeypatch):
    client = FakeClient(lambda m, u, kw: FakeResponse(503, {}, url=u))
    with pytest.raises(Exception):
        run_async(G._get(client, G.DOC.format(pn="US9987654B2")))
    assert not G.available()
    assert "503" in G.block_reason()
    # while cooling down, details() answers {} WITHOUT touching the network
    n_before = len(client.calls)
    adapter = G.GooglePatentsDirect()
    assert run_async(adapter.details("US9987654B2", client)) == {}
    assert len(client.calls) == n_before
    # ...and the facade skips it as unavailable rather than erroring
    use_fake_client(monkeypatch, lambda m, u, kw: FakeResponse(200, {}, url=u))
    env = sources.bulk([{"source": "gpatents_direct", "q": "vacuum"}], timeout=10)
    assert env["n_queries"] == 0 and env["skipped"]
    assert "cooling down" in env["skipped"][0]["why"]
    G.reset_cooldown()
    assert G.available()


# ---------------------------------------------------------------------------
# serpapi quota latch
# ---------------------------------------------------------------------------
def test_serpapi_quota_exhausted_latch(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    sources.reset_adapters()

    def handler(method, url, kw):
        return FakeResponse(429, {"error": "You have run out of searches."}, url=url)
    client = use_fake_client(monkeypatch, handler)
    env = sources.bulk([{"source": "serpapi_gpatents",
                         "q": f"latch {uuid.uuid4().hex[:6]}"}], timeout=15)
    assert env["stats"]["serpapi_gpatents"]["errors"] == 1
    assert "SerpApiQuotaExhausted" in env["errors"][0]["error"]
    # the latch fails the NEXT call fast, without an HTTP round trip
    n = len(client.calls)
    env2 = sources.bulk([{"source": "serpapi_gpatents",
                          "q": f"latch2 {uuid.uuid4().hex[:6]}"}], timeout=15)
    assert env2["stats"]["serpapi_gpatents"]["errors"] == 1
    assert len(client.calls) == n


# ---------------------------------------------------------------------------
# env aliases
# ---------------------------------------------------------------------------
def test_env_aliases(monkeypatch):
    for var in ("SERPAPI_KEY", "SERPAPI_API_KEY", "EPO_OPS_KEY", "EPO_OPS_SECRET",
                "OPS_CONSUMER_KEY", "OPS_CONSUMER_SECRET"):
        monkeypatch.delenv(var, raising=False)
    sources.reset_adapters()
    reg = sources.registry()
    assert not reg["serpapi_gpatents"].enabled()
    assert not reg["epo_ops"].enabled()
    # the pilot's spellings alone are enough
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    monkeypatch.setenv("OPS_CONSUMER_KEY", "ck")
    monkeypatch.setenv("OPS_CONSUMER_SECRET", "cs")
    sources.reset_adapters()
    reg = sources.registry()
    assert reg["serpapi_gpatents"].enabled()
    assert reg["epo_ops"].enabled()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def test_health_shape():
    h = sources.health()
    names = {s["name"] for s in h["sources"]}
    assert {"serpapi_gpatents", "bigquery_gpatents", "pqai", "epo_ops", "uspto",
            "openalex", "ipaustralia", "gpatents_direct", "himmpat",
            "web_patent_fallback"} <= names
    for s in h["sources"]:
        for key in ("enabled", "search_available", "note", "reason"):
            assert key in s
    assert "day_cap" in h["himmpat_usage"]
    assert "available" in h["gpatents_direct"]
    assert "docstore" in h       # dict or {"error": ...} — never missing


# ---------------------------------------------------------------------------
# fulltext ladder
# ---------------------------------------------------------------------------
class FakeDocstore:
    """In-memory stand-in with the docstore's async surface."""

    def __init__(self, held=None):
        self.records = dict(held or {})
        self.put_calls = []

    async def have(self, pns):
        out = {}
        for p in pns:
            r = self.records.get(p)
            if r:
                out[p] = {"biblio": True,
                          "claims_chars": len(r.get("claims", "")),
                          "desc_chars": len(r.get("description", "")),
                          "fulltext_source": r.get("source", "")}
        return out

    async def get(self, pn, want_text=True):
        return self.records.get(pn)

    async def put_many(self, records):
        self.put_calls.append(dict(records))
        self.records.update(records)


def test_ladder_docstore_hit_short_circuits_network(monkeypatch):
    from sources import fulltext as FT
    held = {"US9987654B2": {"pub_number": "US9987654B2", "title": "Held doc",
                            "claims": "1. A claim." * 40,
                            "description": "body " * 400,
                            "abstract": "held", "source": "gpatents_direct"}}
    fake_ds = FakeDocstore(held)
    monkeypatch.setattr(FT, "docstore", fake_ds)

    def handler(method, url, kw):
        raise AssertionError(f"network touched for a cached document: {url}")
    use_fake_client(monkeypatch, handler)
    got = sources.fetch_fulltext(["US9987654B2"])
    rec = got["US9987654B2"]
    assert rec["title"] == "Held doc" and rec["claims"].startswith("1. A claim.")
    assert rec["source"] == "gpatents_direct"
    assert got["_summary"]["by_source"] == {"docstore": 1}


def test_ladder_order_ops_before_google_and_persists(monkeypatch):
    """A missed EP document is fetched from EPO OPS (rung 2) — Google/ScrapingBee/
    SerpApi are never touched — and the result is written back to the docstore."""
    from sources import fulltext as FT
    fake_ds = FakeDocstore()
    monkeypatch.setattr(FT, "docstore", fake_ds)
    monkeypatch.setenv("EPO_OPS_KEY", "ck")
    monkeypatch.setenv("EPO_OPS_SECRET", "cs")
    # keys for the rungs that must NOT run
    monkeypatch.setenv("SERPAPI_KEY", "sk")
    monkeypatch.setenv("SCRAPINGBEE_API_KEY", "sb")
    sources.reset_adapters()

    body = {"p": [{"$": "sentence " * 120}]}     # ~960 chars, past MIN_DESC_CHARS

    def handler(method, url, kw):
        if url.endswith("auth/accesstoken"):
            return FakeResponse(200, {"access_token": "tok", "expires_in": 1200}, url=url)
        if "/published-data/publication/epodoc/" in url:
            return FakeResponse(200, body, url=url)
        raise AssertionError(f"a costlier rung ran before OPS: {url}")
    client = use_fake_client(monkeypatch, handler)

    got = sources.fetch_fulltext(["EP1234567A1"])
    rec = got["EP1234567A1"]
    assert len(rec["description"]) >= FT.MIN_DESC_CHARS
    assert got["_summary"]["by_source"].get("epo_ops") == 1
    # persisted for next time, with the source stamped
    assert fake_ds.put_calls and "EP1234567A1" in fake_ds.put_calls[0]
    assert fake_ds.put_calls[0]["EP1234567A1"]["source"] == "epo_ops"
    # nothing but OPS auth + the two part fetches went out
    assert all("epo.org" in u for _, u, _kw in client.calls)


# ---------------------------------------------------------------------------
# docstore (real Postgres — auto-skips where the pilot DB is unreachable)
# ---------------------------------------------------------------------------
def _pg_available():
    flag = os.environ.get("SOURCES_TEST_PG", "").strip()
    if flag == "0":
        return False
    try:
        import db
        with db.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        if flag == "1":
            raise
        return False


pg = pytest.mark.skipif(not _pg_available(),
                        reason="pilot Postgres unreachable (set SOURCES_TEST_PG=1 to force)")

_TEST_PUB = "ZZTESTSRC0000001A1"


@pg
def test_docstore_merge_never_overwrite():
    from sources import docstore as DS
    DS.delete_sync([_TEST_PUB])
    try:
        # 1. a thin record from a cheap source
        run_async(DS.put(_TEST_PUB, {"claims": "short claim", "claims_lang": "en",
                                     "description": "short body", "desc_lang": "en",
                                     "title": "Thin", "source": "pqai"}))
        # 2. a richer record upgrades every field
        long_desc = "richer body " * 100
        run_async(DS.put(_TEST_PUB, {"claims": "1. A much longer claim text here.",
                                     "claims_lang": "en",
                                     "description": long_desc, "desc_lang": "en",
                                     "title": "Thin record, now richer",
                                     "source": "gpatents_direct"}))
        rec = run_async(DS.get(_TEST_PUB))
        assert rec["description"] == long_desc
        assert rec["fulltext_source"] == "gpatents_direct"
        assert rec["title"] == "Thin record, now richer"
        # 3. a cheap source answering AGAIN with less must not blank anything
        run_async(DS.put(_TEST_PUB, {"claims": "tiny", "description": "tiny",
                                     "claims_lang": "en", "desc_lang": "en",
                                     "title": "x", "source": "pqai"}))
        rec = run_async(DS.get(_TEST_PUB))
        assert rec["description"] == long_desc          # merge, never overwrite
        assert rec["fulltext_source"] == "gpatents_direct"
        assert rec["title"] == "Thin record, now richer"  # longer title kept
    finally:
        DS.delete_sync([_TEST_PUB])


@pg
def test_docstore_english_beats_longer_original():
    from sources import docstore as DS
    DS.delete_sync([_TEST_PUB])
    try:
        zh = "汉字" * 500                       # 1000 chars, zh
        en = "english translation " * 30       # 600 chars: >= 50% of the original
        run_async(DS.put(_TEST_PUB, {"description": zh, "desc_lang": "zh",
                                     "source": "himmpat"}))
        run_async(DS.put(_TEST_PUB, {"description": en, "desc_lang": "en",
                                     "source": "gpatents_direct"}))
        rec = run_async(DS.get(_TEST_PUB))
        assert rec["description"] == en        # English wins at similar length
        # but a MUCH shorter English snippet must not (under the 50% floor)
        DS.delete_sync([_TEST_PUB])
        run_async(DS.put(_TEST_PUB, {"description": zh, "desc_lang": "zh",
                                     "source": "himmpat"}))
        run_async(DS.put(_TEST_PUB, {"description": "tiny english", "desc_lang": "en",
                                     "source": "x"}))
        rec = run_async(DS.get(_TEST_PUB))
        assert rec["description"] == zh
    finally:
        DS.delete_sync([_TEST_PUB])


@pg
def test_docstore_have_and_biblio_merge():
    from sources import docstore as DS
    DS.delete_sync([_TEST_PUB])
    try:
        run_async(DS.put(_TEST_PUB, {"title": "T", "assignee": "ACME",
                                     "inventors": ["A"], "cpc": ["B66C1/02"],
                                     "country": "ZZ", "source": "serpapi_gpatents"}))
        run_async(DS.put(_TEST_PUB, {"inventors": ["B"], "cpc": ["B66C1/02", "B25J15/06"],
                                     "claims": "1. A claim long enough to keep." * 10,
                                     "claims_lang": "en", "source": "epo_ops"}))
        rec = run_async(DS.get(_TEST_PUB))
        assert rec["assignee"] == "ACME"                        # untouched scalar kept
        assert rec["inventors"] == ["A", "B"]                   # lists dedup-merged
        assert set(rec["cpc"]) == {"B66C1/02", "B25J15/06"}
        held = run_async(DS.have([_TEST_PUB, "ZZTESTSRC_MISSING"]))
        assert _TEST_PUB in held and "ZZTESTSRC_MISSING" not in held
        assert held[_TEST_PUB]["claims_chars"] > 0
        assert held[_TEST_PUB]["fulltext_source"] == "epo_ops"
    finally:
        DS.delete_sync([_TEST_PUB])
