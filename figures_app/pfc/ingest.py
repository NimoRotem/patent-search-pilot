"""Getting the patent in: a file, or a link, into one canonical document.

The search application already solved the hard parts of this and had them measured against real
grants, so this module is mostly a matter of asking it the right questions in the right order.
What is new here is what a figure compiler needs and a search engine does not:

* **The description, in full.** A search runs happily on an abstract and the claims. Reference
  numerals live in the detailed description, so a compiler that cannot read it has nothing to
  work with. The publication record often carries no description at all, and the facsimile PDF
  always does, so the PDF is preferred and the record is the fallback.
* **The applicant's own drawings, kept separate.** They are extracted, stored and shown beside
  the generated figures for comparison, and they are never read into the semantic model. The
  compiler's whole claim is that it drew the figure from the words; tracing the filed drawing
  would make that claim false.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import parse, pilot
from .schemas import OriginalFigure, Section, SourceDocument, sha256_text

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_ORIGINAL_FIGURES = 40
MIN_USEFUL_TEXT = 400

Stage = Optional[Callable[[str, str], None]]


class IngestError(ValueError):
    """A user-facing reason the document could not be read."""


@dataclass
class IngestResult:
    document: SourceDocument
    original_images: list[bytes] = field(default_factory=list)
    original_urls: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def notes(self) -> list[str]:
        return self.document.notes


def _stage(on_stage: Stage, key: str, message: str) -> None:
    if on_stage:
        try:
            on_stage(key, message)
        except Exception:
            pass


def _document(text: str, *, origin: str, origin_label: str, title: str = "",
              publication_number: str = "", notes: Optional[list[str]] = None,
              google_patents: Optional[str] = None,
              espacenet: Optional[str] = None) -> SourceDocument:
    normalized = parse.normalize(text)
    sections, paragraphs = parse.parse_sections(normalized)
    if not paragraphs:
        raise IngestError("no readable text could be taken out of this document")
    return SourceDocument(
        document_id=sha256_text(normalized)[:16],
        title=parse.find_title(normalized, title),
        publication_number=publication_number,
        origin=origin,  # type: ignore[arg-type]
        origin_label=origin_label,
        sha256=sha256_text(normalized),
        sections=sections, paragraphs=paragraphs,
        notes=list(notes or []),
        google_patents=google_patents, espacenet=espacenet)


def _describe(document: SourceDocument) -> str:
    counts = {section.id: len(section.paragraph_ids) for section in document.sections}
    parts = [f"{count} paragraph{'s' if count != 1 else ''} of "
             f"{section.replace('_', ' ')}" for section, count in counts.items()
             if section != "other" and count]
    return "read " + ", ".join(parts) if parts else "read the document"


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------
def ingest_upload(data: bytes, filename: str, on_stage: Stage = None) -> IngestResult:
    if not data:
        raise IngestError("that file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestError(f"that file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")

    label = pilot.safe_label(filename)
    kind = _sniff(data, filename)
    notes: list[str] = []
    images: list[bytes] = []

    if kind == "pdf":
        _stage(on_stage, "read", "reading the PDF and reconstructing its columns")
        import os
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(handle, "wb") as sink:
                sink.write(data)
            read = pilot.read_pdf(path)
            text = read.get("text") or ""
            notes.extend(str(note)[:200] for note in (read.get("notes") or []))
            if read.get("n_pages"):
                notes.append(f"{read['n_pages']} page(s) read")
            if not read.get("text_layer"):
                raise IngestError(
                    "this PDF is a scan with no text layer. The compiler builds figures from "
                    "the words of the description, so it needs a PDF whose text can be read.")
            _stage(on_stage, "figures", "extracting the drawings that were filed")
            images = pilot.figures_from_pdf(path)[:MAX_ORIGINAL_FIGURES]
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    elif kind == "docx":
        _stage(on_stage, "read", "reading the document")
        text = pilot.docx_text(data)
    else:
        _stage(on_stage, "read", "reading the document")
        text = data.decode("utf-8", "ignore")
        if kind == "md":
            # Headings must survive as their own lines for the sectioner to see them.
            text = re.sub(r"(?m)^#{1,6}\s*", "", text)

    if len(parse.normalize(text)) < MIN_USEFUL_TEXT:
        raise IngestError("there is not enough text in that file to build figures from")

    if images:
        notes.append(f"{len(images)} drawing sheet(s) taken from the filed document, for "
                     "comparison only")
    else:
        notes.append("no drawing sheets were found in the file, so there is nothing to compare "
                     "against")

    document = _document(text, origin="upload", origin_label=label, notes=notes)
    document.original_figures = [OriginalFigure(index=index) for index in range(len(images))]
    document.notes.append(_describe(document))
    return IngestResult(document=document, original_images=images, text=parse.normalize(text))


def _sniff(data: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if data[:5] == b"%PDF-":
        return "pdf"
    if name.endswith(".pdf"):
        raise IngestError("that file is named .pdf but is not a PDF")
    if data[:4] == b"PK\x03\x04" and name.endswith(".docx"):
        return "docx"
    if name.endswith(".docx"):
        raise IngestError("that file is named .docx but is not a Word document")
    try:
        data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        raise IngestError("upload a PDF, a .docx, a .txt or a .md file") from None
    return "md" if name.endswith((".md", ".markdown")) else "txt"


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------
def ingest_link(raw: str, on_stage: Stage = None) -> IngestResult:
    pub = pilot.parse_patent_ref(raw)
    if not pub:
        raise IngestError("that is not a patent link or publication number the compiler "
                          "recognises")
    _stage(on_stage, "read", f"looking up publication {pub}")
    record = pilot.display_record(pub)
    notes = [f"resolved publication {pub}"]

    text, images, urls = _text_from_pdf(pub, record, notes, on_stage)
    if len(parse.normalize(text)) < MIN_USEFUL_TEXT:
        text = _text_from_record(record, notes)
        images, urls = _images_from_record(pub, record, notes)

    if len(parse.normalize(text)) < MIN_USEFUL_TEXT:
        raise IngestError(
            f"nothing this box can reach carries the full text of {pub}. Upload the PDF and the "
            "compiler will read it from that instead.")

    document = _document(
        text, origin="link", origin_label=pub, title=str(record.get("title") or ""),
        publication_number=pub, notes=notes,
        google_patents=record.get("google_patents"), espacenet=record.get("espacenet"))
    document.original_figures = [
        OriginalFigure(index=index, url=urls[index] if index < len(urls) else "")
        for index in range(max(len(images), len(urls)))]
    document.notes.append(_describe(document))
    return IngestResult(document=document, original_images=images, original_urls=urls,
                        text=parse.normalize(text))


def _text_from_pdf(pub: str, record: dict, notes: list[str],
                   on_stage: Stage) -> tuple[str, list[bytes], list[str]]:
    """The facsimile PDF is the only source that reliably carries the description."""
    pdf_dir = pilot.pdf_dir()
    if pdf_dir is None:
        return "", [], []
    path = Path(pdf_dir) / f"{pub}.pdf"
    if not path.is_file():
        url = str(record.get("pdf_url") or "") or pilot.scrape_pdf_url(pub)
        if not url:
            notes.append("no facsimile PDF could be found for this publication")
            return "", [], []
        _stage(on_stage, "read", "downloading the published document")
        if not pilot.download(url, path) or not path.is_file():
            notes.append("the facsimile PDF could not be downloaded")
            return "", [], []
    read = pilot.read_pdf(str(path))
    text = read.get("text") or ""
    if not read.get("text_layer") or len(text) < MIN_USEFUL_TEXT:
        notes.append("the published PDF is a scan with no text layer")
        return "", [], []
    notes.append(f"read the published document: {read.get('n_pages') or 0} page(s), "
                 f"{len(text):,} characters")
    _stage(on_stage, "figures", "extracting the drawings that were filed")
    images = pilot.figures_from_pdf(str(path))[:MAX_ORIGINAL_FIGURES]
    if images:
        notes.append(f"{len(images)} drawing sheet(s) taken from the published document, for "
                     "comparison only")
    return text, images, []


def _text_from_record(record: dict, notes: list[str]) -> str:
    """Fallback: whatever text the publication record itself carries."""
    parts: list[str] = []
    title = str(record.get("title") or "").strip()
    if title:
        parts.append(title)
    abstract = str(record.get("abstract") or "").strip()
    if abstract:
        parts.append("ABSTRACT\n" + abstract)
    description = record.get("description")
    if isinstance(description, list):
        description = "\n\n".join(str(item) for item in description)
    description = str(description or "").strip()
    if description:
        parts.append("DETAILED DESCRIPTION\n" + description)
        notes.append("the description came from the publication record rather than the PDF")
    claims = record.get("claims")
    if isinstance(claims, list):
        claims_text = "\n".join(str(item) for item in claims)
    else:
        claims_text = str(claims or "")
    if claims_text.strip():
        parts.append("CLAIMS\n" + claims_text.strip())
    if not description:
        notes.append("no source this box can reach carries the description of this publication, "
                     "so only the abstract and claims were available")
    return "\n\n".join(parts)


def _images_from_record(pub: str, record: dict, notes: list[str]
                        ) -> tuple[list[bytes], list[str]]:
    """The filed drawings, from local files where they exist and remote URLs where they do not."""
    images: list[bytes] = []
    urls: list[str] = []
    directory = pilot.figure_dir(pub)
    for item in (record.get("images") or [])[:MAX_ORIGINAL_FIGURES]:
        if not isinstance(item, dict):
            continue
        name = item.get("file")
        if name and directory is not None:
            candidate = Path(directory) / str(name)
            try:
                if candidate.is_file():
                    images.append(candidate.read_bytes())
                    urls.append("")
                    continue
            except OSError:
                pass
        url = str(item.get("full") or item.get("src_url") or "")
        if url:
            urls.append(url)
            images.append(b"")
    if any(images) or any(urls):
        notes.append(f"{len([u for u in urls if u]) + len([i for i in images if i])} filed "
                     "drawing sheet(s) available for comparison")
    return images, urls


def save_original_figures(result: IngestResult, destination: Path) -> list[OriginalFigure]:
    """Write the filed sheets into the job directory and record where they went."""
    destination.mkdir(parents=True, exist_ok=True)
    figures: list[OriginalFigure] = []
    for index, blob in enumerate(result.original_images):
        url = result.original_urls[index] if index < len(result.original_urls) else ""
        filename = ""
        if blob:
            filename = f"original_{index:03d}.png"
            (destination / filename).write_bytes(blob)
        figures.append(OriginalFigure(index=index, filename=filename, url=url))
    result.document.original_figures = figures
    return figures
