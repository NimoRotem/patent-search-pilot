"""Patent-aware translation into English + a no-LLM source-language heuristic.

Ported from the federated app's `llm.py`. Two pieces:

  * `looks_nonenglish(text)` — a pure, deterministic, NO-LLM language guess. Cheap enough
    to run on every card in a result list, so the UI can proactively flag "this reference
    is in German" without spending a model call per row. The pilot's corpus is 8,876 DE
    and 8,071 EP publications (many with German or French text), so this fires often.
  * `translate(text)` — chunked, cached, patent-aware LLM translation.

Adapted to the pilot: the federated version was async (`httpx` + its own `_gen`); this one
is synchronous and goes through the pilot's `src/llm.py` (`chat_json`, gemini-2.5-flash via
Vertex, thinking_budget=0, response_mime_type=application/json, tenacity retry). Per the
pilot's convention that EVERY LLM call has a deterministic fallback, a failed or disabled
translation returns the ORIGINAL text with translated=False — never an empty string, so a
caller can always render something.

The disk cache lives under `data/translations/` keyed by SHA1 of (text, target), because
translation is by far the most expensive per-character LLM call in the app and users
re-open the same reference repeatedly.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import llm
from config import DATA

CHUNK = 5000                     # chars per LLM call — keeps the JSON reply from truncating
CACHE_DIR = Path(DATA) / "translations"

# --- no-LLM language heuristic --------------------------------------------------------
# English function words, plus the patentese that saturates claim text.
_EN_FUNC = set((
    "the of and to in is are for with that this an be by on as it or from which said "
    "wherein comprising according claim claims method system device apparatus least one "
    "having each said therein thereof whereby further"
).split())

# Function words of the languages EP/DE/WO national-phase text actually appears in, kept per
# language so a detected source can also be NAMED (the UI shows "translated from German").
_LANG_FUNC = {
    "Spanish": set("de la el los las un una que para con por del se su como es en y o al lo".split()),
    "German":  set("der die das und ein eine mit fur für ist den des dem zur zum auf wird bei aus "
                   "nach einer einem eines durch dass dadurch gekennzeichnet wobei nicht sich von "
                   "zu im am oder als bis dieser diese dieses an vor über unter zwischen werden "
                   "sind ansprüche anspruch".split()),
    "French":  set("le les du et pour avec dans par qui au aux sur ce il une est".split()),
    "Italian": set("di il lo gli della delle per con del che una sono".split()),
}
_FOREIGN_FUNC = set().union(*_LANG_FUNC.values())


def guess_source_language(text: str) -> str:
    """Best-effort name for the source language. Deterministic, no LLM.

    Only used to LABEL text we already know is non-English; an empty string means "unsure",
    which callers render as a generic "translated" badge rather than a wrong language name.
    """
    words = _WORD_RE.findall((text or "").lower())[:500]
    if not words:
        return ""
    scores = {lang: sum(1 for w in words if w in fn) for lang, fn in _LANG_FUNC.items()}
    lang, hits = max(scores.items(), key=lambda kv: kv[1])
    if hits < 3 or hits / len(words) < 0.02:
        return ""
    # Ambiguous when a second language scores nearly as well (Spanish/Italian share many words).
    runner_up = max(v for k, v in scores.items() if k != lang)
    return lang if hits >= runner_up * 1.5 else ""

_WORD_RE = re.compile(r"[a-zà-ÿ]{2,}")


def _is_non_latin(ch: str) -> bool:
    """Is this codepoint from a non-Latin script?

    NOTE — this FIXES A BUG in the federated original, which tested `ord(ch) > 0x2E80`.
    That catches CJK and Kana but silently misses Cyrillic (U+0400), Greek (U+0370),
    Hebrew (U+0590) and Arabic (U+0600) — every one of which its own docstring claimed to
    detect. A Russian abstract was therefore classified as English and never offered for
    translation. We test two ranges instead:
      * 0x0370-0x1FFF: Greek, Cyrillic, Hebrew, Arabic, Devanagari, Thai, ...
      * 0x2E80 and up: CJK, Kana, Hangul.
    The gap between them (0x2000-0x2E7F: general punctuation, arrows, math) is deliberately
    NOT counted — em dashes, curly quotes and ± appear constantly in English patent text and
    counting them would misfire.
    """
    o = ord(ch)
    return 0x0370 <= o <= 0x1FFF or o >= 0x2E80


def looks_nonenglish(text: str) -> bool:
    """Deterministic guess: is this text NOT English? No LLM, no network.

    Two signals, in order:
      1. Non-Latin codepoint density > 4% (CJK, Cyrillic, Greek, Arabic, Hebrew) -> certain.
      2. Otherwise compare English vs Spanish/German/French/Italian function-word ratios.

    Deliberately conservative: short strings and low-signal text return False, because a
    false "this is foreign" costs a wasted translation call and a wrong UI badge, whereas
    a false "this is English" is comparatively cheap.

    CAVEAT that used to be stated wrongly here: the old docstring claimed a false "English"
    "costs nothing (the translator detects the language anyway)". That was not true. translate()
    uses this function as a PRE-CHECK and returns early when it says English, so the translator
    is never reached and the source text is handed back labelled English. That is exactly how
    German claims ended up rendered as English -- see split_bilingual() for the real cause.
    """
    t = (text or "").strip()
    if len(t) < 40:
        return False
    head = t[:2000]
    non_latin = sum(1 for ch in head if _is_non_latin(ch))
    if non_latin > len(head) * 0.04:
        return True
    words = _WORD_RE.findall(t.lower())[:500]
    if len(words) < 20:
        return False
    en = sum(1 for w in words if w in _EN_FUNC) / len(words)
    fr = sum(1 for w in words if w in _FOREIGN_FUNC) / len(words)
    return (fr > en and fr > 0.05) or en < 0.03


# --- bilingual (source + machine-English) fields ---------------------------------------
# The corpus stores many non-English claims ALREADY BILINGUAL: the original claim immediately
# followed by its English machine translation, concatenated with no separator at all --
#
#   "...verbunden sind.Suction device with ..."
#   "...verschwenkbar ist.1. Lifting device with a mast ..."
#
# Two consequences, both bad:
#   1. looks_nonenglish() sees one blob that is ~half English function words, measures EN 0.230
#      vs FOREIGN 0.142, answers False, and translate() short-circuits -- so /api/translate/DE-*
#      returned German text labelled lang:"English" in 0.13-0.25 s having made no LLM call.
#   2. Even labelled correctly, an attorney was being shown German and English run together
#      mid-sentence, which is unreadable.
#
# Splitting the field fixes both, and costs nothing: the English half is already there, so the
# right answer is to RETURN it rather than pay to re-translate the German half.
#
# Finding the boundary takes two signals together, because either alone is wrong:
#
#   * PUNCTUATION alone is not enough -- the junction is inconsistently marked. It can be
#     ".1. ", ". ", or (most often) a bare "." with no space at all: "...fluchten.Lifting".
#     Claims are also full of interior sentences, so there are many candidates.
#   * LANGUAGE CROSSOVER alone is not precise enough -- scoring every word boundary picks an
#     argmax a few words off the true junction, which strands German at the head of the English
#     half ("Galgens befindet.Gripping means (121)...").
#
# So: enumerate the punctuation candidates (a full stop, optionally a claim number, then a
# capital), and pick the candidate whose split best separates foreign-dominant text from
# English-dominant text. Exact boundaries, and no reliance on any single junction spelling.
_MIN_HALF_WORDS = 8
# The negative lookbehind rejects single-letter abbreviations, so German "z.B." ("e.g.") is not
# mistaken for a sentence end -- it otherwise split DE-2536829-A1 claim 1 at "...z." / "B. Glas-
# scheiben...", handing back German as the English half.
_BOUNDARY_RE = re.compile(r"(?<![\s(][a-zA-ZäöüÄÖÜ])\.\s*(?:\d+\s*\.\s*)?(?=[A-ZÄÖÜ])")


def _looks_mixed(text: str) -> bool:
    """Substantial amounts of BOTH English and a foreign language in one field.

    Backstop for bilingual text whose junction split_bilingual() could not locate (no usable
    punctuation candidate near the midpoint). Such text must never be short-circuited as
    "English" -- that is the original defect. Returning True routes it to the real translator,
    which costs a call but is correct.
    """
    words = _WORD_RE.findall((text or "").lower())[:500]
    if len(words) < 30:
        return False
    en, fr = _lang_votes(words)
    return fr >= 5 and fr / len(words) >= 0.06 and en / len(words) >= 0.06


def _lang_votes(words):
    """(english_votes, foreign_votes) over a word list."""
    en = sum(1 for w in words if w in _EN_FUNC)
    fr = sum(1 for w in words if w in _FOREIGN_FUNC)
    return en, fr


def split_bilingual(text: str):
    """Split "<source-language text><its English translation>" into (source, english).

    Returns None when the text does not look like a source+English pair -- monolingual text of
    either language, text too short to judge, or no boundary with a clear language flip.
    """
    t = (text or "").strip()
    if len(t) < 120:
        return None

    if len(_WORD_RE.findall(t.lower())) < _MIN_HALF_WORDS * 2:
        return None

    # Candidate cuts, restricted to the middle of the text: a claim and its translation are
    # close to the same length, so the junction is always near the midpoint, and this also rules
    # out degenerate near-empty halves.
    cands = [m.end() for m in _BOUNDARY_RE.finditer(t)
             if 0.2 * len(t) <= m.end() <= 0.8 * len(t)]
    if not cands:
        return None

    best = None
    for cut in cands:
        pre = _WORD_RE.findall(t[:cut].lower())
        post = _WORD_RE.findall(t[cut:].lower())
        if len(pre) < _MIN_HALF_WORDS or len(post) < _MIN_HALF_WORDS:
            continue
        en_pre, fr_pre = _lang_votes(pre)
        en_post, fr_post = _lang_votes(post)
        # Fractions, so the two halves compare fairly even at uneven lengths.
        score = ((fr_pre - en_pre) / len(pre)) + ((en_post - fr_post) / len(post))
        if best is None or score > best[0]:
            best = (score, cut)

    if best is None:
        return None
    score, cut = best
    # A genuine DE->EN flip scores well clear of this; monolingual text hovers near 0 and can go
    # slightly positive by chance, which is why the threshold is not simply > 0.
    if score < 0.12:
        return None

    src, eng = t[:cut].strip(), t[cut:].strip()
    # The English half often opens with the claim number ("...ist.2. Vacuum lifting device..."),
    # which the snap above consumes into the source half and leaves dangling there as "2.".
    src = re.sub(r"\s*\d+\s*\.\s*$", "", src).strip()
    if len(src) < 40 or len(eng) < 40:
        return None
    # Final sanity check: the halves must actually differ in language, not just in position.
    #
    # This deliberately does NOT call looks_nonenglish(). That function returns False for
    # anything under 20 words by design, and a single claim's German half is routinely 15-25
    # words -- so gating on it rejected genuine splits whose crossover score was above 1.0
    # (measured: 29 of 120 sampled DE claims, all of them really bilingual). Compare the raw
    # votes instead, which stays meaningful on short halves.
    en_s, fr_s = _lang_votes(_WORD_RE.findall(src.lower()))
    en_e, fr_e = _lang_votes(_WORD_RE.findall(eng.lower()))
    if not (fr_s > en_s and en_e > fr_e):
        return None
    return src, eng


# --- LLM translation ------------------------------------------------------------------
_SYS = (
    "You are a patent translator. Detect the source language of TEXT. If it is already "
    "English, return it UNCHANGED with translated=false. Otherwise translate faithfully "
    "into clear English, preserving claim numbering, technical terms and structure. Do "
    "not summarize, omit or add anything. Return ONLY JSON: "
    '{"lang":"<English name of source language>","translated":<true|false>,'
    '"text":"<the English text>"}'
)


def _cache_key(text: str, target: str) -> str:
    return hashlib.sha1(f"{target}\x00{text}".encode("utf-8")).hexdigest()


def _cached(key: str):
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _store(key: str, val: dict):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.json").write_text(json.dumps(val))
    except Exception:
        pass


def _translate_chunk(text: str, target: str) -> dict:
    """One LLM call. Deterministic fallback = the input text, untranslated."""
    d = llm.chat_json(_SYS, f"TEXT:\n{text}", max_tokens=8000) or {}
    out = d.get("text")
    if not isinstance(out, str) or not out.strip():
        return {"lang": "", "translated": False, "text": text}
    return {"lang": str(d.get("lang") or ""), "translated": bool(d.get("translated")), "text": out}


def translate(text: str, target: str = "English", use_cache: bool = True) -> dict:
    """Translate `text` into English. Returns {lang, translated, text, cached}.

    Long inputs are split at 5000 chars so no single JSON reply truncates. If the FIRST
    chunk comes back already-English we short-circuit and skip the rest — a full patent
    description would otherwise burn a dozen calls to translate English into English.

    Fail-soft: on any error the original text is returned with translated=False.
    """
    text = (text or "").strip()
    if not text:
        return {"lang": "", "translated": False, "text": "", "cached": False}

    key = _cache_key(text, target)
    if use_cache:
        hit = _cached(key)
        if hit is not None:
            hit["cached"] = True
            return hit

    # Already bilingual? Then the English is sitting right there — hand it back instead of
    # paying to translate the German half, and instead of mislabelling the whole blob "English".
    pair = split_bilingual(text)
    if pair is not None:
        src, eng = pair
        res = {"lang": guess_source_language(src), "translated": True, "text": eng,
               "cached": False, "bilingual": True, "source_text": src}
        if use_cache:
            _store(key, dict(res))
        return res

    # Cheap pre-check: don't spend calls on text the heuristic is confident is English.
    # _looks_mixed catches bilingual text we could not cleanly split, so it goes to the
    # translator rather than being returned as "English".
    if not (looks_nonenglish(text) or _looks_mixed(text)):
        res = {"lang": "English", "translated": False, "text": text, "cached": False}
        if use_cache:
            _store(key, res)
        return res

    try:
        if len(text) <= CHUNK:
            res = _translate_chunk(text, target)
        else:
            parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
            first = _translate_chunk(parts[0], target)
            if not first.get("translated"):
                res = {"lang": first.get("lang", ""), "translated": False, "text": text}
            else:
                out = [first["text"]]
                for p in parts[1:]:
                    out.append(_translate_chunk(p, target).get("text") or p)
                res = {"lang": first.get("lang", ""), "translated": True, "text": "\n".join(out)}
    except Exception:
        res = {"lang": "", "translated": False, "text": text}

    res["cached"] = False
    if use_cache:
        _store(key, dict(res))
    return res


def translate_publication(pub: str, fields=("abstract", "claims"), use_cache: bool = True) -> dict:
    """Translate one publication's stored text straight out of the pilot's DB.

    Reads the real rows (publications.abstract, claims.text ordered by claim_no) rather
    than asking the caller to assemble text, so the webapp route is a one-liner. Returns
    {pub, found, fields:{<field>:{lang, translated, text, cached}}}.
    """
    import db
    res = {"pub": pub, "found": False, "fields": {}}
    with db.cursor() as cur:
        cur.execute("SELECT id, abstract FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
        row = cur.fetchone()
        if not row:
            return res
        res["found"] = True
        src = {}
        if "abstract" in fields:
            src["abstract"] = row["abstract"] or ""
        if "claims" in fields:
            cur.execute("SELECT claim_no, text FROM claims WHERE publication_id=%s ORDER BY claim_no",
                        (row["id"],))
            rows = [c for c in cur.fetchall() if c["text"]]
            # The bilingual duplication is PER CLAIM ROW, so it has to be undone per row. Joining
            # first and splitting once would find a single crossover in the middle of claim ~10
            # and mangle everything either side of it.
            claim_src, claim_eng, langs, any_bilingual = [], [], [], False
            for c in rows:
                one = f"{c['claim_no']}. {c['text']}"
                pair = split_bilingual(c["text"])
                if pair:
                    any_bilingual = True
                    s, e = pair
                    claim_src.append(f"{c['claim_no']}. {s}")
                    claim_eng.append(f"{c['claim_no']}. {e}")
                    langs.append(guess_source_language(s))
                else:
                    claim_src.append(one)
                    claim_eng.append(one)
            src["claims"] = "\n\n".join(claim_src)
            if any_bilingual:
                named = [x for x in langs if x]
                res["fields"]["claims"] = {
                    "lang": max(set(named), key=named.count) if named else "",
                    "translated": True, "text": "\n\n".join(claim_eng), "cached": False,
                    "bilingual": True, "source_text": "\n\n".join(claim_src),
                }
    for k, v in src.items():
        if k in res["fields"]:
            continue                                  # already resolved by the bilingual split
        res["fields"][k] = translate(v, use_cache=use_cache) if v.strip() else \
            {"lang": "", "translated": False, "text": "", "cached": False}
    return res
