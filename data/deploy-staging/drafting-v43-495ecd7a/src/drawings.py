"""Higher-quality drawing extraction from LOCAL patent PDFs.

Adapted from the federated app's `pdf_drawings.py`, with its remote-fetch layer removed:
the federated app had no corpus, so it downloaded a PDF per request over httpx behind a
semaphore. The pilot already caches PDFs under `data/pdfs/<pub>.pdf` and serves figures
from `data/figures/<pub>/`, so this module is purely local and synchronous — no httpx, no
event loop, no API keys, no network.

What this adds over the pilot's existing `enrich_display.extract_pdf_drawings`:

  1. Works on SCANNED PDFs. The existing extractor rejects text pages by `pdftotext`
     page-length, which needs a text layer. Old EP/DE/WO facsimiles have none, so today
     every page (including the front page and the description) survives the filter and is
     served as a "figure". `image_utils.is_text_page` decides from pixels instead.
  2. Vector-only fallback. `pdfimages` returns nothing for PDFs whose drawings are vector
     art (common for modern EP). We then render pages with `pdftoppm` and detect figures
     in the raster — the existing extractor gives up and returns [].
  3. Tight cropping + multi-figure splitting. A patent sheet usually stacks Fig.1/Fig.2
     with whitespace between; both are cropped out as separate images instead of one
     mostly-white page scan.

Layering: the text-layer signal is genuinely better than pixels WHEN it exists (it is
exact, not a heuristic), so we keep it as the primary filter and fall back to the pixel
classifier only for pages where the PDF has no text layer. Best of both.

Fail-soft everywhere: any error returns [] and the caller keeps whatever figures it had.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import image_utils

MIN_DIM = 190              # px — below this it is a logo, stamp, seal or barcode
MAX_FIGS = 16
RENDER_DPI = 130
RENDER_MAX_PAGES = 25      # cap pdftoppm work on 200-page specifications
TEXT_PAGE_CHARS = 500      # chars of extractable text above which a page is prose, not a figure


def poppler_available() -> bool:
    """True if poppler-utils is installed (pdfimages + pdftoppm on PATH)."""
    return bool(shutil.which("pdfimages") and shutil.which("pdftoppm"))


def _page_text_len(pdf_path, page: int) -> int:
    """Characters of extractable text on one page. 0 means either a blank page or (much
    more often, for our problem documents) no text layer at all."""
    try:
        r = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), str(pdf_path), "-"],
                           capture_output=True, timeout=25)
        return len((r.stdout or b"").decode("utf-8", "ignore").strip())
    except Exception:
        return 0


def _pdf_has_text_layer(pdf_path, probe_pages=6) -> bool:
    """Does this PDF carry a usable text layer at all? Probes the first few pages; if none
    of them yields meaningful text the PDF is a scan and pdftotext-based page filtering is
    worthless (this is the check the existing extractor is missing)."""
    return any(_page_text_len(pdf_path, p) > 200 for p in range(1, probe_pages + 1))


def _size_ok(path) -> bool:
    """Reject logos/stamps/rule-lines by absolute size and extreme aspect ratio."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return False
    if min(w, h) < MIN_DIM:
        return False
    ar = w / max(1, h)
    return 0.2 <= ar <= 5.0


def _collect(tmpdir, prefix, text_pages, use_text_layer) -> list[bytes]:
    """Turn extracted page/image files into cropped figure PNG blobs.

    Which filter decides "is this a drawing?" depends on what evidence the PDF offers, and
    the two are ALTERNATIVES, not a chain — this was the subtle part.

      * PDF HAS a text layer (74 of 78 cached PDFs): use `pdftotext` page length. It is an
        exact measurement, not a heuristic, so nothing beats it. Crucially we then do NOT
        also run the pixel classifier: measured on real corpus drawings, a detailed
        mechanical figure (hatched cross-sections, stipple, closely-spaced leader lines)
        produces 24-46 ink transitions per row, well above the text threshold, so the pixel
        rule would throw away pages that pdftotext already proved are drawings. Running
        both filters is strictly worse than running the better one.
      * PDF has NO text layer (scanned facsimiles): pdftotext returns 0 for every page and
        can reject nothing, so the pixel classifier is the only thing standing between the
        user and a gallery full of scanned description pages. This is the case the port
        exists for.
    """
    out: list[bytes] = []
    for fn in sorted(os.listdir(tmpdir)):
        if not fn.startswith(prefix) or not fn.lower().endswith((".png", ".ppm", ".jpg", ".jpeg")):
            continue
        fp = os.path.join(tmpdir, fn)
        if not _size_ok(fp):
            continue
        m = re.match(rf"{re.escape(prefix)}-?(\d+)", os.path.splitext(fn)[0])
        page = int(m.group(1)) if m else None
        if use_text_layer and page is not None and text_pages.get(page, 0) > TEXT_PAGE_CHARS:
            continue
        try:
            with open(fp, "rb") as fh:
                raw = fh.read()
            if not fn.lower().endswith(".png"):          # normalize ppm/jpg -> png
                from PIL import Image
                import io as _io
                with Image.open(fp) as im:
                    buf = _io.BytesIO()
                    im.convert("RGB").save(buf, "PNG")
                    raw = buf.getvalue()
            if use_text_layer:
                # Already proven a drawing page by the text layer -> just crop it tightly.
                fig = image_utils.process(raw, drop_text=False)
                figs = [fig] if fig else []
            else:
                # No text layer -> pixel classifier decides, and splits multi-figure sheets.
                figs = image_utils.extract_figures(raw)
            for fig in figs:
                out.append(fig)
                if len(out) >= MAX_FIGS:
                    return out
        except Exception:
            continue
    return out


def figures_from_pdf(pdf_path) -> list[bytes]:
    """Extract drawing figures from a local PDF. Returns a list of cropped PNG blobs.

    Two-stage, mirroring the federated app:
      1. `pdfimages` — born-digital patents embed only the drawings as rasters; scanned
         patents embed one image per page and the classifier drops the text ones.
      2. `pdftoppm` — if stage 1 yields nothing usable the drawings are vector art, so
         render the pages and detect figures in the rendering.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or not poppler_available():
        return []
    try:
        with open(pdf_path, "rb") as fh:
            if not fh.read(5).startswith(b"%PDF"):
                return []
    except Exception:
        return []

    use_text_layer = _pdf_has_text_layer(pdf_path)
    text_pages: dict[int, int] = {}

    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(["pdfimages", "-png", "-p", str(pdf_path), os.path.join(td, "img")],
                           timeout=180, check=False, capture_output=True)
        except Exception:
            pass
        if use_text_layer:
            pages = set()
            for fn in os.listdir(td):
                m = re.match(r"img-(\d+)", os.path.splitext(fn)[0])
                if m:
                    pages.add(int(m.group(1)))
            text_pages = {p: _page_text_len(pdf_path, p) for p in sorted(pages)[:60]}
        figs = _collect(td, "img", text_pages, use_text_layer)
        if figs:
            return figs

        # stage 2: vector-only drawings -> render pages, then detect + crop
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(RENDER_DPI), "-l", str(RENDER_MAX_PAGES),
                            str(pdf_path), os.path.join(td, "pg")],
                           timeout=180, check=False, capture_output=True)
        except Exception:
            return []
        if use_text_layer:
            pages = set()
            for fn in os.listdir(td):
                m = re.match(r"pg-(\d+)", os.path.splitext(fn)[0])
                if m:
                    pages.add(int(m.group(1)))
            text_pages = {p: _page_text_len(pdf_path, p) for p in sorted(pages)[:60]}
        return _collect(td, "pg", text_pages, use_text_layer)


def refine_existing_figures(figdir, dry_run=True) -> dict:
    """Quality pass over figures the pilot has ALREADY extracted for one publication.

    Classifies every PNG in `figdir` and reports (or, with dry_run=False, removes) the
    ones that are text pages or blanks, and tightens the crop on the ones that are real
    figures. This is the cheap win on the existing 171k-row figure set: no re-extraction,
    no PDFs, just drop the junk that the text-layer filter could not catch on scans.

    Returns {checked, text_pages, blanks, kept, cropped}. Non-destructive by default.
    """
    figdir = Path(figdir)
    stats = {"checked": 0, "text_pages": 0, "blanks": 0, "kept": 0, "cropped": 0}
    if not figdir.is_dir():
        return stats
    for f in sorted(figdir.glob("*.png")):
        try:
            raw = f.read_bytes()
        except Exception:
            continue
        stats["checked"] += 1
        st = image_utils.page_stats(raw)
        if st.get("verdict") == "text":
            stats["text_pages"] += 1
            if not dry_run:
                f.unlink(missing_ok=True)
            continue
        if st.get("verdict") == "blank":
            stats["blanks"] += 1
            if not dry_run:
                f.unlink(missing_ok=True)
            continue
        stats["kept"] += 1
        tight = image_utils.process(raw, drop_text=False)
        if tight and len(tight) < len(raw) * 0.98 and not dry_run:
            try:
                f.write_bytes(tight)
                stats["cropped"] += 1
            except Exception:
                pass
        elif tight and len(tight) < len(raw) * 0.98:
            stats["cropped"] += 1
    return stats
