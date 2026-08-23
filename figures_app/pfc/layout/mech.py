"""Mechanical schematic layout: conservative primitives, disclosed arrangement, nothing more.

The rule this module exists to obey is the hardest one in the specification to keep: never
invent the physical appearance of a component because you recognise its name. A patent that
says "sensor 120 is positioned within housing 110" has disclosed one fact about shape — that
120 is inside 110 — and this module draws exactly that fact: a plain outline inside a plain
outline, each carrying its own reference numeral.

So there is no rank and no flow here. Containment nests, disclosed above/below/left-of
arrangements order siblings, and everything else is packed into a near-square grid, because a
grid asserts nothing. A drawing that is abstract and right is worth more than one that is
detailed and imagined.
"""
from __future__ import annotations

import math
from typing import Optional

from ..geometry import boundary_point, ellipse_boundary_point, union
from ..numerals import sort_key
from ..profiles import DrawingProfile
from ..schemas import (Box, Entity, FigureSpec, LayoutEdge, LayoutNode, LayoutScene,
                       PatentGraph, Point, Relation)

# The primitive library, keyed by what the document actually disclosed. Everything a patent does
# not describe the shape of comes out as a plain rectangle.
_SHAPE_BY_HINT = {
    "rectangular": ("box", 1.6), "planar": ("plate", 2.6), "circular": ("circle", 1.0),
    "spherical": ("circle", 1.0), "elliptical": ("ellipse", 1.5),
    "annular": ("ellipse", 1.4), "cylindrical": ("cylinder", 1.1),
    "tubular": ("tube", 2.4), "conical": ("box", 1.4),
}
_SHAPE_BY_CLASS = {
    "housing": ("box", 1.5), "plate": ("plate", 2.4), "shaft": ("shaft", 3.0),
    "tube": ("tube", 2.4), "chamber": ("chamber", 1.4), "opening": ("opening", 1.0),
    "connector": ("box", 1.2), "boundary": ("container", 1.5),
}

_ARRANGEMENT = {"above": (0, -1), "below": (0, 1)}


def _primitive(entity: Entity, is_container: bool) -> tuple[str, float]:
    if is_container:
        return ("container", 1.4)
    if entity.shape_hint_grounded and entity.shape_hint in _SHAPE_BY_HINT:
        return _SHAPE_BY_HINT[entity.shape_hint]
    if entity.visual_class in _SHAPE_BY_CLASS:
        return _SHAPE_BY_CLASS[entity.visual_class]
    return ("box", 1.4)


class _Part:
    __slots__ = ("entity", "children", "parent", "box", "shape")

    def __init__(self, entity: Entity):
        self.entity = entity
        self.children: list[_Part] = []
        self.parent: Optional[_Part] = None
        self.box = Box(x=0.0, y=0.0, width=1.0, height=1.0)
        self.shape = "box"


def _grid(count: int) -> tuple[int, int]:
    columns = max(1, int(math.ceil(math.sqrt(count))))
    rows = max(1, int(math.ceil(count / columns)))
    return columns, rows


def _order_siblings(parts: list[_Part], arrangement: list[tuple[str, str, str]]) -> list[_Part]:
    """Sort siblings so a disclosed above/below/left-of arrangement is respected.

    A stable insertion sort over the stated pairs, falling back to reference-numeral order. It
    is not a constraint solver and does not pretend to be: contradictory statements simply leave
    the numeral order in place, and the semantic validators report the contradiction.
    """
    order = sorted(parts, key=lambda part: sort_key(part.entity.reference_numeral or ""))
    index = {part.entity.id: position for position, part in enumerate(order)}
    for _ in range(len(order)):
        moved = False
        for predicate, first, second in arrangement:
            if first not in index or second not in index:
                continue
            if predicate in {"above", "left_of", "upstream_of"} and index[first] > index[second]:
                order.insert(index[second], order.pop(index[first]))
                moved = True
            elif predicate in {"below"} and index[first] < index[second]:
                order.insert(index[second], order.pop(index[first]))
                moved = True
            if moved:
                index = {part.entity.id: position for position, part in enumerate(order)}
                break
        if not moved:
            break
    return order


def layout_mechanical(spec: FigureSpec, graph: PatentGraph, profile: DrawingProfile,
                      *, sheet_number: int = 1, sheet_total: int = 1,
                      seed: int = 0) -> LayoutScene:
    from .graphlayout import _containment_parent

    relations = {relation.id: relation for relation in graph.relations}
    entities = {spec_entity.entity_id: graph.entity(spec_entity.entity_id)
                for spec_entity in spec.entities}
    entities = {key: value for key, value in entities.items() if value is not None}
    roles = {spec_entity.entity_id: spec_entity.role for spec_entity in spec.entities}

    parent_of = _containment_parent(spec, relations)
    parts = {eid: _Part(entity) for eid, entity in entities.items()}
    roots: list[_Part] = []
    for eid, part in parts.items():
        parent_id = parent_of.get(eid)
        if parent_id in parts and parent_id != eid:
            part.parent = parts[parent_id]
            parts[parent_id].children.append(part)
        else:
            roots.append(part)

    arrangement: list[tuple[str, str, str]] = []
    for spec_relation in spec.relations:
        relation = relations.get(spec_relation.relation_id)
        if relation is not None and relation.predicate in {"above", "below", "upstream_of"}:
            arrangement.append((relation.predicate, relation.subject, relation.object))
    for constraint in spec.layout_constraints:
        if constraint.type in {"left_of", "above"}:
            arrangement.append((constraint.type, constraint.a, constraint.b))

    unit = profile.min_node_width
    gap = profile.sibling_gap * (1.0 + 0.25 * (seed % 3))

    def arrange(group: list[_Part]) -> Box:
        for part in group:
            shape, ratio = _primitive(part.entity, bool(part.children))
            part.shape = shape
            if part.children:
                inner = arrange(part.children)
                pad = profile.container_padding
                part.box = Box(x=0.0, y=0.0, width=inner.width + 2 * pad,
                               height=inner.height + 2 * pad)
                for child in part.children:
                    _shift(child, pad - inner.x, pad - inner.y)
            else:
                height = max(profile.min_node_height, unit / max(ratio, 0.4))
                part.box = Box(x=0.0, y=0.0, width=max(profile.min_node_width, height * ratio),
                               height=height)
        ordered = _order_siblings(group, arrangement)
        columns, _ = _grid(len(ordered))
        column_width = max(part.box.width for part in ordered) + gap
        row_height = max(part.box.height for part in ordered) + gap
        for position, part in enumerate(ordered):
            row, column = divmod(position, columns)
            _move(part, column * column_width + (column_width - gap - part.box.width) / 2,
                  row * row_height + (row_height - gap - part.box.height) / 2)
        return union(part.box for part in ordered) or Box(x=0.0, y=0.0, width=1.0, height=1.0)

    bounds = arrange(roots)
    everything = list(parts.values())

    area = Box(x=profile.drawing_left, y=profile.drawing_top,
               width=profile.drawing_width, height=profile.drawing_height)
    reserve = profile.reference_height * 4.5
    usable_w = max(1.0, area.width - 2 * reserve)
    usable_h = max(1.0, area.height - 2 * reserve - profile.caption_height * 3)
    factor = min(1.0, usable_w / max(1.0, bounds.width), usable_h / max(1.0, bounds.height))
    if factor < 1.0:
        for part in everything:
            part.box = Box(
                x=bounds.x + (part.box.x - bounds.x) * factor,
                y=bounds.y + (part.box.y - bounds.y) * factor,
                width=max(profile.min_node_width * 0.5, part.box.width * factor),
                height=max(profile.min_node_height * 0.5, part.box.height * factor))
        bounds = union(part.box for part in everything) or bounds
    dx = area.x + reserve + (usable_w - bounds.width) / 2 - bounds.x
    dy = area.y + reserve + (usable_h - bounds.height) / 2 - bounds.y
    for part in everything:
        part.box = Box(x=part.box.x + dx, y=part.box.y + dy,
                       width=part.box.width, height=part.box.height)

    nodes = [
        LayoutNode(entity_id=part.entity.id,
                   reference_numeral=part.entity.reference_numeral,
                   caption="", shape=part.shape,  # type: ignore[arg-type]
                   box=part.box, depth=_depth(part), is_container=bool(part.children),
                   role=roles.get(part.entity.id, "primary"))  # type: ignore[arg-type]
        for part in sorted(everything,
                           key=lambda p: (_depth(p), sort_key(p.entity.reference_numeral or "")))]
    by_id = {node.entity_id: node for node in nodes}

    edges: list[LayoutEdge] = []
    for spec_relation in spec.relations:
        if spec_relation.visual_representation == "containment":
            continue
        relation = relations.get(spec_relation.relation_id)
        if relation is None:
            continue
        source, target = by_id.get(relation.subject), by_id.get(relation.object)
        if source is None or target is None:
            continue
        if _nests(source, target) or _nests(target, source):
            continue
        start = _edge_point(source, (target.box.cx, target.box.cy))
        end = _edge_point(target, (source.box.cx, source.box.cy))
        directed = relation.direction == "subject_to_object"
        edges.append(LayoutEdge(
            relation_id=relation.id, from_entity=relation.subject, to_entity=relation.object,
            edge_type=spec_relation.visual_representation,
            points=[Point(x=start[0], y=start[1]), Point(x=end[0], y=end[1])],
            arrow_at_end=directed and spec_relation.visual_representation in {
                "data_flow", "control_flow", "movement"},
            arrow_at_start=spec_relation.visual_representation == "bidirectional_association"))
        if spec_relation.visual_representation == "bidirectional_association":
            edges[-1].arrow_at_end = True

    return LayoutScene(
        figure_id=spec.figure_id, figure_number=spec.figure_number,
        figure_type=spec.figure_type, profile_id=profile.version_tag,
        sheet_width=profile.sheet_width, sheet_height=profile.sheet_height,
        drawing_area=area, nodes=nodes, edges=edges, labels=[], caption=spec.title,
        sheet_number=sheet_number, sheet_total=sheet_total)


def _edge_point(node: LayoutNode, toward: tuple[float, float]) -> tuple[float, float]:
    if node.shape in {"circle", "ellipse", "cylinder"}:
        return ellipse_boundary_point(node.box, toward)
    return boundary_point(node.box, toward)


def _nests(outer: LayoutNode, inner: LayoutNode) -> bool:
    return (outer.is_container and outer.box.x <= inner.box.x and outer.box.y <= inner.box.y
            and inner.box.right <= outer.box.right and inner.box.bottom <= outer.box.bottom)


def _move(part: _Part, x: float, y: float) -> None:
    _shift(part, x - part.box.x, y - part.box.y)


def _shift(part: _Part, dx: float, dy: float) -> None:
    part.box = Box(x=part.box.x + dx, y=part.box.y + dy,
                   width=part.box.width, height=part.box.height)
    for child in part.children:
        _shift(child, dx, dy)


def _depth(part: _Part) -> int:
    depth = 0
    walker = part.parent
    while walker is not None:
        depth += 1
        walker = walker.parent
    return depth
