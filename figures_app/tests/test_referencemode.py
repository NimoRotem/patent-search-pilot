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


def test_an_unmatched_shape_is_reported_not_thrown_away(figure, profile, neighbourhood,
                                                        monkeypatch):
    """Whether a shape is a separate component or the contour of a listed one is a judgement the
    reader is not reliable at. It rejected two good drawings of a housing for having a recessed
    centre, so it reports instead."""
    graph, _plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area, unlisted=["a mounting bracket", "a cable"]))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert drawn.ok, "a drawing is not discarded on the reader's opinion of a shape"
    assert any("mounting bracket" in note for note in drawn.notes)
    assert any("check the drawing" in note for note in drawn.notes)


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


def test_boilerplate_in_a_caption_is_not_an_unnumbered_part():
    """"...of the presently disclosed subject matter" names no component.

    Reading it as one put every figure of US-2024/0246200-A1 into NEEDS_TEXT_UPDATE for a part
    that does not exist.
    """
    from pfc.spec import unnumbered_components

    caption = ("shows a bottom perspective view of a vacuum gripper according to one example "
               "of the presently disclosed subject matter")
    assert unnumbered_components(caption, {}) == []


def test_an_arrangement_is_not_demanded_as_a_line_on_generated_artwork(
        figure, profile, neighbourhood, monkeypatch):
    """A pump inside a housing is shown by being drawn inside it, not by a connector."""
    graph, plan, spec = figure
    assert spec.relations, "the fixture needs a relationship for this to mean anything"
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    _patch(monkeypatch, _located(spec, area))
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    assert not drawn.scene.edges, "a raster sheet carries no connector lines"
    bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg, artwork=drawn.artwork)
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    assert "SEM004" not in {i.rule_id for i in blocking(validate_figure(context))}


def test_a_containers_own_leader_is_not_called_ambiguous(figure, profile, neighbourhood,
                                                         monkeypatch):
    """It lands on its own outline, which has the parts it holds just inside it."""
    graph, plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    located = _located(spec, area)
    holder = spec.entities[0].entity_id
    located.encloses.add(holder)
    located.boxes[holder] = Box(x=area.x + 10, y=area.y + 10,
                                width=area.width - 20, height=area.height - 20)
    _patch(monkeypatch, located)
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg, artwork=drawn.artwork)
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    offenders = [i for i in blocking(validate_figure(context))
                 if i.rule_id == "GEO009" and i.entity_id == holder]
    assert not offenders


def test_a_part_nested_inside_another_makes_that_other_one_a_container(
        figure, profile, neighbourhood, monkeypatch):
    """The reader ticks ``encloses_others`` for housings and misses a track holding a seal.

    Measured on US-2024/0246200-A1: numeral 142, the leakage seal element, sits wholly inside
    numeral 148, the track. Its leader has nowhere to land that is not inside 148, so GEO009 was
    unsatisfiable and three identical correction attempts blocked the figure.
    """
    graph, plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    located = _located(spec, area)
    outer, inner = spec.entities[0].entity_id, spec.entities[1].entity_id
    located.boxes[outer] = Box(x=area.x + 400, y=area.y + 300, width=540, height=200)
    located.boxes[inner] = Box(x=area.x + 490, y=area.y + 340, width=360, height=115)
    assert outer not in located.encloses, "the reader did not say so; the boxes have to"

    _patch(monkeypatch, located)
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    holder = next(n for n in drawn.scene.nodes if n.entity_id == outer)
    assert holder.is_container

    bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg, artwork=drawn.artwork)
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    offenders = [i for i in blocking(validate_figure(context))
                 if i.rule_id == "GEO009" and i.entity_id == inner]
    assert not offenders, "a nested part's leader cannot avoid the part it is nested in"


def test_two_parts_the_reader_could_not_tell_apart_do_not_become_containers():
    """Identical boxes mean neither encloses the other, or every scene loses GEO009."""
    from pfc.layout.traced import enclosing, holds

    same = Box(x=100, y=100, width=400, height=300)
    twin = Box(x=100, y=100, width=400, height=300)
    assert not holds(same, twin)
    assert enclosing({"a": same, "b": twin}) == set()


def test_numerals_located_at_one_place_are_reported_not_rejected(figure):
    """Four numerals on one outline is what a reader returns when it gave up.

    It is also what a sealing lip, a groove and a flange on the same rim honestly look like, so
    this is a note for a human rather than grounds for redrawing.
    """
    graph, _plan, spec = figure
    shared = Box(x=604, y=1202, width=1046, height=424)
    ids = [entity.entity_id for entity in spec.entities[:3]]
    located = Located(boxes={eid: shared.model_copy() for eid in ids}, ok=True)

    assert imagegrounding.defects(located, spec, graph) == []
    notes = imagegrounding.concerns(located, graph)
    assert any("same place" in note for note in notes)
    numerals = [graph.entity(eid).reference_numeral for eid in ids]
    assert all(numeral in notes[-1] for numeral in numerals)


def test_the_patents_own_drawings_are_never_a_reference_for_itself():
    """Its own family carries its own sheets under other numbers.

    A continuation and the granted version of the same application appear in a record's
    citations and similar documents like any other art, and their drawings are this patent's
    drawings. Copying those is the one thing this mode was told not to do.
    """
    from pfc.neighbours import _ranked_candidates, _family_of

    record = {
        "pub": "US-2024/0246200-A1",
        "application_number": "18/158,123",
        "family": [{"pub": "US11338449B2"}, "WO-2023/012345-A1"],
        "citations": [
            {"pub": "US20240246200A1", "origin": "examiner", "title": "itself, differently spelt"},
            {"pub": "US-11338449-B2", "origin": "examiner", "title": "its own grant"},
            {"pub": "US3240525A", "origin": "examiner", "title": "genuine art"},
        ],
        "similar": [{"pub": "WO2023012345A1", "title": "its own PCT"},
                    {"pub": "US4852926A", "title": "more genuine art"}],
    }
    family = _family_of(record) | {"US20240246200A1"}
    kept = [pub for pub, _title, _why in _ranked_candidates(record, exclude=family)]
    assert kept == ["US3240525A", "US4852926A"], kept


def test_a_lookup_that_failed_is_not_reported_as_a_patent_with_no_citations(monkeypatch):
    """The one that cost a whole job.

    ``display_record`` swallowed an exception and returned ``{}``, which was written down as
    "this publication's record carries no citations or similar documents" for a record holding
    forty-three of them. Every figure then fell back to a schematic and the note gave a human no
    way to tell that from a patent that genuinely stands alone.
    """
    from pfc import neighbours, pilot

    def always_fails(pub):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(pilot, "module",
                        lambda name: type("M", (), {"enrich_for_display":
                                                    staticmethod(always_fails)})())
    found = neighbours.find("US-20240246200-A1")

    assert not found.neighbours
    joined = " ".join(found.notes)
    assert "could not be looked up" in joined
    assert "connection reset by peer" in joined
    assert "carries no citations" not in joined, "a failure is not a fact about the patent"


def test_a_record_that_is_genuinely_empty_still_says_so(monkeypatch):
    """The other half: asked, and it holds nothing. That one IS a fact about the patent."""
    from pfc import neighbours, pilot

    monkeypatch.setattr(pilot, "module",
                        lambda name: type("M", (), {"enrich_for_display": staticmethod(
                            lambda pub: {"pub": pub, "citations": [], "similar": []})})())
    found = neighbours.find("US-20240246200-A1")
    assert "carries no citations or similar documents" in " ".join(found.notes)


def test_the_record_is_asked_more_than_once_before_its_absence_is_believed(monkeypatch):
    """A transient failure costs a retry, not the whole drawing style."""
    from pfc import neighbours, pilot

    calls = []

    def flaky(pub):
        calls.append(pub)
        if len(calls) < 2:
            raise RuntimeError("temporarily unavailable")
        return {"pub": pub, "citations": [{"pub": "US3240525A", "origin": "examiner"}],
                "similar": []}

    monkeypatch.setattr(pilot, "module",
                        lambda name: type("M", (), {"enrich_for_display": staticmethod(flaky)})())
    monkeypatch.setattr(neighbours, "_sheets_for", lambda pub, limit=4: [])
    found = neighbours.find("US-20240246200-A1")

    assert len(calls) == 2, "it gave up on the first failure"
    assert "carries no citations" not in " ".join(found.notes)


def test_a_stray_module_on_the_path_does_not_silently_become_the_search_apps(tmp_path,
                                                                             monkeypatch):
    """A ``/tmp/config.py`` from another app made enrich_display point its data dir at ``/data``.

    The search app's modules import each other by bare name, and the directory of whatever
    script started the process is always first on sys.path. So the search app's own src has to
    win for the duration of its own imports.
    """
    from pfc import pilot

    src = tmp_path / "src"
    src.mkdir()
    (src / "config.py").write_text("WHOSE = 'the search app'\n")
    (src / "thing.py").write_text("import config\nWHOSE = config.WHOSE\n")

    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "config.py").write_text("WHOSE = 'somebody else'\n")

    monkeypatch.setattr(pilot, "PILOT_SRC", src)
    monkeypatch.setattr(pilot, "PILOT_ROOT", tmp_path)
    pilot._ensure_path.cache_clear()
    monkeypatch.syspath_prepend(str(stray))
    for name in ("config", "thing"):
        monkeypatch.delitem(__import__("sys").modules, name, raising=False)

    assert pilot.module("thing").WHOSE == "the search app"
    pilot._ensure_path.cache_clear()


def test_the_path_is_put_back_after_a_pilot_import(tmp_path, monkeypatch):
    """Prepending permanently would shadow this app's own top-level ``app`` module."""
    import sys

    from pfc import pilot

    src = tmp_path / "src"
    src.mkdir()
    (src / "harmless.py").write_text("VALUE = 1\n")
    monkeypatch.setattr(pilot, "PILOT_SRC", src)
    monkeypatch.setattr(pilot, "PILOT_ROOT", tmp_path)
    pilot._ensure_path.cache_clear()

    before = list(sys.path)
    pilot.module("harmless")
    assert sys.path == before + [str(src)], "the search app's src stayed at the front"
    pilot._ensure_path.cache_clear()


def test_the_image_model_is_told_which_element_to_draw(figure):
    """The settled appearance never reached the prompt, so consistency stopped at the renderer.

    The compiler decides once per part what simple recognisable element it stands for, and the
    cross-figure rules hold every figure to that. The image model was told only the part's name:
    given "release button" with nothing else it drew a desktop computer monitor.
    """
    graph, _plan, spec = figure
    node = graph.entity(spec.entities[0].entity_id)
    node.appearance.symbol = "suction_cup"
    node.appearance.size = "large"

    prompt = generate.build_prompt(spec, graph, reference_count=3)
    assert "suction cup" in prompt
    assert "large" in prompt
    assert "suction_cup" not in prompt, "the underscore is a code name, not a drawing instruction"


def test_a_part_with_nothing_settled_gets_no_invented_element(figure):
    """Guessing here would defeat the point of settling it once."""
    graph, _plan, spec = figure
    for entity in spec.entities:
        node = graph.entity(entity.entity_id)
        if node is not None:
            node.appearance.symbol = "generic_component"

    prompt = generate.build_prompt(spec, graph, reference_count=3)
    assert "drawn as a simple" not in prompt


def test_two_parts_at_one_place_do_not_block_each_others_leaders(figure, profile, neighbourhood,
                                                                 monkeypatch):
    """GEO009 is unsatisfiable between parts the reader gave one box.

    On US-2024/0246200-A1 numerals 141, 144 and 147 came back on identical boxes, because in a
    perspective view the reader could not tell a rib from a bracing structure. Each leader then
    landed inside the others and three correction passes changed nothing.
    """
    graph, plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    located = _located(spec, area)
    shared = Box(x=area.x + 300, y=area.y + 250, width=750, height=280)
    twins = [entity.entity_id for entity in spec.entities[:3]]
    for entity_id in twins:
        located.boxes[entity_id] = shared.model_copy()

    _patch(monkeypatch, located)
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg, artwork=drawn.artwork)
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    offenders = [i for i in blocking(validate_figure(context))
                 if i.rule_id == "GEO009" and i.entity_id in twins]
    assert not offenders, "no placement satisfies this, so blocking on it never terminates"


def test_a_leader_landing_on_a_genuinely_different_part_still_blocks(figure, profile,
                                                                    neighbourhood, monkeypatch):
    """The carve-out is for boxes at one place, not for a leader that wandered.

    The two boxes here OVERLAP, the way a pump behind a housing wall overlaps it in a perspective
    view, without being the same place. Widen the carve-out to cover mere overlap and every
    reference-guided sheet loses GEO009 entirely.
    """
    graph, plan, spec = figure
    area = Box(x=254.0, y=254.0, width=1400.0, height=1000.0)
    located = _located(spec, area)
    owner, other = spec.entities[0].entity_id, spec.entities[1].entity_id
    # Overlapping by roughly two fifths of their union: substantial, and nowhere near identical.
    located.boxes[owner] = Box(x=area.x + 60, y=area.y + 60, width=400, height=320)
    located.boxes[other] = Box(x=area.x + 150, y=area.y + 140, width=400, height=320)

    _patch(monkeypatch, located)
    drawn = referencemode.draw_figure(spec, graph, profile, neighbourhood, object())
    label = next(item for item in drawn.scene.labels if item.entity_id == owner)
    victim = next(node for node in drawn.scene.nodes if node.entity_id == other)
    label.leader_points[-1].x = victim.box.cx        # point it straight at the other part
    label.leader_points[-1].y = victim.box.cy

    bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg, artwork=drawn.artwork)
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    offenders = [i for i in blocking(validate_figure(context))
                 if i.rule_id == "GEO009" and i.entity_id == owner]
    assert offenders, "a leader ending on another part is exactly what GEO009 is for"
