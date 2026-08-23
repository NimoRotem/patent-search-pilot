"""Test doubles. Never selectable from a deployed configuration.

Unit tests must not make live model calls: they would be slow, they would cost money on every
push, and a flaky provider would make a green test suite mean nothing. These stand in.

``ScriptedTextReasoner`` replays canned structured replies keyed by task.

``SvgReadingVerifier`` is the interesting one. A real vision verifier must reconstruct the
drawing from pixels precisely because it must be able to disagree with the renderer. A test
double may take the shortcut of reading the SVG's own DOM, and that shortcut is what makes the
synthetic-corruption suite work: corrupt the SVG, and the observation genuinely changes, so the
semantic diff and the correction loop are exercised end to end with no network. It is exported
here, and only here, so nothing can reach for it by accident from application code.
"""
from __future__ import annotations

import re
from typing import Any, Optional, TypeVar

from pydantic import BaseModel

from ..schemas import (ObservedComponent, ObservedConnection, ObservedFigure,
                       ObservedReference)
from .base import CallLog, StructuredOutputError, coerce

T = TypeVar("T", bound=BaseModel)


class ScriptedTextReasoner:
    name = "mock"

    def __init__(self, replies: Optional[dict[str, Any]] = None,
                 log: Optional[CallLog] = None, model: str = "scripted"):
        self.replies = dict(replies or {})
        self.log = log
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def generate_structured(self, task: str, schema: type[T], system: str, context: str,
                            *, prompt_version: str = "", max_tokens: int = 8000) -> T:
        self.calls.append((task, context))
        if task not in self.replies:
            raise StructuredOutputError(f"no scripted reply for task {task!r}")
        reply = self.replies[task]
        if callable(reply):
            reply = reply(context)
        return coerce(schema, reply)


_ENTITY_G = re.compile(
    r'<g[^>]*data-entity-id="(?P<entity>[^"]+)"[^>]*data-reference="(?P<ref>[^"]*)"[^>]*>')
_LABEL_T = re.compile(
    r'<text[^>]*data-reference-label="(?P<ref>[^"]+)"[^>]*data-entity-id="(?P<entity>[^"]+)"'
    r'[^>]*data-leader-target="(?P<target>[^"]*)"[^>]*>')
_EDGE_P = re.compile(
    r'<path[^>]*data-relation-id="(?P<rel>[^"]+)"[^>]*data-from="(?P<from>[^"]*)"'
    r'[^>]*data-to="(?P<to>[^"]*)"[^>]*data-directed="(?P<dir>[^"]*)"[^>]*>')
_TEXT = re.compile(r"<text[^>]*>(?P<body>[^<]*)</text>")
_CAPTION = re.compile(r'<text[^>]*data-caption-for="(?P<entity>[^"]+)"[^>]*>(?P<body>[^<]*)</text>')


class SvgReadingVerifier:
    """A verifier that reads the SVG instead of the pixels. Tests only."""

    name = "mock-svg"
    model = "svg-dom"

    def __init__(self, svg_by_figure: Optional[dict[str, str]] = None,
                 log: Optional[CallLog] = None):
        self.svg_by_figure = dict(svg_by_figure or {})
        self.log = log
        self.current: str = ""

    def observe_svg(self, svg: str) -> ObservedFigure:
        references: list[ObservedReference] = []
        components: list[ObservedComponent] = []
        connections: list[ObservedConnection] = []

        # A real reader describes the object a leader lands on in words. The double reads the
        # caption the renderer drew inside that object, which is the same information by a
        # shortcut only a test is allowed to take.
        caption_of = {match.group("entity"): match.group("body").strip()
                      for match in _CAPTION.finditer(svg)}
        entity_reference: dict[str, str] = {}
        for match in _ENTITY_G.finditer(svg):
            entity = match.group("entity")
            entity_reference[entity] = match.group("ref")
            components.append(ObservedComponent(
                observed_id=entity,
                description=caption_of.get(entity, entity.replace("_", " ")),
                confidence=0.99))
        for match in _LABEL_T.finditer(svg):
            entity = match.group("entity")
            references.append(ObservedReference(
                reference=match.group("ref"),
                target_description=caption_of.get(entity, entity), confidence=0.99))
        for match in _EDGE_P.finditer(svg):
            directed = match.group("dir") == "1"
            connections.append(ObservedConnection(
                from_reference=entity_reference.get(match.group("from"), ""),
                to_reference=entity_reference.get(match.group("to"), ""),
                direction="forward" if directed else "none", confidence=0.99))
        visible_text = [m.group("body").strip() for m in _TEXT.finditer(svg)
                        if m.group("body").strip()]
        seen: dict[tuple[str, str], int] = {}
        overlapping: list[str] = []
        for reference in references:
            key = (reference.reference, reference.target_description)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                overlapping.append(reference.reference)
        return ObservedFigure(
            visible_references=references, visible_components=components,
            connections=connections, visible_text=visible_text,
            overlapping_labels=sorted(set(overlapping)))

    def inspect(self, image_png: bytes, system: str, instruction: str, schema: type[T],
                *, prompt_version: str = "", max_tokens: int = 4000) -> T:
        svg = self.current or self.svg_by_figure.get(instruction, "")
        observed = self.observe_svg(svg)
        return coerce(schema, observed.model_dump())
