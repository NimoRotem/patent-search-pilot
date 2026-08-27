"""What a limitation REQUIRES, worked out before anything is searched for it.

THE FAILURE THIS EXISTS FOR. Counsel, 2026-08-26, on the packet built for adhoc-efbf2979420b:
claim 1's controlling limitation is "the contact surface angle ranges in size from 170° to 190°",
and the report said it was disclosed by 0 of 232 references read in full. That is a false negative,
and its cause is mechanical. One paragraph away from the claim the applicant defines the term:

    "Each of the workpiece contact surfaces is aligned at least substantially parallel to the
     displacement direction, i.e., defines between itself and the displacement direction ... a
     contact surface angle which ranges in size from 170° to 190°."

170° to 190° means "the workpiece contact surface is parallel to the direction the magnet travels".
Searched as a NUMBER it looks unprecedented. Searched as a GEOMETRY it is the defining architecture
of the switchable permanent-magnet chuck and has been in the art since the 1930s: GB 874,600, which
this very search selected, claims "rectilinear sliding of the assembly in a direction parallel to a
holding face".

So a numeric range is never searched as a number alone. Two passes, both deterministic:

  LEXICOGRAPHY   the applicant's own definitions, taken from the specification wherever it says
                 "i.e.", "that is", "in other words", "namely", "defined as", "means" or "refers
                 to". A patentee is his own lexicographer and the definition is a construction of
                 the claim, so it is searched as well as the claim's words.
  GEOMETRY       any angular range is reduced to the structural relationship it encodes. A range
                 centred on 0 or 180 degrees says PARALLEL; one centred on 90 says PERPENDICULAR;
                 a genuinely oblique range (Schmalz's own 130 to 170 degree bevel) says nothing
                 structural and is left alone, which is the discrimination that matters.

AND IT GATES THE STRONGEST SENTENCE THE REPORT MAKES. "No reference in this search discloses this
limitation" is worth more than everything else on the page, and a vocabulary mismatch can produce
it. `zero_is_confirmable` answers whether that sentence may be said flat, or must be said with the
construction named beside it.

Nothing here calls a model. A construction that changes between runs is not a construction.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- numeric ranges

#  "170° to 190°", "170 to 190 degrees", "between 130 and 170 degrees", "130° bis 170°",
#  "from 170 to 190°". The unit may sit on either bound or after both.
_RANGE = re.compile(
    r"(?P<lo>\d{1,3}(?:\.\d+)?)\s*(?P<u1>°|\s*degrees?|\s*deg\b)?\s*"
    r"(?:to|and|bis|through|[-–—]|\.\.\.)\s*"
    r"(?P<hi>\d{1,3}(?:\.\d+)?)\s*(?P<u2>°|\s*degrees?|\s*deg\b)?", re.I)

#  "+/- 25%", "±25 %", "within approximately 25 percent of". A tolerance band around a named
#  quantity is a statement that the two are SUBSTANTIALLY EQUAL, and the art writes it that way.
_TOLERANCE = re.compile(
    r"(?:\+\s*/\s*-|±|plus\s+or\s+minus|within(?:\s+approximately)?)\s*"
    r"(?P<pct>\d{1,2}(?:\.\d+)?)\s*(?:%|percent)", re.I)


def _unit(m):
    u = (m.group("u1") or m.group("u2") or "").strip().lower()
    return "deg" if u.startswith(("°", "degree", "deg")) else ""


def ranges(text):
    """Every numeric range in `text`, with its unit. -> [{"lo", "hi", "unit", "span"}]"""
    out = []
    for m in _RANGE.finditer(str(text or "")):
        try:
            lo, hi = float(m.group("lo")), float(m.group("hi"))
        except (TypeError, ValueError):
            continue
        if hi < lo:
            lo, hi = hi, lo
        out.append({"lo": lo, "hi": hi, "unit": _unit(m), "span": m.group(0).strip()})
    return out


#  How far from the canonical angle a range's midpoint may sit and still be read as that
#  relationship. The range's own half-width when that is wider, because a drafter who wrote
#  "170 to 190" has already told us how much slop the claim tolerates; ten degrees otherwise, which
#  is what "at least substantially parallel" is worth in practice.
ANGLE_SLACK = 10.0

PARALLEL, PERPENDICULAR = "parallel", "perpendicular"

#  Facet terms, in the shape limitation_query._terms will accept: one or two words, four
#  characters or more, no leading preposition, stems where a stem is safe.
RELATION_TERMS = {
    PARALLEL: ["parallel", "coplanar", "flush", "aligned", "rectilinear", "same plane",
               "in-line", "collinear", "lengthwise"],
    PERPENDICULAR: ["perpendicular", "orthogonal", "right angle", "transverse", "normal",
                    "upright", "crosswise"],
}

#  What the relationship is called in prose, for the sentence a practitioner reads.
RELATION_PROSE = {
    PARALLEL: "parallel to, coplanar with, flush with or aligned with",
    PERPENDICULAR: "perpendicular to, normal to, orthogonal to or transverse to",
}


def angle_relation(lo, hi):
    """The structural relationship an angular range encodes. -> (relation, why) or ("", "")

    A range CENTRED on 0 or 180 degrees is a statement that two things are parallel; one centred on
    90 is a statement that they are perpendicular. Anything genuinely oblique encodes no canonical
    relationship and gets none invented for it: Schmalz's own pole-shoe bevel of 130 to 170 degrees
    is a bevel, and calling it "parallel" would be worse than saying nothing.
    """
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return "", ""
    if hi < lo:
        lo, hi = hi, lo
    half = (hi - lo) / 2.0
    slack = max(half, ANGLE_SLACK)
    raw = (lo + hi) / 2.0
    mid = raw % 180.0
    if mid <= slack or mid >= 180.0 - slack:
        return PARALLEL, (
            "%g to %g degrees is centred on %g, so the limitation requires the two features to be "
            "parallel. Searched as a number it looks unprecedented; searched as a geometry it is "
            "ordinary." % (lo, hi, round(raw / 180.0) * 180))
    if abs(mid - 90.0) <= slack:
        return PERPENDICULAR, (
            "%g to %g degrees is centred on 90, so the limitation requires the two features to be "
            "perpendicular." % (lo, hi))
    return "", ""


def relations(text):
    """Every structural relationship the numbers in `text` encode. -> [{...}]"""
    out, seen = [], set()
    for r in ranges(text):
        if r["unit"] != "deg" and not re.search(r"angle|degree|°", str(text or ""), re.I):
            continue
        rel, why = angle_relation(r["lo"], r["hi"])
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append({"kind": rel, "range": r["span"], "why": why,
                    "terms": list(RELATION_TERMS[rel]), "prose": RELATION_PROSE[rel]})
    for m in _TOLERANCE.finditer(str(text or "")):
        if "tolerance" in seen:
            break
        seen.add("tolerance")
        out.append({
            "kind": "tolerance", "range": m.group(0).strip(),
            "why": ("A band of plus or minus %s per cent around a named quantity is a statement "
                    "that the two are substantially equal. The art writes that qualitatively and "
                    "gives no number, so the number cannot be the only thing searched for."
                    % m.group("pct")),
            "terms": ["substantially equal", "approximately equal", "corresponds", "matches",
                      "same thickness", "equal to"],
            "prose": "substantially equal to, approximately matching or corresponding to"})
    return out


# --------------------------------------------------------------------------- lexicography

#  The cues a patentee uses when he is being his own lexicographer. Ordered longest first so
#  "in other words" is not eaten by "words".
#  Each alternative carries its OWN boundaries: a trailing \b after "i.e." never matches, because
#  the character after the full stop is usually a comma and two non-word characters have no
#  boundary between them. That one detail found zero definitions in a specification full of them.
_CUE = re.compile(
    r"(?:\bi\.\s?e\.|\bthat\s+is\s+to\s+say\b|\bthat\s+is\b|\bin\s+other\s+words\b|\bnamely\b|"
    r"\b(?:is|are)\s+(?:herein\s+)?defined\s+as\b|"
    r"\b(?:means|refers\s+to)\s+(?:that|a|an|the)\b)", re.I)

#  A definition never spans a paragraph. Bounded on both sides so a cue in the middle of a long
#  recitation does not swallow the whole page.
_SIDE = 400
MAX_DEFINITIONS = 40


def _clean(s):
    return " ".join(str(s or "").split()).strip(" ,;:")


def definitions(spec_text):
    """The applicant's own definitions, as (term, definition) pairs. -> [{...}]

    Deliberately symmetric: neither side is "the term". "X, i.e. Y" is a statement that X and Y are
    the same requirement, and which of the two a claim happens to use is exactly what varies. The
    caller matches a limitation against BOTH sides and takes the other one as the construction.
    """
    text = " ".join(str(spec_text or "").split())
    if not text:
        return []
    out, seen = [], set()
    for m in _CUE.finditer(text):
        cue = m.group(0)
        left = text[max(0, m.start() - _SIDE):m.start()]
        right = text[m.end():m.end() + _SIDE]
        #  Back to the start of the sentence, forward to the end of the clause: a definition is one
        #  sentence and its two halves sit either side of the cue.
        left = re.split(r"(?<=[.;])\s+", left)[-1]
        right = re.split(r"(?<=[.;])\s+", right)[0]
        term, defn = _clean(left), _clean(right)
        if len(term) < 12 or len(defn) < 12:
            continue
        key = (term.lower()[:120], defn.lower()[:120])
        if key in seen:
            continue
        seen.add(key)
        out.append({"term": term, "definition": defn, "cue": _clean(cue)})
        if len(out) >= MAX_DEFINITIONS:
            break
    return out


_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "at", "and", "or", "is", "are", "be", "which",
    "that", "with", "for", "from", "by", "as", "its", "said", "it", "itself", "between", "each",
    "least", "substantially", "wherein", "whereby", "one", "least", "such", "this", "these",
    "thereof", "therein", "having", "has", "comprising", "comprises", "further", "may", "can",
    "about", "size", "ranges", "range",
}


def _content_words(s):
    return {w for w in re.findall(r"[a-z]{4,}", str(s or "").lower()) if w not in _STOP}


def _overlap(a, b):
    """How much of the smaller word set the two share, 0 to 1."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / float(min(len(wa), len(wb)))


#  How much of a limitation's distinctive vocabulary a definition has to share before it is that
#  limitation's definition. Measured against the Schmalz specification: the "i.e." sentence shares
#  contact/surface/angle/displacement/direction with claim 1[e] and nothing else comes close.
DEFINITION_MATCH = 0.55


def definition_for(limitation_text, defs):
    """The applicant's own construction of THIS limitation, or None.

    Matches on either side of the cue and returns the OTHER side, because that is the half the
    claim did not use and therefore the half nothing has been searched for.
    """
    best, best_score = None, DEFINITION_MATCH
    for d in (defs or []):
        for near, far, side in ((d["definition"], d["term"], "definition"),
                                (d["term"], d["definition"], "term")):
            score = _overlap(limitation_text, near)
            if score > best_score:
                best_score = score
                best = {"matched": near, "construed_as": far, "cue": d["cue"],
                        "side": side, "score": round(score, 3)}
    return best


# --------------------------------------------------------------------------- the construction

#  Facet terms are matched literally against full text, so they obey the same shape rules as the
#  model's own: one or two words, four characters or more.
def _facet_terms(text, limit=10):
    out = []
    for w in re.findall(r"[a-z][a-z-]{3,}(?:\s+[a-z][a-z-]{3,})?", str(text or "").lower()):
        w = " ".join(w.split())
        if w.split()[0] in _STOP or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def construe(text, spec_defs=()):
    """Everything a search has to look for besides this limitation's own words. -> dict

    -> {"relations": [...], "definition": {...}|None, "terms": [...], "restated": str,
        "words_alone": bool}

    `words_alone` is True when the limitation says what it means and nothing was added. That is the
    ordinary case and it is the one where "no reference discloses this" may be said flat.
    """
    rels = relations(text)
    defn = definition_for(text, spec_defs)
    terms, seen = [], set()
    for r in rels:
        for t in r["terms"]:
            if t not in seen:
                seen.add(t)
                terms.append(t)
    if defn:
        for t in _facet_terms(defn["construed_as"]):
            if t not in seen:
                seen.add(t)
                terms.append(t)
    bits = []
    if defn:
        bits.append("The specification defines this limitation, using “%s”, as: %s"
                    % (defn["cue"], defn["construed_as"][:300]))
    for r in rels:
        bits.append(r["why"] + " Search it as %s." % r["prose"])
    return {
        "relations": rels,
        "definition": defn,
        "terms": terms[:14],
        "restated": " ".join(bits),
        "words_alone": not (rels or defn),
    }


def construe_all(limitations, spec_text=""):
    """Attach a construction to every limitation. Mutates and returns the list.

    Runs once, on the subject's own specification, before the query portfolio is built. Every
    consumer reads `lim["construction"]`: the portfolio adds a reading for it, the report gates
    its zero-coverage sentence on it, and the concise picker names it.
    """
    lims = list(limitations or [])
    defs = definitions(spec_text)
    for lim in lims:
        if not isinstance(lim, dict):
            continue
        lim["construction"] = construe(lim.get("text") or "", defs)
    return lims


def zero_is_confirmable(construction):
    """May "no reference in this search discloses this limitation" be said flat? -> bool

    No, when the limitation encodes something its own words do not say. The sentence is the
    strongest claim the report makes and a vocabulary mismatch can manufacture it, so it is either
    confirmed or it is qualified; it is never simply printed.
    """
    c = construction or {}
    if not isinstance(c, dict):
        return True
    if c.get("searched"):
        return True
    return bool(c.get("words_alone", True))


def zero_caveat(construction):
    """The sentence that goes beside an unconfirmed zero, or "". """
    c = construction or {}
    if zero_is_confirmable(c):
        return ""
    return ("Nothing matched the WORDS of this limitation. %s Until that has been searched as a "
            "concept rather than as a phrase, read this as “not found” and not as "
            "“not disclosed”." % (c.get("restated") or ""))
