"""Typed SVG primitives. No module anywhere builds an SVG string by hand, and no model ever
produces one.

Two rules give this file its shape.

**Determinism.** The same scene and the same profile must produce byte-identical SVG, because
that is what makes "the rendered sheet matches its specification" a checkable statement rather
than an opinion. Numbers are formatted through one function, elements are emitted in one order,
and nothing anywhere consults a clock or a random source.

**Self-describing output.** Every drawn thing carries the identifier of the semantic object it
came from: ``data-entity-id`` on a component, ``data-relation-id`` on a connection,
``data-reference-label`` on a numeral. That is what lets a validator check the rendered artifact
itself, rather than checking the intention that produced it.
"""
from __future__ import annotations

import json
import math
from html import escape
from typing import Iterable, Optional, Sequence

from ..profiles import DrawingProfile
from ..schemas import Box, Point

BLACK = "#000000"
WHITE = "#ffffff"


def number(value: float) -> str:
    """One formatting of a coordinate, used everywhere."""
    rounded = round(float(value) + 0.0, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def attribute(name: str, value: object) -> str:
    return f'{name}="{escape(str(value), quote=True)}"'


def points_attribute(points: Sequence[Point]) -> str:
    return " ".join(f"{number(p.x)},{number(p.y)}" for p in points)


def path_data(points: Sequence[Point]) -> str:
    if not points:
        return ""
    head = f"M {number(points[0].x)} {number(points[0].y)}"
    tail = " ".join(f"L {number(p.x)} {number(p.y)}" for p in points[1:])
    return f"{head} {tail}".strip()


class SvgDocument:
    """An accumulating SVG document. Append-only, emitted once."""

    def __init__(self, profile: DrawingProfile, metadata: Optional[dict] = None):
        self.profile = profile
        self.metadata = dict(metadata or {})
        self.parts: list[str] = []

    # -- structure ----------------------------------------------------------
    def open_group(self, **attributes: object) -> None:
        rendered = " ".join(attribute(key.replace("_", "-"), value)
                            for key, value in attributes.items() if value not in (None, ""))
        self.parts.append(f"<g {rendered}>" if rendered else "<g>")

    def close_group(self) -> None:
        self.parts.append("</g>")

    # -- primitives ---------------------------------------------------------
    def rect(self, box: Box, *, radius: float = 0.0, **attributes: object) -> None:
        extra = self._extra(attributes)
        corner = f' rx="{number(radius)}" ry="{number(radius)}"' if radius else ""
        self.parts.append(
            f'<rect x="{number(box.x)}" y="{number(box.y)}" width="{number(box.width)}" '
            f'height="{number(box.height)}"{corner} fill="none"{extra}/>')

    def ellipse(self, box: Box, **attributes: object) -> None:
        extra = self._extra(attributes)
        self.parts.append(
            f'<ellipse cx="{number(box.cx)}" cy="{number(box.cy)}" '
            f'rx="{number(box.width / 2)}" ry="{number(box.height / 2)}" fill="none"{extra}/>')

    def circle(self, box: Box, **attributes: object) -> None:
        extra = self._extra(attributes)
        radius = min(box.width, box.height) / 2
        self.parts.append(
            f'<circle cx="{number(box.cx)}" cy="{number(box.cy)}" r="{number(radius)}" '
            f'fill="none"{extra}/>')

    def polygon(self, points: Sequence[Point], **attributes: object) -> None:
        extra = self._extra(attributes)
        self.parts.append(
            f'<polygon points="{points_attribute(points)}" fill="none"{extra}/>')

    def polyline(self, points: Sequence[Point], **attributes: object) -> None:
        extra = self._extra(attributes)
        self.parts.append(f'<path d="{path_data(points)}" fill="none"{extra}/>')

    def line(self, start: Point, end: Point, **attributes: object) -> None:
        self.polyline([start, end], **attributes)

    def cylinder(self, box: Box, **attributes: object) -> None:
        """A cylinder drawn as an outline plus its visible top ellipse."""
        extra = self._extra(attributes)
        lift = min(box.height / 4, box.width / 5)
        self.parts.append(
            f'<path d="M {number(box.x)} {number(box.y + lift)} '
            f'A {number(box.width / 2)} {number(lift)} 0 0 1 {number(box.right)} '
            f'{number(box.y + lift)} L {number(box.right)} {number(box.bottom - lift)} '
            f'A {number(box.width / 2)} {number(lift)} 0 0 1 {number(box.x)} '
            f'{number(box.bottom - lift)} Z" fill="none"{extra}/>')
        self.parts.append(
            f'<path d="M {number(box.x)} {number(box.y + lift)} '
            f'A {number(box.width / 2)} {number(lift)} 0 0 0 {number(box.right)} '
            f'{number(box.y + lift)}" fill="none"{extra}/>')

    def text(self, x: float, y: float, body: str, *, height: float,
             anchor: str = "start", **attributes: object) -> None:
        extra = self._extra(attributes)
        self.parts.append(
            f'<text x="{number(x)}" y="{number(y)}" font-family="{escape(self.profile.font_family, quote=True)}" '
            f'font-size="{number(height)}" fill="{BLACK}" stroke="none" '
            f'text-anchor="{anchor}"{extra}>{escape(str(body))}</text>')

    def text_block(self, lines: Iterable[str], cx: float, cy: float, *, height: float,
                   leading: float = 1.25, **attributes: object) -> None:
        rows = list(lines)
        if not rows:
            return
        total = len(rows) * height * leading
        first = cy - total / 2 + height * 0.95
        for index, row in enumerate(rows):
            self.text(cx, first + index * height * leading, row, height=height,
                      anchor="middle", **(attributes if index == 0 else {}))

    def arrowhead(self, tip: Point, previous: Point, **attributes: object) -> None:
        """A closed arrowhead at ``tip``, pointing away from ``previous``."""
        dx, dy = tip.x - previous.x, tip.y - previous.y
        norm = math.hypot(dx, dy)
        if norm < 1e-6:
            return
        ux, uy = dx / norm, dy / norm
        length = self.profile.arrow_length
        half = self.profile.arrow_width / 2
        base = (tip.x - ux * length, tip.y - uy * length)
        left = Point(x=base[0] - uy * half, y=base[1] + ux * half)
        right = Point(x=base[0] + uy * half, y=base[1] - ux * half)
        extra = self._extra(attributes)
        self.parts.append(
            f'<polygon points="{points_attribute([tip, left, right])}" fill="{BLACK}"{extra}/>')

    # -- emission -----------------------------------------------------------
    def _extra(self, attributes: dict) -> str:
        rendered = " ".join(attribute(key.replace("_", "-"), value)
                            for key, value in attributes.items() if value not in (None, ""))
        return (" " + rendered) if rendered else ""

    def render(self) -> str:
        profile = self.profile
        width_mm = profile.mm(profile.sheet_width)
        height_mm = profile.mm(profile.sheet_height)
        meta = escape(json.dumps(self.metadata, sort_keys=True))
        header = (
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{number(width_mm)}mm" height="{number(height_mm)}mm" '
            f'viewBox="0 0 {number(profile.sheet_width)} {number(profile.sheet_height)}" '
            f'{attribute("data-profile", profile.version_tag)}>')
        background = (f'<rect x="0" y="0" width="{number(profile.sheet_width)}" '
                      f'height="{number(profile.sheet_height)}" fill="{WHITE}" stroke="none"/>')
        return "".join([header, f"<metadata>{meta}</metadata>", background,
                        *self.parts, "</svg>"])
