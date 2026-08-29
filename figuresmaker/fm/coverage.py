"""The coverage matrix: what has to appear where, agreed before anything is drawn.

Rows are the things that must be shown. Columns are the figures. A cell says that this numeral or
this claim element appears in that view.

It exists because of what the evaluation found. The planner reads figure *types* perfectly and
distributes *parts* badly: on a draft with a hundred numerals it places about two thirds of them,
and it puts a fair number in the wrong view. Both are cheap to fix and expensive to discover. A
matrix an attorney can read in thirty seconds and correct in two minutes turns the pipeline's
weakest judgement into its cheapest one, and it does so before a single scene has been generated.

Two properties are worth stating.

**It is the gate.** Nothing expensive runs until the matrix is approved. The stages before it are
seconds of fast-model work; everything after it is minutes.

**Editing it regenerates only what changed.** A scene is cached against the figure's element set,
so moving 114 out of FIG. 3 and into FIG. 4 changes the identity of those two figures and of
nothing else. The other sixteen are reused exactly as they were.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import sources as sources_mod
from .schemas import Claim, FigurePlan, Plan, Registry
from .sections import figure_sort_key

CELL_STATES = ("proposed", "approved", "added", "removed")


class Column(BaseModel):
    """One figure, as the matrix sees it."""
    label: str
    kind: str
    title: str = ""
    source_kind: str = "blockout"
    source_id: str = ""
    filing_ready: bool = False
    needs_a_source: bool = True


class Row(BaseModel):
    """One thing that has to be shown somewhere."""
    key: str                      # "102", or "claim:1:3"
    kind: str                     # numeral | claim_element
    label: str                    # the term, or the limitation
    numeral: str = ""             # for a claim element, the part it was matched to
    figures: list[str] = Field(default_factory=list)
    mentions: int = 0
    note: str = ""

    @property
    def covered(self) -> bool:
        return bool(self.figures)


class Coverage(BaseModel):
    columns: list[Column] = Field(default_factory=list)
    rows: list[Row] = Field(default_factory=list)
    approved: bool = False
    approved_at: float = 0.0
    edited: bool = False

    # ------------------------------------------------------------------------------ queries

    def row(self, key: str) -> Optional[Row]:
        for item in self.rows:
            if item.key == key:
                return item
        return None

    def column(self, label: str) -> Optional[Column]:
        for item in self.columns:
            if item.label == label:
                return item
        return None

    def gaps(self) -> dict[str, list[str]]:
        """What the matrix says is wrong with itself, before anything is rendered."""
        return {
            "numerals_in_no_figure": [r.key for r in self.rows
                                      if r.kind == "numeral" and not r.figures],
            "claim_elements_in_no_figure": [r.key for r in self.rows
                                            if r.kind == "claim_element" and not r.figures],
            "claim_elements_unmatched": [r.key for r in self.rows
                                         if r.kind == "claim_element" and not r.numeral],
            "figures_needing_a_source": [c.label for c in self.columns
                                         if c.needs_a_source and not c.filing_ready],
            "empty_figures": [c.label for c in self.columns
                              if not any(c.label in r.figures for r in self.rows)],
        }

    def summary(self) -> dict[str, Any]:
        numerals = [r for r in self.rows if r.kind == "numeral"]
        elements = [r for r in self.rows if r.kind == "claim_element"]
        gaps = self.gaps()
        return {
            "figures": len(self.columns),
            "numerals": len(numerals),
            "numerals_covered": sum(1 for r in numerals if r.figures),
            "claim_elements": len(elements),
            "claim_elements_covered": sum(1 for r in elements if r.figures),
            "filing_ready_figures": sum(1 for c in self.columns if c.filing_ready),
            "approved": self.approved,
            "gaps": {k: len(v) for k, v in gaps.items()},
        }


# ------------------------------------------------------------------------------- proposing


def propose(plan: Plan, registry: Registry, claims: list[Claim]) -> Coverage:
    """The matrix as the planner would have it. Every cell is a proposal, none is a decision."""
    columns = [
        Column(label=figure.label, kind=figure.kind, title=figure.title,
               source_kind=(figure.source.kind if figure.source else "blockout"),
               source_id=(figure.source.source_id if figure.source else ""),
               filing_ready=sources_mod.is_authoritative(
                   figure.kind, (figure.source.kind if figure.source else "blockout")),
               needs_a_source=sources_mod.needs_a_source(figure.kind))
        for figure in sorted(plan.figures, key=lambda f: figure_sort_key(f.label))]

    where: dict[str, list[str]] = {}
    for figure in plan.figures:
        for element in figure.elements:
            where.setdefault(element.numeral, []).append(figure.label)

    rows: list[Row] = []
    for entry in registry.entries:
        rows.append(Row(
            key=entry.numeral, kind="numeral", label=entry.term, numeral=entry.numeral,
            figures=sorted(set(where.get(entry.numeral, [])), key=figure_sort_key),
            mentions=entry.mentions,
            note="" if where.get(entry.numeral) else "the description mentions it and no view "
                                                     "shows it"))

    for claim in claims:
        if not claim.independent:
            continue
        for index, element in enumerate(claim.elements):
            figures = sorted(set(where.get(element.numeral, [])), key=figure_sort_key) \
                if element.numeral else []
            rows.append(Row(
                key=f"claim:{claim.number}:{index}", kind="claim_element",
                label=element.text[:180], numeral=element.numeral, figures=figures,
                note="" if element.numeral else "no reference character in the description "
                                                "names this, so it cannot be checked"))
    return sync(Coverage(columns=columns, rows=rows))


# --------------------------------------------------------------------------------- editing


def sync(coverage: Coverage) -> Coverage:
    """Make the claim rows agree with the numeral rows they are about.

    A claim element is not a separate thing that can be in a figure. It is a *part*, named the
    way the claim names it, and the figure holds the part. Keeping two independently editable
    copies of one fact is how a matrix ends up telling a user that unticking 104 left 104 in the
    figure, which it did, because the claim row still said so.
    """
    where = {row.key: list(row.figures) for row in coverage.rows if row.kind == "numeral"}
    for row in coverage.rows:
        if row.kind != "claim_element":
            continue
        row.figures = list(where.get(row.numeral, [])) if row.numeral else []
    return coverage


def set_cell(coverage: Coverage, key: str, figure: str, present: bool) -> Coverage:
    """Put a part in a figure, or take it out. The one edit the matrix supports.

    Ticking a claim element ticks the part it names, because that is the only thing a figure can
    actually hold. A claim element no reference character names cannot be moved at all, and
    saying so is more use than appearing to move it.
    """
    row = coverage.row(key)
    if row is None:
        raise KeyError(f"{key} is not in the matrix")
    if coverage.column(figure) is None:
        raise KeyError(f"{figure} is not in the drawing set")

    if row.kind == "claim_element":
        if not row.numeral:
            raise ValueError(
                f"{row.label[:60]!r} has no reference character, so there is no part to put in "
                f"{figure}. Number it in the description first.")
        target = coverage.row(row.numeral)
        if target is None:
            raise KeyError(f"{row.numeral} is not in the registry")
        row = target

    if (figure in row.figures) == present:
        return coverage
    if present:
        row.figures = sorted(set(row.figures) | {figure}, key=figure_sort_key)
    else:
        row.figures = [f for f in row.figures if f != figure]
    row.note = ""
    coverage.edited = True
    coverage.approved = False
    return sync(coverage)


def set_source(coverage: Coverage, figure: str, source_kind: str,
               source_id: str = "") -> Coverage:
    """Say where a figure's geometry will come from."""
    column = coverage.column(figure)
    if column is None:
        raise KeyError(f"{figure} is not in the drawing set")
    if source_kind not in sources_mod.SOURCE_KINDS:
        raise ValueError(f"{source_kind!r} is not a source kind")
    column.source_kind = source_kind
    column.source_id = source_id
    column.filing_ready = sources_mod.is_authoritative(column.kind, source_kind)
    coverage.edited = True
    coverage.approved = False
    return coverage


def approve(coverage: Coverage) -> Coverage:
    coverage.approved = True
    coverage.approved_at = time.time()
    return coverage


# ------------------------------------------------------------------------- back into the plan


def apply_to_plan(plan: Plan, coverage: Coverage, registry: Registry) -> Plan:
    """Rewrite the plan so it says what the matrix says.

    A claim element's row moves the *numeral* it was matched to, because a figure holds parts and
    a claim element is a part under another name. A row with no numeral cannot move anything, and
    the matrix says so in its own note rather than silently doing nothing.
    """
    from .schemas import FigureSource, PlanElement

    known = registry.by_numeral()
    wanted: dict[str, set[str]] = {column.label: set() for column in coverage.columns}
    # Only the numeral rows are read. The claim rows mirror them, and reading both would let a
    # stale mirror put a part back into a figure the user had just taken it out of.
    for row in coverage.rows:
        if row.kind != "numeral" or row.key not in known:
            continue
        for label in row.figures:
            if label in wanted:
                wanted[label].add(row.key)

    by_label = {figure.label: figure for figure in plan.figures}
    rebuilt: list[FigurePlan] = []
    for column in coverage.columns:
        figure = by_label.get(column.label)
        if figure is None:
            figure = FigurePlan(label=column.label, kind=column.kind, title=column.title)
        numerals = sorted(wanted.get(column.label, set()),
                          key=lambda n: (len(n), n))
        # Keep the planner's own ordering for numerals it already had, then append the rest, so
        # an edit does not reshuffle a figure that was otherwise untouched.
        original = [e.numeral for e in figure.elements]
        ordered = [n for n in original if n in numerals] + \
                  [n for n in numerals if n not in original]
        figure.elements = [PlanElement(numeral=n, term=known[n].term) for n in ordered]
        allowed = set(ordered)
        figure.relations = [r for r in figure.relations
                            if r.source in allowed and r.target in allowed]
        figure.source = FigureSource(kind=column.source_kind, source_id=column.source_id,
                                     view=figure.source.view if figure.source else "",
                                     note=figure.source.note if figure.source else "")
        rebuilt.append(figure)

    plan.figures = rebuilt
    return plan


def changed_figures(before: Plan, after: Plan) -> list[str]:
    """Which figures an edit actually altered, and so which have to be drawn again.

    The rest are reused. This is what makes correcting the matrix cheap enough to do properly:
    moving one numeral costs one or two scene calls, not the whole set.
    """
    def identity(figure: FigurePlan) -> tuple:
        return (figure.kind, figure.source.kind if figure.source else "",
                figure.source.source_id if figure.source else "",
                tuple(sorted(e.numeral for e in figure.elements)))

    old = {figure.label: identity(figure) for figure in before.figures}
    out: list[str] = []
    for figure in after.figures:
        if old.get(figure.label) != identity(figure):
            out.append(figure.label)
    return out
