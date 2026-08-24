"""Flowchart layout: a vertical main line, branches to the right, loops around the left.

A patent flowchart is read top to bottom and the office expects that, so the layout is not a
general graph problem. The steps in the order the document states them are the spine; a
decision's second outcome leaves to the right and rejoins below; an edge that returns to an
earlier step is drawn round the left margin where it cannot be mistaken for forward flow.

The renderer draws the boxes and the arrows. The language model's contribution ended when it
returned the steps and their order, each with the sentence it came from.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .. import textfit
from ..geometry import boundary_point, union
from ..profiles import DrawingProfile
from ..schemas import (Box, FigureSpec, FlowEdge, FlowStep, LayoutEdge, LayoutNode,
                       LayoutScene, Point)

MAX_STEP_LINES = 4
BRANCH_LABEL_CHARS = 6


def _order_steps(steps: list[FlowStep], edges: list[FlowEdge]) -> list[FlowStep]:
    """Document order, corrected only where an edge proves a step comes later.

    The list already arrives in the order the description states, and that order is evidence.
    A topological pass is applied on top so an explicit edge is never drawn upward when the
    document says it goes down; a cycle leaves the document order alone.
    """
    index = {step.id: position for position, step in enumerate(steps)}
    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.from_step in index and edge.to_step in index:
            if index[edge.to_step] > index[edge.from_step]:      # forward edges only
                outgoing[edge.from_step].add(edge.to_step)
                incoming[edge.to_step].add(edge.from_step)
    ready = [step for step in steps if not incoming[step.id]]
    seen: set[str] = set()
    out: list[FlowStep] = []
    while ready:
        ready.sort(key=lambda s: index[s.id])
        step = ready.pop(0)
        if step.id in seen:
            continue
        seen.add(step.id)
        out.append(step)
        for target in sorted(outgoing[step.id], key=lambda t: index[t]):
            incoming[target].discard(step.id)
            if not incoming[target]:
                ready.append(next(s for s in steps if s.id == target))
    out.extend(step for step in steps if step.id not in seen)
    return out


def _shape(step: FlowStep) -> str:
    return {"decision": "diamond", "terminator": "stadium"}.get(step.kind, "rounded_box")


def layout_flowchart(spec: FigureSpec, profile: DrawingProfile, *, sheet_number: int = 1,
                     sheet_total: int = 1, seed: int = 0) -> LayoutScene:
    area = Box(x=profile.drawing_left, y=profile.drawing_top,
               width=profile.drawing_width, height=profile.drawing_height)
    steps = _order_steps(list(spec.steps), list(spec.step_edges))
    if not steps:
        return LayoutScene(
            figure_id=spec.figure_id, figure_number=spec.figure_number,
            figure_type=spec.figure_type, profile_id=profile.version_tag,
            sheet_width=profile.sheet_width, sheet_height=profile.sheet_height,
            drawing_area=area, caption=spec.title, sheet_number=sheet_number,
            sheet_total=sheet_total)

    height = profile.caption_height
    reserve = profile.reference_height * 4
    # The main column is narrow enough to leave a lane on each side: numerals sit to the right
    # of every box, and loop-backs run down the left.
    column_width = min(area.width * 0.52, profile.min_node_width * 4.5)
    gap = max(profile.rank_gap * 0.8, profile.caption_height * 2.2)

    nodes: list[LayoutNode] = []
    y = area.y + reserve * 0.5
    for step in steps:
        label = step.text
        lines = textfit.wrap(profile, label, column_width - height * 2.4, height,
                             MAX_STEP_LINES)
        body = textfit.block_height(lines, height)
        box_height = max(profile.min_node_height * 1.1, body + height * 1.4)
        if step.kind == "decision":
            box_height = max(box_height, profile.min_node_height * 1.6)
        nodes.append(LayoutNode(
            entity_id=step.id, reference_numeral=step.reference_numeral,
            caption=" ".join(lines), shape=_shape(step),  # type: ignore[arg-type]
            box=Box(x=area.x + reserve + (area.width - 2 * reserve - column_width) / 2,
                    y=y, width=column_width, height=box_height)))
        y += box_height + gap

    total_height = (y - gap) - (area.y + reserve * 0.5)
    available = area.height - reserve - profile.caption_height * 3
    if total_height > available:
        factor = available / total_height
        top = area.y + reserve * 0.5
        for node in nodes:
            node.box = Box(x=node.box.x, y=top + (node.box.y - top) * factor,
                           width=node.box.width,
                           height=max(profile.min_node_height * 0.7,
                                      node.box.height * max(factor, 0.55)))

    by_id = {node.entity_id: node for node in nodes}
    order = {step.id: position for position, step in enumerate(steps)}
    edges = _route_edges(spec, by_id, order, profile, area)

    return LayoutScene(
        figure_id=spec.figure_id, figure_number=spec.figure_number,
        figure_type=spec.figure_type, profile_id=profile.version_tag,
        sheet_width=profile.sheet_width, sheet_height=profile.sheet_height,
        drawing_area=area, nodes=nodes, edges=edges, labels=[], caption=spec.title,
        sheet_number=sheet_number, sheet_total=sheet_total)


def _route_edges(spec: FigureSpec, by_id: dict[str, LayoutNode], order: dict[str, int],
                 profile: DrawingProfile, area: Box) -> list[LayoutEdge]:
    out: list[LayoutEdge] = []
    branch_taken: set[str] = set()
    for edge in spec.step_edges:
        source = by_id.get(edge.from_step)
        target = by_id.get(edge.to_step)
        if source is None or target is None:
            continue
        forward = order.get(edge.to_step, 0) > order.get(edge.from_step, 0)
        adjacent = abs(order.get(edge.to_step, 0) - order.get(edge.from_step, 0)) == 1
        label = edge.label[:BRANCH_LABEL_CHARS]
        if forward and adjacent and edge.from_step not in branch_taken:
            branch_taken.add(edge.from_step)
            points = [Point(x=source.box.cx, y=source.box.bottom),
                      Point(x=target.box.cx, y=target.box.y)]
        elif forward:
            # A skip-forward edge, which is normally a decision's second outcome: out to the
            # right, down past the steps it skips, and back in from the right.
            lane = min(area.right - profile.reference_height,
                       max(source.box.right, target.box.right) + profile.rank_gap * 0.7)
            points = [Point(x=source.box.right, y=source.box.cy),
                      Point(x=lane, y=source.box.cy),
                      Point(x=lane, y=target.box.cy),
                      Point(x=target.box.right, y=target.box.cy)]
        else:
            # A loop back to an earlier step, down the left lane.
            lane = max(area.x + profile.reference_height,
                       min(source.box.x, target.box.x) - profile.rank_gap * 0.7)
            points = [Point(x=source.box.x, y=source.box.cy),
                      Point(x=lane, y=source.box.cy),
                      Point(x=lane, y=target.box.cy),
                      Point(x=target.box.x, y=target.box.cy)]
        out.append(LayoutEdge(
            relation_id=f"flow_{edge.from_step}_{edge.to_step}",
            from_entity=edge.from_step, to_entity=edge.to_step,
            edge_type="process_sequence", points=points, arrow_at_end=True, label=label))
    return out
