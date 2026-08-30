"""Search with a query SET, not with one long brief.

MEASURED REASON THIS MODULE EXISTS (RECALL_STUDY_2026-08-02.md)
--------------------------------------------------------------
The search used to embed one 4,737-character LLM brief, of which 54% was figure-description
prose folded in by ``ingest_input`` ("FIGURE 1 depicts ... an outer wall 210 and an inner wall
220 ... thickness T and height D1"). Every patent has prose like that, so it is pure dilution of
the query vector. On the live corpus (4.95M publications), holding everything else fixed and
changing ONLY the query text, a reference the searcher named as highly relevant moved:

    the live brief + figure prose ................ dense rank #528
    the same brief, figure prose deleted ......... dense rank #35
    a 30-word plain-language essence sentence .... dense rank #2
    claim 1, verbatim ............................ not in the top 1,000

So: never embed figure prose, and never rely on ONE vector. A long brief averages a device, its
materials, its assembly and its drawings into a single point that is close to nothing in
particular. A set of short, individually-coherent queries each retrieves its own neighbourhood,
and fusion recovers the intersection.

Claim 1 verbatim being WORSE than the brief is not a surprise once measured: an independent claim
is written to be broad and legally precise, not to describe the device. It is kept in the set (it
is the right query for a claim-level match) but it must never be the only one.

WHAT THIS BUILDS
----------------
``build()`` returns a list of ``QuerySpec(name, text, kind)``:

  * ``essence``  one <= 35-word sentence: the device, how it is powered, its characterising
    feature. This is the single strongest query in the measurement above.
  * ``alt``      five alternative-vocabulary phrasings, because the corpus calls the same object a
    lifter, a gripper, a sucker, a suction cup, a vacuum handling device and a Sauger.
  * ``brief``    the de-figured brief, which is the broadest single query and the safety net.
  * ``element``  the technical elements the coverage agent already extracts.
  * ``claim``    each independent claim of an uploaded patent, verbatim.

One LLM call produces essence + alts, cached per query text. Every failure path degrades to the
de-figured brief plus the elements, which is still strictly better than today's behaviour.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass

import llm

#  The exact sentence ingest_input prepends when it folds a vision description of the drawings
#  into the query text. Matching on the marker rather than on the description keeps this robust:
#  the description itself is model-written and different every time.
_FIGURE_MARKER = re.compile(
    r"\n*\s*Drawings\s*\(figures analysed and folded into the query text.*",
    re.IGNORECASE | re.DOTALL)

MAX_ELEMENTS = 14
MAX_CLAIMS = 6
MAX_ALTS = 5

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class QuerySpec:
    name: str
    text: str
    kind: str            # essence | alt | brief | element | claim

    def as_dict(self):
        return {"name": self.name, "kind": self.kind, "text": self.text}


def retrieval_text(query: str) -> str:
    """The part of a search query that should ever reach an embedding.

    Strips the folded-in figure description. The drawings still reach the image-similarity
    channel as images (``figure_blobs``) and still reach the reader on the report page; they
    simply stop poisoning the text vectors. Anything that is not a document upload passes
    through unchanged, so a typed query is unaffected.
    """
    if not query:
        return ""
    return _FIGURE_MARKER.sub("", query).strip()


def figure_text(query: str) -> str:
    """The figure-description block that ``retrieval_text`` removes, for display/audit."""
    if not query:
        return ""
    m = _FIGURE_MARKER.search(query)
    return query[m.start():].strip() if m else ""


_SYS = (
    "You are writing the SEARCH QUERIES a patent examiner would type to find prior art for an "
    "invention. Return ONLY JSON:\n"
    '{"essence":"<one sentence, at most 35 words: what the device IS, how it is powered or '
    'driven, and the one structural feature that distinguishes it>",\n'
    ' "alts":["<at most 12 words>", ... five of them]}\n'
    "The alternatives must use DIFFERENT vocabulary from each other and from the essence: the "
    "words other inventors, examiners and translators would use for the same object (older terms, "
    "the generic term, the industrial term, the robotics term). Use the plain technical words that "
    "appear in patent titles and abstracts. No marketing words, no claim language, no reference "
    "numerals, no figure descriptions."
)


def _essence_and_alts(brief: str) -> dict:
    key = hashlib.sha1(brief[:4000].encode("utf-8", "replace")).hexdigest()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    out = llm.chat_json(_SYS, brief[:6000], max_tokens=700) or {}
    essence = str(out.get("essence") or "").strip()
    alts = [str(a).strip() for a in (out.get("alts") or []) if str(a or "").strip()]
    result = {"essence": essence, "alts": alts[:MAX_ALTS]}
    with _CACHE_LOCK:
        _CACHE[key] = result
    return result


def build(query: str, elements=None, claims=None, want_llm: bool = True) -> list:
    """The query set for one search. `query` may still carry the figure block; it is stripped here.

    Deterministic parts (brief, elements, claims) are always present, so an LLM outage costs the
    essence and the alternatives and nothing else.
    """
    brief = retrieval_text(query)
    specs: list[QuerySpec] = []
    seen: set[str] = set()

    def add(name, text, kind):
        text = " ".join(str(text or "").split())
        if len(text) < 8:
            return
        k = text.lower()[:160]
        if k in seen:
            return
        seen.add(k)
        specs.append(QuerySpec(name=name, text=text, kind=kind))

    if want_llm and brief:
        try:
            ea = _essence_and_alts(brief)
            add("essence", ea.get("essence"), "essence")
            for i, a in enumerate(ea.get("alts") or []):
                add(f"alt{i + 1}", a, "alt")
        except Exception:
            pass
    add("brief", brief, "brief")
    for i, e in enumerate((elements or [])[:MAX_ELEMENTS]):
        add(f"element{i + 1}", e, "element")

    #  Independent claims first: a dependent claim is mostly its parent's words plus one detail,
    #  so it adds a near-duplicate vector rather than a new direction.
    ordered = sorted((claims or []), key=lambda c: (not c.get("independent"),
                                                    c.get("claim_no") or 0))
    for c in ordered[:MAX_CLAIMS]:
        add(f"claim{c.get('claim_no')}", c.get("text"), "claim")
    return specs


def seed_specs(specs) -> list:
    """The subset that describes the WHOLE invention, so it can be fused as the ranking backbone.

    Element queries describe one part each: they are the right thing to attribute evidence with,
    and the wrong thing to rank a whole reference by.
    """
    return [s for s in specs if s.kind in ("essence", "alt", "brief", "claim")]
