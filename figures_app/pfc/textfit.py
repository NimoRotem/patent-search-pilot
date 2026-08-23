"""Text measurement and wrapping, from the profile's character ratio and nothing else.

The renderer draws a caption and the geometry validator boxes it. They must agree exactly on
how much room that caption takes, and the only way to guarantee that without shipping a font
parser is to make both call the same arithmetic. It is an approximation of Helvetica's advance
widths; being an approximation is fine, being two different approximations is not.
"""
from __future__ import annotations

import re

from .profiles import DrawingProfile

_SPLIT = re.compile(r"\s+")


def measure(profile: DrawingProfile, text: str, height: float) -> float:
    return profile.text_width(text, height)


def wrap(profile: DrawingProfile, text: str, max_width: float, height: float,
         max_lines: int = 3) -> list[str]:
    """Break a caption into lines that fit, eliding the tail rather than overflowing.

    An overflowing caption is a clipped element, which is a blocking geometry defect. Truncating
    with an ellipsis is visible and honest; silently letting it run past the box edge is not.
    """
    words = [w for w in _SPLIT.split(str(text or "").strip()) if w]
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and measure(profile, candidate, height) > max_width:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) == max_lines:
        consumed = sum(len(line.split()) for line in lines)
        if consumed < len(words):
            last = lines[-1]
            while last and measure(profile, last + "…", height) > max_width:
                last = last[:-1].rstrip()
            lines[-1] = (last + "…") if last else "…"
    # A single word longer than the box is cut on the character, not left to overflow.
    fitted: list[str] = []
    for line in lines:
        while line and measure(profile, line, height) > max_width:
            line = line[:-1]
        fitted.append(line)
    return [line for line in fitted if line]


def block_height(lines: list[str], height: float, leading: float = 1.25) -> float:
    return max(0.0, len(lines) * height * leading)


def block_width(profile: DrawingProfile, lines: list[str], height: float) -> float:
    return max((measure(profile, line, height) for line in lines), default=0.0)


def shorten(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:max(1, limit - 1)].rstrip() + "…"
