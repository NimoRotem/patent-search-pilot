"""Where a figure's geometry actually came from.

This module is the product definition in code. A specification says what the parts of an
invention are and how they relate; it does not say where they are in space. Asking a model to
supply that from prose is asking it to invent, and invention is the one thing a filing-ready
drawing may not contain. So every figure declares a source, the renderer stamps it, and the
checker refuses to call a mechanical view filing-ready unless that source is authoritative.

Six kinds, and the difference between them is who decided the geometry:

``cad``              a mesh the applicant supplied. The geometry is theirs.
``sketch``           a drawing the applicant made, traced. The geometry is theirs.
``screenshot``       a real screen. The layout is theirs.
``existing_figure``  a sheet already filed or drafted, re-read. The geometry is theirs.
``schema``           a graph or a wireframe derived from the text. Legitimate, because the
                     content of a block diagram, a flow chart or a sequence really is in the
                     prose: the boxes are the parts, the arrows are the sentences.
``blockout``         solids a model proposed from prose alone. Useful for agreeing a layout with
                     an attorney. Not a drawing of the invention, and never presented as one.

The table below is the whole rule. It is deliberately small and deliberately strict.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Which sources may stand behind which kind of figure. A figure whose source is not in its set is
# a draft: it may be planned, drawn and reviewed, but it is not filing-ready and every surface
# that shows it says so.
AUTHORITATIVE_FOR: dict[str, frozenset[str]] = {
    "block_diagram": frozenset({"schema", "sketch", "existing_figure", "cad"}),
    "flowchart": frozenset({"schema", "sketch", "existing_figure"}),
    "sequence": frozenset({"schema", "sketch", "existing_figure"}),
    "ui_screen": frozenset({"screenshot", "schema", "sketch", "existing_figure"}),
    "perspective": frozenset({"cad", "sketch", "existing_figure"}),
    "exploded": frozenset({"cad", "sketch", "existing_figure"}),
    "cross_section": frozenset({"cad", "sketch", "existing_figure"}),
}

SOURCE_KINDS: tuple[str, ...] = ("cad", "sketch", "screenshot", "existing_figure", "schema",
                                 "blockout")

# The kinds of figure whose content genuinely is in the prose. Everything else needs a source.
DERIVABLE_FROM_TEXT: frozenset[str] = frozenset({"block_diagram", "flowchart", "sequence",
                                                 "ui_screen"})

MESH_SUFFIXES = {".stl", ".obj", ".ply", ".off"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
SHEET_SUFFIXES = {".pdf"}
VECTOR_SUFFIXES = {".svg"}

MAX_SOURCE_BYTES = int(os.environ.get("FM_MAX_SOURCE_BYTES", str(64 * 1024 * 1024)))


class SourceError(RuntimeError):
    """A source could not be taken in. Says which file and why, never drops it silently."""


def is_authoritative(figure_kind: str, source_kind: str) -> bool:
    return source_kind in AUTHORITATIVE_FOR.get(figure_kind, frozenset())


def needs_a_source(figure_kind: str) -> bool:
    """True when this kind of figure cannot honestly be built from the text alone."""
    return figure_kind not in DERIVABLE_FROM_TEXT


@dataclass
class Source:
    """One file the applicant supplied, and what was found in it."""
    id: str
    kind: str                       # one of SOURCE_KINDS, minus "schema" and "blockout"
    filename: str
    suffix: str
    bytes: int
    meta: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Source":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in allowed})

    def path(self, root: Path) -> Path:
        return root / "sources" / f"{self.id}{self.suffix}"


def classify(filename: str, blob: bytes) -> str:
    """What kind of source a file is, from its extension and its first bytes.

    A screenshot and a scanned sketch are both PNGs, so the caller may override this; what is
    decided here is the honest default, and a wrong default is visible in the coverage matrix
    before anything is rendered.
    """
    suffix = Path(filename).suffix.lower()
    if suffix in MESH_SUFFIXES:
        return "cad"
    if suffix in SHEET_SUFFIXES or blob[:5] == b"%PDF-":
        return "existing_figure"
    if suffix in VECTOR_SUFFIXES:
        return "existing_figure"
    if suffix in IMAGE_SUFFIXES:
        return "sketch"
    raise SourceError(
        f"{filename}: this is not a kind of source the compiler reads. Meshes "
        f"({', '.join(sorted(MESH_SUFFIXES))}), images "
        f"({', '.join(sorted(IMAGE_SUFFIXES))}), PDFs and SVGs are.")


def inspect(kind: str, filename: str, blob: bytes) -> dict[str, Any]:
    """What is in the file, checked at upload rather than at render time.

    A mesh with no triangles and a PDF with no pages both look fine until the figure that needed
    them comes out empty, by which time the reason is three stages away.
    """
    if kind == "cad":
        from .importers import mesh as mesh_import

        counts = mesh_import.probe(filename, blob)
        if not counts.get("triangles"):
            raise SourceError(f"{filename}: no triangles were found in this mesh.")
        return counts
    if kind in ("sketch", "screenshot"):
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(blob)) as image:
                info = {"width": image.size[0], "height": image.size[1], "mode": image.mode}
        except Exception as exc:
            raise SourceError(f"{filename}: this is not an image that can be opened ({exc}).")
        if kind == "sketch":
            # Traced at upload rather than at render time, so "this is a photograph, not line
            # work" is said while the person who chose the file is still looking at it.
            from .importers import trace as trace_import

            try:
                info.update(trace_import.probe(filename, blob))
            except trace_import.TraceError as exc:
                raise SourceError(str(exc))
        return info
    if kind == "existing_figure":
        if blob[:5] == b"%PDF-":
            try:
                from pypdf import PdfReader
                import io

                pages = len(PdfReader(io.BytesIO(blob)).pages)
            except Exception as exc:
                raise SourceError(f"{filename}: this PDF could not be read ({exc}).")
            if not pages:
                raise SourceError(f"{filename}: this PDF has no pages.")
            return {"pages": pages}
        return {"vector": True}
    return {}


@dataclass
class SourceSet:
    """Every source for one job."""
    items: list[Source] = field(default_factory=list)

    def by_id(self) -> dict[str, Source]:
        return {s.id: s for s in self.items}

    def of_kind(self, kind: str) -> list[Source]:
        return [s for s in self.items if s.kind == kind]

    def as_dict(self) -> dict[str, Any]:
        return {"items": [s.as_dict() for s in self.items]}

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> "SourceSet":
        return cls(items=[Source.from_dict(s) for s in (raw or {}).get("items") or []])

    def summary(self) -> str:
        if not self.items:
            return "no sources"
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items()))


def add(root: Path, filename: str, blob: bytes, *, kind: str = "") -> Source:
    """Take one file in, check it, and store it under a hash of its contents."""
    if not blob:
        raise SourceError(f"{filename} is empty.")
    if len(blob) > MAX_SOURCE_BYTES:
        raise SourceError(f"{filename} is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB.")
    kind = kind or classify(filename, blob)
    if kind not in SOURCE_KINDS:
        raise SourceError(f"{kind!r} is not a source kind.")
    meta = inspect(kind, filename, blob)

    digest = hashlib.sha256(blob).hexdigest()[:16]
    suffix = Path(filename).suffix.lower() or ".bin"
    source = Source(id=digest, kind=kind, filename=Path(filename).name[:120], suffix=suffix,
                    bytes=len(blob), meta=meta)
    target = source.path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(blob)
    return source


def load(root: Path) -> SourceSet:
    try:
        raw = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SourceSet()
    return SourceSet.from_dict(raw)


def save(root: Path, sources: SourceSet) -> None:
    (root / "sources.json").write_text(json.dumps(sources.as_dict(), indent=1), encoding="utf-8")


def read(root: Path, source_id: str) -> bytes:
    found = load(root).by_id().get(source_id)
    if found is None:
        raise SourceError(f"no source {source_id!r} is held for this job.")
    try:
        return found.path(root).read_bytes()
    except OSError as exc:
        raise SourceError(f"source {source_id} is recorded but its file is gone ({exc}).")


# ------------------------------------------------------------------------------ what to say

def draft_reason(figure_kind: str, source_kind: str) -> str:
    """Why a figure is not filing-ready, in words an attorney can act on."""
    if is_authoritative(figure_kind, source_kind):
        return ""
    wanted = sorted(AUTHORITATIVE_FOR.get(figure_kind, frozenset()))
    pretty = ", ".join(w.replace("_", " ") for w in wanted) or "a supplied source"
    if source_kind == "blockout":
        return (f"a {figure_kind.replace('_', ' ')} built from the description alone is a "
                f"blockout, not a drawing of the invention. Supply {pretty} for this view and it "
                "will be compiled from that instead.")
    return (f"a {figure_kind.replace('_', ' ')} cannot be compiled from a "
            f"{source_kind.replace('_', ' ')}. It needs {pretty}.")
