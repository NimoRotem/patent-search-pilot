"""The coverage matrix and the gate in front of the expensive half."""
from __future__ import annotations

import pytest

from fm import coverage as cov
from fm.schemas import (Claim, ClaimElement, FigurePlan, FigureSource, Plan, PlanElement,
                        RefEntry, Registry)


def a_registry() -> Registry:
    return Registry(entries=[
        RefEntry(numeral="100", term="apparatus", mentions=4),
        RefEntry(numeral="102", term="housing", mentions=6),
        RefEntry(numeral="104", term="pump", mentions=3),
        RefEntry(numeral="106", term="seal", mentions=2),
    ])


def a_plan() -> Plan:
    return Plan(figures=[
        FigurePlan(label="FIG. 1", kind="block_diagram", source=FigureSource(kind="schema"),
                   elements=[PlanElement(numeral="100"), PlanElement(numeral="102")]),
        FigurePlan(label="FIG. 2", kind="perspective", source=FigureSource(kind="blockout"),
                   elements=[PlanElement(numeral="102"), PlanElement(numeral="104")]),
    ])


def a_claim() -> list[Claim]:
    return [Claim(number=1, independent=True, text="An apparatus.", elements=[
        ClaimElement(text="a housing", term="housing", numeral="102"),
        ClaimElement(text="a pump", term="pump", numeral="104"),
        ClaimElement(text="a widget", term="widget", numeral=""),
    ])]


@pytest.fixture
def matrix() -> cov.Coverage:
    return cov.propose(a_plan(), a_registry(), a_claim())


def test_every_numeral_gets_a_row(matrix):
    keys = {r.key for r in matrix.rows if r.kind == "numeral"}
    assert keys == {"100", "102", "104", "106"}


def test_every_independent_claim_element_gets_a_row(matrix):
    assert len([r for r in matrix.rows if r.kind == "claim_element"]) == 3


def test_a_numeral_in_no_figure_is_visible_before_anything_is_drawn(matrix):
    """This is the failure the whole matrix exists to surface early."""
    assert matrix.row("106").figures == []
    assert "106" in matrix.gaps()["numerals_in_no_figure"]
    assert matrix.row("106").note


def test_a_claim_element_with_no_numeral_says_so(matrix):
    row = next(r for r in matrix.rows if r.kind == "claim_element" and not r.numeral)
    assert "cannot be checked" in row.note


def test_a_mechanical_figure_with_no_source_is_flagged_in_the_matrix(matrix):
    column = matrix.column("FIG. 2")
    assert column.needs_a_source
    assert not column.filing_ready
    assert "FIG. 2" in matrix.gaps()["figures_needing_a_source"]


def test_a_diagram_needs_no_source(matrix):
    column = matrix.column("FIG. 1")
    assert not column.needs_a_source
    assert column.filing_ready


def test_adding_a_cell_covers_the_row(matrix):
    cov.set_cell(matrix, "106", "FIG. 2", True)
    assert matrix.row("106").figures == ["FIG. 2"]
    assert "106" not in matrix.gaps()["numerals_in_no_figure"]


def test_removing_a_cell_uncovers_it(matrix):
    cov.set_cell(matrix, "104", "FIG. 2", False)
    assert matrix.row("104").figures == []


def test_an_edit_withdraws_approval(matrix):
    cov.approve(matrix)
    assert matrix.approved
    cov.set_cell(matrix, "106", "FIG. 1", True)
    assert not matrix.approved, "changing the matrix after approving it must re-open the gate"


def test_an_unknown_row_or_figure_is_refused(matrix):
    with pytest.raises(KeyError):
        cov.set_cell(matrix, "999", "FIG. 1", True)
    with pytest.raises(KeyError):
        cov.set_cell(matrix, "102", "FIG. 9", True)


def test_setting_a_source_makes_a_figure_filing_ready(matrix):
    cov.set_source(matrix, "FIG. 2", "cad", "abc123")
    column = matrix.column("FIG. 2")
    assert column.filing_ready
    assert column.source_id == "abc123"


def test_a_source_that_cannot_stand_behind_the_figure_is_not_filing_ready(matrix):
    cov.set_source(matrix, "FIG. 2", "screenshot", "abc123")
    assert not matrix.column("FIG. 2").filing_ready


def test_an_unknown_source_kind_is_refused(matrix):
    with pytest.raises(ValueError):
        cov.set_source(matrix, "FIG. 2", "photograph")


# ------------------------------------------------------------------------- back into the plan


def test_the_plan_follows_the_matrix(matrix):
    cov.set_cell(matrix, "106", "FIG. 2", True)
    cov.set_cell(matrix, "104", "FIG. 2", False)
    plan = cov.apply_to_plan(a_plan(), matrix, a_registry())
    two = next(f for f in plan.figures if f.label == "FIG. 2")
    numerals = {e.numeral for e in two.elements}
    assert "106" in numerals
    assert "104" not in numerals


def test_a_claim_element_moves_the_part_it_names(matrix):
    """A figure holds parts; a claim element is a part under another name."""
    key = next(r.key for r in matrix.rows if r.numeral == "104" and r.kind == "claim_element")
    cov.set_cell(matrix, key, "FIG. 1", True)
    plan = cov.apply_to_plan(a_plan(), matrix, a_registry())
    one = next(f for f in plan.figures if f.label == "FIG. 1")
    assert "104" in {e.numeral for e in one.elements}


def test_a_source_chosen_in_the_matrix_reaches_the_plan(matrix):
    cov.set_source(matrix, "FIG. 2", "cad", "mesh1")
    plan = cov.apply_to_plan(a_plan(), matrix, a_registry())
    two = next(f for f in plan.figures if f.label == "FIG. 2")
    assert two.source.kind == "cad" and two.source.source_id == "mesh1"


# ---------------------------------------------------------------- incremental regeneration


def test_only_the_figures_an_edit_touched_are_regenerated(matrix):
    before = a_plan()
    cov.set_cell(matrix, "106", "FIG. 2", True)
    after = cov.apply_to_plan(a_plan(), matrix, a_registry())
    assert cov.changed_figures(before, after) == ["FIG. 2"]


def test_moving_a_numeral_between_views_regenerates_exactly_two(matrix):
    before = a_plan()
    cov.set_cell(matrix, "104", "FIG. 2", False)
    cov.set_cell(matrix, "104", "FIG. 1", True)
    after = cov.apply_to_plan(a_plan(), matrix, a_registry())
    assert sorted(cov.changed_figures(before, after)) == ["FIG. 1", "FIG. 2"]


def test_an_untouched_matrix_regenerates_nothing(matrix):
    before = a_plan()
    after = cov.apply_to_plan(a_plan(), matrix, a_registry())
    assert cov.changed_figures(before, after) == []


def test_changing_only_the_source_still_regenerates_that_figure(matrix):
    before = a_plan()
    cov.set_source(matrix, "FIG. 2", "cad", "mesh1")
    after = cov.apply_to_plan(a_plan(), matrix, a_registry())
    assert cov.changed_figures(before, after) == ["FIG. 2"]


def test_the_summary_counts_what_an_attorney_looks_at(matrix):
    summary = matrix.summary()
    assert summary["figures"] == 2
    assert summary["numerals"] == 4
    assert summary["numerals_covered"] == 3
    assert summary["gaps"]["numerals_in_no_figure"] == 1
    assert summary["gaps"]["figures_needing_a_source"] == 1


def test_a_claim_row_mirrors_its_part_and_cannot_disagree_with_it(matrix):
    """Two editable copies of one fact is how a matrix lies to the person reading it."""
    claim_row = next(r for r in matrix.rows if r.numeral == "104" and r.kind == "claim_element")
    assert claim_row.figures == matrix.row("104").figures
    cov.set_cell(matrix, "104", "FIG. 2", False)
    assert matrix.row("104").figures == []
    assert claim_row.figures == [], "the claim row must follow the part it names"


def test_ticking_a_claim_element_ticks_the_part(matrix):
    key = next(r.key for r in matrix.rows if r.numeral == "104" and r.kind == "claim_element")
    cov.set_cell(matrix, key, "FIG. 1", True)
    assert "FIG. 1" in matrix.row("104").figures, "the part moved, not just its claim row"


def test_an_unnumbered_claim_element_cannot_be_placed(matrix):
    key = next(r.key for r in matrix.rows if r.kind == "claim_element" and not r.numeral)
    with pytest.raises(ValueError) as caught:
        cov.set_cell(matrix, key, "FIG. 1", True)
    assert "Number it in the description" in str(caught.value)
