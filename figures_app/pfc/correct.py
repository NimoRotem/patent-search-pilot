"""The correction loop: repair the smallest thing that can be wrong.

Regenerating a whole figure because two numerals overlap throws away everything that was right
about it and produces a different drawing to review. So each rule declares the repair that owns
its defect, and only that repair runs:

    two numerals overlap        move those two numerals
    a leader ends ambiguously   re-route that leader
    components overlap          lay the figure out again, differently
    a relationship is missing   nothing here can fix it, and the figure blocks

The last line is the important one. A correction may change where things are; it may never
change what they mean. A defect whose repair is ``respec``, ``revise_text`` or
``reevaluate_evidence`` is a defect in the document or in the semantic model, and quietly
redrawing until it stops being reported would be the compiler hiding a real finding.

Three attempts, then ``BLOCKED`` with the diagnosis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .layout import build_scene, relocate
from .profiles import DrawingProfile
from .schemas import FigureSpec, LayoutScene, PatentGraph, ValidationIssue

MAX_ATTEMPTS = 3

# Repairs this module can carry out, in the order it prefers them: the most local first.
LOCAL_REPAIRS = ("move_label", "reroute_leader", "rebind_leader")
GLOBAL_REPAIRS = ("relayout",)
# Repairs that need something outside the drawing to change.
ESCALATIONS = ("respec", "revise_text", "reevaluate_evidence")


@dataclass
class CorrectionOutcome:
    scene: LayoutScene
    applied: list[str] = field(default_factory=list)
    escalated: list[ValidationIssue] = field(default_factory=list)
    changed: bool = False


def _numerals(issues: Sequence[ValidationIssue], actions: Sequence[str]) -> list[str]:
    out: list[str] = []
    for issue in issues:
        if issue.repair_action not in actions:
            continue
        if issue.reference_numeral:
            out.append(issue.reference_numeral)
        other = issue.detail.get("with") if isinstance(issue.detail, dict) else None
        if isinstance(other, str) and other:
            out.append(other)
        references = issue.detail.get("references") if isinstance(issue.detail, dict) else None
        if isinstance(references, list):
            out.extend(str(value) for value in references)
    return sorted({value for value in out if value})


def correct(spec: FigureSpec, graph: PatentGraph, scene: LayoutScene,
            profile: DrawingProfile, issues: Sequence[ValidationIssue],
            attempt: int) -> CorrectionOutcome:
    """One repair pass. Returns the scene to try next and what was done to it."""
    blocking = [issue for issue in issues if issue.severity == "blocking"]
    escalated = [issue for issue in blocking if issue.repair_action in ESCALATIONS]
    outcome = CorrectionOutcome(scene=scene, escalated=escalated)
    if not blocking:
        return outcome

    local = _numerals(blocking, LOCAL_REPAIRS)
    if local:
        working = scene.model_copy(deep=True)
        outcome.scene = relocate(working, profile, local)
        outcome.applied.append(
            f"re-placed reference numeral{'s' if len(local) > 1 else ''} "
            f"{', '.join(local)}")
        outcome.changed = True
        return outcome

    if any(issue.repair_action in GLOBAL_REPAIRS for issue in blocking):
        # A different seed changes the ordering within each rank and the direction each numeral
        # is offered first. The semantics are untouched: the same specification goes in.
        outcome.scene = build_scene(spec, graph, profile,
                                    sheet_number=scene.sheet_number,
                                    sheet_total=scene.sheet_total,
                                    seed=attempt + 1)
        outcome.applied.append("laid the figure out again with a different arrangement")
        outcome.changed = True
        return outcome

    return outcome


def summarise(issues: Sequence[ValidationIssue]) -> str:
    """A one-paragraph diagnosis for a figure that could not be repaired."""
    blocking = [issue for issue in issues if issue.severity == "blocking"]
    if not blocking:
        return ""
    first = blocking[0]
    if len(blocking) == 1:
        return first.message
    others = len(blocking) - 1
    return (f"{first.message} And {others} further blocking problem"
            f"{'s' if others > 1 else ''} on this figure.")
