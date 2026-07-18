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
    for good in ["US-11999030-B2", "DE-202019005606-U1", "WO-2020193405-A1", "EP-2496850-A1"]:
        assert e._pubkey(good) == good


def test_enrich_display_bad_pub_is_graceful():
    import enrich_display as e
    # a caller passing an unsafe key gets a graceful no-details dict, not a traversal/crash
    disp = e.enrich_for_display("../../../etc/passwd")
    assert disp["no_details"] is True and disp["images"] == []
    assert e.load_cached("../../../etc/passwd") is None


def test_normal_figure_and_pdf_still_served(app_client):
    # regression guard: hardening must not break legitimate serving
    assert app_client.get("/figures/US-11207792-B2/000.png").status_code == 200
    assert app_client.get("/pdf/US-11207792-B2").status_code in (200, 302)
