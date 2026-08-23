"""Layered layout for the diagram figures: block, data flow, logical, network, state.

The coordinates come from the topology, never from a language model. A model asked for
positions produces plausible numbers that overlap, and there is then no principle by which to
repair them; a solver produces positions that can be re-derived, so the correction loop can
change one input and get a different, still-correct drawing.

The method is the classic layered one, kept small:

1. **Containment first.** A disclosed "inside"/"contains" relationship nests one node in
   another, and each container's children are laid out in their own coordinate frame.
2. **Rank by flow.** Only the relations that carry a disclosed direction set rank, by longest
   path. Undirected relations tie nodes together without claiming an order.
3. **Order to reduce crossings.** Median heuristic, a few forward and backward sweeps, keeping
   the best arrangement seen. Ties break on reference numeral so the same input always produces
   the same drawing.
4. **Place, then fit.** Ranks become columns, the whole assembly is scaled once to the drawing
   area, and nothing is stretched non-uniformly.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .. import textfit
from ..geometry import boundary_point, orthogonal_route, union
from ..numerals import sort_key
from ..profiles import DrawingProfile
from ..schemas import (Box, Entity, FigureSpec, LayoutEdge, LayoutNode, LayoutScene,
                       PatentGraph, Point, Relation, VisualRepresentation)

# Representations that impose an order on the page.
_ORDERING = {"data_flow", "control_flow", "process_sequence"}
_CONTAINMENT = "containment"

MAX_ORDER_SWEEPS = 8
MAX_CAPTION_LINES = 3
# How far the finished assembly may be scaled to fit the sheet.
MIN_SCALE = 0.25
MAX_SCALE = 2.2


class _Node:
    __slots__ = ("entity", "children", "parent", "box", "rank", "order", "lines")

    def __init__(self, entity: Entity):
        self.entity = entity
        self.children: list[_Node] = []
        self.parent: Optional[_Node] = None
        self.box = Box(x=0.0, y=0.0, width=1.0, height=1.0)
        self.rank = 0
        self.order = 0.0
        self.lines: list[str] = []


def _shape_for(entity: Entity, is_container: bool) -> str:
    if is_container:
        return "container"
    if entity.shape_hint_grounded:
        return {"circular": "circle", "elliptical": "ellipse", "cylindrical": "cylinder",
                "annular": "ellipse", "spherical": "circle", "tubular": "tube",
                "planar": "plate", "rectangular": "box", "conical": "box"}.get(
                    entity.shape_hint or "", "box")
    if entity.visual_class in {"data_store", "storage", "memory"}:
        return "cylinder"
    if entity.visual_class == "decision":
        return "diamond"
    if entity.visual_class in {"terminator"}:
        return "stadium"
    return "box"


def _containment_parent(spec: FigureSpec, relations: dict[str, Relation]
                        ) -> dict[str, str]:
    """child entity id -> parent entity id, from disclosed containment only."""
    parent: dict[str, str] = {}
    for spec_relation in spec.relations:
        if spec_relation.visual_representation != _CONTAINMENT:
            continue
        relation = relations.get(spec_relation.relation_id)
        if relation is None:
            continue
        if relation.predicate in {"contains", "surrounds"}:
            child, container = relation.object, relation.subject
        else:  # inside
            child, container = relation.subject, relation.object
        parent.setdefault(child, container)
    # A boundary role is a container for everything the figure does not already nest.
    boundary = next((e.entity_id for e in spec.entities if e.role == "boundary"), None)
    if boundary:
        parent.pop(boundary, None)
        for entity in spec.entities:
            if entity.entity_id != boundary and entity.entity_id not in parent:
                parent[entity.entity_id] = boundary
    # Break any cycle the document's wording produced; a cycle here is a drafting contradiction
    # and is reported by the semantic validators, not silently nested.
    for child in list(parent):
        seen = {child}
        walker = parent.get(child)
        while walker is not None:
            if walker in seen:
                parent.pop(child, None)
                break
            seen.add(walker)
            walker = parent.get(walker)
    return parent


def _rank(nodes: list[_Node], edges: list[tuple[str, str]]) -> None:
    """Longest-path ranking over the ordering edges among one set of siblings."""
    index = {node.entity.id: node for node in nodes}
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        if source in index and target in index and source != target:
            outgoing[source].append(target)
            incoming[target].append(source)

    ranks: dict[str, int] = {node.entity.id: 0 for node in nodes}
    # Iterate to a fixed point, bounded by the node count: a cycle stops moving instead of
    # looping forever, which is the right answer for a document that describes one.
    for _ in range(len(nodes) + 1):
        changed = False
        for node in sorted(nodes, key=lambda n: sort_key(n.entity.reference_numeral or "")):
            best = 0
            for source in incoming[node.entity.id]:
                best = max(best, ranks[source] + 1)
            if best != ranks[node.entity.id]:
                ranks[node.entity.id] = best
                changed = True
        if not changed:
            break
    for node in nodes:
        node.rank = ranks[node.entity.id]


def _order(nodes: list[_Node], edges: list[tuple[str, str]], seed: int = 0) -> None:
    """Median-heuristic ordering within each rank, minimising crossings."""
    by_rank: dict[int, list[_Node]] = defaultdict(list)
    for node in nodes:
        by_rank[node.rank].append(node)
    for rank in by_rank:
        by_rank[rank].sort(key=lambda n: sort_key(n.entity.reference_numeral or ""))
        for position, node in enumerate(by_rank[rank]):
            node.order = float((position + seed) % max(1, len(by_rank[rank])))
        by_rank[rank].sort(key=lambda n: n.order)

    neighbours: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        neighbours[source].append(target)
        neighbours[target].append(source)
    index = {node.entity.id: node for node in nodes}
    ranks = sorted(by_rank)

    def crossings() -> int:
        total = 0
        for position in range(len(ranks) - 1):
            left = {node.entity.id: order for order, node in enumerate(by_rank[ranks[position]])}
            right = {node.entity.id: order
                     for order, node in enumerate(by_rank[ranks[position + 1]])}
            pairs = [(left[s], right[t]) for s, t in edges
                     if s in left and t in right]
            for i, first in enumerate(pairs):
                for second in pairs[i + 1:]:
                    if (first[0] - second[0]) * (first[1] - second[1]) < 0:
                        total += 1
        return total

    best_order = {node.entity.id: node.order for node in nodes}
    best = crossings()
    for sweep in range(MAX_ORDER_SWEEPS):
        sequence = ranks if sweep % 2 == 0 else list(reversed(ranks))
        for rank in sequence:
            for node in by_rank[rank]:
                positions = [by_rank[index[other].rank].index(index[other])
                             for other in neighbours[node.entity.id]
                             if other in index and index[other].rank != rank]
                if positions:
                    positions.sort()
                    middle = len(positions) // 2
                    node.order = (positions[middle] if len(positions) % 2
                                  else (positions[middle - 1] + positions[middle]) / 2)
            by_rank[rank].sort(key=lambda n: (n.order,
                                              sort_key(n.entity.reference_numeral or "")))
            for position, node in enumerate(by_rank[rank]):
                node.order = float(position)
        score = crossings()
        if score < best:
            best = score
            best_order = {node.entity.id: node.order for node in nodes}
    for node in nodes:
        node.order = best_order[node.entity.id]
    for rank in by_rank:
        by_rank[rank].sort(key=lambda n: (n.order, sort_key(n.entity.reference_numeral or "")))
        for position, node in enumerate(by_rank[rank]):
            node.order = float(position)


def _size(node: _Node, profile: DrawingProfile, caption: bool) -> None:
    height = profile.caption_height
    if caption:
        target_width = max(profile.min_node_width, profile.min_node_width * 1.6)
        node.lines = textfit.wrap(profile, node.entity.canonical_name, target_width, height,
                                  MAX_CAPTION_LINES)
        width = max(profile.min_node_width,
                    textfit.block_width(profile, node.lines, height) + height * 1.6)
        body = textfit.block_height(node.lines, height)
        node.box = Box(x=0.0, y=0.0, width=width,
                       height=max(profile.min_node_height, body + height * 1.2))
    else:
        node.lines = []
        node.box = Box(x=0.0, y=0.0, width=profile.min_node_width * 1.2,
                       height=profile.min_node_height * 1.2)


def _place(nodes: list[_Node], profile: DrawingProfile) -> Box:
    """Ranks become columns; each column is centred vertically. Returns the assembly bounds."""
    if not nodes:
        return Box(x=0.0, y=0.0, width=1.0, height=1.0)
    by_rank: dict[int, list[_Node]] = defaultdict(list)
    for node in nodes:
        by_rank[node.rank].append(node)
    for rank in by_rank:
        by_rank[rank].sort(key=lambda n: n.order)

    column_heights = {
        rank: sum(n.box.height for n in members) + profile.sibling_gap * (len(members) - 1)
        for rank, members in by_rank.items()}
    tallest = max(column_heights.values(), default=1.0)

    x = 0.0
    for rank in sorted(by_rank):
        members = by_rank[rank]
        width = max(node.box.width for node in members)
        y = (tallest - column_heights[rank]) / 2
        for node in members:
            node.box = Box(x=x + (width - node.box.width) / 2, y=y,
                           width=node.box.width, height=node.box.height)
            y += node.box.height + profile.sibling_gap
        x += width + profile.rank_gap
    return union(node.box for node in nodes) or Box(x=0.0, y=0.0, width=1.0, height=1.0)


def _translate(nodes: list[_Node], dx: float, dy: float) -> None:
    for node in nodes:
        node.box = Box(x=node.box.x + dx, y=node.box.y + dy,
                       width=node.box.width, height=node.box.height)


def _scale(nodes: list[_Node], factor: float, origin: tuple[float, float]) -> None:
    for node in nodes:
        node.box = Box(
            x=origin[0] + (node.box.x - origin[0]) * factor,
            y=origin[1] + (node.box.y - origin[1]) * factor,
            width=max(1.0, node.box.width * factor),
            height=max(1.0, node.box.height * factor))


def layout_graph(spec: FigureSpec, graph: PatentGraph, profile: DrawingProfile,
                 *, sheet_number: int = 1, sheet_total: int = 1, seed: int = 0,
                 captions: bool = True) -> LayoutScene:
    relations = {relation.id: relation for relation in graph.relations}
    entities = {spec_entity.entity_id: graph.entity(spec_entity.entity_id)
                for spec_entity in spec.entities}
    entities = {key: value for key, value in entities.items() if value is not None}
    roles = {spec_entity.entity_id: spec_entity.role for spec_entity in spec.entities}

    parent_of = _containment_parent(spec, relations)
    nodes = {eid: _Node(entity) for eid, entity in entities.items()}
    roots: list[_Node] = []
    for eid, node in nodes.items():
        parent_id = parent_of.get(eid)
        if parent_id in nodes and parent_id != eid:
            node.parent = nodes[parent_id]
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)

    ordering_edges: list[tuple[str, str]] = []
    for spec_relation in spec.relations:
        relation = relations.get(spec_relation.relation_id)
        if relation is None:
            continue
        if spec_relation.visual_representation in _ORDERING:
            ordering_edges.append((relation.subject, relation.object))

    def arrange(group: list[_Node]) -> Box:
        """Lay out one sibling group, recursing into any container among them."""
        for node in group:
            if node.children:
                inner = arrange(node.children)
                header = profile.caption_height * 1.8 if captions else profile.container_padding
                node.box = Box(
                    x=0.0, y=0.0,
                    width=inner.width + 2 * profile.container_padding,
                    height=inner.height + profile.container_padding + header)
                _translate(node.children,
                           profile.container_padding - inner.x,
                           header - inner.y)
                node.lines = textfit.wrap(
                    profile, node.entity.canonical_name,
                    node.box.width - 2 * profile.container_padding,
                    profile.caption_height, 1) if captions else []
            else:
                _size(node, profile, captions)
        ids = {node.entity.id for node in group}
        edges = [(s, t) for s, t in ordering_edges if s in ids and t in ids]
        _rank(group, edges)
        _order(group, edges, seed)
        before = {node.entity.id: (node.box.x, node.box.y) for node in group}
        bounds = _place(group, profile)
        for node in group:
            dx = node.box.x - before[node.entity.id][0]
            dy = node.box.y - before[node.entity.id][1]
            if node.children and (dx or dy):
                _translate(node.children, dx, dy)
        return bounds

    bounds = arrange(roots)
    everything = list(nodes.values())
    area = Box(x=profile.drawing_left, y=profile.drawing_top,
               width=profile.drawing_width, height=profile.drawing_height)
    # Leave room around the assembly for reference numerals and their leaders, which live
    # outside the geometry they point at.
    reserve = profile.reference_height * 4
    usable_w = max(1.0, area.width - 2 * reserve)
    usable_h = max(1.0, area.height - 2 * reserve - profile.caption_height * 3)
    # Fit the assembly to the sheet in BOTH directions. Only ever shrinking left a four-box
    # figure sitting in the middle third of a letter page surrounded by white, which is not
    # what a patent drawing looks like and makes a 3.6 mm numeral look like a misprint. The
    # upper bound stops three boxes being blown up into wall art.
    factor = min(usable_w / max(1.0, bounds.width), usable_h / max(1.0, bounds.height))
    factor = max(MIN_SCALE, min(MAX_SCALE, factor))
    if abs(factor - 1.0) > 0.01:
        _scale(everything, factor, (bounds.x, bounds.y))
        bounds = union(node.box for node in everything) or bounds
    _translate(everything,
               area.x + reserve + (usable_w - bounds.width) / 2 - bounds.x,
               area.y + reserve + (usable_h - bounds.height) / 2 - bounds.y)

    # The caption carries the entity's FULL name, not the lines the sizing pass wrapped it
    # into. Those lines were computed against the box BEFORE the assembly was scaled to the
    # sheet, so a name elided there ("first platform" -> "first...") could never be recovered
    # by the renderer however much room it later had. Sizing wraps; the renderer decides what
    # is actually printed.
    layout_nodes = [
        LayoutNode(
            entity_id=node.entity.id,
            reference_numeral=node.entity.reference_numeral,
            caption=(node.entity.canonical_name if (node.lines or captions) else ""),
            shape=_shape_for(node.entity, bool(node.children)),  # type: ignore[arg-type]
            box=node.box, depth=_depth(node), is_container=bool(node.children),
            role=roles.get(node.entity.id, "primary"))  # type: ignore[arg-type]
        for node in sorted(everything, key=lambda n: (_depth(n),
                                                      sort_key(n.entity.reference_numeral or "")))]

    layout_edges = _route(spec, relations, {node.entity_id: node for node in layout_nodes})

    return LayoutScene(
        figure_id=spec.figure_id, figure_number=spec.figure_number,
        figure_type=spec.figure_type, profile_id=profile.version_tag,
        sheet_width=profile.sheet_width, sheet_height=profile.sheet_height,
        drawing_area=area, nodes=layout_nodes, edges=layout_edges, labels=[],
        caption=spec.title, sheet_number=sheet_number, sheet_total=sheet_total)


def _depth(node: _Node) -> int:
    depth = 0
    walker = node.parent
    while walker is not None:
        depth += 1
        walker = walker.parent
    return depth


def _route(spec: FigureSpec, relations: dict[str, Relation],
           nodes: dict[str, LayoutNode]) -> list[LayoutEdge]:
    """Connections between siblings, routed orthogonally between box boundaries.

    Containment is not routed: it is already drawn by one box sitting inside another, and a line
    as well would assert a second, separate relationship.
    """
    out: list[LayoutEdge] = []
    for spec_relation in spec.relations:
        representation: VisualRepresentation = spec_relation.visual_representation
        if representation == _CONTAINMENT:
            continue
        relation = relations.get(spec_relation.relation_id)
        if relation is None:
            continue
        source = nodes.get(relation.subject)
        target = nodes.get(relation.object)
        if source is None or target is None:
            continue
        if _encloses(source, target) or _encloses(target, source):
            # A part and the housing it sits in, with a SECOND disclosed relationship between
            # them: an electrical connection to the enclosure, a shaft passing through it. This
            # used to be skipped on the grounds that a line from inside a box to its own wall
            # reads oddly, and the result was worse than odd — the relation was in the
            # specification, absent from the drawing, and the figure blocked on a defect no
            # correction could reach. It is drawn: a short straight run from the inner outline
            # to the enclosing one, which is the convention a draughtsman uses for exactly this.
            inner, outer = ((source, target) if _encloses(target, source)
                            else (target, source))
            start = boundary_point(inner.box, (outer.box.cx, outer.box.cy))
            end = boundary_point(outer.box, (inner.box.cx, inner.box.cy))
            points = [Point(x=start[0], y=start[1]), Point(x=end[0], y=end[1])]
            if inner is target:
                points.reverse()
        else:
            prefer = "horizontal" if abs(source.box.cx - target.box.cx) >= \
                abs(source.box.cy - target.box.cy) else "vertical"
            start = boundary_point(source.box, (target.box.cx, target.box.cy))
            end = boundary_point(target.box, (source.box.cx, source.box.cy))
            points = orthogonal_route(start, end, prefer)
            # Re-anchor on the real elbow so the line leaves and meets each box squarely.
            if len(points) == 3:
                start = boundary_point(source.box, (points[1].x, points[1].y))
                end = boundary_point(target.box, (points[1].x, points[1].y))
                points = [Point(x=start[0], y=start[1]), points[1], Point(x=end[0], y=end[1])]
        directed = relation.direction == "subject_to_object"
        out.append(LayoutEdge(
            relation_id=relation.id, from_entity=relation.subject, to_entity=relation.object,
            edge_type=representation, points=points,
            arrow_at_end=directed and representation in {
                "data_flow", "control_flow", "process_sequence", "movement"},
            arrow_at_start=representation == "bidirectional_association",
            label=""))
        if representation == "bidirectional_association":
            out[-1].arrow_at_end = True
    return out


def _encloses(outer: LayoutNode, inner: LayoutNode) -> bool:
    return (outer.is_container and outer.box.x <= inner.box.x and
            outer.box.y <= inner.box.y and inner.box.right <= outer.box.right and
            inner.box.bottom <= outer.box.bottom)
