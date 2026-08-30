"""Hermetic tests for the EUIPO registered-design adapter.

No paid API is hit: every call goes through an httpx.MockTransport that answers the three
real endpoints (token, search, details, view). The shapes asserted here are the shapes the
live Production API actually returned on 2026-08-23, not invented ones.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

import httpx

import sources
from sources import base as sbase
from sources import euipo as E
from sources.schema import SubQuery, is_design


@pytest.fixture(autouse=True)
def no_paid_apis():
    """Override the suite-wide autouse fixture; the HTTP layer here is a MockTransport."""
    yield


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setenv("EUIPO_KEY", "test-key")
    monkeypatch.setenv("EUIPO_SECRET", "test-secret")
    sbase.CACHE.clear()
    sources.reset_adapters()
    yield
    sbase.CACHE.clear()
    sources.reset_adapters()


# --------------------------------------------------------------------------- fake wire
#  One search row, exactly as the live API returns it: NO title, NO applicant name, and
#  applicants carried as bare {office, identifier} references. Gotcha 4 in the adapter.
SEARCH_ROW = {
    "designNumber": "005632742-0001",
    "applicationNumber": "005632742",
    "locarnoClasses": ["15.05"],
    "applicants": [{"office": "EM", "identifier": "779921"}],
    "applicationDate": "2018-09-07",
    "registrationDate": "2018-09-07",
    "expiryDate": "2028-09-07",
    "status": "REGISTERED_AND_FULLY_PUBLISHED",
    "designRepresentationMeans": "STATIC",
}

#  The detail record. The product indication really does differ in meaning between
#  languages on this record, which is why _english must not take the first block.
DETAIL = {
    "designNumber": "005632742-0001",
    "applicationNumber": "005632742",
    "locarnoClasses": ["15.05"],
    "productIndications": [
        {"language": "bg", "terms": ["Vakuumni chashki"]},
        {"language": "fr", "terms": ["Robots de nettoyage (partie de -)"]},
        {"language": "en", "terms": ["Suction cups for attachment"]},
    ],
    "applicants": [{"office": "EM", "identifier": "779921",
                    "name": "Ecovacs Robotics Co., Ltd."}],
    "designers": [{"identifier": "197945", "name": "LI, Xiaowen"}],
    "representatives": [{"identifier": "10633", "name": "FRKelly"}],
    "views": [{"order": 1, "imageFormat": "JPG"}, {"order": 2, "imageFormat": "JPG"}],
    "publications": [
        {"bulletinNumber": "2021/026", "publicationDate": "2021-02-09"},
        {"bulletinNumber": "2018/178", "publicationDate": "2018-09-19"},
    ],
    "publicationDefermentIndicator": False,
    "applicationDate": "2018-09-07",
    "registrationDate": "2018-09-07",
    "expiryDate": "2028-09-07",
    "status": "REGISTERED_AND_FULLY_PUBLISHED",
}

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


class Wire:
    """Records every request so the tests can assert on headers and params."""

    def __init__(self, search_body=None, detail_status=200, rows=None):
        self.calls = []
        self.search_body = search_body
        self.rows = rows
        self.detail_status = detail_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        headers = {"x-ratelimit-remaining": "name=default,34861;"}
        if url.startswith(E.TOKEN_URL):
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 28800,
                                             "token_type": "bearer", "scope": "uid"})
        if "/views/" in url:
            return httpx.Response(200, content=JPEG,
                                  headers={**headers, "content-type": "image/jpeg"})
        if url.split("?")[0].rstrip("/").endswith("/designs"):
            body = self.search_body
            if body is None:
                rows = self.rows if self.rows is not None else [SEARCH_ROW]
                body = {"designs": rows, "totalElements": len(rows),
                        "totalPages": 1, "size": 50, "page": 0}
            return httpx.Response(200, json=body, headers=headers)
        # details
        if self.detail_status != 200:
            return httpx.Response(self.detail_status, json={"title": "boom"}, headers=headers)
        return httpx.Response(200, json=DETAIL, headers=headers)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def run(coro):
    import asyncio
    return asyncio.run(coro)


def search(wire, **sq_kw):
    a = E.EUIPO()
    kw = {"source": "euipo", "native": "suction cup gripper vacuum",
          "rationale": "t", "element": "whole"}
    kw.update(sq_kw)

    async def go():
        async with wire.client() as c:
            return await a.search(SubQuery(**kw), c)
    return a, run(go())


# --------------------------------------------------------------------------- query building
def test_terms_drop_stopwords_and_order_by_length():
    assert E._terms("a suction cup gripper for the vacuum device") == \
        ["suction", "gripper", "vacuum", "cup"]


def test_terms_are_deduplicated_and_capped(monkeypatch):
    """Longest first, and the cap is applied AFTER the sort so the longest survive.

    gripper, suction and lifting are all seven letters; the sort is stable, so a tie keeps
    first-appearance order and the same query always builds the same RSQL. That determinism
    is what lets the facade cache a sub-query by its text.
    """
    monkeypatch.setattr(E, "MAX_TERMS", 3)
    assert E._terms("gripper GRIPPER suction vacuum lifting clamp") == \
        ["gripper", "suction", "lifting"]
    assert E._terms("gripper gripper gripper") == ["gripper"]


def test_the_date_bound_is_parenthesised_or_it_silently_admits_later_designs():
    """MEASURED against the live API 2026-08-23, and this is the whole reason for the parens.

    RSQL binds `and` tighter than `or`, so the unparenthesised
    `A or B and applicationDate<D` returned 588 rows with the newest dated 2026-08-11, while
    `(A or B) and applicationDate<D` returned 215 with the newest dated 2015-04-08. Without
    the parentheses the prior-art cutoff is not a filter at all, it is decoration on the last
    term, and a design published after the subject's priority date would be presented as
    prior art.
    """
    q = E._rsql(["suction", "gripper"], "2015-06-01")
    assert q.startswith('(productIndications=="*suction*" or productIndications=="*gripper*")')
    assert q.endswith(" and applicationDate<2015-06-01")


def test_a_single_term_is_still_parenthesised():
    assert E._rsql(["suction"], "2015-06-01") == \
        '(productIndications=="*suction*") and applicationDate<2015-06-01'


def test_no_date_bound_means_no_date_clause():
    assert "applicationDate" not in E._rsql(["suction"], None)


def test_a_term_that_could_break_out_of_the_wildcard_is_refused():
    with pytest.raises(ValueError):
        E._rsql(['suction" or designNumber=="1'], None)


def test_an_empty_query_raises_rather_than_returning_nothing():
    """A query with no distinctive terms would match the whole register. Refusing is the only
    honest answer; returning [] is indistinguishable from 'the EU has no such designs'."""
    with pytest.raises(ValueError):
        search(Wire(), native="the and of")


# --------------------------------------------------------------------------- language
def test_english_indication_wins_over_the_first_language_present():
    """The translations are not synonyms: this record reads 'Suction cups for attachment' in
    en and 'Robots de nettoyage' in fr, so taking the first block changes the subject."""
    assert E._english(DETAIL["productIndications"]) == "Suction cups for attachment"


def test_english_falls_back_to_whatever_language_exists():
    assert E._english([{"language": "fr", "terms": ["Ventouses"]}]) == "Ventouses"
    assert E._english([]) == ""
    assert E._english(None) == ""


# --------------------------------------------------------------------------- search mapping
def test_search_maps_a_row_to_a_candidate():
    a, rows = search(Wire())
    assert len(rows) == 1
    c = rows[0]
    assert c.pub_number == "EM005632742-0001"
    assert c.source == "euipo"
    assert c.date == "2018-09-07" and c.priority_date == "2018-09-07"
    assert c.url == "https://euipo.europa.eu/eSearch/#details/designs/005632742-0001"
    assert c.extra["locarno"] == ["15.05"]
    assert c.extra["design_number"] == "005632742-0001"


def test_a_design_is_marked_as_one_so_the_patent_guards_recognise_it():
    """kind 'S' is what makes schema.is_design() true. If a registered design ever reaches a
    patent report, every existing design-aware guard must see it for what it is rather than
    treating an appearance right as a utility publication."""
    _, rows = search(Wire())
    assert rows[0].kind == "S"
    assert is_design(rows[0].pub_number, rows[0].kind)


def test_both_auth_headers_go_on_every_api_call():
    """The gateway 401s with 'Invalid client id or secret' when X-IBM-Client-Id is missing,
    which reads as a credential problem and is not one."""
    wire = Wire()
    search(wire)
    api = [r for r in wire.calls if not str(r.url).startswith(E.TOKEN_URL)]
    assert api, "no API call was made"
    for r in api:
        assert r.headers.get("authorization") == "Bearer tok-123"
        assert r.headers.get("x-ibm-client-id") == "test-key"


def test_page_size_never_drops_below_the_gateways_floor(monkeypatch):
    """size < 10 is a hard 400, so a small page is zero results rather than a short page."""
    monkeypatch.setattr(E, "PAGE_SIZE", max(E.MIN_PAGE, 10))
    wire = Wire()
    search(wire)
    req = next(r for r in wire.calls if "/designs" in str(r.url)
               and not str(r.url).startswith(E.TOKEN_URL))
    assert int(dict(req.url.params)["size"]) >= E.MIN_PAGE


def test_the_quota_left_is_read_off_the_response():
    a, _ = search(Wire())
    assert a.remaining == 34861
    assert "34861 calls left today" in a.search_note()


def test_a_changed_wire_contract_raises_instead_of_reporting_no_matches():
    wire = Wire(search_body={"results": [], "totalElements": 0})
    with pytest.raises(ValueError, match="no 'designs' key"):
        search(wire)


def test_a_row_without_a_design_number_warns_and_is_skipped():
    wire = Wire(rows=[{"applicationDate": "2018-09-07"}])
    a, rows = search(wire)
    assert rows == []
    assert any("without designNumber" in w for w in a.pop_warnings())


def test_totalelements_without_rows_is_reported_not_swallowed():
    wire = Wire(search_body={"designs": [], "totalElements": 91})
    a, rows = search(wire)
    assert rows == []
    assert any("totalElements=91" in w for w in a.pop_warnings())


# --------------------------------------------------------------------------- hydration
def test_hydration_fills_the_text_the_search_response_does_not_carry():
    """A search row has no title and no applicant name, so an unhydrated candidate is
    unreadable. This is the same omission that made the ip-monitor drop every design."""
    _, rows = search(Wire())
    c = rows[0]
    assert c.title == "Suction cups for attachment"
    assert c.assignee == "Ecovacs Robotics Co., Ltd."
    assert c.inventors == ["LI, Xiaowen"]
    assert c.extra["hydrated"] is True
    assert c.extra["views"] == [1, 2]


def test_a_failed_detail_call_warns_and_keeps_the_rest_of_the_page():
    wire = Wire(detail_status=500)
    a, rows = search(wire)
    assert len(rows) == 1, "a detail failure must not cost the caller the search row"
    assert rows[0].extra["hydrated"] is False
    assert any("details failed" in w for w in a.pop_warnings())


def test_only_the_first_n_rows_are_hydrated(monkeypatch):
    """One detail call per row, against a 35,000/day budget."""
    monkeypatch.setattr(E, "HYDRATE", 2)
    rows = [dict(SEARCH_ROW, designNumber="00563274%d-0001" % i) for i in range(5)]
    wire = Wire(rows=rows)
    _, out = search(wire)
    assert [c.extra["hydrated"] for c in out] == [True, True, False, False, False]
    details = [r for r in wire.calls if "/designs/" in str(r.url) and "/views/" not in str(r.url)]
    assert len(details) == 2


# --------------------------------------------------------------------------- details
def test_publication_date_is_the_earliest_bulletin_not_the_application_date():
    """Publication can be deferred, and it is the date the appearance became public that
    decides whether it is prior art. The bulletins are not returned in date order."""
    a = E.EUIPO()
    wire = Wire()

    async def go():
        async with wire.client() as c:
            return await a.details("005632742-0001", c)
    d = run(go())
    assert d["publication_date"] == "2018-09-19"
    assert d["application_date"] == "2018-09-07"
    assert d["deferred"] is False


def test_details_accepts_the_em_prefixed_number_the_candidates_carry():
    a = E.EUIPO()
    wire = Wire()

    async def go():
        async with wire.client() as c:
            return await a.details("EM005632742-0001", c)
    assert run(go())["design_number"] == "005632742-0001"
    req = next(r for r in wire.calls if "/designs/" in str(r.url))
    assert "/designs/005632742-0001" in str(req.url), "the EM prefix must be stripped for the API"


def test_a_view_is_fetched_as_image_bytes():
    a = E.EUIPO()
    wire = Wire()

    async def go():
        async with wire.client() as c:
            return await a.view_bytes("EM005632742-0001", 1, client=c, thumbnail=True)
    assert run(go()).startswith(b"\xff\xd8\xff")
    req = next(r for r in wire.calls if "/views/" in str(r.url))
    assert str(req.url).endswith("/designs/005632742-0001/views/1/thumbnail")


# --------------------------------------------------------------------------- wiring
def test_the_adapter_is_registered():
    assert "euipo" in sources.registry()


def test_it_reports_why_it_is_off_without_credentials(monkeypatch):
    monkeypatch.delenv("EUIPO_KEY", raising=False)
    monkeypatch.delenv("EUIPO_SECRET", raising=False)
    a = E.EUIPO()
    assert not a.enabled()
    assert "EUIPO_KEY" in a.disabled_reason()


def test_no_em_dash_reaches_the_customer_facing_notes():
    """House rule, and corpus_profile renders these on the /corpus page."""
    a = E.EUIPO()
    for s in (a.search_note(), a.disabled_reason(), E.EUIPO.__doc__ or ""):
        assert "—" not in s


def test_designs_are_not_planned_into_the_patent_fan_out():
    """DELIBERATE, and this test is the latch on it.

    `external.materialise()` inserts every surviving candidate into the `publications`
    corpus. A registered design has no claims, no description and no abstract, so wiring
    euipo into `external.plan()` would put thousands of permanently text-less rows into the
    corpus whose text-depth accounting is the engine's whole quality story. Designs are
    answered on their own terms instead. If someone adds euipo to the planner, they have to
    delete this test and read why first.
    """
    import external
    plan = external.plan([], brief="a handheld vacuum gripper with a suction cup", claims=[])
    assert not [q for q in plan["queries"] if q.get("source") == "euipo"]


# --------------------------------------------------------------------------- web routes
def test_the_designs_page_renders(app_client):
    r = app_client.get("/designs")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "EU registered designs" in html
    #  The page must say what the right actually covers. A reader who thinks these are
    #  patents will read a claim chart into a drawing.
    assert "not how it works" in html


def test_the_designs_api_refuses_an_empty_query(app_client):
    assert app_client.get("/api/designs").status_code == 400
    assert app_client.get("/api/designs?q=%20").status_code == 400


def test_a_query_with_no_distinctive_words_says_why(app_client, monkeypatch):
    """The adapter refuses to match the whole register; the route must pass that reason on
    rather than rendering an empty grid that reads as 'the EU holds nothing like this'."""
    r = app_client.get("/api/designs?q=the%20and%20of")
    assert r.status_code == 400
    assert "euipo" in (r.get_json() or {}).get("error", "").lower()


def test_an_upstream_failure_is_a_502_with_the_reason_not_an_empty_list(app_client, monkeypatch):
    """A dead credential must be visible. Reporting zero designs would be indistinguishable
    from a genuine no-match, which is how EUIPO stayed dark for a month in the first place."""
    from sources import euipo as _e

    def boom(*a, **k):
        raise httpx.ConnectError("upstream is down")
    monkeypatch.setattr(_e, "search_designs", boom)
    r = app_client.get("/api/designs?q=suction%20gripper")
    assert r.status_code == 502
    assert "upstream is down" in (r.get_json() or {}).get("error", "")


@pytest.mark.parametrize("bad", [
    "../../secrets", "005632742", "abc-0001", "005632742-0001/../x", "%2e%2e",
])
def test_the_image_proxy_refuses_anything_that_is_not_a_design_number(app_client, bad):
    """The number is interpolated into an upstream URL path, so it is validated, not escaped."""
    assert app_client.get("/api/designs/%s/view/1" % bad).status_code == 404


def test_the_image_proxy_serves_a_drawing(app_client, monkeypatch):
    from sources import euipo as _e
    seen = {}

    def fake(num, order, thumbnail=True):
        seen.update(num=num, order=order, thumbnail=thumbnail)
        return JPEG
    monkeypatch.setattr(_e, "design_view", fake)
    r = app_client.get("/api/designs/005632742-0001/view/2")
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"
    assert r.data == JPEG
    #  Thumbnails by default: the full view measured 1.5 MB on the record checked 2026-08-23.
    assert seen == {"num": "005632742-0001", "order": 2, "thumbnail": True}
    assert "max-age" in r.headers.get("Cache-Control", "")


def test_the_full_view_is_available_when_asked_for(app_client, monkeypatch):
    from sources import euipo as _e
    seen = {}
    monkeypatch.setattr(_e, "design_view",
                        lambda n, o, thumbnail=True: (seen.update(t=thumbnail), JPEG)[1])
    assert app_client.get("/api/designs/005632742-0001/view/1?full=1").status_code == 200
    assert seen["t"] is False


def test_euipo_declares_itself_outside_the_report_fan_out():
    """The /corpus page groups sources into 'searched' and 'not available'. EUIPO is neither:
    it is live, but a patent report never consults it. Without this flag the page said a
    report searched the EU design register when it did not, which is precisely the kind of
    overclaim the disclosure layer exists to prevent."""
    import sources
    a = sources.registry()["euipo"]
    assert a.enabled() and a.search_available()
    assert a.in_report_fanout is False
    #  every other source IS part of a report
    assert all(getattr(x, "in_report_fanout", True)
               for x in sources.all_adapters() if x.name != "euipo")


def test_health_reports_the_fan_out_flag():
    import sources
    row = next(s for s in sources.health()["sources"] if s["name"] == "euipo")
    assert row["in_report_fanout"] is False
    assert row["search_available"] is True


def test_the_corpus_page_reconciles_its_source_table_with_the_designs_page(app_client):
    """The source table on /corpus describes the retrieval ENGINE's fan-out, a separate app
    that has no EUIPO adapter, so it correctly reports EUIPO as not available. This app does
    have one, on its own page. Without a line reconciling the two, a reader sees 'EUIPO: not
    available' next to a Designs tab in the nav and cannot tell which is true."""
    html = app_client.get("/corpus").get_data(as_text=True)
    assert "are searched separately, on the" in html
    assert "/designs" in html


def test_the_designs_page_is_reachable_from_every_page(app_client):
    assert 'href="/designs"' in app_client.get("/corpus").get_data(as_text=True)
