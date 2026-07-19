"""Tests for the capabilities ported from the federated app.

Focus is the two genuinely pure, deterministic pieces — the drawing-vs-text page classifier
and the no-LLM language heuristic — plus the deterministic parts of the claim chart
(grounding gate, realignment, fallback) which are the anti-hallucination guarantee and
must not silently regress.

Page images are SYNTHESIZED rather than loaded from fixtures so the suite has no binary
assets and no dependency on any particular PDF being present on the box.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")

import image_utils          # noqa: E402
import translate            # noqa: E402
import claim_chart          # noqa: E402


# --------------------------------------------------------------------------- fixtures
def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_text_page(w=1000, h=1400, lines=40) -> bytes:
    """A page of 'text': many short ink runs per row => many ink<->paper transitions."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    top, gap = 80, (h - 160) // lines
    for i in range(lines):
        y = top + i * gap
        x = 80
        while x < w - 120:                       # word-like runs with gaps between
            run = 14
            d.rectangle([x, y, x + run, y + 12], fill=0)
            x += run + 6
    return _png(im)


def make_drawing_page(w=1000, h=1400) -> bytes:
    """A 'drawing': outlines only => very few transitions per inked row."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([200, 200, 800, 900], outline=0, width=6)
    d.ellipse([350, 380, 650, 680], outline=0, width=6)
    d.line([200, 1000, 800, 1000], fill=0, width=6)
    return _png(im)


def make_two_figure_sheet(w=1000, h=1600) -> bytes:
    """Two drawings stacked with a tall white gap => should split into two figures."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([200, 100, 800, 500], outline=0, width=6)      # figure 1
    d.ellipse([300, 200, 700, 420], outline=0, width=6)
    # gap of ~500px (>7% of 1600) here
    d.rectangle([200, 1050, 800, 1500], outline=0, width=6)    # figure 2
    d.line([250, 1200, 750, 1200], fill=0, width=6)
    return _png(im)


def make_blank_page(w=800, h=1000) -> bytes:
    from PIL import Image
    return _png(Image.new("L", (w, h), 255))


# ----------------------------------------------------- drawing-vs-text page classifier
def test_text_page_classified_as_text():
    st = image_utils.page_stats(make_text_page())
    assert st["ok"] and st["is_text"], st
    assert st["verdict"] == "text"
    assert st["median_transitions"] > image_utils.TEXT_TRANSITIONS


def test_drawing_page_not_classified_as_text():
    st = image_utils.page_stats(make_drawing_page())
    assert st["ok"] and not st["is_text"], st
    assert st["verdict"] == "drawing"
    assert st["median_transitions"] <= image_utils.TEXT_TRANSITIONS


def test_text_and_drawing_are_well_separated():
    """The threshold should sit in a wide gap, not squeak past one sample — otherwise it
    will flip on real scans."""
    t = image_utils.page_stats(make_text_page())["median_transitions"]
    d = image_utils.page_stats(make_drawing_page())["median_transitions"]
    assert t > d * 3, (t, d)


def test_blank_page_is_blank():
    assert image_utils.page_stats(make_blank_page())["verdict"] == "blank"


def test_extract_figures_rejects_text_pages():
    """The whole point of the port: a text page must yield NO figures."""
    assert image_utils.extract_figures(make_text_page()) == []


def test_extract_figures_rejects_blank_pages():
    assert image_utils.extract_figures(make_blank_page()) == []


def test_extract_figures_returns_a_figure_for_a_drawing_page():
    figs = image_utils.extract_figures(make_drawing_page())
    assert len(figs) >= 1
    assert all(f[:8].startswith(b"\x89PNG") for f in figs)


def test_extract_figures_splits_a_two_figure_sheet():
    figs = image_utils.extract_figures(make_two_figure_sheet())
    assert len(figs) == 2, f"expected 2 bands, got {len(figs)}"


def test_extract_figures_crops_tightly():
    """A cropped figure must be smaller than the source page (whitespace removed)."""
    from PIL import Image
    src = make_drawing_page()
    figs = image_utils.extract_figures(src)
    with Image.open(io.BytesIO(src)) as full, Image.open(io.BytesIO(figs[0])) as crop:
        assert crop.size[0] < full.size[0] and crop.size[1] < full.size[1]


def test_extract_figures_caps_bands():
    assert image_utils.MAX_BANDS == 8


# ------------------------------------------------- polarity + threshold calibration
def make_inverted_drawing_page() -> bytes:
    """A 1-bit stencil mask as `pdfimages` really emits it: ink WHITE on a black field."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(make_drawing_page())).convert("L")
    return _png(ImageOps.invert(im))


def test_inverted_stencil_mask_is_normalized():
    """Regression: pdfimages emits patent drawings as inverted stencils (ink_frac ~0.95).
    Without polarity normalization every one is discarded as a 'dense' page and the figure
    gallery comes back empty."""
    st = image_utils.page_stats(make_inverted_drawing_page())
    assert st["ink_frac"] < 0.5, f"polarity not normalized: {st}"
    assert st["verdict"] == "drawing", st


def test_inverted_drawing_still_yields_a_figure():
    assert len(image_utils.extract_figures(make_inverted_drawing_page())) >= 1


def test_normal_polarity_is_left_alone():
    """The inversion must trigger on mostly-dark images ONLY."""
    normal = image_utils.page_stats(make_drawing_page())
    inverted = image_utils.page_stats(make_inverted_drawing_page())
    assert abs(normal["ink_frac"] - inverted["ink_frac"]) < 0.01


@pytest.mark.parametrize("median_transitions,expected_text", [
    (12, False), (20, False), (26, False), (32, False), (46, False),   # measured drawings
    (80, True), (96, True), (140, True), (172, True),                  # measured text pages
])
def test_threshold_matches_measured_corpus_pages(median_transitions, expected_text):
    """Locks the recalibration to real measurements. The federated app's inherited value of
    16 misclassifies every drawing row in this table as text."""
    assert (median_transitions > image_utils.TEXT_TRANSITIONS) is expected_text


def test_threshold_sits_inside_the_observed_gap():
    """Observed populations: drawings top out at 46, text starts at 80. The threshold must
    sit strictly between so neither class is clipped."""
    assert 46 < image_utils.TEXT_TRANSITIONS < 80


def test_extract_figures_fails_closed_on_garbage():
    assert image_utils.extract_figures(b"not a png") == []
    assert image_utils.extract_figures(b"") == []


def test_process_fails_open_on_garbage():
    """process() must NOT lose an existing figure when PIL cannot read it."""
    assert image_utils.process(b"not a png") == b"not a png"


def test_process_drops_text_only_when_asked():
    txt = make_text_page()
    assert image_utils.process(txt, drop_text=True) is None
    assert image_utils.process(txt, drop_text=False) is not None


def test_process_returns_none_for_blank():
    assert image_utils.process(make_blank_page()) is None


# ------------------------------------------------------------- language heuristic
def test_english_patent_text_is_english():
    t = ("A method for gripping a stone slab, the method comprising the steps of "
         "positioning a vacuum lifter above the slab and applying a negative pressure "
         "to the sealing element, wherein the control unit is configured to monitor "
         "the pressure in the suction chamber according to claim 1.")
    assert translate.looks_nonenglish(t) is False


def test_german_patent_text_is_nonenglish():
    t = ("Verfahren zum Greifen einer Steinplatte, wobei das Verfahren die Schritte "
         "umfasst, dass ein Vakuumheber ueber der Platte positioniert wird und ein "
         "Unterdruck an dem Dichtelement angelegt wird, wobei die Steuereinheit dazu "
         "eingerichtet ist, den Druck in der Saugkammer zu ueberwachen.")
    assert translate.looks_nonenglish(t) is True


def test_french_patent_text_is_nonenglish():
    t = ("Procede de prehension d une plaque de pierre, le procede comprenant les etapes "
         "consistant a positionner un dispositif de levage sous vide au dessus de la plaque "
         "et a appliquer une depression sur les elements d etancheite de la chambre.")
    assert translate.looks_nonenglish(t) is True


def test_spanish_patent_text_is_nonenglish():
    t = ("Metodo para agarrar una losa de piedra, el metodo que comprende las etapas de "
         "posicionar un elevador de vacio por encima de la losa y aplicar una presion "
         "negativa a los elementos de sellado de la camara de succion.")
    assert translate.looks_nonenglish(t) is True


@pytest.mark.parametrize("label,text", [
    ("japanese", "真空吸盤を用いて石板を把持する方法であって、" * 6),
    ("cyrillic", "Способ захвата каменной плиты, включающий этапы " * 4),
    ("greek", "Μέθοδος συγκράτησης πλάκας πέτρας που περιλαμβάνει στάδια " * 3),
    ("hebrew", "שיטה לאחיזת לוח אבן הכוללת שלבים של מיקום מרים ואקום " * 3),
    ("arabic", "طريقة لإمساك لوح حجري تشمل خطوات وضع رافعة تفريغ فوق اللوح " * 3),
])
def test_non_latin_script_is_nonenglish(label, text):
    """Regression: the federated original tested `ord(ch) > 0x2E80`, which caught Japanese
    but silently passed Cyrillic/Greek/Hebrew/Arabic through as 'English'."""
    assert translate.looks_nonenglish(text) is True, label


def test_english_with_typographic_punctuation_stays_english():
    """The other half of the same fix: em dashes, curly quotes and ± live in the codepoint
    gap we deliberately do NOT count, so ordinary English patent prose must not trip it."""
    t = ("A method for gripping a stone slab — the so-called “vacuum lifter” — comprising "
         "positioning the device above the slab and applying a pressure of ±2 bar to the "
         "sealing element of the suction chamber.")
    assert translate.looks_nonenglish(t) is False


def test_short_text_is_not_flagged():
    """Too little signal -> must not guess (a false positive costs a wasted LLM call)."""
    assert translate.looks_nonenglish("Vakuumheber") is False
    assert translate.looks_nonenglish("") is False
    assert translate.looks_nonenglish(None) is False


def test_heuristic_is_pure_and_repeatable():
    t = "Verfahren zum Greifen einer Steinplatte mit einem Vakuumheber und der Steuereinheit fuer das System."
    assert translate.looks_nonenglish(t) == translate.looks_nonenglish(t)


# ------------------------------------------------------- claim chart (deterministic parts)
def _ref():
    return {
        "found": True, "pub": "EP-1-A1", "title": "Vacuum lifter",
        "passages": [
            {"kind": "abstract", "coord": {}, "label": "abstract",
             "text": "A vacuum lifting device for handling stone slabs using a suction plate."},
            {"kind": "claim", "coord": {"claim_no": 1}, "label": "claim 1",
             "text": "A vacuum lifter comprising a suction plate, a sealing lip arranged "
                     "circumferentially, and a control unit monitoring chamber pressure."},
        ],
    }


def test_grounded_accepts_a_real_quote():
    assert claim_chart._grounded("a sealing lip arranged circumferentially",
                                 _ref()["passages"][1]["text"]) is True


def test_grounded_rejects_a_hallucinated_quote():
    """The anti-hallucination gate: invented text must not pass."""
    assert claim_chart._grounded("a hydraulic accumulator coupled to a servo gearbox",
                                 _ref()["passages"][1]["text"]) is False


def test_grounded_threshold_matches_webapp():
    """Must stay in lockstep with webapp._ground_reads_on or the two surfaces disagree."""
    assert claim_chart.MIN_OVERLAP == 0.6


def test_grounded_rejects_empty_quote():
    assert claim_chart._grounded("", "anything at all here") is False


def test_locate_resolves_a_real_local_coordinate():
    loc = claim_chart._locate("a sealing lip arranged circumferentially", _ref()["passages"])
    assert loc["kind"] == "claim"
    assert loc["coord"] == {"claim_no": 1}
    assert loc["label"] == "claim 1"


def test_locate_returns_empty_for_unlocatable_quote():
    assert claim_chart._locate("hydraulic accumulator servo gearbox flywheel", _ref()["passages"]) == {}


def test_fallback_chart_never_claims_disclosed():
    """Without an LLM reading the text, 'disclosed' is not an honest verdict."""
    rows = claim_chart._fallback_chart(
        ["a suction plate for handling stone slabs", "a hydraulic servo gearbox"], _ref())
    assert len(rows) == 2
    assert all(r["verdict"] in ("partial", "absent") for r in rows)
    assert all(r["method"] == "deterministic" for r in rows)


def test_build_chart_with_no_local_text_returns_all_absent():
    """No text => an LLM could only hallucinate; every row must be absent, not invented."""
    res = claim_chart.build_chart(["some element"], "EP-0-A1",
                                  ref={"found": False, "passages": []})
    assert res["method"] == "no-text"
    assert all(r["verdict"] == "absent" for r in res["rows"])
    assert all(r["grounding"] == "no-reference-text" for r in res["rows"])


def test_build_chart_with_no_elements():
    assert claim_chart.build_chart([], "EP-1-A1", ref=_ref())["method"] == "none"


def test_build_chart_caps_elements():
    res = claim_chart.build_chart([f"element {i}" for i in range(40)], "EP-1-A1",
                                  ref={"found": False, "passages": []})
    assert len(res["rows"]) == claim_chart.MAX_ELEMENTS == 12


def test_build_chart_demotes_ungrounded_llm_rows(monkeypatch):
    """End-to-end anti-hallucination: a model that invents a quote gets demoted to absent."""
    def fake(system, user, max_tokens=0):
        return {"chart": [
            {"element": "a suction plate", "verdict": "disclosed",
             "quote": "using a suction plate", "confidence": 0.9},
            {"element": "a hydraulic servo gearbox", "verdict": "disclosed",
             "quote": "the hydraulic servo gearbox drives the flywheel assembly", "confidence": 0.95},
        ]}
    monkeypatch.setattr(claim_chart.llm, "chat_json", fake)
    res = claim_chart.build_chart(["a suction plate", "a hydraulic servo gearbox"],
                                  "EP-1-A1", ref=_ref())
    grounded, invented = res["rows"][0], res["rows"][1]
    assert grounded["verdict"] == "disclosed" and grounded["grounding"] == "verified"
    assert grounded["coord"] == {}                      # located in the abstract
    assert invented["verdict"] == "absent", "hallucinated quote was not demoted"
    assert invented["grounding"].startswith("dropped-")
    assert res["stats"]["demoted_ungrounded"] == 1


def test_build_chart_realigns_paraphrased_elements(monkeypatch):
    """The model often echoes a slightly reworded element; prefix match must recover it."""
    el = "a sealing lip arranged circumferentially around the plate"
    def fake(system, user, max_tokens=0):
        return {"chart": [{"element": el[:30] + " (reworded by the model)",
                           "verdict": "disclosed",
                           "quote": "a sealing lip arranged circumferentially",
                           "confidence": 0.8}]}
    monkeypatch.setattr(claim_chart.llm, "chat_json", fake)
    res = claim_chart.build_chart([el], "EP-1-A1", ref=_ref())
    assert res["rows"][0]["verdict"] == "disclosed"
    assert res["rows"][0]["coord"] == {"claim_no": 1}


def test_build_chart_falls_back_when_llm_returns_nothing(monkeypatch):
    """chat_json swallows exceptions and returns {} — that must not produce an empty chart."""
    monkeypatch.setattr(claim_chart.llm, "chat_json", lambda *a, **k: {})
    res = claim_chart.build_chart(["a suction plate for stone slabs"], "EP-1-A1", ref=_ref())
    assert res["method"] == "fallback"
    assert len(res["rows"]) == 1


def test_build_chart_clamps_bad_confidence(monkeypatch):
    monkeypatch.setattr(claim_chart.llm, "chat_json", lambda *a, **k: {"chart": [
        {"element": "a suction plate", "verdict": "banana",
         "quote": "using a suction plate", "confidence": "not-a-number"}]})
    res = claim_chart.build_chart(["a suction plate"], "EP-1-A1", ref=_ref())
    r = res["rows"][0]
    assert r["verdict"] in claim_chart.VERDICTS
    assert 0.0 <= r["confidence"] <= 1.0


def test_quote_is_truncated_to_40_words(monkeypatch):
    long_quote = " ".join(["suction"] * 80)
    monkeypatch.setattr(claim_chart.llm, "chat_json", lambda *a, **k: {"chart": [
        {"element": "a suction plate", "verdict": "disclosed",
         "quote": long_quote, "confidence": 0.5}]})
    res = claim_chart.build_chart(["a suction plate"], "EP-1-A1", ref=_ref())
    assert len(res["rows"][0]["quote"].split()) <= claim_chart.MAX_QUOTE_WORDS


# ------------------------------------------------------------------ sibling helpers
def test_split_and_sibling_candidates():
    import enrich
    assert enrich.split_pubnum("EP-1609990-A4") == ("EP-1609990", "A4")
    assert enrich.split_pubnum("US-11999030-B2") == ("US-11999030", "B2")
    cands = enrich.sibling_candidates("EP-1609990-A4")
    assert "EP-1609990-A1" in cands
    assert "EP-1609990-A4" not in cands           # never propose itself
    # granted specifications rank ahead of published applications: their claims are the
    # examined, operative scope
    assert cands[0] == "EP-1609990-B1"
    assert cands.index("EP-1609990-B1") < cands.index("EP-1609990-A1")


def test_kind_rank_prefers_granted_then_published_then_translations():
    import enrich
    assert enrich.kind_rank("B1") < enrich.kind_rank("A1") < enrich.kind_rank("T2")
    assert enrich.kind_rank("ZZ") == enrich._KIND_FALLBACK_RANK   # unknown kinds sort last


def test_kind_codes_missed_by_the_federated_whitelist_are_ranked():
    """B4/T5/T2 carry claims in this corpus but were absent from the federated app's
    hardcoded list; find_sibling_with_claims now reads the DB, and these must still rank."""
    import enrich
    for k in ("B4", "T5", "T2", "U8"):
        assert enrich.kind_rank(k) < enrich._KIND_FALLBACK_RANK, k


def test_sibling_candidates_handles_no_kind_suffix():
    import enrich
    assert enrich.split_pubnum("EP1609990") == ("EP1609990", "")
    assert enrich.sibling_candidates("") == []
