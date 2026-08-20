"""CONCISE DESCRIPTION OF RELEVANCE — third-party submission under 37 CFR 1.290.

A finished search already holds the thing a preissuance submission is made of: for every reference
read, a per-limitation verdict with the verbatim passage that supports it and the coordinate that
passage sits at. This module pivots that evidence from the search's axis (limitation x document)
onto the filing's axis (one document, its claims in order) and emits the two-column table a
practitioner files: claim language on the left, relevant disclosure of that document on the right.

THREE RULES THIS MODULE DOES NOT BEND, because the output is filed at the USPTO:

1. A CITATION IS NEVER WRITTEN BY A MODEL. Every coordinate rendered comes from the cell's own
   `location`/`coord`, recorded when the passage was read. The language model is given the cells
   and asked only to phrase them; the citations are re-attached afterwards from the data. A
   fabricated column or paragraph number in a filed paper is the one failure mode worth designing
   the whole module around.
2. A QUOTATION IS ONLY SHOWN FOR A VERIFIED CELL ON THE STRONG BAR. The search records two bars:
   "discloses" (a verbatim passage survived grounding) and "teaches" (the reference conveys the
   idea with no quotable passage). A teaches cell still earns a row, because it is real evidence,
   but it is rendered as a characterisation with no quotation marks, because there is no passage
   to stand behind them.
3. A CLAIM WITH NO EVIDENCE GETS NO ROW. The attorney examples do this too: Document 4 in the
   reference set speaks to claims 13, 14 and 20 and is silent on the rest. Silence is a finding;
   an empty row invites the examiner to read one in.

The subject's own claim text is quoted verbatim for independent claims (that is what the examples
do, and the left column is a quotation of the application, not a summary of it) and paraphrased for
dependent claims, where the examples use a parenthetical.
"""
from __future__ import annotations

import json
import os
import re
import traceback

CITE_MAX = int(os.environ.get("CONCISE_CITES_PER_ROW", "4"))
#  A row is worth filing when the reference actually says something about the claim. "absent" and
#  "uncertain" are not findings, and an unverified cell has no passage behind it.
_USABLE_VERDICTS = ("disclosed", "partial")


# --------------------------------------------------------------------------- biblio


def _display(pub, allow_fetch=True):
    """Biblio for the cover block. Cache first; fetch only for the handful actually selected.

    The cover block is the part an examiner uses to identify the document, so a missing inventor
    or date is worth one enrichment call. `allow_fetch=False` keeps the picker free.
    """
    try:
        import enrich_display
    except Exception:
        return {}
    try:
        disp = (enrich_display.load_cached(pub) or {}).get("_display") or {}
    except Exception:
        disp = {}
    if disp.get("title") and (disp.get("publication_date") or disp.get("priority_date")):
        return disp
    if not allow_fetch:
        return disp
    try:
        got = enrich_display.enrich_for_display(pub) or {}
        #  load_cached returns the whole cache file (_display/mongo/raw); enrich_for_display
        #  returns the compact display dict itself. Accept either shape.
        return (got.get("_display") or got) if isinstance(got, dict) else disp
    except Exception:
        traceback.print_exc()
        return disp


def _first_inventor(disp):
    inv = disp.get("inventors") or []
    if isinstance(inv, str):
        inv = [inv]
    for i in inv:
        name = (i.get("name") if isinstance(i, dict) else str(i or "")).strip()
        if name:
            return name
    return ""


def _us_style(pub):
    """US-11413727-B2 -> ("U.S. Patent No. 11,413,727", "patent").

    A granted US number is filed with comma separators and a pre-grant publication as
    "US 2025/0033224 A1"; anything else keeps its office prefix. Getting this wrong is not
    cosmetic, it is how the examiner finds the document.
    """
    s = (pub or "").upper().replace(" ", "")
    #  Pre-grant publication: 4-digit year + a 7-digit serial. The corpus drops the serial's
    #  leading zero on some rows (US-2023103821-A1 is 2023 + 0103821), so accept 6 or 7 and
    #  zero-pad. Filing "US 2023103821 A1" instead of "US 2023/0103821 A1" is the kind of
    #  formality that gets a submission bounced.
    m = re.match(r"^US-?(\d{4})(\d{6,7})-?([A-Z]\d?)?$", s)
    if m:
        return "U.S. Patent Application Publication No. US %s/%s %s" % (
            m.group(1), m.group(2).zfill(7), m.group(3) or "A1"), "publication"
    m = re.match(r"^US-?(\d{6,8})-?([AB]\d?)?$", s)
    if m:
        n = m.group(1)
        return "U.S. Patent No. %s" % ("{:,}".format(int(n))), "patent"
    return (pub or "").replace("-", " ").strip(), "foreign"


def biblio(pub):
    disp = _display(pub)
    label, kind = _us_style(pub)
    return {
        "pub": pub,
        "label": label,
        "kind": kind,
        "title": (disp.get("title") or "").strip(),
        #  The issuing office, because the date alone does not decide availability: 102(a)(2)
        #  reaches only US and PCT publications. See submission_compliance.qualify.
        "country": (disp.get("country") or "").strip().upper() or _bare(pub)[:2],
        "inventor": _first_inventor(disp),
        "assignee": ", ".join([a for a in (disp.get("assignees") or []) if a][:2]),
        "publication_date": (disp.get("publication_date") or "").strip(),
        "priority_date": (disp.get("priority_date") or "").strip(),
        "filing_date": (disp.get("filing_date") or "").strip(),
        "abstract": (disp.get("abstract") or "").strip(),
    }


def _pretty_date(iso):
    if not iso:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(iso))
    if not m:
        return str(iso)
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    y, mo, d = m.groups()
    try:
        return "%s %d, %s" % (months[int(mo) - 1], int(d), y)
    except Exception:
        return str(iso)


# --------------------------------------------------------------------------- citations


def _cite(cell):
    """One human-readable coordinate, built from the cell's own recorded position.

    The corpus stores US pre-grant text by paragraph number and granted claims by claim number, so
    that is what gets cited. Column/line coordinates are NOT synthesised: the reader never saw a
    column-and-line layout, and inventing one would be a fabricated citation in a filed paper.
    """
    coord = cell.get("coord")
    if isinstance(coord, str):
        try:
            coord = json.loads(coord.replace("'", '"'))
        except Exception:
            coord = {}
    coord = coord if isinstance(coord, dict) else {}
    para = coord.get("para_no") or ""
    if para:
        p = re.sub(r"^p0*", "", str(para)) or str(para)
        return "Paragraph [%s]" % p.zfill(4)
    if coord.get("claim_no"):
        return "Claim %s" % coord["claim_no"]
    if coord.get("figure") or coord.get("fig_no"):
        return "FIG. %s" % (coord.get("figure") or coord.get("fig_no"))
    loc = (cell.get("location") or "").strip()
    if not loc:
        return ""
    m = re.match(r"^paragraph\s+p?0*(\d+)$", loc, re.I)
    if m:
        return "Paragraph [%s]" % m.group(1).zfill(4)
    m = re.match(r"^claim\s+(\d+)$", loc, re.I)
    if m:
        return "Claim %s" % m.group(1)
    if loc.lower().startswith("abstract"):
        return "Abstract"
    return loc[:80]


def _dedupe_keep_order(items):
    seen, out = set(), []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


# --------------------------------------------------------------------------- assembly


def _claim_no(label):
    m = re.search(r"claim\s+(\d+)", str(label or ""), re.I)
    return int(m.group(1)) if m else 0


def _paraphrase(text, limit=180):
    """A parenthetical for a dependent claim, taken from the claim's own words.

    Deterministic on purpose: it is the application's language, trimmed, not a restatement.
    """
    t = " ".join(str(text or "").split())
    t = re.sub(r"^\d+\.\s*", "", t)
    t = re.sub(r"^The\s+\w+\s+(of|according to)\s+claim\s+\d+[,;]?\s*", "", t, flags=re.I)
    t = re.sub(r"^wherein\s+", "", t, flags=re.I)
    if len(t) <= limit:
        return t.rstrip(" .;,")
    cut = t[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" .;,") + " …"


def rows_for_reference(ref, claims):
    """The table rows for ONE reference, in the application's own claim order.

    Independent claims get a row per limitation with the limitation quoted verbatim, which is how
    an examiner reads a chart. Dependent claims collapse to a single row, as in the examples.
    """
    by_label = {}
    for c in (ref.get("claims") or []):
        if not isinstance(c, dict):
            continue
        if (c.get("verdict") or "") not in _USABLE_VERDICTS:
            continue
        if (c.get("grounding") or "") != "verified":
            continue
        by_label[c.get("item")] = c

    meta = {c.get("label"): c for c in claims if isinstance(c, dict)}
    order = sorted({_claim_no(l) for l in by_label} - {0})
    rows = []
    for n in order:
        labels = [l for l in by_label if _claim_no(l) == n]
        labels.sort(key=lambda l: str(l))
        indep = any(bool((meta.get(l) or {}).get("independent")) for l in labels)
        if indep:
            for l in labels:
                cell = by_label[l]
                m = meta.get(l) or {}
                rows.append(_row(n, m.get("text") or "", cell, quote_claim=True, label=l))
        else:
            best = max(labels, key=lambda l: (by_label[l].get("bar") == "discloses",
                                              float(by_label[l].get("confidence") or 0)))
            m = meta.get(best) or {}
            merged = dict(by_label[best])
            merged["_extra_cites"] = [_cite(by_label[l]) for l in labels if l != best]
            rows.append(_row(n, m.get("text") or "", merged, quote_claim=False, label=best))
    return rows


_HEDGE = re.compile(
    r"\s*(?:however|but|although|though|that said)\b[^.]*?\b(?:not|no|lacks?|fails?|silent|"
    r"absent|unclear)\b[^.]*\.", re.I)


def _filing_safe(note):
    """Strip the analyst hedge from a note so the fallback prose reads as disclosure, not argument.

    The reader writes for the ledger ("The reference discloses X. However, it does not explicitly
    mention Y."). The second sentence is exactly what a 1.290 submission may not say, because it
    argues the merits. Whether the disclosure is partial is already recorded in the verdict.
    """
    t = " ".join(str(note or "").split())
    t = _HEDGE.sub("", t)
    return t.strip(" ,;")


def _row(claim_no, claim_text, cell, quote_claim, label):
    cites = _dedupe_keep_order([_cite(cell)] + list(cell.get("_extra_cites") or []))[:CITE_MAX]
    strong = (cell.get("bar") or "") == "discloses"
    return {
        "claim_no": claim_no,
        "label": label,
        "quote_claim": bool(quote_claim),
        "claim_text": " ".join(str(claim_text or "").split()),
        "claim_paraphrase": _paraphrase(claim_text),
        "verdict": cell.get("verdict"),
        "bar": cell.get("bar") or "",
        "strong": strong,
        #  Only a strong-bar cell carries a passage that survived grounding; a teaches cell is
        #  rendered as a characterisation, never inside quotation marks.
        "quote": (cell.get("quote") or "").strip() if strong else "",
        "note": _filing_safe(cell.get("note")),
        "cites": [c for c in cites if c],
        "confidence": float(cell.get("confidence") or 0),
    }


# --------------------------------------------------------------------------- phrasing


_SYS = (
    "You are a patent practitioner drafting the 'Relevant disclosure' column of a concise "
    "description of relevance for a third-party preissuance submission under 37 CFR 1.290.\n"
    "\n"
    "For EACH row you are given: the claim limitation from the application under examination, and "
    "what a reference was found to disclose about it, including the verbatim passage where one "
    "survived verification.\n"
    "\n"
    "Write, for each row, ONE or TWO sentences stating what THIS REFERENCE discloses that "
    "corresponds to that limitation. Rules:\n"
    "- Describe only what the supplied evidence supports. Add nothing from your own knowledge.\n"
    "- Name the reference's own element numbers when the evidence contains them.\n"
    "- NEVER write a citation, column number, paragraph number, figure number or claim number. "
    "Citations are attached separately from the record. Text like 'Paragraph [0012]' in your "
    "answer is an error.\n"
    "- Do NOT assert anticipation, obviousness, invalidity or patentability. State disclosure "
    "only. A submission under 1.290 may not argue the merits.\n"
    "- Where the evidence is partial, say what IS disclosed and stop. Do NOT write what the "
    "reference fails to disclose, does not teach, or is silent on. The supplied notes are an "
    "analyst's working commentary and often contain such hedges; drop them. A submission "
    "under 1.290 states disclosure, it does not argue a rejection.\n"
    "\n"
    "Also write ONE summary sentence beginning 'This document discloses' that characterises the "
    "reference as a whole, from its title, abstract and the rows.\n"
    "\n"
    'Return JSON: {"summary": "...", "rows": [{"id": <int>, "disclosure": "..."}]}'
)


def phrase(doc, tier="strong"):
    """Fill in `summary` and each row's `disclosure`, grounded in the cells. Best-effort.

    A failure here must not lose the document: every row already carries the reader's own note,
    which is used verbatim as the fallback.
    """
    for i, r in enumerate(doc["rows"]):
        r.setdefault("_id", i)
        r["disclosure"] = r.get("note") or ""
    try:
        import llm
    except Exception:
        return doc
    b = doc["biblio"]
    payload = {
        "reference": {"number": b.get("label"), "title": b.get("title"),
                      "abstract": (b.get("abstract") or "")[:1500]},
        "rows": [{"id": r["_id"],
                  "claim_limitation": (r["claim_text"] or r["claim_paraphrase"])[:700],
                  "evidence_note": r["note"][:700],
                  "verified_passage": r["quote"][:700],
                  "bar": "verbatim passage" if r["strong"] else "taught, no quotable passage"}
                 for r in doc["rows"]],
    }
    try:
        got = llm.chat_json(_SYS, json.dumps(payload, ensure_ascii=False),
                            max_tokens=8000, tier=tier) or {}
    except Exception:
        traceback.print_exc()
        return doc
    summary = " ".join(str(got.get("summary") or "").split())
    if summary:
        doc["summary"] = summary
    out = {}
    for r in (got.get("rows") or []):
        if isinstance(r, dict) and r.get("id") is not None:
            out[int(r["id"])] = " ".join(str(r.get("disclosure") or "").split())
    for r in doc["rows"]:
        text = out.get(r["_id"])
        if text:
            #  Belt and braces on rule 1: strip anything that reads like a citation the model
            #  invented, so a hallucinated coordinate can never reach the page even if the
            #  instruction is ignored.
            text = re.sub(r"\s*[\(\[]?(?:see\s+)?(?:col(?:umn)?\.?\s*\d+[^\)\]\.]*|"
                          r"paragraphs?\s*\[?\d+\]?|¶+\s*\d+|FIGs?\.?\s*\d+[A-Za-z]?)"
                          r"[\)\]]?\s*", " ", text, flags=re.I)
            r["disclosure"] = " ".join(text.split()).strip(" ,;") or r["note"]
    return doc


# --------------------------------------------------------------------------- public


def _ledger_weights(report):
    """pub -> what the ledger says this reference kills. -> ({pub: {...}}, n_claims)

    Read through `limitations.Ledger.from_stored` rather than off the stored summary, so an old
    report is weighed under the 112(d) rule too and a dependent claim's "anticipated" does not
    buy a document a place it has not earned.
    """
    led = (report or {}).get("ledger") or {}
    if not led.get("limitations"):
        return {}, 0
    try:
        import limitations as _lim
        claims = _lim.Ledger.from_stored(led).summary().get("claims") or {}
    except Exception:
        traceback.print_exc()
        return {}, 0
    out = {}
    for label, m in claims.items():
        for pub in (m.get("anticipated_by") or []):
            out.setdefault(pub, {"anticipates": [], "adds": []})["anticipates"].append(label)
        for pub in (m.get("adds_disclosed_by") or []):
            if label not in (out.get(pub, {}).get("anticipates") or []):
                out.setdefault(pub, {"anticipates": [], "adds": []})["adds"].append(label)
    return out, len(claims)


def candidates(report, deep, limit=40, collapse_families=True):
    """References worth offering, ORDERED BY WHAT THEY DO TO THE CLAIMS, not by row count.

    Counsel, 2026-08-20: "the 10-document package contains none of the references the ledger
    credits with anticipation. What's the selection logic?" It was `n_strong` then `n_rows` then
    retrieval rank — a measure of how much a reference SAYS, which is not the same as how much it
    kills, and it is systematically wrong for the references worth filing. A document that
    discloses every limitation of one independent claim beats one that touches thirty limitations
    across twenty claims and completes none of them, and it loses on row count every time.

    The order now is: what the ledger says it anticipates, then whether the Office itself applied
    it against this family, then whole dependent additions, then evidence on the independent
    claims, and only then breadth. One document per DOCDB family, because two members of one
    family are one disclosure and a submission that cites both spends two slots to say one thing.
    """
    claims = deep.get("claims") or []
    weights, _ = _ledger_weights(report)
    applied, considered = set(), set()
    mined = ((report or {}).get("prosecution") or {}).get("mined") or {}
    for a in (mined.get("applied") or []):
        #  DOUBLE PATENTING IS NOT A PRIOR-ART GROUND. An examiner citing the applicant's own
        #  earlier patent under obviousness-type double patenting has not said it is prior art,
        #  and it usually is not: on the measured subject US 12,115,659 is the patent this very
        #  application is a continuation of, with the SAME priority date. Giving it the
        #  examiner-applied boost put the applicant's own parent at the top of a package of art
        #  to file against them.
        if a.get("pub") and "double" not in str(a.get("statute") or "").lower():
            applied.add(a["pub"])
    considered = set(mined.get("considered") or []) - applied
    indep = {c.get("label") for c in claims
             if isinstance(c, dict) and c.get("independent")}

    out = []
    for ref in (deep.get("references") or []):
        rows = rows_for_reference(ref, claims)
        if not rows:
            continue
        pub = ref.get("pub")
        w = weights.get(pub) or {}
        strong_indep = sum(1 for c in (ref.get("claims") or [])
                           if isinstance(c, dict) and c.get("item") in indep
                           and (c.get("bar") or "") == "discloses")
        out.append({
            "pub": pub, "title": ref.get("title") or "",
            "family": ref.get("family"),
            "n_rows": len(rows), "n_strong": sum(1 for r in rows if r["strong"]),
            "claims": sorted({r["claim_no"] for r in rows}),
            "anticipates": sorted(w.get("anticipates") or [], key=_claim_no),
            "adds": sorted(w.get("adds") or [], key=_claim_no),
            "strong_indep": strong_indep,
            #  Authority, not similarity: an examiner used this document against this family.
            "office": ("applied" if pub in applied else
                       "considered" if pub in considered else ""),
            "rank": ref.get("rank") or 9999,
        })
    out.sort(key=lambda d: (-len(d["anticipates"]), d["office"] != "applied",
                            -len(d["adds"]), -d["strong_indep"], d["office"] != "considered",
                            -d["n_strong"], -d["n_rows"], d["rank"]))
    #  FAMILY COLLAPSE AT SELECTION, not after building. Building a document costs a model call and
    #  an enrichment fetch, so a sibling dropped later is money already spent on a page nobody
    #  files. Siblings are named on the survivor so the choice stays visible.
    #
    #  `collapse_families=False` is for the ROUTE'S GATE, which asks "does this publication carry
    #  verified evidence in this report" of a pub the user typed. Collapsing there would refuse a
    #  sibling somebody deliberately chose, which is the opposite of the point.
    if not collapse_families:
        return out[:limit]
    kept, seen_fam = [], {}
    for d in out:
        fam = str(d.get("family") or d["pub"])
        if fam in seen_fam:
            seen_fam[fam].setdefault("family_siblings", []).append(d["pub"])
            continue
        seen_fam[fam] = d
        kept.append(d)
    return kept[:limit]


def _bare(pub):
    """US-2025/033224 A1, US-20250033224-A1, us 2025033224 a1 -> US20250033224A1."""
    return re.sub(r"[^A-Z0-9]", "", str(pub or "").upper())


def subject_facts(label):
    """Effective filing date and owner of the application under examination, from the corpus.

    The EFD is not cosmetic here: it is the line that decides whether a reference is prior art at
    all, so it is read from the publications table rather than typed in and trusted.
    """
    out = {"efd": None, "assignees": [], "pub": label}
    if not label:
        return out
    #  MATCH ON EVERY SPELLING OF THE SAME NUMBER. A US pre-grant publication is a 4-digit year
    #  plus a 7-digit serial, but this corpus stores some rows with the serial's leading zero
    #  dropped: the report calls the target US-20250033224-A1 and the publications table calls it
    #  US-2025033224-A1. Matching one spelling silently returns no effective filing date, and a
    #  missing EFD means the prior-art check cannot run at all and every document ships marked
    #  "basis unknown" — which is exactly what happened on 2026-08-19 before this fix.
    keys = {_bare(label)}
    m = re.match(r"^US(\d{4})(\d{4,8})([A-Z]\d?)?$", _bare(label))
    if m:
        year, serial, kind = m.group(1), m.group(2), m.group(3) or ""
        core = serial.lstrip("0") or "0"
        #  The corpus drops SOME leading zeros, not all of them: the true serial 0033224 is stored
        #  as 033224 here. So offer the serial at every plausible width rather than guessing which
        #  one this row used.
        for width in (5, 6, 7, 8):
            if len(core) <= width:
                keys.add("US%s%s%s" % (year, core.zfill(width), kind))
        keys.add("US%s%s%s" % (year, core, kind))
    try:
        import db
        with db.cursor() as cur:
            cur.execute(
                "SELECT earliest_priority_date, filing_date, publication_date, title "
                "FROM publications WHERE replace(upper(publication_number),'-','') = ANY(%s) "
                "LIMIT 1", (sorted(keys),))
            row = cur.fetchone()
        if row:
            out["efd"] = row.get("earliest_priority_date") or row.get("filing_date")
            out["title"] = row.get("title")
    except Exception:
        traceback.print_exc()
    try:
        disp = _display(label, allow_fetch=False)
        out["assignees"] = [a for a in (disp.get("assignees") or []) if a]
        if not out.get("title"):
            out["title"] = disp.get("title")
    except Exception:
        pass
    return out


#  A wrapper document is offered under this id rather than a publication number, because it is not
#  a publication and must never be looked up as one.
OA_PREFIX = "OA:"


def _claims_listed(text):
    """"1-3, 5-9, 11-12 and 14-15" -> [1,2,3,5,6,7,8,9,11,12,14,15]."""
    out = []
    for part in re.split(r"[,;]|\band\b", str(text or "")):
        m = re.match(r"\s*(\d+)\s*(?:[-–—]\s*(\d+))?\s*$", part)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if b < a or b - a > 200:
            continue
        out.extend(range(a, b + 1))
    return sorted(set(out))


def office_action_candidates(report):
    """The family's office actions, offered as documents in their own right. -> [candidate]

    Counsel's own Document 6 was the examiner's Non-Final Rejection from the abandoned parent
    application. It is not prior art and no search engine can produce it: it lives in a file
    wrapper, it is non-patent literature, and finding it requires knowing the parent exists. What
    it IS, is a printed publication under 37 CFR 1.290(a) in which a USPTO examiner has already
    made limitation-by-limitation findings on substantially these claims — so the examiner's
    analysis does the arguing that 1.290(b) forbids the submitter from doing.
    """
    mined = ((report or {}).get("prosecution") or {}).get("mined") or {}
    out = []
    for d in (mined.get("documents") or []):
        applied = [a for a in (d.get("applied") or []) if a.get("number")]
        if d.get("code") not in ("CTNF", "CTFR") or not applied:
            continue
        claims = sorted({n for a in applied for n in _claims_listed(a.get("claims"))})
        out.append({
            "pub": "%s%s/%s" % (OA_PREFIX, d.get("app"), d.get("date")),
            "title": "%s, U.S. Application No. %s, %s" % (
                d.get("description") or "Office Action", _pretty_app(d.get("app")),
                _pretty_date(d.get("date"))),
            "family": None, "kind": "office_action", "office": "applied",
            "n_rows": len(applied), "n_strong": len(applied), "strong_indep": 0,
            "claims": claims, "anticipates": [], "adds": [], "rank": 0,
            "summary": d.get("summary") or "", "applied": applied, "date": d.get("date"),
            "app": d.get("app"), "pdf": d.get("pdf") or "",
        })
    return out


def _pretty_app(app):
    """"17724791" -> "17/724,791", the way an application number is written on a filing."""
    a = re.sub(r"\D", "", str(app or ""))
    if len(a) != 8:
        return app or ""
    return "%s/%s,%s" % (a[:2], a[2:5], a[5:])


def office_action_doc(cand, subject, n=1):
    """One office action as a filing document model. No model call: the facts are already facts.

    Every row states what the document SAYS — which claims an examiner rejected, over what, under
    which section. That is a description of the document, which is what 1.290(d)(2) asks for, and
    it is not an argument about patentability, which is what 1.290 forbids.
    """
    where_doc = "%s mailed %s in U.S. Application No. %s" % (
        (cand.get("title") or "Office Action").split(",")[0], _pretty_date(cand.get("date")),
        _pretty_app(cand.get("app")))
    rows = []
    for a in (cand.get("applied") or []):
        listed = _claims_listed(a.get("claims"))
        statute = str(a.get("statute") or "").strip()
        where = "claim%s %s" % ("s" if len(listed) != 1 else "", a.get("claims") or "")
        #  Prefer the number the corpus resolved: a form prints "11,413,727" as often as
        #  "US 11,413,727", and a filing document should carry the full one either way.
        ref, _kind = _us_style(a.get("pub") or "")
        ref = ref or str(a.get("number") or "").strip()
        rows.append({
            "claim_no": (listed or [0])[0],
            "label": where.strip(),
            "quote_claim": False,
            "claim_text": "",
            "claim_paraphrase": where.strip(),
            "verdict": "disclosed", "bar": "discloses", "strong": True,
            "quote": "",
            #  Factual and attributed: the examiner did this, on this date, in this application.
            #  It states what the document SAYS, which 1.290(d)(2) asks for; it does not argue that
            #  the pending claims are unpatentable, which 1.290(b) forbids.
            "note": ("The examiner rejected %s over %s%s."
                     % (where.strip(), ref,
                        " under 35 U.S.C. %s" % statute if statute and "double" not in
                        statute.lower() else
                        " on the ground of nonstatutory double patenting" if statute else "")),
            "cites": [where_doc],
            "confidence": 1.0,
            "claims_listed": listed,
        })
    return {
        "n": n,
        "pub": cand["pub"],
        "kind": "office_action",
        "family_id": None,
        "biblio": {
            "pub": cand["pub"],
            "label": where_doc,
            "kind": "", "country": "US",
            "title": cand.get("title") or "Office Action",
            "inventor": "", "assignee": "United States Patent and Trademark Office",
            "publication_date": cand.get("date") or "", "priority_date": "", "filing_date": "",
            "issue_date_pretty": _pretty_date(cand.get("date")),
            "priority_date_pretty": "",
            "abstract": cand.get("summary") or "",
        },
        "subject": subject,
        "summary": (cand.get("summary") or "").strip() or
                   "This document is an Office action issued by the United States Patent and "
                   "Trademark Office in a related application.",
        "rows": rows,
        "pdf_url": cand.get("pdf") or "",
        #  Not prior art and not claiming to be: it is a printed publication whose content is an
        #  examiner's findings. The compliance pass must not date-check it as a reference.
        "not_prior_art_document": True,
    }


def build(deep, pubs, subject, start_at=1, do_phrase=True, on_progress=None, report=None):
    """-> [document model] ready to render, one per requested publication.

    A `pub` prefixed `OA:` is a file-wrapper document, not a publication: it is built from the
    prosecution record on `report` and never looked up in the corpus.
    """
    claims = deep.get("claims") or []
    refs = {r.get("pub"): r for r in (deep.get("references") or [])}
    oas = {c["pub"]: c for c in office_action_candidates(report)}
    docs = []
    for i, pub in enumerate(pubs):
        if on_progress:
            #  Named, not just counted: "Reading US-11413727-B2" tells the user which document is
            #  costing the wait, which matters when one reference is slow to enrich.
            on_progress(i, "Reading %s (%d of %d)" % (pub, i + 1, len(pubs)))
        if str(pub).startswith(OA_PREFIX):
            cand = oas.get(pub)
            if cand:
                docs.append(office_action_doc(cand, subject, n=start_at + i))
            continue
        ref = refs.get(pub)
        if not ref:
            continue
        rows = rows_for_reference(ref, claims)
        if not rows:
            continue
        b = biblio(pub)
        b["issue_date_pretty"] = _pretty_date(b["publication_date"])
        b["priority_date_pretty"] = _pretty_date(b["priority_date"])
        doc = {
            "n": start_at + i,
            "pub": pub,
            #  Carried for the family collapse: two members of one DOCDB family are one
            #  disclosure, and a submission that cites both spends two document slots to say one
            #  thing.
            "family_id": ref.get("family"),
            "biblio": b,
            "subject": subject,
            "summary": "",
            "rows": rows,
        }
        if do_phrase:
            phrase(doc)
        if not doc["summary"]:
            t = (b.get("title") or "").strip()
            doc["summary"] = ("This document discloses %s%s." % (t[0].lower(), t[1:]) if t
                              else "This document is cited for the disclosure set out below.")
        docs.append(doc)
    return docs
