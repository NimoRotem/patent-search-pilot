"""Tests for the enrichment additions: legal-status tags, family grouping, PDF-drawing extraction,
multi-source PDF, and the (dormant-until-tokened) Lens client."""
import json
from pathlib import Path
import pytest
import status
import lens
import webview
import webapp
import enrich_display as ed


# ---- legal-status classifier ---------------------------------------------------------------
def test_status_from_kind_and_age():
    g = lambda kc, ctry, yr: status.classify_status(kc, ctry, f"{yr}-01-01", None, f"{yr}-06-01",
                                                    today_year=2026)["code"]
    assert g("B2", "US", 2015) == "granted"
    assert g("A1", "US", 2020) == "application"
    assert g("A1", "DE", 2005) == "application"
    assert g("B1", "EP", 1998) == "expired"        # granted but > 20y old -> likely expired
    assert g("S1", "US", 2021) == "design"
    assert g("U1", "DE", 2019) == "utility"
    assert g("A", "US", 1975) == "expired"         # old US 'A' grant, aged out


def test_status_explicit_legal_events_override():
    ev = [{"code": "PLFP", "title": "Fee payment"}, {"code": "ST", "title": "Lapsed for failure to pay"}]
    assert status.classify_status("B2", "US", "2020-01-01", None, "2021-01-01",
                                  legal_events=ev, today_year=2026)["code"] == "expired"
    ev2 = [{"code": "X", "title": "Application withdrawn"}]
    assert status.classify_status("A1", "EP", "2020-01-01", None, "2021-01-01",
                                  legal_events=ev2, today_year=2026)["code"] == "dead"


# ---- family grouping -----------------------------------------------------------------------
def test_cards_group_family_members(gold_slug):
    rep = json.loads((webapp.REPORTS / f"{gold_slug}.json").read_text())
    v = webview.build_view(rep, top_n=25)
    assert v["cards"]
    for c in v["cards"]:
        assert "family_members" in c and isinstance(c["family_members"], list)
        assert c["n_family"] == len(c["family_members"])
        for m in c["family_members"]:
            assert m["pub"] and m["pub"] != c["pub"]     # a DIFFERENT filing of the same family
            assert "flag" in m and "status" in m
    # at least one multi-jurisdiction family should be found in a gold report
    assert any(c["n_family"] > 0 for c in v["cards"])


# ---- Lens client (dormant without a token) -------------------------------------------------
def test_lens_dormant_without_token(monkeypatch):
    monkeypatch.setattr(lens, "TOKEN", "")
    assert lens.available() is False
    assert lens.fetch("US-10815075-B2") is None


def test_lens_pub_number_parsing():
    assert lens._parts("US-10815075-B2") == ("US", "10815075", "B2")
    assert lens._parts("EP-4048620-A1") == ("EP", "4048620", "A1")
    j, n, k = lens._parts("WO-2019012345")
    assert j == "WO" and n == "2019012345"


def test_lens_normalize_shapes_a_hit():
    raw = {"data": [{
        "lens_id": "000-000",
        "legal_status": {"patent_status": "Active", "anticipated_term_date": "2039-01-01"},
        "abstract": [{"lang": "en", "text": "A vacuum gripper."}],
        "claims": [{"claims": [{"claim_text": ["1. A device."]}]}],
        "families": {"simple_family": {"members": [
            {"document_id": {"jurisdiction": "EP", "doc_number": "123", "kind": "A1"}}]}},
    }]}
    n = lens.normalize("US-1-B2", raw)
    assert n["legal_status"] == "Active" and n["granted"] is True
    assert n["abstract"] == "A vacuum gripper." and n["claims"]
    assert n["family_members"][0]["pub"] == "EP-123-A1"


# ---- PDF drawing extraction ----------------------------------------------------------------
def test_extract_pdf_drawings_from_a_real_pdf(tmp_path, monkeypatch):
    pdfs = sorted(ed.PDFDIR.glob("*.pdf"), key=lambda p: -p.stat().st_size)
    if not pdfs:
        pytest.skip("no local PDF fixture to extract from")
    monkeypatch.setattr(ed, "FIGDIR", tmp_path)         # don't touch the real figure cache
    imgs = ed.extract_pdf_drawings(pdfs[0], "TEST-0001-A1", cap=6)
    assert isinstance(imgs, list)
    if imgs:                                            # a text-only PDF could yield none — that's OK
        assert all(i["file"].endswith(".png") and i.get("from_pdf") for i in imgs)
        assert (tmp_path / "TEST-0001-A1" / imgs[0]["file"]).exists()


def test_google_patents_id_zero_pads_us_pregrant_publications():
    """Stripping hyphens is right for a granted patent and WRONG for a US pre-grant publication.
    The corpus stores US-2015032252-A1 with the leading zero of the serial dropped, and Google
    Patents only resolves the padded US20150032252A1, so every pre-grant lookup asked for a
    document that does not exist, got nothing back, and still spent a SerpApi call.

    Measured live on five of them: 'no claims' at the old id, 22 to 39 claims at the new one. The
    field backfill's hit rate was 27% until this was fixed."""
    import enrich
    assert enrich.gp_id("US-2015032252-A1") == "patent/US20150032252A1/en"
    assert enrich.gp_id("US-2004094979-A1") == "patent/US20040094979A1/en"
    #  granted patents and non-US numbers are unchanged
    assert enrich.gp_id("US-11999030-B2") == "patent/US11999030B2/en"
    assert enrich.gp_id("DE-102010002317-A1") == "patent/DE102010002317A1/en"
    assert enrich.gp_id("US-5795001-A") == "patent/US5795001A/en"


def test_google_patents_id_survives_an_unparseable_number():
    import enrich
    assert enrich.gp_id("not-a-patent") == "patent/notapatent/en"


# --- the live path must leave a reserve too ---------------------------------------------------
def test_live_path_stops_at_the_reserve(monkeypatch):
    """Live searches and the background field backfill share ONE monthly SerpApi allowance.

    ops/enrich_field.py refuses to start without leaving a reserve; the live path had none, so the
    batch job stopped politely while searches kept spending until the allowance hit zero mid-run
    and on-demand full-text enrichment silently went dark.

    Driven through `may_spend` rather than `fetch_details`, because conftest.no_paid_apis stubs
    fetch_details out for every test -- a test that went through it would pass whether the guard
    existed or not.
    """
    import enrich
    monkeypatch.setattr(enrich, "SERP_KEY", "x")
    monkeypatch.setattr(enrich, "LIVE_RESERVE", 200)
    monkeypatch.setattr(enrich, "searches_left", lambda force=False: 150)
    assert enrich.may_spend() is False
    monkeypatch.setattr(enrich, "searches_left", lambda force=False: 201)
    assert enrich.may_spend() is True


def test_unreadable_quota_never_blocks_a_search(monkeypatch):
    """Refusing to search because we could not CHECK a quota is worse than overspending."""
    import enrich
    monkeypatch.setattr(enrich, "SERP_KEY", "x")
    monkeypatch.setattr(enrich, "searches_left", lambda force=False: None)
    assert enrich.may_spend() is True


def test_no_key_means_no_spend(monkeypatch):
    import enrich
    monkeypatch.setattr(enrich, "SERP_KEY", "")
    assert enrich.may_spend() is False


def test_searches_left_is_cached_and_fail_soft(monkeypatch):
    """The account read must never raise into a search, and must not be re-read per reference."""
    import enrich
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("serpapi down")

    monkeypatch.setattr(enrich, "SERP_KEY", "x")
    monkeypatch.setattr(enrich.requests, "get", boom)
    monkeypatch.setattr(enrich, "_quota", {"left": None, "at": 0.0})
    assert enrich.searches_left() is None
    assert enrich.searches_left() is None
    assert len(calls) == 1, "the second call must come from the cache"
