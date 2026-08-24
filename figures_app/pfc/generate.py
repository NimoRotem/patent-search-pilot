"""Reference-guided artwork: line art conditioned on real patent drawings.

The deterministic renderer draws exactly what the text says and nothing else, which is correct
and which produces schematics. This mode produces drawings that look like the drawings in the
document they will be filed alongside, by showing an image model what a real figure of a
comparable device looks like and asking it for OUR arrangement in that idiom.

**The artwork is generated. The meaning is not.** Three properties keep this from being the
"prompt an image model with the whole patent" approach the compiler exists to avoid:

* the prompt is built from the FigureSpec — the parts this figure is grounded to show and the
  relationships the description states — so it cannot introduce a component the patent does not
  disclose;
* the model is forbidden to write any text, and a sheet with text on it is rejected, because a
  reference numeral drawn by an image model is a numeral nobody can trust. Ours are composited
  afterwards, by the same code that places them on a deterministic sheet;
* every generated sheet records the references it saw, so a drawing that came out looking like
  somebody's prior art can be traced to the reason.

**Consistency across a figure set** comes from chaining: after the first figure is generated, it
becomes a reference for the next one. That is the same mechanism that makes an edit an edit, and
it is what stops the housing in FIG. 1 turning into a different housing in FIG. 2.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .neighbours import Sheet
from .schemas import FigureSpec, PatentGraph

MAX_PNG_BYTES = 12 * 1024 * 1024
ATTEMPTS = 3
# How many of OUR earlier figures ride along as consistency references. Every figure was being
# appended, so a twelve-figure patent reached figure twelve carrying eleven of its own sheets plus
# three from the neighbours: fourteen images for one drawing, at a cost and a latency that grow
# with the patent, and with the instruction still speaking of "the last reference image" as though
# there were one. The two most recent carry the shape; the rest carry the bill.
MAX_EARLIER_FIGURES = 2

SYSTEM = """You draw patent figures. Black line art on a white background, uniform line weight,
no shading, no hatching except where asked, no colour, no perspective tricks, no photorealism.
The style of a published patent drawing.

You will be shown one or more REFERENCE drawings from other patents. They are there to show you
the drawing conventions and the level of abstraction for this kind of device. You are not
copying them. The arrangement you draw is the one described in the instruction, which is a
different invention.

ABSOLUTE RULES:
1. Draw NO text of any kind. No labels, no reference numerals, no captions, no dimensions, no
   figure number, no arrows carrying words. Not one character. The numerals are added afterwards
   by a different process and anything you write will collide with them.
2. Draw only the parts the instruction names. Do not add a component because the reference has
   one or because such a device usually has one.
3. Simplify. Fewer lines than the reference, not more. Where the instruction does not state a
   detail, leave it out rather than inventing it.
4. Leave clear white space around and between the parts. Reference numerals and their leader
   lines have to be placed into that space afterwards.
"""


@dataclass
class Generated:
    png: bytes = b""
    prompt: str = ""
    references: list[str] = field(default_factory=list)
    attempts: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.png)


class GenerationUnavailable(RuntimeError):
    """No image model on this host."""


class GenerationExhausted(RuntimeError):
    """The image model is out of quota. Not now, rather than not ever, and not this drawing.

    Distinct because the answer is different in every direction: retrying inside the figure is
    pointless, retrying the NEXT figure is worse than pointless because it spends the same
    seconds finding out again, and the sentence a human needs says the box ran out of quota
    rather than that the drawing could not be made. Seen on a real run, where all four figures
    each burned three attempts and each wrote a raw 429 payload into the report.
    """


# What a quota or rate-limit refusal looks like across the transports this can arrive on.
_EXHAUSTED = ("resource_exhausted", "429", "quota", "rate limit", "rate_limit",
              "too many requests")


def _is_exhausted(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _EXHAUSTED)


def _client():
    from . import pilot

    try:
        draft_figures = pilot.module("draft_figures")
    except Exception as exc:
        raise GenerationUnavailable(str(exc)) from exc
    return draft_figures


_SIZE_WORDS = {"small": "small", "medium": "", "large": "large"}


def _appearance_of(node) -> str:
    """The simple recognisable element already settled for this part, in words.

    The compiler decides once per part what kind of thing it is drawing, and the cross-figure
    rules hold every figure to that decision. Until this was passed through, the image model
    never saw it: it was told "release button" with no further guidance and drew a desktop
    computer monitor on a vacuum gripper. Saying which element to draw is what makes the same
    part the same part in FIG. 1 and FIG. 4.
    """
    appearance = getattr(node, "appearance", None)
    symbol = getattr(appearance, "symbol", "") or ""
    if not symbol or symbol == "generic_component":
        # Nothing was settled, and inventing one here would defeat the point of settling it.
        return ""
    kind = symbol.replace("_", " ")
    size = _SIZE_WORDS.get(getattr(appearance, "size", "medium") or "medium", "")
    orientation = getattr(appearance, "orientation", "") or ""
    words = [word for word in (size, orientation if orientation != "horizontal" else "", kind)
             if word]
    return f", drawn as a simple {' '.join(words)}"


def _describe_parts(spec: FigureSpec, graph: PatentGraph) -> tuple[list[str], list[str]]:
    """The parts and the arrangement, in the drafter's own words."""
    from .numerals import sort_key

    parts: list[str] = []
    for entity in sorted(spec.entities, key=lambda e: sort_key(e.reference_numeral or "")):
        node = graph.entity(entity.entity_id)
        if node is None:
            continue
        shape = ""
        if node.shape_hint_grounded and node.shape_hint:
            shape = f", which the description calls {node.shape_hint}"
        role = " (the enclosing part)" if entity.role == "boundary" else ""
        parts.append(f"{node.canonical_name}{shape}{_appearance_of(node)}{role}")

    relations = {relation.id: relation for relation in graph.relations}
    human = {
        "contains": "contains", "inside": "is inside", "surrounds": "surrounds",
        "attached_to": "is attached to", "coupled_to": "is coupled to",
        "connected_to": "is connected to", "mounted_on": "is mounted on",
        "supports": "supports", "passes_through": "passes through",
        "adjacent_to": "is next to", "above": "is above", "below": "is below",
        "between": "is between", "fluidly_connected_to": "is connected by a duct to",
        "electrically_connected_to": "is wired to",
    }
    arrangement: list[str] = []
    for spec_relation in spec.relations:
        relation = relations.get(spec_relation.relation_id)
        if relation is None:
            continue
        verb = human.get(relation.predicate)
        if not verb:
            continue
        left = graph.entity(relation.subject)
        right = graph.entity(relation.object)
        if left is None or right is None:
            continue
        arrangement.append(f"the {left.canonical_name} {verb} the {right.canonical_name}")
    return parts, arrangement


VIEW_WORDS = {
    "perspective": "a perspective view", "plan": "a top view looking down",
    "elevation": "a side view", "section": "a cross-section through the device",
    "exploded": "an exploded view with the parts separated along an axis",
    "detail": "an enlarged detail view", "schematic": "a simple schematic arrangement",
    "flow": "a flow diagram", "other": "a simple view",
}


def build_prompt(spec: FigureSpec, graph: PatentGraph, *, reference_count: int,
                 earlier_figures: Sequence[str] = ()) -> str:
    parts, arrangement = _describe_parts(spec, graph)
    view = VIEW_WORDS.get(spec.view_type, "a simple view")

    lines = [f"Draw {view} of the following, in the style of the reference drawing"
             f"{'s' if reference_count != 1 else ''}."]
    if spec.source_description:
        lines.append(f"\nThe patent describes this figure as: {spec.source_description}")
    if parts:
        lines.append("\nDraw exactly these parts and no others:")
        lines.extend(f"  - {part}" for part in parts)
    if arrangement:
        lines.append("\nArranged as the description states:")
        lines.extend(f"  - {item}" for item in arrangement)
    if spec.steps:
        lines.append("\nThe steps, as a flow of plain boxes joined by arrows, in this order:")
        lines.extend(f"  - {step.text}" for step in spec.steps)
    if earlier_figures:
        count = len(earlier_figures)
        which = ("The last reference image is a figure YOU drew for this same patent."
                 if count == 1 else
                 f"The last {count} reference images are figures YOU drew for this same patent.")
        lines.append(
            f"\n{which} Any part that appears in one of them and in this figure must be drawn the "
            "same way here as it was there: same shape, same proportions, same level of detail. "
            "Only the viewpoint may change.")
    lines.append(
        "\nRemember: no text, no numbers, no labels anywhere on the drawing. Leave white space "
        "around each part for numerals to be added later.")
    return "\n".join(lines)


def _generate_once(prompt: str, references: Sequence[bytes], model: str,
                   temperature: float) -> bytes:
    from google.genai.types import GenerateContentConfig, Part

    draft_figures = _client()
    contents: list = [Part.from_bytes(data=blob, mime_type="image/png")
                      for blob in references if blob]
    contents.append(SYSTEM + "\n\n" + prompt)
    response = draft_figures._image_client().models.generate_content(
        model=model, contents=contents,
        config=GenerateContentConfig(response_modalities=["TEXT", "IMAGE"],
                                     temperature=temperature))
    try:
        parts = response.candidates[0].content.parts
    except Exception:
        raise RuntimeError("the image model returned nothing")
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob and blob.data:
            if len(blob.data) > MAX_PNG_BYTES:
                raise RuntimeError("the generated sheet is unexpectedly large")
            return bytes(blob.data)
    said = " ".join(str(getattr(part, "text", "") or "") for part in parts).strip()
    raise RuntimeError(said[:200] or "the image model returned no image")


def draw(spec: FigureSpec, graph: PatentGraph, references: Sequence[Sheet],
         *, earlier: Sequence[bytes] = (), model: str = "",
         temperature: float = 0.32) -> Generated:
    """One figure's artwork, conditioned on the reference sheets and on our earlier figures."""
    draft_figures = _client()
    model = model or draft_figures.image_model()
    reference_blobs = [sheet.png for sheet in references if sheet.ok]
    # Most recent first in intent, appended last in order, so the newest of our own figures is
    # the final image the model sees and the prompt's "the last ... reference images" is true.
    ours = [blob for blob in earlier if blob][-MAX_EARLIER_FIGURES:]
    reference_blobs.extend(ours)
    prompt = build_prompt(spec, graph, reference_count=len(reference_blobs),
                          earlier_figures=ours)
    out = Generated(prompt=prompt,
                    references=[f"{sheet.pub}#{sheet.index}" for sheet in references
                                if sheet.ok])

    last = ""
    for attempt in range(ATTEMPTS):
        out.attempts = attempt + 1
        try:
            png = _generate_once(prompt, reference_blobs, model, temperature)
        except Exception as exc:
            if _is_exhausted(exc):
                raise GenerationExhausted(
                    "the image model is out of quota on this box") from exc
            last = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(0.4 * (2 ** attempt) + random.uniform(0, 0.2))
            continue
        out.png = png
        return out
    out.error = last or "the image model could not draw this figure"
    return out
