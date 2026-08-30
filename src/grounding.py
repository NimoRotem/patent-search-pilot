"""Deterministic, length-stable grounding checks for AI-authored evidence quotes.

WHY THIS MODULE EXISTS
----------------------
The original check (`webapp._ground_reads_on` and its copy `claim_chart._grounded`) asked:
what fraction of the quote's content words appear ANYWHERE in the reference text? That is a
bag-of-words test against a haystack SET that grows with the passage. Adding text to the
reference can only ever add words to that set, so the test gets monotonically EASIER as
passages get longer and can never get harder.

Measured consequence: rationale overclaim went 22.5% (no filter) -> 10.0% (filter added) ->
26.3% after an EPO OPS full-text backfill lengthened passages. The filter did not fail loudly;
it silently stopped biting, and ended up WORSE than having no filter at all.

The replacement is LOCAL and LENGTH-STABLE:

  * span_ratio   - the best score over a sliding WINDOW roughly the size of the quote. The
    quote's words must co-occur CLOSE TOGETHER rather than be scattered across 24k characters
    of unrelated text. Appending more text adds more windows but does not make any single
    window score better, so the metric does not drift upward as reference text grows.

  * bigram_ratio - the fraction of the quote's adjacent word pairs that also appear adjacent
    in the source. The prompts demand a VERBATIM quote, and a verbatim quote preserves word
    order; a fabricated or reassembled one does not. Word order is completely invisible to a
    bag-of-words check, which is why the old filter could be cleared by a quote that merely
    reused the reference's vocabulary.

Both metrics are bounded in [0,1] and neither is a function of len(source), which is the
property the old check lacked.

Shared by `webapp` and `claim_chart` deliberately. `claim_chart` previously duplicated the
logic to dodge a circular import (webapp -> claim_chart -> webapp); a leaf module with no
project imports breaks that cycle without duplication, so the two can no longer drift apart.
"""
from __future__ import annotations

import re
from collections import Counter

#  Language guards, shared by the reader (deep_analysis._row), the batch tail (batch_reader)
#  and the view (webview claim chart). Live here because grounding is the one module all three
#  already import without cycles. Doc-level answers "is this text readable at all"; quote-level
#  demands REPEATED foreign function words so an English quote mentioning 'mit' once is never
#  flagged. Soft hyphens stripped: OCR'd German splits words with them.
_EN_HINTS = (" the ", " of ", " and ", " to ", " in ", " is ", " for ", " with ")
_XX_HINTS = (" der ", " die ", " das ", " und ", " mit ", " eine ", " einem ", " einer ",
             " zum ", " dadurch ", " gekennzeichnet ", " les ", " dans ", " pour ",
             " selon ", " une ", " est ")


def mostly_english(text):
    s = " " + (text or "")[:6000].lower().replace("\n", " ").replace("­", "") + " "
    en = sum(s.count(w) for w in _EN_HINTS)
    xx = sum(s.count(w) for w in _XX_HINTS)
    return en >= xx


#  ------------------------------------------------------------------- writing systems, not words
#
#  `mostly_english` compares ENGLISH function words against GERMAN and FRENCH ones. On a script
#  that has neither it scores nought against nought and answers True, so a Japanese claim set reads
#  to it as English. Measured on this corpus: 4,957 claim rows tagged `en` contain CJK characters
#  and 3,497 more are unmistakably German, out of 542,546.
#
#  That is not an academic gap. A reference whose stored text is not English earns almost no
#  verified rows, because an English limitation will not match a verbatim foreign passage, and the
#  1.290 picker ranks on verified rows: it silently dropped the single most on-point examiner-cited
#  reference in the case this was measured on. The same blind spot decides whether the packet
#  attaches the English translation that 1.290(d)(4) requires.
#
#  A script test is not a language detector and does not try to be. It answers the one question
#  those rows actually pose, and it answers it deterministically.
_FOREIGN_SCRIPT = (
    (0x0400, 0x04FF),      # Cyrillic
    (0x0590, 0x05FF),      # Hebrew
    (0x0600, 0x06FF),      # Arabic
    (0x1100, 0x11FF),      # Hangul Jamo
    (0x3000, 0x303F),      # CJK symbols and punctuation
    (0x3040, 0x309F),      # Hiragana
    (0x30A0, 0x30FF),      # Katakana
    (0x3400, 0x4DBF),      # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),      # CJK unified ideographs
    (0xAC00, 0xD7AF),      # Hangul syllables
    (0xF900, 0xFAFF),      # CJK compatibility ideographs
)
#  A stray CJK character in an English patent is ordinary: an assignee name, a units symbol, a
#  transliterated inventor. A tenth of the body is not.
FOREIGN_SCRIPT_SHARE = 0.10


def foreign_script_share(text) -> float:
    """Fraction of the non-space characters that belong to a non-Latin script."""
    s = (text or "")[:6000]
    body = [ch for ch in s if not ch.isspace()]
    if not body:
        return 0.0
    n = 0
    for ch in body:
        o = ord(ch)
        for lo, hi in _FOREIGN_SCRIPT:
            if lo <= o <= hi:
                n += 1
                break
    return n / len(body)


def is_english_text(text) -> bool:
    """English on BOTH axes: not another Latin-script language, and not another writing system.

    Use this, not `mostly_english`, wherever the answer decides something a person relies on.
    `mostly_english` is kept because the reader and the view are tuned against it on Latin text.
    """
    return foreign_script_share(text) < FOREIGN_SCRIPT_SHARE and mostly_english(text)


def quote_is_english(quote):
    s = " " + (quote or "").lower().replace("­", "") + " "
    en = sum(s.count(w) for w in _EN_HINTS)
    xx = sum(s.count(w) for w in _XX_HINTS)
    return xx < 2 or en >= xx

# Thresholds. Tuned on the audited rationale/cell sets - see MEASUREMENT.md. Kept as module
# constants because tests pin webapp and claim_chart to the SAME numbers.
MIN_SPAN = 0.70            # >=70% of the quote's distinct content words inside one local window
MIN_BIGRAM = 0.30          # >=30% of the quote's word pairs appear adjacent in the source
MIN_OVERLAP = MIN_SPAN     # back-compat alias for tests that pinned the old name

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = set((
    "the of and to in is are for a an be by on as it or from which said with that this "
    "at least one such each may can also then than there their they them these those"
).split())


def tokens(s: str) -> list[str]:
    """All word tokens, order preserved. Stopwords KEPT - they carry the word-order signal
    that makes a verbatim quote distinguishable from a bag of reused vocabulary."""
    return _WORD_RE.findall((s or "").lower())


def content_words(s: str) -> list[str]:
    """Substantive tokens only, order preserved. Used for the span test, where stopwords would
    inflate the score (every passage contains 'the')."""
    return [w for w in _WORD_RE.findall((s or "").lower()) if len(w) > 3 and w not in _STOP]


def span_ratio(quote: str, source: str) -> float:
    """Best fraction of the quote's DISTINCT content words found inside a single sliding window
    of the source. Length-stable: independent of how much unrelated text surrounds the match.

    Window is ~2.5x the quote's content-word count so a genuine quote still fits even when the
    source interleaves stopwords and markup the quote dropped.
    """
    q = content_words(quote)
    if not q:
        return 0.0
    qset = set(q)
    st = tokens(source)
    if not st:
        return 0.0
    width = min(len(st), max(12, int(len(q) * 2.5)))
    counts: Counter = Counter()
    present = 0
    best = 0
    for i, tok in enumerate(st):
        if tok in qset:
            counts[tok] += 1
            if counts[tok] == 1:
                present += 1
        if i >= width:
            old = st[i - width]
            if old in qset:
                counts[old] -= 1
                if counts[old] == 0:
                    present -= 1
        if present > best:
            best = present
    return best / len(qset)


def bigram_ratio(quote: str, source: str) -> float:
    """Fraction of the quote's adjacent word pairs that appear adjacent in the source.
    Detects word-order preservation, i.e. actual quoting rather than vocabulary reuse.
    Quotes shorter than two tokens are not testable and score 1.0 (the span test still applies).
    """
    qt = tokens(quote)
    st = tokens(source)
    if len(qt) < 2:
        return 1.0
    if len(st) < 2:
        return 0.0
    src_bigrams = set(zip(st, st[1:]))
    q_bigrams = list(zip(qt, qt[1:]))
    return sum(1 for b in q_bigrams if b in src_bigrams) / len(q_bigrams)


def grounded(quote: str, source: str, min_span: float = MIN_SPAN,
             min_bigram: float = MIN_BIGRAM) -> bool:
    """True only if the quote is BOTH locally concentrated and word-order-faithful.

    Requiring both is the point: span alone still admits a quote assembled from words that
    happen to cluster, and bigrams alone still admit a short verbatim fragment padded with
    invented material.
    """
    if not (quote or "").strip() or not (source or "").strip():
        return False
    return (span_ratio(quote, source) >= min_span
            and bigram_ratio(quote, source) >= min_bigram)


def explain(quote: str, source: str) -> dict:
    """Both metrics plus the verdict - recorded on rows so a reviewer can see WHY something was
    kept or dropped instead of trusting an opaque boolean."""
    sp = round(span_ratio(quote, source), 3)
    bg = round(bigram_ratio(quote, source), 3)
    return {"span": sp, "bigram": bg, "grounded": sp >= MIN_SPAN and bg >= MIN_BIGRAM}


def best_passage(quote: str, passages: list, min_span: float = MIN_SPAN) -> dict:
    """Which passage does this quote actually come from?

    Scores every candidate with the SAME length-stable metric rather than the old global
    overlap, then requires the winner to clear the grounding bar. Returns {} when the quote
    cannot be placed - callers treat that as ungrounded, so a quote that exists nowhere
    specific can never acquire a citable coordinate.
    """
    if not (quote or "").strip():
        return {}
    best, best_span, best_bg = None, 0.0, 0.0
    for p in passages:
        sp = span_ratio(quote, p.get("text") or "")
        if sp > best_span:
            best, best_span = p, sp
            best_bg = bigram_ratio(quote, p.get("text") or "")
    if best is None or best_span < min_span or best_bg < MIN_BIGRAM:
        return {}
    return {"kind": best.get("kind"), "coord": best.get("coord"), "label": best.get("label"),
            "span": round(best_span, 3), "bigram": round(best_bg, 3)}
