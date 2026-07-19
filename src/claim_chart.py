"""Per-element claim charts for ONE reference, grounded in the pilot's own chunk rows.

Ported from the federated app's `llm.claim_chart`, but re-grounded on the pilot's much
better local data. The federated app only had loose blobs of scraped text, so its chart's
`location` was a model-authored guess out of {"abstract","claims","description"}. The pilot
stores claims as real ROWS (claims.claim_no) and every passage as a `chunks` row carrying a
`coord` JSONB, so here the location is RESOLVED DETERMINISTICALLY: we find which chunk the
model's quote actually came from and report that chunk's real coordinate. The model never
authors a citation — it only supplies a quote, and code decides where the quote lives.

ANTI-HALLUCINATION (non-negotiable). An audit measured 22% rationale overclaim, which is
why `webapp._ground_reads_on` exists. A claim chart is a strictly bigger hallucination
surface than a rationale: it invites a verdict on EVERY element, so the model is tempted to
manufacture support for elements that are simply absent. This module therefore applies the
SAME deterministic filter with the SAME 60% content-word-overlap threshold, and goes
further: an element whose quote is not grounded is not merely dropped, it is FORCED to
verdict="absent" with the reason recorded in `grounding`. A chart row can only say
"disclosed" if code verified the quote against the exact text the model was shown.

The grounding logic is duplicated here rather than imported from `webapp.py` on purpose:
webapp is a Flask module owned by another agent and importing it would create a circular
import (webapp would import this). `tests/test_enrichment.py` pins the two to the same
threshold.

Every path has a deterministic fallback: with no LLM, no local text, or a malformed reply
we return a lexical-overlap chart instead of nothing.
"""
from __future__ import annotations

import json
import re

import db
import llm

MAX_ELEMENTS = 12
MAX_QUOTE_WORDS = 40
MIN_OVERLAP = 0.6          # must match webapp._ground_reads_on
VERDICTS = ("disclosed", "partial", "absent")

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = set((
    "the of and to in is are for a an be by on as it or from which said with that this "
    "at least one such each may can also then than there their they them these those"
).split())


def _content_words(s: str) -> list[str]:
    return [w for w in _WORD_RE.findall((s or "").lower()) if len(w) > 3 and w not in _STOP]


def _grounded(quote: str, ref_text: str, min_overlap: float = MIN_OVERLAP) -> bool:
    """True if >=60% of the quote's content words actually appear in the shown text.
    Deterministic anti-overclaim — identical rule to webapp._ground_reads_on."""
    hay = set(_WORD_RE.findall((ref_text or "").lower()))
    words = _content_words(quote)
    if not words:
        return False
    return sum(w in hay for w in words) >= max(1, min_overlap * len(words))


# --- local evidence assembly ----------------------------------------------------------
def _load_reference(pub: str, max_chunks: int = 40) -> dict:
    """Pull one publication's local text as CITABLE UNITS.

    Returns {found, pub, title, passages:[{kind, coord, text, label}]}. Claims come from
    the claims table (authoritative, ordered); abstract from publications; supporting
    passages from chunks. Each unit keeps its real coordinate so a quote can be traced
    back to an exact location instead of a model-guessed section name.
    """
    ref = {"found": False, "pub": pub, "title": "", "passages": []}
    with db.cursor() as cur:
        cur.execute("SELECT id, title, abstract FROM publications WHERE publication_number=%s LIMIT 1",
                    (pub,))
        row = cur.fetchone()
        if not row:
            return ref
        ref["found"] = True
        ref["title"] = row["title"] or ""
        pid = row["id"]
        if (row["abstract"] or "").strip():
            ref["passages"].append({"kind": "abstract", "coord": {}, "label": "abstract",
                                    "text": row["abstract"].strip()})
        cur.execute("SELECT claim_no, text, resolved_text FROM claims WHERE publication_id=%s "
                    "ORDER BY claim_no LIMIT 60", (pid,))
        for c in cur.fetchall():
            t = (c["resolved_text"] or c["text"] or "").strip()
            if t:
                ref["passages"].append({"kind": "claim", "coord": {"claim_no": c["claim_no"]},
                                        "label": f"claim {c['claim_no']}", "text": t})
        # description/other passages from chunks, only to top up the evidence budget
        room = max(0, max_chunks - len(ref["passages"]))
        if room:
            cur.execute("SELECT kind, coord, text FROM chunks WHERE publication_id=%s "
                        "AND kind NOT LIKE 'claim%%' AND kind <> 'abstract' "
                        "AND text IS NOT NULL ORDER BY id LIMIT %s", (pid, room))
            for ch in cur.fetchall():
                coord = ch["coord"] if isinstance(ch["coord"], dict) else {}
                ref["passages"].append({"kind": ch["kind"], "coord": coord,
                                        "label": _coord_label(ch["kind"], coord),
                                        "text": (ch["text"] or "").strip()})
    return ref


def _coord_label(kind: str, coord: dict) -> str:
    if not isinstance(coord, dict):
        return kind or ""
    for k in ("claim_no", "para_no", "paragraph", "fig_no", "figure_no"):
        if coord.get(k) is not None:
            return f"{kind} {coord[k]}"
    return kind or ""


def _locate(quote: str, passages: list) -> dict:
    """Which local passage does this quote come from? Picks the passage with the highest
    content-word overlap. Returns {kind, coord, label} — a REAL coordinate, or {} if the
    quote matches nothing (which the caller treats as ungrounded)."""
    words = _content_words(quote)
    if not words:
        return {}
    best, best_score = None, 0.0
    for p in passages:
        hay = set(_WORD_RE.findall(p["text"].lower()))
        score = sum(w in hay for w in words) / len(words)
        if score > best_score:
            best, best_score = p, score
    if best is None or best_score < MIN_OVERLAP:
        return {}
    return {"kind": best["kind"], "coord": best["coord"], "label": best["label"],
            "match": round(best_score, 3)}


# --- deterministic fallback -----------------------------------------------------------
def _fallback_chart(elements: list, ref: dict) -> list:
    """No-LLM chart by lexical overlap. Never claims "disclosed": the strongest verdict
    reachable without a model reading the text is "partial", because word overlap is
    evidence of topical proximity, not of disclosure. Honest by construction."""
    out = []
    for el in elements:
        words = _content_words(el)
        best, best_score = None, 0.0
        for p in ref.get("passages", []):
            hay = set(_WORD_RE.findall(p["text"].lower()))
            score = (sum(w in hay for w in words) / len(words)) if words else 0.0
            if score > best_score:
                best, best_score = p, score
        if best is not None and best_score >= 0.5:
            snippet = " ".join(best["text"].split()[:MAX_QUOTE_WORDS])
            out.append({"element": el, "verdict": "partial", "quote": snippet,
                        "location": best["label"], "coord": best["coord"], "kind": best["kind"],
                        "confidence": round(min(0.5, best_score / 2), 2),
                        "grounding": "lexical-fallback", "method": "deterministic"})
        else:
            out.append({"element": el, "verdict": "absent", "quote": "", "location": "",
                        "coord": {}, "kind": "", "confidence": 0.0,
                        "grounding": "lexical-fallback", "method": "deterministic"})
    return out


_SYS = (
    "You are a patent examiner building a claim chart against ONE reference. For EVERY "
    "claim element given, decide whether the REFERENCE TEXT below discloses it.\n"
    "- verdict: \"disclosed\" (the text clearly teaches the element), \"partial\" (related "
    "but incomplete or different), or \"absent\" (not in the text).\n"
    "- quote: the EXACT verbatim passage from the reference text that discloses it, copied "
    "word-for-word, at most 40 words. NEVER paraphrase and NEVER invent. Empty string if absent.\n"
    "- confidence: 0.0-1.0.\n"
    "Use ONLY the reference text provided. You have no outside knowledge of this patent. If "
    "the text does not show an element, verdict=\"absent\" with an empty quote — that is the "
    "correct, expected answer, not a failure. Prefer \"absent\" over guessing.\n"
    "Return STRICT JSON: {\"chart\":[{\"element\":\"<verbatim element>\",\"verdict\":\"...\","
    "\"quote\":\"...\",\"confidence\":0.0}]} with every element, in the given order."
)


def build_chart(elements: list, pub: str, ref: dict | None = None) -> dict:
    """Build a grounded claim chart for `pub` against `elements`.

    Returns {pub, found, method, rows:[...], stats:{...}}. Each row carries a REAL local
    coordinate (`coord`/`location`) resolved by code, and a `grounding` field recording how
    the row was verified. Rows whose quote fails the 60% overlap check are demoted to
    verdict="absent" — never silently kept.
    """
    elements = [e for e in (elements or []) if isinstance(e, str) and e.strip()][:MAX_ELEMENTS]
    if ref is None:
        ref = _load_reference(pub)
    result = {"pub": pub, "found": ref.get("found", False), "method": "llm", "rows": [],
              "stats": {}}
    if not elements:
        result["method"] = "none"
        return result
    if not ref.get("found") or not ref.get("passages"):
        # No local text at all -> an LLM could only hallucinate. Deterministic empty chart.
        result["method"] = "no-text"
        result["rows"] = [{"element": e, "verdict": "absent", "quote": "", "location": "",
                           "coord": {}, "kind": "", "confidence": 0.0,
                           "grounding": "no-reference-text", "method": "deterministic"}
                          for e in elements]
        return result

    shown = f"TITLE: {ref.get('title','')}\n\n" + "\n\n".join(
        f"[{p['label']}] {p['text']}" for p in ref["passages"])
    shown = shown[:24000]

    payload = {"reference": pub, "claim_elements": elements, "reference_text": shown}
    out = llm.chat_json(_SYS, json.dumps(payload)[:60000], max_tokens=3000) or {}
    rows = out.get("chart")
    if not isinstance(rows, list) or not rows:
        res = _fallback_chart(elements, ref)
        result.update({"method": "fallback", "rows": res})
        result["stats"] = _stats(res)
        return result

    by_el = {r.get("element", ""): r for r in rows if isinstance(r, dict)}
    final, demoted = [], 0
    for el in elements:
        r = by_el.get(el)
        if not r:
            # prefix realignment: the model paraphrased the element back at us
            r = next((rr for rr in rows if isinstance(rr, dict)
                      and (rr.get("element") or "")[:24].lower() == el[:24].lower()), None)
        if not r:
            final.append({"element": el, "verdict": "absent", "quote": "", "location": "",
                          "coord": {}, "kind": "", "confidence": 0.0,
                          "grounding": "no-row-returned", "method": "llm"})
            continue
        verdict = str(r.get("verdict") or "absent").lower()
        if verdict not in VERDICTS:
            verdict = "absent"
        quote = " ".join(str(r.get("quote") or "").split()[:MAX_QUOTE_WORDS])
        try:
            conf = float(r.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        if verdict == "absent" or not quote:
            final.append({"element": el, "verdict": "absent", "quote": "", "location": "",
                          "coord": {}, "kind": "", "confidence": conf,
                          "grounding": "model-absent", "method": "llm"})
            continue
        # DETERMINISTIC GATE: the quote must exist in the text we actually showed.
        if not _grounded(quote, shown):
            demoted += 1
            final.append({"element": el, "verdict": "absent", "quote": "", "location": "",
                          "coord": {}, "kind": "", "confidence": 0.0,
                          "grounding": "dropped-ungrounded-quote", "method": "llm"})
            continue
        loc = _locate(quote, ref["passages"])
        if not loc:
            demoted += 1
            final.append({"element": el, "verdict": "absent", "quote": "", "location": "",
                          "coord": {}, "kind": "", "confidence": 0.0,
                          "grounding": "dropped-unlocatable-quote", "method": "llm"})
            continue
        final.append({"element": el, "verdict": verdict, "quote": quote,
                      "location": loc["label"], "coord": loc["coord"], "kind": loc["kind"],
                      "confidence": conf, "grounding": "verified", "method": "llm"})

    result["rows"] = final
    result["stats"] = _stats(final)
    result["stats"]["demoted_ungrounded"] = demoted
    return result


def _stats(rows: list) -> dict:
    return {
        "elements": len(rows),
        "disclosed": sum(1 for r in rows if r["verdict"] == "disclosed"),
        "partial": sum(1 for r in rows if r["verdict"] == "partial"),
        "absent": sum(1 for r in rows if r["verdict"] == "absent"),
        "coverage": round(
            sum(1.0 if r["verdict"] == "disclosed" else 0.5 if r["verdict"] == "partial" else 0.0
                for r in rows) / len(rows), 3) if rows else 0.0,
    }
