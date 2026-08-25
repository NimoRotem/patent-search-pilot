"""Structured extraction of the search material from an uploaded patent.

The search is only as good as the text it is given, so the front door has to produce three
things from one uploaded file, and be able to show each of them to the user for correction:

  * **the claims, separately** — each claim as its own verbatim string, numbered, with
    independent claims marked. They are the legal definition of the invention and the strongest
    single retrieval signal in the corpus (claim chunks carry the element evidence).
  * **the abstract, separately** — the applicant's own one-paragraph statement of the invention.
  * **a condensed search brief written from the WHOLE file** — not from its first pages. A
    patent's distinguishing matter is spread across the detailed description; a brief condensed
    from the head alone reproduces the field, not the invention.

Two extraction paths feed one contract:

  ``text``   — the PDF had a text layer. :mod:`patent_pdf` reconstructs columns, then the
               deterministic segmenter below finds the claims/abstract by their headings, and a
               vision-free model pass repairs what the headings could not (OCR-mangled claim
               numbering, a claims section that starts mid-line).
  ``vision`` — the PDF is an image-only scan (measured: a large share of Google Patents PDFs
               are CCITT-G4 with zero extractable characters). The document is handed to the
               model as a PDF and transcribed.

Everything the model returns is **grounded before it is used**: a claim whose words do not
appear in the source text is rejected in favour of the deterministic reading, so a
transcription pass can repair structure but cannot invent claim scope. When there is no source
text at all (a scan), the transcription is marked ``unverified`` and the UI says so.
"""
from __future__ import annotations

import re

import llm

MAX_CLAIMS = 200
MAX_CLAIM_CHARS = 12000
MAX_ABSTRACT_CHARS = 4000
MIN_PARA_CHARS = 80

# Brief construction. A window is one model pass over a slice of the document; WINDOW_CHARS is
# sized to sit well inside the model's context with room for the instruction, and MAX_WINDOWS
# bounds a pathological 500-page file to a fixed cost. Anything beyond the cap is sampled
# evenly across the document rather than truncated from the end, and the caller is told.
WINDOW_CHARS = 24000
MAX_WINDOWS = 8
GROUND_MIN_OVERLAP = 0.55        # fraction of a model-returned claim's words found in the source

# ---------------------------------------------------------------------------
# deterministic segmentation
# ---------------------------------------------------------------------------
# Claims headings, across the offices we index and tolerant of OCR spacing ("claimed is :").
_CLAIMS_HDR = re.compile(
    r"(?im)^[^\S\n]*(?:"
    r"(?:having\s+thus\s+described[^\n:]{0,120}?,\s*)?what\s+is\s+claimed(?:\s+as\s+new[^\n:]{0,120})?(?:\s+is)?"
    r"|what\s+we\s+claim(?:\s+is)?"
    r"|(?:we|i)\s+claim"
    r"|that\s+which\s+is\s+claimed"
    r"|the\s+(?:invention|embodiments?)\s+claimed\s+is"
    r"|the\s+invention\s+is\s+claimed\s+as\s+follows"
    r"|claims?"
    r"|patentanspr(?:ü|ue)che|anspr(?:ü|ue)che"
    r"|revendications"
    r"|权利要求(?:书)?"
    r"|청구범위"
    r")[^\S\n]*[:.．。]?[^\S\n]*$")

# Same headings but allowed to run straight into "1." on the same line, which is what a
# reconstructed column often produces.
_CLAIMS_HDR_INLINE = re.compile(
    r"(?im)(?:what\s+is\s+claimed(?:\s+is)?|what\s+we\s+claim(?:\s+is)?|(?:we|i)\s+claim"
    r"|that\s+which\s+is\s+claimed|the\s+invention\s+claimed\s+is"
    r"|patentanspr(?:ü|ue)che|anspr(?:ü|ue)che|revendications|权利要求(?:书)?)"
    r"\s*[:.．。]\s*(?=\d{1,3}\s*[.)．、])")

_ABSTRACT_HDR = re.compile(
    r"(?im)^[^\S\n]*(?:\(?57\)?[^\S\n]*)?(?:abstract(?:\s+of\s+the\s+disclosure)?"
    r"|zusammenfassung|abr(?:é|e)g(?:é|e)|摘\s*要)[^\S\n]*[:.]?[^\S\n]*$")
_ABSTRACT_INLINE = re.compile(
    r"(?im)\(\s*57\s*\)\s*(?:abstract|zusammenfassung|摘\s*要)?\s*[:.]?\s*")

# "20 Claims, 36 Drawing Sheets" and friends sit directly under the front-page abstract.
_COVER_TAIL = re.compile(
    r"(?im)^\s*\d{1,3}\s+(?:claims?|anspr|revendications)\b.*$|^\s*\(\s*\d{1,2}\s*\).*$")
# Where a claims section stops. A US grant ends it with a row of asterisks; a CN or EP
# publication prints the claims FIRST and simply starts the description, so the description's
# own opening heading is the boundary. Without this the final claim swallows the entire
# specification — measured on CN-113413479-B, whose "claim 12" came out 11,997 characters long.
_END_OF_CLAIMS = re.compile(
    r"(?m)^[^\S\n]*(?:(?:\*[^\S\n]*){3,}"
    r"|description|technical\s+field|field\s+of\s+the\s+(?:invention|disclosure)"
    r"|background\s+of\s+the\s+(?:invention|disclosure)"
    r"|beschreibung|technisches\s+gebiet"
    r"|说\s*明\s*书|技术领域|背景技术"
    r")[^\S\n]*[:.．。]?[^\S\n]*$"
    r"|\[\s*0*1\s*\]|\[\s*0001\s*\]", re.I)

_FIG_CAPTION = re.compile(
    r"(?im)^[^\S\n]*(?:FIG(?:URE)?S?\.?\s*\d+[A-Za-z]?(?:\s*[-–]\s*\d+[A-Za-z]?)?)\s*[.:—-]?\s*(.{10,300})$")


_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")


def _norm_words(s):
    """Comparison tokens for the grounding guard.

    CJK is split per CHARACTER, not per run. A run-based tokeniser makes every Chinese claim a
    handful of enormous tokens ("所述至少一个气味扩散装置" as one), so any difference in where
    the OCR put a space produces zero overlap and the guard rejects a perfectly good verbatim
    transcription — measured on CN-113413479-B, where it threw away 9 of 12 claims.
    """
    out = []
    for tok in re.findall(r"[0-9a-zà-öø-ÿ]+|[^\W\d_]", (s or "").lower(), re.UNICODE):
        if _CJK.match(tok):
            out.append(tok)
        elif len(tok) > 1 or tok.isdigit():
            out.append(tok)
    return out


def _grounded(candidate, source_words, min_overlap=GROUND_MIN_OVERLAP):
    """True when `candidate`'s words are largely present in the source document.

    The guard that lets a model repair structure without inventing scope: a rephrased or
    hallucinated claim shares far fewer tokens with the source than a verbatim transcription.
    """
    cw = _norm_words(candidate)
    if len(cw) < 6:
        return False
    if not source_words:
        return False
    hit = sum(1 for w in cw if w in source_words)
    return hit / len(cw) >= min_overlap


def is_independent(text):
    """A claim is dependent when it refers back to another claim.

    German drafters put words between the preposition and the noun, and the commonest forms carry
    no claim number at all: "nach dem vorherigen Anspruch", "nach einem der vorhergehenden
    Ansprüche". Requiring "anspr" to follow "nach" immediately marked eleven of the fourteen
    claims of DE 10 2024 133 318 A1 independent, which is both false and load-bearing: the ledger
    prioritises independent claims and 112(d) hangs off the dependency.
    """
    t = (text or "")
    return not re.search(
        r"(?i)\b(?:according\s+to|as\s+(?:claimed|recited|set\s+forth)\s+in|of|in|per)?\s*"
        r"\bclaims?\s+\d|"
        r"\b(?:nach|gem(?:ä|ae)ß)\s+(?:\w+\s+){0,3}anspr(?:uch|ü?che)|"
        r"selon\s+(?:l\w+\s+)?revendication|如权利要求\s*\d|"
        r"\b(?:preceding|previous)\s+claims?\b", t)


# A claim number is "<digits><.|)|．|、>" followed by whitespace OR — in a CJK document, which
# does not space after its punctuation — directly by a non-ASCII character. Requiring one of the
# two is what keeps "1.5 mm" and "Fig. 3." out of the marker list.
_CLAIM_MARK = re.compile(
    r"(?:(?<=^)|(?<=\n)|(?<=[.;:：；。]\s)|(?<=[。；])) *(\d{1,3})[^\S\n]*[.)．、](?:[^\S\n]+|(?=[^\x00-\x7F]))",
    re.M)


def _claim_markers(blob):
    return [(m.start(), m.end(), int(m.group(1))) for m in _CLAIM_MARK.finditer(blob)
            if 1 <= int(m.group(1)) <= MAX_CLAIMS]


def _split_claims_span(blob):
    """``(claims, consumed_chars)`` — the claim list plus where the claims section ended.

    Splits on claim NUMBERS IN SEQUENCE rather than on every "<digits>." in the text. A patent
    claim is full of internal references ("the system of claim 10", "10 mm", "Fig. 3.") and a
    naive split shreds long claims into fragments; walking 1, 2, 3 ... anchors each boundary to
    the number actually expected next. A gap of one or two is tolerated (OCR drops a numeral)
    and recorded by the returned numbering, so a dropped number costs one claim's numbering
    rather than the entire tail.

    The consumed length matters because CN and EP publications print the claims BEFORE the
    description: without it the caller cannot tell where the description resumes and would
    either lose it or fold it into the last claim.
    """
    blob = (blob or "").strip()
    if not blob:
        return [], 0
    limit = len(blob)
    end = _END_OF_CLAIMS.search(blob)
    if end:
        limit = end.start()
        blob = blob[:limit]
    markers = _claim_markers(blob)
    if not markers:
        return ([{"claim_no": 1, "text": blob[:MAX_CLAIM_CHARS]}], limit) if len(blob) >= 15 else ([], 0)

    picked = []
    expect = 1
    idx = 0
    pos = 0
    while expect <= MAX_CLAIMS and idx < len(markers):
        found = None
        for j in range(idx, len(markers)):
            s, e, n = markers[j]
            if s < pos:
                continue
            if n == expect:
                found = j
                break
            if n in (expect + 1, expect + 2) and picked:
                found = j                       # OCR dropped a numeral; resync
                expect = n
                break
        if found is None:
            break
        picked.append(markers[found])
        pos = markers[found][1]
        idx = found + 1
        expect += 1

    if not picked:
        return ([{"claim_no": 1, "text": blob[:MAX_CLAIM_CHARS]}], limit) if len(blob) >= 15 else ([], 0)

    claims = []
    for k, (s, e, n) in enumerate(picked):
        stop = picked[k + 1][0] if k + 1 < len(picked) else limit
        body = re.sub(r"\s*\n\s*", " ", blob[e:stop].strip()).strip()
        if len(body) >= 15:
            claims.append({"claim_no": n, "text": body[:MAX_CLAIM_CHARS]})
    return claims, limit


def split_claims(blob):
    """A claims section -> ordered verbatim claim records ``[{claim_no, text}]``."""
    return _split_claims_span(blob)[0]


MIN_RUN_CLAIMS = 3
MIN_RUN_CHARS = 600
MAX_RUN_CANDIDATES = 60


def _content_len(s):
    """Length in Latin-equivalent characters.

    A Chinese claim says in 100 characters what English needs about 300 for, so a raw character
    threshold that is right for a US claims section silently rejects a Chinese one. Each CJK
    character counts for three.
    """
    s = s or ""
    return len(s) + 2 * sum(1 for ch in s if _CJK.match(ch))


def _find_claim_run(text):
    """Locate a claims section that carries no recognisable heading.

    Returns ``(start_offset, end_offset, claims)``. Every "1." in the document is tried as a
    section start and the longest resulting sequential run wins, so a "1." inside the
    description (a numbered list, a reference numeral) loses to the real claims, which run
    1, 2, 3 ... for pages.
    """
    best = (len(text or ""), len(text or ""), [])
    tried = 0
    for m in _CLAIM_MARK.finditer(text or ""):
        if int(m.group(1)) != 1:
            continue
        tried += 1
        if tried > MAX_RUN_CANDIDATES:
            break
        cand, used = _split_claims_span(text[m.start():])
        if (len(cand) >= MIN_RUN_CLAIMS and len(cand) > len(best[2])
                and sum(_content_len(c["text"]) for c in cand) >= MIN_RUN_CHARS):
            best = (m.start(), m.start() + used, cand)
    return best


def _clean_abstract(s):
    s = (s or "").strip()
    s = _COVER_TAIL.sub("", s).strip()
    s = re.sub(r"\s*\n\s*", " ", s)
    return s[:MAX_ABSTRACT_CHARS].strip()


def _without(text, start, stop):
    """`text` with [start:stop) removed — the claims section lifted out of the body.

    A US grant prints its claims last, so cutting to `start` loses nothing. A CN or EP
    publication prints them FIRST, and cutting to `start` would throw away the entire
    description that follows, which is exactly the material the search brief is condensed from.
    """
    return (text[:start].rstrip() + "\n\n" + text[stop:].lstrip()).strip()


def segment(text):
    """Deterministic structure from clean document text.

    Returns ``{title, abstract, claims:[{claim_no,text,independent}], paragraphs, figure_captions}``.
    Every field is best-effort: a document with no recognisable headings still returns its body
    as paragraphs so it can always be searched.
    """
    text = text or ""
    body = text
    claims = []

    hdr = _CLAIMS_HDR.search(text)
    inline = _CLAIMS_HDR_INLINE.search(text)
    # Prefer whichever heading appears LAST — a US specification names its claims section in the
    # "SUMMARY" prose long before the claims themselves begin.
    cut = None
    if hdr and inline:
        cut = (hdr.start(), hdr.end()) if hdr.start() >= inline.start() else (inline.start(), inline.end())
    elif hdr:
        cut = (hdr.start(), hdr.end())
    elif inline:
        cut = (inline.start(), inline.end())
    if cut:
        claims, used = _split_claims_span(text[cut[1]:])
        if claims:
            body = _without(text, cut[0], cut[1] + used)
        else:
            cut = None
    if not claims:
        # No heading survived. A claims section still betrays itself as a run of sequentially
        # numbered paragraphs starting at "1.". Search the WHOLE document for it rather than
        # only the tail: a US grant puts its claims last, but CN and EP publications print them
        # immediately after the cover page, before the description.
        start, stop, cand = _find_claim_run(text)
        if cand:
            claims = cand
            body = _without(text, start, stop)

    title = ""
    for ln in body.splitlines():
        s = ln.strip()
        if s and len(s) <= 200 and not _ABSTRACT_HDR.match(s):
            title = s
            break

    abstract = ""
    am = _ABSTRACT_HDR.search(body)
    if am:
        after = body[am.end():].strip()
        abstract = _clean_abstract(re.split(r"\n\s*\n", after, 1)[0])
    if not abstract:
        im = _ABSTRACT_INLINE.search(body)
        if im:
            abstract = _clean_abstract(re.split(r"\n\s*\n", body[im.end():].strip(), 1)[0])

    captions = []
    seen_caps = set()
    for m in _FIG_CAPTION.finditer(body):
        cap = m.group(0).strip()
        key = cap.lower()[:80]
        if key not in seen_caps:
            seen_caps.add(key)
            captions.append(cap[:400])
    # MIN_PARA_CHARS filters header/footer debris out of the description. Measured in
    # Latin-equivalent characters so a real Chinese paragraph — which says as much in 40
    # characters as English does in 120 — is not discarded as noise.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body)
                  if _content_len(p.strip()) >= MIN_PARA_CHARS]

    for c in claims:
        c["text"] = _tidy_ocr_spacing(c["text"])
        c["independent"] = is_independent(c["text"])
    return {"title": _tidy_ocr_spacing(title), "abstract": _tidy_ocr_spacing(abstract),
            "claims": claims[:MAX_CLAIMS], "paragraphs": paragraphs,
            "figure_captions": [_tidy_ocr_spacing(c) for c in captions[:40]]}


# ---------------------------------------------------------------------------
# model-assisted repair / transcription
# ---------------------------------------------------------------------------
_STRUCT_SYS = (
    "You are transcribing a patent document so it can be searched for prior art. Read the text "
    "and return its structure EXACTLY as written. Transcribe, do not summarise, do not "
    "paraphrase, do not correct the drafter's wording. "
    "Rules: (1) every claim is returned separately, in order, with its own number, and its text "
    "is copied VERBATIM from the document; (2) a claim that refers to another claim "
    '("the system of claim 1", "nach Anspruch 1") is dependent, otherwise independent; '
    "(3) the abstract is the applicant's abstract only, never the first paragraph of the "
    "description, and never the \"N Claims, M Drawing Sheets\" line; (4) if something is not "
    "present in the document, return it empty rather than inventing it. "
    'Return ONLY JSON: {"title":"","publication_number":"","abstract":"","language":"en",'
    '"claims":[{"claim_no":1,"text":"","independent":true}],"figure_captions":[""]}'
)

_TRANSCRIBE_USER = ("Transcribe this patent: its title, publication number, abstract, every "
                    "claim verbatim and in order, and the figure captions.")


def _coerce_claims(raw, source_words=None, require_grounding=True):
    """Model claim list -> clean records, dropping anything not grounded in the source text."""
    out = []
    rejected = 0
    for i, c in enumerate(raw or []):
        if isinstance(c, str):
            c = {"text": c}
        if not isinstance(c, dict):
            continue
        t = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
        if len(t) < 15:
            continue
        if require_grounding and not _grounded(t, source_words):
            rejected += 1
            continue
        try:
            n = int(c.get("claim_no") or (len(out) + 1))
        except (TypeError, ValueError):
            n = len(out) + 1
        ind = c.get("independent")
        out.append({"claim_no": n, "text": t[:MAX_CLAIM_CHARS],
                    "independent": is_independent(t) if ind is None else bool(ind)})
        if len(out) >= MAX_CLAIMS:
            break
    return out, rejected


def structure_from_text(text, deterministic=None):
    """Repair the deterministic segmentation of a text-layer document with one model pass.

    The model sees the head of the document (title/abstract live there) and the tail (claims
    live there), which is where every field this returns actually comes from, and its output is
    grounded against the full source before it is allowed to replace anything.
    """
    text = (text or "").strip()
    if len(text) < 200:
        return {}
    head = text[:16000]
    tail = text[-40000:] if len(text) > 56000 else text[len(head):]
    user = (_TRANSCRIBE_USER + "\n\n--- DOCUMENT (beginning) ---\n" + head +
            ("\n\n--- DOCUMENT (end, contains the claims) ---\n" + tail if tail else ""))
    d = llm.chat_json(_STRUCT_SYS, user, max_tokens=32000) or {}
    if not d:
        return {}
    source_words = set(_norm_words(text))
    claims, rejected = _coerce_claims(d.get("claims"), source_words, require_grounding=True)
    abstract = _clean_abstract(d.get("abstract"))
    if abstract and not _grounded(abstract, source_words, 0.5):
        abstract = ""
    return {"title": (d.get("title") or "").strip()[:300],
            "publication_number": (d.get("publication_number") or "").strip()[:40],
            "abstract": abstract, "claims": claims, "rejected_claims": rejected,
            "language": (d.get("language") or "").strip()[:8],
            "figure_captions": [str(x)[:400] for x in (d.get("figure_captions") or [])][:40],
            "verified": True}


def structure_from_pdf(pdf_bytes):
    """Transcribe an image-only patent PDF with the vision model.

    This is the path for scans, where there is no text layer to segment and no source text to
    ground against. The result is therefore marked ``verified: False`` and the UI presents it as
    a reading of the document that the user should check, not as extracted text.
    """
    if not pdf_bytes:
        return {}
    try:
        from google.genai.types import GenerateContentConfig, ThinkingConfig, Part
        resp = llm._client().models.generate_content(
            model=llm.AGENT_MODEL,
            contents=[Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                      _TRANSCRIBE_USER],
            config=GenerateContentConfig(system_instruction=_STRUCT_SYS,
                                         response_mime_type="application/json",
                                         temperature=0.0, max_output_tokens=32000,
                                         thinking_config=ThinkingConfig(thinking_budget=0)))
    except Exception:
        return {}
    um = getattr(resp, "usage_metadata", None)
    llm._record_usage(getattr(um, "prompt_token_count", 0) if um else 0,
                      getattr(um, "candidates_token_count", 0) if um else 0)
    try:
        import json
        d = json.loads(resp.text)
    except Exception:
        return {}
    claims, _ = _coerce_claims(d.get("claims"), None, require_grounding=False)
    return {"title": (d.get("title") or "").strip()[:300],
            "publication_number": (d.get("publication_number") or "").strip()[:40],
            "abstract": _clean_abstract(d.get("abstract")), "claims": claims,
            "language": (d.get("language") or "").strip()[:8],
            "figure_captions": [str(x)[:400] for x in (d.get("figure_captions") or [])][:40],
            "verified": False}


# ---------------------------------------------------------------------------
# the search brief — condensed from the WHOLE document
# ---------------------------------------------------------------------------
_WINDOW_SYS = (
    "You are reading ONE SECTION of a patent for a prior-art search. Pull out only what "
    "distinguishes this invention technically: components and their arrangement, the mechanism, "
    "materials, dimensions and ranges, control logic, the problem solved, the stated advantages "
    "over prior art, and the field of use. Ignore boilerplate, legal recitals, incorporation by "
    "reference, and lists of reference numerals. "
    'Return ONLY JSON: {"facts":["short technical statement", ...]} with at most 12 entries. '
    "Return an empty list if the section carries nothing technical."
)

_BRIEF_SYS = (
    "You are writing the SEARCH BRIEF that a prior-art engine will use as its query. You are "
    "given a patent's title, abstract, independent claims and technical facts gathered from "
    "EVERY section of the document. "
    "Write 200-400 words of flowing prose, no headings and no bullet points, covering: what the "
    "invention is in one or two sentences; the features of the independent claims; how it works; "
    "the materials, structures and control involved; the application; and the SYNONYMS and "
    "alternative terminology a searcher in this field would use for each key component. Write "
    "about the technology, not about the document — never mention claims, figures, embodiments, "
    "or the patent itself. Ground every statement in the material provided; add no feature that "
    "is not there. "
    'Return ONLY JSON: {"disclosure":"<the brief>","title":"<short invention title>",'
    '"keywords":["term", ...]}'
)


def _windows(text, size=WINDOW_CHARS, cap=MAX_WINDOWS):
    """Slice the document into windows. If it needs more than `cap`, sample them EVENLY across
    the document rather than taking the first `cap` — the distinguishing matter of a patent is
    usually in the detailed description, which is the part a head-truncation throws away."""
    text = text or ""
    if not text:
        return [], False
    n = (len(text) + size - 1) // size
    if n <= cap:
        return [text[i * size:(i + 1) * size] for i in range(n)], False
    step = (len(text) - size) / float(cap - 1) if cap > 1 else 0
    return [text[int(round(i * step)): int(round(i * step)) + size] for i in range(cap)], True


def _window_facts(windows):
    """Map step: technical facts per window, run concurrently. Fail-soft per window."""
    if not windows:
        return []
    if len(windows) == 1:
        d = llm.chat_json(_WINDOW_SYS, windows[0], max_tokens=1200) or {}
        return [str(x)[:400] for x in (d.get("facts") or [])][:12]
    from concurrent.futures import ThreadPoolExecutor

    def one(w):
        try:
            d = llm.chat_json(_WINDOW_SYS, w, max_tokens=1200) or {}
            return [str(x)[:400] for x in (d.get("facts") or [])][:12]
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=min(4, len(windows))) as ex:
        return [f for group in ex.map(one, windows) for f in group]


def search_brief(text, struct=None, notes=None):
    """A search brief condensed from the ENTIRE document.

    Map-reduce, because the alternative -- condensing the first N thousand characters -- reads
    a patent's boilerplate front matter and misses the detailed description where the invention
    is actually distinguished. Every window of the document contributes technical facts; the
    reduce step writes them, the abstract and the independent claims into one query.

    Fail-soft at every step: with no model available it still returns the abstract and the
    independent claims, which is a usable query.
    """
    notes = notes if notes is not None else []
    struct = struct or {}
    text = (text or "").strip()
    claims = struct.get("claims") or []
    indep = [c for c in claims if c.get("independent")] or claims[:1]
    abstract = struct.get("abstract") or ""
    title = struct.get("title") or ""

    facts = []
    if text:
        wins, sampled = _windows(text)
        facts = _window_facts(wins)
        if wins:
            notes.append("brief built from %d section%s covering %s of the document"
                         % (len(wins), "" if len(wins) == 1 else "s",
                            "an even sample" if sampled else "the whole"))
        if sampled:
            notes.append("document longer than %d characters; sections sampled evenly across it"
                         % (WINDOW_CHARS * MAX_WINDOWS))

    material = []
    if title:
        material.append("TITLE: " + title)
    if abstract:
        material.append("ABSTRACT: " + abstract)
    for c in indep[:4]:
        material.append("INDEPENDENT CLAIM %s: %s" % (c.get("claim_no"), c.get("text", "")[:4000]))
    if facts:
        material.append("TECHNICAL FACTS FROM THE WHOLE DOCUMENT:\n- " + "\n- ".join(facts[:80]))
    if not material and text:
        material.append(text[:12000])
    if not material:
        return {"disclosure": "", "title": "", "keywords": []}

    d = llm.chat_json(_BRIEF_SYS, "\n\n".join(material), max_tokens=1800) or {}
    disclosure = (d.get("disclosure") or "").strip()
    if not disclosure:
        # Deterministic fallback: abstract + independent claims is a poor brief but a real query.
        parts = [abstract] + [c.get("text", "") for c in indep[:2]]
        disclosure = "\n\n".join(p for p in parts if p).strip() or text[:4000]
        notes.append("brief model pass unavailable; used the abstract and independent claims")
    return {"disclosure": disclosure,
            "title": (d.get("title") or title or "").strip()[:300],
            "keywords": [str(k)[:80] for k in (d.get("keywords") or [])][:24]}


# ---------------------------------------------------------------------------
# one call: bytes / text -> everything the search and the review UI need
# ---------------------------------------------------------------------------
def _merge(det, model, notes):
    """Deterministic segmentation + model pass -> the structure actually used.

    The model wins on CLAIMS when it found more of them than the headings did (its whole purpose
    is recovering a claim set the headings shredded) and on the ABSTRACT when the deterministic
    pass found none. Everything else keeps the deterministic reading, which cannot hallucinate.
    """
    det = det or {}
    model = model or {}
    out = dict(det)
    dc, mc = det.get("claims") or [], model.get("claims") or []
    if mc and (len(mc) > len(dc) or (dc and _shredded(dc))):
        out["claims"] = mc
        out["claims_source"] = "model"
        notes.append("claims recovered by the transcription pass (%d, from %d found by layout)"
                     % (len(mc), len(dc)))
    else:
        out["claims"] = dc
        out["claims_source"] = "layout" if dc else "none"
    if model.get("rejected_claims"):
        notes.append("%d transcribed claim(s) rejected as not grounded in the document text"
                     % model["rejected_claims"])
    if not out.get("abstract") and model.get("abstract"):
        out["abstract"] = model["abstract"]
        out["abstract_source"] = "model"
    elif out.get("abstract"):
        out["abstract_source"] = "layout"
    else:
        out["abstract_source"] = "none"
    if not out.get("title"):
        out["title"] = model.get("title") or ""
    if model.get("title") and len(model["title"]) > 4:
        out["title"] = model["title"]          # a heading line is rarely the real title
    out["publication_number"] = model.get("publication_number") or ""
    out["language"] = model.get("language") or ""
    if not out.get("figure_captions"):
        out["figure_captions"] = model.get("figure_captions") or []
    out["verified"] = bool(model.get("verified", True))
    return out


def _shredded(claims):
    """True when a claim set looks like column-interleaving damage rather than real claims.

    The reliable tell is claim 1 being SHORT. Claim 1 is the broadest independent claim and is
    in practice the longest or near-longest claim in the set; when column interleaving splices
    the two columns together the split lands mid-sentence and claim 1 comes out a fragment while
    some later claim swallows several. Measured on US-11338449-B2 before the layout fix: claim 1
    was 281 characters and claim 7 was 1364. A merely long claim 1 is NOT damage, which is why
    the ratio is only consulted once claim 1 is already short.

    A false positive is cheap: it only means the transcription pass's claims are preferred, and
    those are grounded against the source before they can be used at all.
    """
    if len(claims) < 3:
        return False
    first = len(claims[0].get("text", ""))
    longest = max(len(c.get("text", "")) for c in claims)
    return first < 600 and longest > 3 * max(1, first)


_OCR_SPACE = (
    (re.compile(r"\s+([,;:.)\]%])"), r"\1"),
    (re.compile(r"([(\[])\s+"), r"\1"),
    (re.compile(r"(\w)\s+-\s+(\w)"), r"\1-\2"),
    (re.compile(r"[ \t]{2,}"), " "),
)


def _tidy_ocr_spacing(s):
    """Repair the spacing an OCR text layer inserts around punctuation.

    A USPTO text layer reads "the system , comprising : a pick - up position" — spacing the
    printed page does not have. It is cosmetic for display but not for retrieval: "pick - up"
    and "pick-up" are different tokens to the embedder and to BM25. Applied only to text taken
    from the layout path; the transcription path already returns correctly spaced text.
    """
    s = s or ""
    for pat, rep in _OCR_SPACE:
        s = pat.sub(rep, s)
    return s.strip()


def analyze(text="", pdf_bytes=None, use_model=True, on_stage=None):
    """Produce the search material for one uploaded document.

    Returns ``{title, abstract, claims, paragraphs, figure_captions, brief, keywords,
    verified, source_text, notes, ...}`` — the contract the ``/extract`` route hands to the
    review UI and the search pipeline.

    ``on_stage(key, message)`` is called as each phase begins. Reading a 64-page grant takes
    the better part of a minute, most of it in two model passes, and a progress bar that
    reports the phase it is actually in is the difference between waiting and wondering.
    """
    def stage(key, msg):
        if on_stage:
            try:
                on_stage(key, msg)
            except Exception:
                pass

    notes = []
    text = (text or "").strip()
    det = segment(text) if text else {"title": "", "abstract": "", "claims": [],
                                      "paragraphs": [], "figure_captions": []}
    model = {}
    if use_model:
        if text:
            stage("structure", "checking the claims and abstract against the document text")
            model = structure_from_text(text, det) or {}
            if model:
                notes.append("structure checked against the document text")
        elif pdf_bytes:
            stage("structure", "no text layer — reading the pages with vision")
            model = structure_from_pdf(pdf_bytes) or {}
            if model:
                notes.append("no text layer in this PDF; the document was read with vision "
                             "(check the claims below before searching)")
    struct = _merge(det, model, notes)

    brief_text = text
    if not brief_text:
        # Vision path: the transcription is the only text there is, so the brief is written from
        # the abstract + the full claim set rather than from a description we never received.
        brief_text = "\n\n".join([struct.get("abstract") or ""] +
                                 [c.get("text", "") for c in (struct.get("claims") or [])]).strip()
    if brief_text or struct.get("claims"):
        stage("brief", "condensing a search brief from the whole document")
        brief = search_brief(brief_text, struct, notes)
    else:
        brief = {"disclosure": "", "title": "", "keywords": []}

    struct["brief"] = brief.get("disclosure", "")
    struct["keywords"] = brief.get("keywords") or []
    if brief.get("title"):
        struct["title"] = struct.get("title") or brief["title"]
    struct["n_claims"] = len(struct.get("claims") or [])
    struct["n_independent"] = sum(1 for c in (struct.get("claims") or []) if c.get("independent"))
    struct["notes"] = notes
    return struct
