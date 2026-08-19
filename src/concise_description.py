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


def candidates(report, deep, limit=40):
    """References worth offering, best first: most claims spoken to, strong bar first."""
    claims = deep.get("claims") or []
    out = []
    for ref in (deep.get("references") or []):
        rows = rows_for_reference(ref, claims)
        if not rows:
            continue
        strong = sum(1 for r in rows if r["strong"])
        out.append({"pub": ref.get("pub"), "title": ref.get("title") or "",
                    "n_rows": len(rows), "n_strong": strong,
                    "claims": sorted({r["claim_no"] for r in rows}),
                    "rank": ref.get("rank") or 9999})
    out.sort(key=lambda d: (-d["n_strong"], -d["n_rows"], d["rank"]))
    return out[:limit]


def build(deep, pubs, subject, start_at=1, do_phrase=True):
    """-> [document model] ready to render, one per requested publication."""
    claims = deep.get("claims") or []
    refs = {r.get("pub"): r for r in (deep.get("references") or [])}
    docs = []
    for i, pub in enumerate(pubs):
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
