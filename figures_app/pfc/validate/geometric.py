"""Geometry rules: is the drawing readable, and does every numeral point where it should.

These are measurements, not impressions. The same functions the layout engine used to place
things are used here to check them, so a disagreement between placement and validation is
impossible by construction rather than by care.

The severities are set by what the defect costs. A numeral that points at the wrong object or
falls outside the office's required margin is a defect in the filing and blocks. Two connection
lines that cross is untidy and common in real patent drawings, so it is a warning: blocking on
it would refuse figures that examiners accept every day.
"""
from __future__ import annotations

from itertools import combinations

from ..geometry import (distance, polyline_hits_box, segments, segments_cross)
from ..numerals import sort_key
from ..schemas import Box, ValidationIssue
from .engine import ValidationContext, ValidationRule

MAX_LEADER_CROSSINGS = 0
MAX_EDGE_CROSSINGS = 6


class ComponentOverlap(ValidationRule):
    """GEO001 — two objects that are not nested must not sit on top of each other."""

    rule_id = "GEO001"
    severity = "blocking"
    category = "geometry"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        if figure.scene.artwork:
            # The parts were located in a drawing, not placed on a grid. In a perspective view a
            # pump behind a housing wall gives boxes that intersect, and that is the drawing
            # being correct. Refusing it would refuse every sheet this mode produces.
            return []
        gap = context.profile.min_component_gap
        issues: list[ValidationIssue] = []
        for first, second in combinations(figure.scene.nodes, 2):
            # One outline inside another is only legitimate when the outer one is a disclosed
            # container. Two ordinary components in the same place is an overlap however neatly
            # one happens to fit inside the other.
            if first.is_container and _nested(first.box, second.box):
                continue
            if second.is_container and _nested(second.box, first.box):
                continue
            if first.box.overlaps(second.box):
                issues.append(self.issue(
                    "Two components overlap on the sheet.",
                    entity_id=first.entity_id,
                    detail={"with": second.entity_id}))
            elif first.box.overlaps(second.box, gap):
                issues.append(ValidationIssue(
                    rule_id="GEO010", severity="warning", category="geometry",
                    repair_action="relayout",
                    message="Two components are closer than the profile's minimum gap.",
                    entity_id=first.entity_id, detail={"with": second.entity_id}))
        return issues


class LabelOverlap(ValidationRule):
    """GEO002 — two reference numerals must be separately readable."""

    rule_id = "GEO002"
    severity = "blocking"
    category = "geometry"
    repair_action = "move_label"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        gap = context.profile.min_label_gap
        issues = []
        for first, second in combinations(figure.scene.labels, 2):
            if first.box.overlaps(second.box, gap):
                issues.append(self.issue(
                    f"Reference numerals {first.reference_numeral} and "
                    f"{second.reference_numeral} overlap.",
                    reference_numeral=first.reference_numeral,
                    detail={"with": second.reference_numeral}))
        return issues


class LabelGeometryCollision(ValidationRule):
    """GEO003 — a numeral must not be printed over the artwork."""

    rule_id = "GEO003"
    severity = "blocking"
    category = "geometry"
    repair_action = "move_label"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        issues = []
        for label in figure.scene.labels:
            for node in figure.scene.nodes:
                if node.is_container or node.entity_id == label.entity_id:
                    continue
                if label.box.overlaps(node.box):
                    issues.append(self.issue(
                        f"Reference numeral {label.reference_numeral} is printed over another "
                        "component.", reference_numeral=label.reference_numeral,
                        entity_id=node.entity_id))
        return issues


class LeaderTarget(ValidationRule):
    """GEO004 — the leader ends on the outline of the object its numeral names."""

    rule_id = "GEO004"
    severity = "blocking"
    category = "geometry"
    repair_action = "rebind_leader"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        nodes = {node.entity_id: node for node in figure.scene.nodes}
        tolerance = context.profile.leader_clearance
        issues = []
        for label in figure.scene.labels:
            node = nodes.get(label.entity_id)
            if node is None:
                issues.append(self.issue(
                    f"Reference numeral {label.reference_numeral} has a leader but no object to "
                    "point at.", reference_numeral=label.reference_numeral))
                continue
            end = label.leader_points[-1]
            box = node.box.inflated(tolerance)
            if not (box.x <= end.x <= box.right and box.y <= end.y <= box.bottom):
                issues.append(self.issue(
                    f"The leader for reference numeral {label.reference_numeral} does not reach "
                    "the object it names.", reference_numeral=label.reference_numeral,
                    entity_id=label.entity_id))
        return issues


class AmbiguousLeader(ValidationRule):
    """GEO009 — a leader must not be readable as pointing at a neighbouring object."""

    rule_id = "GEO009"
    severity = "blocking"
    category = "geometry"
    repair_action = "reroute_leader"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        nodes = {node.entity_id: node for node in figure.scene.nodes}
        clearance = context.profile.leader_clearance
        issues = []
        for label in figure.scene.labels:
            owner = nodes.get(label.entity_id)
            if owner is None:
                continue
            if owner.is_container:
                # A container's leader lands on its own outline, which by definition has the
                # parts it holds just inside it. That is not ambiguity, it is containment.
                continue
            end = (label.leader_points[-1].x, label.leader_points[-1].y)
            for node in figure.scene.nodes:
                if node.entity_id == label.entity_id or node.is_container:
                    continue
                box = node.box.inflated(clearance)
                if box.x <= end[0] <= box.right and box.y <= end[1] <= box.bottom:
                    issues.append(self.issue(
                        f"The leader for reference numeral {label.reference_numeral} ends close "
                        "enough to another component to be read as naming it.",
                        reference_numeral=label.reference_numeral,
                        entity_id=label.entity_id, detail={"near": node.entity_id}))
                    break
        return issues


class LeaderCrossing(ValidationRule):
    """GEO005 — leaders belonging to different numerals must not cross."""

    rule_id = "GEO005"
    severity = "blocking"
    category = "geometry"
    repair_action = "reroute_leader"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        crossings = 0
        offenders: set[str] = set()
        for first, second in combinations(figure.scene.labels, 2):
            if first.entity_id == second.entity_id:
                continue
            hit = any(segments_cross(a, b)
                      for a in segments(first.leader_points)
                      for b in segments(second.leader_points))
            if hit:
                crossings += 1
                offenders.update({first.reference_numeral, second.reference_numeral})
        if crossings > MAX_LEADER_CROSSINGS:
            return [self.issue(
                f"{crossings} pair(s) of reference leaders cross, which makes it ambiguous which "
                "numeral belongs to which object.",
                detail={"count": crossings,
                        "references": sorted(offenders, key=sort_key)})]
        return []


class EdgeCrossing(ValidationRule):
    """GEO005b — connection lines crossing is untidy, not wrong."""

    rule_id = "GEO005B"
    severity = "warning"
    category = "geometry"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        crossings = 0
        for first, second in combinations(figure.scene.edges, 2):
            shared = {first.from_entity, first.to_entity} & {second.from_entity,
                                                             second.to_entity}
            if shared:
                continue
            crossings += sum(1 for a in segments(first.points) for b in segments(second.points)
                             if segments_cross(a, b))
        if crossings > MAX_EDGE_CROSSINGS:
            return [self.issue(
                f"{crossings} connection lines cross. The figure is readable but would be "
                "clearer rearranged.", detail={"count": crossings})]
        return []


class DrawingBounds(ValidationRule):
    """GEO006 / GEO007 — nothing is clipped and nothing enters the required blank margin."""

    rule_id = "GEO007"
    severity = "blocking"
    category = "jurisdiction"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        area = figure.scene.drawing_area
        issues: list[ValidationIssue] = []
        for node in figure.scene.nodes:
            if not _inside(node.box, area):
                issues.append(self.issue(
                    "A component crosses the blank margin the drawing rules require.",
                    entity_id=node.entity_id,
                    reference_numeral=node.reference_numeral))
        for label in figure.scene.labels:
            if not _inside(label.box, area):
                issues.append(self.issue(
                    f"Reference numeral {label.reference_numeral} crosses the blank margin the "
                    "drawing rules require.", reference_numeral=label.reference_numeral,
                    repair_action="move_label"))
            for point in label.leader_points:
                if not (area.x <= point.x <= area.right and area.y <= point.y <= area.bottom):
                    issues.append(ValidationIssue(
                        rule_id="GEO006", severity="blocking", category="jurisdiction",
                        repair_action="reroute_leader",
                        message=(f"The leader for reference numeral {label.reference_numeral} "
                                 "runs outside the drawing area and would be clipped."),
                        reference_numeral=label.reference_numeral))
                    break
        for edge in figure.scene.edges:
            for point in edge.points:
                if not (area.x <= point.x <= area.right and area.y <= point.y <= area.bottom):
                    issues.append(ValidationIssue(
                        rule_id="GEO006", severity="blocking", category="jurisdiction",
                        repair_action="relayout",
                        message="A connection line runs outside the drawing area.",
                        relation_id=edge.relation_id))
                    break
        return issues


class ArrowLabelCollision(ValidationRule):
    """GEO008 — an arrow must not run through a numeral."""

    rule_id = "GEO008"
    severity = "blocking"
    category = "geometry"
    repair_action = "move_label"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        issues = []
        for label in figure.scene.labels:
            for edge in figure.scene.edges:
                if polyline_hits_box(edge.points, label.box):
                    issues.append(self.issue(
                        f"A connection line runs through reference numeral "
                        f"{label.reference_numeral}.",
                        reference_numeral=label.reference_numeral,
                        relation_id=edge.relation_id))
                    break
        return issues


class Legibility(ValidationRule):
    """GEO010 — numerals are drawn at or above the size the office requires."""

    rule_id = "GEO010"
    severity = "blocking"
    category = "jurisdiction"
    repair_action = "none"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        minimum = context.profile.min_reference_height
        issues = []
        for label in figure.scene.labels:
            if label.text_height + 1e-6 < minimum:
                issues.append(self.issue(
                    f"Reference numeral {label.reference_numeral} is drawn at "
                    f"{context.profile.mm(label.text_height):.2f} mm, below the "
                    f"{context.profile.mm(minimum):.2f} mm minimum.",
                    reference_numeral=label.reference_numeral))
        return issues


class Monochrome(ValidationRule):
    """JUR001 — filing artwork is black lines on a white sheet, and vector only."""

    rule_id = "JUR001"
    severity = "blocking"
    category = "jurisdiction"
    repair_action = "none"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None or not figure.svg or not context.profile.monochrome:
            return []
        import re

        svg = figure.svg
        issues = []
        if "<image" in svg and not figure.scene.artwork:
            issues.append(self.issue("The drawing embeds a raster image."))
        colours = {value.lower() for value in re.findall(r"#[0-9A-Fa-f]{3,6}", svg)}
        stray = colours - {"#000000", "#ffffff"}
        if stray or "rgb(" in svg:
            issues.append(self.issue(
                f"The drawing uses colour: {', '.join(sorted(stray)) or 'rgb()'}."))
        return issues


def _inside(box: Box, area: Box) -> bool:
    return (area.x <= box.x and area.y <= box.y and box.right <= area.right
            and box.bottom <= area.bottom)


def _nested(outer: Box, inner: Box) -> bool:
    return (outer.x <= inner.x and outer.y <= inner.y and inner.right <= outer.right
            and inner.bottom <= outer.bottom)


GEOMETRY_RULES = [
    ComponentOverlap(), LabelOverlap(), LabelGeometryCollision(), LeaderTarget(),
    AmbiguousLeader(), LeaderCrossing(), EdgeCrossing(), DrawingBounds(),
    ArrowLabelCollision(), Legibility(), Monochrome(),
]
