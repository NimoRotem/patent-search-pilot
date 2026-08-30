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
  * ``limitation`` each REQUIREMENT of each claim, verbatim, one query per requirement.

THE LIMITATION QUERIES, AND WHY THEY BEAT THE BRIEF
---------------------------------------------------
MEASURED 2026-08-27 on the v2 corpus with quantisation error removed entirely, an exact scan over
all 11,162,752 vectors rather than a larger pool standing in for truth:

    one brief-shaped query ................. 1 of 6 known-relevant references in the top 25
    three limitation-shaped queries ........ 3 of 6 in the union of their top 30,
                                             with the two most on-point at ranks 3 and 4

The reason is the same one this module already exists for, one level down. A brief averages a
whole device into a point; a CLAIM averages its separate requirements into a point. "A grip unit
comprising a hollow grip portion, an electric vacuum generating device, and a sound-damping device
in the exhaust path" is three searchable requirements wearing one label, and the art that discloses
the third resembles the invention barely at all. Every reference an attorney filed against
US 2026/0109053 A1 was cited for ONE requirement.

So a limitation is searched on its own, verbatim. These are ATTRIBUTION queries, not ranking ones:
like elements they describe one part of the case, so `seed_specs` deliberately excludes them.

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
#  The ceiling the settings knob itself is capped at, used only for the progress bar's honest
#  upper bound. The live number is `max_limitations()`.
MAX_LIMITATIONS = 40

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class QuerySpec:
    name: str
    text: str
    kind: str            # essence | alt | brief | element | claim

    def as_dict(self):
        return {"name": self.name, "kind": self.kind, "text": self.text}


#  "The magnetic gripper of claim 1", "A method according to claim 3", "The device as claimed in
#  claim 2" and their kin: a dependency reference standing alone, with nothing after it.
_DEP_ONLY = re.compile(
    r"^\s*(?:the|a|an|said)?\s*[\w\s\-/]{0,60}?"
    r"(?:of|according to|as claimed in|as set forth in|as recited in|as defined in)\s+"
    r"(?:any\s+(?:one\s+)?of\s+)?claims?\s+[\d\s,\-and]*\d\s*[.,;:]?\s*$", re.I)


def max_limitations() -> int:
    """How many limitation queries this search may issue. 0 disables them."""
    try:
        import search_settings as _ss
        return 0 if not _ss.get("qs_limitations") else max(0, int(_ss.get("qs_max_limitations")))
    except Exception:
        return 0


def _limitation_texts(claims) -> list:
    """[(id, verbatim limitation text)], independent claims first. Empty when switched off.

    The split is the STRUCTURAL one (`use_llm=False`). This runs at the front of every search,
    before the ledger exists, and must not put a model call there; and a limitation QUERY does not
    need the legal precision the ledger's model-split is paid for, because nothing legal is decided
    from it. The ledger still does its own, better split later. These are questions, not findings.

    Independent claims first because a dependent claim's added limitation is mostly its parent's
    words plus one detail, so it retrieves a neighbourhood the parent's query already covered.
    """
    cap = max_limitations()
    if cap <= 0 or not claims:
        return []
    try:
        import limitations as _lim
        rows = _lim.split_claims(claims, use_llm=False, log=lambda *a, **k: None)
    except Exception:
        return []
    rows = sorted(rows, key=lambda r: (not r.get("independent"),
                                       r.get("claim_no") or 0, r.get("index") or 0))
    out = []
    for r in rows:
        text = " ".join(str(r.get("text") or "").split())
        if len(text) < 25:
            continue
        #  A dependent claim's structural split emits its dependency reference as a clause of its
        #  own: "The magnetic gripper of claim 1" is 31 characters, clears the length floor, and is
        #  a query for the phrase "of claim 1". It states no requirement, so it is dropped rather
        #  than issued. The requirement that claim ADDS is the next clause, and it is kept.
        if _DEP_ONLY.match(text):
            continue
        out.append((str(r.get("id") or ("limitation%d" % (len(out) + 1))), text))
        if len(out) >= cap:
            break
    return out


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


def _construed_queries(rows, spec_text=""):
    """The queries a limitation needs BESIDES its own words. -> [(name, text)]

    THE FAILURE THIS CLOSES. `claim_construction` has been able to do this since it was written,
    and its own docstring says the construction "runs once, before the query portfolio is built"
    and that "the portfolio adds a reading for it". The portfolio never did: the construction ran
    in `deep_rank`, which is AFTER retrieval, so it could explain a miss and never prevent one.

    Measured consequence, counsel on the packet built for adhoc-6393d0766a31: claim 1's
    controlling limitation is "the contact surface angle ranges in size from 170 to 190 degrees",
    and the report said 0 of 232 references read in full disclosed it. 170 to 190 degrees means
    "the workpiece contact surface is parallel to the direction the magnet travels". Searched as a
    NUMBER it is unprecedented. Searched as a GEOMETRY it is the defining architecture of the
    switchable permanent-magnet chuck and has been in the art since the 1930s, and the search had
    already selected GB 874,600, which claims "rectilinear sliding of the assembly in a direction
    parallel to a holding face".

    The same rule reaches claim 15: a band of plus or minus 25 per cent around the workpiece
    thickness is a statement that the two are substantially equal, and the art writes that
    qualitatively with no number at all.

    Two kinds of query come out, and both are cheap and deterministic:

      DEFINITION   the applicant's own words, wherever the specification says "i.e.", "that is",
                   "namely" or "defined as" next to the limitation. A patentee is his own
                   lexicographer, so his definition is a construction of the claim and is the
                   single best query text there is: it is the invention described in the words the
                   art will have used.
      GEOMETRY     the limitation with its numeric range removed and the structural relationship
                   put in its place, so "an angle of 170 to 190 degrees" is also searched as
                   "parallel to, coplanar with, flush with or aligned with".
    """
    try:
        import claim_construction
    except Exception:
        return []
    out, seen = [], set()
    try:
        rows = claim_construction.construe_all(rows, spec_text or "")
    except Exception:
        return []
    for r in rows:
        c = (r or {}).get("construction") or {}
        base = " ".join(str((r or {}).get("text") or "").split())
        lid = str((r or {}).get("id") or "")
        defn = c.get("definition") or {}
        if defn.get("construed_as"):
            t = " ".join(str(defn["construed_as"]).split())[:600]
            if len(t) >= 25 and t.lower() not in seen:
                seen.add(t.lower())
                out.append((lid + "[def]", t))
        for n, rel in enumerate(c.get("relations") or []):
            #  The limitation MINUS the number, PLUS what the number was saying. Removing the span
            #  matters as much as adding the prose: leaving "170 to 190 degrees" in the query keeps
            #  pulling the vector back towards documents that state an angle in degrees, which is
            #  the small set that made this a false negative in the first place.
            stripped = base.replace(str(rel.get("range") or ""), " ")
            t = " ".join((stripped + " " + str(rel.get("prose") or "")).split())[:600]
            if len(t) >= 25 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(("%s[%s]" % (lid, rel.get("kind") or "rel"), t))
    return out


def build(query: str, elements=None, claims=None, want_llm: bool = True,
          spec_text: str = "") -> list:
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

    #  When a document brought its own claims, its LIMITATIONS are its elements, and they are
    #  verbatim spans of the legal requirement rather than a model's paraphrase of the device. So
    #  the element list is trimmed rather than paid for twice; `qs_elements_with_claims` is what
    #  trims it, and setting it back to MAX_ELEMENTS restores the old pass count exactly.
    lims = _limitation_texts(claims)
    n_elements = MAX_ELEMENTS
    if lims:
        try:
            import search_settings as _ss
            n_elements = min(MAX_ELEMENTS, int(_ss.get("qs_elements_with_claims")))
        except Exception:
            n_elements = MAX_ELEMENTS
    for i, e in enumerate((elements or [])[:n_elements]):
        add(f"element{i + 1}", e, "element")
    for lid, text in lims:
        add(lid, text, "limitation")
    #  AND WHAT EACH LIMITATION MEANS, not only what it says. See `_construed_queries`: this is
    #  the pass that turns "170 to 190 degrees" into "parallel", and it is free.
    if lims:
        try:
            import limitations as _lim
            rows = _lim.split_claims(claims, use_llm=False, log=lambda *a, **k: None)
            keep = {l for l, _ in lims}
            rows = [r for r in rows if str(r.get("id") or "") in keep]
            for name, text in _construed_queries(rows, spec_text):
                add(name, text, "construction")
        except Exception:
            pass

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
    and the wrong thing to rank a whole reference by. Limitation queries are the same shape for the
    same reason: one requirement is not the invention, and ranking a reference by how well it
    matches one requirement is how a search ends up recommending fifty documents that all disclose
    the same easy element.
    """
    #  A CONSTRUCTION IS NOT A SEED EITHER. It says what one requirement means; ranking a whole
    #  reference by it would promote every document that happens to be about parallel surfaces.
    return [s for s in specs if s.kind in ("essence", "alt", "brief", "claim")]
