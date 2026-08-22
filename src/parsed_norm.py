"""Normalise a fetched publication into claims, paragraphs, abstract, title and figure captions.

THE FAILURE THIS EXISTS TO CATCH
--------------------------------
Measured on US 2025/0033224 A1, the application a patent attorney actually attacked: the source
record came back as TWO items, claims 1 to 19 glued into one 17,309-character blob and claim 20 on
its own. Positional numbering turned that into a "claim 1" that was nineteen claims and a
"claim 2" that was claim 20, and the downstream limitation splitter only ever sees the first 4,000
characters of a claim, so claims 5 through 19 were invisible to the entire pipeline. Nothing
raised. The ledger simply had two rows and the subject was lost.

So this module does not trust the shape it is handed. It re-splits the claims text with two
independent splitters, takes the higher yield, and then ASSERTS:

  * the count agrees with the count the source declared, when the source declared one;
  * the numbering is a contiguous 1..N run;
  * no claim body still contains a sequential run of later claim numbers (the glued blob);
  * no claim sits exactly on a known truncation boundary (4,000 / 8,000 / 12,000 characters),
    which is what a silent clip looks like from the outside.

A document that fails any of those is REJECTED with a reason and staged for nobody. A wrong
publication in the index is worse than a missing one, because it is invisible.

WHAT IT IS CONSISTENT WITH
--------------------------
`patent_text.split_paragraphs`, `patent_text.figure_captions` and `patent_text.resolve_claims` are
the same functions `ingest_pg` and `enrich` used to build the corpus's `paragraphs`, `figures` and
`claims` rows, so paragraph boundaries and resolved claim text from this parser match the corpus
rather than merely resembling it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

import patent_text as pt

MAX_CLAIMS = 200
MAX_PARAGRAPHS = 1200
MAX_FIGURES = 60

# A claim whose length is EXACTLY one of these was clipped by something upstream, not written that
# way. 4,000 is the limitation splitter's window, 8,000 the chunker's, 12,000 patent_doc's own
# per-claim cap. Exact equality at a round boundary has effectively no false-positive rate; a
# claim that happens to be 12,001 characters long is not flagged and a 12,000 one is.
TRUNCATION_BOUNDARIES = (4000, 8000, 12000)

# How long a run of later claim numbers inside one claim body counts as "this is several claims
# glued together". Two is not enough: a numbered sub-list inside a claim can accidentally produce
# a pair. Three consecutive numbers starting at exactly the next expected claim, whose last member
# implies more claims than we hold, is the glued blob and not a coincidence.
GLUE_RUN_MIN = 3


class ParseRejected(Exception):
    """The document could not be normalised safely. `.code` names which assertion failed."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass
class ParsedDoc:
    publication_number: str
    title: str = ""
    abstract: str = ""
    abstract_lang: str = "en"
    abstract_orig: str = ""
    abstract_orig_lang: str = ""
    lang: str = "en"
    claims: list = field(default_factory=list)       # claim_no, text, lang, is_independent,
    paragraphs: list = field(default_factory=list)   # parents, resolved_text
    figures: list = field(default_factory=list)
    declared_claim_count: int | None = None
    source: str = ""
    notes: list = field(default_factory=list)
    claim_defect: dict | None = None      # set when the claims were refused and the rest kept

    def counts(self):
        return {"claims": len(self.claims), "paragraphs": len(self.paragraphs),
                "figures": len(self.figures), "abstract": 1 if self.abstract else 0}


# --------------------------------------------------------------------------- shape flattening

def _text(v) -> str:
    """Everything upstream calls "the claims" is one of four shapes. Flatten all four."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("text", "content", "value", "claim", "paragraph"):
            if v.get(k):
                return _text(v[k])
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(x for x in (_text(i) for i in v) if x)
    return str(v)


def claim_items(payload) -> list:
    """-> the source's OWN list of claim items, as strings, in the order it gave them.

    The count of this list is the shape we are handed. It is recorded and then verified, never
    trusted: on the measured failure it said 2 for a twenty-claim application.
    """
    v = payload.get("claims")
    if v is None:
        v = payload.get("claims_text")
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, dict):
        return [_text(v)] if _text(v) else []
    out = []
    for item in v:
        t = _text(item)
        if t.strip():
            out.append(t)
    return out


# Google Patents prints the count above the claims: "Claims (15)", then "Translated from German".
# MEASURED 2026-08-22 on the 281 documents in `sources_docstore`: every one of them carries this
# header, and for the machine-translated DE records the claims that follow it are separated by
# NEWLINES AND NOTHING ELSE. Both numeric splitters return ONE claim for a fifteen-claim document,
# which is the same silent loss as the glued blob wearing different clothes. The header is the
# independent count that catches it.
GP_COUNT = re.compile(
    r"^[^\S\n]*(?:claims?|patentanspr(?:ü|ue)che|anspr(?:ü|ue)che|revendications"
    r"|权利要求(?:书)?)[^\S\n]*\((\d{1,3})\)[^\S\n]*$", re.I | re.M)
TRANSLATED_LINE = re.compile(r"^[^\S\n]*translated from [a-zÀ-ɏ]+[^\S\n]*$", re.I | re.M)
SECTION_LINE = re.compile(
    r"^[^\S\n]*(?:claims?|description|abstract|patentanspr(?:ü|ue)che|anspr(?:ü|ue)che"
    r"|beschreibung|revendications)[^\S\n]*(?:\(\d{1,3}\))?[^\S\n]*:?[^\S\n]*$", re.I)
LEADING_NUMBER = re.compile(r"^[^\S\n]*\d{1,3}[^\S\n]*[.)．、][^\S\n]*")


def strip_source_preamble(blob: str, max_lines: int = 3) -> str:
    """Drop the source's own section furniture from the TOP of a blob, and only from the top.

    "Claims (15)" and "Translated from German" are Google Patents chrome. Removing them anywhere
    in the text would delete a line of a description that happens to say "Description"; removing
    them only from the first few lines cannot.
    """
    lines = (blob or "").split("\n")
    cut = 0
    for i, line in enumerate(lines[:max_lines]):
        if not line.strip():
            cut = i + 1
            continue
        if (GP_COUNT.match(line) or TRANSLATED_LINE.match(line)
                or (SECTION_LINE.match(line) and len(line.strip()) <= 40)):
            cut = i + 1
    return "\n".join(lines[cut:]).strip()


def declared_claim_count(payload, claims_blob: str | None = None) -> int | None:
    """The count the SOURCE says the document has, from wherever it put it.

    This is the only count that is independent of how the text was split, which is exactly what
    makes it the check worth having.
    """
    for key in ("n_claims", "num_claims", "claim_count", "claims_count", "number_of_claims"):
        v = payload.get(key)
        if v is None and isinstance(payload.get("meta"), dict):
            v = payload["meta"].get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and 0 < v <= MAX_CLAIMS:
            return v
        if isinstance(v, str) and v.strip().isdigit():
            n = int(v.strip())
            if 0 < n <= MAX_CLAIMS:
                return n
    # "Claims (15)" at the head of the claims text itself.
    head = "\n".join((claims_blob or "").split("\n")[:3])
    m = GP_COUNT.search(head)
    if m and 0 < int(m.group(1)) <= MAX_CLAIMS:
        return int(m.group(1))
    # "20 Claims, 36 Drawing Sheets" is what the front page of a US grant actually prints.
    cover = _text(payload.get("cover") or payload.get("front_page") or "")
    m = re.search(r"\b(\d{1,3})\s+(?:claims?|anspr(?:ü|ue)che|revendications)\b", cover, re.I)
    if m and 0 < int(m.group(1)) <= MAX_CLAIMS:
        return int(m.group(1))
    return None


# --------------------------------------------------------------------------- claim verification

def _split_both(blob: str):
    """Both splitters, so a count is never one implementation's opinion.

    `patent_doc.split_claims` anchors each boundary to the claim number expected NEXT and is
    CJK-aware; `patent_text.split_claims` splits on line-leading "N.". They fail differently,
    which is the point of running both.
    """
    a = []
    try:
        import patent_doc
        a = patent_doc.split_claims(blob) or []
    except Exception:                                                # noqa: BLE001
        a = []
    b = pt.split_claims(blob) or []
    return a, b


def _embedded_run(text: str, own_no: int, held: int) -> list:
    """Claim numbers still sitting INSIDE one claim body, as a sequential run.

    Returns the run when the body looks like several claims glued together, else []. The three
    conditions together are what keep a numbered sub-list from tripping it: the run must start at
    exactly the next claim number, be at least GLUE_RUN_MIN long, and end above the number of
    claims we actually hold, i.e. it must imply claims that are missing.
    """
    try:
        import patent_doc
        marks = patent_doc._claim_markers(text)
    except Exception:                                                # noqa: BLE001
        return []
    nums = [n for _, _, n in marks]
    run, expect = [], own_no + 1
    for n in nums:
        if n == expect:
            run.append(n)
            expect += 1
    if len(run) >= GLUE_RUN_MIN and run[-1] > held:
        return run
    return []


def _split_by_lines(blob: str, min_chars: int = 0) -> list:
    """One claim per line, numbered by position.

    For a machine-translated DE record from Google Patents this is the ONLY reading that works:
    the claims are separated by newlines and carry no numbers at all, so both numeric splitters
    return the whole section as claim 1. It is offered as a candidate and never trusted on its
    own; it is used only when the source's declared count says it is right.
    """
    out = []
    for line in (blob or "").split("\n"):
        s = line.strip()
        if not s or len(s) < min_chars:
            continue
        if SECTION_LINE.match(s) and len(s) <= 40:
            continue
        out.append({"claim_no": len(out) + 1, "text": LEADING_NUMBER.sub("", s).strip()})
    return [c for c in out if c["text"]][:MAX_CLAIMS]


def _assert_claims(chosen: list, name: str) -> list:
    """The assertions that hold whichever splitter produced the reading. Raises, or returns."""
    chosen = [{"claim_no": int(c["claim_no"]), "text": (c.get("text") or "").strip()}
              for c in chosen if (c.get("text") or "").strip()][:MAX_CLAIMS]
    if not chosen:
        raise ParseRejected("EMPTY_CLAIMS", "no claim survived splitting", {"splitter": name})
    numbers = [c["claim_no"] for c in chosen]

    # The numbering is a contiguous 1..N run. A gap means a claim was dropped, and a dropped claim
    # is invisible rather than wrong, which is why nothing noticed the first time this happened.
    if numbers != list(range(1, len(numbers) + 1)):
        raise ParseRejected("CLAIM_NUMBERING", f"claim numbers are not 1..{len(numbers)}",
                            {"numbers": numbers[:40], "parsed": len(chosen), "splitter": name})

    # No claim body still holds a run of later claim numbers.
    for c in chosen:
        run = _embedded_run(c["text"], c["claim_no"], len(chosen))
        if run:
            raise ParseRejected("CLAIMS_GLUED",
                                f"claim {c['claim_no']} still contains claims "
                                f"{run[0]}..{run[-1]} ({len(c['text'])} chars)",
                                {"claim_no": c["claim_no"], "chars": len(c["text"]),
                                 "embedded": [run[0], run[-1]], "parsed": len(chosen),
                                 "splitter": name})

    # Nothing sits exactly on a truncation boundary.
    for c in chosen:
        if len(c["text"]) in TRUNCATION_BOUNDARIES:
            raise ParseRejected("CLAIM_TRUNCATED",
                                f"claim {c['claim_no']} is exactly {len(c['text'])} characters, "
                                f"a clip boundary",
                                {"claim_no": c["claim_no"], "chars": len(c["text"]),
                                 "splitter": name})
    return chosen


def verify_claims(items: list, declared: int | None, notes: list) -> list:
    """-> claims as `[{claim_no, text}]`, or raise `ParseRejected`.

    The whole point of the module. Read the arbitration rule, not the plumbing.

    Four deterministic readings of the same text are produced, and the SOURCE'S OWN COUNT decides
    between them. That is the difference between a parser and a guess: the count comes from the
    bibliographic record or the printed header, not from whichever splitter happened to yield the
    most rows. When there is no declared count the highest yield wins, because a source that split
    correctly is never re-split into fewer claims and a source that glued nineteen claims together
    is always corrected.
    """
    raw = "\n".join(items).strip()
    if not raw:
        if declared:
            raise ParseRejected("EMPTY_CLAIMS",
                                f"source declares {declared} claims and carried none",
                                {"declared": declared})
        return []
    blob = strip_source_preamble(raw)

    doc_split, txt_split = _split_both(blob)
    positional = [{"claim_no": i + 1, "text": strip_source_preamble(t)}
                  for i, t in enumerate(items) if t.strip()]
    candidates = [("patent_doc", doc_split), ("patent_text", txt_split),
                  ("source", positional), ("lines", _split_by_lines(blob)),
                  ("lines_long", _split_by_lines(blob, min_chars=25))]
    counts = {n: len(c) for n, c in candidates}

    if declared is not None:
        agreeing = [(n, c) for n, c in candidates if len(c) == declared]
        if not agreeing:
            raise ParseRejected("CLAIM_COUNT_MISMATCH",
                                f"source declares {declared} claims and no reading of the text "
                                f"produces {declared}",
                                {"declared": declared, "source_items": len(positional),
                                 "readings": counts})
        last = None
        for name, cand in agreeing:
            try:
                chosen = _assert_claims(cand, name)
            except ParseRejected as exc:
                last = exc
                continue
            if name != "source":
                notes.append(f"claims re-split by {name} from {len(positional)} source "
                             f"{'entry' if len(positional) == 1 else 'entries'} into "
                             f"{len(chosen)} claims, matching the {declared} the source declares")
            return chosen
        raise last

    name, cand = max(candidates[:3], key=lambda kv: len(kv[1]))
    chosen = _assert_claims(cand, name)
    if name != "source" and len(chosen) != len(positional):
        notes.append(f"claims re-split by {name} from {len(positional)} source "
                     f"{'entry' if len(positional) == 1 else 'entries'} into {len(chosen)} "
                     f"numbered claims")
    return chosen


# --------------------------------------------------------------------------- the whole document

def _paragraph_rows(payload, notes) -> list:
    desc = strip_source_preamble(
        _text(payload.get("description") if payload.get("description") is not None
              else payload.get("description_text")))
    if not desc.strip():
        return []
    paras = pt.split_paragraphs(desc)
    if not paras:
        # Text that carries no line structure at all still has to be searchable.
        paras = [{"para_no": "p0001", "heading": None, "text": desc}]
        notes.append("description carried no line structure; kept as one paragraph")
    if len(paras) > MAX_PARAGRAPHS:
        notes.append(f"description capped at {MAX_PARAGRAPHS} paragraphs (had {len(paras)})")
    return paras[:MAX_PARAGRAPHS]


def _figure_rows(payload, notes) -> list:
    given = payload.get("figure_captions") or payload.get("figures")
    rows = []
    if isinstance(given, list) and given:
        for f in given:
            if isinstance(f, dict):
                cap = _text(f.get("caption") or f.get("text"))
                if cap:
                    rows.append({"figure_no": f.get("figure_no") or f.get("no"), "caption": cap})
            else:
                cap = _text(f)
                if cap:
                    rows.append({"figure_no": None, "caption": cap})
    if not rows:
        desc = _text(payload.get("description") if payload.get("description") is not None
                     else payload.get("description_text"))
        rows = [{"figure_no": f["figure_no"], "caption": f["caption"]}
                for f in pt.figure_captions(desc)]
    return rows[:MAX_FIGURES]


def content_sha(payload) -> str:
    """A stable digest of the material that would be embedded, so a source that is refreshed with
    better text can be told apart from one that is merely re-listed."""
    parts = [_text(payload.get("title")), _text(payload.get("abstract")),
             "\n".join(claim_items(payload)),
             _text(payload.get("description") if payload.get("description") is not None
                   else payload.get("description_text"))]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8", "replace")).hexdigest()


def normalise(payload: dict, publication_number: str | None = None,
              allow_claim_defect: bool = False) -> ParsedDoc:
    """A fetched record -> a `ParsedDoc`, or `ParseRejected`.

    `payload` is workstream C's parsed document (see docs/parsed_doc_contract.md) or a
    `sources_docstore` row rehydrated by `ops/parsed_sources.py`. Both go through here, so the
    claim-count assertion cannot be bypassed by arriving from the other source.

    `allow_claim_defect` is what the worker passes, and it changes only WHICH material is thrown
    away. Measured 2026-08-22 over 460 documents in `sources_docstore`: 88 are 1970s German
    publications whose OCR is beyond any splitter ("ι 2 kubaren suction head consists of two"),
    and rejecting the whole record would throw away a perfectly good abstract and description with
    the unreadable claims. So the CLAIMS are refused, the rest is kept, and the ledger records the
    defect. No wrong claim is ever staged either way: that is the harm this module exists to
    prevent, because a "claim 1" that is nineteen claims feeds the limitation splitter.
    """
    pub = (publication_number or payload.get("publication_number")
           or payload.get("publication") or payload.get("pub") or "").strip()
    if not pub:
        raise ParseRejected("NO_PUBLICATION", "record carries no publication number", {})

    notes: list = []
    defect = None
    items = claim_items(payload)
    declared = declared_claim_count(payload, "\n".join(items))
    try:
        claims = verify_claims(items, declared, notes)
    except ParseRejected as exc:
        if not allow_claim_defect:
            raise
        defect = {"code": exc.code, "reason": exc.message, "detail": exc.detail}
        notes.append(f"claims refused ({exc.code}): {exc.message}")
        claims = []
    claims = pt.resolve_claims(claims)

    lang = (payload.get("lang") or (payload.get("meta") or {}).get("claims_lang") or "en")
    abstract = _text(payload.get("abstract"))
    orig = payload.get("abstract_orig") or {}
    orig_text = _text(orig.get("text") if isinstance(orig, dict) else orig)
    orig_lang = (orig.get("lang") if isinstance(orig, dict) else "") or ""
    if orig_text and orig_text.strip() == abstract.strip():
        orig_text, orig_lang = "", ""

    doc = ParsedDoc(
        publication_number=pub,
        title=_text(payload.get("title")).strip(),
        abstract=abstract.strip(),
        abstract_lang=(payload.get("abstract_lang") or "en"),
        abstract_orig=orig_text.strip(),
        abstract_orig_lang=orig_lang,
        lang=lang,
        claims=claims,
        paragraphs=_paragraph_rows(payload, notes),
        figures=_figure_rows(payload, notes),
        declared_claim_count=declared,
        source=_text(payload.get("source") or (payload.get("meta") or {}).get("source")),
        notes=notes,
        claim_defect=defect,
    )
    if not (doc.claims or doc.paragraphs or doc.abstract):
        if defect:
            raise ParseRejected(defect["code"], defect["reason"], defect["detail"])
        raise ParseRejected("NO_TEXT", "record carries no claims, no description and no abstract",
                            {"publication_number": pub})
    return doc


def rejection_row(pub: str, source_key: str, exc: ParseRejected) -> dict:
    return {"publication_number": pub, "source_key": source_key, "code": exc.code,
            "reason": exc.message, "detail": json.dumps(exc.detail, default=str)[:4000]}
