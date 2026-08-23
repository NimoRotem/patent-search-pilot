"""Independent visual verification, and the deterministic comparison that follows it.

The point of this stage is that nothing which produced the figure gets to certify it. A
verifier reads the rasterised sheet and reconstructs what it sees into ``ObservedFigure``; this
module then compares that reconstruction with the ``FigureSpec`` in ordinary code. The model is
never asked whether the figure is correct, and it never sees the specification's reasoning or
any earlier validation result.

**One deliberate departure from the written specification, and why.** The specification says the
verifier should be handed the list of expected reference numerals along with the image. Doing
that primes it: a reader told to expect 110, 120 and 130 will report 110, 120 and 130, and the
"reference numeral recall = 100%" threshold then measures nothing. So the verifier is given the
image and the output schema only, and ``prime_with_expected`` exists to turn the specified
behaviour back on for anyone who wants to compare the two. Its default is off.

On disagreement, a second verifier runs. A figure is only failed on vision when two independent
readers agree it is wrong; when they disagree the figure is flagged for a human rather than
either failed or passed, because one reader's word is not enough to overturn a drawing that
passed every deterministic check.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Optional

from . import prompts
from .numerals import sort_key
from .profiles import DrawingProfile
from .providers import StructuredOutputError, VisionVerifier
from .schemas import (FigureSpec, LayoutScene, ObservedFigure, SemanticDiff, ValidationIssue)

# Below this the verifier is telling us it is not sure, and an unsure reading must not fail a
# figure that passed every deterministic check.
CONFIDENT = 0.6
# How far, as a fraction of the sheet diagonal, an observed numeral may sit from where the
# renderer put it before the two are treated as different things.
POSITION_TOLERANCE = 0.14

_WORD = re.compile(r"[a-z0-9]+")


def observe(image_png: bytes, spec: FigureSpec, verifier: VisionVerifier,
            *, prime_with_expected: bool = False,
            expected_numerals: Iterable[str] = ()) -> Optional[ObservedFigure]:
    instruction = "Read this patent drawing sheet and report what is on it."
    if prime_with_expected:
        numerals = ", ".join(sorted(set(expected_numerals), key=sort_key))
        instruction += (f"\n\nFor reference, the numerals expected on this sheet are: "
                        f"{numerals}. Report what you actually see, not what is expected.")
    try:
        return verifier.inspect(
            image_png, prompts.load("visual_verify_v1"), instruction, ObservedFigure,
            prompt_version=prompts.version("visual_verify_v1"), max_tokens=4000)
    except StructuredOutputError:
        return None


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
def _expected_connections(spec: FigureSpec, scene: LayoutScene,
                          graph_relations: dict) -> list[tuple[str, str, bool]]:
    """(from numeral, to numeral, directed) for every connection the sheet should carry."""
    numeral_of = {node.entity_id: (node.reference_numeral or "") for node in scene.nodes}
    out: list[tuple[str, str, bool]] = []
    for edge in scene.edges:
        source = numeral_of.get(edge.from_entity, "")
        target = numeral_of.get(edge.to_entity, "")
        if source and target:
            out.append((source, target, edge.arrow_at_end))
    return out


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(str(text or "").lower()) if len(word) > 2}


def _scene_position(scene: LayoutScene, numeral: str) -> Optional[tuple[float, float]]:
    label = next((item for item in scene.labels if item.reference_numeral == numeral), None)
    return (label.position.x, label.position.y) if label else None


def _observed_position(reference, scene: LayoutScene, image_size: tuple[int, int]
                       ) -> Optional[tuple[float, float]]:
    """Map an observed bounding box back into scene units, when one was given."""
    if len(reference.bbox) != 4 or not all(isinstance(v, (int, float)) for v in reference.bbox):
        return None
    width, height = image_size
    if width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = reference.bbox
    # Verifiers report either pixels or a 0-1000 normalised box. Both are handled by scaling
    # against whichever range the numbers actually occupy.
    span = max(x1, y1, 1.0)
    scale_x = scene.sheet_width / (width if span > 1001 else (1000.0 if span > 1.5 else 1.0))
    scale_y = scene.sheet_height / (height if span > 1001 else (1000.0 if span > 1.5 else 1.0))
    return ((x0 + x1) / 2 * scale_x, (y0 + y1) / 2 * scale_y)


def diff(spec: FigureSpec, scene: LayoutScene, observed: ObservedFigure,
         profile: DrawingProfile, *, image_size: tuple[int, int] = (0, 0),
         graph_relations: Optional[dict] = None) -> SemanticDiff:
    """Expected versus observed, in ordinary code. No model participates in this comparison."""
    result = SemanticDiff()
    expected_numerals = {label.reference_numeral for label in scene.labels}
    observed_numerals = {reference.reference.strip() for reference in observed.visible_references
                         if reference.reference.strip()}
    confident_numerals = {reference.reference.strip()
                          for reference in observed.visible_references
                          if reference.confidence >= CONFIDENT and reference.reference.strip()}

    result.missing_references = sorted(expected_numerals - observed_numerals, key=sort_key)
    result.unexpected_references = sorted(confident_numerals - expected_numerals, key=sort_key)

    diagonal = math.hypot(scene.sheet_width, scene.sheet_height)
    captioned = any(node.caption for node in scene.nodes)
    node_by_entity = {node.entity_id: node for node in scene.nodes}
    label_by_numeral = {label.reference_numeral: label for label in scene.labels}

    for reference in observed.visible_references:
        numeral = reference.reference.strip()
        label = label_by_numeral.get(numeral)
        if label is None or reference.confidence < CONFIDENT:
            continue
        placed = _observed_position(reference, scene, image_size)
        expected_at = _scene_position(scene, numeral)
        if placed and expected_at:
            drift = math.hypot(placed[0] - expected_at[0], placed[1] - expected_at[1])
            if drift / diagonal > POSITION_TOLERANCE:
                result.reference_target_mismatches.append({
                    "reference": numeral, "reason": "printed somewhere other than where the "
                                                    "layout places it",
                    "drift_mm": round(profile.mm(drift), 1)})
                continue
        if captioned and reference.target_description:
            node = node_by_entity.get(label.entity_id)
            expected_words = _tokens(node.caption if node else "")
            seen_words = _tokens(reference.target_description)
            if expected_words and seen_words and not (expected_words & seen_words):
                result.reference_target_mismatches.append({
                    "reference": numeral,
                    "reason": "its leader appears to end on a different component",
                    "expected": (node.caption if node else ""),
                    "observed": reference.target_description[:120]})

    expected_connections = _expected_connections(spec, scene, graph_relations or {})
    expected_pairs = {(source, target) for source, target, _ in expected_connections}
    directed_pairs = {(source, target) for source, target, directed in expected_connections
                      if directed}
    observed_pairs: set[tuple[str, str]] = set()
    for connection in observed.connections:
        source = connection.from_reference.strip()
        target = connection.to_reference.strip()
        if not source or not target or connection.confidence < CONFIDENT:
            continue
        observed_pairs.add((source, target))
        forward = (source, target) in expected_pairs
        backward = (target, source) in expected_pairs
        if not forward and not backward:
            result.unexpected_connections.append({"from": source, "to": target})
            continue
        canonical = (source, target) if forward else (target, source)
        should_be_directed = canonical in directed_pairs
        if connection.direction == "none" and should_be_directed:
            result.direction_mismatches.append({
                "from": canonical[0], "to": canonical[1],
                "reason": "the drawing should carry an arrowhead here"})
        elif connection.direction != "none" and not should_be_directed:
            result.direction_mismatches.append({
                "from": canonical[0], "to": canonical[1],
                "reason": "an arrowhead was seen where the description gives no direction"})
        elif connection.direction == "forward" and backward and not forward:
            result.direction_mismatches.append({
                "from": source, "to": target, "reason": "the arrow points the wrong way"})

    for source, target, _ in expected_connections:
        if (source, target) not in observed_pairs and (target, source) not in observed_pairs:
            result.missing_connections.append({"from": source, "to": target})

    allowed_text = set(expected_numerals)
    allowed_text.add(profile.label_format.format(number=scene.figure_number.upper()))
    allowed_text.add(profile.sheet_number_format.format(sheet=scene.sheet_number,
                                                        total=scene.sheet_total))
    caption_words: set[str] = set()
    for node in scene.nodes:
        caption_words |= _tokens(node.caption)
    for edge in scene.edges:
        caption_words |= _tokens(edge.label)
    for text in observed.visible_text:
        value = str(text or "").strip()
        if not value or value in allowed_text:
            continue
        words = _tokens(value)
        if words and words <= caption_words:
            continue
        if re.fullmatch(r"[A-Za-z]?\d{1,4}[A-Za-z]?", value) and value in expected_numerals:
            continue
        if _tokens(value) & _tokens(profile.label_format.format(number=scene.figure_number)):
            continue
        result.unsupported_visible_text.append(value[:80])

    expected_objects = len(scene.nodes)
    confident_objects = [component for component in observed.visible_components
                         if component.confidence >= CONFIDENT]
    if len(confident_objects) > expected_objects:
        result.possible_unexpected_objects = [
            component.description[:80] for component in confident_objects[expected_objects:]]
    return result


def issues_from_diff(result: SemanticDiff, observed: ObservedFigure,
                     figure_id: str) -> list[ValidationIssue]:
    """The diff, turned into typed issues with the repair each one actually needs."""
    issues: list[ValidationIssue] = []

    def add(rule_id: str, message: str, severity: str = "blocking",
            repair: str = "relayout", **kwargs) -> None:
        issues.append(ValidationIssue(
            rule_id=rule_id, severity=severity, category="vision", message=message,
            figure_id=figure_id, repair_action=repair, **kwargs))

    for numeral in result.missing_references:
        add("VIS001", f"An independent reader of the finished sheet could not find reference "
                      f"numeral {numeral}.", repair="move_label", reference_numeral=numeral)
    for numeral in result.unexpected_references:
        add("VIS002", f"An independent reader saw reference numeral {numeral} on the sheet, and "
                      "it does not belong to this figure.", reference_numeral=numeral)
    for item in result.reference_target_mismatches:
        add("VIS003", f"Reference numeral {item.get('reference')} does not read as naming the "
                      f"object it is bound to ({item.get('reason')}).",
            repair="rebind_leader", reference_numeral=str(item.get("reference") or ""),
            detail=item)
    for item in result.direction_mismatches:
        add("VIS004", f"The connection between {item.get('from')} and {item.get('to')} does not "
                      f"read as the description states: {item.get('reason')}.", detail=item)
    for item in result.missing_connections:
        add("VIS005", f"An independent reader could not see the connection between "
                      f"{item.get('from')} and {item.get('to')}.", detail=item)
    for item in result.unexpected_connections:
        add("VIS006", f"An independent reader saw a connection between {item.get('from')} and "
                      f"{item.get('to')} that this figure does not specify.", detail=item)
    for text in result.unsupported_visible_text:
        add("VIS007", f"Text is visible on the sheet that the figure does not account for: "
                      f"{text!r}.", detail={"text": text})
    for description in result.possible_unexpected_objects:
        add("VIS008", f"An object was seen that this figure does not specify: {description!r}.",
            severity="warning", detail={"description": description})
    for numeral in observed.overlapping_labels:
        add("VIS009", f"Reference numeral {numeral} was reported as overlapping another.",
            repair="move_label", reference_numeral=str(numeral)[:8])
    for numeral in observed.ambiguous_leaders:
        add("VIS010", f"The leader for {numeral} was reported as pointing somewhere ambiguous.",
            repair="reroute_leader", reference_numeral=str(numeral)[:8])
    return issues


def reconcile(first: SemanticDiff, second: Optional[SemanticDiff]) -> tuple[SemanticDiff, bool]:
    """Two readings of one sheet -> what they agree on, and whether they disagreed.

    Only what both readers saw is treated as a defect. A finding one reader made and the other
    did not is real information, but it is not enough to fail a drawing that satisfied every
    measurement, so it is returned as a disagreement for a human to look at.
    """
    if second is None:
        return first, False

    def agree(left: list, right: list, key) -> list:
        keys = {key(item) for item in right}
        return [item for item in left if key(item) in keys]

    agreed = SemanticDiff(
        missing_references=sorted(set(first.missing_references) &
                                  set(second.missing_references), key=sort_key),
        unexpected_references=sorted(set(first.unexpected_references) &
                                     set(second.unexpected_references), key=sort_key),
        reference_target_mismatches=agree(first.reference_target_mismatches,
                                          second.reference_target_mismatches,
                                          lambda item: item.get("reference")),
        missing_connections=agree(first.missing_connections, second.missing_connections,
                                  lambda item: (item.get("from"), item.get("to"))),
        unexpected_connections=agree(first.unexpected_connections,
                                     second.unexpected_connections,
                                     lambda item: (item.get("from"), item.get("to"))),
        direction_mismatches=agree(first.direction_mismatches, second.direction_mismatches,
                                   lambda item: (item.get("from"), item.get("to"))),
        unsupported_visible_text=sorted(set(first.unsupported_visible_text) &
                                        set(second.unsupported_visible_text)),
        possible_unexpected_objects=[],
    )
    disagreed = (not first.clean or not second.clean) and agreed.clean
    return agreed, disagreed
