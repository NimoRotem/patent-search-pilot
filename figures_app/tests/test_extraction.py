"""Graph extraction and the guards that stand between a model's reply and the graph."""
from __future__ import annotations

from conftest import make_document

from pfc import numerals, parse
from pfc.extract import extract_graph
from pfc.ground import make_grounder, statement
from pfc.providers.mock import ScriptedTextReasoner

DOC = """A SENSING SYSTEM

DETAILED DESCRIPTION
The system 100 includes a sensor 120 and a controller 130. The sensor 120 transmits
measurement data to the controller 130. The controller 130 is mounted on the frame 140.
"""


def build(reply, grounding_reply=None):
    document = make_document(DOC)
    description = parse.description_paragraphs(document.paragraphs)
    registry = numerals.build_registry(description)
    replies = {"patent_graph": reply}
    if grounding_reply is not None:
        replies["evidence_check"] = grounding_reply
    reasoner = ScriptedTextReasoner(replies)
    grounder = make_grounder(reasoner) if grounding_reply is not None else None
    graph = extract_graph(document, registry, description, reasoner, grounder=grounder)
    return document, graph


def paragraph_with(document, needle):
    return next(p for p in document.paragraphs if needle in p.text)


def test_entities_come_from_the_registry_not_from_the_model():
    document, graph = build({"relations": [], "shape_hints": [], "entity_types": []})
    assert {entity.reference_numeral for entity in graph.entities} == {"100", "120", "130", "140"}
    assert all(entity.numeral_status == "EXISTING" for entity in graph.entities)
    assert all(entity.evidence for entity in graph.entities)


def test_a_grounded_relation_is_kept():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build({"relations": [{
        "subject": "e120", "predicate": "transmits_to", "object": "e130",
        "direction": "subject_to_object", "paragraph_id": target.id,
        "quote": "The sensor 120 transmits measurement data to the controller 130",
        "confidence": 0.9}], "shape_hints": [], "entity_types": []})
    assert len(graph.relations) == 1
    relation = graph.relations[0]
    assert (relation.subject, relation.predicate, relation.object) == (
        "e120", "transmits_to", "e130")
    assert relation.direction == "subject_to_object"
    assert relation.evidence[0].paragraph_id == target.id
    assert relation.evidence[0].quote


def test_a_quotation_that_is_not_in_the_paragraph_is_refused():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build({"relations": [{
        "subject": "e120", "predicate": "connected_to", "object": "e130",
        "direction": "none", "paragraph_id": target.id,
        "quote": "the sensor is wired directly to the controller by a cable",
        "confidence": 0.9}], "shape_hints": [], "entity_types": []})
    assert graph.relations == []
    assert any("quoted words that are not in the cited paragraph" in row["reason"]
               for row in graph.discarded)


def test_an_entity_the_registry_does_not_carry_is_refused():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build({"relations": [{
        "subject": "e120", "predicate": "connected_to", "object": "e999",
        "direction": "none", "paragraph_id": target.id,
        "quote": "The sensor 120 transmits measurement data", "confidence": 0.9}],
        "shape_hints": [], "entity_types": []})
    assert graph.relations == []
    assert any("not in the reference registry" in row["reason"] for row in graph.discarded)


def test_a_predicate_outside_the_enumeration_is_refused():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build({"relations": [{
        "subject": "e120", "predicate": "telepathically_linked_to", "object": "e130",
        "direction": "none", "paragraph_id": target.id,
        "quote": "The sensor 120 transmits measurement data to the controller 130",
        "confidence": 0.9}], "shape_hints": [], "entity_types": []})
    assert graph.relations == []
    assert any("outside the enumeration" in row["reason"] for row in graph.discarded)


def test_an_arrow_is_stripped_from_a_predicate_that_carries_no_direction():
    document = make_document(DOC)
    target = paragraph_with(document, "mounted on")
    _document, graph = build({"relations": [{
        "subject": "e130", "predicate": "mounted_on", "object": "e140",
        "direction": "subject_to_object", "paragraph_id": target.id,
        "quote": "The controller 130 is mounted on the frame 140", "confidence": 0.9}],
        "shape_hints": [], "entity_types": []})
    assert len(graph.relations) == 1
    assert graph.relations[0].direction == "none"


def test_a_shape_the_text_does_not_state_is_refused():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build({"relations": [], "entity_types": [], "shape_hints": [{
        "entity_id": "e120", "shape": "cylindrical", "paragraph_id": target.id,
        "quote": "The sensor 120 transmits measurement data"}]})
    sensor = next(entity for entity in graph.entities if entity.id == "e120")
    assert sensor.shape_hint is None
    assert sensor.shape_hint_grounded is False


def test_a_shape_the_text_does_state_is_kept():
    text = ("A DEVICE\n\nDETAILED DESCRIPTION\n"
            "The housing 110 is rectangular and encloses the sensor 120.\n")
    document = make_document(text)
    description = parse.description_paragraphs(document.paragraphs)
    registry = numerals.build_registry(description)
    target = description[0]
    reasoner = ScriptedTextReasoner({"patent_graph": {
        "relations": [], "entity_types": [], "shape_hints": [{
            "entity_id": "e110", "shape": "rectangular", "paragraph_id": target.id,
            "quote": "The housing 110 is rectangular"}]}})
    graph = extract_graph(document, registry, description, reasoner)
    housing = next(entity for entity in graph.entities if entity.id == "e110")
    assert housing.shape_hint == "rectangular"
    assert housing.shape_hint_grounded is True


def test_the_grounding_pass_discards_what_the_paragraph_does_not_entail():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build(
        {"relations": [{
            "subject": "e120", "predicate": "controls", "object": "e130",
            "direction": "subject_to_object", "paragraph_id": target.id,
            "quote": "The sensor 120 transmits measurement data to the controller 130",
            "confidence": 0.95}], "shape_hints": [], "entity_types": []},
        grounding_reply={"supported": False, "confidence": 0.9,
                         "reason": "the paragraph states transmission, not control"})
    assert graph.relations == []
    assert any("not entailed by its own evidence" in row["reason"] for row in graph.discarded)


def test_the_grounding_pass_keeps_what_the_paragraph_does_entail():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build(
        {"relations": [{
            "subject": "e120", "predicate": "transmits_to", "object": "e130",
            "direction": "subject_to_object", "paragraph_id": target.id,
            "quote": "The sensor 120 transmits measurement data to the controller 130",
            "confidence": 0.9}], "shape_hints": [], "entity_types": []},
        grounding_reply={"supported": True, "confidence": 0.95, "reason": "stated directly"})
    assert len(graph.relations) == 1


def test_the_grounding_checker_is_shown_one_paragraph_and_one_sentence():
    """It must not be able to infer what the extractor wanted, or it is not independent."""
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    reasoner = ScriptedTextReasoner({
        "patent_graph": {"relations": [{
            "subject": "e120", "predicate": "transmits_to", "object": "e130",
            "direction": "subject_to_object", "paragraph_id": target.id,
            "quote": "The sensor 120 transmits measurement data to the controller 130",
            "confidence": 0.9}], "shape_hints": [], "entity_types": []},
        "evidence_check": {"supported": True, "confidence": 0.9, "reason": "yes"}})
    description = parse.description_paragraphs(document.paragraphs)
    registry = numerals.build_registry(description)
    extract_graph(document, registry, description, reasoner,
                  grounder=make_grounder(reasoner))
    checks = [context for task, context in reasoner.calls if task == "evidence_check"]
    assert len(checks) == 1
    context = checks[0]
    assert context.count("\n\n") == 1
    assert "confidence" not in context
    assert "e120" not in context and "transmits_to" not in context


def test_a_relation_reads_back_as_a_plain_sentence():
    document = make_document(DOC)
    target = paragraph_with(document, "transmits")
    _document, graph = build({"relations": [{
        "subject": "e120", "predicate": "transmits_to", "object": "e130",
        "direction": "subject_to_object", "paragraph_id": target.id,
        "quote": "The sensor 120 transmits measurement data to the controller 130",
        "confidence": 0.9}], "shape_hints": [], "entity_types": []})
    by_id = {entity.id: entity for entity in graph.entities}
    assert statement(graph.relations[0], by_id) == (
        "The sensor 120 transmits to the controller 130.")
