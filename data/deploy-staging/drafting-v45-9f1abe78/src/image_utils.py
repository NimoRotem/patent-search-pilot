"""Figure detection + cropping for patent drawings (no ML, pure pixel heuristics).

Ported from the federated app (patents-app/image_utils.py) because it solves a problem
the pilot's existing extractor cannot: telling a DRAWING page from a TEXT page on a
*scanned* PDF.

Why this matters here. `enrich_display.extract_pdf_drawings` already filters text pages,
but it does so with `pdftotext` page-text length — which only works when the PDF carries a
text layer. The pilot's problem documents are exactly the ones that DON'T: old EP/DE/WO
facsimiles are scanned images, where pdfimages emits one full-page raster per page and
pdftotext returns 0 chars for every page, so no page can be rejected and text pages are
served as "figures". These functions decide from the pixels instead, so they work on
scanned and born-digital PDFs alike.

Text-vs-drawing is decided by horizontal ink<->paper transition density: a row of text
flips ink/paper many times (dozens of glyph edges), whereas a drawing row crosses only a
few strokes. Text pages therefore have a HIGH median transitions-per-inked-row. The
threshold (>16) is inherited unchanged from the federated app, where it was tuned on real
patent PDFs.

All functions are fail-soft and dependency-light (PIL + numpy, both already installed).
They are pure: bytes in, bytes out, no I/O, no globals -> directly unit-testable.
"""
from __future__ import annotations

import io

# --- tuning constants (inherited from the federated app; do not change casually) -------
INK_THRESHOLD = 200        # grayscale value below which a pixel counts as "ink"

# Median ink<->paper transitions per inked row, above which a page is text.
#
# RECALIBRATED from the federated app's 16, which was far too aggressive for this corpus.
# Measured on real pages rendered at 130 dpi from cached PDFs (labels verified by eye):
#
#   REAL DRAWING PAGES        12, 18, 20, 22, 24, 26, 28, 28, 32, 46
#   REAL TEXT PAGES           80, 96, 136, 140, 144, 150, 154, 172
#
# Detailed mechanical drawings — hatched cross-sections, stipple fills, dense leader lines
# and reference numerals — flip ink/paper far more often than the original threshold
# assumed. At 16, seven of the ten real drawing pages above are misclassified as text and
# thrown away; the user sees an empty gallery for a patent that has eight sheets of
# figures. The observed gap between the two populations is 32..80, so 60 sits near its
# middle and classifies every page above correctly.
#
# Residual known imperfection: a patent FRONT page (biblio + abstract + representative
# figure) measures ~46 and is called a drawing. That is the benign direction — front pages
# really do carry a figure — and the size/crop filters trim the surrounding text.
TEXT_TRANSITIONS = 60
BLANK_INK_FRAC = 0.003     # below this ink fraction the page is blank
DENSE_INK_FRAC = 0.32      # above this it is solid text / a dark scan artifact
GAP_FRAC = 0.07            # white gap >7% of page height splits two figures
MAX_BANDS = 8              # max figures returned per page
MIN_CROP_PX = 120          # a crop smaller than this each way is not a figure


def _to_gray_np(png_bytes: bytes):
    """Decode to (RGB-ish PIL image, grayscale numpy array), normalizing polarity.

    POLARITY FIX (not in the federated original): `pdfimages` frequently extracts patent
    drawings as 1-bit STENCIL MASKS, where the bit is set for the painted area — i.e. the
    image comes out inverted, ink white on a black field. Measured on real corpus PDFs
    these arrive with an "ink fraction" of 0.95, so every downstream heuristic reads the
    page background as solid ink and the figure is discarded as a "dense" page. Since a
    patent page is essentially never more than half ink, a mean brightness below mid-grey
    is a reliable inversion signal; flip it before measuring anything.
    """
    import numpy as np
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(png_bytes))
    im.load()
    g = ImageOps.grayscale(im)
    if float(np.asarray(g).mean()) < 128.0:
        g = ImageOps.invert(g)
        im = ImageOps.invert(im.convert("RGB"))
    return im, np.asarray(g)


def _encode(im_rgb) -> bytes:
    buf = io.BytesIO()
    im_rgb.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def is_text_page(arr, ink=None) -> bool:
    """True if the grayscale array looks like a page of text rather than a drawing.

    `arr` is a 2-D numpy array of grayscale values. Measures ink<->paper transitions per
    row and takes the MEDIAN over inked rows only (blank margins would otherwise drag the
    average down and make every page look like a drawing).
    """
    import numpy as np
    if ink is None:
        ink = arr < INK_THRESHOLD
    h, w = arr.shape
    step = max(1, w // 1000)              # downsample very wide scans for speed
    row_ink = ink[:, ::step]
    trans = np.abs(np.diff(row_ink.astype(np.int8), axis=1)).sum(axis=1)
    inked_rows = row_ink.sum(axis=1) > (row_ink.shape[1] * 0.01)
    if inked_rows.sum() < 5:              # too little content to judge -> not text
        return False
    return float(np.median(trans[inked_rows])) > TEXT_TRANSITIONS


def page_stats(png_bytes: bytes) -> dict:
    """Diagnostics for one page image: {ok, width, height, ink_frac, median_transitions,
    is_text, verdict}. Used by tests and by the drawings QA tooling to explain a decision
    instead of just returning a bare bool."""
    try:
        import numpy as np
        _, arr = _to_gray_np(png_bytes)
    except Exception:
        return {"ok": False, "verdict": "unreadable"}
    h, w = arr.shape
    ink = arr < INK_THRESHOLD
    frac = float(ink.mean())
    step = max(1, w // 1000)
    row_ink = ink[:, ::step]
    trans = np.abs(np.diff(row_ink.astype(np.int8), axis=1)).sum(axis=1)
    inked = row_ink.sum(axis=1) > (row_ink.shape[1] * 0.01)
    med = float(np.median(trans[inked])) if inked.sum() >= 5 else 0.0
    txt = med > TEXT_TRANSITIONS and inked.sum() >= 5
    if frac < BLANK_INK_FRAC:
        verdict = "blank"
    elif frac > DENSE_INK_FRAC:
        verdict = "dense"
    elif txt:
        verdict = "text"
    else:
        verdict = "drawing"
    return {"ok": True, "width": int(w), "height": int(h), "ink_frac": frac,
            "median_transitions": med, "is_text": bool(txt), "verdict": verdict}


def process(png_bytes: bytes, drop_text: bool = False):
    """Trim whitespace around a figure so it fills the preview (zoom-in).

    Returns cropped PNG bytes, or None if the image is blank (or, with drop_text=True, if
    it is a text page). Fail-OPEN: if PIL/numpy blow up we return the original bytes
    rather than losing a figure the pilot already has.
    """
    try:
        import numpy as np
        im, arr = _to_gray_np(png_bytes)
    except Exception:
        return png_bytes
    try:
        h, w = arr.shape
        if w < 40 or h < 40:
            return None if drop_text else png_bytes
        ink = arr < INK_THRESHOLD
        frac = float(ink.mean())
        if frac < 0.002:                                  # blank
            return None
        if drop_text and (frac > DENSE_INK_FRAC or is_text_page(arr, ink)):
            return None
        ys, xs = np.where(ink)
        if len(xs) == 0:
            return None
        m = max(4, int(min(w, h) * 0.02))                 # small margin around the figure
        box = (max(0, int(xs.min()) - m), max(0, int(ys.min()) - m),
               min(w, int(xs.max()) + m), min(h, int(ys.max()) + m))
        return _encode(im.crop(box))
    except Exception:
        return png_bytes


def extract_figures(png_bytes: bytes) -> list:
    """Return tightly-cropped figure images from ONE rendered page.

    [] if the page is text / cover / blank. A sheet holding several figures is split on
    tall white gaps so each figure becomes its own image (patent sheets routinely stack
    Fig.1/Fig.2/Fig.3 vertically). Fail-CLOSED: on error return [] so a broken page never
    injects a junk "figure".
    """
    try:
        import numpy as np
        im, arr = _to_gray_np(png_bytes)
    except Exception:
        return []
    try:
        h, w = arr.shape
        if w < 120 or h < 120:
            return []
        ink = arr < INK_THRESHOLD
        frac = float(ink.mean())
        if frac < BLANK_INK_FRAC or frac > DENSE_INK_FRAC:
            return []
        if is_text_page(arr, ink):
            return []

        # split into vertical bands separated by tall white gaps
        row_has_ink = ink.sum(axis=1) > (w * 0.006)
        bands, start, gap = [], None, 0
        gap_thresh = max(12, int(h * GAP_FRAC))
        for y in range(h):
            if row_has_ink[y]:
                if start is None:
                    start = y
                gap = 0
            elif start is not None:
                gap += 1
                if gap >= gap_thresh:
                    bands.append((start, y - gap))
                    start = None
        if start is not None:
            bands.append((start, h - 1))

        out = []
        for (y0, y1) in bands:
            if (y1 - y0) < h * 0.08:                       # a stray line, not a figure
                continue
            sub = ink[y0:y1 + 1, :]
            cols = np.where(sub.sum(axis=0) > 0)[0]
            rows = np.where(sub.sum(axis=1) > 0)[0]
            if len(cols) == 0 or len(rows) == 0:
                continue
            if (cols.max() - cols.min()) < w * 0.12:       # too narrow to be a drawing
                continue
            m = max(6, int(min(w, h) * 0.015))
            box = (max(0, int(cols.min()) - m), max(0, y0 + int(rows.min()) - m),
                   min(w, int(cols.max()) + m), min(h, y0 + int(rows.max()) + m))
            crop = im.crop(box)
            if crop.size[0] >= MIN_CROP_PX and crop.size[1] >= MIN_CROP_PX:
                out.append(_encode(crop))
            if len(out) >= MAX_BANDS:
                break
        return out
    except Exception:
        return []
