"""Shared fixtures. No test in this suite makes a live model call.

Every model interaction is either scripted or replaced with the SVG-reading double, so the
suite runs offline, costs nothing, and fails for exactly one reason: the code changed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pfc import numerals, parse  # noqa: E402
from pfc.extract import build_entities, entity_id_for  # noqa: E402
from pfc.profiles import load_profile  # noqa: E402
from pfc.schemas import (Evidence, PatentGraph, Relation, SourceDocument,  # noqa: E402
                         sha256_text)

SIMPLE_SYSTEM = """AN EXAMPLE SENSING SYSTEM

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 illustrates a block diagram of an example system 100.

DETAILED DESCRIPTION
The system 100 includes a sensor 120 and a controller 130. The sensor 120 communicates
measurement data to the controller 130.

As shown in FIG. 1, the system 100 contains the sensor 120 and the controller 130.

CLAIMS
1. A system comprising a sensor and a controller.
"""

HOUSING_SYSTEM = """A SENSING DEVICE

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 is a schematic diagram of the housing 110, the sensor 120, the controller 130 and the
actuator 140.
FIG. 2 is a flowchart of a method performed by the controller 130.

DETAILED DESCRIPTION
The sensor 120 is positioned within the housing 110 and communicates with the controller
130. The controller 130 transmits a command to the actuator 140.

Referring to FIG. 1, the housing 110 contains the sensor 120. The controller 130 controls
the actuator 140.

Referring to FIG. 2, the method includes receiving sensor data 502, processing the sensor
data 504, and actuating the actuator 506.

CLAIMS
1. A device comprising a housing, a sensor and a controller.
"""


def make_document(text: str, *, origin_label: str = "test") -> SourceDocument:
    normalized = parse.normalize(text)
    sections, paragraphs = parse.parse_sections(normalized)
    return SourceDocument(
        document_id=sha256_text(normalized)[:16],
        title=parse.find_title(normalized),
        origin="upload", origin_label=origin_label,
        sha256=sha256_text(normalized), sections=sections, paragraphs=paragraphs)


def make_graph(document: SourceDocument, relations: list[tuple[str, str, str, str]]
               ) -> tuple[PatentGraph, dict]:
    """Build a graph deterministically, with relations supplied as test data.

    ``relations`` are ``(subject numeral, predicate, object numeral, direction)``. Each one is
    given evidence from the first paragraph that mentions both numerals, so the graph a test
    works with is grounded the same way a real one is.
    """
    registry = numerals.build_registry(parse.description_paragraphs(document.paragraphs))
    graph = PatentGraph(
        document_sha256=document.sha256, entities=build_entities(registry),
        reference_registry={key: entry.canonical_name for key, entry in registry.items()})
    for subject, predicate, obj, direction in relations:
        paragraph = next(
            (p for p in document.paragraphs
             if subject in numerals.numerals_in(p.text) and obj in numerals.numerals_in(p.text)),
            document.paragraphs[0])
        graph.relations.append(Relation(
            id=f"rel_{subject}_{predicate}_{obj}",
            subject=entity_id_for(subject), predicate=predicate,  # type: ignore[arg-type]
            object=entity_id_for(obj), direction=direction,  # type: ignore[arg-type]
            evidence=[Evidence(section_id=paragraph.section_id, paragraph_id=paragraph.id,
                               quote_start=0, quote_end=min(120, len(paragraph.text)),
                               quote=paragraph.text[:120])],
            confidence=0.95))
    return graph, registry


@pytest.fixture
def profile():
    return load_profile("uspto_utility")


@pytest.fixture
def simple_document():
    return make_document(SIMPLE_SYSTEM)


@pytest.fixture
def housing_document():
    return make_document(HOUSING_SYSTEM)


@pytest.fixture
def simple_graph(simple_document):
    graph, registry = make_graph(simple_document, [
        ("100", "contains", "120", "none"),
        ("100", "contains", "130", "none"),
        ("120", "transmits_to", "130", "subject_to_object"),
    ])
    return graph, registry
