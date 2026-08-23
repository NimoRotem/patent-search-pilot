"""Ingestion: the ladder of text sources, and what it does when a rung is empty.

The search app's modules are stubbed out. This is about the order the compiler asks in and what
it does with the answers, which is where the failure was: it stopped at a scanned facsimile and
reported that no source had the description, without having asked the source that did.
"""
from __future__ import annotations

import pytest

from pfc import ingest, parse

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

    def fetch_fulltext(pub, timeout=120.0):
        calls.append(pub)
        return {"title": "Induction-assisted gluing system and method",
                "abstract": "A system for adhering objects.",
                "description": DESCRIPTION, "claims": CLAIMS, "source": "gpatents_direct"}

    monkeypatch.setattr(ingest.pilot, "fetch_fulltext", fetch_fulltext)
    return calls


def test_a_scanned_publication_is_read_from_the_full_text_ladder(stub):
    """The reported failure: the compiler stopped at the scan and drew nothing."""
    result = ingest.ingest_link("US-20240324075-A1")
    document = result.document
    sections = {section.id for section in document.sections}
    assert "detailed_description" in sections
    assert "brief_drawings" in sections
    assert stub == ["US-20240324075-A1"]
    assert any("gpatents_direct" in note for note in document.notes)


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
    monkeypatch.setattr(ingest.pilot, "fetch_fulltext", lambda pub, timeout=120.0: {})
    monkeypatch.setattr(ingest.pilot, "display_record", lambda pub: {
        "title": "Induction-assisted gluing system and method",
        "abstract": "A system for adhering objects with an induction-assisted adhesive.",
        "description": DESCRIPTION, "claims": [CLAIMS], "images": []})
    document = ingest.ingest_link("US-20240324075-A1").document
    assert any("publication record" in note for note in document.notes)
    assert "detailed_description" in {section.id for section in document.sections}


def test_a_publication_no_source_holds_says_so_plainly(stub, monkeypatch):
    monkeypatch.setattr(ingest.pilot, "fetch_fulltext", lambda pub, timeout=120.0: {})
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
