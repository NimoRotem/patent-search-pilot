"""Front-door document / patent-link ingestion (upload + URL modes).

All hermetic: the pure parsers/guards hit no network, and the route tests only exercise the
REJECTION paths (size / content-type / URL sanitisation), which short-circuit before any Vertex
or SerpApi call. The fusion tests monkeypatch the LLM helpers so no paid API is touched.
"""
import io
import pytest

import ingest_input as ii


# ---------------------------------------------------------------------------
# publication-number normalisation + URL parsing (SSRF-safe: parse, never fetch)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("DE1286275B", "DE-1286275-B"),
    ("de-1286275-b", "DE-1286275-B"),
    ("DE 1286275 B", "DE-1286275-B"),
    ("US11999030B2", "US-11999030-B2"),
    ("US-11999030-B2", "US-11999030-B2"),
    ("EP2496850A1", "EP-2496850-A1"),
    ("WO2020193405A1", "WO-2020193405-A1"),
])
def test_normalize_pub_variants(raw, expected):
    assert ii.normalize_pub(raw) == expected


@pytest.mark.parametrize("bad", ["", None, "not a patent", "12345", "X", "A" * 60, "!!!", "http://x"])
def test_normalize_pub_rejects_junk(bad):
    assert ii.normalize_pub(bad) is None


@pytest.mark.parametrize("url,expected", [
    ("https://patents.google.com/patent/DE1286275B/en", "DE-1286275-B"),
    ("https://patents.google.com/patent/US11999030B2", "US-11999030-B2"),
    ("https://worldwide.espacenet.com/patent/search/publication/DE1286275B?q=pn%3DDE1286275B",
     "DE-1286275-B"),
    ("https://worldwide.espacenet.com/patent/search?q=pn%3DEP2496850A1", "EP-2496850-A1"),
    ("DE1286275B", "DE-1286275-B"),            # bare number
    ("US-11999030-B2", "US-11999030-B2"),
])
def test_parse_patent_ref_ok(url, expected):
    assert ii.parse_patent_ref(url) == expected


@pytest.mark.parametrize("hostile", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata SSRF
    "http://localhost:8631/admin",
    "file:///etc/passwd",
    "https://evil.example.com/patent/steal",       # unknown host, no /patent/<pub> pub match...
    "ftp://internal/secret",
    "https://patents.google.com/",                 # no pub
    "javascript:alert(1)",
    "x" * 500,                                     # oversized
    "",
    None,
])
def test_parse_patent_ref_rejects_hostile(hostile):
    # None of these should yield a publication number a fetcher could be pointed at.
    assert ii.parse_patent_ref(hostile) is None


def test_evil_host_with_patent_path_only_yields_pubnumber_not_host():
    # Even if an attacker embeds /patent/<x> in a hostile URL, we only ever pull out a pub
    # number and hand THAT to the known-host adapters — the host in the URL is never used.
    out = ii.parse_patent_ref("https://evil.example.com/patent/US1234567B2/en")
    assert out == "US-1234567-B2"          # a pub number, not a host — the adapter picks the host


# ---------------------------------------------------------------------------
# upload content sniffing + filename hygiene
# ---------------------------------------------------------------------------
def test_sniff_kind_real_pdf():
    assert ii.sniff_kind(b"%PDF-1.7\n...", "doc.pdf") == "pdf"


def test_sniff_kind_fake_pdf_rejected():
    # claims .pdf but no %PDF- magic -> refused, not coerced
    assert ii.sniff_kind(b"<html>gotcha", "malware.pdf") == "bad_pdf"
    assert ii.sniff_kind(b"MZ\x90\x00", "payload.pdf") == "bad_pdf"


def test_sniff_kind_docx_and_bad_docx():
    assert ii.sniff_kind(b"PK\x03\x04rest", "d.docx") == "docx"
    assert ii.sniff_kind(b"not a zip", "d.docx") == "bad_docx"


def test_sniff_kind_txt_and_unknown():
    assert ii.sniff_kind(b"just some plain text", "notes.txt") == "txt"
    assert ii.sniff_kind(b"\xff\xfe\x00\x01\x02binary", "mystery.bin") == "unknown"


@pytest.mark.parametrize("name,expect_contains", [
    ("../../etc/passwd", "passwd"),
    ("/abs/path/report.pdf", "report.pdf"),
    ("C:\\Users\\x\\evil.pdf", "evil.pdf"),
    ("na\x00me.pdf", "name.pdf"),
    ("", "document"),
])
def test_safe_label_strips_paths(name, expect_contains):
    lab = ii.safe_label(name)
    assert "/" not in lab and "\\" not in lab and "\x00" not in lab
    assert expect_contains in lab


# ---------------------------------------------------------------------------
# extract_upload guards (unit level — reached before any network/LLM call)
# ---------------------------------------------------------------------------
def test_extract_upload_empty():
    r = ii.extract_upload(b"", "x.pdf")
    assert r["ok"] is False and r["status"] == 400


def test_extract_upload_too_large():
    big = b"%PDF-" + b"0" * (ii.MAX_BYTES + 1)
    r = ii.extract_upload(big, "big.pdf")
    assert r["ok"] is False and r["status"] == 413


def test_extract_upload_fake_pdf():
    r = ii.extract_upload(b"<html>not a pdf</html>", "claims.pdf")
    assert r["ok"] is False and r["status"] == 415


def test_extract_upload_unknown_type():
    r = ii.extract_upload(b"\xff\xd8\xff\xe0binaryjunk", "photo.jpg")
    assert r["ok"] is False and r["status"] == 415


def test_extract_link_bad_ref():
    r = ii.extract_link("http://169.254.169.254/")
    assert r["ok"] is False and r["status"] == 400


# ---------------------------------------------------------------------------
# fusion: proceed with whatever modality is present (never block on a missing one)
# ---------------------------------------------------------------------------
def test_build_text_only(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "condense_for_search",
                        lambda t: {"disclosure": "a compact search brief about vacuum grippers",
                                   "title": "Vacuum gripper"})
    r = ii._build(text="some long extracted patent text " * 5, figures=[], source="upload",
                  label="doc.pdf", notes=[])
    assert r["ok"] is True
    assert "vacuum grippers" in r["brief"]
    assert r["figures_present"] is False and r["text_present"] is True
    assert r["vision"] == ""


def test_build_figures_only(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "condense_for_search", lambda t: {"disclosure": "", "title": ""})
    monkeypatch.setattr(llm, "describe_figures",
                        lambda blobs, context="", **k: "a clamping mechanism with a lever and pivot")
    monkeypatch.setattr(ii, "_thumb", lambda b, **k: "data:image/jpeg;base64,AAA")
    r = ii._build(text="", figures=[b"fakepng", b"fakepng2"], source="upload",
                  label="scan.pdf", notes=[])
    assert r["ok"] is True
    assert "clamping mechanism" in r["brief"]
    assert "folded into the query" in r["brief"].lower()
    assert r["text_present"] is False and r["figures_present"] is True
    assert len(r["thumbs"]) == 2


def test_build_nothing_usable(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "condense_for_search", lambda t: {"disclosure": "", "title": ""})
    r = ii._build(text="", figures=[], source="upload", label="empty.pdf", notes=[])
    assert r["ok"] is False and r["status"] == 422


# ---------------------------------------------------------------------------
# route-level guards via the Flask test client (rejection paths only)
# ---------------------------------------------------------------------------
def test_route_no_input(app_client):
    r = app_client.post("/extract", data={}, content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_route_fake_pdf_rejected(app_client):
    data = {"file": (io.BytesIO(b"<html>not a pdf"), "evil.pdf")}
    r = app_client.post("/extract", data=data, content_type="multipart/form-data")
    assert r.status_code == 415
    assert r.get_json()["ok"] is False


def test_route_hostile_url_rejected(app_client):
    r = app_client.post("/extract", data={"url": "http://169.254.169.254/latest/meta-data/"},
                        content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
