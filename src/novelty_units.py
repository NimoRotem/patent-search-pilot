"""Claim limitations, the RELATIONSHIPS between them, and the combinations worth searching together.

WHY THIS EXISTS
---------------
`query_set` already knew that a brief averages a device into one point and that a claim averages
its requirements into one point, and it split both. It stopped one level too early. Novelty rarely
lives in a component; it lives in a relationship between components and in a functional constraint
on them. Splitting a claim into independently-searchable requirements and then searching each one
alone destroys exactly the thing that distinguishes the claim.

Audited on the search behind report adhoc-f1410b74df48 (US 2025/0033224 A1, portable vacuum
gripper). Claim 1 turns on five things:

    peripheral openings in the base;
    sections of a flexible, stretchable FIRST seal portion exposed THROUGH those openings;
    a compressible, deformable SECOND portion under it;
    the first portion being HARDER or LESS compressible than the second;
    the two cooperating so the first portion is pressed THROUGH an opening and reduces the gap at
    a STEP in the object surface.

The decomposition that was actually searched reduced that to "base element having peripheral
openings", "flexible, stretchable first portion exposed through openings" and "compressible and
deformable second portion supporting the first portion". The relative-property constraint and the
press-through/gap-bridging mechanism, the two least conventional ideas in the claim, were searched
by nothing at all. Meanwhile "a battery housed in a handle" and "alarms for cavity pressure and
battery level" each got a retrieval pass of their own, and each of those retrieves an ocean.

WHAT THIS BUILDS
----------------
One record per limitation, carrying more than its text:

    verbatim      the claim language, unaltered, because that is what an examiner reads
    concept       the same requirement in the words another patent would use
    components    the parts it names
    relationship  the spatial, relative or causal link between them, empty when there is none
    function      the result the claim requires that link to produce
    alternatives  embodiment branches that live in different prior art and must not be merged
    genericity    0 (unheard of) to 1 (every device in the field has one)
    specificity   0 to 1, how much of the claim's novelty this requirement carries
    kind          which of the four classes below
    cpc, synonyms retrieval hints

and one record per CLUSTER: 2 to 4 limitations searched as one query, because a document that
discloses the combination is prior art and a document that discloses one member usually is not.

THE PRIORITY ORDER, and it is a budget, not a label:

    structural_relationship   how the parts are arranged with respect to each other
    material_relationship     one part's property stated RELATIVE to another's
    function_mechanism        the causal chain and the result it must produce
    context                   the field's defining apparatus, needed to stay in the field
    secondary                 real but narrow detail, and alternative embodiments
    generic_component         a battery, an alarm, a switch: a filter and a ranking signal,
                              not a query worth a pass of its own

NOTHING HERE DECIDES ANYTHING LEGAL. The ledger still owns what a limitation is and whether a
reference discloses it. This module decides only what gets asked, and in what order.

A MODEL OUTAGE COSTS ACCURACY, NOT THE FEATURE. `_fallback_units` classifies every limitation with
the same four classes from comparative, causal and conventional-subsystem cues in the claim text,
and `_fallback_clusters` still builds the combinations. Measured on claim 1 of US 2025/0033224 A1
the fallback finds the relative-property limitation and the press-through mechanism, which is the
whole point of the module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading

#  ---- the four classes, in the order the retrieval budget is spent -----------------------------
STRUCTURAL = "structural_relationship"
MATERIAL = "material_relationship"
MECHANISM = "function_mechanism"
CONTEXT = "context"
SECONDARY = "secondary"
GENERIC = "generic_component"

KINDS = (STRUCTURAL, MATERIAL, MECHANISM, CONTEXT, SECONDARY, GENERIC)
PRIORITY = {STRUCTURAL: 0, MATERIAL: 1, MECHANISM: 2, CONTEXT: 3, SECONDARY: 4, GENERIC: 5}
#  Everything at or above this rank is "core": it may join a cluster and it is searched before any
#  supporting component gets a pass.
CORE_KINDS = (STRUCTURAL, MATERIAL, MECHANISM)

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()

#  How many requirements the model is asked to read in one call. It returns a full record for each,
#  so this is a token budget as much as a scope one: at 40 the answer was truncated mid-list and
#  the clusters, which come last in the schema, never arrived at all. Independent claims are sent
#  first, so a cap trims dependents.
MAX_UNITS = int(os.environ.get("NOVELTY_MAX_UNITS", "24"))
#  Two requirements this similar are the same requirement. See limitations.dedupe_claim_text: the
#  extractor repeats clauses, and a cluster built from two copies of one clause asks nothing.
NEAR_DUP = float(os.environ.get("NOVELTY_NEAR_DUP", "0.82"))
#  A cluster of five is a claim again. Two to four is the range in which a combination is still
#  discriminative and still short enough to embed as one coherent query.
CLUSTER_MIN, CLUSTER_MAX = 2, 4


# ------------------------------------------------------------------ deterministic language cues
#  A property stated RELATIVE to another part. This is the class the audited run lost entirely.
_REL_PROP = re.compile(
    r"\b(higher|lower|greater|less|lesser|more|softer|harder|stiffer|thicker|thinner|larger|"
    r"smaller|denser|coarser|finer|coarser)\b[^.;]{0,80}?\bthan\b"
    r"|\bless\s+(compressible|deformable|flexible|stretchable|rigid|elastic)\b"
    r"|\bmore\s+(compressible|deformable|flexible|stretchable|rigid|elastic)\b"
    r"|\b(relative|compared)\s+to\b|\bsubstantially\s+(equal|parallel|coplanar|flush)\b", re.I)

#  A causal chain and the result it has to produce. "configured to" alone is not enough: almost
#  every clause in a US claim says it. It counts when it names an EFFECT.
_CAUSAL = re.compile(
    r"\b(so\s+(?:as|that)|such\s+that|thereby|whereby|in\s+order\s+to|configured\s+to\s+cause|"
    r"causes?\s+the|adapted\s+to\s+cause|results?\s+in|to\s+reduce|to\s+increase|to\s+bridge|"
    r"to\s+conform|to\s+fill|to\s+seal\s+against|upon|after\s+the)\b", re.I)

#  Spatial and structural arrangement between two named parts.
_SPATIAL = re.compile(
    r"\b(through|within|inside|around|about the periphery|peripheral|circumferential|between|"
    r"adjacent|disposed on|disposed in|mounted on|coupled to|attached to|surrounds?|overlying|"
    r"underlying|beneath|above|below|concentric|coaxial|interposed|extends? into|projecting)\b",
    re.I)

#  The conventional supporting subsystems. A pass spent on one of these retrieves the field, not
#  the invention. They stay in the ledger and in the ranking; they lose their own retrieval pass.
#  MATCHED ON WORD BOUNDARIES, not as substrings. In this corpus "grip" is inside "gripper" and
#  "handle" is inside "handling", so a substring test made almost every clause look conventional.
_GENERIC_TERMS = (
    "battery", "batteries", "rechargeable", "power supply", "power source", "charger",
    "alarm", "alert", "buzzer", "indicator", "warning", "display", "led",
    "switch", "button", "trigger", "handle", "housing", "casing", "enclosure",
    "controller", "processor", "microcontroller", "memory", "circuit",
    "manually operated", "manual pump", "hand pump", "backup",
    "sensor", "gauge", "meter",
)
#  The field's own defining apparatus. Needed to stay in the right art, useless as a novelty query.
_CONTEXT_TERMS = (
    "vacuum pump", "air extraction", "extract gas", "suction force", "vacuum source",
    "cavity", "vacuum gripper", "suction cup", "gripper", "lifting device", "vacuum seal",
)

#  "X, or Y" / "either X or Y" inside one requirement is two embodiments in one sentence, and they
#  sit in different prior art. A multi-material laminate and a fluid-filled bladder are not
#  neighbours in any corpus.
_ALT_SPLIT = re.compile(r",?\s+\bor\s+(?:even\s+)?(?:as\s+)?(?:a\s+|an\s+|the\s+)?", re.I)
_ALT_MARKERS = ("or even", "or a ", "or an ", "either", "alternatively", "potentially as")


def _norm(t) -> str:
    return " ".join(str(t or "").split())


_STOP = {"a", "an", "the", "of", "to", "in", "on", "at", "and", "or", "is", "are", "be", "with",
         "for", "that", "which", "wherein", "said", "one", "more", "least", "comprises",
         "comprising", "configured", "element", "portion", "first", "second", "by", "from",
         "such", "as", "its", "it", "into", "onto", "than", "the"}


def _bag(text) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) > 2 and w not in _STOP}


#  True function words only. `_STOP` above also drops claim-drafting words, which is right for
#  judging whether two requirements are the SAME and wrong for judging whether a phrase says
#  ANYTHING: by that list "the second portion comprises a composite" is one content word.
_FUNCTION = {"a", "an", "the", "of", "to", "in", "on", "at", "and", "or", "is", "are", "be",
             "with", "for", "that", "which", "as", "by", "from", "its", "it", "into", "onto",
             "than", "such", "more", "less", "least", "any", "all", "each", "being", "said"}


def _content_words(text) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) > 2 and w not in _FUNCTION}


def overlap(a, b) -> float:
    """Jaccard over content words. 1.0 is the same requirement wearing two ids."""
    x, y = _bag(a), _bag(b)
    if not x or not y:
        return 0.0
    return len(x & y) / float(len(x | y))


def _count(text, terms) -> int:
    low = text.lower()
    n = 0
    for t in terms:
        pat = r"\b" + r"\s+".join(re.escape(w) for w in t.split()) + r"\b"
        if re.search(pat, low):
            n += 1
    return n


def _has(text, terms) -> bool:
    return _count(text, terms) > 0


def _generic_hits(text) -> int:
    return _count(text, _GENERIC_TERMS)


def classify(text) -> str:
    """The class of one limitation, from its own words. Deterministic and cheap.

    Order matters and is the priority order: a requirement that states a relative property AND
    names a battery is a relative-property requirement, because that is the part of it no other
    document will have. The reverse is what must not happen: naming the invention's own apparatus
    does not rescue a clause whose actual content is a battery in a handle.
    """
    t = _norm(text)
    if not t:
        return GENERIC
    if _REL_PROP.search(t):
        return MATERIAL
    if _CAUSAL.search(t) and len(t.split()) >= 8:
        return MECHANISM
    gen = _generic_hits(t)
    ctx = _has(t, _CONTEXT_TERMS)
    #  A spatial link between parts is structural only when there are parts to link. A clause that
    #  says "coupled to the base element" and nothing else is assembly boilerplate, and one that
    #  says "a battery housed in a handle" is spatial about nothing that distinguishes anything.
    if _SPATIAL.search(t) and len(t.split()) >= 7 and not gen:
        return STRUCTURAL
    #  Two conventional parts in one requirement is a conventional requirement, whatever else it
    #  names. One, plus the field's own apparatus, is a narrow but real detail.
    if gen >= 2 or (gen and not ctx):
        return GENERIC
    if gen:
        return SECONDARY
    if ctx:
        return CONTEXT
    return SECONDARY if len(t.split()) >= 6 else GENERIC


def genericity(text) -> float:
    """0 (unheard of) to 1 (every device in this field has one). Deterministic."""
    t = _norm(text).lower()
    if not t:
        return 1.0
    score = 0.15
    score += 0.22 * min(3, _generic_hits(t))
    if _has(t, _CONTEXT_TERMS):
        score += 0.15
    if _REL_PROP.search(t):
        score -= 0.35
    if _CAUSAL.search(t):
        score -= 0.20
    if len(t.split()) >= 25:
        score -= 0.10
    return round(max(0.0, min(1.0, score)), 2)


#  The verb that ends the stem of a Markush-style requirement, so the branches after it can be
#  given that stem back. "the second portion COMPRISES x, or y" -> stem "the second portion
#  comprises", branches "x" and "y".
_STEM_HEAD = re.compile(
    r"\b(compris\w*|includ\w*|consist\w*|is|are|being|made of|formed of|selected from)\b", re.I)

#  A branch has to be a technical concept on its own. Below this it is a fragment: "layer hardness
#  is selected", "more durable than first portion" and "pneumatic element" are all things the
#  splitter used to emit, and each is a query for the whole corpus or for nothing.
ALT_MIN_CONTENT_WORDS = int(os.environ.get("NOVELTY_ALT_MIN_WORDS", "4"))


def split_alternatives(text) -> list:
    """Embodiment branches inside one requirement. [] when there is only one embodiment.

    "the second portion comprises multiple elastic compressible and deformable materials as
    discrete layers or a composite, or even a fluid-filled pneumatic or hydraulic element" is a
    laminate AND a bladder, and merging them into one query lands between two prior-art
    neighbourhoods and inside neither.

    TWO THINGS IT MUST NOT DO, both observed on this claim set. It must not emit a FRAGMENT: a
    Markush list of properties ("layer thickness, layer hardness, layer flexibility ... is
    selected") is one requirement written as a list, and splitting it produced four queries of
    three words each. And it must not emit a branch that has lost its subject: "hydraulic element"
    alone retrieves hydraulics, so the stem is given back to every branch that dropped it.
    """
    t = _norm(text)
    if not t or not any(m in t.lower() for m in _ALT_MARKERS):
        return []
    parts = [p.strip(" ,;.") for p in _ALT_SPLIT.split(t) if p.strip(" ,;.")]
    if len(parts) < 2:
        return []
    m = _STEM_HEAD.search(parts[0])
    stem = parts[0][:m.end()].strip() if m else ""
    out = []
    for p in parts:
        p = _norm(p)
        if stem and not p.lower().startswith(stem.lower()) and len(_content_words(p)) < 7:
            p = f"{stem} {p}"
        if len(_content_words(p)) < ALT_MIN_CONTENT_WORDS:
            continue
        #  Two branches this alike are one embodiment described twice, and searching both spends a
        #  pass to ask the same question.
        if any(overlap(p, q) >= 0.7 for q in out):
            continue
        out.append(p[:300])
    return out[:3] if len(out) >= 2 else []


#  Words that describe a PROPERTY rather than a thing. A branch made only of these is a restated
#  comparative, not a different embodiment, so it is not worth a retrieval pass of its own.
_PROPERTY_ONLY = {
    "hardness", "harder", "softer", "durable", "durability", "abrasion", "resistant",
    "compressible", "compressibility", "deformable", "deformability", "flexible",
    "flexibility", "stretchable", "stretchability", "thickness", "thick", "thin", "stiff",
    "stiffness", "elastic", "elasticity", "density", "selected", "layer", "portion", "property",
    "properties", "greater", "higher", "lower", "smaller", "larger", "localized", "pattern",
}

#  Everything that is not a THING: a property, a piece of claim drafting, or a linking verb. A
#  branch whose content words all live in here has described something, not named something.
_NOT_A_THING = _PROPERTY_ONLY | {
    "first", "second", "third", "portion", "portions", "element", "elements", "member",
    "comprising", "comprises", "wherein", "said", "configured", "disposed", "coupled",
    "respective", "plurality", "least", "another", "thereof", "claim", "has", "have",
    "having", "include", "includes", "including", "made", "form", "forms", "formed",
    "when", "where", "which", "that", "than", "with", "like", "such", "same", "other",
}


def _gate_alternatives(alts) -> list:
    """Model-proposed branches, kept only when each is a different EMBODIMENT.

    THE TEST IS WHETHER THEY NAME DIFFERENT THINGS. "the bottom layer is more durable than the
    first portion" and "the bottom layer is more abrasion-resistant than the first portion" name
    exactly one thing between them, a bottom layer, so they are one requirement with two adjectives
    and they retrieve the same neighbourhood twice. "a composite of discrete layers" and "a
    fluid-filled pneumatic element" name different things and sit in different art, which is the
    only case worth a pass of its own.
    """
    out, things = [], []
    for a in (alts or []):
        a = _norm(a)
        words = _content_words(a)
        if len(words) < ALT_MIN_CONTENT_WORDS:
            continue
        mine = words - _NOT_A_THING
        if not mine:
            continue
        if any(mine <= s or s <= mine for s in things):
            continue
        if any(overlap(a, b) >= 0.7 for b in out):
            continue
        out.append(a[:300])
        things.append(mine)
    return out[:3] if len(out) >= 2 else []


# ------------------------------------------------------------------------------- the model pass
_SYS = (
    "You are preparing a PRIOR-ART SEARCH from the claims of one patent application.\n\n"
    "Extract the claim limitations and, SEPARATELY, identify the technically distinctive "
    "RELATIONSHIPS between limitations. Do not reduce a relationship into independent component "
    "descriptions. Preserve relative properties, spatial relationships, causal mechanisms and "
    "functional results whenever those relationships may be what distinguishes the claim from "
    "conventional systems.\n\n"
    "Novelty almost never lives in a component. A battery, a pump, an alarm and a handle are in "
    "ten thousand patents each. What is rarely in any of them is: this part being HARDER than that "
    "part; this part passing THROUGH an opening in that part; that action producing a stated "
    "EFFECT on a stated feature of the workpiece. Those are the search targets.\n\n"
    "Return ONLY JSON:\n"
    '{"units":[{\n'
    '  "id":"<the limitation id you were given>",\n'
    '  "concept":"<the requirement in the words ANOTHER patent would use: no reference numerals, '
    'no \'said\', no \'at least one\', no private reference frame>",\n'
    '  "components":["<the parts it names>"],\n'
    '  "relationship":"<the spatial, relative or causal link BETWEEN those parts, in one clause. '
    'Empty string only if the requirement genuinely names one part and no link>",\n'
    '  "function":"<the result the claim requires that link to produce. Empty if none is stated>",\n'
    '  "alternatives":["<separate embodiment branches stated in this requirement, e.g. a layered '
    'composite versus a fluid-filled element. Omit when there is only one embodiment>"],\n'
    '  "kind":"structural_relationship|material_relationship|function_mechanism|context|secondary|'
    'generic_component",\n'
    '  "genericity":<0.0 conventional-in-every-device .. 1.0 unheard of, INVERTED: 1.0 means every '
    'device in the field has one>,\n'
    '  "specificity":<0.0 .. 1.0, how much of this claim\'s novelty this requirement carries>,\n'
    '  "cpc":["<CPC symbols where this requirement would be classified>"],\n'
    '  "synonyms":["<the words other inventors, examiners and translators use for it>"]}],\n'
    ' "clusters":[{\n'
    '  "members":["<2 to 4 unit ids that TOGETHER carry the novelty>"],\n'
    '  "text":"<one search query, at most 45 words, that asks for that combination in plain '
    'technical language. No claim syntax, no reference numerals>",\n'
    '  "why":"<one clause: why this combination is what a document has to disclose to be prior '
    'art>"}]}\n\n'
    "RULES.\n"
    "1. Emit one unit for every limitation id you are given, and no others.\n"
    "2. `kind` is the priority order for search budget. Use `generic_component` freely: a "
    "requirement that only adds a battery, an alarm, a switch, a handle or a manual backup is a "
    "generic_component even when the claim spends a sentence on it.\n"
    "3. Emit 3 to 6 clusters. The FIRST must be the strongest combination in the claim set: the "
    "one an examiner would have to find in a single document to reject it. Later clusters may be "
    "narrower pairs.\n"
    "4. A cluster's `text` is a QUERY, not a summary. Write what the document you want would say "
    "about itself."
)


def dedupe_limitations(rows) -> list:
    """One row per distinct requirement, independent claims first, capped at MAX_UNITS.

    `limitations.dedupe_claim_text` removes the repetition inside ONE claim; this removes the
    repetition ACROSS claims, where a dependent claim restates its parent's clause before adding
    its own. Independent first because a dependent's added requirement is mostly its parent's
    words plus one detail, so when the cap bites it should bite there.
    """
    rows = [r for r in (rows or []) if _norm(r.get("text"))]
    rows = sorted(rows, key=lambda r: (not r.get("independent"), r.get("claim_no") or 0,
                                       r.get("index") or 0))
    out = []
    for r in rows:
        t = _norm(r.get("text"))
        if len(t.split()) < 4:
            continue
        if any(overlap(t, _norm(k.get("text"))) >= NEAR_DUP for k in out):
            continue
        out.append(r)
        if len(out) >= MAX_UNITS:
            break
    return out


def _key(limitations) -> str:
    blob = "\n".join(f"{l.get('id')}::{_norm(l.get('text'))}" for l in limitations)
    return hashlib.sha1(blob[:20000].encode("utf-8", "replace")).hexdigest()


def _llm_analyse(limitations, spec_text="") -> dict:
    import llm
    payload = {"limitations": [{"id": str(l.get("id") or ""),
                                "claim": l.get("claim_label") or "",
                                "independent": bool(l.get("independent")),
                                "text": _norm(l.get("text"))[:1200]}
                               for l in limitations[:MAX_UNITS]]}
    user = json.dumps(payload, ensure_ascii=False)
    if spec_text:
        #  The applicant is his own lexicographer, and the sentence that says what a requirement
        #  MEANS is usually one paragraph away from the claim and nowhere inside it.
        user += "\n\nSPECIFICATION (for wording only, never a source of requirements)\n" \
                + _norm(spec_text)[:6000]
    #  STRONG TIER. Runs once per search and every retrieval pass below is keyed on its output.
    return llm.chat_json(_SYS, user, tier="strong", max_tokens=9000) or {}


def _fallback_units(limitations) -> list:
    out = []
    for l in limitations[:MAX_UNITS]:
        text = _norm(l.get("text"))
        kind = classify(text)
        gen = genericity(text)
        out.append({
            "id": str(l.get("id") or ""),
            "claim_label": l.get("claim_label") or "",
            "independent": bool(l.get("independent")),
            "verbatim": text[:1200],
            "concept": text[:600],
            "components": [],
            "relationship": "",
            "function": "",
            "alternatives": split_alternatives(text),
            "kind": kind,
            "genericity": gen,
            "specificity": round(max(0.0, 1.0 - gen), 2),
            "cpc": [],
            "synonyms": [],
            "source": "deterministic",
        })
    return out


def _merge(limitations, parsed) -> list:
    """The model's reading of each limitation, with the deterministic one underneath it.

    The verbatim claim text and the id are NEVER taken from the model: they are what the ledger,
    the chart and the packet are keyed on, and a paraphrase in that field is a wrong citation.
    """
    by_id = {}
    for u in (parsed.get("units") or []):
        if isinstance(u, dict) and str(u.get("id") or "").strip():
            by_id[str(u["id"]).strip()] = u
    out = []
    for base in _fallback_units(limitations):
        u = by_id.get(base["id"])
        if not u:
            out.append(base)
            continue
        kind = str(u.get("kind") or "").strip()
        merged = dict(base)
        merged.update({
            "concept": _norm(u.get("concept")) or base["concept"],
            "components": [_norm(c) for c in (u.get("components") or []) if _norm(c)][:8],
            "relationship": _norm(u.get("relationship"))[:400],
            "function": _norm(u.get("function"))[:400],
            "kind": kind if kind in KINDS else base["kind"],
            "cpc": [_norm(c) for c in (u.get("cpc") or []) if _norm(c)][:6],
            "synonyms": [_norm(s) for s in (u.get("synonyms") or []) if _norm(s)][:12],
            "source": "model",
        })
        #  THE DETERMINISTIC BRANCHES WIN. Asked for "separate embodiment branches", the model
        #  returns the two halves of any "or" in the requirement, and most of those are one
        #  embodiment stated twice: "higher hardness than the second portion" / "less compressible
        #  than the second portion" is a single claim limitation with an alternative WORDING, and
        #  giving each its own retrieval pass buys the same neighbourhood twice. `split_alternatives`
        #  branches only where the claim actually offers different structures, which is the case
        #  the user of this list cares about: a layered composite and a fluid-filled bladder.
        merged["alternatives"] = base["alternatives"] or _gate_alternatives(u.get("alternatives"))
        for f, lo, hi in (("genericity", 0.0, 1.0), ("specificity", 0.0, 1.0)):
            try:
                merged[f] = round(min(hi, max(lo, float(u.get(f)))), 2)
            except (TypeError, ValueError):
                pass
        #  A DETERMINISTIC OVERRIDE, deliberately one-way. The model is willing to call "a battery
        #  housed in a handle" a structural relationship because the words "housed in" are spatial.
        #  Nothing in this corpus is distinguished by a battery being in a handle.
        if merged["kind"] in CORE_KINDS and _generic_hits(base["verbatim"]) >= 2 \
                and not _REL_PROP.search(base["verbatim"]):
            merged["kind"] = GENERIC
            merged["genericity"] = max(merged["genericity"], 0.85)
        out.append(merged)
    return out


def _fallback_clusters(units) -> list:
    """Combinations built from the classes alone, for when the model is unavailable.

    The rule is the one an examiner works to: take the requirement that carries the most novelty,
    then add the requirements it is IN A RELATIONSHIP WITH. In practice that is the top of the
    priority order, so the first cluster is the core of the claim.
    """
    core = [u for u in units if u["kind"] in CORE_KINDS]
    core.sort(key=lambda u: (PRIORITY[u["kind"]], -u["specificity"]))
    if len(core) < CLUSTER_MIN:
        core = sorted(units, key=lambda u: (PRIORITY[u["kind"]], -u["specificity"]))
    if len(core) < CLUSTER_MIN:
        return []
    #  ONE MEMBER PER DISTINCT REQUIREMENT. Without this the first cluster of the audited claim
    #  set was claim 2[b] + claim 2[c] + claim 9[a] + claim 9[c], which is two requirements each
    #  listed twice: a four-member query that asks two questions.
    distinct = []
    for u in core:
        if all(overlap(u["concept"] or u["verbatim"],
                       v["concept"] or v["verbatim"]) < NEAR_DUP for v in distinct):
            distinct.append(u)
    if len(distinct) < CLUSTER_MIN:
        return []
    out = []
    #  ONE FROM EACH CLASS FIRST. A structural relation plus the relative property plus the
    #  mechanism is the combination the claim turns on; four structural relations is one idea
    #  spread over four clauses.
    top, taken = [], set()
    for kind in CORE_KINDS:
        for u in distinct:
            if u["kind"] == kind and u["id"] not in taken:
                top.append(u)
                taken.add(u["id"])
                break
    for u in distinct:
        if len(top) >= CLUSTER_MAX:
            break
        if u["id"] not in taken:
            top.append(u)
            taken.add(u["id"])
    top.sort(key=lambda u: (PRIORITY[u["kind"]], -u["specificity"]))
    out.append({"members": [u["id"] for u in top[:CLUSTER_MAX]],
                "text": _cluster_text(top[:CLUSTER_MAX]),
                "why": "the combination an examiner would have to find in one document",
                "source": "deterministic"})
    #  Then pairs that cross a class boundary, which is the cheapest way to ask "this arrangement
    #  AND this property" without inventing a relationship the claim does not state.
    for i, a in enumerate(distinct):
        for b in distinct[i + 1:]:
            if a["kind"] == b["kind"]:
                continue
            out.append({"members": [a["id"], b["id"]], "text": _cluster_text([a, b]),
                        "why": "a relationship and the property or effect it is stated to produce",
                        "source": "deterministic"})
            break
        if len(out) >= 6:
            break
    return out


def _cluster_text(units) -> str:
    """A query from 2 to 4 units: their concepts, joined, with the relationships kept.

    Concept first because it is already in the words another patent would use; the relationship
    and the function are appended when the model supplied them AND they say something the concept
    does not, because those clauses are the discriminative half and dropping them is the failure
    this module exists to end. The overlap test is what stops the concatenation restating itself:
    a model's `relationship` is usually a paraphrase of its own `concept`, so a substring test
    lets the same words through three times and the query becomes an echo.
    """
    bits = []
    for u in units:
        piece = _norm(u.get("concept") or u.get("verbatim") or "")
        for extra in (u.get("relationship") or "", u.get("function") or ""):
            extra = _norm(extra)
            if extra and overlap(extra, piece) < 0.6:
                piece = f"{piece}, {extra}"
        if piece and all(overlap(piece, b) < NEAR_DUP for b in bits):
            bits.append(piece)
    text = "; ".join(bits)
    #  A query is a query. Past about 60 words a vector is an average again, which is the thing
    #  the whole query-set design exists to avoid.
    return " ".join(text.split()[:60])


def analyse(limitations, spec_text="", want_llm=True) -> dict:
    """-> {"units": [...], "clusters": [...], "source": "model"|"deterministic"}

    Cached on the limitation texts, so the two calls a search makes (retrieval and the report) do
    not pay for it twice.
    """
    limitations = dedupe_limitations(limitations)
    if not limitations:
        return {"units": [], "clusters": [], "source": "empty"}
    key = _key(limitations)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    parsed = {}
    if want_llm:
        try:
            parsed = _llm_analyse(limitations, spec_text) or {}
        except Exception:
            import traceback
            traceback.print_exc()
            parsed = {}

    units = _merge(limitations, parsed)
    by_id = {u["id"]: u for u in units}
    clusters = []
    for c in (parsed.get("clusters") or []):
        if not isinstance(c, dict):
            continue
        members = [str(m).strip() for m in (c.get("members") or []) if str(m).strip() in by_id]
        #  Dedupe while keeping order: a model that lists the same id twice has proposed a pair,
        #  not a quadruple, and the size gate below must judge the real size.
        seen, uniq = set(), []
        for m in members:
            if m not in seen:
                seen.add(m)
                uniq.append(m)
        if not (CLUSTER_MIN <= len(uniq) <= CLUSTER_MAX):
            continue
        text = _norm(c.get("text")) or _cluster_text([by_id[m] for m in uniq])
        clusters.append({"members": uniq, "text": " ".join(text.split()[:60]),
                         "why": _norm(c.get("why"))[:300], "source": "model"})
    if not clusters:
        clusters = _fallback_clusters(units)
    #  SIX QUERIES FOR ONE IDEA IS ONE QUERY. Asked for 3 to 6 combinations, the model returns
    #  variations on its favourite one as often as not: on this claim set three of the six were
    #  "a two-layer seal whose outer layer is harder than the inner". Each costs the deepest
    #  retrieval pass in the run, and the combinations they crowd out are the ones nothing else
    #  asks for.
    deduped = []
    for c in clusters:
        if any(overlap(c["text"], k["text"]) >= 0.7 for k in deduped):
            continue
        deduped.append(c)
    clusters = deduped

    result = {"units": units, "clusters": clusters[:8],
              "source": "model" if parsed.get("units") else "deterministic"}
    with _CACHE_LOCK:
        _CACHE[key] = result
    return result


# ------------------------------------------------------------------------------ the query plan
def query_plan(analysis, max_clusters=6, max_limitations=12, max_generic=2) -> list:
    """The searches this claim set deserves, best first. -> [{"name","kind","text","priority"}]

    THE ORDER IS THE BUDGET. Whatever the caller can afford, it takes from the front, so a run
    that can pay for six passes spends them on the novelty combinations and a run that can pay for
    thirty also reaches the generic components. Reversing this list is how a search ends up
    recommending fifty documents that all disclose the same battery.

      cluster      2 to 4 requirements together. The largest share, because a document that has
                   the combination is prior art and a document that has one member is not.
      limitation   one requirement, verbatim, for recall. Core classes first.
      alternative  one embodiment branch of a requirement that states several. Separate queries
                   on purpose: a laminate and a fluid-filled bladder are not neighbours.
    """
    units = list(analysis.get("units") or [])
    by_id = {u["id"]: u for u in units}
    out = []

    for i, c in enumerate(analysis.get("clusters") or []):
        if i >= max_clusters:
            break
        if not _norm(c.get("text")):
            continue
        out.append({"name": f"cluster{i + 1}", "kind": "cluster", "text": _norm(c["text"]),
                    "priority": 0, "members": list(c.get("members") or []),
                    "why": c.get("why") or ""})

    ordered = sorted(units, key=lambda u: (PRIORITY[u["kind"]], -u.get("specificity", 0.0),
                                           u.get("id", "")))
    n_generic = 0
    n_lim = 0
    for u in ordered:
        if n_lim >= max_limitations:
            break
        if u["kind"] == GENERIC:
            if n_generic >= max_generic:
                continue
            n_generic += 1
        text = u.get("verbatim") or u.get("concept") or ""
        if len(text.split()) < 4:
            continue
        out.append({"name": u["id"], "kind": "limitation", "text": text,
                    "priority": 1 + PRIORITY[u["kind"]], "unit_kind": u["kind"],
                    "genericity": u.get("genericity"), "specificity": u.get("specificity")})
        n_lim += 1
        #  The requirement's normalised concept is a DIFFERENT query when the model actually
        #  rewrote it, and the rewrite is the one written in the words the art uses. Only for a
        #  requirement that carries real novelty: paying twice to ask "a seal coupled to a base"
        #  in two vocabularies buys two views of the same enormous set.
        concept = _norm(u.get("concept"))
        if concept and overlap(concept, text) < 0.85 and len(concept.split()) >= 5 \
                and u["kind"] in CORE_KINDS and float(u.get("specificity") or 0.0) >= 0.5:
            out.append({"name": u["id"] + "[concept]", "kind": "limitation", "text": concept,
                        "priority": 1 + PRIORITY[u["kind"]], "unit_kind": u["kind"]})

    for u in ordered:
        for j, alt in enumerate(u.get("alternatives") or []):
            if len(_norm(alt).split()) < 4:
                continue
            out.append({"name": f"{u['id']}[alt{j + 1}]", "kind": "alternative",
                        "text": _norm(alt), "priority": 4 + PRIORITY[u["kind"]],
                        "unit_kind": u["kind"]})

    #  Deduplicate on the query text: a one-limitation claim and its own cluster can converge, and
    #  paying for the same vector twice buys nothing.
    seen, uniq = set(), []
    for q in out:
        k = q["text"].lower()[:160]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(q)
    return uniq


def claim_first_aspects(analysis, max_aspects=5) -> list:
    """The claim-specific concepts the EXTERNAL planner must ask about before any analogy.

    The audited external plan for the vacuum gripper produced eight problem aspects and reached
    laptops, smartphones, electric vehicles, camera tripods, furniture, tyre monitors and medical
    ventilators, while asking nothing at all about a seal section exposed through peripheral
    openings, a differential hardness between two seal portions, or pressing a seal through an
    opening to bridge a step. Cross-domain reach is worth having; it is worth having SECOND.
    """
    units = {u["id"]: u for u in (analysis.get("units") or [])}
    out = []
    for i, c in enumerate((analysis.get("clusters") or [])[:max_aspects]):
        members = [units[m] for m in (c.get("members") or []) if m in units]
        cpc = []
        for u in members:
            for s in (u.get("cpc") or []):
                c4 = re.sub(r"[^A-Z0-9]", "", str(s).upper())[:4]
                if len(c4) == 4 and c4 not in cpc:
                    cpc.append(c4)
        #  KEYWORDS FROM THIS CLUSTER'S OWN TEXT, not from the union of its members' synonyms.
        #  Clusters share members, so the union route gave all five aspects the identical keyword
        #  string and BigQuery and USPTO were asked the same question five times inside a
        #  hard-capped fan-out.
        kws = _title_terms(c.get("text"))
        for u in members:
            for s in (u.get("synonyms") or [])[:3]:
                for w in _title_terms(s):
                    if w not in kws:
                        kws.append(w)
        out.append({
            "name": f"claim combination {i + 1}",
            "problem": _norm(c.get("why")) or "the combination this claim turns on",
            "devices": [],
            "keywords": kws[:10],
            "cpc": cpc[:5],
            "blurb": _norm(c.get("text")),
            "claim_specific": True,
        })
    return out


#  Claim-drafting words. They are in every claim and in no patent TITLE, so as keywords for a
#  title-matching source they are pure noise: "first portion" matched nothing and cost a query.
_DRAFTING = {
    "first", "second", "third", "portion", "portions", "element", "elements", "member",
    "comprising", "comprises", "wherein", "said", "configured", "disposed", "coupled",
    "respective", "plurality", "least", "one", "more", "another", "thereof", "claim",
    #  Not drafting, but equally absent from any title: linking words long enough to survive the
    #  length floor. They cost a slot in a keyword string that a source matches literally.
    "where", "when", "which", "that", "than", "with", "like", "such", "same", "having",
    "into", "onto", "from", "less", "also", "each", "some", "very", "used", "using",
}


def _title_terms(text) -> list:
    """The content words of `text` that could plausibly appear in a patent TITLE, best first."""
    out = []
    for w in re.findall(r"[a-z0-9\-]+", str(text or "").lower()):
        w = w.strip("-")
        if len(w) < 4 or w in _STOP or w in _DRAFTING or w in out:
            continue
        out.append(w)
    return out[:10]
