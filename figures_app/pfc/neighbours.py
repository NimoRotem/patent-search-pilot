"""The patents nearest this one, and the drawing sheets they filed.

Used as *visual reference* for the reference-guided figure mode: a real patent figure of a
comparable device shows the compiler what this kind of drawing looks like — the conventions, the
line weight, the level of abstraction — in a way no prompt describes.

Where the neighbours come from, best signal first:

``examiner citations``  the office's own judgement that this art is close. Nothing else in the
                        record is as good, because a human whose job is finding the nearest art
                        wrote it down.
``similar documents``   Google's similarity, which is broad and cheap and often right.
``applicant citations`` what the drafter thought was close, which is useful and partial.
``classification``      last, and only to fill a thin list: sharing a CPC group is a weak claim
                        to resemblance.

**On using them at all.** A published patent's drawings are public documents, but a figure
generated from ONE of them can come out recognisably that one, which is awkward in prosecution
and worse in litigation. So the references are always plural and always drawn from different
patents, the prompt asks for the arrangement THIS patent describes rather than the reference's,
and every generated sheet records which references it saw. What that buys is the drawing style;
what it must never buy is the drawing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import pilot

MAX_NEIGHBOURS = 8
MAX_SHEETS_PER_NEIGHBOUR = 4
# One reference from any single patent per figure. Two sheets of the same document conditioning
# one drawing is how a generated figure ends up being that document's figure.
MAX_SHEETS_PER_FIGURE = 3


@dataclass
class Sheet:
    pub: str
    index: int
    png: bytes = b""
    url: str = ""
    caption: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.png)


@dataclass
class Neighbour:
    pub: str
    title: str = ""
    why: str = ""
    rank: int = 0
    sheets: list[Sheet] = field(default_factory=list)


@dataclass
class Neighbourhood:
    neighbours: list[Neighbour] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def sheets(self) -> list[Sheet]:
        return [sheet for neighbour in self.neighbours for sheet in neighbour.sheets
                if sheet.ok]


def _ranked_candidates(record: dict) -> list[tuple[str, str, str]]:
    """``(publication, title, why)`` in the order they are worth looking at."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(pub: str, title: str, why: str) -> None:
        key = str(pub or "").upper().replace("-", "")
        if not key or key in seen:
            return
        seen.add(key)
        out.append((str(pub), str(title or ""), why))

    citations = record.get("citations") or []
    for row in citations:
        if isinstance(row, dict) and str(row.get("origin") or "").lower() == "examiner":
            add(row.get("pub", ""), row.get("title", ""), "cited by the examiner")
    for row in record.get("similar") or []:
        if isinstance(row, dict):
            add(row.get("pub", ""), row.get("title", ""), "a similar document")
    for row in citations:
        if isinstance(row, dict) and str(row.get("origin") or "").lower() != "examiner":
            add(row.get("pub", ""), row.get("title", ""), "cited by the applicant")
    return out


def _sheets_for(pub: str, limit: int = MAX_SHEETS_PER_NEIGHBOUR) -> list[Sheet]:
    """The drawing sheets of one neighbour, from the local cache or its CDN URL."""
    record = pilot.display_record(pub)
    directory = pilot.figure_dir(pub)
    out: list[Sheet] = []
    for index, item in enumerate((record.get("images") or [])[:limit]):
        if not isinstance(item, dict):
            continue
        sheet = Sheet(pub=pub, index=index, url=str(item.get("full") or item.get("src_url") or ""))
        name = item.get("file")
        if name and directory is not None:
            path = directory / str(name)
            try:
                if path.is_file():
                    sheet.png = path.read_bytes()
            except OSError:
                pass
        if not sheet.png and sheet.url:
            sheet.png = pilot.fetch_image(sheet.url)
        if sheet.ok:
            out.append(sheet)
    return out


def find(pub: str, record: Optional[dict] = None,
         limit: int = MAX_NEIGHBOURS) -> Neighbourhood:
    """The nearest patents that actually have drawings we can look at."""
    out = Neighbourhood()
    if not pub:
        out.notes.append("no publication number, so no neighbouring patents could be found")
        return out
    record = record if record is not None else pilot.display_record(pub)
    candidates = _ranked_candidates(record)
    if not candidates:
        out.notes.append("this publication's record carries no citations or similar documents")
        return out

    for rank, (candidate, title, why) in enumerate(candidates):
        if len(out.neighbours) >= limit:
            break
        sheets = _sheets_for(candidate)
        if not sheets:
            continue
        out.neighbours.append(Neighbour(pub=candidate, title=title, why=why, rank=rank,
                                        sheets=sheets))
    if out.neighbours:
        examiner = sum(1 for n in out.neighbours if n.why == "cited by the examiner")
        out.notes.append(
            f"{len(out.neighbours)} neighbouring patent(s) with drawings were used as visual "
            f"reference ({examiner} of them cited by the examiner), contributing "
            f"{len(out.sheets)} sheet(s)")
    else:
        out.notes.append(
            f"{len(candidates)} neighbouring patent(s) were found and none of them has a "
            "drawing this box can reach")
    return out


def references_for(neighbourhood: Neighbourhood, chosen: Iterable[tuple[str, int]]
                   ) -> list[Sheet]:
    """The sheets a figure selected, at most one per patent."""
    index = {(sheet.pub, sheet.index): sheet for sheet in neighbourhood.sheets}
    out: list[Sheet] = []
    used: set[str] = set()
    for pub, sheet_index in chosen:
        sheet = index.get((pub, sheet_index))
        if sheet is None or sheet.pub in used:
            continue
        used.add(sheet.pub)
        out.append(sheet)
        if len(out) >= MAX_SHEETS_PER_FIGURE:
            break
    return out


def spread(neighbourhood: Neighbourhood, count: int = MAX_SHEETS_PER_FIGURE) -> list[Sheet]:
    """A default selection: the first sheet of each of the nearest patents.

    One sheet each, from as many different documents as possible, which is both the better
    reference and the safer one.
    """
    out: list[Sheet] = []
    for neighbour in neighbourhood.neighbours:
        if neighbour.sheets:
            out.append(neighbour.sheets[0])
        if len(out) >= count:
            break
    return out
