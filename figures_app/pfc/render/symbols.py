"""Conventional draughting symbols for disclosed component classes.

**Why this exists, and the line it walks.** The compiler's first rule is that it must never
invent the physical appearance of a component because it recognises the component's name. Under
that rule everything came out as a rectangle, and a page of identical rectangles is a true
statement about the invention that tells a reader nothing. The complaint was fair.

The resolution is the distinction a draughtsman already makes. Drawing a coil as a coil is not
inventing geometry: a coil symbol is the standard notation for the thing the description named,
the same way an arrow is the standard notation for a disclosed direction. Drawing that coil with
a specific number of turns, a bobbin, end caps and a mounting flange WOULD be inventing geometry,
because the document says nothing about any of it.

So every symbol here is:

* **notation, not depiction** — schematic, uniform line weight, no shading, no detail that
  implies a dimension or a count the text did not give;
* **keyed to a disclosed class**, never to a guess. An entity whose class cannot be established
  from the document's own words stays a plain outline, which is what most of them are;
* **drawn inside the box the layout engine allotted**, so the geometry validators measure the
  same rectangle whatever symbol fills it.

A symbol that would need a dimension the patent never states is not in this file.
"""
from __future__ import annotations

import math
from typing import Callable

from ..profiles import DrawingProfile
from ..schemas import Box, Point
from .svgdoc import SvgDocument

# Proportion each symbol wants, as width / height. The layout engine uses these so a shaft comes
# out long and a seal comes out round, rather than everything sharing one aspect ratio.
ASPECT: dict[str, float] = {
    "coil": 2.2, "spring": 2.4, "motor": 1.0, "pump": 1.0, "valve": 1.4,
    "power": 1.1, "sensor": 1.3, "magnet": 1.2, "electrode": 2.6, "plate": 2.8,
    "substrate": 3.0, "adhesive": 3.2, "housing": 1.5, "chamber": 1.4, "shaft": 3.4,
    "tube": 2.6, "gear": 1.0, "bearing": 1.0, "roller": 1.6, "belt": 2.4,
    "piston": 2.2, "nozzle": 1.4, "suction_cup": 1.3, "fastener": 0.6, "seal": 1.0,
    "filter": 1.2, "heater": 1.6, "display": 1.4, "processor": 1.3, "memory": 1.1,
    "antenna": 0.9, "lens": 1.4, "opening": 1.0, "connector": 1.2, "workpiece": 2.0,
    "generic_component": 1.5, "actuator": 2.0, "controller": 1.3, "interface": 1.4,
    "storage": 1.1, "network": 1.4, "boundary": 1.5, "conveyor": 2.8, "arm": 2.6,
    "gripper": 1.2, "wheel": 1.0, "cutter": 1.2, "beam": 3.0, "frame": 1.6,
    "button": 1.0, "knob": 1.0, "data_store": 1.1,
}

_HATCH_STEP_MM = 1.6


def aspect(visual_class: str) -> float:
    return ASPECT.get(visual_class, ASPECT["generic_component"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _p(x: float, y: float) -> Point:
    return Point(x=x, y=y)


def _hatch(doc: SvgDocument, box: Box, angle: float = 45.0) -> None:
    """Section hatching: evenly spaced parallel lines clipped to the box.

    The office's convention for a cut surface. Spacing comes from the profile so it survives the
    reduction a drawing may be printed at.
    """
    step = _HATCH_STEP_MM * doc.profile.units_per_mm
    slope = math.tan(math.radians(angle))
    if abs(slope) < 1e-6 or step <= 0:
        return
    # The horizontal distance one hatch line travels crossing the box. Taken on the ABSOLUTE
    # slope: using the signed value meant a negative angle produced a run of 220 million units,
    # and the workpiece symbol spent forty seconds emitting nothing. Found by a test that timed
    # it rather than by one that checked it.
    run = abs(box.height / slope)
    x = box.x - run
    limit = box.right + run
    guard = int((limit - x) / step) + 2
    for _ in range(max(0, guard)):
        if x > limit:
            break
        points = _clip_segment((x, box.bottom), (x + box.height / slope, box.y), box)
        if points:
            doc.polyline([_p(*points[0]), _p(*points[1])],
                         stroke_width=doc.profile.thin_stroke)
        x += step


def _clip_segment(a: tuple[float, float], b: tuple[float, float], box: Box):
    """Liang-Barsky, enough of it to keep hatching inside its own outline."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - box.x), (dx, box.right - a[0]),
                 (-dy, a[1] - box.y), (dy, box.bottom - a[1])):
        if abs(p) < 1e-9:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    if t0 > t1:
        return None
    return ((a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy))


def _arc(doc: SvgDocument, x0: float, y0: float, rx: float, ry: float,
         x1: float, y1: float, sweep: int = 1) -> None:
    from .svgdoc import number

    doc.parts.append(
        f'<path d="M {number(x0)} {number(y0)} A {number(rx)} {number(ry)} 0 0 {sweep} '
        f'{number(x1)} {number(y1)}" fill="none"/>')


def _centreline(doc: SvgDocument, box: Box) -> None:
    dash = f"{doc.profile.stroke * 8:.0f} {doc.profile.stroke * 3:.0f} " \
           f"{doc.profile.stroke * 2:.0f} {doc.profile.stroke * 3:.0f}"
    doc.polyline([_p(box.x - box.width * 0.06, box.cy), _p(box.right + box.width * 0.06, box.cy)],
                 stroke_width=doc.profile.thin_stroke, stroke_dasharray=dash)


# ---------------------------------------------------------------------------
# the symbols
# ---------------------------------------------------------------------------
def _box(doc: SvgDocument, box: Box) -> None:
    doc.rect(box)


def _rounded(doc: SvgDocument, box: Box) -> None:
    doc.rect(box, radius=min(box.width, box.height) * 0.12)


def _coil(doc: SvgDocument, box: Box) -> None:
    """An induction coil: a run of half-turns on a centreline, with its two leads.

    The number of loops is a drawing convention, not a count from the document, so it is fixed
    and modest rather than scaled to anything the text says.
    """
    turns = 5
    span = box.width * 0.78
    left = box.x + (box.width - span) / 2
    radius = span / (turns * 2)
    ry = min(box.height * 0.42, radius * 1.6)
    for index in range(turns):
        x0 = left + index * 2 * radius
        _arc(doc, x0, box.cy, radius, ry, x0 + 2 * radius, box.cy, sweep=1)
    doc.polyline([_p(box.x, box.cy), _p(left, box.cy)])
    doc.polyline([_p(left + turns * 2 * radius, box.cy), _p(box.right, box.cy)])


def _spring(doc: SvgDocument, box: Box) -> None:
    coils = 6
    span = box.width * 0.8
    left = box.x + (box.width - span) / 2
    step = span / (coils * 2)
    top, bottom = box.y + box.height * 0.15, box.bottom - box.height * 0.15
    points = [_p(box.x, box.cy), _p(left, box.cy)]
    for index in range(coils * 2):
        points.append(_p(left + (index + 0.5) * step, top if index % 2 == 0 else bottom))
    points.extend([_p(left + span, box.cy), _p(box.right, box.cy)])
    doc.polyline(points)


def _motor(doc: SvgDocument, box: Box) -> None:
    """A rotary machine: the circle-and-shaft convention, no letters inside."""
    radius = min(box.width, box.height) * 0.42
    doc.circle(Box(x=box.cx - radius, y=box.cy - radius, width=radius * 2, height=radius * 2))
    doc.polyline([_p(box.cx + radius, box.cy), _p(box.right, box.cy)])
    doc.polyline([_p(box.cx - radius * 0.5, box.cy - radius * 0.5),
                  _p(box.cx + radius * 0.5, box.cy + radius * 0.5)])


def _pump(doc: SvgDocument, box: Box) -> None:
    radius = min(box.width, box.height) * 0.42
    doc.circle(Box(x=box.cx - radius, y=box.cy - radius, width=radius * 2, height=radius * 2))
    doc.polygon([_p(box.cx - radius * 0.45, box.cy - radius * 0.5),
                 _p(box.cx + radius * 0.6, box.cy),
                 _p(box.cx - radius * 0.45, box.cy + radius * 0.5)])


def _valve(doc: SvgDocument, box: Box) -> None:
    half = box.height * 0.42
    doc.polygon([_p(box.x, box.cy - half), _p(box.cx, box.cy), _p(box.x, box.cy + half)])
    doc.polygon([_p(box.right, box.cy - half), _p(box.cx, box.cy),
                 _p(box.right, box.cy + half)])
    doc.polyline([_p(box.cx, box.cy), _p(box.cx, box.y)])


def _power(doc: SvgDocument, box: Box) -> None:
    """A source of electrical power: alternating long and short plates."""
    cells = 2
    step = box.width / (cells * 2 + 1)
    x = box.x + step * 0.6
    for index in range(cells * 2):
        long_plate = index % 2 == 0
        half = box.height * (0.42 if long_plate else 0.24)
        doc.polyline([_p(x, box.cy - half), _p(x, box.cy + half)])
        x += step
    doc.polyline([_p(box.x, box.cy), _p(box.x + step * 0.6, box.cy)])
    doc.polyline([_p(x - step, box.cy), _p(box.right, box.cy)])


def _sensor(doc: SvgDocument, box: Box) -> None:
    """A sensing element: a body with a probe, the convention for something that measures."""
    body = Box(x=box.x, y=box.y + box.height * 0.18,
               width=box.width * 0.68, height=box.height * 0.64)
    doc.rect(body, radius=body.height * 0.15)
    doc.polyline([_p(body.right, box.cy), _p(box.right, box.cy)])
    doc.polyline([_p(box.right, box.y + box.height * 0.2),
                  _p(box.right, box.bottom - box.height * 0.2)])
    doc.polyline([_p(body.x + body.width * 0.25, box.cy),
                  _p(body.x + body.width * 0.75, box.cy)])


def _magnet(doc: SvgDocument, box: Box) -> None:
    doc.rect(box)
    doc.polyline([_p(box.cx, box.y), _p(box.cx, box.bottom)])


def _plate(doc: SvgDocument, box: Box) -> None:
    """A plate, substrate or layer: a thin slab, hatched as a cut surface."""
    doc.rect(box)
    _hatch(doc, box)


def _adhesive(doc: SvgDocument, box: Box) -> None:
    """A layer of adhesive: the stippled/wavy band convention for a compliant material."""
    doc.rect(box)
    waves = max(4, int(box.width / max(1.0, box.height)) * 2)
    step = box.width / waves
    points = [_p(box.x, box.cy)]
    for index in range(waves):
        x0 = box.x + index * step
        _arc(doc, x0, box.cy, step / 2, box.height * 0.3, x0 + step, box.cy,
             sweep=1 if index % 2 == 0 else 0)
    del points


def _housing(doc: SvgDocument, box: Box) -> None:
    """An enclosure: an outline with a wall, which is what "within a housing" discloses."""
    doc.rect(box, radius=min(box.width, box.height) * 0.08)
    wall = min(box.width, box.height) * 0.06
    inner = Box(x=box.x + wall, y=box.y + wall,
                width=max(1.0, box.width - 2 * wall), height=max(1.0, box.height - 2 * wall))
    doc.rect(inner, radius=min(inner.width, inner.height) * 0.06)


def _chamber(doc: SvgDocument, box: Box) -> None:
    doc.rect(box, radius=min(box.width, box.height) * 0.2)


def _shaft(doc: SvgDocument, box: Box) -> None:
    doc.rect(box, radius=box.height / 2)
    _centreline(doc, box)


def _tube(doc: SvgDocument, box: Box) -> None:
    """A conduit: two walls and an open bore, shown by the ellipse at the near end.

    The end ellipse is what separates a tube from a solid beam on the page. Without it the two
    symbols are the same three horizontal lines.
    """
    inset = box.height * 0.24
    doc.polyline([_p(box.x, box.y), _p(box.right, box.y)])
    doc.polyline([_p(box.x, box.bottom), _p(box.right, box.bottom)])
    doc.polyline([_p(box.x, box.y + inset), _p(box.right, box.y + inset)])
    doc.polyline([_p(box.x, box.bottom - inset), _p(box.right, box.bottom - inset)])
    mouth = min(box.width * 0.1, box.height * 0.5)
    doc.ellipse(Box(x=box.x - mouth / 2, y=box.y, width=mouth, height=box.height))
    doc.ellipse(Box(x=box.x - mouth / 2 + inset, y=box.y + inset,
                    width=max(1.0, mouth - inset * 0.4),
                    height=max(1.0, box.height - 2 * inset)))


def _gear(doc: SvgDocument, box: Box) -> None:
    """A toothed wheel. The tooth count is notation, not a disclosed number."""
    teeth = 12
    outer = min(box.width, box.height) / 2
    root = outer * 0.82
    points = []
    for index in range(teeth * 2):
        angle = math.pi * index / teeth
        radius = outer if index % 2 == 0 else root
        points.append(_p(box.cx + radius * math.cos(angle), box.cy + radius * math.sin(angle)))
    doc.polygon(points)
    hub = outer * 0.24
    doc.circle(Box(x=box.cx - hub, y=box.cy - hub, width=hub * 2, height=hub * 2))


def _bearing(doc: SvgDocument, box: Box) -> None:
    outer = min(box.width, box.height) / 2
    doc.circle(Box(x=box.cx - outer, y=box.cy - outer, width=outer * 2, height=outer * 2))
    inner = outer * 0.52
    doc.circle(Box(x=box.cx - inner, y=box.cy - inner, width=inner * 2, height=inner * 2))
    ball = outer * 0.14
    mid = (outer + inner) / 2
    for index in range(6):
        angle = math.pi * index / 3
        cx, cy = box.cx + mid * math.cos(angle), box.cy + mid * math.sin(angle)
        doc.circle(Box(x=cx - ball, y=cy - ball, width=ball * 2, height=ball * 2))


def _roller(doc: SvgDocument, box: Box) -> None:
    doc.cylinder(box)
    _centreline(doc, box)


def _belt(doc: SvgDocument, box: Box) -> None:
    radius = box.height * 0.38
    left = Box(x=box.x, y=box.cy - radius, width=radius * 2, height=radius * 2)
    right = Box(x=box.right - radius * 2, y=box.cy - radius, width=radius * 2, height=radius * 2)
    doc.circle(left)
    doc.circle(right)
    doc.polyline([_p(left.cx, left.cy - radius), _p(right.cx, right.cy - radius)])
    doc.polyline([_p(left.cx, left.cy + radius), _p(right.cx, right.cy + radius)])


def _piston(doc: SvgDocument, box: Box) -> None:
    """A cylinder and its rod: the actuator convention."""
    barrel = Box(x=box.x, y=box.y, width=box.width * 0.62, height=box.height)
    doc.rect(barrel)
    head = Box(x=barrel.x + barrel.width * 0.55, y=box.y + box.height * 0.08,
               width=barrel.width * 0.14, height=box.height * 0.84)
    doc.rect(head)
    doc.polyline([_p(head.right, box.cy), _p(box.right, box.cy)])
    doc.polyline([_p(box.right, box.y + box.height * 0.28),
                  _p(box.right, box.bottom - box.height * 0.28)])


def _nozzle(doc: SvgDocument, box: Box) -> None:
    doc.polygon([_p(box.x, box.y), _p(box.right, box.y + box.height * 0.32),
                 _p(box.right, box.bottom - box.height * 0.32), _p(box.x, box.bottom)])


def _suction_cup(doc: SvgDocument, box: Box) -> None:
    """A bell mouth over a stem. This is the standard section of a suction element."""
    lip = box.bottom
    doc.polyline([_p(box.x, lip), _p(box.x + box.width * 0.2, box.y + box.height * 0.35),
                  _p(box.right - box.width * 0.2, box.y + box.height * 0.35), _p(box.right, lip)])
    doc.polyline([_p(box.x, lip), _p(box.right, lip)])
    stem = box.width * 0.09
    doc.rect(Box(x=box.cx - stem, y=box.y, width=stem * 2, height=box.height * 0.36))


def _fastener(doc: SvgDocument, box: Box) -> None:
    head = Box(x=box.x, y=box.y, width=box.width, height=box.height * 0.22)
    doc.rect(head)
    shank = Box(x=box.cx - box.width * 0.28, y=head.bottom,
                width=box.width * 0.56, height=box.height - head.height)
    doc.rect(shank)
    threads = 5
    step = shank.height / (threads + 1)
    for index in range(1, threads + 1):
        y = shank.y + index * step
        doc.polyline([_p(shank.x, y), _p(shank.right, y + step * 0.35)],
                     stroke_width=doc.profile.thin_stroke)


def _seal(doc: SvgDocument, box: Box) -> None:
    outer = min(box.width, box.height) / 2
    doc.circle(Box(x=box.cx - outer, y=box.cy - outer, width=outer * 2, height=outer * 2))
    inner = outer * 0.58
    doc.circle(Box(x=box.cx - inner, y=box.cy - inner, width=inner * 2, height=inner * 2))


def _filter(doc: SvgDocument, box: Box) -> None:
    doc.rect(box)
    _hatch(doc, box, angle=60.0)


def _heater(doc: SvgDocument, box: Box) -> None:
    doc.rect(box)
    inner = Box(x=box.x + box.width * 0.12, y=box.y + box.height * 0.28,
                width=box.width * 0.76, height=box.height * 0.44)
    zig = 6
    step = inner.width / zig
    points = [_p(inner.x, inner.cy)]
    for index in range(zig):
        points.append(_p(inner.x + (index + 0.5) * step,
                         inner.y if index % 2 == 0 else inner.bottom))
    points.append(_p(inner.right, inner.cy))
    doc.polyline(points)


def _display(doc: SvgDocument, box: Box) -> None:
    screen = Box(x=box.x, y=box.y, width=box.width, height=box.height * 0.76)
    doc.rect(screen, radius=screen.height * 0.06)
    inner = Box(x=screen.x + screen.width * 0.06, y=screen.y + screen.height * 0.1,
                width=screen.width * 0.88, height=screen.height * 0.8)
    doc.rect(inner)
    doc.polyline([_p(box.cx - box.width * 0.12, screen.bottom),
                  _p(box.cx - box.width * 0.16, box.bottom)])
    doc.polyline([_p(box.cx + box.width * 0.12, screen.bottom),
                  _p(box.cx + box.width * 0.16, box.bottom)])
    doc.polyline([_p(box.cx - box.width * 0.22, box.bottom), _p(box.cx + box.width * 0.22,
                                                                box.bottom)])


def _button(doc: SvgDocument, box: Box) -> None:
    """A push button: a round cap standing proud of the surface it is set into.

    It exists because it was missing. "Release button" was classified as an interface, the
    interface symbol is a desktop monitor, and a vacuum gripper patent came back with a computer
    screen on FIG. 1.
    """
    side = min(box.width, box.height)
    seat = Box(x=box.cx - side / 2, y=box.cy - side / 2, width=side, height=side)
    doc.rect(seat, radius=side * 0.18)
    cap = side * 0.62
    doc.circle(Box(x=box.cx - cap / 2, y=box.cy - cap / 2, width=cap, height=cap))
    inner = cap * 0.55
    doc.circle(Box(x=box.cx - inner / 2, y=box.cy - inner / 2, width=inner, height=inner))


def _processor(doc: SvgDocument, box: Box) -> None:
    """An integrated device: a body with pins. The pin count is notation."""
    body = Box(x=box.x + box.width * 0.14, y=box.y + box.height * 0.1,
               width=box.width * 0.72, height=box.height * 0.8)
    doc.rect(body)
    pins = 4
    step = body.height / (pins + 1)
    for index in range(1, pins + 1):
        y = body.y + index * step
        doc.polyline([_p(box.x, y), _p(body.x, y)], stroke_width=doc.profile.thin_stroke)
        doc.polyline([_p(body.right, y), _p(box.right, y)],
                     stroke_width=doc.profile.thin_stroke)
    notch = min(body.width, body.height) * 0.12
    _arc(doc, body.x + body.width / 2 - notch, body.y, notch, notch,
         body.x + body.width / 2 + notch, body.y, sweep=1)


def _memory(doc: SvgDocument, box: Box) -> None:
    doc.cylinder(box)


def _antenna(doc: SvgDocument, box: Box) -> None:
    doc.polyline([_p(box.cx, box.bottom), _p(box.cx, box.y + box.height * 0.3)])
    for index in range(1, 4):
        radius = box.width * 0.16 * index
        _arc(doc, box.cx - radius, box.y + box.height * 0.3, radius, radius,
             box.cx + radius, box.y + box.height * 0.3, sweep=1)
    doc.polyline([_p(box.cx - box.width * 0.18, box.bottom),
                  _p(box.cx + box.width * 0.18, box.bottom)])


def _lens(doc: SvgDocument, box: Box) -> None:
    """Biconvex: two arcs bulging away from a common axis, meeting at the rim."""
    half = box.width * 0.5
    radius = box.height * 0.9
    _arc(doc, box.cx, box.y, radius, radius, box.cx, box.bottom, sweep=1)
    _arc(doc, box.cx, box.y, radius, radius, box.cx, box.bottom, sweep=0)
    doc.polyline([_p(box.cx - half, box.cy), _p(box.cx - half * 0.55, box.cy)],
                 stroke_width=doc.profile.thin_stroke)
    doc.polyline([_p(box.cx + half * 0.55, box.cy), _p(box.cx + half, box.cy)],
                 stroke_width=doc.profile.thin_stroke)


def _opening(doc: SvgDocument, box: Box) -> None:
    dash = f"{doc.profile.stroke * 5:.0f} {doc.profile.stroke * 3:.0f}"
    doc.ellipse(box, stroke_dasharray=dash)


def _connector(doc: SvgDocument, box: Box) -> None:
    body = Box(x=box.x, y=box.y + box.height * 0.2,
               width=box.width * 0.6, height=box.height * 0.6)
    doc.rect(body)
    for offset in (0.3, 0.7):
        y = box.y + box.height * offset
        doc.polyline([_p(body.right, y), _p(box.right, y)])


def _workpiece(doc: SvgDocument, box: Box) -> None:
    doc.rect(box)
    _hatch(doc, box, angle=-45.0)


def _wheel(doc: SvgDocument, box: Box) -> None:
    outer = min(box.width, box.height) / 2
    doc.circle(Box(x=box.cx - outer, y=box.cy - outer, width=outer * 2, height=outer * 2))
    hub = outer * 0.2
    doc.circle(Box(x=box.cx - hub, y=box.cy - hub, width=hub * 2, height=hub * 2))
    for index in range(4):
        angle = math.pi * index / 4
        doc.polyline([_p(box.cx - outer * math.cos(angle), box.cy - outer * math.sin(angle)),
                      _p(box.cx + outer * math.cos(angle), box.cy + outer * math.sin(angle))],
                     stroke_width=doc.profile.thin_stroke)


def _beam(doc: SvgDocument, box: Box) -> None:
    """A structural member seen in section: the I profile, the standard mark for a beam."""
    web = box.height * 0.3
    doc.rect(box)
    doc.polyline([_p(box.x, box.y + web), _p(box.right, box.y + web)],
                 stroke_width=doc.profile.thin_stroke)
    doc.polyline([_p(box.x, box.bottom - web), _p(box.right, box.bottom - web)],
                 stroke_width=doc.profile.thin_stroke)


def _frame(doc: SvgDocument, box: Box) -> None:
    """An open supporting structure: an outline with its corner bracing."""
    doc.rect(box)
    brace = min(box.width, box.height) * 0.3
    for x0, x1 in ((box.x, box.x + brace), (box.right - brace, box.right)):
        doc.polyline([_p(x0, box.y + brace * 0.6), _p(x1, box.y)],
                     stroke_width=doc.profile.thin_stroke)
        doc.polyline([_p(x0, box.bottom - brace * 0.6), _p(x1, box.bottom)],
                     stroke_width=doc.profile.thin_stroke)


def _arm(doc: SvgDocument, box: Box) -> None:
    """A link between two joints: a bar with a pivot at each end."""
    pin = min(box.height * 0.32, box.width * 0.1)
    doc.rect(Box(x=box.x + pin, y=box.y + box.height * 0.28,
                 width=max(1.0, box.width - 2 * pin), height=box.height * 0.44),
             radius=box.height * 0.2)
    for cx in (box.x + pin, box.right - pin):
        doc.circle(Box(x=cx - pin, y=box.cy - pin, width=pin * 2, height=pin * 2))


SYMBOLS: dict[str, Callable[[SvgDocument, Box], None]] = {
    "coil": _coil, "spring": _spring, "motor": _motor, "pump": _pump, "valve": _valve,
    "power": _power, "sensor": _sensor, "magnet": _magnet, "electrode": _plate,
    "plate": _plate, "substrate": _plate, "adhesive": _adhesive, "housing": _housing,
    "chamber": _chamber, "shaft": _shaft, "tube": _tube, "gear": _gear,
    "bearing": _bearing, "roller": _roller, "belt": _belt, "conveyor": _belt,
    "piston": _piston, "actuator": _piston, "nozzle": _nozzle,
    "suction_cup": _suction_cup, "fastener": _fastener, "seal": _seal,
    "filter": _filter, "heater": _heater, "display": _display, "interface": _display,
    "button": _button, "knob": _button,
    "processor": _processor, "controller": _processor, "memory": _memory,
    "storage": _memory, "data_store": _memory, "antenna": _antenna, "network": _antenna,
    "lens": _lens, "opening": _opening, "connector": _connector,
    "workpiece": _workpiece, "wheel": _wheel, "arm": _arm, "beam": _beam,
    "frame": _frame, "gripper": _connector, "cutter": _nozzle,
    "generic_component": _rounded, "boundary": _box,
}


def draw(doc: SvgDocument, visual_class: str, box: Box) -> bool:
    """Draw the symbol for a class. Returns False when the class has none."""
    symbol = SYMBOLS.get(visual_class)
    if symbol is None:
        return False
    symbol(doc, box)
    return True


def has_symbol(visual_class: str) -> bool:
    return visual_class in SYMBOLS
