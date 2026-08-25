"""Getting the sheets out as something you can file.

A PDF here is the SVG converted page by page and stitched, so the vectors that were checked are
the vectors that get filed. Nothing is rasterised on the way out: a drawing that goes to the
Office as pixels is a drawing whose line weights are whatever the rasteriser felt like.

If the converter is missing this says so and returns nothing. It does not fall back to an image,
because a silently downgraded export is the kind of failure nobody notices until an examiner
does.
"""
from __future__ import annotations

import io
import zipfile
from typing import Iterable, Optional, Sequence


class ExportUnavailable(RuntimeError):
    """A converter this needs is not installed. Named, so it can be installed."""


def svg_to_pdf(svg: str) -> bytes:
    try:
        import cairosvg
    except Exception as exc:  # pragma: no cover - deployment dependent
        raise ExportUnavailable(
            "cairosvg is not installed, so sheets cannot be written as PDF. Install it with: "
            "pip install cairosvg") from exc
    try:
        return cairosvg.svg2pdf(bytestring=svg.encode("utf-8"))
    except Exception as exc:
        raise ExportUnavailable(f"the sheet could not be converted to PDF: "
                                f"{type(exc).__name__}: {exc}") from exc


def svg_to_png(svg: str, dpi: float = 300.0) -> bytes:
    try:
        import cairosvg
    except Exception as exc:  # pragma: no cover
        raise ExportUnavailable("cairosvg is not installed, so sheets cannot be written as "
                                "PNG.") from exc
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), dpi=dpi, background_color="white")


def sheets_pdf(svgs: Sequence[str]) -> bytes:
    """Every sheet in one PDF, in order."""
    if not svgs:
        raise ExportUnavailable("there are no sheets to export")
    pages = [svg_to_pdf(svg) for svg in svgs]
    if len(pages) == 1:
        return pages[0]
    try:
        from pypdf import PdfWriter
    except Exception as exc:  # pragma: no cover
        raise ExportUnavailable(
            "pypdf is not installed, so multi-sheet PDFs cannot be assembled. Install it with: "
            "pip install pypdf") from exc
    writer = PdfWriter()
    for page in pages:
        writer.append(io.BytesIO(page))
    out = io.BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue()


def bundle(*, sheet_svgs: Sequence[tuple[str, str]], figure_svgs: Sequence[tuple[str, str]],
           redline_html: str = "", extras: Iterable[tuple[str, bytes]] = ()) -> bytes:
    """Everything a filing needs, zipped: the sheets, the individual views, and the redline."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        try:
            archive.writestr("drawings.pdf", sheets_pdf([svg for _name, svg in sheet_svgs]))
        except ExportUnavailable as exc:
            archive.writestr("drawings-PDF-FAILED.txt", str(exc))
        for name, svg in sheet_svgs:
            archive.writestr(f"sheets/{name}", svg)
        for name, svg in figure_svgs:
            archive.writestr(f"figures/{name}", svg)
        if redline_html:
            archive.writestr("specification-redline.html", redline_html)
        for name, blob in extras:
            archive.writestr(name, blob)
    return buffer.getvalue()
