"""Milestone 9 — qualitative relevance + rationale-accuracy audit harness.

Judges are INDEPENDENT of the ranking/rationale prompts (a skeptical-examiner rubric) so this
isn't self-affirmation; the harness also dumps the actual text excerpts so a human (me) can spot-
check the machine judgments. No paid APIs beyond the Vertex LLM used as the judge.
"""
from __future__ import annotations
import json, re
import db, webview, enrich_display, llm
from config import DATA

REPORTS = DATA / "reports"
RATIONALE = DATA / "rationale"


def ref_text(pub, maxlen=1600):
    """Return the reference's ACTUAL text (title + abstract + claim 1) from the DB, falling back
    to the SerpApi enrichment cache for thin EP/WO/DE docs. This is the ground truth to judge.

    IMPORTANT (M9 fix): old / OCR'd patents (esp. pre-1980 US) have NO abstract and NO rows in the
    `claims` table — their disclosure lives only in `paragraph` / `figure_caption` CHUNKS. Judging
    on title alone made the examiner LLM dismiss genuinely-relevant art (e.g. US-762499-A, a 1904
    handheld vacuum-cup glass lifter) as irrelevant, understating precision. So when title/abstract/
    claim are thin, pull the actual body text from the embedded chunks — the same content the
    retriever matched on. The judge must see what the tool saw."""
    out = {"pub": pub, "title": None, "abstract": None, "claim1": None}
    body = None
    with db.cursor() as cur:
        cur.execute("SELECT id, title, abstract FROM publications WHERE publication_number=%s", (pub,))
        r = cur.fetchone()
        if r:
            out["title"] = r["title"]; out["abstract"] = r["abstract"]
            cur.execute("SELECT text FROM claims WHERE publication_id=%s ORDER BY claim_no LIMIT 1", (r["id"],))
            c = cur.fetchone()
            if c:
                out["claim1"] = c["text"]
            # body text from chunks: prefer abstract/claim chunks, then paragraphs (covers OCR'd
            # old patents whose text is only in chunks). Ordered so the most disclosive kinds win.
            cur.execute(
                "SELECT text FROM chunks WHERE publication_id=%s AND embedding IS NOT NULL "
                "AND kind IN ('abstract','claim_own','claim_resolved','paragraph','figure_caption') "
                "ORDER BY CASE kind WHEN 'abstract' THEN 0 WHEN 'claim_own' THEN 1 "
                "WHEN 'claim_resolved' THEN 2 WHEN 'paragraph' THEN 3 ELSE 4 END, id LIMIT 8",
                (r["id"],))
            chunks = [row["text"] for row in cur.fetchall() if row["text"]]
            if chunks:
                body = " ".join(chunks)
    # fall back to enrichment cache for abstract/claims
    disp = enrich_display.load_cached(pub)
    d = (disp or {}).get("_display") if disp else None
    if d:
        out["abstract"] = out["abstract"] or d.get("abstract")
        if not out["claim1"] and d.get("claims"):
            out["claim1"] = d["claims"][0] if isinstance(d["claims"], list) else None
    parts = [out["title"], out["abstract"], out["claim1"]]
    # if the structured fields are thin (no abstract and no claim), lean on the chunk body so the
    # judge sees the real disclosure instead of a bare title.
    if body and not (out["abstract"] and out["claim1"]):
        parts.append(body)
    snippet = " | ".join(x for x in parts if x)[:maxlen]
    out["snippet"] = snippet
    return out


JUDGE_SYS = (
    "You are a SKEPTICAL patent examiner auditing a prior-art search. You are given an invention "
    "query and one candidate reference's ACTUAL text (title/abstract/claims/body). Judge how "
    "relevant the reference is as PRIOR ART for that invention — i.e. would an examiner plausibly "
    "cite it against SOME claim (for novelty OR for obviousness in combination). Judge prior-art "
    "citeability, NOT whether one reference anticipates the entire feature combination. "
    'Return JSON {"verdict":"relevant|borderline|irrelevant","reason":"<=25 words citing the '
    'overlapping OR missing technical feature"}. Rubric: '
    "relevant = discloses the invention's CORE mechanism AND at least one of its specific claimed "
    "features (or is essentially the same device). "
    "borderline = a device in the same narrow field (e.g. a vacuum gripper/lifter for the same "
    "task) that discloses the core mechanism OR a partial / analogous form of a specific feature "
    "(e.g. a mechanical part-presence sensor when a capacitive one is claimed; a manual vacuum "
    "cup when a powered one is claimed) — art an examiner would cite against a broad claim or for "
    "obviousness. Same broad field with NO feature overlap is at most borderline, never relevant. "
    "irrelevant = NOT the same device class (not a vacuum gripper/lifter at all), or a vacuum "
    "device for an unrelated purpose (vacuum cleaner, packaging, thermoforming), or discloses none "
    "of the mechanism.")


def judge_relevance(query, pub):
    t = ref_text(pub)
    if not t["snippet"]:
        return {"verdict": "irrelevant", "reason": "no text available", "pub": pub, "text": ""}
    usr = f"INVENTION QUERY:\n{query[:900]}\n\nCANDIDATE REFERENCE {pub}:\n{t['snippet']}"
    out = llm.chat_json(JUDGE_SYS, usr, max_tokens=200) or {}
    v = (out.get("verdict") or "irrelevant").lower()
    if v not in ("relevant", "borderline", "irrelevant"):
        v = "borderline"
    return {"verdict": v, "reason": out.get("reason", "")[:140], "pub": pub, "text": t["snippet"][:400]}


RAT_SYS = (
    "You are auditing an AI-generated 'why relevant' rationale for a patent reference AGAINST the "
    "reference's ACTUAL text. Decide if the rationale is faithful. Return JSON "
    '{"verdict":"accurate|overclaims|hallucinates|vague","reason":"<=25 words"}. '
    "accurate = every claim in the rationale is supported by the actual text. "
    "overclaims = asserts the reference discloses a specific element that the actual text does NOT "
    "clearly disclose. hallucinates = mentions concrete features/components absent from the text. "
    "vague = generic ('relates to the field') with no specific, checkable tie to the text.")


def judge_rationale(slug, pub):
    c = RATIONALE / f"{slug}__{pub}.json"
    if not c.exists():
        return None
    try:
        rat = json.loads(c.read_text())
    except Exception:
        return None
    why = (rat.get("why") or "").strip()
    reads = rat.get("reads_on") or []
    if not why:
        return None
    # HARNESS BUG (fixed): the generator is shown title + abstract + BEST-MATCHING PASSAGE, but this
    # judge used to rebuild its own reference text as title + abstract + CLAIM 1, falling back to
    # body chunks only when both were missing. The OPS backfill populated claims and so made that
    # fallback rare, leaving generator and judge reading DIFFERENT TEXT -- the judge would mark a
    # faithful rationale "overclaims" simply because the sentence it was grading cited a passage the
    # judge could not see. Grade against the generator's ACTUAL input when we recorded it.
    # NOTE: this desync inflated the number but was never the whole story; correcting it alone still
    # leaves a real regression, so it is fixed here rather than used to explain the problem away.
    src = (rat.get("_source_text") or "").strip()
    if src:
        shown, basis = src, "generator-input"
    else:
        shown, basis = ref_text(pub)["snippet"], "reconstructed"
    usr = (f"REFERENCE {pub} ACTUAL TEXT:\n{shown}\n\n"
           f"AI RATIONALE:\nwhy: {why}\nreads on: {json.dumps(reads)}")
    out = llm.chat_json(RAT_SYS, usr, max_tokens=200) or {}
    v = (out.get("verdict") or "vague").lower()
    if v not in ("accurate", "overclaims", "hallucinates", "vague"):
        v = "vague"
    return {"verdict": v, "reason": out.get("reason", "")[:140], "pub": pub, "why": why[:200],
            "reads_on": reads, "text": shown[:400], "text_basis": basis}


def coord_text(pub, coord, kind=None):
    """Resolve the ACTUAL text at a claim-chart cell's cited coordinate (claim/para/figure)."""
    if not coord:
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
                conds.append(f"coord->>'{k}' = %s"); params.append(str(coord[k]))
        if conds:
            cur.execute(f"SELECT text FROM chunks WHERE publication_id=%s AND ({' OR '.join(conds)}) "
                        f"AND text IS NOT NULL LIMIT 1", params)
            c = cur.fetchone()
            if c:
                return c["text"]
        if coord.get("claim_no") is not None:   # fall back to the claims table
            cur.execute("SELECT COALESCE(resolved_text, text) t FROM claims WHERE publication_id=%s "
                        "AND claim_no=%s", (pid, coord["claim_no"]))
            c = cur.fetchone()
            if c:
                return c["t"]
    return None


CELL_SYS = (
    "You are auditing one cell of a patent claim chart. You are given an invention ELEMENT and the "
    "ACTUAL text at the reference passage the tool cited for that element. Decide whether that "
    'passage genuinely relates to the element. Return JSON {"verdict":"related|weak|unrelated",'
    '"reason":"<=20 words"}. related = the passage discloses or clearly concerns that element. '
    "weak = same general topic but the passage does not really show the element (a loose match). "
    "unrelated = the passage is about something else entirely (a spurious citation).")


def judge_cell(element, pub, coord, kind=None):
    txt = coord_text(pub, coord, kind)
    if not txt:
        return {"verdict": "no_text", "reason": "coordinate text not found", "pub": pub}
    usr = f"INVENTION ELEMENT:\n{element}\n\nCITED PASSAGE ({pub} {coord}):\n{txt[:900]}"
    out = llm.chat_json(CELL_SYS, usr, max_tokens=150) or {}
    v = (out.get("verdict") or "weak").lower()
    if v not in ("related", "weak", "unrelated"):
        v = "weak"
    return {"verdict": v, "reason": out.get("reason", "")[:120], "pub": pub,
            "coord": coord, "text": txt[:300]}


def audit_chart_cells(slugs):
    """§3: for each report, judge every coord-backed claim-chart cell + count whole-only cells."""
    results = []
    for slug in slugs:
        rep = json.loads((REPORTS / f"{slug}.json").read_text())
        chart = webview.build_claim_chart(rep)
        coord_cells, whole_only = [], 0
        for row in chart["rows"]:
            el = row["element"]
            for cell in row["cells"]:
                if not cell.get("covered"):
                    continue
                # recover the raw coord from element_evidence (chart cell coord is a display string)
                raw = None
                for h in rep.get("element_evidence", {}).get(el, []):
                    if h.get("pub") == cell["pub"] and h.get("coord"):
                        raw = h["coord"] if isinstance(h["coord"], dict) else None
                        break
                if raw:
                    j = judge_cell(el, cell["pub"], raw)
                    j["element"] = el; j["score"] = cell["score"]
                    coord_cells.append(j)
                else:
                    whole_only += 1
        rel = sum(1 for c in coord_cells if c["verdict"] == "related")
        weak = sum(1 for c in coord_cells if c["verdict"] == "weak")
        unrel = sum(1 for c in coord_cells if c["verdict"] == "unrelated")
        results.append({"slug": slug, "coord_cells": len(coord_cells), "whole_only": whole_only,
                        "related": rel, "weak": weak, "unrelated": unrel, "cells": coord_cells})
        print(f"  {slug:26s} coord-cells={len(coord_cells)} related={rel} weak={weak} "
              f"unrelated={unrel} | whole-only(no coord)={whole_only}", flush=True)
    return results


def top_cards(slug, n=10):
    rep = json.loads((REPORTS / f"{slug}.json").read_text())
    v = webview.build_view(rep, top_n=max(n, 12))
    return rep.get("query", ""), v["cards"][:n], v


def audit_relevance(queries, n=10):
    """queries: list of (label, slug). Returns per-query precision@10."""
    results = []
    for label, slug in queries:
        query, cards, _ = top_cards(slug, n)
        judged = [judge_relevance(query, c["pub"]) for c in cards]
        rel = sum(1 for j in judged if j["verdict"] == "relevant")
        bor = sum(1 for j in judged if j["verdict"] == "borderline")
        irr = sum(1 for j in judged if j["verdict"] == "irrelevant")
        p_strict = rel / len(judged) if judged else 0
        p_lenient = (rel + 0.5 * bor) / len(judged) if judged else 0
        results.append({"label": label, "slug": slug, "query": query[:80],
                        "n": len(judged), "relevant": rel, "borderline": bor, "irrelevant": irr,
                        "precision_strict": round(p_strict, 2), "precision_lenient": round(p_lenient, 2),
                        "judged": judged})
        print(f"  {label:16s} p@10 strict={p_strict:.2f} lenient={p_lenient:.2f}  "
              f"(rel {rel} / bord {bor} / irr {irr})", flush=True)
    return results
