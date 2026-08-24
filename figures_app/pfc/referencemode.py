"""The reference-guided mode, end to end for one figure.

    neighbouring patents -> their sheets as visual reference
        -> artwork generated for OUR arrangement, with no text on it
        -> the parts located in that artwork
        -> our numerals composited on top by the ordinary renderer
        -> the ordinary validators

The value is that the drawing looks like a patent drawing. The safety is that everything which
carries meaning — which parts, which numerals, which relationships — comes from the FigureSpec
and never from the image model, and that text on the generated artwork is an unconditional
rejection: a numeral this compiler did not place is one nobody can trace to the description.

Shapes a reader cannot match to a listed part are reported for a human rather than rejected; see
``imagegrounding.defects`` for why that line moved.

A rejection is retried a bounded number of times and then the figure falls back to the
deterministic renderer, which always produces something correct even when it produces something
plain.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional, Sequence

from . import generate, imagegrounding
from .layout import traced as traced_layout
from .neighbours import Neighbourhood, Sheet, spread
from .profiles import DrawingProfile
from .providers import VisionVerifier
from .render import render_svg
from .schemas import FigureSpec, LayoutScene, PatentGraph

MAX_DRAW_ATTEMPTS = 3


@dataclass
class Drawn:
    scene: Optional[LayoutScene] = None
    svg: str = ""
    artwork: bytes = b""
    references: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failed: str = ""
    # The image model is out of quota. Every later figure in this job will find the same, so the
    # caller stops asking rather than spending the same seconds on each of them.
    exhausted: bool = False

    @property
    def ok(self) -> bool:
        return self.scene is not None and bool(self.svg)


def _missing_names(located, graph: PatentGraph) -> list[str]:
    """The numerals of the parts this figure needs and the reader could not find."""
    names: list[str] = []
    for entity_id in located.missing:
        entity = graph.entity(entity_id)
        names.append(str(entity.reference_numeral or entity_id) if entity else str(entity_id))
    return names


def _image_size(png: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            return image.size
    except Exception:
        return (0, 0)


def draw_figure(spec: FigureSpec, graph: PatentGraph, profile: DrawingProfile,
                neighbourhood: Neighbourhood, verifier: Optional[VisionVerifier],
                *, earlier: Sequence[bytes] = (), sheet_number: int = 1,
                sheet_total: int = 1) -> Drawn:
    """Generate, ground and composite one figure. Never raises."""
    out = Drawn()
    if verifier is None:
        out.failed = ("the reference-guided mode needs a vision model to find the parts in the "
                      "artwork it generates")
        return out

    references: list[Sheet] = spread(neighbourhood)
    if not references and not earlier:
        out.failed = "no neighbouring patent with a drawing was found to use as a reference"
        return out

    area = traced_layout.fit_artwork(profile, (4, 3))
    for attempt in range(MAX_DRAW_ATTEMPTS):
        try:
            generated = generate.draw(spec, graph, references, earlier=earlier)
        except generate.GenerationExhausted as exc:
            out.exhausted = True
            out.failed = str(exc)
            return out
        except generate.GenerationUnavailable as exc:
            out.failed = f"no image model is available: {exc}"
            return out
        if not generated.ok:
            out.failed = generated.error or "the image model could not draw this figure"
            continue

        out.artwork = generated.png
        out.references = generated.references
        box = traced_layout.fit_artwork(profile, _image_size(generated.png))
        located = imagegrounding.locate(generated.png, spec, graph, box, verifier)
        problems = imagegrounding.defects(located, spec, graph)
        if problems:
            if attempt + 1 < MAX_DRAW_ATTEMPTS:
                out.notes.append(
                    f"attempt {attempt + 1} was redrawn: " + "; ".join(problems))
                continue
            out.notes.append(f"attempt {attempt + 1} was the last: " + "; ".join(problems))
            out.failed = problems[0]
            return out
        out.notes.extend(imagegrounding.concerns(located, graph))
        short = _missing_names(located, graph)
        if short:
            # Every part this figure is specified to show has to be findable in the drawing.
            # A sheet short of one is not a sheet with a small gap: REF004 says the numeral
            # belongs here and was not printed, SEM002 says the component is absent, and neither
            # can be repaired by moving a label. Measured on US-2024/0246200-A1, three of four
            # figures blocked exactly this way while the deterministic renderer, which draws
            # every specified part by construction, would have produced all three.
            listed = ", ".join(short)
            if attempt + 1 < MAX_DRAW_ATTEMPTS:
                out.notes.append(
                    f"attempt {attempt + 1} was redrawn: {len(short)} part(s) this figure has "
                    f"to show could not be found in it ({listed})")
                continue
            out.notes.append(
                f"attempt {attempt + 1} was the last: {len(short)} part(s) this figure has to "
                f"show could not be found in it ({listed})")
            out.failed = (f"after {MAX_DRAW_ATTEMPTS} attempts the generated artwork still did "
                          f"not show {listed}, and a figure that omits a part it is specified "
                          f"to show cannot be corrected by moving a numeral")
            return out

        scene = traced_layout.build(spec, graph, located, profile, artwork_box=box,
                                    sheet_number=sheet_number, sheet_total=sheet_total)
        out.scene = scene
        out.svg = render_svg(scene, profile, generated.png)
        area = box
        return out

    out.failed = out.failed or "the artwork could not be drawn"
    return out
