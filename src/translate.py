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

# Function words of the languages EP/DE/WO national-phase text actually appears in.
_FOREIGN_FUNC = set((
    "de la el los las un una que para con por del se su como es en y o al lo"          # Spanish
    " der die das und ein eine mit fur ist den des dem zur zum auf wird bei aus nach"   # German
    " le les du et pour avec dans par qui au aux sur ce il une est"                     # French
    " di il lo gli della delle per con del che una sono"                                # Italian
).split())

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
    a false "this is English" costs nothing (the translator detects the language anyway).
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

    # Cheap pre-check: don't spend calls on text the heuristic is confident is English.
    if not looks_nonenglish(text):
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
            src["claims"] = "\n\n".join(f"{c['claim_no']}. {c['text']}" for c in cur.fetchall() if c["text"])
    for k, v in src.items():
        res["fields"][k] = translate(v, use_cache=use_cache) if v.strip() else \
            {"lang": "", "translated": False, "text": "", "cached": False}
    return res
