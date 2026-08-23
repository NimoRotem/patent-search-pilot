"""Reading generated artwork back, so the numerals can be placed by code.

This is the join between the two halves of the reference-guided mode. The image model draws the
parts and is forbidden to write anything; this pass finds where each part landed and hands the
boxes to the same layout code that places numerals on a deterministic sheet. The numerals, the
leaders and the figure caption are therefore still drawn by the renderer, still checked by the
geometry rules, and still bound to the reference registry.

It also does the two rejections that make the mode safe to run at all:

* **any text on the drawing is a defect.** A reference numeral written by an image model is a
  numeral nobody can trust, and it will collide with the real one. A sheet with characters on it
  is thrown away and redrawn.
* **any object that is not on the list is a defect.** That is the failure mode of prompting an
  image model for a technical drawing: it adds the bracket, the bolt, the cable that such a
  device usually has. The patent did not disclose them.

A part that cannot be found is not fatal by itself — a small part may genuinely be hidden behind
another in a perspective view — but its numeral has nowhere to point, so the figure is short of
a required reference and the reference rules say so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from pydantic import BaseModel, Field

from . import prompts
from .numerals import sort_key
from .providers import StructuredOutputError, VisionVerifier
from .schemas import Box, FigureSpec, PatentGraph

# A box smaller than this fraction of the sheet is a misread rather than a part.
MIN_BOX_FRACTION = 0.0008
CONFIDENT = 0.4


class _PartReply(BaseModel):
    name: str = ""
    box: list[float] = Field(default_factory=list)
    encloses_others: bool = False
    confidence: float = 0.5
    note: str = ""


class _LocateReply(BaseModel):
    parts: list[_PartReply] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    unlisted_objects: list[str] = Field(default_factory=list)


@dataclass
class Located:
    boxes: dict[str, Box] = field(default_factory=dict)          # entity id -> box, sheet units
    encloses: set[str] = field(default_factory=set)
    visible_text: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)             # entity ids not found
    ok: bool = False


def _scale(box: Sequence[float], area: Box) -> Optional[Box]:
    """A 0-1000 box from the reader, into the sheet's drawing area."""
    if len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    x0, x1 = sorted((max(0.0, min(1000.0, x0)), max(0.0, min(1000.0, x1))))
    y0, y1 = sorted((max(0.0, min(1000.0, y0)), max(0.0, min(1000.0, y1))))
    width = (x1 - x0) / 1000.0
    height = (y1 - y0) / 1000.0
    if width * height < MIN_BOX_FRACTION:
        return None
    return Box(x=area.x + x0 / 1000.0 * area.width,
               y=area.y + y0 / 1000.0 * area.height,
               width=max(1.0, width * area.width),
               height=max(1.0, height * area.height))


def locate(png: bytes, spec: FigureSpec, graph: PatentGraph, area: Box,
           verifier: VisionVerifier) -> Located:
    """Find each specified part in the generated sheet. Never raises."""
    out = Located()
    wanted: list[tuple[str, str]] = []
    for entity in sorted(spec.entities, key=lambda e: sort_key(e.reference_numeral or "")):
        node = graph.entity(entity.entity_id)
        if node is not None:
            wanted.append((entity.entity_id, node.canonical_name))
    if not wanted or not png:
        return out

    listing = "\n".join(f"  - {name}" for _eid, name in wanted)
    instruction = ("Locate these parts in the drawing:\n" + listing +
                   "\n\nReport every piece of text you can see, and anything drawn that is not "
                   "on this list.")
    try:
        reply = verifier.inspect(
            png, prompts.load("locate_parts_v1"), instruction, _LocateReply,
            prompt_version=prompts.version("locate_parts_v1"), max_tokens=8000)
    except StructuredOutputError:
        return out

    out.visible_text = [str(item)[:60] for item in reply.visible_text if str(item).strip()][:20]
    out.unlisted = [str(item)[:120] for item in reply.unlisted_objects if str(item).strip()][:12]

    by_name = {name.strip().lower(): eid for eid, name in wanted}
    for row in reply.parts:
        entity_id = by_name.get(row.name.strip().lower())
        if entity_id is None:
            # The reader renamed it; fall back to the closest name it was given.
            entity_id = next((eid for eid, name in wanted
                              if name.lower() in row.name.lower()
                              or row.name.lower() in name.lower()), None)
        if entity_id is None or row.confidence < CONFIDENT:
            continue
        box = _scale(row.box, area)
        if box is None:
            continue
        out.boxes[entity_id] = box
        if row.encloses_others:
            out.encloses.add(entity_id)

    out.missing = [eid for eid, _name in wanted if eid not in out.boxes]
    out.ok = bool(out.boxes)
    return out


def defects(located: Located, spec: FigureSpec, graph: PatentGraph) -> list[str]:
    """Reasons this sheet has to be redrawn rather than corrected."""
    reasons: list[str] = []
    if located.visible_text:
        shown = ", ".join(repr(item) for item in located.visible_text[:4])
        reasons.append(
            f"the generated artwork has text on it ({shown}), and every numeral on a sheet has "
            "to be one this compiler placed")
    if located.unlisted:
        shown = "; ".join(located.unlisted[:3])
        reasons.append(
            f"the generated artwork shows parts the patent does not disclose for this figure "
            f"({shown})")
    return reasons
