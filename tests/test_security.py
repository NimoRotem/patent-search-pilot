"""Security regression tests — path-traversal on /figures + /pdf must be blocked, and the
enrich_display data layer must reject unsafe publication keys (defense-in-depth). (M8 §1)"""
import pytest

TRAVERSAL = [
    "../../../../etc/passwd",
    "..%2f..%2f..%2fetc%2fpasswd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "....//....//etc/passwd",
]


@pytest.mark.parametrize("attack", TRAVERSAL)
def test_figures_path_traversal_blocked(app_client, attack):
    r = app_client.get(f"/figures/US-11207792-B2/{attack}")
    assert r.status_code == 404
    assert b"root:" not in r.data           # no /etc/passwd leak


@pytest.mark.parametrize("attack", TRAVERSAL)
def test_pdf_path_traversal_blocked(app_client, attack):
    r = app_client.get(f"/pdf/{attack}")
    assert r.status_code in (404,)
    assert b"root:" not in r.data


def test_pubkey_rejects_unsafe_keys():
    import enrich_display as e
    for bad in ["../../etc/passwd", "US/../x", "US-1;rm -rf", "", None, "A" * 60,
                "US-1\x00.png", "..%2f", "/etc/passwd"]:
        with pytest.raises(ValueError):
            e._pubkey(bad)
    # valid publication numbers pass through unchanged
    for good in [
        "US-11999030-B2",
        "DE-202019005606-U1",
        "WO-2020193405-A1",
        "EP-2496850-A1",
        "US20220256273A1",
        "CN219950370U",
    ]:
        assert e._pubkey(good) == good


def test_web_pub_validator_accepts_canonical_and_compact_numbers():
    import webapp

    for good in ["US-11207792-B2", "US20220256273A1", "CN219950370U"]:
        assert webapp._safe_pub(good)
    for bad in ["US/20220256273/A1", "../../etc/passwd", "US.2022.A1", "US-1 A1", "A" * 41]:
        assert not webapp._safe_pub(bad)


def test_compact_and_canonical_publications_share_cache_paths():
    import enrich_display as e

    assert e._canonical_pubkey("US20220256273A1") == "US-20220256273-A1"
    assert e.cache_path("US20220256273A1") == e.cache_path("US-20220256273-A1")


def test_enrich_display_bad_pub_is_graceful():
    import enrich_display as e
    # a caller passing an unsafe key gets a graceful no-details dict, not a traversal/crash
    disp = e.enrich_for_display("../../../etc/passwd")
    assert disp["no_details"] is True and disp["images"] == []
    assert e.load_cached("../../../etc/passwd") is None


def test_normal_figure_and_pdf_still_served(app_client, monkeypatch, tmp_path):
    # regression guard: hardening must not break legitimate serving
    import enrich_display

    figdir = tmp_path / "figures"
    pdfdir = tmp_path / "pdfs"
    (figdir / "US-11207792-B2").mkdir(parents=True)
    pdfdir.mkdir()
    (figdir / "US-11207792-B2" / "000.png").write_bytes(b"canonical figure")
    (pdfdir / "US-11207792-B2.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.setattr(enrich_display, "FIGDIR", figdir)
    monkeypatch.setattr(enrich_display, "PDFDIR", pdfdir)

    assert app_client.get("/figures/US-11207792-B2/000.png").status_code == 200
    assert app_client.get("/pdf/US-11207792-B2").status_code == 200


def test_compact_publication_figure_is_served(app_client, monkeypatch, tmp_path):
    import enrich_display

    # The recovery worker writes the canonical DOCDB directory even when the result card and URL
    # use the compact identifier returned by an external provider.
    pubdir = tmp_path / "US-20220256273-A1"
    pubdir.mkdir()
    (pubdir / "000.png").write_bytes(b"compact publication figure")
    monkeypatch.setattr(enrich_display, "FIGDIR", tmp_path)

    response = app_client.get("/figures/US20220256273A1/000.png")
    assert response.status_code == 200
    assert response.data == b"compact publication figure"

    manifest = app_client.get("/api/figs?pubs=US20220256273A1").get_json()
    assert manifest["US20220256273A1"] == [{"file": "000.png", "from_pdf": False}]


def test_compact_publication_pdf_uses_canonical_cache(app_client, monkeypatch, tmp_path):
    import enrich_display

    (tmp_path / "US-20220256273-A1.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.setattr(enrich_display, "PDFDIR", tmp_path)

    assert app_client.get("/pdf/US20220256273A1").status_code == 200
    assert app_client.get("/api/pdfs?pubs=US20220256273A1").get_json() == {
        "US20220256273A1": True
    }
