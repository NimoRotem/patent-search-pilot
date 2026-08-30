"""When no text source holds a publication, read the publication's own printed pages.

WHY, MEASURED
-------------
DE 10 2024 133 318 A1, published 2026-05-21, asked for on 2026-08-20:

    Google Patents            404 — the document is not there at all
    EPO OPS /claims           404 CLIENT.InvalidCountryCode — the full-text service covers EP
                              and WO, never a DE national publication
    the extractor's verdict   "no usable text extracted", 0 claims, 0 chars, no title

The search ran anyway, on a description of the four drawings, and produced a brief about "a robotic
arm end effector system". The document is a Schlauchheber: a vacuum tube lifter with a venting
valve and a self-switching throttle valve. Nothing downstream could have recovered from that, and a
Type B claims attack was impossible because there were no claims.

What OPS DOES serve for a DE national publication is the document itself, as page images, and the
claims are printed on those pages. Reading them is the same vision pass that reads a scanned USPTO
office action in `prosecution.py`.

THE TRAP THIS MODULE EXISTS TO AVOID
------------------------------------
`ops.fetch_facsimile` prefers the *Drawing* instance, which for this document is four pages of line
art. Handed those and asked to transcribe a patent, the model wrote a complete title, abstract and
claim — "Vorrichtung zum Betreiben eines Arbeitsgeräts, insbesondere eines Forst- oder
Gartengeräts" — none of which is in the document. A model asked to transcribe text from pictures
will produce some.

So this module does two things that are not optional:

  * it takes the FULLDOCUMENT instance, never the drawings; and
  * it CORROBORATES the transcription against the abstract OPS serves separately, and returns
    nothing at all if the vocabulary does not overlap. An invented claim set is far worse than no
    claim set: no claims means a weaker search, invented claims means a confident wrong one.
"""
from __future__ import annotations

import json
import re
import traceback

#  How many pages of the printed document to read. A national A-publication runs to a few dozen;
#  the claims are at the end, so this has to be generous enough to reach them.
MAX_PAGES = 40
#  Corroboration bar: distinctive words (7+ letters) from the official abstract that must also
#  appear in what was transcribed. A third of them, and never fewer than three.
MIN_OVERLAP_FRAC = 3
MIN_OVERLAP_ABS = 3

_SYS = (
    "You are reading the published PDF of a patent document. Return JSON only:\n"
    '{"title":"...","language":"de|en|fr|...","abstract":"...",'
    '"claims":[{"claim_no":1,"independent":true,"text":"the claim VERBATIM"}]}\n'
    "Transcribe the title, the abstract and every patent claim exactly as printed, in full, in "
    "their original language, keeping the reference numerals. Do NOT summarise, translate, "
    "renumber or invent anything. If the pages you are given do not contain the claims, return an "
    "empty claims list rather than composing one from the drawings."
)


def _words(text):
    return {w for w in re.findall(r"[a-zà-öø-ÿ]{7,}", str(text or "").lower())}


def corroborated(transcription, reference_text):
    """Does the transcription actually come from the document the abstract describes?

    -> (bool, n_hit, n_words). An empty reference cannot corroborate anything and returns False:
    "0 of 0 words matched" is not a check, and treating it as a pass is how an invented claim set
    gets through.
    """
    ref = _words(reference_text)
    if len(ref) < 4:
        return False, 0, len(ref)
    mine = " ".join([
        str(transcription.get("title") or ""),
        str(transcription.get("abstract") or ""),
        " ".join(str(c.get("text") or "") for c in (transcription.get("claims") or [])),
    ]).lower()
    hit = [w for w in ref if w in mine]
    need = max(MIN_OVERLAP_ABS, len(ref) // MIN_OVERLAP_FRAC)
    return len(hit) >= need, len(hit), len(ref)


def full_document_pages(pub, max_pages=MAX_PAGES, log=print):
    """The printed pages of `pub`, or []. The FULLDOCUMENT instance, never the drawings."""
    try:
        import ops
    except Exception:
        return []
    try:
        data = ops.ops_fetch(pub, want=("images",))
    except Exception:
        traceback.print_exc()
        return []
    insts = data.get("images") or []
    full = next((i for i in insts if "full" in str(i.get("desc") or "").lower()), None)
    if not full:
        log("[facsimile] %s: OPS offers no FullDocument instance (%s)"
            % (pub, ", ".join(str(i.get("desc")) for i in insts) or "nothing"))
        return []
    pages = []
    for i in range(1, min(int(full.get("pages") or 1) or 1, max_pages) + 1):
        try:
            b = ops.fetch_image_page(full["link"], i)
        except Exception:
            break
        if not b:
            break
        pages.append(b)
    log("[facsimile] %s: %d of %s printed page(s)" % (pub, len(pages), full.get("pages")))
    return pages


def official_abstract(pub, log=print):
    """The abstract OPS serves for `pub`, as plain text, or "". Used ONLY to corroborate.

    Asked of the endpoint directly rather than through `ops.ops_fetch`, whose `want_for` policy
    correctly says a DE national publication has no OPS FULL TEXT and therefore never requests
    anything for it. The abstract is a different service and it answers 200 for exactly the
    documents this module exists to read: verified on DE 10 2024 133 318 A1, where ops_fetch
    returned nothing and /abstract returned the Schlauchheber abstract. Without this the
    corroboration compares against an empty string, scores "0 of 0", and correctly refuses every
    reading — which is safe, and useless.
    """
    try:
        import ops
    except Exception:
        return ""
    bare = re.sub(r"[^A-Z0-9]", "", str(pub or "").upper())
    for fmt in ("docdb", "epodoc"):
        for num in (bare, re.sub(r"[A-Z]\d?$", "", bare)):
            if not num:
                continue
            try:
                st, body, _h = ops._ops_get(
                    "published-data/publication/%s/%s/abstract" % (fmt, num))
            except Exception:
                continue
            if st != 200 or not body:
                continue
            txt = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
            #  Strip the bibliographic header the response wraps the abstract in, then the tags.
            plain = " ".join(re.sub(r"<[^>]+>", " ", txt).split())
            if len(plain) > 20:
                return plain
    log("[facsimile] %s: no official abstract to corroborate against" % pub)
    return ""


def read(pub, log=print, reference_text=""):
    """Transcribe `pub` from its own printed pages. -> {} unless the result is corroborated.

    -> {"title", "abstract", "language", "claims": [{claim_no, text, independent}],
        "source": "facsimile", "n_pages", "corroboration": {...}}
    """
    pages = full_document_pages(pub, log=log)
    if not pages:
        return {}
    try:
        import llm
        from google.genai import types
        parts = [types.Part.from_bytes(data=b, mime_type="application/pdf") for b in pages]
        parts.append("Transcribe the title, the abstract and every patent claim from this "
                     "document.")
        txt = (getattr(llm._call_vision(_SYS, parts, max_tokens=24000), "text", "") or "").strip()
        got = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        traceback.print_exc()
        return {}

    ref = reference_text or official_abstract(pub, log=log)
    ok, hit, total = corroborated(got, ref)
    if not ok:
        #  Refused, and loudly. See the module docstring: the drawings-only attempt produced a
        #  complete and entirely invented claim set for this same publication.
        log("[facsimile] %s: transcription NOT corroborated against the official abstract "
            "(%d of %d distinctive words); discarding it rather than searching on it"
            % (pub, hit, total))
        return {}
    claims = []
    for c in (got.get("claims") or []):
        t = " ".join(str((c or {}).get("text") or "").split())
        if len(t) < 20:
            continue
        claims.append({"claim_no": int(c.get("claim_no") or len(claims) + 1), "text": t,
                       "independent": bool(c.get("independent"))})
    log("[facsimile] %s: read %d claim(s) from %d printed page(s), corroborated %d/%d"
        % (pub, len(claims), len(pages), hit, total))
    return {"title": " ".join(str(got.get("title") or "").split()),
            "abstract": " ".join(str(got.get("abstract") or "").split()),
            "language": (got.get("language") or "").lower(),
            "claims": claims, "source": "facsimile", "n_pages": len(pages),
            "corroboration": {"matched": hit, "of": total}}
