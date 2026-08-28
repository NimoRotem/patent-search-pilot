"""EPO OPS parser tests.

Two layers:
  * the hand-built samples (claims.xml / description.xml / ...) — the original schema-shape proof;
  * the VERBATIM live captures (real_*.xml, EP-2496850-A1 fetched from OPS 2026-07-19) — these
    exist because the hand-built fixtures did NOT match the real wire format, and the mismatch
    hid three parser bugs that made a live backfill silently produce garbage:
       1. all claims of a language arrive under ONE <claim> element as many <claim-text>
          children, so parsing by <claim> yielded one mega-blob per language with DE+FR+EN mixed;
       2. description paragraph numbers are inline "[0001]" text, not num= attributes;
       3. legal event code/desc live in ATTRIBUTES of <legal>, not in child elements, so the
          legal parser returned zero events for every real response.
Everything here runs offline — no credentials required.
"""
import pathlib
import pytest
import ops
import patent_text as pt

SAMPLES = pathlib.Path(ops.__file__).parent / "ops_samples"
REAL = pytest.mark.skipif(not (SAMPLES / "real_claims.xml").exists(),
                          reason="live OPS captures not present")


# --------------------------------------------------------------------------- hand-built samples
def test_ops_mock_fetch_parses_all_sections():
    d = ops.ops_fetch("EP-2496850-A1", mock=True)
    assert d["source"] == "mock"
    assert d["claims"] and d["paragraphs"] and d["images"]
    assert d["claims"][0]["claim_no"] == 1


def test_ops_claims_resolve_dependencies():
    d = ops.ops_fetch("EP-2496850-A1", mock=True)
    resolved = pt.resolve_claims(pt.split_claims("\n".join(c["text"] for c in d["claims"])))
    indep = [c for c in resolved if c["is_independent"]]
    assert len(indep) >= 1


# --------------------------------------------------------------- epodoc number formatting (404s)
def test_epodoc_strips_kind_code_for_fulltext():
    """OPS full-text 404s on the kind-coded number; the kind-less form is what we must send."""
    with_kind, kindless = ops.to_epodoc("EP-2496850-A1")
    assert with_kind == "EP2496850A1"
    assert kindless == "EP2496850"
    assert ops.to_epodoc("WO-2023004786-A1")[1] == "WO2023004786"


def test_want_for_skips_fulltext_on_national_docs():
    """OPS serves full text for EP/WO only — asking DE wastes requests and weekly budget."""
    assert "claims" in ops.want_for("EP-2496850-A1")
    assert "claims" in ops.want_for("WO-2023004786-A1")
    assert "claims" not in ops.want_for("DE-19622127-C1")
    assert "legal" in ops.want_for("DE-19622127-C1")


# ------------------------------------------------------------------------- verbatim live captures
@REAL
def test_real_claims_selects_one_language_not_all_three():
    """Regression: the EP claims response holds DE, FR and EN blocks. We must return exactly one
    language's claims — not all three concatenated."""
    body = (SAMPLES / "real_claims.xml").read_bytes()
    blocks = ops._claim_blocks(ops.ET.fromstring(body))
    assert {(l or "").upper() for l, _ in blocks} == {"DE", "FR", "EN"}

    claims, lang = ops.parse_claims(body)
    assert lang == "en", "LANG_PREF puts English first"
    assert len(claims) == 9, "nine real claims, not three language blobs"
    assert [c["claim_no"] for c in claims] == list(range(1, 10))
    assert claims[0]["text"].startswith("1. Suction cup")
    # no German or French text leaked into the English claim set
    joined = " ".join(c["text"] for c in claims)
    assert "Saugnapf" not in joined and "Ventouse" not in joined


@REAL
def test_real_claims_language_fallback_when_english_absent():
    """A DE-only response must yield German claims rather than an empty set."""
    body = (SAMPLES / "real_claims.xml").read_bytes()
    xml = body.decode("utf-8", "replace")
    de_only = xml.replace('lang="EN"', 'lang="XX"').replace('lang="FR"', 'lang="YY"')
    claims, lang = ops.parse_claims(de_only.encode())
    assert lang == "de" and len(claims) == 9
    assert "Saugnapf" in claims[0]["text"]


@REAL
def test_real_description_paragraphs_and_headings():
    body = (SAMPLES / "real_description.xml").read_bytes()
    paras, lang = ops.parse_description_full(body)
    assert lang == "de"
    assert len(paras) >= 45
    assert paras[0]["para_no"] == "0001"
    assert paras[0]["heading"] == "Hintergrund der Erfindung"
    assert "[0001]" not in paras[0]["text"], "inline paragraph marker must be stripped"
    # content lines that merely look short (the drawings list) must NOT be swallowed as headings
    assert any(p["text"].startswith("Fig.") for p in paras), "figure list kept as real text"


@REAL
def test_real_legal_events_parse_from_attributes():
    """Regression: returned 0 events for every real response before the attribute fix."""
    body = (SAMPLES / "real_legal.xml").read_bytes()
    events = ops.parse_legal(body)
    assert len(events) > 50
    first = events[0]
    assert first["code"] == "17P"
    assert first["desc"] == "REQUEST FOR EXAMINATION FILED"
    assert first["date"] == "2012-06-06"
    assert all(e.get("code") or e.get("desc") for e in events)
    # dates are ISO or None, never the 0001-01-01 migration placeholder
    assert all(e["date"] is None or e["date"][:4] != "0001" for e in events)


@REAL
def test_real_images_extracts_drawing_instances():
    body = (SAMPLES / "real_images.xml").read_bytes()
    imgs = ops.parse_images(body)
    assert imgs and all(i["link"] for i in imgs)
    assert any(i["pages"] > 0 for i in imgs)


# ------------------------------------------------------------------------------------- plumbing
def test_throttle_header_parsing():
    overall, svcs = ops.parse_throttle(
        "busy (images=green:100, inpadoc=green:45, other=green:1000, "
        "retrieval=green:100, search=green:15)")
    assert overall == "busy"
    assert svcs["retrieval"] == ("green", 100)
    assert svcs["search"] == ("green", 15)
    assert ops.parse_throttle(None) == (None, {})


def test_service_routing_for_throttle():
    assert ops._service_for("published-data/publication/epodoc/EP2496850/claims") == "retrieval"
    assert ops._service_for("legal/publication/epodoc/EP2496850/") == "inpadoc"
    assert ops._service_for("published-data/publication/epodoc/EP2496850/images") == "images"


def test_budget_accounting_is_weekly_and_bounded():
    st = ops.budget_state()
    assert st["week"] == ops._week_key()
    assert ops.budget_remaining() <= ops.WEEK_BYTE_LIMIT * ops.BUDGET_SOFT_FRAC
