"""Display-screen figures, from a wireframe tree.

37 CFR 1.84 applies to a screen the same way it applies to a gearbox: black lines, no shading, no
colour, reference characters outside the geometry. What changes is that a screen has no hidden
lines and no perspective, so the whole figure is a nested box layout, and the layout is computed
here rather than asked for. The model says what the screen contains and how much room each part
deserves; the arithmetic is this module's.
"""
from __future__ import annotations

import math
from typing import Optional

from .. import geom
from ..drawing import (Anchor, Figure, LEGEND_SIZE, W_OUTLINE, W_THIN, circle, ellipse, line,
                       polygon, polyline, text as text_prim)
from ..geom import BBox, Point
from ..schemas import FigurePlan, UINode, UIScene
from .graphfig import LINE_HEIGHT, wrap_label

DEVICE_SIZE = {
    "screen": (170.0, 106.0),
    "window": (150.0, 100.0),
    "phone": (62.0, 124.0),
    "tablet": (112.0, 150.0),
}

PAD = 2.6
GAP = 2.2
MIN_CELL = 5.0
CONTROL_H = 8.0
MAX_DEPTH = 7
MAX_NODES = 90

_CONTAINERS = {"screen", "window", "panel", "row", "column", "card", "list", "table",
               "tab_bar", "nav_bar"}


class UIError(RuntimeError):
    """The wireframe could not be laid out."""


def render_ui(plan: FigurePlan, scene: UIScene) -> Figure:
    root = scene.root
    if root is None or (not root.children and not root.type):
        raise UIError(f"{plan.label}: the wireframe is empty")
    width, height = DEVICE_SIZE.get(scene.device, DEVICE_SIZE["window"])
    figure = Figure(label=plan.label, kind=plan.kind, title=plan.title, scene=scene.model_dump())

    budget = {"left": MAX_NODES}
    _draw(figure, root, (0.0, 0.0, width, height), 0, budget, device=scene.device)
    if not figure.prims:
        raise UIError(f"{plan.label}: nothing was drawn")
    return figure


def _children_of(node: UINode, budget: dict) -> list[UINode]:
    kids = [k for k in (node.children or []) if k is not None]
    if budget["left"] <= 0:
        return []
    kids = kids[: max(0, budget["left"])]
    budget["left"] -= len(kids)
    return kids


def _layout(box: BBox, kids: list[UINode], direction: str, pad: float) -> list[BBox]:
    x0, y0, x1, y1 = box[0] + pad, box[1] + pad, box[2] - pad, box[3] - pad
    if x1 <= x0 or y1 <= y0 or not kids:
        return []
    weights = [max(0.05, float(k.weight or 1.0)) for k in kids]
    total = sum(weights)
    gaps = GAP * (len(kids) - 1)
    out: list[BBox] = []
    if direction == "row":
        span = max(MIN_CELL * len(kids), (x1 - x0) - gaps)
        cursor = x0
        for weight in weights:
            width = span * weight / total
            out.append((cursor, y0, cursor + width, y1))
            cursor += width + GAP
    else:
        span = max(MIN_CELL * len(kids), (y1 - y0) - gaps)
        cursor = y0
        for weight in weights:
            height = span * weight / total
            out.append((x0, cursor, x1, cursor + height))
            cursor += height + GAP
    return out


def _draw(figure: Figure, node: UINode, box: BBox, depth: int, budget: dict,
          device: str = "window") -> None:
    kind = (node.type or "panel").lower()
    numeral = (node.numeral or "").strip()
    drew_outline = _draw_self(figure, node, kind, box, numeral, device)
    if numeral:
        outline = geom.bbox_poly(box)
        figure.anchors.setdefault(numeral, []).extend(_anchors(numeral, outline))

    if depth >= MAX_DEPTH:
        return
    kids = _children_of(node, budget)
    if not kids:
        return
    inner = box
    if kind in ("screen", "window") and any(k.type == "titlebar" for k in kids):
        pass
    pad = PAD if drew_outline else GAP / 2.0
    for kid, kid_box in zip(kids, _layout(inner, kids, node.direction or "column", pad)):
        if kid_box[2] - kid_box[0] < 1.5 or kid_box[3] - kid_box[1] < 1.5:
            continue
        _draw(figure, kid, kid_box, depth + 1, budget, device)


# --------------------------------------------------------------------------------- primitives


def _rect(figure: Figure, box: BBox, numeral: str, width: float = W_OUTLINE,
          radius: float = 0.0) -> None:
    x0, y0, x1, y1 = box
    if radius > 0:
        figure.prims.append(polygon(geom.rounded_rect_poly(x0, y0, x1 - x0, y1 - y0, radius),
                                    role="outline", owner=numeral, width=width))
    else:
        figure.prims.append(polygon(geom.rect_poly(x0, y0, x1 - x0, y1 - y0), role="outline",
                                    owner=numeral, width=width))


def _label(figure: Figure, box: BBox, body: str, numeral: str, align: str = "middle",
           inset: float = 1.8) -> None:
    body = " ".join((body or "").split())
    if not body:
        return
    width = max(4.0, box[2] - box[0] - 2 * inset)
    chars = max(3, int(width / (LEGEND_SIZE * geom.TEXT_ASPECT)))
    lines = wrap_label(body, chars)[:3]
    cy = (box[1] + box[3]) / 2.0 - (len(lines) - 1) * LINE_HEIGHT / 2.0
    x = {"middle": (box[0] + box[2]) / 2.0, "start": box[0] + inset,
         "end": box[2] - inset}[align]
    for i, text in enumerate(lines):
        figure.prims.append(text_prim((x, cy + i * LINE_HEIGHT), text, size=LEGEND_SIZE,
                                      role="legend", owner=numeral, anchor=align))


def _draw_self(figure: Figure, node: UINode, kind: str, box: BBox, numeral: str,
               device: str) -> bool:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    mid_y = (y0 + y1) / 2.0
    label = node.label or ""

    if kind in ("screen", "window"):
        radius = 3.0 if device in ("phone", "tablet") else 0.0
        _rect(figure, box, numeral, radius=radius)
        if device in ("phone", "tablet"):
            figure.prims.append(polyline([(x0 + w * 0.38, y0 + 2.4), (x0 + w * 0.62, y0 + 2.4)],
                                         role="outline", owner=numeral, width=W_THIN))
        return True
    if kind == "titlebar":
        _rect(figure, box, numeral)
        for i in range(3):
            figure.prims.append(circle((x1 - 3.0 - i * 3.2, mid_y), 1.0, role="outline",
                                       owner=numeral, width=W_THIN))
        _label(figure, (x0, y0, x1 - 12.0, y1), label, numeral, align="start")
        return True
    if kind in ("panel", "card", "row", "column"):
        if kind in ("panel", "card") or label:
            _rect(figure, box, numeral, width=W_THIN, radius=1.2 if kind == "card" else 0.0)
            if label:
                _label(figure, (x0, y0, x1, y0 + CONTROL_H), label, numeral, align="start")
            return True
        return False
    if kind in ("label", "heading"):
        _label(figure, box, label or "Text", numeral, align="start")
        return False
    if kind == "button":
        _rect(figure, box, numeral, radius=min(1.8, h / 3.0))
        _label(figure, box, label or "Button", numeral)
        return True
    if kind in ("textfield", "search"):
        _rect(figure, box, numeral, width=W_THIN)
        if kind == "search":
            figure.prims.append(circle((x0 + 3.2, mid_y), 1.5, role="outline", owner=numeral,
                                       width=W_THIN))
            figure.prims.append(line((x0 + 4.3, mid_y + 1.1), (x0 + 5.4, mid_y + 2.2),
                                     role="outline", owner=numeral, width=W_THIN))
            _label(figure, (x0 + 5.0, y0, x1, y1), label, numeral, align="start")
        else:
            _label(figure, box, label, numeral, align="start")
        return True
    if kind == "textarea":
        _rect(figure, box, numeral, width=W_THIN)
        rows = max(1, int((h - 4.0) / 3.4))
        for i in range(min(rows, 6)):
            y = y0 + 3.0 + i * 3.4
            if y > y1 - 1.5:
                break
            figure.prims.append(line((x0 + 1.8, y), (x1 - (1.8 if i % 3 else 8.0), y),
                                     role="outline", owner=numeral, width=W_THIN))
        return True
    if kind in ("list", "table"):
        _rect(figure, box, numeral, width=W_THIN)
        rows = max(2, min(8, int(h / 6.5)))
        for i in range(1, rows):
            y = y0 + h * i / rows
            figure.prims.append(line((x0, y), (x1, y), role="outline", owner=numeral,
                                     width=W_THIN))
        if kind == "table":
            for i in (1, 2):
                x = x0 + w * i / 3.0
                figure.prims.append(line((x, y0), (x, y1), role="outline", owner=numeral,
                                         width=W_THIN))
        if label:
            _label(figure, (x0, y0, x1, y0 + h / rows), label, numeral, align="start")
        return True
    if kind == "listitem":
        figure.prims.append(line((x0, y1), (x1, y1), role="outline", owner=numeral, width=W_THIN))
        _label(figure, box, label, numeral, align="start")
        return False
    if kind in ("checkbox", "radio"):
        side = min(3.6, h * 0.7)
        if kind == "checkbox":
            figure.prims.append(polygon(geom.rect_poly(x0 + 1.0, mid_y - side / 2, side, side),
                                        role="outline", owner=numeral, width=W_THIN))
        else:
            figure.prims.append(circle((x0 + 1.0 + side / 2, mid_y), side / 2, role="outline",
                                       owner=numeral, width=W_THIN))
        _label(figure, (x0 + side + 2.4, y0, x1, y1), label, numeral, align="start")
        return True
    if kind == "dropdown":
        _rect(figure, box, numeral, width=W_THIN)
        tip = (x1 - 3.0, mid_y + 1.2)
        figure.prims.append(polygon([(x1 - 5.2, mid_y - 0.9), (x1 - 0.8, mid_y - 0.9), tip],
                                    role="outline", owner=numeral, width=W_THIN, fill=True))
        _label(figure, (x0, y0, x1 - 6.0, y1), label, numeral, align="start")
        return True
    if kind == "toggle":
        span = min(w * 0.5, 10.0)
        height = min(h * 0.8, 5.0)
        figure.prims.append(polygon(
            geom.rounded_rect_poly(x1 - span, mid_y - height / 2, span, height, height / 2),
            role="outline", owner=numeral, width=W_THIN))
        figure.prims.append(circle((x1 - height / 2 - 0.4, mid_y), height / 2 - 0.7,
                                   role="outline", owner=numeral, width=W_THIN))
        _label(figure, (x0, y0, x1 - span - 2.0, y1), label, numeral, align="start")
        return True
    if kind == "slider":
        figure.prims.append(line((x0 + 2.0, mid_y), (x1 - 2.0, mid_y), role="outline",
                                 owner=numeral, width=W_THIN))
        figure.prims.append(circle((x0 + 2.0 + (w - 4.0) * 0.62, mid_y), 1.8, role="outline",
                                   owner=numeral, width=W_THIN))
        return True
    if kind == "progress":
        _rect(figure, box, numeral, width=W_THIN, radius=min(1.4, h / 2.0))
        figure.prims.append(line((x0, y1 - 0.01), (x0 + w * 0.55, y1 - 0.01), role="outline",
                                 owner=numeral, width=W_THIN))
        figure.prims.append(line((x0 + w * 0.55, y0), (x0 + w * 0.55, y1), role="outline",
                                 owner=numeral, width=W_THIN))
        return True
    if kind in ("tab_bar", "nav_bar"):
        _rect(figure, box, numeral, width=W_THIN)
        slots = max(2, min(5, len(node.children or []) or 3))
        for i in range(1, slots):
            x = x0 + w * i / slots
            figure.prims.append(line((x, y0), (x, y1), role="outline", owner=numeral,
                                     width=W_THIN))
        if label:
            _label(figure, (x0, y0, x0 + w / slots, y1), label, numeral)
        return True
    if kind == "tab":
        _rect(figure, box, numeral, width=W_THIN)
        _label(figure, box, label, numeral)
        return True
    if kind == "icon":
        side = min(w, h) * 0.8
        cx, cy = (x0 + x1) / 2.0, mid_y
        figure.prims.append(polygon(geom.rect_poly(cx - side / 2, cy - side / 2, side, side),
                                    role="outline", owner=numeral, width=W_THIN))
        figure.prims.append(line((cx - side / 2, cy - side / 2), (cx + side / 2, cy + side / 2),
                                 role="outline", owner=numeral, width=W_THIN))
        return True
    if kind == "image":
        _rect(figure, box, numeral, width=W_THIN)
        figure.prims.append(line((x0, y0), (x1, y1), role="outline", owner=numeral, width=W_THIN))
        figure.prims.append(line((x0, y1), (x1, y0), role="outline", owner=numeral, width=W_THIN))
        return True
    if kind == "avatar":
        figure.prims.append(circle(((x0 + x1) / 2.0, mid_y), min(w, h) * 0.35, role="outline",
                                   owner=numeral, width=W_THIN))
        return True
    if kind == "chart":
        _rect(figure, box, numeral, width=W_THIN)
        figure.prims.append(polyline([(x0 + 3.0, y0 + 2.0), (x0 + 3.0, y1 - 3.0),
                                      (x1 - 2.0, y1 - 3.0)], role="outline", owner=numeral,
                                     width=W_THIN))
        points: list[Point] = []
        for i in range(7):
            t = i / 6.0
            points.append((x0 + 3.0 + (w - 6.0) * t,
                           y1 - 3.0 - (h - 6.0) * (0.25 + 0.55 * (0.5 + 0.5 *
                                                                  math.sin(3.1 * t + 0.6)))))
        figure.prims.append(polyline(points, role="outline", owner=numeral, width=W_THIN))
        return True
    if kind == "map":
        _rect(figure, box, numeral, width=W_THIN)
        for i in range(1, 4):
            y = y0 + h * i / 4.0
            figure.prims.append(polyline(
                [(x0 + w * t / 6.0, y + math.sin(t) * h * 0.05) for t in range(7)],
                role="outline", owner=numeral, width=W_THIN))
        return True
    if kind == "badge":
        span = min(w, 12.0)
        figure.prims.append(polygon(
            geom.rounded_rect_poly(x0, mid_y - 2.4, span, 4.8, 2.4), role="outline",
            owner=numeral, width=W_THIN))
        _label(figure, (x0, mid_y - 2.4, x0 + span, mid_y + 2.4), label, numeral)
        return True
    if kind == "divider":
        figure.prims.append(line((x0, mid_y), (x1, mid_y), role="outline", owner=numeral,
                                 width=W_THIN))
        return False
    _rect(figure, box, numeral, width=W_THIN)
    _label(figure, box, label, numeral)
    return True


def _anchors(numeral: str, outline: list[Point]) -> list[Anchor]:
    from .graphfig import _box_anchors
    return _box_anchors(numeral, outline)
