"""Turning a sketch into line work.

The geometry is the applicant's. Nothing here decides where a line goes; it reads where they drew
one. What it does decide is how to say the same thing cleanly enough to file.

The central choice is **centreline, not outline**. The obvious move is to threshold the image and
run a boundary tracer over it, which is what potrace does and what every logo converter does. On a
line drawing that is wrong: a pencil stroke is a long thin filled region, so its boundary is a
loop *around* the stroke, and stroking that loop draws every line twice, a hair apart. What a
patent drawing wants is the stroke's spine. So the ink is thinned to one pixel wide by Zhang-Suen,
the skeleton is walked as a graph, and each run between two junctions comes out as one polyline.

Then it is straightened, which is most of what makes a traced sketch stop looking traced. A hand
drawn straight line is not straight, and a drawing full of nearly-straight lines reads as a
photograph of a drawing rather than a drawing. Douglas-Peucker removes the tremor, a whole-polyline
flatness test collapses a line that was meant to be one, and anything within a couple of degrees
of horizontal or vertical is snapped to it, because on a hand sketch it was meant to be.

What this does not do is guess. It does not close gaps, join strokes that do not meet, infer a
hidden edge, or decide that two arcs are really one circle. Every one of those is a statement
about the invention that the applicant did not make.
"""
from __future__ import annotations

import io
import math
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..geom import Point

MAX_EDGE = 1400              # traced above this a sketch gains noise, not detail
MIN_COMPONENT_POINTS = 12    # a piece smaller than this is dust or a JPEG artefact
MAX_COMPONENTS = 40
MIN_POLYLINE_PIXELS = 5
SPUR_FRACTION = 0.020        # a branch shorter than this, with a free end, is thinning hair
RDP_FRACTION = 0.0016        # simplification tolerance, as a fraction of the image diagonal
STRAIGHT_FRACTION = 0.012    # flatter than this over its own length was meant to be straight
SNAP_DEGREES = 2.5           # within this of horizontal or vertical, it was meant to be
LOCAL_WINDOW_DIVISOR = 20
LOCAL_OFFSET = 10.0
MAX_THINNING_PASSES = 200


class TraceError(RuntimeError):
    """The sketch could not be traced. Says which step failed."""


@dataclass
class Traced:
    """One sketch, as line work, in millimetres."""
    components: list[list[list[Point]]] = field(default_factory=list)
    width_mm: float = 0.0
    height_mm: float = 0.0
    ink_fraction: float = 0.0
    source_pixels: tuple[int, int] = (0, 0)

    def polylines(self) -> list[list[Point]]:
        return [poly for component in self.components for poly in component]

    def stats(self) -> dict[str, Any]:
        return {
            "components": len(self.components),
            "polylines": sum(len(c) for c in self.components),
            "points": sum(len(p) for c in self.components for p in c),
            "size_mm": [round(self.width_mm, 1), round(self.height_mm, 1)],
            "ink_fraction": round(self.ink_fraction, 4),
            "source_pixels": list(self.source_pixels),
        }

    def component_bounds(self, index: int) -> tuple[float, float, float, float]:
        points = [p for poly in self.components[index] for p in poly]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))


def _numpy():
    try:
        import numpy
    except Exception as exc:  # pragma: no cover - deployment dependent
        raise TraceError(f"numpy is not installed, so a sketch cannot be traced: {exc}") from exc
    return numpy


# ---------------------------------------------------------------------------------- loading


def load_image(filename: str, blob: bytes):
    """A sketch as a greyscale image, whatever container it arrived in."""
    from PIL import Image, ImageOps

    if blob[:5] == b"%PDF-":
        blob = _pdf_first_page(blob)
    try:
        image = Image.open(io.BytesIO(blob))
        # A photograph of a sketch is usually rotated by its EXIF tag and by nothing else.
        image = ImageOps.exif_transpose(image).convert("L")
    except Exception as exc:
        raise TraceError(f"{filename}: this image could not be opened ({exc}).") from exc

    longest = max(image.size)
    if longest > MAX_EDGE:
        scale = MAX_EDGE / longest
        image = image.resize((max(1, int(image.size[0] * scale)),
                              max(1, int(image.size[1] * scale))), Image.LANCZOS)
    return image


def _pdf_first_page(blob: bytes) -> bytes:
    """A PDF page rendered to a bitmap, because a scanned sketch usually arrives as one."""
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / "in.pdf"
        source.write_bytes(blob)
        try:
            subprocess.run(["pdftoppm", "-r", "200", "-f", "1", "-l", "1", "-png",
                            str(source), str(Path(work) / "page")],
                           check=True, capture_output=True, timeout=90)
        except FileNotFoundError as exc:
            raise TraceError("pdftoppm is not installed, so a PDF sketch cannot be rasterised. "
                             "Install poppler-utils, or upload a PNG.") from exc
        except subprocess.CalledProcessError as exc:
            raise TraceError("this PDF could not be rendered: "
                             + exc.stderr.decode("utf-8", "replace")[:200]) from exc
        pages = sorted(Path(work).glob("page*.png"))
        if not pages:
            raise TraceError("this PDF produced no page image.")
        return pages[0].read_bytes()


# ------------------------------------------------------------------------------- binarising


def binarise(image, *, invert: Optional[bool] = None):
    """Ink as True, on a local threshold so a photograph of paper still works.

    A single global threshold fails on the commonest input there is: a phone photograph of a
    sketch with a shadow across one corner. Half the drawing falls on the wrong side of any one
    number. A local mean handles that, and the offset stops flat paper from turning into noise.
    """
    numpy = _numpy()
    array = numpy.asarray(image, dtype=numpy.float32)
    if array.ndim != 2 or min(array.shape) < 8:
        raise TraceError("this image is too small to trace.")

    window = max(15, (min(array.shape) // LOCAL_WINDOW_DIVISOR) | 1)
    local = _box_blur(array, window)
    ink = array < (local - LOCAL_OFFSET)

    if invert is None:
        # A drawing is mostly paper. If "ink" came out as most of the image, it was white on
        # black and the sense is the other way round.
        invert = bool(ink.mean() > 0.5)
    if invert:
        ink = array > (local + LOCAL_OFFSET)
    return ink


def _box_blur(array, window: int):
    """A mean over a window, in constant time per pixel, from an integral image."""
    numpy = _numpy()
    pad = window // 2
    padded = numpy.pad(array, ((pad, pad + 1), (pad, pad + 1)), mode="edge")
    integral = numpy.zeros((padded.shape[0] + 1, padded.shape[1] + 1), dtype=numpy.float64)
    integral[1:, 1:] = padded.cumsum(0).cumsum(1)
    height, width = array.shape
    total = (integral[window:window + height, window:window + width]
             - integral[0:height, window:window + width]
             - integral[window:window + height, 0:width]
             + integral[0:height, 0:width])
    return (total / float(window * window)).astype(numpy.float32)


# --------------------------------------------------------------------------------- thinning


def thin(ink):
    """Zhang-Suen: erode the ink to a one-pixel spine, keeping it connected.

    This is the step that makes the difference between a drawing and a rubbing of one. Without
    it every stroke comes back as the loop around itself, and stroking that loop draws each of
    the applicant's lines twice.
    """
    numpy = _numpy()
    image = numpy.pad(ink.astype(numpy.uint8), 1)

    for _ in range(MAX_THINNING_PASSES):
        removed = 0
        for step in (0, 1):
            north = image[0:-2, 1:-1]
            north_east = image[0:-2, 2:]
            east = image[1:-1, 2:]
            south_east = image[2:, 2:]
            south = image[2:, 1:-1]
            south_west = image[2:, 0:-2]
            west = image[1:-1, 0:-2]
            north_west = image[0:-2, 0:-2]
            centre = image[1:-1, 1:-1]

            ring = (north, north_east, east, south_east, south, south_west, west, north_west,
                    north)
            neighbours = (north + north_east + east + south_east + south + south_west + west
                          + north_west)
            transitions = numpy.zeros(centre.shape, dtype=numpy.uint8)
            for i in range(8):
                transitions += ((ring[i] == 0) & (ring[i + 1] == 1)).astype(numpy.uint8)

            common = ((centre == 1) & (neighbours >= 2) & (neighbours <= 6)
                      & (transitions == 1))
            if step == 0:
                condition = (common & (north * east * south == 0)
                             & (east * south * west == 0))
            else:
                condition = (common & (north * east * west == 0)
                             & (north * south * west == 0))

            count = int(condition.sum())
            if count:
                # ``centre`` is a view, so this writes through into ``image``. Every condition
                # was materialised before this line, which is what Zhang-Suen requires.
                centre[condition] = 0
                removed += count
        if not removed:
            break
    return image[1:-1, 1:-1].astype(bool)


# ---------------------------------------------------------------------------------- walking

_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def walk(skeleton) -> tuple[list[list[tuple[int, int]]], dict[tuple[int, int], int]]:
    """The skeleton as polylines: every run between two junctions or ends, plus closed loops.

    Walked as a graph rather than scanned as an image, because a drawing is a set of strokes and
    a stroke is a path. A junction is where the applicant's lines meet and it is where one
    polyline should stop and the next begin.

    A junction is a CLUSTER of pixels, not a pixel. Thinning leaves several adjacent pixels with
    three or more neighbours wherever lines meet, and on a wobbly hand drawn line it leaves them
    at every staircase too. Treating each one as its own junction turned a sketch of six shapes
    into nine hundred and eighty fragments, most of them two pixels long and lying between two
    pixels of the same junction. So adjacent branch pixels are collapsed into one node, every
    path between two nodes is emitted once, and paths that never leave a node are not paths.
    """
    numpy = _numpy()
    rows, cols = numpy.nonzero(skeleton)
    pixels = set(zip(rows.tolist(), cols.tolist()))
    if not pixels:
        return [], {}

    def neighbours(pixel: tuple[int, int]) -> list[tuple[int, int]]:
        y, x = pixel
        return [(y + dy, x + dx) for dy, dx in _OFFSETS if (y + dy, x + dx) in pixels]

    degree = {pixel: len(neighbours(pixel)) for pixel in pixels}
    nodes = {pixel for pixel, count in degree.items() if count != 2}

    # Collapse touching branch pixels into one node, and stand each cluster at its middle so two
    # paths meeting there share an endpoint exactly.
    cluster_of: dict[tuple[int, int], int] = {}
    representative: list[tuple[int, int]] = []
    for pixel in sorted(nodes):
        if pixel in cluster_of:
            continue
        identifier = len(representative)
        members = [pixel]
        cluster_of[pixel] = identifier
        stack = [pixel]
        while stack:
            here = stack.pop()
            for other in neighbours(here):
                if other in nodes and other not in cluster_of:
                    cluster_of[other] = identifier
                    members.append(other)
                    stack.append(other)
        mid_y = sum(m[0] for m in members) / len(members)
        mid_x = sum(m[1] for m in members) / len(members)
        representative.append(
            min(members, key=lambda m: (m[0] - mid_y) ** 2 + (m[1] - mid_x) ** 2))

    used: set[frozenset] = set()
    paths: list[list[tuple[int, int]]] = []

    for pixel in sorted(nodes):
        home = cluster_of[pixel]
        for neighbour in neighbours(pixel):
            if neighbour in nodes and cluster_of[neighbour] == home:
                continue                      # still inside the same junction
            edge = frozenset((pixel, neighbour))
            if edge in used:
                continue
            used.add(edge)
            path = [pixel, neighbour]
            previous, current = pixel, neighbour
            while current not in nodes:
                ahead = [p for p in neighbours(current) if p != previous]
                if not ahead:
                    break
                step = ahead[0]
                onward = frozenset((current, step))
                if onward in used:
                    break
                used.add(onward)
                path.append(step)
                previous, current = current, step
            path[0] = representative[home]
            if path[-1] in nodes:
                path[-1] = representative[cluster_of[path[-1]]]
            paths.append(path)

    # Anything left is a closed loop: every pixel on it has exactly two neighbours, so it has no
    # junction to start from.
    for pixel in sorted(pixels):
        if pixel in nodes:
            continue
        if any(frozenset((pixel, n)) in used for n in neighbours(pixel)):
            continue
        start = pixel
        path = [start]
        previous, current = None, neighbours(start)[0]
        used.add(frozenset((start, current)))
        while current != start:
            path.append(current)
            ahead = [p for p in neighbours(current) if p != previous]
            if not ahead:
                break
            step = ahead[0]
            used.add(frozenset((current, step)))
            previous, current = current, step
        path.append(start)
        paths.append(path)

    return paths, degree


def prune(paths: Sequence[Sequence[tuple[int, int]]],
          min_pixels: int) -> list[list[tuple[int, int]]]:
    """Drop the hairs thinning leaves on a wobbly stroke, and nothing else.

    Every bump on a hand drawn line becomes a little branch when the ink is thinned, and a sketch
    that traced to six shapes came back as nine hundred fragments because of them. A spur has a
    free end, so removing it cannot disconnect the drawing; a short run between two junctions has
    no free end and is load bearing. Removing one spur can expose another, so it repeats.
    """
    from collections import Counter

    kept = [list(path) for path in paths]
    while True:
        ends: Counter = Counter()
        for path in kept:
            ends[path[0]] += 1
            if path[-1] != path[0]:
                ends[path[-1]] += 1
        drop = {index for index, path in enumerate(kept)
                if len(path) < min_pixels
                and (ends[path[0]] <= 1 or ends[path[-1]] <= 1)}
        if not drop:
            return kept
        kept = [path for index, path in enumerate(kept) if index not in drop]


def stitch(paths: Sequence[Sequence[tuple[int, int]]]) -> list[list[tuple[int, int]]]:
    """Rejoin the strokes that pruning turned back into one.

    Thinning makes a junction a small cluster of pixels, so a stroke passing through one is
    chopped into pieces. Once the spurs are gone, a pixel where exactly two path ends meet is not
    a junction at all: it is the middle of a line, and the two pieces are one stroke. Rejoining
    them matters for more than tidiness, because simplification can only straighten a line it can
    see the whole of.
    """
    from collections import defaultdict

    remaining = [list(path) for path in paths]
    at_end: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, path in enumerate(remaining):
        if path[0] == path[-1]:
            continue
        at_end[path[0]].append(index)
        at_end[path[-1]].append(index)

    merged_into = list(range(len(remaining)))
    alive = [True] * len(remaining)

    def root(index: int) -> int:
        while merged_into[index] != index:
            merged_into[index] = merged_into[merged_into[index]]
            index = merged_into[index]
        return index

    for pixel, owners in at_end.items():
        live = [root(i) for i in owners]
        live = sorted({i for i in live if alive[i]})
        if len(live) != 2:
            continue
        first, second = remaining[live[0]], remaining[live[1]]
        if first[0] == pixel:
            first.reverse()
        if second[-1] == pixel:
            second.reverse()
        if first[-1] != pixel or second[0] != pixel:
            continue
        remaining[live[0]] = first + second[1:]
        alive[live[1]] = False
        merged_into[live[1]] = live[0]

    return [path for index, path in enumerate(remaining) if alive[index]]


# ----------------------------------------------------------------------------- straightening


def simplify(points: Sequence[Point], tolerance: float) -> list[Point]:
    """Douglas-Peucker, iteratively so a long stroke cannot blow the stack."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        worst, index = -1.0, -1
        for i in range(start + 1, end):
            px, py = points[i]
            if length < 1e-9:
                distance = math.hypot(px - ax, py - ay)
            else:
                distance = abs(dy * px - dx * py + bx * ay - by * ax) / length
            if distance > worst:
                worst, index = distance, i
        if worst > tolerance and index > 0:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [points[i] for i, flag in enumerate(keep) if flag]


def collapse_runs(points: Sequence[Point], tolerance: float, noise: float) -> list[Point]:
    """Replace every genuinely straight run with its two ends, and leave curves alone.

    Raising the simplification tolerance until the tremor goes would also flatten the curves, so a
    traced circle comes back as a decagon. The way to tell them apart is the SIGN of the
    deviation, not its size: a hand's wobble crosses the chord repeatedly, an arc bows to one side
    and stays there.

    The test has to be made on the whole candidate run rather than on each extension of it. Asked
    after every single step it can never see a crossing, because two points cannot alternate, and
    then every run stops at its first wobble. So the run is grown as far as the loose tolerance
    allows, and only then asked whether what it grew across was a tremor or a bow; if it was a
    bow, it is regrown under the tight one.
    """
    if len(points) < 3:
        return list(points)

    def deviations(first: int, last: int) -> list[float]:
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        chord = math.hypot(dx, dy)
        if chord < 1e-9 or last <= first + 1:
            return []
        return [(dy * (px - ax) - dx * (py - ay)) / chord
                for px, py in points[first + 1:last]]

    def extend(first: int, limit: float) -> int:
        best = first + 1
        for last in range(first + 2, len(points)):
            values = deviations(first, last)
            if values and max(abs(v) for v in values) > limit:
                break
            best = last
        return best

    out = [points[0]]
    cursor = 0
    while cursor < len(points) - 1:
        far = extend(cursor, tolerance)
        values = deviations(cursor, far)
        if values and max(abs(v) for v in values) > noise:
            crossings = sum(1 for i in range(len(values) - 1)
                            if (values[i] > 0) != (values[i + 1] > 0))
            if crossings < 2:
                far = max(cursor + 1, extend(cursor, noise))
        out.append(points[far])
        cursor = far
    return out


def straighten(points: Sequence[Point], tolerance: float) -> list[Point]:
    """A stroke that was meant to be one line becomes one line.

    Two moves, both conservative. A polyline whose every point lies within a small fraction of
    its own length from the chord was drawn as a single line, so it becomes one. And a segment
    within a couple of degrees of horizontal or vertical is snapped, because on a hand sketch it
    was meant to be and leaving it at 1.4 degrees is what makes a traced drawing look traced.
    """
    if len(points) < 2:
        return list(points)

    start, end = points[0], points[-1]
    chord = math.dist(start, end)
    if chord > 1e-9:
        dx, dy = end[0] - start[0], end[1] - start[1]
        worst = max(
            abs(dy * px - dx * py + end[0] * start[1] - end[1] * start[0]) / chord
            for px, py in points)
        if worst <= max(tolerance, chord * STRAIGHT_FRACTION):
            points = [start, end]

    out = [points[0]]
    for point in points[1:]:
        previous = out[-1]
        dx, dy = point[0] - previous[0], point[1] - previous[1]
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            continue
        angle = math.degrees(math.atan2(dy, dx))
        if min(abs(angle), abs(180 - abs(angle))) <= SNAP_DEGREES:
            point = (point[0], previous[1])
        elif abs(abs(angle) - 90.0) <= SNAP_DEGREES:
            point = (previous[0], point[1])
        out.append(point)
    return out if len(out) >= 2 else list(points)


# ------------------------------------------------------------------------------- components


def group(paths: Sequence[Sequence[tuple[int, int]]]) -> list[list[int]]:
    """Strokes that touch are one piece of the drawing, largest piece first.

    A reference numeral points at a part, and on a sketch a part is usually a shape drawn without
    lifting the pen. Grouping on shared pixels recovers those without deciding anything about
    what they are.
    """
    parent = list(range(len(paths)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    owner: dict[tuple[int, int], int] = {}
    for index, path in enumerate(paths):
        for pixel in (path[0], path[-1]):
            held = owner.get(pixel)
            if held is None:
                owner[pixel] = index
            else:
                a, b = find(held), find(index)
                if a != b:
                    parent[a] = b

    groups: dict[int, list[int]] = {}
    for index in range(len(paths)):
        groups.setdefault(find(index), []).append(index)
    ordered = sorted(groups.values(),
                     key=lambda members: -sum(len(paths[m]) for m in members))
    return ordered


# ------------------------------------------------------------------------------ entry point


def trace(filename: str, blob: bytes, *, target_size_mm: float = 150.0,
          invert: Optional[bool] = None) -> Traced:
    """A sketch, as clean line work in millimetres."""
    image = load_image(filename, blob)
    ink = binarise(image, invert=invert)
    fraction = float(ink.mean())
    if fraction < 0.0004:
        raise TraceError(f"{filename}: almost no ink was found. If the drawing is faint, "
                         "photograph it in even light or raise the contrast first.")
    if fraction > 0.45:
        raise TraceError(f"{filename}: {fraction * 100:.0f}% of this image reads as ink, which "
                         "is a photograph or a filled drawing rather than line work.")

    skeleton = thin(ink)
    paths, _degree = walk(skeleton)

    width, height = image.size
    diagonal = math.hypot(width, height)
    tolerance = diagonal * RDP_FRACTION
    scale = target_size_mm / max(width, height)

    paths = prune(paths, max(MIN_POLYLINE_PIXELS, int(diagonal * SPUR_FRACTION)))
    paths = stitch(paths)
    if not paths:
        raise TraceError(f"{filename}: no strokes survived once the ink was thinned and the "
                         "hairs were removed. The lines may be too faint or too broken.")

    # One entry per path, in the same order, so a group's indices still mean something. Filtering
    # this list instead of blanking it is what shifted every index the first time.
    prepared: list[Optional[list[Point]]] = []
    for path in paths:
        # Pixels are (row, column); a drawing is (x, y), and y already points down on both.
        points = [(float(x), float(y)) for y, x in path]
        # Collapse BEFORE simplifying. The sign test needs the dense pixel path to see the
        # tremor crossing the chord; after Douglas-Peucker only the extremes survive and a
        # wobbly line looks as one-sided as an arc.
        points = collapse_runs(points, tolerance * 2.4, tolerance * 0.7)
        points = straighten(simplify(points, tolerance), tolerance)
        prepared.append(points if len(points) >= 2 else None)

    components: list[list[list[Point]]] = []
    for member in group(paths):
        polylines = [prepared[index] for index in member if prepared[index] is not None]
        drawn = sum(_length(poly) for poly in polylines)
        if not polylines or drawn < diagonal * 0.01:
            continue
        components.append([[(x * scale, y * scale) for x, y in poly] for poly in polylines])
        if len(components) >= MAX_COMPONENTS:
            break

    if not components:
        raise TraceError(f"{filename}: every stroke was too small to keep.")

    return Traced(components=components, width_mm=width * scale, height_mm=height * scale,
                  ink_fraction=fraction, source_pixels=(width, height))


def _length(points: Sequence[Point]) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def probe(filename: str, blob: bytes) -> dict[str, Any]:
    """What a sketch would trace to, for the upload check."""
    return trace(filename, blob).stats()
