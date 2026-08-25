"""The drawing itself, held as data.

A figure here is a list of primitives in millimetres plus the anchors, numerals and lead lines
that the placement solver attaches to it. Nothing about a figure is an image until the very last
step, which is what makes the compliance checks in ``fm.validate`` arithmetic rather than
opinion: you can ask whether two lead lines cross because you have both lead lines as numbers.

Every primitive answers two questions. ``svg`` says how to draw it, so curves stay curves and the
sheet stays crisp at any size. ``polys`` says what it occupies, flattened to polylines, so the
solver and the validator have one uniform thing to reason about.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

from . import geom
from .geom import BBox, Point, Poly

# Line weights in millimetres. 37 CFR 1.84(l) asks for lines that are black, dense, and uniformly
# thick; it sets no number, so these are drafting-practice weights chosen to survive the Office's
# reduction to two thirds and still read.
W_OUTLINE = 0.45
W_HIDDEN = 0.35
W_HATCH = 0.30
W_CENTRE = 0.30
W_LEADER = 0.35
W_THIN = 0.30

# 37 CFR 1.84(p)(3): reference characters and figure captions at least 0.32 cm high. The rule is
# about the height of the characters, so the font size is set from the cap height.
MIN_CHAR_MM = 3.2
NUMERAL_SIZE = MIN_CHAR_MM / geom.TEXT_CAP_RATIO      # font size giving a 3.2 mm cap height
CAPTION_SIZE = NUMERAL_SIZE * 1.25
LEGEND_SIZE = NUMERAL_SIZE

DASH_HIDDEN = (1.6, 1.0)
DASH_CENTRE = (4.0, 1.2, 0.8, 1.2)
DASH_PROJECTION = (2.4, 1.4)

ROLES = ("outline", "hidden", "hatch", "centre", "leader", "arrow", "legend",
         "numeral", "caption", "projection", "frame")


@dataclass
class Prim:
    """One drawable thing.

    ``owner`` names the reference numeral whose element this belongs to, or is empty for the
    parts of a figure that no numeral claims. It is what lets the editor highlight a part when
    you click its row in the registry, and what lets the solver know which ink a lead line is
    allowed to touch.
    """
    kind: str                       # line | polyline | polygon | circle | ellipse | arc | text
    pts: list[Point] = field(default_factory=list)
    role: str = "outline"
    owner: str = ""
    width: float = W_OUTLINE
    dash: Optional[tuple[float, ...]] = None
    text: str = ""
    size: float = NUMERAL_SIZE
    anchor: str = "middle"          # text anchor: start | middle | end
    baseline: str = "middle"
    r: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    a0: float = 0.0
    a1: float = 0.0
    fill: bool = False              # solid black, only ever for an arrow head

    # ---------------------------------------------------------------------------------- data

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["pts"] = [[round(x, 4), round(y, 4)] for x, y in self.pts]
        if self.dash is not None:
            out["dash"] = list(self.dash)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Prim":
        data = dict(raw)
        data["pts"] = [(float(p[0]), float(p[1])) for p in data.get("pts") or []]
        dash = data.get("dash")
        data["dash"] = tuple(dash) if dash else None
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    # ------------------------------------------------------------------------------- geometry

    def polys(self) -> list[Poly]:
        """What this primitive occupies, as polylines. Closed shapes repeat their first point."""
        if self.kind in ("line", "polyline"):
            return [list(self.pts)] if len(self.pts) >= 2 else []
        if self.kind == "polygon":
            return [list(self.pts) + [self.pts[0]]] if len(self.pts) >= 3 else []
        if self.kind == "circle":
            c = self.pts[0]
            ring = geom.circle_poly(c[0], c[1], self.r)
            return [ring + [ring[0]]]
        if self.kind == "ellipse":
            c = self.pts[0]
            ring = geom.ellipse_poly(c[0], c[1], self.rx, self.ry)
            return [ring + [ring[0]]]
        if self.kind == "arc":
            c = self.pts[0]
            return [geom.arc_poly(c[0], c[1], self.r, self.a0, self.a1)]
        if self.kind == "text":
            return [geom.bbox_poly(self.bbox()) + [self.bbox()[:2]]]
        return []

    def bbox(self) -> BBox:
        if self.kind == "text":
            p = self.pts[0] if self.pts else (0.0, 0.0)
            return geom.text_bbox(self.text, self.size, p[0], p[1], self.anchor, self.baseline)
        boxes = [geom.poly_bbox(poly) for poly in self.polys() if poly]
        return geom.bbox_union(boxes) or (0.0, 0.0, 0.0, 0.0)

    def translated(self, dx: float, dy: float) -> "Prim":
        clone = Prim.from_dict(self.to_dict())
        clone.pts = geom.translate(self.pts, dx, dy)
        return clone

    # ------------------------------------------------------------------------------------ svg

    def svg(self, scale: float = 1.0, dx: float = 0.0, dy: float = 0.0,
            text_scale: Optional[float] = None) -> str:
        """SVG for this primitive.

        ``text_scale`` exists because a character does not shrink with the drawing. 37 CFR
        1.84(p)(3) sets a floor on the height of a character on the sheet, and a draughtsman
        letters a reduced view at the same size as a full one. Positions scale; glyphs do not.
        """
        text_scale = scale if text_scale is None else text_scale

        def sx(p: Point) -> str:
            return f"{p[0] * scale + dx:.3f},{p[1] * scale + dy:.3f}"

        stroke = self.width * scale
        style = f'stroke="#000" stroke-width="{stroke:.3f}" fill="none"'
        if self.dash:
            pattern = " ".join(f"{v * scale:.2f}" for v in self.dash)
            style += f' stroke-dasharray="{pattern}"'
        style += ' stroke-linecap="round" stroke-linejoin="round"'
        attrs = f' data-role="{self.role}"' + (f' data-owner="{self.owner}"' if self.owner else "")

        if self.kind in ("line", "polyline") and len(self.pts) >= 2:
            return f'<polyline points="{" ".join(sx(p) for p in self.pts)}" {style}{attrs}/>'
        if self.kind == "polygon" and len(self.pts) >= 3:
            fill = '#000' if self.fill else 'none'
            style = style.replace('fill="none"', f'fill="{fill}"')
            return f'<polygon points="{" ".join(sx(p) for p in self.pts)}" {style}{attrs}/>'
        if self.kind == "circle":
            c = self.pts[0]
            return (f'<circle cx="{c[0] * scale + dx:.3f}" cy="{c[1] * scale + dy:.3f}" '
                    f'r="{self.r * scale:.3f}" {style}{attrs}/>')
        if self.kind == "ellipse":
            c = self.pts[0]
            return (f'<ellipse cx="{c[0] * scale + dx:.3f}" cy="{c[1] * scale + dy:.3f}" '
                    f'rx="{self.rx * scale:.3f}" ry="{self.ry * scale:.3f}" {style}{attrs}/>')
        if self.kind == "arc":
            c = self.pts[0]
            return _arc_svg(c, self.r, self.a0, self.a1, scale, dx, dy, style, attrs)
        if self.kind == "text":
            p = self.pts[0]
            baseline = {"middle": "central", "hanging": "hanging"}.get(self.baseline, "alphabetic")
            return (f'<text x="{p[0] * scale + dx:.3f}" y="{p[1] * scale + dy:.3f}" '
                    f'font-family="DejaVu Sans, Liberation Sans, Helvetica, Arial, sans-serif" '
                    f'font-size="{self.size * text_scale:.3f}" text-anchor="{self.anchor}" '
                    f'dominant-baseline="{baseline}" fill="#000" stroke="none"{attrs}>'
                    f'{_xml_escape(self.text)}</text>')
        return ""


def _arc_svg(c: Point, r: float, a0: float, a1: float, scale: float, dx: float, dy: float,
             style: str, attrs: str) -> str:
    start = (c[0] + r * math.cos(math.radians(a0)), c[1] + r * math.sin(math.radians(a0)))
    end = (c[0] + r * math.cos(math.radians(a1)), c[1] + r * math.sin(math.radians(a1)))
    large = 1 if abs(a1 - a0) > 180 else 0
    sweep = 1 if a1 > a0 else 0
    return (f'<path d="M {start[0] * scale + dx:.3f},{start[1] * scale + dy:.3f} '
            f'A {r * scale:.3f},{r * scale:.3f} 0 {large} {sweep} '
            f'{end[0] * scale + dx:.3f},{end[1] * scale + dy:.3f}" {style}{attrs}/>')


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ------------------------------------------------------------------------------------ builders


def line(a: Point, b: Point, **kw) -> Prim:
    return Prim(kind="line", pts=[a, b], **kw)


def polyline(points: Sequence[Point], **kw) -> Prim:
    return Prim(kind="polyline", pts=list(points), **kw)


def polygon(points: Sequence[Point], **kw) -> Prim:
    return Prim(kind="polygon", pts=list(points), **kw)


def circle(centre: Point, r: float, **kw) -> Prim:
    return Prim(kind="circle", pts=[centre], r=r, **kw)


def ellipse(centre: Point, rx: float, ry: float, **kw) -> Prim:
    return Prim(kind="ellipse", pts=[centre], rx=rx, ry=ry, **kw)


def arc(centre: Point, r: float, a0: float, a1: float, **kw) -> Prim:
    return Prim(kind="arc", pts=[centre], r=r, a0=a0, a1=a1, **kw)


def text(at: Point, body: str, size: float = LEGEND_SIZE, role: str = "legend", **kw) -> Prim:
    return Prim(kind="text", pts=[at], text=body, size=size, role=role, width=0.0, **kw)


def hidden(points: Sequence[Point], **kw) -> Prim:
    kw.setdefault("width", W_HIDDEN)
    return Prim(kind="polyline", pts=list(points), role="hidden", dash=DASH_HIDDEN, **kw)


# ------------------------------------------------------------------------- anchors and numerals


@dataclass
class Anchor:
    """A place on an element that a lead line may land on.

    ``normal`` points away from the body of the element, which is the direction a numeral wants
    to sit in: outside the geometry, per 37 CFR 1.84(p)(2), which keeps characters off hatched
    and shaded surfaces.
    """
    numeral: str
    point: Point
    normal: Point = (0.0, -1.0)
    weight: float = 1.0
    inside: bool = False        # an anchor in the middle of an open area, e.g. a block-diagram box

    def to_dict(self) -> dict[str, Any]:
        return {"numeral": self.numeral, "point": list(self.point),
                "normal": list(self.normal), "weight": self.weight, "inside": self.inside}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Anchor":
        return cls(numeral=raw["numeral"], point=tuple(raw["point"]),
                   normal=tuple(raw.get("normal", (0.0, -1.0))),
                   weight=float(raw.get("weight", 1.0)), inside=bool(raw.get("inside")))


@dataclass
class NumeralLabel:
    numeral: str
    x: float
    y: float
    size: float = NUMERAL_SIZE
    placed_by: str = "solver"    # solver | user

    def bbox(self) -> BBox:
        return geom.text_bbox(self.numeral, self.size, self.x, self.y, "middle", "middle")

    def to_dict(self) -> dict[str, Any]:
        return {"numeral": self.numeral, "x": round(self.x, 4), "y": round(self.y, 4),
                "size": self.size, "placed_by": self.placed_by}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NumeralLabel":
        return cls(numeral=raw["numeral"], x=float(raw["x"]), y=float(raw["y"]),
                   size=float(raw.get("size", NUMERAL_SIZE)),
                   placed_by=raw.get("placed_by", "solver"))


@dataclass
class Leader:
    """A lead line, from just outside its numeral to a point on the element it indicates."""
    numeral: str
    points: list[Point]
    arrow: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"numeral": self.numeral,
                "points": [[round(x, 4), round(y, 4)] for x, y in self.points],
                "arrow": self.arrow}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Leader":
        return cls(numeral=raw["numeral"],
                   points=[(float(p[0]), float(p[1])) for p in raw["points"]],
                   arrow=bool(raw.get("arrow")))

    def tip(self) -> Point:
        return self.points[-1]

    def tail(self) -> Point:
        return self.points[0]


# ------------------------------------------------------------------------------------- figures


@dataclass
class Figure:
    """One view. Coordinates are millimetres at the figure's own drawing scale."""
    label: str
    kind: str
    title: str = ""
    prims: list[Prim] = field(default_factory=list)
    anchors: dict[str, list[Anchor]] = field(default_factory=dict)
    labels: list[NumeralLabel] = field(default_factory=list)
    leaders: list[Leader] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scene: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------------------------- queries

    def content_bbox(self, include_labels: bool = True) -> BBox:
        boxes = [p.bbox() for p in self.prims if p.pts]
        if include_labels:
            boxes += [lab.bbox() for lab in self.labels]
            boxes += [geom.poly_bbox(ld.points) for ld in self.leaders if ld.points]
        return geom.bbox_union(boxes) or (0.0, 0.0, 1.0, 1.0)

    def ink_polys(self, *, roles: Optional[Iterable[str]] = None,
                  exclude_owner: str = "") -> list[Poly]:
        wanted = set(roles) if roles else None
        out: list[Poly] = []
        for p in self.prims:
            if wanted is not None and p.role not in wanted:
                continue
            if exclude_owner and p.owner == exclude_owner:
                continue
            out.extend(poly for poly in p.polys() if len(poly) >= 2)
        return out

    def numerals(self) -> list[str]:
        seen: list[str] = []
        for numeral in list(self.anchors.keys()) + [lab.numeral for lab in self.labels]:
            if numeral not in seen:
                seen.append(numeral)
        return seen

    def label_for(self, numeral: str) -> Optional[NumeralLabel]:
        for lab in self.labels:
            if lab.numeral == numeral:
                return lab
        return None

    def leader_for(self, numeral: str) -> Optional[Leader]:
        for leader in self.leaders:
            if leader.numeral == numeral:
                return leader
        return None

    # ----------------------------------------------------------------------------- production

    def decorated_prims(self) -> list[Prim]:
        """Everything to draw: the figure, then its lead lines, then its numerals on top."""
        out = list(self.prims)
        for leader in self.leaders:
            out.append(polyline(leader.points, role="leader", owner=leader.numeral,
                                width=W_LEADER))
            if leader.arrow and len(leader.points) >= 2:
                head = geom.arrow_head(leader.points[-1], leader.points[-2])
                out.append(polygon(head, role="arrow", owner=leader.numeral, width=W_LEADER,
                                   fill=True))
        for lab in self.labels:
            out.append(Prim(kind="text", pts=[(lab.x, lab.y)], text=lab.numeral, size=lab.size,
                            role="numeral", owner=lab.numeral, width=0.0))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "kind": self.kind, "title": self.title,
            "prims": [p.to_dict() for p in self.prims],
            "anchors": {k: [a.to_dict() for a in v] for k, v in self.anchors.items()},
            "labels": [lab.to_dict() for lab in self.labels],
            "leaders": [ld.to_dict() for ld in self.leaders],
            "notes": list(self.notes), "scene": self.scene,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Figure":
        return cls(
            label=raw["label"], kind=raw["kind"], title=raw.get("title", ""),
            prims=[Prim.from_dict(p) for p in raw.get("prims", [])],
            anchors={k: [Anchor.from_dict(a) for a in v]
                     for k, v in (raw.get("anchors") or {}).items()},
            labels=[NumeralLabel.from_dict(x) for x in raw.get("labels", [])],
            leaders=[Leader.from_dict(x) for x in raw.get("leaders", [])],
            notes=list(raw.get("notes") or []), scene=raw.get("scene") or {},
        )


def normalise(figure: Figure, margin: float = 2.0) -> Figure:
    """Move a figure so its ink starts at (margin, margin). Layout is easier from a known corner."""
    box = figure.content_bbox()
    dx, dy = margin - box[0], margin - box[1]
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return figure
    figure.prims = [p.translated(dx, dy) for p in figure.prims]
    for numeral, anchors in figure.anchors.items():
        figure.anchors[numeral] = [
            Anchor(a.numeral, (a.point[0] + dx, a.point[1] + dy), a.normal, a.weight, a.inside)
            for a in anchors]
    for lab in figure.labels:
        lab.x += dx
        lab.y += dy
    for leader in figure.leaders:
        leader.points = geom.translate(leader.points, dx, dy)
    return figure
