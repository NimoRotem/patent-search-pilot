"""What the intake was given, and how it tells.

The page used to ask. It does not any more, and that makes `recognise` load-bearing: a
description becomes "write the first draft of this application", and an existing draft becomes
"take the draft in input/ and improve it, do not discard the user's own text". Read the second as
the first and an agent rewrites somebody's application from scratch.

So the cases here are the ones that are easy to get wrong in each direction: a long careful
description that is still not an application, and an application that arrives without the one
signal you were counting on.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import draft_intake


# =============================================================================================
# Material
# =============================================================================================
DESCRIPTION = """
A handheld vacuum lifting tool for moving glass panels. The grip itself is hollow and forms the
vacuum reservoir, so the pump does not have to run continuously to hold a panel. A battery driven
pump evacuates the reservoir through a check valve, and a pressure sensor in the reservoir tells
the controller to restart the pump when the pressure rises past a threshold.

The advantage over what is sold today is that the operator hears the pump far less often and the
tool keeps its grip for several seconds after the battery is removed, which is the case that
worries people. I would also like to cover a version where the reservoir is a separate cartridge
that clips into the handle, and one where the sensor is a simple mechanical switch.
"""

APPLICATION = """
VACUUM LIFTING DEVICE HAVING A HOLLOW HANDLE RESERVOIR

CROSS-REFERENCE TO RELATED APPLICATIONS

This application claims priority to U.S. Provisional Application No. 63/123,456.

FIELD OF THE INVENTION

The present invention relates to vacuum lifting devices, and more particularly to a handheld
vacuum lifting device in which the handle defines a vacuum reservoir.

BACKGROUND

Vacuum lifters are used to move glass and stone panels. In one embodiment of the prior art the
pump runs continuously.

BRIEF DESCRIPTION OF THE DRAWINGS

FIG. 1 is a perspective view of the device.
FIG. 2 is a section through the handle.
FIG. 3 is a schematic of the control circuit.

DETAILED DESCRIPTION

Referring to FIG. 1, the device 10 includes a hollow grip portion 12 defining a reservoir 14. A
suction cup 16 is carried by the grip portion 12 and communicates with the reservoir 14 through a
passage 18. A pump 20 driven by a motor 22 evacuates the reservoir 14 through a check valve 24. A
pressure sensor 26 in the reservoir 14 reports to a controller 28.

WHAT IS CLAIMED IS:

1. A handheld vacuum lifting device comprising: a hollow grip portion defining an evacuated
reservoir; a suction cup carried by the grip portion; a pump driven by a motor and in fluid
communication with the reservoir; and a controller configured to restart the pump.

2. The device of claim 1, wherein the reservoir is sealed by a check valve.

3. The device of claim 1, wherein the controller restarts the pump above a threshold pressure.
"""


# =============================================================================================
# Recognising what arrived
# =============================================================================================
def test_a_description_of_an_invention_is_not_an_application():
    found = draft_intake.recognise(DESCRIPTION)
    assert found["kind"] == "description"
    assert found["score"] < draft_intake.RECOGNISE_THRESHOLD


def test_an_application_is_recognised_and_says_which_signals_it_found():
    found = draft_intake.recognise(APPLICATION)
    assert found["kind"] == "existing_draft"
    assert found["evidence"]["claims"] >= 2
    assert found["evidence"]["headings"] >= 4
    assert found["evidence"]["numerals"] >= 8
    joined = " ".join(found["signals"])
    assert "claims" in joined and "headings" in joined and "numerals" in joined


def test_an_application_survives_losing_its_claims():
    """A specification pasted without the claim set is still an application, and rewriting it
    from scratch would throw away the drafting the person came here with."""
    body = APPLICATION.split("WHAT IS CLAIMED IS:")[0]
    assert draft_intake.recognise(body)["kind"] == "existing_draft"


def test_a_claim_set_on_its_own_is_an_application():
    """The other half. Claims with no specification are the most obviously drafted thing there
    is, and there are three independent signals in them."""
    claims = APPLICATION.split("WHAT IS CLAIMED IS:")[1]
    assert draft_intake.recognise("WHAT IS CLAIMED IS:" + claims)["kind"] == "existing_draft"


def test_one_heading_over_some_notes_is_not_an_application():
    """The false positive that would cost the most: somebody organising their own notes."""
    notes = "BACKGROUND\n\n" + DESCRIPTION
    assert draft_intake.recognise(notes)["kind"] == "description"


def test_a_stray_measurement_is_not_a_reference_numeral():
    """"a pressure of 40 kPa", "a 12 volt battery". Numerals are counted because prose does not
    normally put a bare number after a noun, and units are exactly where it does."""
    text = ("The pump draws 12 W and holds 40 kPa across a 300 mm panel over 30 s, with a mass "
            "of 2 kg and a reach of 3 m. " * 4)
    found = draft_intake.recognise(text)
    assert found["kind"] == "description"


def test_the_threshold_needs_more_than_one_kind_of_signal():
    """Any single signal is reachable by accident; three at once is not."""
    assert draft_intake.RECOGNISE_THRESHOLD >= 3
    only_figures = "The device works as shown. FIG. 1 shows it. FIG. 2 shows it from the side."
    assert draft_intake.recognise(only_figures)["kind"] == "description"


def test_empty_input_is_a_description_and_is_reported_as_certain():
    found = draft_intake.recognise("")
    assert found["kind"] == "description"
    assert found["signals"] == []
    assert found["confident"] is True


def test_a_borderline_reading_does_not_claim_to_be_certain():
    found = draft_intake.recognise(APPLICATION)
    assert found["confident"] is True                      # a real application clears it easily
    edge = "BACKGROUND\n\n1. A device comprising a widget.\n\nThe widget 10 is round."
    assert draft_intake.recognise(edge)["confident"] is False


def test_the_sentence_the_page_prints_says_what_it_means_for_the_agent():
    said = draft_intake.describe(draft_intake.recognise(APPLICATION))
    assert "already have" in said
    assert "keep your text" in said
    said = draft_intake.describe(draft_intake.recognise(DESCRIPTION))
    assert "description of an invention" in said
    assert "write the application" in said


def test_nothing_the_page_prints_carries_an_em_dash():
    for text in (DESCRIPTION, APPLICATION, ""):
        assert "—" not in draft_intake.describe(draft_intake.recognise(text))


# =============================================================================================
# What a draft takes out of an extraction
# =============================================================================================
def extraction(**over):
    base = {
        "ok": True, "source": "upload", "label": "US9108319.pdf",
        "title": "Vacuum lifting device", "full_text": APPLICATION,
        "abstract": "A handheld vacuum lifting device.",
        "claims": [{"claim_no": 1, "text": "A handheld vacuum lifting device comprising a grip."}],
        "figure_images": [{"mime": "image/png", "b64": base64.b64encode(b"PNG-1").decode()},
                          {"mime": "image/png", "b64": base64.b64encode(b"PNG-2").decode()}],
        "figure_descriptions": "FIG. 1 shows the device.",
        "publication_number": "US-9108319-B2", "drawings_source": "Google Patents",
        "notes": ["3 page(s) read"], "verified": True,
        "summary_brief": "A model's summary of the whole document.",
    }
    base.update(over)
    return base


def test_a_draft_starts_from_the_verbatim_document_not_the_summary():
    """The search runs on the condensed brief. An application drafted from a summary has no
    support for anything the summary dropped, and support is the whole of what a draft owes."""
    material = draft_intake.material_from_extract(extraction())
    assert material["text"].strip().startswith("VACUUM LIFTING DEVICE")
    assert "A model's summary" not in material["text"]
    assert material["recognised"]["kind"] == "existing_draft"


def test_the_drawings_come_with_it():
    material = draft_intake.material_from_extract(extraction())
    assert material["figures"] == [b"PNG-1", b"PNG-2"]
    assert material["drawings_source"] == "Google Patents"


def test_a_link_with_no_full_text_is_assembled_from_what_the_detail_path_did_give():
    """`ingest_input` stashes full text for an upload and not for a link, and a patent LINK is
    exactly how somebody starts from an existing patent. Title, abstract and claims are still a
    document worth drafting from; refusing here would refuse the main use."""
    material = draft_intake.material_from_extract(extraction(full_text="", source="link"))
    assert "Vacuum lifting device" in material["text"]
    assert "ABSTRACT" in material["text"]
    assert "CLAIMS" in material["text"]
    assert "A handheld vacuum lifting device comprising a grip." in material["text"]


def test_a_broken_figure_is_skipped_rather_than_losing_the_document():
    material = draft_intake.material_from_extract(
        extraction(figure_images=[{"b64": "!!!not base64!!!"},
                                  {"b64": base64.b64encode(b"PNG-2").decode()}]))
    assert material["figures"] == [b"PNG-2"]


def test_the_figure_count_is_capped():
    many = [{"b64": base64.b64encode(b"x").decode()} for _ in range(200)]
    material = draft_intake.material_from_extract(extraction(figure_images=many))
    assert len(material["figures"]) == draft_intake.MAX_FIGURES


# =============================================================================================
# Holding it between the upload and the submit
# =============================================================================================
@pytest.fixture
def stash(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_intake, "STASH", tmp_path / "intake")
    return tmp_path / "intake"


def test_an_extraction_survives_the_round_trip_with_its_drawings(stash):
    material = draft_intake.material_from_extract(extraction())
    token = draft_intake.stash(material)
    back = draft_intake.load(token)
    assert back["text"] == material["text"]
    assert back["figures"] == [b"PNG-1", b"PNG-2"]
    assert back["recognised"]["kind"] == "existing_draft"
    assert draft_intake.figure_path(token, 0).read_bytes() == b"PNG-1"


def test_an_unknown_token_is_a_miss_not_a_crash(stash):
    assert draft_intake.load("deadbeef") is None
    assert draft_intake.figure_path("deadbeef", 0) is None
    #  and a token that is not hex cannot walk out of the store
    assert draft_intake.load("../../etc/passwd") is None


def test_an_extraction_nobody_submitted_is_swept(stash):
    token = draft_intake.stash(draft_intake.material_from_extract(extraction()))
    assert draft_intake.load(token) is not None
    assert draft_intake.sweep(now=9e18) == 1
    assert draft_intake.load(token) is None


def test_the_page_is_told_what_it_needs_and_not_the_figures_themselves(stash):
    material = draft_intake.material_from_extract(extraction())
    token = draft_intake.stash(material)
    shown = draft_intake.public(draft_intake.load(token), token,
                                lambda t, n: f"/drafts/start/extract/{t}/figure/{n}")
    assert shown["n_figures"] == 2
    assert shown["figures"] == [f"/drafts/start/extract/{token}/figure/0",
                                f"/drafts/start/extract/{token}/figure/1"]
    assert shown["kind"] == "existing_draft"
    assert "already have" in shown["recognised"]
    assert shown["publication_number"] == "US-9108319-B2"
    #  the payload is JSON, and a PNG in it would be megabytes on the wire
    assert "figures" not in json.dumps(shown)[:0] or all(
        isinstance(item, str) for item in shown["figures"])


def test_the_sheets_keep_the_numbering_the_source_gave_them():
    """A detailed description that refers to FIG. 3 needs the third sheet to BE FIG. 3."""
    assert draft_intake.figure_labels(3) == ["FIG. 1", "FIG. 2", "FIG. 3"]


# =============================================================================================
# Wiring
# =============================================================================================
def test_the_intake_page_no_longer_asks_which_kind_it_is():
    page = (Path(__file__).resolve().parents[1] / "templates" / "draft_start.html").read_text(
        encoding="utf-8")
    for gone in ("startopt", "inputKind", "An invention to describe", "A draft I already have",
                 "Where you are starting from"):
        assert gone not in page, gone
    #  one box, and the three ways into it
    assert 'id="startDrop"' in page and 'id="sourceDoc"' in page and 'id="docToken"' in page
    assert "paste a patent link or number" in page


def test_the_server_decides_the_kind_rather_than_trusting_the_form():
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    assert 'draft_intake.recognise(values["disclosure_text"])' in source
    assert 'values["input_kind"] = found["kind"]' in source
    #  and the form's own input_kind is no longer read
    assert '"input_kind", "priority_status"' not in source


def test_the_intake_reads_documents_through_this_apps_own_prefix():
    """/extract belongs to the search app at the root of this domain: it would read the document
    and stash it in another process, where nothing here could reach it."""
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    assert '@app.route("/drafts/start/extract", methods=["POST"])' in source
    assert '@app.route("/drafts/start/extract/status/<job>")' in source
    assert '@app.route("/drafts/start/extract/<token>/figure/<int:index>")' in source
    #  one reader, two consumers, rather than a second extraction path
    assert "def _search_extract_payload(res)" in source
    assert "ingest_input.extract_upload" in source


def test_the_drawings_are_attached_to_the_project():
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    assert "def _attach_intake_drawings(principal, project, intake)" in source
    assert "_attach_intake_drawings(principal, project, intake)" in source
    helper = source[source.index("def _attach_intake_drawings"):
                    source.index("@app.route(\"/drafts/start/extract\", methods=[\"POST\"])")]
    assert "draft_intake.figure_labels" in helper
    assert "upload_figure" in helper


# =============================================================================================
# A link to a publication we already hold
# =============================================================================================
def test_a_link_is_filled_out_from_the_corpus_copy(monkeypatch):
    """`extract_link` is built for a SEARCH, and a search runs on the brief, so for a publication
    it returns the abstract and stops. Measured on US-9108319-B2, which this corpus holds whole:
    535 characters, no claims, no drawings. Drafting from that is drafting from an abstract."""
    passages = [
        {"kind": "abstract", "label": "abstract", "text": "A suction cup assembly."},
        {"kind": "claim", "label": "claim 1",
         "text": "1. A suction cup assembly comprising a housing and an actuator."},
        {"kind": "claim", "label": "claim 2", "text": "2. The assembly of claim 1, sealed."},
        {"kind": "paragraph", "label": "paragraph 4",
         "text": "The housing 12 carries the actuator 14 against the flexible cup 16."},
    ]
    monkeypatch.setitem(__import__("sys").modules, "deep_analysis", type("M", (), {
        "full_text": staticmethod(lambda pub, max_chars=0: {
            "found": True, "title": "Electric suction cup", "passages": passages,
            "n_claims": 2, "n_paragraphs": 1})})())
    monkeypatch.setattr(draft_intake, "corpus_figures", lambda pub, cap=0: [b"A", b"B"])
    thin = draft_intake.material_from_extract(
        extraction(full_text="", source="link", figure_images=[], claims=[],
                   abstract="A suction cup assembly."))
    assert len(thin["text"]) < draft_intake.MIN_LINK_CHARS

    full = draft_intake.enrich_from_corpus(dict(thin))
    assert "WHAT IS CLAIMED IS:" in full["text"]
    assert "DETAILED DESCRIPTION" in full["text"]
    assert "ELECTRIC SUCTION CUP" in full["text"]
    assert full["figures"] == [b"A", b"B"]
    #  and the reading is redone on the document that actually arrived
    assert full["recognised"]["kind"] == "existing_draft"


def test_an_upload_is_never_second_guessed(monkeypatch):
    """A PDF already arrives with its text and its drawings. Going back to the corpus for it
    would replace the applicant's own document with our copy of a different one."""
    monkeypatch.setattr(draft_intake, "corpus_figures",
                        lambda pub, cap=0: pytest.fail("an upload must not be re-fetched"))
    material = draft_intake.material_from_extract(extraction())
    before = material["text"]
    assert draft_intake.enrich_from_corpus(material)["text"] == before


def test_the_assembled_document_reads_in_the_order_an_application_is_written():
    """`full_text` returns abstract, claims, description, which is the order a reader charting
    claims wants. The agent is handed this AS the document to improve, so it has to read as one."""
    text = draft_intake._document_from_passages("A clamp", [
        {"kind": "claim", "label": "claim 1", "text": "1. A clamp comprising a jaw."},
        {"kind": "abstract", "label": "abstract", "text": "A clamp."},
        {"kind": "paragraph", "label": "paragraph 2", "text": "The jaw 10 pivots."},
    ])
    assert text.index("A CLAMP") < text.index("ABSTRACT")
    assert text.index("ABSTRACT") < text.index("DETAILED DESCRIPTION")
    assert text.index("DETAILED DESCRIPTION") < text.index("WHAT IS CLAIMED IS:")


def test_one_stale_thumbnail_does_not_hide_the_rest_of_the_sheets(monkeypatch, tmp_path):
    """A card thumbnail downloads the LEAD drawing only, so a publication the results list has
    been scrolled past has exactly one file on disk. Measured: 1 local against 10 on the CDN, and
    a draft missing FIG. 2 to FIG. 10 refers to sheets nobody has."""
    import sys
    folder = tmp_path / "US-1-B2"
    folder.mkdir(parents=True)
    (folder / "000.png").write_bytes(b"LEAD")
    fake_display = type("M", (), {
        "FIGDIR": tmp_path,
        "_canonical_pubkey": staticmethod(lambda pub: "US-1-B2"),
        "_fig_ext": staticmethod(lambda url: ".png"),
        "remote_thumbs": staticmethod(lambda pub: [{"full": f"http://x/{n}.png"}
                                                   for n in range(10)]),
        #  Distinct bytes per sheet: real drawings differ, and identical ones are deduped.
        "_download": staticmethod(
            lambda url, dest, retries=2: bool(dest.write_bytes(b"REMOTE-" + url.encode()) or True)),
    })()
    fake_view = type("M", (), {
        "_cached_images": staticmethod(lambda pub: [{"file": "000.png"}])})()
    monkeypatch.setitem(sys.modules, "enrich_display", fake_display)
    monkeypatch.setitem(sys.modules, "webview", fake_view)
    assert len(draft_intake.corpus_figures("US-1-B2")) == 10


def test_the_local_store_still_wins_when_it_is_the_complete_one(monkeypatch, tmp_path):
    import sys
    folder = tmp_path / "US-2-B2"
    folder.mkdir(parents=True)
    for n in range(3):
        (folder / f"00{n}.png").write_bytes(b"LOCAL-%d" % n)
    fake_display = type("M", (), {
        "FIGDIR": tmp_path, "_canonical_pubkey": staticmethod(lambda pub: "US-2-B2"),
        "_fig_ext": staticmethod(lambda url: ".png"),
        "remote_thumbs": staticmethod(lambda pub: [{"full": "http://x/0.png"}]),
        "_download": staticmethod(lambda url, dest, retries=2: pytest.fail("no download needed")),
    })()
    fake_view = type("M", (), {"_cached_images": staticmethod(
        lambda pub: [{"file": f"00{n}.png"} for n in range(3)])})()
    monkeypatch.setitem(sys.modules, "enrich_display", fake_display)
    monkeypatch.setitem(sys.modules, "webview", fake_view)
    assert draft_intake.corpus_figures("US-2-B2") == [b"LOCAL-0", b"LOCAL-1", b"LOCAL-2"]


def test_the_same_sheet_under_two_names_is_one_sheet(monkeypatch, tmp_path):
    """The lead drawing is on disk twice: once as the card thumbnail the results list downloaded,
    once from the CDN set. Two identical FIG. 1s is a drawing objection nobody caused."""
    import sys
    folder = tmp_path / "US-3-B2"
    folder.mkdir(parents=True)
    (folder / "cdn000.png").write_bytes(b"LEAD")
    (folder / "mongo-00.png").write_bytes(b"LEAD")
    (folder / "mongo-01.png").write_bytes(b"SECOND")
    monkeypatch.setitem(sys.modules, "enrich_display", type("M", (), {
        "FIGDIR": tmp_path, "_canonical_pubkey": staticmethod(lambda pub: "US-3-B2"),
        "_fig_ext": staticmethod(lambda url: ".png"),
        "remote_thumbs": staticmethod(lambda pub: []),
        "_download": staticmethod(lambda url, dest, retries=2: False)})())
    monkeypatch.setitem(sys.modules, "webview", type("M", (), {
        "_cached_images": staticmethod(lambda pub: [
            {"file": "cdn000.png"}, {"file": "mongo-00.png"}, {"file": "mongo-01.png"}])})())
    assert draft_intake.corpus_figures("US-3-B2") == [b"LEAD", b"SECOND"]
