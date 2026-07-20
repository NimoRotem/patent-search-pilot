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


# ===========================================================================
# FULL-TEXT QUERY CHUNKS — corpus-parity chunking, embedding, and the new
# multi-material return contract (summary_brief + chunks + figure_images).
# ===========================================================================
import chunker as _corpus_chunker


def _corpus_rows(title, abstract, claims, paragraphs, captions):
    """Reproduce chunker.run's per-publication row logic (kinds/clip/token) INLINE, so the
    parity test asserts against the corpus chunker's own algorithm — not a copy of ours."""
    clip, tok = _corpus_chunker._clip, _corpus_chunker._tok
    rows = []
    whole = clip((title or "") + (". " + abstract if abstract else ""), 2000)
    if whole:
        rows.append(("whole", whole, tok(whole)))
    if abstract:
        a = clip(abstract)
        rows.append(("abstract", a, tok(a)))
    for c in claims:
        own = clip(str(c))
        if own:
            rows.append(("claim_own", own, tok(own)))
    for p in paragraphs:
        t = clip(str(p))
        if t:
            rows.append(("paragraph", t, tok(t)))
    for cap in captions:
        t = clip(str(cap))
        if t:
            rows.append(("figure_caption", t, tok(t)))
    return rows


def test_chunk_parity_kinds_sizes_tokens():
    """Query chunks must be byte-identical to what the CORPUS chunker would emit for the same
    structured fields: same kinds, same clipped text, same token_count."""
    title = "Vacuum gripper for handling porous slabs"
    abstract = "An abstract about a suction cup gripper. " * 250         # > MAX so it gets clipped
    claims = ["1. A vacuum gripper comprising a suction cup and a pump.",
              "2. The gripper of claim 1, further comprising a sensor."]
    paragraphs = ["The invention relates to vacuum handling of porous stone slabs " * 20,
                  "A pump generates negative pressure at the suction interface " * 20]
    captions = ["FIG. 1 is a perspective view of the gripper."]

    ours = ii.build_query_chunks(title=title, abstract=abstract, claims=claims,
                                 paragraphs=paragraphs, figure_captions=captions)
    theirs = _corpus_rows(title, abstract, claims, paragraphs, captions)

    ours_tuples = [(c["kind"], c["text"], c["token_count"]) for c in ours]
    assert ours_tuples == theirs                       # exact parity: kind + clipped text + tokens
    # the long abstract really was clipped to MAX_CHARS (proves _clip parity, not a no-op)
    ab = next(c for c in ours if c["kind"] == "abstract")
    assert len(ab["text"]) == _corpus_chunker.MAX_CHARS
    # kinds are a subset of the corpus vocabulary
    assert set(c["kind"] for c in ours) <= {
        "whole", "abstract", "claim_own", "claim_resolved", "paragraph", "figure_caption"}


def test_chunk_cap_keeps_independent_claims_first():
    """A pathological document with more claims than the cap keeps independent claims and drops
    the least-informative tail; the count never exceeds MAX_QUERY_CHUNKS."""
    claims = ["1. An independent apparatus comprising A and B."]
    claims += [f"{i}. The apparatus of claim 1, wherein feature {i}." for i in range(2, 120)]
    chunks = ii.build_query_chunks(title="T", abstract="an abstract", claims=claims, paragraphs=[])
    assert len(chunks) <= ii.MAX_QUERY_CHUNKS
    kinds = [c["kind"] for c in chunks]
    # summary chunks are kept ABOVE dependent claims even in a claim-heavy spec
    assert "claim_own" in kinds and "abstract" in kinds and "whole" in kinds
    # the sole independent claim survived the cap and sorts first
    assert chunks[0]["kind"] == "claim_own" and chunks[0].get("independent")


def test_segment_free_text_finds_claims_and_paragraphs():
    text = (
        "A Better Vacuum Gripper\n\n"
        "Abstract\n"
        "A gripper using a compliant suction cup to lift porous slabs.\n\n"
        "Detailed Description\n"
        "The gripper includes a vacuum pump connected to a manifold that distributes negative "
        "pressure across several suction cups arranged in a grid pattern for stability.\n\n"
        "A control unit monitors the vacuum level and triggers an alarm on loss of grip so the "
        "operator is warned before the load is dropped.\n\n"
        "What is claimed is:\n"
        "1. A vacuum gripper comprising a suction cup and a vacuum pump.\n"
        "2. The gripper of claim 1, further comprising a vacuum sensor.\n"
    )
    title, abstract, claims, paras = ii._segment_text(text)
    assert "gripper" in title.lower()
    assert "suction cup" in abstract.lower()
    assert len(claims) == 2 and claims[0].startswith("1.")
    assert ii._is_independent_claim(claims[0]) and not ii._is_independent_claim(claims[1])
    assert len(paras) >= 2                                   # description split into paragraphs


def test_build_returns_chunks_and_images_contract(monkeypatch):
    """_build exposes the NEW contract: summary_brief + embedded chunks + figure_images, in
    addition to the legacy brief. Embedding uses the (mocked) corpus embedder at 768d."""
    import llm
    monkeypatch.setattr(llm, "condense_for_search",
                        lambda t: {"disclosure": "a compact brief about vacuum grippers",
                                   "title": "Vacuum gripper"})
    monkeypatch.setattr(llm, "describe_figures",
                        lambda blobs, context="", **k: "a suction cup and a lever")
    monkeypatch.setattr(ii, "_thumb", lambda b, **k: "data:image/jpeg;base64,AAA")
    text = ("What is claimed is:\n1. A vacuum gripper comprising a suction cup and a pump.\n"
            "2. The gripper of claim 1, further comprising a sensor.\n")
    r = ii._build(text=text, figures=[b"png1", b"png2"], source="upload", label="d.pdf", notes=[])
    assert r["ok"] is True
    # summary kept
    assert r["summary_brief"] == "a compact brief about vacuum grippers"
    # chunks present, each embedded at 768d, kinds valid
    assert r["chunks"] and r["n_chunks"] == len(r["chunks"])
    for c in r["chunks"]:
        assert c["vector"] is not None and len(c["vector"]) == ii.EMBED_DIM
        assert c["kind"] in {"whole", "abstract", "claim_own", "claim_resolved",
                             "paragraph", "figure_caption"}
    assert any(c["kind"] == "claim_own" for c in r["chunks"])
    # figure descriptions + raw images for the image channel
    assert r["figure_descriptions"] == "a suction cup and a lever"
    assert len(r["figure_images"]) == 2
    assert all(fi["mime"] == "image/png" and fi["b64"] for fi in r["figure_images"])


def test_build_figures_only_still_yields_image_channel(monkeypatch):
    """A scanned facsimile with NO extractable text and NO vision still returns ok with the raw
    figures exposed for image search (text channels get nothing, image channel carries it)."""
    import llm
    monkeypatch.setattr(llm, "condense_for_search", lambda t: {"disclosure": "", "title": ""})
    monkeypatch.setattr(llm, "describe_figures", lambda blobs, context="", **k: "")
    monkeypatch.setattr(ii, "_thumb", lambda b, **k: "data:image/jpeg;base64,AAA")
    r = ii._build(text="", figures=[b"facsimile"], source="link", label="DE-1286275-B", notes=[])
    assert r["ok"] is True
    assert r["chunks"] == []                     # no text -> no text chunks (honest)
    assert len(r["figure_images"]) == 1          # but the drawing is exposed to image search


def test_embed_query_chunks_failsoft(monkeypatch):
    import embed
    def boom(*a, **k):
        raise RuntimeError("vertex down")
    monkeypatch.setattr(embed, "embed_texts", boom)
    chunks = [{"kind": "whole", "text": "x", "token_count": 1}]
    out = ii.embed_query_chunks(chunks)
    assert out[0]["vector"] is None              # fail-soft: text kept, vector None
