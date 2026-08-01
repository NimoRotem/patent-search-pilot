"""Front-door document / patent-link ingestion for the search page.

Turns one of three inputs — a dropped/uploaded file, or a pasted Google Patents / Espacenet
URL (or bare publication number) — into search material fed into the EXISTING two-tier
pipeline. It produces, from the SAME document, THREE things (not just a brief):

  1. a full-document SEARCH BRIEF (summary) — llm.condense_for_search, as before
  2. FULL-TEXT QUERY CHUNKS — the document chunked the SAME WAY the corpus is chunked
     (chunker.py kinds/sizing), each embedded with the SAME model as the corpus but with the
     RETRIEVAL_QUERY task type, so every strong chunk can retrieve its own neighbours
     (multi-chunk query-by-example) instead of collapsing the whole document to one vector.
  3. FIGURE IMAGES — the extracted drawing PNGs, exposed cleanly (base64) for the image-search
     channel, PLUS a Gemini vision description folded into the brief text (as before).

What is reused rather than reinvented:
  * text extraction        : pdftotext (mirrors patents-app/app.py:/api/extract_pdf), python-docx
  * search-brief condensing: llm.condense_for_search
  * CORPUS-PARITY chunking : chunker._clip / chunker._tok / chunker.MAX_CHARS are reused directly
                             so query chunks are byte-for-byte comparable to corpus chunks.
  * embedding              : embed.embed_texts(..., task_type="RETRIEVAL_QUERY") — same model,
                             same 768 dims as the corpus (embed.py), query task type.
  * drawing extraction     : drawings.figures_from_pdf (the recently-calibrated pipeline)
  * multi-source drawings  : enrich_display.enrich_for_display -> OPS/Espacenet recovery for
                             publications Google has no figures for (e.g. DE-1286275-B)
  * vision fusion          : llm.describe_figures (Gemini 2.5-flash, multimodal, already wired)

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

# --- full-text query chunking / image-channel caps -------------------------
MAX_QUERY_CHUNKS = 48             # cap on embedded query chunks (see build_query_chunks docstring)
IMG_MAX = 6                       # full-res figure images exposed (base64) to the image channel
MIN_PARA_CHARS = 80               # a body paragraph shorter than this is header/footer noise
EMBED_DIM = 768                   # MUST match the corpus embedding dim (config.EMBED_DIM / embed.py)


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
    """Legacy plain extraction, kept for callers that only want a string."""
    return _pdf_read(data).get("text", "")


def _pdf_read(data: bytes) -> dict:
    """Layout-aware read of an uploaded PDF -> patent_pdf.extract()'s dict.

    Column reconstruction matters more here than anywhere else in the app: this is the text the
    claims are split out of and the brief is written from, and poppler's reading order splices
    the two columns of a granted patent together mid-sentence.
    """
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        import patent_pdf
        return patent_pdf.extract(path)
    except Exception:
        return {"text": "", "pages": [], "n_pages": 0, "text_layer": False,
                "empty_pages": 0, "method": "none", "notes": ["could not read this PDF"]}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


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


def _figure_images_b64(figures: list[bytes], cap: int = IMG_MAX) -> list[dict]:
    """Full-resolution drawing PNGs as JSON-safe base64 records for the IMAGE-SEARCH channel.

    Distinct from `thumbs` (small JPEG previews for the UI): these are the originals task A's
    image channel embeds. Kept JSON-serialisable because the /extract route jsonify()s the whole
    result. Capped (IMG_MAX) to bound the payload; the integrator MAY instead stash the raw bytes
    server-side and pass only a handle — see the return-object docs."""
    out: list[dict] = []
    for fb in (figures or [])[:cap]:
        if not fb:
            continue
        try:
            out.append({"mime": "image/png", "b64": base64.b64encode(fb).decode("ascii")})
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# corpus-parity chunking of the QUERY document
#
# The corpus is chunked by chunker.py into kinds {whole|abstract|claim_own|claim_resolved|
# paragraph|figure_caption}, each clipped by chunker._clip (MAX_CHARS=8000, `whole` to 2000) and
# sized by chunker._tok. We reuse those SAME functions here so a query chunk and the corpus chunk
# of the same text are byte-identical — that is what makes the RETRIEVAL_QUERY vectors comparable
# to the RETRIEVAL_DOCUMENT vectors already in pgvector.
#
# Query-side differences (honest): we emit claim_own but NOT claim_resolved — resolving a
# dependent claim against its parent is a corpus-ingest step we do not run on an ad-hoc query
# document; claim_own already carries the claim text. figure_caption only appears when the source
# actually supplies captions (SerpApi details do not, so link-mode has none).
# ---------------------------------------------------------------------------
try:                                    # reuse the corpus chunker's exact clip/token functions
    import chunker as _chunker
    _clip = _chunker._clip
    _tok = _chunker._tok
    _MAXC = _chunker.MAX_CHARS
except Exception:                       # identical fallback if chunker's heavy imports are absent
    _MAXC = 8000

    def _clip(s, n=_MAXC):
        s = (s or "").strip()
        return s[:n] if len(s) > n else s

    def _tok(s):
        return max(1, len(s) // 4)


_ABSTRACT_HDR = re.compile(r"(?im)^\s*abstract\b.*$")


def _is_independent_claim(txt: str) -> bool:
    """A claim is dependent if it references another claim ('according to claim 3', 'of claim 1').
    Used only to PRIORITISE which claims survive the chunk cap — independent claims first."""
    import patent_doc
    return patent_doc.is_independent(txt)


def _segment_text(text: str):
    """Structure recovery from free document text -> (title, abstract, claims, paragraphs).

    Delegates to :func:`patent_doc.segment`, which handles what the original inline heuristic
    here did not: a claims section that runs into "1." on the same line, claim numbers that must
    appear IN SEQUENCE (so "the system of claim 10" cannot start a new claim), CN and EP
    publications that print the claims BEFORE the description, and the spacing an OCR text layer
    inserts around punctuation. Claims come back as "N. <text>" strings, the shape this module's
    callers expect.
    """
    import patent_doc
    s = patent_doc.segment(text or "")
    claims = ["%s. %s" % (c["claim_no"], c["text"]) for c in (s.get("claims") or [])]
    return s.get("title", ""), s.get("abstract", ""), claims, s.get("paragraphs", [])


def build_query_chunks(title: str = "", abstract: str = "", claims=None, paragraphs=None,
                       figure_captions=None, source_text: str = "",
                       cap: int = MAX_QUERY_CHUNKS) -> list[dict]:
    """Chunk a QUERY document exactly as the corpus is chunked. Returns a list of
    {kind, text, coord, token_count} dicts (NO vectors — call embed_query_chunks to add them).

    Provide structured fields (title/abstract/claims/paragraphs/figure_captions) when the source
    gives them (patent-link mode). Otherwise pass source_text and it is segmented heuristically.

    CAP + PRIORITY (bounds embedding + downstream per-chunk pgvector scans): if the document
    yields more than `cap` chunks (a long spec with dozens of claims/paragraphs), keep the MOST
    INFORMATIVE first — independent claims, then dependent claims, then abstract, then the whole,
    then the longest paragraphs, then figure captions. cap=48 comfortably fits every real patent's
    claims + abstract + key paragraphs in one Vertex batch (SUB=200) and one dense scan per chunk;
    only pathological documents drop their least-informative tail (reported in the chunk count)."""
    claims = list(claims or [])
    paragraphs = list(paragraphs or [])
    figure_captions = list(figure_captions or [])
    if not (title or abstract or claims or paragraphs) and source_text:
        st, sa, sc, sp = _segment_text(source_text)
        title = title or st
        abstract, claims, paragraphs = sa, sc, sp

    rows: list[dict] = []
    whole = _clip(((title or "") + (". " + abstract if abstract else "")), 2000)
    if whole:
        rows.append({"kind": "whole", "text": whole, "coord": None,
                     "token_count": _tok(whole), "independent": None})
    if abstract:
        a = _clip(abstract)
        rows.append({"kind": "abstract", "text": a, "coord": None,
                     "token_count": _tok(a), "independent": None})
    for i, c in enumerate(claims):
        own = _clip(str(c))
        if own:
            rows.append({"kind": "claim_own", "text": own, "coord": {"claim_no": i + 1},
                         "token_count": _tok(own), "independent": _is_independent_claim(own)})
    for i, p in enumerate(paragraphs):
        t = _clip(str(p))
        if t:
            rows.append({"kind": "paragraph", "text": t, "coord": {"para_no": i + 1},
                         "token_count": _tok(t), "independent": None})
    for cpt in figure_captions:
        t = _clip(str(cpt))
        if t:
            rows.append({"kind": "figure_caption", "text": t, "coord": None,
                         "token_count": _tok(t), "independent": None})

    if len(rows) > cap:
        # Most-informative first: independent claims, then the summary chunks (abstract, whole),
        # then dependent claims, then longest paragraphs, then captions. Keeping abstract/whole
        # ABOVE dependent claims means a claim-heavy spec never drops its summary material.
        def _rank(r):
            if r["kind"] == "claim_own":
                return 0 if r.get("independent") else 3
            return {"abstract": 1, "whole": 2, "claim_resolved": 4,
                    "paragraph": 5, "figure_caption": 6}.get(r["kind"], 9)
        rows.sort(key=lambda r: (_rank(r), -len(r["text"])))
        rows = rows[:cap]
    return rows


def embed_query_chunks(chunks: list[dict], dim: int = EMBED_DIM) -> list[dict]:
    """Embed query chunks IN PLACE with the corpus model (Vertex gemini-embedding-001) at the
    corpus dim, but with task_type=RETRIEVAL_QUERY (asymmetric retrieval — corpus used
    RETRIEVAL_DOCUMENT). Adds a `vector` key to each chunk (a 768-float list, or None on failure).
    Fail-soft and batched: one Vertex call for all chunks (<=SUB per sub-batch inside embed)."""
    if not chunks:
        return chunks
    vecs = None
    try:
        import embed as _embed
        vecs = _embed.embed_texts([c["text"] for c in chunks], dim, task_type="RETRIEVAL_QUERY")
    except Exception:
        vecs = None
    for i, c in enumerate(chunks):
        c["vector"] = vecs[i] if (vecs and i < len(vecs)) else None
    return chunks


# ---------------------------------------------------------------------------
# brief assembly (text + full-text chunks + vision fusion + image exposure)
# ---------------------------------------------------------------------------
def _build(text: str, figures: list[bytes], source: str, label: str, notes: list[str],
           pub: str | None = None, drawings_source: str | None = None,
           struct: dict | None = None, embed_chunks: bool = True,
           analysis: dict | None = None, on_stage=None) -> dict:
    """Assemble the search object from a document's text + figures.

    Produces, from the one document, the material the search runs on AND the material the review
    panel shows the user for correction:
      - abstract      : the applicant's abstract, on its own
      - claims        : each claim on its own, numbered, independent ones marked
      - brief         : a search brief condensed from the WHOLE file
      - chunks        : corpus-parity full-text chunks, each embedded (RETRIEVAL_QUERY)
      - figure_images : the drawing PNGs (base64) for the image-search channel

    `analysis` is a :func:`patent_doc.analyze` result and is the preferred input — it carries a
    verified structure and a whole-document brief. Without it the older path runs
    (``llm.condense_for_search`` over the head of the document), which is what the link mode and
    the hermetic unit tests use. `struct` (link mode) carries {title, abstract, claims,
    figure_captions} straight from the detail path. `embed_chunks=False` skips the Vertex call.
    """
    import llm

    def stage(key, msg):
        if on_stage:
            try:
                on_stage(key, msg)
            except Exception:
                pass

    text = re.sub(r"[ \t]+", " ", text or "").strip()
    have_text = len(text) >= 40
    disclosure = title = ""
    if analysis:
        disclosure = analysis.get("brief") or ""
        title = analysis.get("title") or ""
        if have_text:
            notes.append(f"extracted {len(text):,} chars of text" + (f" from {label}" if label else ""))
    elif have_text:
        cond = llm.condense_for_search(text)
        disclosure = cond.get("disclosure", "")
        title = cond.get("title", "")
        notes.append(f"extracted {len(text):,} chars of text" + (f" from {label}" if label else ""))
    elif struct and (struct.get("abstract") or struct.get("claims")):
        # link mode: text may be short but the detail path gave structured abstract/claims
        title = struct.get("title") or ""
    else:
        notes.append("no usable text extracted")

    vision = ""
    thumbs: list[str] = []
    if figures:
        notes.append(f"{len(figures)} figure(s) extracted")
        stage("vision", f"reading the {len(figures)} extracted drawing(s)")
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
        # Be explicit about what this is: figures folded into the QUERY TEXT (for the text
        # channels). The raw images go to the IMAGE channel separately (figure_images below).
        parts.append("Drawings (figures analysed and folded into the query text; the raw drawings "
                     "are ALSO sent to the image-similarity channel): " + vision)
        notes.append("figures analysed and folded into the query text")
    brief = "\n\n".join(parts).strip()

    # Nothing usable ONLY if there is neither query text NOR any figure to image-search.
    if not brief and not figures:
        return {"ok": False,
                "error": "could not extract usable text or figures from this document",
                "status": 422}

    # --- FULL-TEXT QUERY CHUNKS: chunk the SAME way the corpus is chunked --------------------
    abstract = ""
    claim_records: list[dict] = []
    if analysis:
        abstract = analysis.get("abstract") or ""
        claim_records = list(analysis.get("claims") or [])
        chunks = build_query_chunks(
            title=title, abstract=abstract,
            claims=[c.get("text", "") for c in claim_records],
            paragraphs=analysis.get("paragraphs") or [],
            figure_captions=analysis.get("figure_captions") or [])
        # keep the analysed independence flag rather than re-deriving it per chunk
        for c in chunks:
            if c["kind"] == "claim_own":
                i = (c.get("coord") or {}).get("claim_no")
                if i and 1 <= i <= len(claim_records):
                    c["independent"] = bool(claim_records[i - 1].get("independent"))
    elif struct is not None:
        abstract = struct.get("abstract") or ""
        chunks = build_query_chunks(
            title=struct.get("title") or title,
            abstract=abstract,
            claims=struct.get("claims") or [],
            paragraphs=[],                       # SerpApi detail path has no description paragraphs
            figure_captions=struct.get("figure_captions") or [])
        claim_records = [{"claim_no": i + 1, "text": str(c),
                          "independent": _is_independent_claim(str(c))}
                         for i, c in enumerate(struct.get("claims") or [])]
    else:
        st, sa, sc, sp = _segment_text(text)
        abstract = sa
        chunks = build_query_chunks(title=title or st, abstract=sa, claims=sc, paragraphs=sp)
        claim_records = [{"claim_no": i + 1, "text": str(c),
                          "independent": _is_independent_claim(str(c))}
                         for i, c in enumerate(sc)]
    if chunks and embed_chunks:
        stage("embed", f"preparing {len(chunks)} query vectors")
        embed_query_chunks(chunks)
        embedded = sum(1 for c in chunks if c.get("vector"))
        notes.append(f"{len(chunks)} query chunk(s); {embedded} embedded (RETRIEVAL_QUERY @{EMBED_DIM}d)")
    elif chunks:
        for c in chunks:
            c.setdefault("vector", None)
        notes.append(f"{len(chunks)} query chunk(s) produced (not embedded)")

    figure_images = _figure_images_b64(figures)

    return {
        "ok": True, "source": source, "label": label, "pub": pub, "title": title,
        # Kept server-side by webapp._stash_doc and removed before /extract responds.  The search
        # intentionally runs on the condensed brief + bounded vectors, but a later drafting
        # workspace must receive the inventor's verbatim upload rather than that lossy summary.
        "full_text": text[:240_000],
        # legacy single-string query material (current text pipeline consumes this):
        "brief": brief, "text_chars": len(text), "text_snippet": text[:600],
        "text_present": have_text, "vision": vision, "figures_present": bool(figures),
        "n_figures": len(figures), "thumbs": thumbs, "drawings_source": drawings_source,
        # NEW multi-material contract (the user's requirement):
        "summary_brief": disclosure,               # full-document summary (kept, in addition to chunks)
        "chunks": chunks,                          # [{kind,text,coord,token_count,independent,vector}]
        "n_chunks": len(chunks),
        "n_claims": sum(1 for c in chunks if c.get("kind") == "claim_own"),
        "figure_descriptions": vision,             # vision text (alias of `vision`)
        "figure_images": figure_images,            # [{mime,b64}] full-res drawings for image search
        "notes": notes,
        # --- the REVIEW material: what the user is shown and can correct before searching ----
        "abstract": abstract,
        "claims": claim_records,                   # [{claim_no,text,independent}]
        "n_independent": sum(1 for c in claim_records if c.get("independent")),
        "publication_number": (analysis or {}).get("publication_number", "") or (pub or ""),
        "language": (analysis or {}).get("language", ""),
        "abstract_source": (analysis or {}).get("abstract_source", "" if analysis else "layout"),
        "claims_source": (analysis or {}).get("claims_source", "layout"),
        "keywords": (analysis or {}).get("keywords", []),
        # False when the document had no text layer and was read by vision, so nothing could be
        # checked against a source. The UI says so and asks the user to read the claims.
        "verified": bool((analysis or {}).get("verified", True)),
    }


# ---------------------------------------------------------------------------
# public entrypoints
# ---------------------------------------------------------------------------
def extract_upload(data: bytes, filename: str, on_stage=None) -> dict:
    """File upload / drag-drop -> the search + review material, or {ok:False, error, status}.

    ``on_stage(key, message)`` reports each phase so the caller can drive a progress bar; a
    64-page grant takes the better part of a minute and most of it is model time."""

    def stage(key, msg):
        if on_stage:
            try:
                on_stage(key, msg)
            except Exception:
                pass

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

    import patent_doc
    notes: list[str] = []
    figures: list[bytes] = []
    pdf_for_vision = None
    if kind == "pdf":
        stage("read", "reading the PDF and reconstructing its columns")
        read = _pdf_read(data)
        text = read.get("text", "")
        notes.extend(read.get("notes") or [])
        if read.get("n_pages"):
            notes.append(f"{read['n_pages']} page(s) read")
        if not read.get("text_layer"):
            # A scanned facsimile: there is no text to segment, so hand the PDF itself to the
            # transcription pass rather than searching on an empty string.
            pdf_for_vision = data
        stage("figures", "extracting the drawings")
        figures = _pdf_figures(data)
    elif kind == "docx":
        text = _docx_text(data)
    else:
        text = data.decode("utf-8", "ignore")

    analysis = patent_doc.analyze(text=text, pdf_bytes=pdf_for_vision, on_stage=on_stage)
    notes.extend(analysis.get("notes") or [])
    if analysis.get("n_claims"):
        notes.append("%d claim(s) extracted, %d independent"
                     % (analysis["n_claims"], analysis.get("n_independent", 0)))
    return _build(text=text, figures=figures, source="upload", label=label, notes=notes,
                  analysis=analysis, on_stage=on_stage)


def rebuild_from_edits(abstract: str = "", claims=None, brief: str = "", title: str = "",
                       paragraphs=None, figure_captions=None, embed_chunks: bool = True) -> dict:
    """Re-chunk and re-embed after the user corrects the extracted material.

    The review panel exists so a wrong abstract or a mangled claim can be fixed BEFORE the
    search spends on it. That correction has to reach the retrieval side, not just the textarea:
    every claim is its own query vector, so an edited claim must be re-embedded or the search
    still runs on the text the user rejected. Returns the same chunk contract ``_build``
    produces, ready to be re-stashed under a new token.
    """
    claims = [c for c in (claims or []) if str(c.get("text") or "").strip()]
    chunks = build_query_chunks(title=title, abstract=abstract,
                                claims=[str(c.get("text")) for c in claims],
                                paragraphs=list(paragraphs or []),
                                figure_captions=list(figure_captions or []))
    for c in chunks:
        if c["kind"] == "claim_own":
            i = (c.get("coord") or {}).get("claim_no")
            if i and 1 <= i <= len(claims):
                c["independent"] = bool(claims[i - 1].get("independent",
                                                          _is_independent_claim(str(claims[i - 1].get("text")))))
    if chunks and embed_chunks:
        embed_query_chunks(chunks)
    else:
        for c in chunks:
            c.setdefault("vector", None)
    records = [{"claim_no": i + 1, "text": str(c.get("text")),
                "independent": bool(c.get("independent",
                                          _is_independent_claim(str(c.get("text")))))}
               for i, c in enumerate(claims)]
    return {"ok": True, "chunks": chunks, "n_chunks": len(chunks), "claims": records,
            "n_claims": len(records), "abstract": abstract, "brief": brief, "title": title,
            "n_independent": sum(1 for c in records if c["independent"])}


def extract_link(raw: str, on_stage=None) -> dict:
    """Google Patents / Espacenet URL or bare pub number -> search object built from the patent's
    text + its drawings (with multi-source OPS/Espacenet recovery). The abstract + each claim are
    chunked for full-text query-by-example, in addition to the summary brief."""
    pub = parse_patent_ref(raw)
    if not pub:
        return {"ok": False,
                "error": "could not find a valid patent number in that input", "status": 400}
    import enrich_display
    if on_stage:
        try:
            on_stage("read", f"looking up publication {pub}")
        except Exception:
            pass
    try:
        disp = enrich_display.enrich_for_display(pub) or {}
    except Exception:
        disp = {}
    title = disp.get("title") or ""
    abstract = disp.get("abstract") or ""
    claims = disp.get("claims")
    claims_list = [str(c) for c in claims] if isinstance(claims, list) else (
        [str(claims)] if claims else [])
    claims_txt = "\n".join(claims_list)
    text = "\n\n".join(x for x in (title, abstract, claims_txt) if x).strip()

    figblobs: list[bytes] = []
    figdir = enrich_display.FIGDIR / pub
    for im in (disp.get("images") or [])[:IMG_MAX]:
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

    # Same three materials as the upload path, so the review panel behaves identically whichever
    # way the document arrived. The claims here come from the publication record rather than a
    # PDF, so they need no repair pass — only the brief has to be written.
    import patent_doc
    analysis = None
    if abstract or claims_list:
        records = [{"claim_no": i + 1, "text": str(c),
                    "independent": patent_doc.is_independent(str(c))}
                   for i, c in enumerate(claims_list)]
        analysis = {"title": title, "abstract": abstract, "claims": records,
                    "paragraphs": [], "figure_captions": [],
                    "publication_number": pub, "verified": True,
                    "abstract_source": "publication record" if abstract else "none",
                    "claims_source": "publication record" if records else "none",
                    "n_claims": len(records),
                    "n_independent": sum(1 for r in records if r["independent"])}
        if on_stage:
            try:
                on_stage("brief", "condensing a search brief")
            except Exception:
                pass
        brief = patent_doc.search_brief(text, analysis, notes)
        analysis["brief"] = brief.get("disclosure", "")
        analysis["keywords"] = brief.get("keywords") or []
        if brief.get("title"):
            analysis["title"] = analysis["title"] or brief["title"]
    struct = {"title": title, "abstract": abstract, "claims": claims_list, "figure_captions": []}
    res = _build(text=text, figures=figblobs, source="link", label=pub, notes=notes,
                 pub=pub, drawings_source=ds, struct=struct, analysis=analysis, on_stage=on_stage)
    if res.get("ok"):
        res["google_patents"] = disp.get("google_patents")
        res["espacenet"] = disp.get("espacenet")
    return res
