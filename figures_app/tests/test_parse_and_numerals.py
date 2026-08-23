"""Parsing and reference-numeral extraction: the passes everything else depends on."""
from __future__ import annotations

from conftest import SIMPLE_SYSTEM, make_document

from pfc import numerals, parse


def test_sections_are_found_in_order():
    document = make_document(SIMPLE_SYSTEM)
    ids = [section.id for section in document.sections]
    assert "brief_drawings" in ids
    assert "detailed_description" in ids
    assert "claims" in ids
    assert ids.index("brief_drawings") < ids.index("detailed_description")


def test_every_paragraph_has_a_stable_identifier():
    document = make_document(SIMPLE_SYSTEM)
    ids = [paragraph.id for paragraph in document.paragraphs]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert all(pid.startswith("p") for pid in ids)
    # Parsing the same text twice gives the same identifiers.
    again = make_document(SIMPLE_SYSTEM)
    assert [p.id for p in again.paragraphs] == ids


def test_cover_page_citations_do_not_become_components():
    """A granted patent's cover is a page of name-then-number pairs. None are reference signs."""
    cover = (
        "SOME PATENT\n\n"
        "References Cited\n"
        "Hoogland 2004\nSmith 2010\nMacLaughlin 2011\nLert 2012\n\n"
        "DETAILED DESCRIPTION\n"
        "The gripper 100 includes a pad 210 that seals against the workpiece.\n")
    document = make_document(cover)
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    assert set(registry) == {"100", "210"}


def test_measurements_are_not_reference_signs():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The plate 210 is about 5 mm thick and is offset by 12 degrees from the shaft 220. "
        "See claim 3 and FIG. 4 for the arrangement of the plate 210.\n")
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    assert set(registry) == {"210", "220"}


def test_a_numeral_at_the_end_of_a_sentence_is_still_read():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The controller 130 drives the actuator 140. The actuator 140 moves the arm 150.\n")
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    assert set(registry) == {"130", "140", "150"}
    assert registry["140"].count == 2


def test_a_verb_before_the_name_is_not_part_of_the_name():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The housing 110 contains the sensor 120 and supports the bracket 130.\n")
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    assert registry["120"].canonical_name == "sensor"
    assert registry["130"].canonical_name == "bracket"


def test_drafting_variants_of_one_name_are_aliases_not_a_conflict():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The gripper 100 is shown. The vacuum gripper 100 seals against a surface. "
        "The vacuum gripper 100 has a pad.\n")
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    assert numerals.collisions(registry) == []
    assert "vacuum gripper" in registry["100"].canonical_name


def test_one_numeral_for_two_unrelated_things_is_a_conflict():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The sensor 120 measures pressure. The sensor 120 is mounted high. "
        "The controller 120 runs the program. The controller 120 is a processor.\n")
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    found = numerals.collisions(registry)
    assert len(found) == 1
    assert found[0]["numeral"] == "120"
    assert {"sensor", "controller"} <= {name.lower() for name in found[0]["names"]}


def test_claims_are_not_a_source_of_numerals():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\nThe sensor 120 is disclosed.\n\n"
        "CLAIMS\n1. A device comprising a widget 999.\n")
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    assert "999" not in registry
    assert "120" in registry


def test_a_collision_is_reported_with_a_quote_for_each_reading():
    """Two quotations of the same reading assert a collision and fail to show one.

    Modelled on US-2024/0324075-A1, where 124 really is both an impedance sensor and a first
    conductive substrate, and the report quoted the sensor twice.
    """
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The device 112 comprises a power supply 118, a processor 120, an impedance sensor 124 "
        "and a display interface 126. The impedance sensor 124 measures the coils 116.\n\n"
        "The sheet 130 comprises an adhesive substrate 102 surrounded by a first conductive "
        "substrate 124 and a second conductive substrate 126. The conductive substrate 124 "
        "and the conductive substrate 126 heat the adhesive substrate 102.\n")
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    found = {row["numeral"]: row for row in numerals.collisions(registry)}
    assert "124" in found

    readings = found["124"]["readings"]
    assert len(readings) == 2
    names = {reading["name"].lower() for reading in readings}
    assert any("impedance sensor" in name for name in names)
    assert any("conductive substrate" in name for name in names)

    # The load-bearing part: each quotation must contain the name it is offered as evidence for.
    for reading in readings:
        head = reading["name"].split()[-1].lower()
        assert head in reading["quote"].lower(), (
            f"the quote for {reading['name']!r} does not contain it: {reading['quote']!r}")
        assert reading["paragraph_id"]
        assert reading["uses"] >= 1
