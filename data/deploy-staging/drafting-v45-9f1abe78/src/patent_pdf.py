"""Layout-aware text extraction from an uploaded patent PDF.

WHY THIS EXISTS. The front door used to shell out to bare ``pdftotext``, which is wrong for
patents in two measured ways:

  1. **Two-column pages interleave.** A granted US patent is set in two columns. poppler's
     reading order walks the page line by line, so the left column's line 3 is emitted next to
     the right column's line 3 and every sentence in the specification -- and every claim --
     comes out shredded. Measured on US-11338449-B2 (a normal 64-page US grant): the old path
     reported 12 claims where the cover page says 20, and "claim 1" was 281 characters of two
     columns spliced together mid-sentence.
  2. **Image-only PDFs yield nothing at all.** Many Google Patents PDFs are CCITT-G4 scans with
     no text layer (US-12059161-B2, US-20240298859-A1 both measured at *zero* characters).
     The old path silently produced an empty document and the search then ran on whatever the
     user had typed, or on nothing.

What this module does instead:

  * reconstructs COLUMNS from word bounding boxes (``pdftotext -bbox-layout``) rather than
    trusting reading order, so a claim reads as one continuous string;
  * strips the running header/footer band and the marginal line numbers that USPTO prints every
    five lines, both of which otherwise land inside claim text;
  * re-joins words hyphenated across a line break;
  * reports, per page, whether a text layer was actually present, so the caller can decide to
    fall back to a vision transcription (:mod:`patent_doc`) instead of searching on emptiness.

It deliberately does NOT do OCR itself. Local tesseract on the shared host measured slower than
30 s/page, which is minutes of CPU for one upload on a box that also serves the app; the vision
fallback in :mod:`patent_doc` reads the same 67-page scan in ~32 s off-box and returns structure,
not just glyphs.
"""
from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET

NS = "{http://www.w3.org/1999/xhtml}"

HEADER_BAND = 0.045          # fraction of page height treated as running header
FOOTER_BAND = 0.965          # ... and the start of the running footer
Y_TOLERANCE = 4.0            # pt: words within this vertical distance are one line
MIN_COL_WORDS = 20           # a column must hold this many words to be believed
STRADDLE_MAX = 0.02          # fraction of words allowed to cross the gutter in a 2-col page
TEXT_LAYER_MIN_CHARS = 400   # whole-document text below this = treat as an image-only scan
PAGE_TEXT_MIN_CHARS = 120    # a page below this contributed no usable text layer
PDFTOTEXT_TIMEOUT = 120


def _run(cmd, timeout=PDFTOTEXT_TIMEOUT):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "ignore")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# word -> line -> column reconstruction
# ---------------------------------------------------------------------------
def _page_words(page):
    """[(xMin, yMin, xMax, yMax, text)] for one <page>, blank words dropped."""
    out = []
    for w in page.iter(NS + "word"):
        t = (w.text or "").strip()
        if not t:
            continue
        try:
            out.append((float(w.get("xMin")), float(w.get("yMin")),
                        float(w.get("xMax")), float(w.get("yMax")), t))
        except (TypeError, ValueError):
            continue
    return out


def _group_lines(words, ytol=Y_TOLERANCE):
    """Words -> [(yMin, xMin, xMax, [words])] grouped by baseline, each ordered left to right.

    Grouping is done here rather than trusting poppler's own <line> elements because poppler
    joins across the gutter: on a two-column page its <line> already contains both columns, so
    column separation is impossible at that granularity.
    """
    rows = []
    cur = []
    y_ref = None
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if y_ref is None or abs(w[1] - y_ref) <= ytol:
            cur.append(w)
            y_ref = w[1] if y_ref is None else (y_ref + w[1]) / 2.0
        else:
            rows.append(cur)
            cur = [w]
            y_ref = w[1]
    if cur:
        rows.append(cur)
    lines = []
    for r in rows:
        r = sorted(r, key=lambda w: w[0])
        lines.append((min(w[1] for w in r), r[0][0], max(w[2] for w in r), r))
    return lines


_LINE_NO = re.compile(r"^\d{1,3}$")


_ENUM_PUNCT = (".", ")", "．", "、", "。")


def _strip_line_numbers(lines):
    """Drop the marginal line numbers a specification prints every five lines.

    Left in place they land INSIDE claim text ("...adjacent an operator 5 side of the first
    platform..."), corrupting both the displayed claim and its embedding.

    The two margins are treated differently, because a bare integer at the LEFT margin is
    ambiguous: USPTO and EPO both number lines there in some layouts, but a CN or EP claims
    section prints the CLAIM NUMBER in exactly that position. Stripping the left margin
    indiscriminately deleted every claim number of CN-113413479-B and left the claims section
    as a run of sentences beginning with a bare full stop. So at the left margin only a
    multiple of five that is NOT followed by enumeration punctuation is dropped; at the right
    margin, where no claim number ever appears, any lone integer is.
    """
    if not lines:
        return lines
    left_edge = min(l[1] for l in lines)
    right_edge = max(l[2] for l in lines)
    span = max(1.0, right_edge - left_edge)
    out = []
    for y, x0, x1, words in lines:
        keep = []
        for i, w in enumerate(words):
            if len(words) > 1 and _LINE_NO.match(w[4]):
                if (w[0] - left_edge) / span > 0.93:
                    continue                                    # right margin: always a line no.
                if ((w[2] - left_edge) / span < 0.07
                        and int(w[4]) % 5 == 0
                        and not (i + 1 < len(words)
                                 and words[i + 1][4].startswith(_ENUM_PUNCT))):
                    continue                                    # left margin: line no., not claim no.
            keep.append(w)
        if keep:
            out.append((y, keep[0][0], max(w[2] for w in keep), keep))
    return out


def _split_columns(words, page_width):
    """Return [column_words, ...] — two columns when the page really is two columns, else one.

    The test is the gutter: on a genuine two-column page almost no word crosses the midline and
    both halves carry real text. A single-column page (front page, drawing sheet, EP/WO layout)
    fails one of those and is left alone.
    """
    mid = page_width / 2.0
    straddle = sum(1 for w in words if w[0] < mid < w[2])
    left = [w for w in words if w[2] <= mid]
    right = [w for w in words if w[0] >= mid]
    total = len(left) + len(right)
    if (straddle <= STRADDLE_MAX * max(1, len(words))
            and len(left) >= MIN_COL_WORDS and len(right) >= MIN_COL_WORDS
            and total and 0.25 <= len(left) / total <= 0.75):
        return [left, right]
    return [words]


_HYPHEN_END = re.compile(r"(\w)[-‐‑]$")


def _join_lines(lines):
    """Lines -> page text, re-joining words hyphenated across the break ("orienta-\\ntion")."""
    parts = []
    for _, _, _, words in lines:
        parts.append(" ".join(w[4] for w in words))
    text = ""
    for i, ln in enumerate(parts):
        if not text:
            text = ln
            continue
        if _HYPHEN_END.search(text):
            text = _HYPHEN_END.sub(r"\1", text) + ln.lstrip()
        else:
            text += "\n" + ln
    return text


def _page_text(page):
    try:
        width = float(page.get("width"))
        height = float(page.get("height"))
    except (TypeError, ValueError):
        return ""
    words = [w for w in _page_words(page)
             if HEADER_BAND * height < w[1] < FOOTER_BAND * height]
    if not words:
        return ""
    chunks = []
    for col in _split_columns(words, width):
        chunks.append(_join_lines(_strip_line_numbers(_group_lines(col))))
    return "\n".join(c for c in chunks if c)


# ---------------------------------------------------------------------------
# public entrypoint
# ---------------------------------------------------------------------------
def page_texts(path):
    """PDF path -> list of per-page strings, columns reconstructed. [] if poppler fails."""
    xml = _run(["pdftotext", "-bbox-layout", "-q", path, "-"])
    if not xml.strip():
        return []
    xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml, count=1)
    xml = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml)     # OCR debris breaks the parser
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [_page_text(p) for p in root.iter(NS + "page")]


def plain_text(path):
    """poppler's own reading order — the pre-existing behaviour, kept as a safety net."""
    return _run(["pdftotext", "-q", "-nopgbrk", path, "-"])


def extract(path):
    """Extract a patent PDF's text.

    Returns ``{"text", "pages", "n_pages", "text_layer", "empty_pages", "method", "notes"}``.
    ``text_layer`` is False when the PDF is an image-only scan, which is the caller's signal to
    fall back to a vision transcription rather than search on an empty string.
    """
    notes = []
    pages = page_texts(path)
    method = "columns"
    if not pages:
        flat = plain_text(path)
        pages = [p for p in flat.split("\f")] if flat else []
        method = "reading-order"
        if pages:
            notes.append("layout analysis unavailable; used poppler reading order")

    text = "\n\n".join(p for p in pages if p and p.strip()).strip()
    # A column reconstruction that produces materially less text than poppler's own reading
    # order means the bbox pass misread the page; prefer whichever recovered more of the
    # document rather than silently shipping the thinner one.
    if method == "columns":
        flat = plain_text(path).strip()
        if len(flat) > 1.15 * len(text) and len(flat) > TEXT_LAYER_MIN_CHARS:
            text = flat
            pages = flat.split("\f")
            method = "reading-order"
            notes.append("column reconstruction recovered less text than reading order; used reading order")

    empty = sum(1 for p in pages if len((p or "").strip()) < PAGE_TEXT_MIN_CHARS)
    text_layer = len(text) >= TEXT_LAYER_MIN_CHARS
    if not text_layer:
        notes.append("this PDF has no usable text layer (a scanned image); read it with vision instead")
    return {"text": text, "pages": pages, "n_pages": len(pages), "text_layer": text_layer,
            "empty_pages": empty, "method": method, "notes": notes}
