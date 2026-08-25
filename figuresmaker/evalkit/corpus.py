"""Ground truth from granted patents.

A granted patent is a labelled example: the specification went in, and a figure set that an
examiner accepted came out. What it does not give you is the drawings as data. So the ground
truth used here is what the document itself states about its own figures, all of which is text:

* which views exist, and what each one is, from the brief description of the drawings;
* which reference characters exist, from the description;
* which characters belong to which view, from the paragraphs that discuss each view.

That is a real target and it is checkable. A figure set that puts the wrong parts in the wrong
views disagrees with it, and so does one that invents a view the patent never had.

What it deliberately does not measure is whether the picture looks like the applicant's picture.
Two draughtspeople given one specification draw different pictures, and scoring against pixels
would reward imitation rather than correctness.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from fm import claims as claims_mod, ingest, registry as registry_mod, sections as sections_mod
from fm.schemas import Sections


@dataclass
class Truth:
    """What one granted patent says about its own drawings."""
    number: str
    title: str = ""
    figures: list[str] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)         # label -> kind hint
    numerals: dict[str, str] = field(default_factory=dict)      # numeral -> term
    per_figure: dict[str, list[str]] = field(default_factory=dict)
    claim_numerals: list[str] = field(default_factory=list)
    chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "title": self.title, "figures": self.figures,
                "kinds": self.kinds, "numerals": self.numerals, "per_figure": self.per_figure,
                "claim_numerals": self.claim_numerals, "chars": self.chars}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Truth":
        return cls(**{k: raw.get(k, v) for k, v in
                      {"number": "", "title": "", "figures": [], "kinds": {}, "numerals": {},
                       "per_figure": {}, "claim_numerals": [], "chars": 0}.items()})


def derive(sections: Sections, number: str) -> Truth:
    """The truth for one document, from its own text. No model is used."""
    truth = Truth(number=number, title=sections.title, chars=len(sections.raw))
    truth.figures = [item.label for item in sections.brief_items]
    truth.kinds = {item.label: item.kind_hint for item in sections.brief_items if item.kind_hint}

    registry = registry_mod.build(sections, use_model=False)
    truth.numerals = {entry.numeral: entry.term for entry in registry.entries}
    for entry in registry.entries:
        for label in entry.figures:
            truth.per_figure.setdefault(label, []).append(entry.numeral)
    for label in truth.per_figure:
        truth.per_figure[label] = sorted(set(truth.per_figure[label]), key=_numeral_key)
    for label in truth.figures:
        truth.per_figure.setdefault(label, [])

    claim_list = claims_mod.match_to_registry(
        claims_mod.analyse(sections.claims, use_model=False), registry)
    truth.claim_numerals = claims_mod.covered_numerals(claim_list)
    return truth


def _numeral_key(value: str) -> tuple[int, str]:
    match = re.match(r"(\d+)(.*)", value)
    return (int(match.group(1)), match.group(2)) if match else (10 ** 9, value)


def fetch(number: str) -> tuple[Sections, Truth]:
    got = ingest.ingest(text=number)
    sections = sections_mod.analyse(got.text, title=got.title, source=got.source,
                                    source_ref=got.source_ref)
    return sections, derive(sections, got.source_ref or number)


def build(numbers: Iterable[str], out_dir: Path, *, pause: float = 1.5,
          verbose: bool = True) -> list[Truth]:
    """Fetch a corpus and cache it. A number that cannot be fetched is recorded, not dropped."""
    out_dir.mkdir(parents=True, exist_ok=True)
    truths: list[Truth] = []
    problems: list[dict[str, str]] = []
    for number in numbers:
        cache = out_dir / f"{_slug(number)}.json"
        if cache.exists():
            raw = json.loads(cache.read_text(encoding="utf-8"))
            truths.append(Truth.from_dict(raw["truth"]))
            if verbose:
                print(f"  cached  {truths[-1].number:18s} {len(truths[-1].figures):2d} figures, "
                      f"{len(truths[-1].numerals):3d} numerals")
            continue
        try:
            sections, truth = fetch(number)
        except Exception as exc:
            problems.append({"number": number, "error": f"{type(exc).__name__}: {exc}"})
            if verbose:
                print(f"  MISSED  {number:18s} {type(exc).__name__}: {exc}")
            continue
        cache.write_text(json.dumps({"truth": truth.to_dict(), "text": sections.raw},
                                    ensure_ascii=False), encoding="utf-8")
        truths.append(truth)
        if verbose:
            print(f"  fetched {truth.number:18s} {len(truth.figures):2d} figures, "
                  f"{len(truth.numerals):3d} numerals, {truth.chars:,} chars")
        time.sleep(pause)
    if problems:
        (out_dir / "_problems.json").write_text(json.dumps(problems, indent=1), encoding="utf-8")
    return truths


def load(out_dir: Path) -> list[tuple[Truth, str]]:
    """Every cached case, as (truth, specification text)."""
    out: list[tuple[Truth, str]] = []
    for path in sorted(out_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.append((Truth.from_dict(raw["truth"]), raw.get("text") or ""))
    return out


def _slug(number: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", number).upper()[:32]


# A spread on purpose: mechanical, electrical, software, medical and a design-heavy case, so a
# score is not an average over one kind of drawing. Every one is a granted US patent with a brief
# description of the drawings.
DEFAULT_NUMBERS: tuple[str, ...] = (
    "US11000000B2",     # medical device, perspective and cutaway views
    "US9878876B2",      # elevator demand entry, block diagrams and a flow chart
    "US10583560B1",     # robotics, block diagrams and environment views
    "US10000000B2",     # coherent LADAR, optics and block diagrams
    "US9700980B2",      # machine tool
    "US10195470B2",     # exercise device, mechanical
    "US9526884B2",      # medical lead, mechanical detail
    "US10102449B1",     # vision, block and flow
    "US11071905B2",     # sports equipment
    "US10717208B1",     # cutting tool
)
