"""Block diagrams, flow charts and sequence diagrams.

The model gives a graph; Graphviz gives that graph coordinates; this module turns the coordinates
into the same primitives everything else in the pipeline speaks. Nothing about the picture is
decided by a model: box sizes come from the text that has to fit in them, positions come from a
layout engine that is deterministic for a given input, and the style is fixed here.

Graphviz is asked for JSON rather than an image on purpose. An image would have to be read back
to be checked; a coordinate list can be checked as it stands, and it is what lets the placement
solver know exactly where every edge runs before a single numeral is placed.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import textwrap
from collections import defaultdict
from typing import Optional

from .. import geom
from ..drawing import (Anchor, Figure, LEGEND_SIZE, Prim, W_OUTLINE, W_THIN, polygon, polyline,
                       text as text_prim)
from ..geom import Point
from ..schemas import FigurePlan, GraphScene, SeqScene

PT_TO_MM = 25.4 / 72.0
MM_TO_PT = 72.0 / 25.4
MM_TO_INCH = 1.0 / 25.4

PAD_X = 3.0
PAD_Y = 2.6
LINE_HEIGHT = LEGEND_SIZE * 1.25
MIN_NODE_W = 24.0
MIN_NODE_H = 12.0
MAX_LABEL_CHARS = 22
DOT_TIMEOUT = float(os.environ.get("FM_DOT_TIMEOUT", "40"))


class LayoutUnavailable(RuntimeError):
    """Graphviz is missing or failed. Said out loud rather than degraded into a bad picture."""


# ------------------------------------------------------------------------------------- helpers


def wrap_label(label: str, width: int = MAX_LABEL_CHARS) -> list[str]:
    body = " ".join((label or "").split())
    if not body:
        return []
    return textwrap.wrap(body, width=width, break_long_words=True) or [body]


def label_box(lines: list[str]) -> tuple[float, float]:
    if not lines:
        return (MIN_NODE_W, MIN_NODE_H)
    widest = max(geom.text_extent(line, LEGEND_SIZE)[0] for line in lines)
    return (max(MIN_NODE_W, widest + 2 * PAD_X),
            max(MIN_NODE_H, len(lines) * LINE_HEIGHT + 2 * PAD_Y))


def _dot_binary() -> str:
    found = shutil.which("dot")
    if not found:
        raise LayoutUnavailable(
            "graphviz is not installed on this host, so block diagrams and flow charts cannot be "
            "laid out. Install it with: sudo apt-get install -y graphviz")
    return found


def run_dot(source: str, engine: str = "dot") -> dict:
    binary = _dot_binary()
    if engine != "dot":
        binary = shutil.which(engine) or binary
    try:
        result = subprocess.run([binary, "-Tjson"], input=source.encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=DOT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise LayoutUnavailable(f"graphviz did not finish within {DOT_TIMEOUT:.0f}s") from exc
    if result.returncode != 0:
        raise LayoutUnavailable(
            "graphviz rejected the graph: "
            + (result.stderr.decode("utf-8", "replace").strip()[:300] or "no message"))
    try:
        return json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise LayoutUnavailable(f"graphviz returned output that is not JSON: {exc}") from exc


def _pt(pair: str) -> Point:
    x, y = pair.split(",")[:2]
    return (float(x) * PT_TO_MM, float(y) * PT_TO_MM)


def _bb(value: str) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(v) for v in value.split(",")[:4])
    return (x0 * PT_TO_MM, y0 * PT_TO_MM, x1 * PT_TO_MM, y1 * PT_TO_MM)


def _flip(point: Point, height: float) -> Point:
    """Graphviz measures y upwards from the bottom; a drawing measures it downwards from the top."""
    return (point[0], height - point[1])


# ---------------------------------------------------------------------------------- node shapes


def node_outline(shape: str, cx: float, cy: float, w: float, h: float) -> list[Point]:
    x0, y0 = cx - w / 2.0, cy - h / 2.0
    if shape == "rounded":
        return geom.rounded_rect_poly(x0, y0, w, h, min(h / 2.0, 4.0))
    if shape == "stadium":
        return geom.rounded_rect_poly(x0, y0, w, h, h / 2.0)
    if shape == "diamond":
        return [(cx, y0), (x0 + w, cy), (cx, y0 + h), (x0, cy)]
    if shape == "ellipse":
        return geom.ellipse_poly(cx, cy, w / 2.0, h / 2.0)
    if shape == "parallelogram":
        slant = min(w * 0.18, h * 0.9)
        return [(x0 + slant, y0), (x0 + w, y0), (x0 + w - slant, y0 + h), (x0, y0 + h)]
    if shape == "hexagon":
        slant = min(w * 0.16, h * 0.5)
        return [(x0 + slant, y0), (x0 + w - slant, y0), (x0 + w, cy),
                (x0 + w - slant, y0 + h), (x0 + slant, y0 + h), (x0, cy)]
    return geom.rect_poly(x0, y0, w, h)


def _shape_extra(shape: str, cx: float, cy: float, w: float, h: float,
                 owner: str = "") -> list[Prim]:
    """The parts of a shape that are not its outline, such as a store's top ellipse."""
    if shape != "cylinder":
        return []
    ry = min(h * 0.16, 3.2)
    return [Prim(kind="ellipse", pts=[(cx, cy - h / 2.0 + ry)], rx=w / 2.0, ry=ry,
                 role="outline", owner=owner, width=W_OUTLINE)]


def _size_for(shape: str, lines: list[str]) -> tuple[float, float]:
    w, h = label_box(lines)
    if shape == "diamond":
        return (w * 1.5, h * 1.7)
    if shape in ("ellipse", "stadium"):
        return (w * 1.2, h * 1.25)
    if shape in ("parallelogram", "hexagon"):
        return (w * 1.3, h)
    if shape == "cylinder":
        return (w, h * 1.35)
    return (w, h)


# ------------------------------------------------------------------------------- edge geometry


def _spline_points(pos: str) -> tuple[list[Point], Optional[Point]]:
    """Graphviz spline syntax to a polyline, plus the arrow endpoint if it gave one."""
    end: Optional[Point] = None
    start: Optional[Point] = None
    tokens = pos.replace("\\\n", "").split()
    control: list[Point] = []
    for token in tokens:
        if token.startswith("e,"):
            end = _pt(token[2:])
        elif token.startswith("s,"):
            start = _pt(token[2:])
        else:
            control.append(_pt(token))
    if not control:
        return ([], end)
    points: list[Point] = [control[0]]
    i = 1
    while i + 2 < len(control):
        p0 = points[-1]
        p1, p2, p3 = control[i], control[i + 1], control[i + 2]
        for step in range(1, 13):
            points.append(_bezier(p0, p1, p2, p3, step / 12.0))
        i += 3
    if start:
        points.insert(0, start)
    if end:
        points.append(end)
    return (_thin(points), end)


def _bezier(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    return (u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1])


def _thin(points: list[Point], tolerance: float = 0.25) -> list[Point]:
    out = [points[0]]
    for point in points[1:]:
        if math.dist(point, out[-1]) >= tolerance:
            out.append(point)
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


# ------------------------------------------------------------------------------ block and flow


def render_graph(plan: FigurePlan, scene: GraphScene) -> Figure:
    nodes = [n for n in scene.nodes if n.numeral]
    if not nodes:
        raise LayoutUnavailable(f"{plan.label}: the scene has no nodes to lay out")
    known = {n.numeral for n in nodes}
    parents = {n.numeral: (n.parent if n.parent in known and n.parent != n.numeral else "")
               for n in nodes}
    # A cycle in the containment would make Graphviz recurse for ever.
    for numeral in list(parents):
        seen = set()
        cursor = numeral
        while parents.get(cursor):
            if cursor in seen:
                parents[numeral] = ""
                break
            seen.add(cursor)
            cursor = parents[cursor]

    # A block that contains other blocks is drawn as the cluster that holds them, not as a box of
    # its own. Drawing both would put the same numeral on two rectangles in different places.
    containers = {p for p in parents.values() if p}
    layout, lines, sizes = _best_layout(scene, nodes, parents, containers)
    height = _bb(layout.get("bb", "0,0,100,100"))[3]

    figure = Figure(label=plan.label, kind=plan.kind, title=plan.title,
                    scene=scene.model_dump())
    by_gvid: dict[int, dict] = {}
    shapes = {n.numeral: n.shape for n in nodes}

    for obj in layout.get("objects", []):
        by_gvid[int(obj.get("_gvid", -1))] = obj
        name = obj.get("name", "")
        if name.startswith("cluster_"):
            numeral = name[len("cluster_"):]
            if "bb" not in obj:
                continue
            x0, y0, x1, y1 = _bb(obj["bb"])
            top_left = _flip((x0, y1), height)
            box = geom.rect_poly(top_left[0], top_left[1], x1 - x0, y1 - y0)
            figure.prims.append(polygon(box, role="outline", owner=numeral, width=W_OUTLINE))
            if obj.get("lp"):
                at = _flip(_pt(obj["lp"]), height)
                body = lines.get(numeral) or []
                top = at[1] - (len(body) - 1) * LINE_HEIGHT / 2.0
                for i, line in enumerate(body):
                    figure.prims.append(text_prim((at[0], top + i * LINE_HEIGHT), line,
                                                  size=LEGEND_SIZE, role="legend", owner=numeral))
            figure.anchors.setdefault(numeral, []).extend(_box_anchors(numeral, box))
            continue
        numeral = name
        if numeral not in known or numeral in containers or "pos" not in obj:
            continue
        centre = _flip(_pt(obj["pos"]), height)
        w, h = sizes[numeral]
        shape = shapes[numeral]
        outline = node_outline(shape, centre[0], centre[1], w, h)
        figure.prims.append(polygon(outline, role="outline", owner=numeral, width=W_OUTLINE))
        figure.prims.extend(_shape_extra(shape, centre[0], centre[1], w, h, numeral))
        body = lines[numeral]
        top = centre[1] - (len(body) - 1) * LINE_HEIGHT / 2.0
        for i, line in enumerate(body):
            figure.prims.append(text_prim((centre[0], top + i * LINE_HEIGHT), line,
                                          size=LEGEND_SIZE, role="legend", owner=numeral))
        figure.anchors.setdefault(numeral, []).extend(_box_anchors(numeral, outline))

    for edge in layout.get("edges", []):
        pos = edge.get("pos")
        if not pos:
            continue
        points, tip = _spline_points(pos)
        if len(points) < 2:
            continue
        points = [_flip(p, height) for p in points]
        tail = by_gvid.get(int(edge.get("tail", -1)), {}).get("name", "")
        head = by_gvid.get(int(edge.get("head", -1)), {}).get("name", "")
        spec = _edge_spec(scene, tail, head)
        dash = (1.8, 1.2) if (spec and spec.dashed) else None
        figure.prims.append(polyline(points, role="outline", width=W_THIN, dash=dash))
        if spec is None or spec.arrow:
            figure.prims.append(polygon(geom.arrow_head(points[-1], points[-2]),
                                        role="arrow", width=W_THIN, fill=True))
        label = (edge.get("label") or "").strip()
        if label and edge.get("lp"):
            at = _flip(_pt(edge["lp"]), height)
            figure.prims.append(text_prim(at, label, size=LEGEND_SIZE, role="legend"))

    if not figure.prims:
        raise LayoutUnavailable(f"{plan.label}: graphviz produced no geometry")
    return figure


# The usable area of a sheet, from 37 CFR 1.84(g), less room for the numerals that will be hung
# around the outside of the graph.
SIGHT_W = 170.0 - 24.0
SIGHT_H = 262.0 - 30.0


def _best_layout(scene: GraphScene, nodes, parents, containers):
    """Lay the graph out several ways and keep whichever fits the sheet best.

    A block diagram is sized by the words in its boxes, and those words may not be lettered below
    0.32 cm, so a graph that comes out too big cannot simply be reduced: shrinking it leaves the
    text sticking out of the boxes. What is free is which way the ranks run and how narrowly the
    labels wrap, and between them they are often the whole difference between a view that fits a
    sheet and one the author has to split.

    Returns the layout together with the wrapping it was produced from, because the caller draws
    the boxes at sizes that have to match what Graphviz was told.
    """
    tried: list[tuple[float, dict, dict, dict]] = []
    directions = [scene.direction] + [d for d in ("TB", "LR") if d != scene.direction]
    failure: Optional[Exception] = None
    for width in (MAX_LABEL_CHARS, 14, 10):
        lines = {n.numeral: wrap_label(n.label or n.numeral, width) for n in nodes}
        sizes = {n.numeral: _size_for(n.shape, lines[n.numeral]) for n in nodes}
        for direction in directions:
            candidate = GraphScene(direction=direction, nodes=scene.nodes, edges=scene.edges)
            try:
                layout = run_dot(_dot_source(candidate, nodes, sizes, parents, lines, containers))
            except LayoutUnavailable as exc:
                failure = exc
                continue
            box = _bb(layout.get("bb", "0,0,100,100"))
            overflow = (max(0.0, (box[2] - box[0]) - SIGHT_W)
                        + max(0.0, (box[3] - box[1]) - SIGHT_H))
            if overflow <= 0.0:
                return layout, lines, sizes
            tried.append((overflow, layout, lines, sizes))
    if not tried:
        raise failure or LayoutUnavailable("graphviz produced no usable layout")
    best = min(tried, key=lambda item: item[0])
    return best[1], best[2], best[3]


def _edge_spec(scene: GraphScene, tail: str, head: str):
    for edge in scene.edges:
        if edge.source == tail and edge.target == head:
            return edge
    return None


def _dot_source(scene: GraphScene, nodes, sizes, parents, lines, containers) -> str:
    out = ["digraph G {",
           "  compound=true;",
           f'  rankdir={scene.direction};',
           "  splines=ortho;" if scene.direction == "TB" else "  splines=polyline;",
           "  nodesep=0.55; ranksep=0.75; margin=0;",
           '  node [shape=box, fixedsize=true, label=""];',
           '  edge [arrowhead=none, arrowtail=none];']

    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        children[parents.get(node.numeral) or ""].append(node.numeral)

    def emit(numeral: str, indent: str) -> str:
        # A block that holds other blocks becomes a cluster, and its own children are emitted
        # inside it. Flattening this is what made a subsystem inside a system come out as the
        # box beside it rather than the box around it.
        if numeral in containers:
            caption = _dot_escape("\\n".join(lines.get(numeral) or []))
            body = (f'{indent}subgraph "cluster_{numeral}" {{\n'
                    f'{indent}  label="{caption}"; labelloc=t; labeljust=c; margin=12;\n'
                    f'{indent}  fontsize={LEGEND_SIZE * MM_TO_PT:.1f};\n')
            for kid in children[numeral]:
                body += emit(kid, indent + "  ")
            return body + f"{indent}}}\n"
        w, h = sizes[numeral]
        return (f'{indent}"{numeral}" [width={w * MM_TO_INCH:.4f}, '
                f'height={h * MM_TO_INCH:.4f}];\n')

    out.append("".join(emit(n, "  ") for n in children[""]).rstrip("\n"))

    known = {n.numeral for n in nodes}
    for edge in scene.edges:
        if edge.source not in known or edge.target not in known:
            continue
        # An edge into or out of a container has to run between real nodes; Graphviz clips it to
        # the cluster boundary when told which cluster each end belongs to.
        attrs = []
        if edge.source in containers:
            attrs.append(f'ltail="cluster_{edge.source}"')
        if edge.target in containers:
            attrs.append(f'lhead="cluster_{edge.target}"')
        if edge.label:
            attrs.append(f'label="{_dot_escape(edge.label)}"')
            attrs.append(f"fontsize={LEGEND_SIZE * MM_TO_PT:.1f}")
        source = _representative(edge.source, children, containers)
        target = _representative(edge.target, children, containers)
        if not source or not target or source == target:
            continue
        joined = (" [" + ", ".join(attrs) + "]") if attrs else ""
        out.append(f'  "{source}" -> "{target}"{joined};')
    out.append("}")
    return "\n".join(out)


def _representative(numeral: str, children: dict[str, list[str]], containers) -> str:
    """A cluster cannot be an edge endpoint. Its first descendant that is a real node stands in."""
    seen: set[str] = set()
    cursor = numeral
    while cursor in containers:
        if cursor in seen or not children.get(cursor):
            return ""
        seen.add(cursor)
        cursor = children[cursor][0]
    return cursor


def _dot_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


MIN_ANCHOR_SPACING = 4.0


def _box_anchors(numeral: str, outline: list[Point], want: int = 16) -> list[Anchor]:
    """Where a lead line may land: points on the outline itself, never on its bounding box.

    37 CFR 1.84(q) requires a lead line to extend to the feature it indicates. Taking anchors
    from the bounding box satisfies that for a rectangle and quietly breaks it for everything
    else: the corner of a diamond's bounding box is several millimetres clear of the diamond, and
    the lead line stops in mid-air. So the anchors are sampled along the real edges, with the
    outward direction taken from each edge rather than assumed.
    """
    if len(outline) < 3:
        return []
    n = len(outline)
    cx = sum(p[0] for p in outline) / n
    cy = sum(p[1] for p in outline) / n

    def outward(point: Point, along: Point) -> Point:
        normal = geom.unit(along[1], -along[0])
        if (point[0] - cx) * normal[0] + (point[1] - cy) * normal[1] < 0:
            normal = (-normal[0], -normal[1])
        return normal

    candidates: list[tuple[float, Anchor]] = []
    for i in range(n):
        a, b = outline[i], outline[(i + 1) % n]
        edge = (b[0] - a[0], b[1] - a[1])
        length = math.hypot(*edge)
        if length < 1e-9:
            continue
        steps = max(1, min(5, int(length // 9) + 1))
        for k in range(steps):
            t = (k + 0.5) / steps
            point = (a[0] + edge[0] * t, a[1] + edge[1] * t)
            # The middle of a side is the best place to land; the ends of it are next best.
            weight = 1.0 - abs(t - 0.5) * 0.3
            candidates.append((weight, Anchor(numeral, point, outward(point, edge), weight)))
        candidates.append((0.85, Anchor(numeral, a, geom.unit(a[0] - cx, a[1] - cy), 0.85)))

    candidates.sort(key=lambda item: -item[0])
    chosen: list[Anchor] = []
    for _weight, anchor in candidates:
        if all(math.dist(anchor.point, other.point) > MIN_ANCHOR_SPACING for other in chosen):
            chosen.append(anchor)
        if len(chosen) >= want:
            break
    return chosen or [candidates[0][1]] if candidates else []


# ---------------------------------------------------------------------------------- sequence

ACTOR_GAP = 46.0
MESSAGE_GAP = 13.0
LIFELINE_TOP = 4.0


def render_sequence(plan: FigurePlan, scene: SeqScene) -> Figure:
    actors = [a for a in scene.actors if a.numeral]
    if not actors:
        raise LayoutUnavailable(f"{plan.label}: the sequence has no actors")
    figure = Figure(label=plan.label, kind=plan.kind, title=plan.title, scene=scene.model_dump())

    columns: dict[str, float] = {}
    head_bottom = 0.0
    for i, actor in enumerate(actors):
        lines = wrap_label(actor.label or actor.numeral, 16)
        w, h = label_box(lines)
        w = max(w, 26.0)
        cx = i * max(ACTOR_GAP, w + 10.0) + w / 2.0
        columns[actor.numeral] = cx
        box = geom.rect_poly(cx - w / 2.0, LIFELINE_TOP, w, h)
        figure.prims.append(polygon(box, role="outline", owner=actor.numeral, width=W_OUTLINE))
        top = LIFELINE_TOP + h / 2.0 - (len(lines) - 1) * LINE_HEIGHT / 2.0
        for j, line in enumerate(lines):
            figure.prims.append(text_prim((cx, top + j * LINE_HEIGHT), line, size=LEGEND_SIZE,
                                          role="legend", owner=actor.numeral))
        figure.anchors.setdefault(actor.numeral, []).extend(_box_anchors(actor.numeral, box))
        head_bottom = max(head_bottom, LIFELINE_TOP + h)

    messages = [m for m in scene.messages
                if m.source in columns and m.target in columns][:40]
    y = head_bottom + MESSAGE_GAP
    for index, message in enumerate(messages, start=1):
        x0, x1 = columns[message.source], columns[message.target]
        dash = (1.8, 1.2) if message.dashed else None
        self_call = abs(x1 - x0) < 0.5
        row_y = y
        if self_call:
            loop = [(x0, y), (x0 + 14.0, y), (x0 + 14.0, y + MESSAGE_GAP * 0.6),
                    (x0 + 1.2, y + MESSAGE_GAP * 0.6)]
            figure.prims.append(polyline(loop, role="outline", width=W_THIN, dash=dash))
            figure.prims.append(polygon(geom.arrow_head(loop[-1], loop[-2]), role="arrow",
                                        width=W_THIN, fill=True))
            tip = loop[-1]
            y += MESSAGE_GAP * 0.6
        else:
            direction = 1.0 if x1 > x0 else -1.0
            start = (x0 + direction * 1.0, y)
            tip = (x1 - direction * 1.0, y)
            figure.prims.append(polyline([start, tip], role="outline", width=W_THIN, dash=dash))
            figure.prims.append(polygon(geom.arrow_head(tip, start), role="arrow", width=W_THIN,
                                        fill=True))
        label = " ".join((message.label or "").split())
        if label:
            body = wrap_label(label, 26)
            # The caption sits above its arrow, the last line just clear of it, so a two-line
            # message grows upwards into the gap rather than down across the next lifeline.
            # A self-call has no span to sit over, so its caption goes beside the loop instead
            # of on top of it.
            if self_call:
                anchor = "start"
                cx = x0 + 16.5
                bottom = row_y + MESSAGE_GAP * 0.3 + (len(body) - 1) * LINE_HEIGHT / 2.0
            else:
                anchor = "middle"
                cx = (x0 + x1) / 2.0
                bottom = row_y - LEGEND_SIZE * 0.9
            for j, line in enumerate(body):
                figure.prims.append(text_prim(
                    (cx, bottom - (len(body) - 1 - j) * LINE_HEIGHT), line,
                    size=LEGEND_SIZE, role="legend", anchor=anchor))
            if len(body) > 1 and not self_call:
                y += (len(body) - 1) * LINE_HEIGHT
        y += MESSAGE_GAP

    # The lifelines are drawn last because only now is it known how far down the exchange ran:
    # a two-line caption pushes every later message down, and a lifeline that stops short of the
    # bottom arrow is the one thing a reader notices immediately.
    depth = y
    for numeral, cx in columns.items():
        figure.prims.insert(0, polyline([(cx, head_bottom), (cx, depth)], role="centre",
                                        owner=numeral, width=W_THIN, dash=(2.4, 1.6)))
    return figure
