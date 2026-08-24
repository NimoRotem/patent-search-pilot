"""Ingestion: the ladder of text sources, and what it does when a rung is empty.

The search app's modules are stubbed out. This is about the order the compiler asks in and what
it does with the answers, which is where the failure was: it stopped at a scanned facsimile and
reported that no source had the description, without having asked the source that did.
"""
from __future__ import annotations

import pytest

from pfc import fulltext, ingest, parse

DESCRIPTION = (
    "Description\n"
    "FIELD OF TECHNOLOGY\n"
    "This disclosure relates to an induction-assisted gluing system.\n\n"
    "BRIEF DESCRIPTION OF THE DRAWINGS\n"
    "FIG. 1 is a schematic diagram of the gluing system 100.\n\n"
    "DETAILED DESCRIPTION\n"
    "The gluing system 100 includes an induction coil 110 and a controller 120. The "
    "induction coil 110 is positioned within a housing 130. The controller 120 controls the "
    "induction coil 110 to heat an adhesive layer 140.\n")
CLAIMS = "1. A gluing system comprising an induction coil and a controller.\n"


@pytest.fixture
def stub(monkeypatch):
    """A publication whose facsimile is a scan and whose record has no description."""
    calls: list[str] = []

    monkeypatch.setattr(ingest.pilot, "parse_patent_ref", lambda raw: "US-20240324075-A1")
    monkeypatch.setattr(ingest.pilot, "display_record", lambda pub: {
        "title": "Induction-assisted gluing system and method",
        "abstract": "A system for adhering objects with an induction-assisted adhesive.",
        "claims": [CLAIMS], "description": None,
        "images": [{"file": None, "full": "https://example.invalid/sheet0.png"}],
        "google_patents": "https://example.invalid/p", "espacenet": None})
    monkeypatch.setattr(ingest.pilot, "pdf_dir", lambda: None)
    monkeypatch.setattr(ingest.pilot, "figure_dir", lambda pub: None)

    # The compiler's own ladder: our stores first, then our reader, then the paid channel.
    monkeypatch.setattr(fulltext.pilot, "corpus_record", lambda pub, strict=False: {})
    monkeypatch.setattr(fulltext.pilot, "docstore_record", lambda pub, strict=False: {})

    def adapter_details(pub, adapter_name, timeout=90.0):
        calls.append(adapter_name)
        if adapter_name != "gpatents_direct":
            return {}
        return {"title": "Induction-assisted gluing system and method",
                "abstract": "A system for adhering objects.",
                "description": DESCRIPTION, "claims": CLAIMS}

    monkeypatch.setattr(fulltext.pilot, "adapter_details", adapter_details)
    return calls


def test_a_scanned_publication_is_read_from_the_full_text_ladder(stub):
    """The reported failure: the compiler stopped at the scan and drew nothing."""
    result = ingest.ingest_link("US-20240324075-A1")
    document = result.document
    sections = {section.id for section in document.sections}
    assert "detailed_description" in sections
    assert "brief_drawings" in sections
    assert stub[0] == "gpatents_direct", "our own reader is asked before any paid channel"
    assert any("Google Patents reader" in note for note in document.notes)


def test_the_numerals_survive_the_round_trip(stub):
    """The whole point of the description: without numerals nothing can be labelled."""
    from pfc import numerals

    document = ingest.ingest_link("US-20240324075-A1").document
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    assert set(registry) == {"100", "110", "120", "130", "140"}
    assert registry["110"].canonical_name == "induction coil"


def test_the_figure_the_patent_describes_is_found(stub):
    from pfc import plan as planning

    document = ingest.ingest_link("US-20240324075-A1").document
    figures = planning.discover_figures(document)
    assert [item.figure_number for item in figures] == ["1"]
    assert figures[0].figure_type == "block_diagram"


def test_the_record_is_used_only_when_the_ladder_is_empty(stub, monkeypatch):
    monkeypatch.setattr(fulltext.pilot, "adapter_details",
                        lambda pub, adapter_name, timeout=90.0: {})
    monkeypatch.setattr(ingest.pilot, "display_record", lambda pub: {
        "title": "Induction-assisted gluing system and method",
        "abstract": "A system for adhering objects with an induction-assisted adhesive.",
        "description": DESCRIPTION, "claims": [CLAIMS], "images": []})
    document = ingest.ingest_link("US-20240324075-A1").document
    assert any("publication record" in note for note in document.notes)
    assert "detailed_description" in {section.id for section in document.sections}


def test_a_publication_no_source_holds_says_so_plainly(stub, monkeypatch):
    monkeypatch.setattr(fulltext.pilot, "adapter_details",
                        lambda pub, adapter_name, timeout=90.0: {})
    monkeypatch.setattr(ingest.pilot, "display_record", lambda pub: {
        "title": "A patent", "abstract": "", "description": None, "claims": [], "images": []})
    with pytest.raises(ingest.IngestError) as raised:
        ingest.ingest_link("US-20240324075-A1")
    message = str(raised.value)
    assert "image-only scan" in message or "full text" in message
    assert "Upload the document" in message


def test_the_filed_drawings_survive_a_scanned_facsimile(tmp_path, monkeypatch, stub):
    """A scan has no words for the compiler; its sheets are still what the comparison needs."""
    pdf = tmp_path / "US-20240324075-A1.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really a pdf")
    monkeypatch.setattr(ingest.pilot, "pdf_dir", lambda: tmp_path)
    monkeypatch.setattr(ingest.pilot, "read_pdf",
                        lambda path: {"text": "", "text_layer": False, "n_pages": 5})
    monkeypatch.setattr(ingest.pilot, "figures_from_pdf",
                        lambda path: [b"\x89PNG-sheet-%d" % n for n in range(5)])

    result = ingest.ingest_link("US-20240324075-A1")
    assert len(result.original_images) == 5
    assert any("scan with no text layer" in note for note in result.document.notes)
    assert any("drawing sheet" in note for note in result.document.notes)
    # and the words still came from the ladder
    assert "detailed_description" in {s.id for s in result.document.sections}


def test_a_source_that_drops_its_headings_is_still_readable():
    """Composed sections: the document's own headings are kept, the missing ones supplied."""
    text = ingest._compose_sections(
        title="A patent", abstract="An abstract of some length to survive the filter.",
        description=DESCRIPTION, claims=CLAIMS)
    assert text.count("DETAILED DESCRIPTION") == 2      # the marker, and the document's own
    assert "BRIEF DESCRIPTION OF THE DRAWINGS" in text
    assert not text.lstrip().startswith("Description")   # the bare field label is dropped
    sections, _paragraphs = parse.parse_sections(text)
    ids = {section.id for section in sections}
    assert {"abstract", "brief_drawings", "detailed_description", "claims"} <= ids


# ---------------------------------------------------------------------------
# The usability gate. Forty thousand characters of background reads as a healthy
# document to any size check and cannot label a single figure.
# ---------------------------------------------------------------------------
TRUNCATED = (
    "TECHNOLOGICAL FIELD\n"
    "The present disclosure relates to grippers for gripping object surfaces.\n"
    "BACKGROUND\n"
    + ("The following are examples of publications relevant to the background of the "
       "presently disclosed subject matter, none of which describes the present invention "
       "or any part of it in any way whatsoever at all. ") * 40)

COMPLETE = (
    "TECHNOLOGICAL FIELD\nThe disclosure relates to vacuum grippers.\n"
    "BACKGROUND\nPrior devices are known.\n"
    "DETAILED DESCRIPTION\n"
    "The vacuum gripper 100 comprises a body 110 carrying a suction cup 120. The suction cup "
    "120 seals against a surface. A vacuum pump 130 is mounted on the body 110 and is fluidly "
    "connected to the suction cup 120. A pressure sensor 140 measures the pressure in the "
    "suction cup 120 and reports it to a controller 150.\n")


def test_a_truncated_description_is_refused_however_long_it_is():
    ok, found, why = fulltext.usability(TRUNCATED)
    assert not ok
    assert found == 0
    assert "truncated" in why or "numeral" in why


def test_a_complete_description_is_accepted():
    ok, found, _why = fulltext.usability(COMPLETE)
    assert ok
    assert found >= 5


def test_the_ladder_walks_past_a_source_that_returns_a_truncated_document(monkeypatch):
    """The reported failure on US-2024/0246200-A1: the first free rung returned 40,000
    characters of background with no numerals and the compiler accepted it."""
    asked: list[str] = []
    monkeypatch.setattr(fulltext.pilot, "corpus_record", lambda pub, strict=False: {})
    monkeypatch.setattr(fulltext.pilot, "docstore_record",
                        lambda pub, strict=False: {"description": TRUNCATED,
                                                   "claims": "1. A gripper."})

    def adapter_details(pub, adapter_name, timeout=90.0):
        asked.append(adapter_name)
        if adapter_name == "gpatents_direct":
            return {"title": "Vacuum Gripper", "description": COMPLETE, "claims": "1. A."}
        return {}

    monkeypatch.setattr(fulltext.pilot, "adapter_details", adapter_details)
    notes: list[str] = []
    got = fulltext.fetch("US-20240246200-A1", notes)
    assert got.ok
    assert got.source == "our Google Patents reader"
    assert "100" in got.description
    assert "gpatents_direct" in asked
    # and it says out loud why the cached answer was passed over
    assert any("truncated" in note for note in notes), notes


def test_pqai_is_never_asked():
    """Free, fast, and it truncates. That is a fine trade for a search and not for this."""
    names = [name for name, _call in fulltext._rungs()]
    assert not any("pqai" in name.lower() for name in names)
    assert "pqai" in fulltext.EXCLUDED


def test_our_own_stores_are_asked_before_anything_that_costs(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(fulltext.pilot, "corpus_record",
                        lambda pub, strict=False: {"description": COMPLETE, "claims": "1. A."})
    monkeypatch.setattr(fulltext.pilot, "docstore_record", lambda pub, strict=False: {})
    monkeypatch.setattr(fulltext.pilot, "adapter_details",
                        lambda pub, adapter_name, timeout=90.0: asked.append(adapter_name) or {})
    got = fulltext.fetch("US-1-A1", [])
    assert got.source == "our own corpus"
    assert asked == [], "nothing external should be asked once our own corpus answered"


def test_the_fullest_incomplete_answer_is_kept_when_every_source_falls_short(monkeypatch):
    """Better to compile what can be compiled and say it is incomplete than to refuse."""
    monkeypatch.setattr(fulltext.pilot, "corpus_record", lambda pub, strict=False: {})
    monkeypatch.setattr(fulltext.pilot, "docstore_record",
                        lambda pub, strict=False: {"description": TRUNCATED,
                                                   "claims": "1. A gripper."})
    monkeypatch.setattr(fulltext.pilot, "adapter_details",
                        lambda pub, adapter_name, timeout=90.0: {})
    notes: list[str] = []
    got = fulltext.fetch("US-1-A1", notes)
    assert got.ok
    assert got.source.endswith("(incomplete)")
    assert any("not enough to label a figure" in note for note in notes)


def test_every_stub_of_a_pilot_call_matches_the_real_signature():
    """A double that cannot be called the way the product calls it is a false green.

    ``_from_our_stores`` started passing ``strict=True``. The doubles here took ``pub`` only, so
    the call raised TypeError, the ladder caught it as "that source failed", and three tests
    reported the ladder skipping our own corpus as though that were the behaviour under test.
    The stub is a contract with the real function and nothing was checking it.
    """
    import inspect

    from pfc import pilot

    # (what the product calls, with what) for every pilot call the ladder makes.
    calls = [
        (pilot.corpus_record, ("US-1-A1",), {"strict": True}),
        (pilot.docstore_record, ("US-1-A1",), {"strict": True}),
        (pilot.display_record, ("US-1-A1",), {"strict": True}),
        (pilot.adapter_details, ("US-1-A1", "gpatents_direct"), {"timeout": 90.0}),
    ]
    for function, args, kwargs in calls:
        inspect.signature(function).bind(*args, **kwargs)

    # And the doubles those tests install have to accept the same call.
    doubles = [
        lambda pub, strict=False: {},
        lambda pub, adapter_name, timeout=90.0: {},
    ]
    inspect.signature(doubles[0]).bind("US-1-A1", strict=True)
    inspect.signature(doubles[1]).bind("US-1-A1", "gpatents_direct", timeout=90.0)
