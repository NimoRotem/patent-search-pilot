"""The reference-guided mode: what it will and will not accept from an image model.

No image model is called. Generation and reading-back are both stubbed, because what these tests
are about is the contract around them — that the artwork is rejected when it carries text or
undisclosed parts, that the numerals come from the registry and not from the drawing, and that a
figure which cannot be grounded falls back to something correct rather than shipping.
"""
from __future__ import annotations

import pytest
from conftest import make_graph

from pfc import appearance, generate, imagegrounding, referencemode
from pfc import plan as planning, spec as speccing
from pfc.imagegrounding import Located
from pfc.neighbours import Neighbour, Neighbourhood, Sheet
from pfc.render import render_svg
from pfc.schemas import Box
from pfc.validate import FigureBundle, ValidationContext, blocking, validate_figure

ART = b"\x89PNG\r\n\x1a\n" + b"pretend-this-is-line-art" * 20


@pytest.fixture
def figure(housing_document, profile):
    graph, registry = make_graph(housing_document, [
        ("110", "contains", "120", "none"),
        ("130", "controls", "140", "subject_to_object"),
    ])
    appearance.decide(graph, None, [])
    plan = planning.build_plan(housing_document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == "1")
    spec, _ = speccing.build_spec(housing_document, graph, registry, item, None)
    return graph, plan, spec


@pytest.fixture
def neighbourhood():
    return Neighbourhood(neighbours=[
        Neighbour(pub="US1111111A", title="A gripper", why="cited by the examiner",
                  sheets=[Sheet(pub="US1111111A", index=0, png=ART)]),
        Neighbour(pub="US2222222A", title="Another gripper", why="a similar document",
                  sheets=[Sheet(pub="US2222222A", index=0, png=ART)]),
    ])


def _located(spec, area, *, text=(), unlisted=(), skip=()):
    boxes = {}
    step = area.width / max(1, len(spec.entities) + 1)
    for index, entity in enumerate(spec.entities):
        if entity.entity_id in skip:
            continue
        boxes[entity.entity_id] = Box(x=area.x + step * index + 20, y=area.y + 200,
                                      width=step * 0.6, height=200.0)
    return Located(boxes=boxes, visible_text=list(text), unlisted=list(unlisted),
                   missing=list(skip), ok=bool(boxes))


def _patch(monkeypatch, located, *, png=ART, fail=""):
    def draw(spec, graph, references, earlier=(), model="", temperature=0.32):
        result = generate.Generated(prompt="p",
                                    references=[f"{s.pub}#{s.index}" for s in references])
        if fail:
            result.error = fail
        else:
            result.png = png
        return result

    monkeypatch.setattr(referencemode.generate, "draw", draw)
    monkeypatch.setattr(referencemode.imagegrounding, "locate",
                        lambda png, spec, graph, area, verifier: located)
    monkeypatch.setattr(referencemode, "_image_size", lambda png: (1400, 900))


def test_the_artwork_is_drawn_and_our_numerals_are_composited(
        figure, profile, neighbourhood, monkeypatch):
    graph, _plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert drawn.ok
    assert drawn.scene.artwork is True
    assert drawn.artwork == ART
    # Every numeral on the sheet is one from the registry, placed by the renderer.
    printed = {label.reference_numeral for label in drawn.scene.labels}
    assert printed == {entity.reference_numeral for entity in spec.entities}
    assert "<image" in drawn.svg
    for numeral in printed:
        assert f'data-reference-label="{numeral}"' in drawn.svg


def test_references_come_from_more_than_one_patent(figure, profile, neighbourhood,
                                                   monkeypatch):
    """A figure derived from one document can come out being that document's figure."""
    graph, _plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert len({ref.split("#")[0] for ref in drawn.references}) >= 2


def test_artwork_with_text_on_it_is_refused(figure, profile, neighbourhood, monkeypatch):
    """A reference numeral an image model wrote is a numeral nobody can trace."""
    graph, _plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area, text=["112", "FIG. 1"]))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert not drawn.ok
    assert "text on it" in drawn.failed


def test_artwork_showing_an_undisclosed_part_is_refused(figure, profile, neighbourhood,
                                                        monkeypatch):
    """The failure mode of prompting for a technical drawing: it adds the bracket."""
    graph, _plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area, unlisted=["a mounting bracket", "a cable"]))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert not drawn.ok
    assert "does not disclose" in drawn.failed


def test_a_part_that_cannot_be_found_is_reported_not_invented(
        figure, profile, neighbourhood, monkeypatch):
    graph, _plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    missing = spec.entities[-1].entity_id
    _patch(monkeypatch, _located(spec, area, skip=[missing]))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert drawn.ok
    assert any("could not be found" in note for note in drawn.notes)
    assert missing not in {label.entity_id for label in drawn.scene.labels}


def test_the_sheet_is_reproducible_from_the_scene_and_the_artwork(
        figure, profile, neighbourhood, monkeypatch):
    """Everything the semantic rules check is checked against the scene, which is only sound
    while re-rendering reproduces the file."""
    graph, plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg, artwork=drawn.artwork)
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    assert "RND001" not in {issue.rule_id for issue in blocking(validate_figure(context))}
    assert render_svg(drawn.scene, profile, drawn.artwork) == drawn.svg


def test_a_raster_sheet_is_not_refused_for_being_a_raster(figure, profile, neighbourhood,
                                                          monkeypatch):
    graph, plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg, artwork=drawn.artwork)
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    ids = {issue.rule_id for issue in blocking(validate_figure(context))}
    assert "JUR001" not in ids, "the artwork IS the drawing in this mode"
    assert "GEO001" not in ids, "located parts overlap in a perspective view"


def test_no_image_model_means_no_generated_figure(figure, profile, neighbourhood,
                                                  monkeypatch):
    graph, _plan, spec = figure
    _patch(monkeypatch, Located(), fail="the image model refused")
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert not drawn.ok
    assert drawn.failed


def test_without_a_vision_model_the_mode_declines(figure, profile, neighbourhood):
    """Nothing can place a numeral if nothing can say where the part is."""
    graph, _plan, spec = figure
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, None)
    assert not drawn.ok
    assert "vision model" in drawn.failed


def test_with_no_neighbours_the_mode_declines(figure, profile):
    graph, _plan, spec = figure
    drawn = referencemode.draw_figure(spec, graph, profile, Neighbourhood(), object())
    assert not drawn.ok
    assert "no neighbouring patent" in drawn.failed


def test_the_prompt_names_only_the_parts_this_figure_specifies(figure):
    graph, _plan, spec = figure
    prompt = generate.build_prompt(spec, graph, reference_count=2)
    for entity in spec.entities:
        node = graph.entity(entity.entity_id)
        assert node.canonical_name in prompt
    assert "no text" in prompt.lower()
    # Nothing outside this figure's specification reaches the image model.
    excluded = [graph.entity(eid) for eid in spec.prohibited_entities]
    for node in excluded:
        if node is not None and len(node.canonical_name) > 6:
            assert node.canonical_name not in prompt
