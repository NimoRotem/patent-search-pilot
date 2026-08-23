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

MAX_DRAW_ATTEMPTS = 2


@dataclass
class Drawn:
    scene: Optional[LayoutScene] = None
    svg: str = ""
    artwork: bytes = b""
    references: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failed: str = ""

    @property
    def ok(self) -> bool:
        return self.scene is not None and bool(self.svg)


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
            out.notes.append(
                f"attempt {attempt + 1} was redrawn: " + "; ".join(problems))
            if attempt + 1 < MAX_DRAW_ATTEMPTS:
                continue
            out.failed = problems[0]
            return out
        out.notes.extend(imagegrounding.concerns(located, graph))
        if not located.ok:
            out.notes.append(
                f"attempt {attempt + 1} was redrawn: none of the parts could be found in it")
            if attempt + 1 < MAX_DRAW_ATTEMPTS:
                continue
            out.failed = "the parts could not be found in the generated artwork"
            return out

        scene = traced_layout.build(spec, graph, located, profile, artwork_box=box,
                                    sheet_number=sheet_number, sheet_total=sheet_total)
        out.scene = scene
        out.svg = render_svg(scene, profile, generated.png)
        if located.missing:
            names = []
            for entity_id in located.missing:
                entity = graph.entity(entity_id)
                names.append(entity.reference_numeral or entity_id if entity else entity_id)
            out.notes.append(
                f"{len(located.missing)} part(s) specified for this figure could not be found in "
                f"the artwork ({', '.join(str(n) for n in names)}), so they carry no numeral")
        area = box
        return out

    out.failed = out.failed or "the artwork could not be drawn"
    return out
