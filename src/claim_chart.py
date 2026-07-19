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
SAME deterministic filter as the rationale path, and goes further: an element whose quote is
not grounded is not merely dropped, it is FORCED to verdict="absent" with the reason recorded
in `grounding`.

TWO SEPARATE THINGS MUST HOLD, and conflating them was the bug. Grounding proves the quote was
COPIED FROM the reference. It proves nothing about whether the quote TEACHES the element. An
audit found 7 of 12 coordinate-backed cells were false positives with perfectly real quotes at
perfectly real coordinates — the coordinate was right and the verdict was wrong. So `verify_rows`
runs an independent pass that argues the opposite side, and a "disclosed" verdict the refuter
will not confirm becomes "uncertain" rather than staying green.

`verify_matrix` applies the same check to the element x reference matrix on the report page,
which previously had NO disclosure check whatsoever — a filled cell there meant only that the
retriever returned that publication for that element.

The grounding logic now lives in the leaf module `grounding.py`, which BOTH this module and
`webapp._ground_reads_on` import. It used to be duplicated here to dodge a circular import
(webapp -> claim_chart -> webapp); a leaf module removes the cycle and the drift.
`tests/test_enrichment.py` pins the two to the same threshold.

The 60% bag-of-words rule described below was RETIRED: it scored a quote against a haystack that
grew with the passage, so it weakened silently as reference text got longer. See grounding.py.

Every path has a deterministic fallback: with no LLM, no local text, or a malformed reply
we return a lexical-overlap chart instead of nothing.
"""
from __future__ import annotations

import json
import re

import db
import grounding
import llm

MAX_ELEMENTS = 12
MAX_QUOTE_WORDS = 40
MIN_OVERLAP = grounding.MIN_SPAN     # shared with webapp._ground_reads_on via grounding.py
VERDICTS = ("disclosed", "partial", "uncertain", "absent")

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = set((
    "the of and to in is are for a an be by on as it or from which said with that this "
    "at least one such each may can also then than there their they them these those"
).split())


def _content_words(s: str) -> list[str]:
    return grounding.content_words(s)


def _grounded(quote: str, ref_text: str, min_overlap: float = MIN_OVERLAP) -> bool:
    """True if the quote is actually QUOTED FROM the shown text — local span concentration plus
    word-order fidelity. Delegates to grounding.py, which webapp._ground_reads_on also uses, so
    the two can no longer drift. The previous global bag-of-words rule got easier as passages
    grew; see src/grounding.py for the measurement that retired it."""
    return grounding.grounded(quote, ref_text, min_span=min_overlap)


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
    """Which local passage does this quote come from? Scored with the same length-stable metric
    as the grounding gate, so a quote cannot acquire a citable coordinate from a passage it was
    not actually taken from. Returns {kind, coord, label} or {} (treated as ungrounded)."""
    loc = grounding.best_passage(quote, passages)
    if not loc:
        return {}
    return {"kind": loc["kind"], "coord": loc["coord"], "label": loc["label"],
            "match": loc["span"], "bigram": loc["bigram"]}


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

    # Grounding proved the quotes are real. It did NOT prove they teach the elements, so an
    # independent pass argues the other side and unconfirmed "disclosed" rows become "uncertain".
    final = verify_rows(final)
    result["rows"] = final
    result["stats"] = _stats(final)
    result["stats"]["demoted_ungrounded"] = demoted
    result["stats"]["refuted"] = sum(1 for r in final if r.get("entailment") == "refuted")
    return result


def _stats(rows: list) -> dict:
    return {
        "elements": len(rows),
        "disclosed": sum(1 for r in rows if r["verdict"] == "disclosed"),
        "partial": sum(1 for r in rows if r["verdict"] == "partial"),
        "uncertain": sum(1 for r in rows if r["verdict"] == "uncertain"),
        "absent": sum(1 for r in rows if r["verdict"] == "absent"),
        # "uncertain" contributes NOTHING to coverage. A disputed cell must not read as half a
        # disclosure — that is exactly the arithmetic that made an unreliable chart look decisive.
        "coverage": round(
            sum(1.0 if r["verdict"] == "disclosed" else 0.5 if r["verdict"] == "partial" else 0.0
                for r in rows) / len(rows), 3) if rows else 0.0,
    }


# --- independent refutation pass -------------------------------------------------------
# The grounding gate proves a quote was COPIED from the reference. It says nothing about whether
# that quote ENTAILS the element. The audited failure mode is precisely this gap: a real passage,
# a real coordinate, and a verdict of "disclosed" for an element the passage does not teach
# (measured 7/12 coordinate cells wrong). So a second model pass argues the OPPOSITE side and
# only agreement survives as "disclosed".
_REFUTE_SYS = (
    "You are opposing counsel attacking a claim chart. For each item you are given a claim ELEMENT "
    "and the QUOTE the chart cites as disclosing it. Your job is to REFUTE: does that quote, ON ITS "
    "OWN, actually teach that element to a skilled reader? Sharing keywords, being from the same "
    "field, or describing a related component is NOT disclosure. Be strict and literal.\n"
    'Return STRICT JSON {"items":[{"i":<index>,"entails":true|false,'
    '"why":"<=15 words"}]} with one entry per item, same indices.')


def _refute(pairs: list) -> dict:
    """pairs: [(idx, element, quote)] -> {idx: (entails: bool, why: str)}.
    One batched call. On any failure returns {} and callers keep the optimistic verdict rather
    than mass-downgrading a chart because the verifier was unavailable."""
    if not pairs:
        return {}
    payload = {"items": [{"i": i, "element": el, "quote": q} for i, el, q in pairs]}
    try:
        out = llm.chat_json(_REFUTE_SYS, json.dumps(payload)[:60000], max_tokens=2000) or {}
    except Exception:
        return {}
    res = {}
    for it in (out.get("items") or []):
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        res[i] = (bool(it.get("entails")), str(it.get("why") or "")[:120])
    return res


def verify_rows(rows: list) -> list:
    """Downgrade every 'disclosed' row the refuter will not confirm to 'uncertain'.

    'uncertain' is deliberately a distinct verdict rather than a demotion to 'partial' or
    'absent': the honest statement is that two passes disagreed and a human must look, which is
    different from "the reference partly shows this" and different from "it is not there".
    """
    idx = [(i, r["element"], r.get("quote") or "") for i, r in enumerate(rows)
           if r.get("verdict") == "disclosed" and (r.get("quote") or "").strip()]
    verdicts = _refute(idx)
    if not verdicts:
        for i, _, _ in idx:
            rows[i]["entailment"] = "unverified"
        return rows
    for i, _, _ in idx:
        ent = verdicts.get(i)
        if ent is None:
            rows[i]["entailment"] = "unverified"
            continue
        entails, why = ent
        if entails:
            rows[i]["entailment"] = "confirmed"
        else:
            rows[i]["verdict"] = "uncertain"
            rows[i]["entailment"] = "refuted"
            rows[i]["entailment_note"] = why
            rows[i]["confidence"] = round(min(float(rows[i].get("confidence") or 0.0), 0.4), 2)
    return rows


# --- verification for the element x reference MATRIX -------------------------------------
# The per-reference chart above is heavily defended. The MATRIX on the report page
# (webview.build_claim_chart) was NOT: a green cell there means only "the retriever returned this
# publication for this element with fused score X". No model, and no code, ever checked that the
# cited passage discloses the element -- yet it renders in the visual language of a claim chart,
# where a filled cell reads as verified coverage. That is the surface the 58% false-positive
# measurement came from. This function supplies the missing check.
_CELL_SYS = (
    "You audit cells of a patent claim chart. Each item gives a claim ELEMENT and the ACTUAL text "
    "of the passage a search engine retrieved for it. Decide whether that passage genuinely "
    "discloses the element. Retrieval similarity is NOT disclosure: same field, shared vocabulary, "
    "or a related component is 'unrelated' or 'weak', not 'discloses'.\n"
    'Return STRICT JSON {"items":[{"i":<index>,"verdict":"discloses|weak|unrelated",'
    '"why":"<=15 words"}]} with one entry per item.')

MAX_VERIFY_CELLS = 60


def coord_text(pub: str, coord) -> str | None:
    """Resolve the real local text at a matrix cell's coordinate."""
    if not isinstance(coord, dict) or not coord:
        return None
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s", (pub,))
        r = cur.fetchone()
        if not r:
            return None
        pid = r["id"]
        conds, params = [], [pid]
        for k in ("para_no", "claim_no", "figure_no"):
            if coord.get(k) is not None:
                conds.append(f"coord->>'{k}' = %s")
                params.append(str(coord[k]))
        if conds:
            cur.execute(f"SELECT text FROM chunks WHERE publication_id=%s AND "
                        f"({' OR '.join(conds)}) AND text IS NOT NULL LIMIT 1", params)
            c = cur.fetchone()
            if c:
                return c["text"]
        if coord.get("claim_no") is not None:
            cur.execute("SELECT COALESCE(resolved_text, text) t FROM claims WHERE "
                        "publication_id=%s AND claim_no=%s", (pid, coord["claim_no"]))
            c = cur.fetchone()
            if c:
                return c["t"]
    return None


def verify_matrix(chart: dict, report: dict) -> dict:
    """Annotate every covered matrix cell with a real disclosure verdict.

    Sets cell["verify"] to one of:
      discloses  - passage checked and it does teach the element
      weak       - same topic, does not actually show the element
      unrelated  - spurious citation
      no-coord   - retrieval matched the document as a whole; there is no passage to check, so
                   nothing here was ever verifiable (previously rendered identically to a hit)
      unchecked  - verifier unavailable / budget exceeded

    Only "discloses" may render as coverage. Everything else renders as retrieval-only.
    """
    ev = (report or {}).get("element_evidence", {}) or {}
    todo, cells = [], []
    for row in chart.get("rows", []):
        el = row.get("element", "")
        for cell in row.get("cells", []):
            if not cell.get("covered"):
                continue
            raw = None
            for h in ev.get(el, []):
                if h.get("pub") == cell.get("pub") and isinstance(h.get("coord"), dict):
                    raw = h["coord"]
                    break
            if not raw:
                cell["verify"] = "no-coord"
                continue
            txt = None
            try:
                txt = coord_text(cell["pub"], raw)
            except Exception:
                pass
            if not txt:
                cell["verify"] = "no-coord"
                continue
            cell["verify"] = "unchecked"
            cells.append(cell)
            todo.append({"i": len(cells) - 1, "element": el, "passage": txt[:900]})
    if not todo:
        chart["verification"] = _verify_stats(chart)
        return chart
    truncated = len(todo) > MAX_VERIFY_CELLS
    todo = todo[:MAX_VERIFY_CELLS]
    try:
        out = llm.chat_json(_CELL_SYS, json.dumps({"items": todo})[:120000], max_tokens=4000) or {}
    except Exception:
        out = {}
    for it in (out.get("items") or []):
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(cells):
            v = str(it.get("verdict") or "").lower()
            if v in ("discloses", "weak", "unrelated"):
                cells[i]["verify"] = v
                cells[i]["verify_why"] = str(it.get("why") or "")[:120]
    chart["verification"] = _verify_stats(chart)
    chart["verification"]["truncated"] = truncated
    return chart


def _verify_stats(chart: dict) -> dict:
    tally = {"discloses": 0, "weak": 0, "unrelated": 0, "no-coord": 0, "unchecked": 0}
    for row in chart.get("rows", []):
        for cell in row.get("cells", []):
            if cell.get("covered"):
                tally[cell.get("verify", "unchecked")] = tally.get(cell.get("verify", "unchecked"), 0) + 1
    tally["covered"] = sum(v for k, v in tally.items() if k != "covered")
    checked = tally["discloses"] + tally["weak"] + tally["unrelated"]
    tally["checked"] = checked
    tally["confirmed_rate"] = round(tally["discloses"] / checked, 3) if checked else None
    return tally
