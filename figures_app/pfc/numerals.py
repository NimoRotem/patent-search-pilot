"""Deterministic reference-numeral extraction, run before any model sees the document.

The registry this builds is the compiler's spine: it decides which numerals exist, what each
one names, and which paragraphs prove it. A language model is allowed to reconcile aliases and
propose relationships later, but it is never allowed to invent a numeral or move one, so the
quality of everything downstream is bounded by the quality of this pass.

Three things it must get right and one it must refuse:

* **Read only real mentions.** ``the vacuum gripper 100`` is a mention. ``FIG. 1``,
  ``claim 12``, ``about 5 mm``, ``U.S. Pat. No. 9,000,000`` and ``2012`` are not.
* **Tolerate a drafter's vocabulary.** One numeral is routinely introduced as "gripper 100",
  then called "vacuum gripper 100" and later "device 100". Those are aliases of one entity, not
  a conflict.
* **Refuse to guess when the draft really does collide.** Two unrelated names on one numeral,
  each used repeatedly, is a defect in the document. It is reported, never repaired.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .schemas import Evidence, Paragraph

# A candidate is a noun phrase followed by a number, optionally parenthesised, with an optional
# single letter suffix ("110a", "210A"). Four digits is the practical ceiling; a five-digit run
# is a patent number or a date, not a reference sign.
#
# The trailing guard rejects a number that continues into another number — "1.5", "1,000",
# "10/20", "2018-2020" — while accepting one that merely ends a sentence. Rejecting on a bare
# full stop looks equivalent and is not: it silently drops every reference sign that happens to
# fall at the end of a sentence, which in a patent description is a great many of them.
_MENTION = re.compile(
    r"(?P<phrase>(?:[A-Za-z][A-Za-zÀ-ɏ-]{1,24}\s+){0,5}[A-Za-z][A-Za-zÀ-ɏ-]{1,24})"
    r"\s*(?:\(\s*(?P<paren>\d{1,4}[A-Za-z]?)\s*\)|(?P<bare>\d{1,4}[A-Za-z]?))"
    r"(?!\d|[.,/-]\d|%)")

# Words that turn the following number into something other than a reference sign. "step" and
# "part" are deliberately absent: "step 502" is exactly how a flowchart numbers its boxes, and
# "part 14" is a component.
_NOT_A_SIGN = frozenset({
    "fig", "figs", "figure", "figures", "claim", "claims", "table", "tables", "equation",
    "formula", "page", "pages", "column", "columns", "line", "lines", "no", "nos", "number",
    "section", "paragraph", "chapter", "item", "example", "examples", "patent", "pat",
    "publication", "application", "ser", "serial", "appl", "vol", "pp",
    "day", "days", "month", "months", "year", "years", "version", "rev", "sheet", "sheets",
})

# A number followed by one of these is a measurement, not a reference sign.
_UNIT_AFTER = re.compile(
    r"^\s*(?:mm|cm|m|km|nm|um|µm|in|inch|inches|ft|mil|mils|deg|degrees?|°|%|percent"
    r"|kg|g|mg|lb|lbs|n|kn|pa|kpa|mpa|bar|psi|hz|khz|mhz|ghz|v|kv|ma|amps?|a|w|kw|s|sec|secs"
    r"|seconds?|ms|min|mins|minutes?|h|hr|hrs|hours?|rpm|c\b|f\b)\b", re.I)

# Determiners and quantifiers that are never part of a component's name.
_LEADING_NOISE = frozenset({
    "the", "a", "an", "said", "each", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "any", "some", "such", "this", "that", "these", "those", "another",
    "other", "same", "respective", "corresponding", "least", "at", "and", "or", "of", "to",
    "in", "on", "with", "by", "from", "for", "as", "is", "are", "be", "been", "being", "via",
    "into", "onto", "within", "between", "through", "about", "approximately", "e.g", "i.e",
    "shown", "illustrated", "depicted", "example", "exemplary", "wherein", "where", "which",
    "when", "while", "also", "further", "additionally", "may", "can", "will", "would", "shall",
    "comprises", "comprising", "includes", "including", "having", "has", "have",
})

# Ordinals stay in the phrase (a "second plate" is a different object from a "first plate") but
# are stripped when two names are compared, so "first plate 210" and "plate 210" are one entity.
_ORDINAL = frozenset({
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
    "tenth", "upper", "lower", "inner", "outer", "left", "right", "front", "rear", "top",
    "bottom", "proximal", "distal", "primary", "secondary", "additional",
})

_WORD = re.compile(r"[A-Za-zÀ-ɏ-]+")
MIN_NAME_CHARS = 3
MAX_NAME_WORDS = 4
# Below this many mentions a competing name for one numeral is treated as a drafting variant
# rather than as evidence of a genuine collision.
COLLISION_MIN_MENTIONS = 2
COLLISION_MIN_SHARE = 0.25


@dataclass
class Mention:
    numeral: str
    phrase: str
    normalized: str
    paragraph_id: str
    section_id: str
    start: int
    end: int
    quote: str


@dataclass
class RegistryEntry:
    numeral: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    competing: list[tuple[str, int]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.mentions)

    def evidence(self, limit: int = 6) -> list[Evidence]:
        return [Evidence(section_id=m.section_id, paragraph_id=m.paragraph_id,
                         quote_start=m.start, quote_end=m.end, quote=m.quote)
                for m in self.mentions[:limit]]


def normalize_name(phrase: str) -> str:
    """A comparison key for two spellings of one component's name."""
    words = [w.lower() for w in _WORD.findall(phrase or "")]
    words = [w for w in words if w not in _LEADING_NOISE and w not in _ORDINAL]
    words = [_singular(w) for w in words]
    return " ".join(words[-MAX_NAME_WORDS:]).strip()


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


_DETERMINER = frozenset({"the", "a", "an", "said", "each", "its", "their", "his", "her",
                         "this", "that", "these", "those", "another", "one", "any", "such",
                         "every", "both"})


def _clean_phrase(phrase: str) -> str:
    """Trim a captured phrase down to the component's name.

    The determiner is the reliable boundary. In "the housing 110 contains the sensor 120" the
    words before "sensor" are a verb and an article, not part of the component's name, and
    cutting at the LAST determiner gets that right without needing to know which English words
    are verbs. Where there is no determiner the noise list does what it can.
    """
    words = _WORD.findall(phrase or "")
    last_determiner = max((index for index, word in enumerate(words)
                           if word.lower() in _DETERMINER), default=-1)
    if last_determiner >= 0:
        words = words[last_determiner + 1:]
    while words and words[0].lower() in _LEADING_NOISE:
        words.pop(0)
    while words and words[-1].lower() in _LEADING_NOISE:
        words.pop()
    return " ".join(words[-MAX_NAME_WORDS:]).strip()


def _related(left: str, right: str) -> bool:
    """True when two normalized names plausibly denote the same thing.

    The test is the head noun plus containment, which is how patent drafters actually vary a
    name: "gripper" / "vacuum gripper" / "vacuum gripper assembly" share a word and one contains
    the other. "gripper" and "controller" share nothing and are a real collision.
    """
    if not left or not right:
        return True
    if left == right or left in right or right in left:
        return True
    left_words, right_words = set(left.split()), set(right.split())
    if left.split()[-1] == right.split()[-1]:
        return True
    return bool(left_words & right_words) and len(left_words & right_words) >= min(
        len(left_words), len(right_words))


def find_mentions(paragraphs: Iterable[Paragraph]) -> list[Mention]:
    """Every legitimate ``<name> <numeral>`` mention in the supplied paragraphs."""
    out: list[Mention] = []
    for paragraph in paragraphs:
        text = paragraph.text
        for match in _MENTION.finditer(text):
            numeral = (match.group("paren") or match.group("bare") or "").upper()
            if not numeral:
                continue
            tail = text[match.end():match.end() + 16]
            if _UNIT_AFTER.match(tail):
                continue
            phrase_raw = match.group("phrase") or ""
            last_word = (_WORD.findall(phrase_raw) or [""])[-1].lower().strip(".")
            if last_word in _NOT_A_SIGN:
                continue
            # A four-digit number that reads as a year in prose is a date, not a sign. Reference
            # signs in the 1900-2100 band do exist, so this only fires when the phrase is one of
            # the words that introduces a date.
            if re.fullmatch(r"(19|20)\d{2}", numeral) and last_word in {
                    "since", "in", "until", "filed", "published", "issued", "dated"}:
                continue
            name = _clean_phrase(phrase_raw)
            if len(name) < MIN_NAME_CHARS:
                continue
            normalized = normalize_name(name)
            if not normalized:
                continue
            start = match.start("phrase")
            end = match.end()
            out.append(Mention(
                numeral=numeral, phrase=name, normalized=normalized,
                paragraph_id=paragraph.id, section_id=paragraph.section_id,
                start=start, end=end, quote=text[max(0, start - 40):end + 40].strip()))
    return out


def build_registry(paragraphs: Iterable[Paragraph]) -> dict[str, RegistryEntry]:
    """Mentions -> one entry per numeral, with a canonical name and its aliases."""
    grouped: dict[str, list[Mention]] = defaultdict(list)
    for mention in find_mentions(paragraphs):
        grouped[mention.numeral].append(mention)

    registry: dict[str, RegistryEntry] = {}
    for numeral, mentions in grouped.items():
        counts = Counter(m.normalized for m in mentions)
        canonical_norm, _ = counts.most_common(1)[0]
        # Display the longest spelling actually used for the winning name: "vacuum gripper"
        # reads better on a validation report than "gripper", and both are the drafter's words.
        display = max((m.phrase for m in mentions if m.normalized == canonical_norm),
                      key=lambda p: (len(p), p))
        aliases = sorted({m.phrase for m in mentions if m.normalized != canonical_norm})
        competing = [(name, count) for name, count in counts.items() if name != canonical_norm]
        registry[numeral] = RegistryEntry(
            numeral=numeral, canonical_name=display, aliases=aliases,
            mentions=sorted(mentions, key=lambda m: (m.paragraph_id, m.start)),
            competing=competing)
    return dict(sorted(registry.items(), key=lambda item: sort_key(item[0])))


def sort_key(numeral: str) -> tuple[int, str, str]:
    match = re.match(r"^([A-Za-z]*)(\d+)([A-Za-z]*)$", str(numeral or ""))
    if not match:
        return (10 ** 9, str(numeral), "")
    return (int(match.group(2)), match.group(1).upper(), match.group(3).upper())


def collisions(registry: dict[str, RegistryEntry]) -> list[dict]:
    """Numerals whose competing names are too different and too frequent to be variants."""
    out: list[dict] = []
    for entry in registry.values():
        total = entry.count
        canonical = normalize_name(entry.canonical_name)
        for name, count in entry.competing:
            if count < COLLISION_MIN_MENTIONS or count / max(1, total) < COLLISION_MIN_SHARE:
                continue
            if _related(canonical, name):
                continue
            out.append({
                "numeral": entry.numeral, "names": [entry.canonical_name, name],
                "counts": [total - count, count],
                "evidence": entry.evidence(),
            })
    return out


def duplicate_numerals(registry: dict[str, RegistryEntry]) -> list[dict]:
    """One entity name carrying two different numerals.

    Usually a drafting slip, occasionally deliberate (two instances of the same kind of part).
    It is reported as a warning rather than a blocker precisely because both readings are
    common, and the figure can be drawn either way once a human has said which it is.
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    for entry in registry.values():
        by_name[normalize_name(entry.canonical_name)].append(entry.numeral)
    return [{"name": name, "numerals": sorted(numerals, key=sort_key)}
            for name, numerals in sorted(by_name.items()) if len(numerals) > 1]


def numerals_in(text: str) -> set[str]:
    """Every numeral token in a string, used to bind figure captions to their references."""
    return {m.group(1).upper()
            for m in re.finditer(r"(?<![\d.,\-/])(\d{1,4}[A-Za-z]?)(?!\d|[.,/-]\d|%)",
                                 str(text or ""))}


def find_numeral(text: str, registry: dict[str, RegistryEntry]) -> Optional[str]:
    """The registry numeral a free-text phrase names, or None."""
    key = normalize_name(text)
    if not key:
        return None
    for entry in registry.values():
        if normalize_name(entry.canonical_name) == key:
            return entry.numeral
    for entry in registry.values():
        if any(normalize_name(alias) == key for alias in entry.aliases):
            return entry.numeral
    return None
