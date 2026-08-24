"""Getting the WHOLE description, and knowing when what you got is not it.

The search application has a full-text ladder and this module deliberately does not use it. That
ladder is built for bulk retrieval, where a partial document is a slightly weaker query and the
cheapest source that answers is the right one. A figure compiler has a different requirement: it
labels drawings with reference numerals, the numerals live in the detailed description, and a
description that stops before the detailed description is worth exactly nothing.

The difference is not theoretical. On US-2024/0246200-A1 the search ladder's first free rung
returned 40,000 characters, which looked like plenty and was the front matter and the background
with **no reference numerals in it at all**; the compiler accepted it and produced no figures.
The same publication read through our own Google Patents reader gives 82,256 characters with 117
numerals. So:

**The order is ours.** Our own stores first because they are free and complete when they have it,
then our in-house reader, then the paid channel, then the office services for the jurisdictions
they cover. PQAI is excluded outright: it is free and it truncates, which is a fine trade for a
search and a disqualifying one here.

**Every rung is tested before it is accepted.** Not "did I get text" but "can this text label a
drawing": does it carry a detailed description, and does that description contain reference
numerals? A source that fails is recorded with the reason and the ladder continues. That check
is what makes the ladder self-correcting when a source silently degrades, which is a thing
sources do.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import parse, pilot

# A description has to carry at least this many distinct reference numerals before a figure can
# be labelled from it. Three is the floor for a drawing that says anything; most patents have
# dozens, and a document returning one or two is a truncation or a cover page.
MIN_NUMERALS = 3
MIN_DESCRIPTION_CHARS = 1200

# Never asked. It is free, it is fast, and it truncates the description, which is the one thing
# this pipeline cannot tolerate.
EXCLUDED = {"pqai": "it truncates the description, and the numerals are at the end of it"}


@dataclass
class Attempt:
    source: str
    chars: int = 0
    numerals: int = 0
    accepted: bool = False
    reason: str = ""


@dataclass
class FullText:
    title: str = ""
    abstract: str = ""
    description: str = ""
    claims: str = ""
    source: str = ""
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.description or self.claims)


def usability(description: str) -> tuple[bool, int, str]:
    """``(usable, numerals found, why not)`` — can a drawing be labelled from this text?

    The test is deliberately the compiler's actual requirement rather than a length. Forty
    thousand characters of background reads as a healthy document to any size check and cannot
    label a single figure.
    """
    from . import numerals as numerals_module

    text = parse.normalize(description or "")
    # Length is the last thing asked, not the first. A short description that names six parts
    # can label a figure; forty thousand characters of background cannot. Testing size first
    # rejected the good case and admitted the bad one.
    if len(text) < MIN_DESCRIPTION_CHARS // 6:
        return False, 0, f"only {len(text):,} characters of description"
    _sections, paragraphs = parse.parse_sections(text)
    body = parse.description_paragraphs(paragraphs)
    if not body:
        return False, 0, ("no detailed description in it, only front matter and background, so "
                          "the document is truncated before the part that carries the numerals")
    registry = numerals_module.build_registry(body)
    if len(registry) < MIN_NUMERALS:
        return False, len(registry), (
            f"{len(registry)} reference numeral(s) in its description, too few to label a figure")
    return True, len(registry), ""


# ---------------------------------------------------------------------------
# the rungs
# ---------------------------------------------------------------------------
def _from_our_stores(pub: str) -> dict:
    """The pre-built corpus and the shared docstore. Free, instant, ours.

    Asked strictly, because the ladder writes down every answer and a store that could not be
    REACHED must not be recorded as a store that does not hold the publication. That distinction
    is the whole point of the notes this module emits.
    """
    record = pilot.corpus_record(pub, strict=True)
    if record.get("description") or record.get("claims"):
        return record
    return pilot.docstore_record(pub, strict=True)


def _adapter_details(pub: str, adapter_name: str, timeout: float) -> dict:
    """One source adapter, asked for one document."""
    return pilot.adapter_details(pub, adapter_name, timeout=timeout)


def _rungs() -> list[tuple[str, Callable[[str, float], dict]]]:
    return [
        ("our own corpus", lambda pub, _t: _from_our_stores(pub)),
        ("our Google Patents reader",
         lambda pub, t: _adapter_details(pub, "gpatents_direct", t)),
        ("SerpApi", lambda pub, t: _adapter_details(pub, "serpapi_gpatents", t)),
        ("EPO OPS", lambda pub, t: _adapter_details(pub, "epo_ops", t)),
        ("HimmPat", lambda pub, t: _adapter_details(pub, "himmpat", t)),
    ]


def fetch(pub: str, notes: Optional[list[str]] = None, timeout: float = 90.0) -> FullText:
    """Walk the ladder until a source returns text a figure can actually be built from."""
    notes = notes if notes is not None else []
    out = FullText()
    best: Optional[tuple[Attempt, dict]] = None

    for label, call in _rungs():
        try:
            record = call(pub, timeout) or {}
        except pilot.SourceUnavailable as exc:
            # Could not be ASKED, which is a different fact from "has nothing" and has to be
            # said out loud: a wiring mistake reported as a missing document is a wiring
            # mistake nobody finds.
            out.attempts.append(Attempt(source=label, reason=str(exc)))
            continue
        except Exception as exc:
            out.attempts.append(
                Attempt(source=label, reason=f"it failed: {type(exc).__name__}"))
            continue
        description = str(record.get("description") or "")
        claims = str(record.get("claims") or "")
        if not description and not claims:
            out.attempts.append(Attempt(source=label, reason="it does not hold this publication"))
            continue
        ok, found, why = usability(description)
        attempt = Attempt(source=label, chars=len(description), numerals=found,
                          accepted=ok, reason=why)
        out.attempts.append(attempt)
        if ok:
            out.title = str(record.get("title") or "")
            out.abstract = str(record.get("abstract") or "")
            out.description = description
            out.claims = claims
            out.source = label
            break
        # Not usable, but better than nothing if every remaining rung also fails.
        if best is None or len(description) > best[0].chars:
            best = (attempt, record)

    if not out.ok and best is not None:
        attempt, record = best
        out.title = str(record.get("title") or "")
        out.abstract = str(record.get("abstract") or "")
        out.description = str(record.get("description") or "")
        out.claims = str(record.get("claims") or "")
        out.source = attempt.source + " (incomplete)"

    _explain(pub, out, notes)
    return out


def _explain(pub: str, out: FullText, notes: list[str]) -> None:
    """Say what was asked and what each answer was worth. A silent ladder cannot be debugged."""
    rejected = [a for a in out.attempts if not a.accepted and a.reason]
    if out.source and not out.source.endswith("(incomplete)"):
        accepted = next((a for a in out.attempts if a.accepted), None)
        detail = (f"{accepted.chars:,} characters of description carrying "
                  f"{accepted.numerals} reference numerals" if accepted else "")
        notes.append(f"the full text came from {out.source}: {detail}")
    elif out.source:
        notes.append(
            f"no source this box can reach holds a complete description of {pub}; the fullest "
            f"answer was from {out.source} and it is not enough to label a figure")
    for attempt in rejected:
        if attempt.reason == "it does not hold this publication":
            continue
        notes.append(f"{attempt.source}: {attempt.reason}")
