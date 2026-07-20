"""Front-door document / patent-link ingestion for the search page.

Turns one of three inputs — a dropped/uploaded file, or a pasted Google Patents / Espacenet
URL (or bare publication number) — into a rich, multi-angle SEARCH BRIEF that is fed into the
EXISTING two-tier pipeline (POST /run). It creates NO new search path: its only job is to
produce good query text FROM the document and hand it to the same fan-out a typed query uses.

What is reused rather than reinvented:
  * text extraction        : pdftotext (mirrors patents-app/app.py:/api/extract_pdf), python-docx
  * search-brief condensing: llm.condense_for_search  (mirror of the federated app's helper)
  * drawing extraction     : drawings.figures_from_pdf (the recently-calibrated pipeline)
  * multi-source drawings  : enrich_display.enrich_for_display -> OPS/Espacenet recovery for
                             publications Google has no figures for (e.g. DE-1286275-B)
  * vision fusion          : llm.describe_figures (Gemini 2.5-flash, multimodal, already wired)

HONEST SCOPE of the vision pass: the corpus is text-embedding based. Drawings improve the
search by ENRICHING THE QUERY TEXT (a technical description of the figures is fused into the
brief). They are not themselves embedded for image similarity — this is not image search.

SECURITY: the pasted URL is untrusted and is NEVER fetched. Only a strict publication number
is parsed out of it and passed to the existing adapters, which fetch from known patent hosts
only (SSRF-safe). Uploaded bytes are size-capped and content-sniffed by magic bytes; a file
that claims to be a PDF but is not is rejected. No user-supplied filename ever touches a path
or a subprocess argument — uploads go to os-generated temp files and are deleted after use.
"""
from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import tempfile

MAX_BYTES = 30 * 1024 * 1024      # matches the federated app's cap
MAX_THUMBS = 6                    # figure previews returned to the UI
VISION_MAX = 4                    # figures sent to the vision model (bounds cost)
THUMB_W = 240                     # px — keep the preview payload light


# ---------------------------------------------------------------------------
# publication-number parsing (SSRF-safe: parse only, never fetch the raw input)
# ---------------------------------------------------------------------------
def normalize_pub(s) -> str | None:
    """Any spelling of a publication number -> the corpus canonical 'CC-NUMBER[-KIND]'.

    'DE1286275B' / 'de-1286275-b' / 'DE 1286275 B' -> 'DE-1286275-B'. Returns None if the
    string is not a plausible publication number."""
    if not s:
        return None
    t = re.sub(r"[^A-Za-z0-9]", "", str(s)).upper()
    if not t or len(t) > 40:
        return None
    m = re.match(r"^([A-Z]{2})([0-9]{2,})([A-Z][0-9]{0,2})?$", t)
    if not m:
        return None
    cc, num, kind = m.groups()
    return f"{cc}-{num}-{kind}" if kind else f"{cc}-{num}"


def parse_patent_ref(raw) -> str | None:
    """Extract a canonical publication number from a Google Patents URL, an Espacenet URL, or
    a bare publication number. NEVER fetches anything — pure string parsing (SSRF guard)."""
    if not raw:
        return None
    raw = str(raw).strip()
    if len(raw) > 400:
        return None
    # Try each known shape in priority order and accept the FIRST capture that is a real
    # publication number. A pattern can match a non-pub segment (e.g. Espacenet's
    # '/patent/search/...'), so we must fall through on a failed normalisation rather than
    # stopping at the first regex hit.
    #   Google Patents : /patent/DE1286275B/en
    #   Espacenet      : ...?q=pn%3DDE1286275B  |  pn=DE1286275B  |  /publication/DE1286275B
    for pat in (r"/patent/([A-Za-z]{2}[0-9A-Za-z]+)",
                r"pn(?:%3[dD]|=)([A-Za-z]{2}[0-9A-Za-z]+)",
                r"/publication/([A-Za-z]{2}[0-9A-Za-z]+)"):
        for m in re.finditer(pat, raw):
            pub = normalize_pub(m.group(1))
            if pub:
                return pub
    # Bare publication number only. Anything URL-shaped we did not recognise above is refused
    # rather than guessed at — we must never hand an arbitrary host to a fetcher.
    if "://" in raw or "/" in raw or " " in raw:
        return None
    return normalize_pub(raw)


# ---------------------------------------------------------------------------
# upload content sniffing + text/figure extraction
# ---------------------------------------------------------------------------
def sniff_kind(data: bytes, filename: str) -> str:
    """Decide the true type from magic bytes, cross-checked against the claimed extension.

    Returns 'pdf' | 'docx' | 'txt' | 'bad_pdf' | 'bad_docx' | 'unknown'. A file that claims a
    type its bytes do not support is rejected (bad_*), never coerced."""
    name = (filename or "").lower()
    if data[:5] == b"%PDF-":
        return "pdf"
    if name.endswith(".pdf"):
        return "bad_pdf"                                  # claims PDF, no %PDF- magic
    if data[:4] == b"PK\x03\x04" and name.endswith(".docx"):
        return "docx"
    if name.endswith(".docx"):
        return "bad_docx"
    try:
        data[:8192].decode("utf-8")                       # plain text must actually decode
        return "txt"
    except Exception:
        return "unknown"


def safe_label(filename: str) -> str:
    """A DISPLAY-ONLY label from a user filename: basename, control chars stripped, capped.
    Never used as a path or a subprocess argument."""
    base = os.path.basename(str(filename or "")).replace("\\", "/").split("/")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    return base[:120] or "document"


def _pdf_text(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
        tf.write(data)
        tf.flush()
        try:
            r = subprocess.run(["pdftotext", "-q", "-nopgbrk", tf.name, "-"],
                               capture_output=True, timeout=90)
            return (r.stdout or b"").decode("utf-8", "ignore")
        except Exception:
            return ""


def _pdf_figures(data: bytes) -> list[bytes]:
    """Extract drawing figures from an uploaded PDF via the shared drawings pipeline."""
    import drawings
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return drawings.figures_from_pdf(path)
    except Exception:
        return []
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def _docx_text(data: bytes) -> str:
    try:
        import docx
    except Exception:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".docx") as tf:
        tf.write(data)
        tf.flush()
        try:
            d = docx.Document(tf.name)
            return "\n".join(p.text for p in d.paragraphs)
        except Exception:
            return ""


def _thumb(png_bytes: bytes, max_w: int = THUMB_W) -> str | None:
    """A small JPEG data: URI for a figure preview. Keeps the upload UI payload light."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(png_bytes)) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w > max_w:
                im = im.resize((max_w, max(1, int(h * max_w / w))))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=72)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# brief assembly (text + vision fusion) — the single place a brief is built
# ---------------------------------------------------------------------------
def _build(text: str, figures: list[bytes], source: str, label: str, notes: list[str],
           pub: str | None = None, drawings_source: str | None = None) -> dict:
    import llm
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    have_text = len(text) >= 40
    disclosure = title = ""
    if have_text:
        cond = llm.condense_for_search(text)
        disclosure = cond.get("disclosure", "")
        title = cond.get("title", "")
        notes.append(f"extracted {len(text):,} chars of text" + (f" from {label}" if label else ""))
    else:
        notes.append("no usable text extracted")

    vision = ""
    thumbs: list[str] = []
    if figures:
        notes.append(f"{len(figures)} figure(s) extracted")
        try:
            vision = llm.describe_figures(figures[:VISION_MAX], context=title or disclosure[:300])
        except Exception:
            vision = ""
        for fb in figures[:MAX_THUMBS]:
            t = _thumb(fb)
            if t:
                thumbs.append(t)
    else:
        notes.append("no usable figures found")

    parts = []
    if disclosure:
        parts.append(disclosure)
    if vision:
        # Be explicit about what this is: figures folded into the QUERY TEXT, not image search.
        parts.append("Drawings (figures analysed and folded into the query, not image-matched): "
                     + vision)
        notes.append("figures analysed and folded into the query")
    brief = "\n\n".join(parts).strip()

    if not brief:
        return {"ok": False,
                "error": "could not extract usable text or figures from this document",
                "status": 422}
    return {
        "ok": True, "source": source, "label": label, "pub": pub, "title": title,
        "brief": brief, "text_chars": len(text), "text_snippet": text[:600],
        "text_present": have_text, "vision": vision, "figures_present": bool(figures),
        "n_figures": len(figures), "thumbs": thumbs, "drawings_source": drawings_source,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# public entrypoints
# ---------------------------------------------------------------------------
def extract_upload(data: bytes, filename: str) -> dict:
    """File upload / drag-drop -> {ok, brief, preview...} or {ok:False, error, status}."""
    if not data:
        return {"ok": False, "error": "empty file", "status": 400}
    if len(data) > MAX_BYTES:
        return {"ok": False, "error": f"file too large (max {MAX_BYTES // (1024 * 1024)} MB)",
                "status": 413}
    label = safe_label(filename)
    kind = sniff_kind(data, filename)
    if kind == "bad_pdf":
        return {"ok": False, "error": "not a valid PDF (file does not start with %PDF-)",
                "status": 415}
    if kind == "bad_docx":
        return {"ok": False, "error": "not a valid .docx file", "status": 415}
    if kind == "unknown":
        return {"ok": False,
                "error": "unsupported file — upload a PDF, .txt or .docx", "status": 415}

    figures: list[bytes] = []
    if kind == "pdf":
        text = _pdf_text(data)
        figures = _pdf_figures(data)
    elif kind == "docx":
        text = _docx_text(data)
    else:
        text = data.decode("utf-8", "ignore")
    return _build(text=text, figures=figures, source="upload", label=label, notes=[])


def extract_link(raw: str) -> dict:
    """Google Patents / Espacenet URL or bare pub number -> brief built from the patent's
    text + its drawings (with multi-source OPS/Espacenet recovery)."""
    pub = parse_patent_ref(raw)
    if not pub:
        return {"ok": False,
                "error": "could not find a valid patent number in that input", "status": 400}
    import enrich_display
    try:
        disp = enrich_display.enrich_for_display(pub) or {}
    except Exception:
        disp = {}
    title = disp.get("title") or ""
    abstract = disp.get("abstract") or ""
    claims = disp.get("claims")
    claims_txt = "\n".join(str(c) for c in claims) if isinstance(claims, list) else str(claims or "")
    text = "\n\n".join(x for x in (title, abstract, claims_txt) if x).strip()

    figblobs: list[bytes] = []
    figdir = enrich_display.FIGDIR / pub
    for im in (disp.get("images") or [])[:MAX_THUMBS]:
        fn = im.get("file") if isinstance(im, dict) else None
        if not fn:
            continue
        p = figdir / fn
        try:
            if p.exists():
                figblobs.append(p.read_bytes())
        except Exception:
            pass

    ds = disp.get("drawings_source")
    notes = [f"resolved publication {pub}"]
    if ds:
        notes.append(f"drawings source: {ds}")
    res = _build(text=text, figures=figblobs, source="link", label=pub, notes=notes,
                 pub=pub, drawings_source=ds)
    if res.get("ok"):
        res["google_patents"] = disp.get("google_patents")
        res["espacenet"] = disp.get("espacenet")
    return res
