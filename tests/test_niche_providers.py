from __future__ import annotations

import asyncio
import json
import threading

import pytest

from corpus.niche.limits import PaidBudget, PaidLimits
from corpus.niche.models import Completeness, FetchRequest, ProviderResult
from corpus.niche.providers.base import BaseProvider, ProviderTemporaryError
from corpus.niche.providers.epo import EPOProvider
from corpus.niche.providers.firecrawl import FirecrawlProvider
from corpus.niche.providers.scrapingbee import ScrapingBeeProvider
from corpus.niche.providers.uspto import USPTOProvider
from corpus.niche.storage import FileObjectStore, GCSObjectStore
from corpus.niche.waterfall import (
    PipelineFatalError,
    ProviderWaterfall,
    default_provider_names,
)


class _Provider(BaseProvider):
    def __init__(self, name, result=None, error=None, *, paid=False, estimate=0):
        self.name = name
        self.paid = paid
        self._result = result
        self._error = error
        self._estimate = estimate
        self.calls = 0

    def enabled(self):
        return True

    def estimated_credits(self, _request):
        return self._estimate

    def fetch(self, _request):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


def _request(**values):
    defaults = {
        "publication_id": "US1234567A1",
        "publication_number": "US1234567A1",
        "authority": "US",
        "missing_fields": frozenset({"claims", "description"}),
        "completeness": Completeness(),
    }
    defaults.update(values)
    return FetchRequest(**defaults)


def _success(provider="marec", credits=0):
    return ProviderResult(
        provider=provider,
        content=b"<patent-document/>",
        media_type="application/xml",
        source_url="https://example.invalid/patent.xml",
        http_status=200,
        credits_used=credits,
        completeness=Completeness(
            has_claims=True,
            has_complete_claims=True,
            has_description=True,
            has_complete_description=True,
        ),
    )


def test_provider_fallback_order_is_explicit_and_stable():
    assert default_provider_names() == [
        "local",
        "marec",
        "uspto",
        "epo",
        "google_patents",
        "self_serp",
        "firecrawl",
        "scrapingbee",
        "serpapi",
    ]


def test_scaled_acquisition_reuses_firecrawl_before_scrapingbee(monkeypatch):
    from acquire import providers as acquisition
    from sources import gpatents_direct

    seen = []

    def fake_scrape(_provider, request):
        seen.append(request.publication_number)
        return ProviderResult(
            provider="firecrawl",
            content=b"<html>full patent</html>",
            media_type="text/html",
            source_url="https://patents.google.com/patent/US1234567A1/en",
            http_status=200,
            credits_used=1,
        )

    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-only")
    monkeypatch.setattr(FirecrawlProvider, "fetch", fake_scrape)
    monkeypatch.setattr(
        gpatents_direct,
        "parse_document",
        lambda _html, _publication: {
            "claims": "A complete claim " * 20,
            "description": "A complete description " * 50,
        },
    )

    cascade = acquisition.build(["firecrawl", "scrapingbee"])

    assert [provider.name for provider in cascade] == ["firecrawl", "scrapingbee"]

    async def fetch():
        import httpx

        async with httpx.AsyncClient() as client:
            return await cascade[0].fetch("US1234567A1", client)

    result = asyncio.run(fetch())
    assert seen == ["US1234567A1"]
    assert result.complete()
    assert result.credits == 1
    assert result.source_url.endswith("/US1234567A1/en")


def test_provider_waterfall_stops_at_first_complete_result():
    calls = []

    class RecordingProvider(_Provider):
        def fetch(self, request):
            calls.append(self.name)
            return super().fetch(request)

    providers = [
        RecordingProvider("local", result=None),
        RecordingProvider("marec", result=None),
        RecordingProvider("uspto", result=_success("uspto")),
        RecordingProvider("firecrawl", result=_success("firecrawl"), paid=True, estimate=1),
    ]
    waterfall = ProviderWaterfall(providers, PaidBudget({"firecrawl": 1}))

    outcome = waterfall.fetch(_request())

    assert outcome.status == "completed"
    assert outcome.result.provider == "uspto"
    assert calls == ["local", "marec", "uspto"]


def test_existing_text_skips_every_paid_provider():
    paid = _Provider("firecrawl", result=_success("firecrawl", 1), paid=True, estimate=1)
    complete = Completeness(
        has_claims=True,
        has_complete_claims=True,
        has_description=True,
        has_complete_description=True,
    )
    waterfall = ProviderWaterfall([paid], PaidBudget({"firecrawl": 10}))

    outcome = waterfall.fetch(_request(missing_fields=frozenset(), completeness=complete))

    assert outcome.status == "already_complete"
    assert paid.calls == 0


def test_complete_owned_text_can_request_local_only_normalization():
    complete = Completeness(
        has_claims=True,
        has_complete_claims=True,
        has_description=True,
        has_complete_description=True,
    )
    local = _Provider("local", result=_success("local"))
    external = _Provider("google_patents", result=_success("google_patents"))
    waterfall = ProviderWaterfall([local, external], PaidBudget({}))

    outcome = waterfall.fetch(_request(
        missing_fields=frozenset(),
        completeness=complete,
        require_artifact=True,
        local_only=True,
    ))

    assert outcome.status == "completed"
    assert local.calls == 1
    assert external.calls == 0


def test_reusable_cached_provider_is_skipped_while_fallback_continues():
    cached = _Provider("firecrawl", result=_success("firecrawl", 1), paid=True, estimate=1)
    fallback = _Provider("scrapingbee", result=_success("scrapingbee", 15), paid=True, estimate=15)
    waterfall = ProviderWaterfall(
        [cached, fallback],
        PaidBudget({"firecrawl": 1, "scrapingbee": 15}),
    )

    outcome = waterfall.fetch(_request(skip_providers=frozenset({"firecrawl"})))

    assert outcome.status == "completed"
    assert cached.calls == 0
    assert fallback.calls == 1


def test_provider_error_isolation_continues_to_the_next_rung():
    broken = _Provider("marec", error=ProviderTemporaryError("archive unavailable"))
    working = _Provider("epo", result=_success("epo"))
    waterfall = ProviderWaterfall([broken, working], PaidBudget({}))

    outcome = waterfall.fetch(_request())

    assert outcome.status == "completed"
    assert [attempt.status for attempt in outcome.attempts] == ["error", "success"]
    assert outcome.attempts[0].error_class == "ProviderTemporaryError"


def test_persistence_failure_stops_before_another_provider_call():
    first = _Provider("marec", result=_success("marec"))
    fallback = _Provider("firecrawl", result=_success("firecrawl", 1), paid=True, estimate=1)

    def broken_storage(_request, _result):
        raise PipelineFatalError("object storage unavailable")

    waterfall = ProviderWaterfall(
        [first, fallback],
        PaidBudget({"firecrawl": 1}),
        on_result=broken_storage,
    )

    outcome = waterfall.fetch(_request())

    assert outcome.status == "fatal"
    assert first.calls == 1
    assert fallback.calls == 0
    assert outcome.attempts[-1].error_class == "PipelineFatalError"


def test_paid_result_is_settled_once_when_persistence_fails():
    paid = _Provider(
        "firecrawl",
        result=_success("firecrawl", 1),
        paid=True,
        estimate=2,
    )
    budget = PaidBudget({"firecrawl": 2})

    def broken_storage(_request, _result):
        raise PipelineFatalError("object storage unavailable")

    outcome = ProviderWaterfall(
        [paid],
        budget,
        on_result=broken_storage,
    ).fetch(_request())

    assert outcome.status == "fatal"
    assert budget.snapshot() == {"firecrawl": 1}


@pytest.mark.parametrize(
    "env_name,provider",
    [
        ("MAX_FIRECRAWL_CREDITS_PER_RUN", "firecrawl"),
        ("MAX_SCRAPINGBEE_CREDITS_PER_RUN", "scrapingbee"),
        ("MAX_SERPAPI_REQUESTS_PER_RUN", "serpapi"),
    ],
)
def test_missing_or_invalid_paid_limit_fails_closed(env_name, provider):
    assert PaidLimits.from_env({}).caps[provider] == 0
    assert PaidLimits.from_env({env_name: "unlimited"}).caps[provider] == 0
    assert PaidLimits.from_env({env_name: "-1"}).caps[provider] == 0


def test_firecrawl_credit_limit_prevents_request_before_network_call():
    paid = _Provider("firecrawl", result=_success("firecrawl", 1), paid=True, estimate=1)
    waterfall = ProviderWaterfall([paid], PaidBudget({"firecrawl": 0}))

    outcome = waterfall.fetch(_request())

    assert outcome.status == "missing"
    assert paid.calls == 0
    assert outcome.attempts[0].status == "budget_exhausted"


def test_scrapingbee_credit_limit_reserves_non_js_and_js_retry():
    paid = _Provider("scrapingbee", result=_success("scrapingbee", 30), paid=True, estimate=30)
    waterfall = ProviderWaterfall([paid], PaidBudget({"scrapingbee": 29}))

    outcome = waterfall.fetch(_request())

    assert outcome.status == "missing"
    assert paid.calls == 0


def test_paid_budget_is_atomic_across_workers():
    barrier = threading.Barrier(2)

    class SlowBudget(PaidBudget):
        def remaining(self, provider):
            value = super().remaining(provider)
            barrier.wait()
            return value

    budget = SlowBudget({"firecrawl": 1})
    results = []

    def reserve():
        results.append(budget.reserve("firecrawl", 1))

    workers = [threading.Thread(target=reserve) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(results) == [False, True]
    assert budget.spent["firecrawl"] == 1


class _Response:
    def __init__(self, status=200, content=b"", json_data=None, headers=None):
        self.status_code = status
        self.content = content
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        return self._json


class _HTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


def test_firecrawl_uses_one_deterministic_scrape_and_records_credits():
    html = '<section itemprop="claims">1. A gripper.</section>'
    http = _HTTP([
        _Response(
            json_data={
                "success": True,
                "data": {"rawHtml": html, "metadata": {"creditsUsed": 1}},
            }
        )
    ])
    provider = FirecrawlProvider(api_key="secret", http=http)

    result = provider.fetch(_request())

    method, url, kwargs = http.calls[0]
    assert (method, url) == ("POST", "https://api.firecrawl.dev/v2/scrape")
    assert kwargs["json"]["url"].endswith("/patent/US1234567A1/en")
    assert kwargs["json"]["formats"] == ["rawHtml"]
    assert result.credits_used == 1


def test_deterministic_patent_url_preserves_known_source_language():
    http = _HTTP([
        _Response(
            json_data={
                "success": True,
                "data": {"rawHtml": '<section itemprop="claims">Anspruch 1.</section>'},
            }
        )
    ])
    provider = FirecrawlProvider(api_key="secret", http=http)

    provider.fetch(_request(language="de"))

    assert http.calls[0][2]["json"]["url"].endswith("/patent/US1234567A1/de")


def test_scrapingbee_starts_without_js_and_only_retries_when_text_is_thin():
    thin = b"<html><body>blocked</body></html>"
    rich = b'<section itemprop="claims">1. A gripper.</section>'
    http = _HTTP([
        _Response(content=thin, headers={"Spb-cost": "15"}),
        _Response(content=rich, headers={"spb-cost": "15"}),
    ])
    provider = ScrapingBeeProvider(api_key="secret", http=http)

    result = provider.fetch(_request())

    assert [call[2]["params"]["render_js"] for call in http.calls] == ["false", "true"]
    assert result.credits_used == 30
    assert result.content == rich


def test_existing_async_adapters_use_the_shared_source_event_loop():
    class Adapter:
        def enabled(self):
            return True

        async def details(self, _publication, _client):
            return {"claims": ["1. A vacuum tool."], "description": "Description."}

        async def fulltext(self, _publication, _client):
            return {"claims": "1. A vacuum tool.", "description": "Description."}

    uspto = USPTOProvider(adapter=Adapter(), timeout=5)
    epo = EPOProvider(adapter=Adapter(), timeout=5)

    assert uspto.fetch(_request()).provider == "uspto"
    assert epo.fetch(_request(authority="EP")).provider == "epo"


def test_raw_source_hashing_is_content_addressed_and_idempotent(tmp_path):
    store = FileObjectStore(tmp_path)
    first = store.put_raw(
        authority="US",
        publication_number="US1234567A1",
        provider="marec",
        content=b"same bytes",
        media_type="application/xml",
        http_status=200,
        headers={"Authorization": "secret", "ETag": "abc"},
        source_url="https://example.invalid/doc?id=1&api_key=secret",
    )
    second = store.put_raw(
        authority="US",
        publication_number="US1234567A1",
        provider="marec",
        content=b"same bytes",
        media_type="application/xml",
        http_status=200,
        headers={"Authorization": "different", "ETag": "abc"},
    )

    assert first.content_hash == second.content_hash
    assert first.uri == second.uri
    assert len(list((tmp_path / "patents" / "raw").rglob("*.xml"))) == 1
    metadata = json.loads((tmp_path / first.metadata_uri).read_text())
    assert "Authorization" not in metadata["http_headers"]
    assert metadata["http_headers"] == {"etag": "abc"}
    assert metadata["source_url"] == "https://example.invalid/doc?id=1"
    assert "secret" not in json.dumps(metadata)


class _Blob:
    def __init__(self, name, objects):
        self.name = name
        self.objects = objects

    def upload_from_string(self, content, **kwargs):
        if kwargs.get("if_generation_match") == 0 and self.name in self.objects:
            raise RuntimeError("precondition failed")
        self.objects[self.name] = bytes(content)

    def download_as_bytes(self):
        return self.objects[self.name]


class _Bucket:
    def __init__(self, objects):
        self.objects = objects

    def blob(self, name):
        return _Blob(name, self.objects)


class _GCS:
    def __init__(self):
        self.objects = {}

    def bucket(self, name):
        assert name == "patent-bucket"
        return _Bucket(self.objects)


def test_gcs_raw_storage_uses_shared_content_addressed_objects():
    client = _GCS()
    store = GCSObjectStore("gs://patent-bucket/niche", client=client)

    first = store.put_raw(
        authority="EP",
        publication_number="EP1234567A1",
        provider="epo",
        content=b"<xml/>",
        media_type="application/xml",
        http_status=200,
        headers={},
    )
    second = store.put_raw(
        authority="EP",
        publication_number="EP1234567A1",
        provider="epo",
        content=b"<xml/>",
        media_type="application/xml",
        http_status=200,
        headers={},
    )

    assert first.uri == second.uri
    assert first.uri.startswith("gs://patent-bucket/niche/patents/raw/EP/EP1234567A1/epo/")
    assert store.read(first.uri) == b"<xml/>"
    assert len(client.objects) == 2
