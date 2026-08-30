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

import citation                            # the one grammar for a pinpoint, writer and checker

CITE_MAX = int(os.environ.get("CONCISE_CITES_PER_ROW", "4"))
#  A row is worth filing when the reference actually says something about the claim. "absent" and
#  "uncertain" are not findings, and an unverified cell has no passage behind it.
_USABLE_VERDICTS = ("disclosed", "partial")


# --------------------------------------------------------------------------- biblio


def _stage_tier(key, default):
    """Which tier this stage asks for. Settings page first, then the code default."""
    try:
        import model_settings
        return model_settings.tier_for(key, default)
    except Exception:
        return default


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


def _is_latin(name):
    """True when every letter in `name` can be typeset by the filing font.

    The PDF is Times-Roman, which has no CJK glyphs: a kanji name came out as a row of solid black
    boxes on a paper filed at the USPTO. U+2E80 is the start of the CJK radicals block, above every
    Latin, accented Latin and punctuation codepoint we care about.
    """
    return all(ord(c) < 0x2E80 for c in str(name or ""))


def _first_inventor(disp):
    """The first named inventor, in a script a US filing can carry.

    THE ROMANISED NAME IS ALREADY IN THE RECORD and was being passed over. For JP-2019155534-A the
    enrichment holds `["勇星 木村", "Yusei Kimura", "勇星 木村", ...]` and this returned the first
    entry, so the filed PDF showed black boxes where the inventor's name belongs. Nothing is
    transliterated here: a kanji reading is ambiguous and inventing one puts a name on a filing
    that nobody verified. We only PREFER a Latin form the source already supplied, and fall back to
    the original when there is none, which the renderer then typesets in a font that can draw it.
    """
    inv = disp.get("inventors") or []
    if isinstance(inv, str):
        inv = [inv]
    names = []
    for i in inv:
        name = (i.get("name") if isinstance(i, dict) else str(i or "")).strip()
        if name and name not in names:
            names.append(name)
    for name in names:
        if _is_latin(name):
            return name
    return names[0] if names else ""


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


def corpus_dates(pub):
    """The three dates from the publications table, or {}. THE FILING DECISION USES THESE.

    `_display` reads an enrichment cache built for the COVER BLOCK: a title, an inventor, a date to
    print. It is not the date engine's source and it is not always right. Measured 2026-08-20 on
    US 2022/0331993 A1, offered against a target whose effective filing date is 2021-08-02:

        cache   publication_date 2022-04-20   priority ''   filing ''
        corpus  publication_date 2022-10-20   priority 2021-04-20   filing 2022-04-20

    The cache had put the FILING date in the publication field and dropped the priority date
    entirely, so `classify_basis` saw no priority at all, called the document NOT_PRIOR_ART and
    the compliance pass refused to file it. It is 102(a)(2) art: earlier-filed, later-published.

    `subject_facts` already reads the target's effective filing date from this table for exactly
    this reason. The reference's dates decide the same question and come from the same place, so
    the search and the filing cannot disagree about what is prior art.
    """
    keys = _corpus_keys(pub)
    if not keys:
        return {}
    try:
        import db
        with db.cursor() as cur:
            cur.execute(
                "SELECT publication_date, earliest_priority_date, filing_date, country "
                "FROM publications WHERE replace(upper(publication_number),'-','') = ANY(%s) "
                "LIMIT 1", (sorted(keys),))
            row = cur.fetchone()
    except Exception:
        traceback.print_exc()
        return {}
    if not row:
        return {}
    return {"publication_date": str(row["publication_date"] or "") or "",
            "priority_date": str(row["earliest_priority_date"] or "") or "",
            "filing_date": str(row["filing_date"] or "") or "",
            "country": (row["country"] or "").upper()}


def _corpus_keys(pub):
    """Every spelling of `pub` this corpus might have stored, via the shared normaliser."""
    bare = _bare(pub)
    if not bare:
        return set()
    keys = {bare}
    try:
        import pubnorm
        keys |= {re.sub(r"[^A-Z0-9]", "", c.upper()) for c in pubnorm.mongo_candidates(pub)}
    except Exception:
        pass
    return {k for k in keys if k}


def biblio(pub):
    disp = _display(pub)
    label, kind = _us_style(pub)
    #  THE CORPUS WINS ON DATES. See corpus_dates: these three fields decide whether the document
    #  may be filed at all, and the display cache is not the date engine's source.
    dates = corpus_dates(pub)
    return {
        "pub": pub,
        "label": label,
        "kind": kind,
        "title": (disp.get("title") or "").strip(),
        #  The issuing office, because the date alone does not decide availability: 102(a)(2)
        #  reaches only US and PCT publications. See submission_compliance.qualify.
        "country": (dates.get("country") or (disp.get("country") or "").strip().upper()
                    or _bare(pub)[:2]),
        "inventor": _first_inventor(disp),
        "assignee": ", ".join([a for a in (disp.get("assignees") or []) if a][:2]),
        "publication_date": (dates.get("publication_date")
                             or (disp.get("publication_date") or "").strip()),
        "priority_date": (dates.get("priority_date")
                          or (disp.get("priority_date") or "").strip()),
        "filing_date": (dates.get("filing_date") or (disp.get("filing_date") or "").strip()),
        #  Recorded so a reader can see WHICH source dated the document that was, or was not, filed.
        "dates_source": "corpus" if dates else "enrichment cache",
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

    The grammar lives in `citation` because the compliance pass has to read back exactly what this
    wrote: it verifies the quotation appears AT this place, and a location it cannot parse is a
    location it cannot check. A place that does not resolve to a claim, a paragraph, a figure or
    the abstract returns "" rather than a fragment of prose an examiner cannot turn to.
    """
    return citation.render(citation.of_cell(cell))


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
                rows.append(_row(n, m.get("text") or "", cell, quote_claim=True, label=l,
                                 pub=ref.get("pub")))
        else:
            best = max(labels, key=lambda l: (by_label[l].get("bar") == "discloses",
                                              float(by_label[l].get("confidence") or 0)))
            m = meta.get(best) or {}
            merged = dict(by_label[best])
            merged["_extra_cites"] = [_cite(by_label[l]) for l in labels if l != best]
            rows.append(_row(n, m.get("text") or "", merged, quote_claim=False, label=best,
                             pub=ref.get("pub")))
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


def _row(claim_no, claim_text, cell, quote_claim, label, pub=""):
    cites = _dedupe_keep_order([_cite(cell)] + list(cell.get("_extra_cites") or []))[:CITE_MAX]
    strong = (cell.get("bar") or "") == "discloses"
    return {
        "claim_no": claim_no,
        "label": label,
        #  THE PUBLICATION THIS ROW'S CITATIONS BELONG TO, kind code and all. A1 and B4 of one
        #  application number paragraphs differently and do not have the same claims, so a row that
        #  cannot say which publication its pinpoint came from cannot be checked, and one that is
        #  carried onto a sibling resolves to nothing. See citation.same_publication.
        "cite_pub": pub or "",
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
    "- INFER NOTHING. Every sentence must be a restatement of something the supplied passage "
    "actually says. If the passage says a member is annular and generally cylindrical, do NOT "
    "write that it therefore extends along a longitudinal axis: that is you supplying a claim "
    "limitation. Do not write that the reference 'constitutes', 'amounts to', 'may be considered' "
    "or 'corresponds to the claimed' anything. Do not use 'implying', 'suggesting', 'indicating', "
    "'appears to', 'effectively' or 'thereby' to carry a fact the passage does not contain. "
    "Argument is the one defect that gets a submission discarded rather than corrected, and it "
    "does not need a single word from the patentability vocabulary to be argument.\n"
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


def phrase(doc, tier=None, model=None):
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
        #  `model`, when a person chose one in the rebuild dialog, pins this call to it
        #  instead of letting the strong tier pick. Unset is the default behaviour.
        got = llm.chat_json(_SYS, json.dumps(payload, ensure_ascii=False),
                            max_tokens=8000, provider=model,
                            tier=tier or _stage_tier("concise_description", "strong")) or {}
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


#  How many never-read references the picker still shows, so a grid-topping one is never silently
#  gone. Enough to explain the ranking, few enough not to crowd out documents that can be filed.
UNREADABLE_SHOWN = 5


def sole_reach_notes(deep):
    """Limitations exactly one reference in the whole search reaches, and what it actually says.

    NOT A FILING LIST. A reference here may have nothing chartable at all, and one of them is the
    applicant's own: neither belongs in a picker of documents to file. It belongs on the page
    because it is the single most valuable thing a search of 233 documents can report, and the
    picker had nowhere to say it.

    On adhoc-efbf2979420b this is one row. Claim 1[e] is "the contact surface angle ranges in size
    from 170° to 190°", exactly one reference of 233 is not absent on it, that reference is
    Schmalz's own DE 10 2024 105 114 A1, and what it actually teaches is 130° to 170°. The
    limitation, the document, the overlap and the fact that no passage was ever quoted are four
    separate things a practitioner needs before building a case on it, and all four are here.
    """
    labels = {c.get("label"): c for c in (deep or {}).get("claims") or [] if isinstance(c, dict)}
    reach = {}
    for ref in ((deep or {}).get("references") or []):
        for c in (ref.get("claims") or []):
            if not isinstance(c, dict) or not c.get("item"):
                continue
            if (c.get("verdict") or "") == "absent":
                continue
            reach.setdefault(c["item"], []).append((ref, c))
    out = []
    for label, hits in reach.items():
        if len(hits) != 1:
            continue
        ref, cell = hits[0]
        out.append({
            "limitation": label,
            "text": str((labels.get(label) or {}).get("text") or "").strip(),
            "pub": ref.get("pub"), "title": ref.get("title") or "",
            "verdict": cell.get("verdict") or "",
            "grounding": cell.get("grounding") or "",
            #  A chart row needs both. Without them the document says something and cannot be
            #  filed saying it, which is exactly the case worth flagging.
            "chartable": ((cell.get("verdict") or "") in _USABLE_VERDICTS
                          and (cell.get("grounding") or "") == "verified"),
            "note": " ".join(str(cell.get("note") or "").split())[:400],
        })
    return sorted(out, key=lambda d: (_claim_no(d["limitation"]), d["limitation"]))


def unreached_limitations(deep, report=None):
    """Limitations NO reference in the search reaches at all. -> [{"limitation", "text", ...}]

    The other half of the same answer, and the one that decides whether a claim survives. Said
    plainly rather than left to be inferred from an empty column.

    AND NEVER SAID FLAT WHEN THE WORDS WERE THE ONLY THING SEARCHED. Counsel, 2026-08-26: claim
    1[e], "the contact surface angle ranges in size from 170° to 190°", was reported reached by 0
    of 232 references. 170 to 190 degrees means "parallel to the direction the magnet travels",
    which GB 874,600 claims outright and which this search had already selected as Document 6. The
    sentence was true of the vocabulary and false of the art. So each row carries its construction
    and, where the construction was not itself searched, the caveat that goes beside it.
    """
    import claim_construction
    labels = [c for c in (deep or {}).get("claims") or [] if isinstance(c, dict)]
    touched = set()
    for ref in ((deep or {}).get("references") or []):
        for c in (ref.get("claims") or []):
            if isinstance(c, dict) and c.get("item") and (c.get("verdict") or "") != "absent":
                touched.add(c["item"])
    stored = ((report or {}).get("claim_construction") or {})
    out = []
    for c in labels:
        if c.get("label") in touched:
            continue
        text = str(c.get("text") or "").strip()
        #  The run's own construction when the report carries one (it also holds the applicant's
        #  definitions and whether the concept reached the portfolio); the geometry alone when it
        #  does not, so an older report is gated too rather than trusted.
        con = stored.get(c.get("label")) or claim_construction.construe(text)
        out.append({"limitation": c.get("label"), "text": text,
                    "construction": con,
                    "confirmed": claim_construction.zero_is_confirmable(con),
                    "caveat": claim_construction.zero_caveat(con)})
    return out


def _by_marginal_coverage(ranked):
    """Re-order so each document is the one that adds most to what the ones above it already cover.

    Breadth alone is the wrong greedy. Twelve documents that each read on the same twelve popular
    limitations cover twelve limitations between them; eleven of those plus one that reaches a
    thirteenth cover thirteen. Counsel, 2026-08-24, on a Schunk family the package omitted: "it is
    the only third-party document in the entire search that reads on claim 16's groove-shaped
    receptacle". A ranking that cannot see that will pass it over every time.

    So this is set cover, greedily: repeatedly take the document contributing the most limitations
    nothing above it contributes, breaking ties on the order it was handed. Once nothing new is
    left to add the remainder keeps that order, because at that point breadth is all there is.
    Only used when nothing anticipates: an anticipation is decisive on its own and does not want
    reordering by how much company it keeps.
    """
    remaining = list(ranked)
    seen, out = set(), []
    while remaining:
        #  NO EARLY EXIT. "The head contributes all of its own coverage" does not mean nothing
        #  beats it: a head adding one new limitation loses to a document adding three. An exit on
        #  that condition left the order almost untouched, and it hid the case this pass exists
        #  for. Schmalz's own DE 10 2024 105 114 A1 charts ONE limitation and is the only document
        #  in the search that reaches it, the 170 to 190 degree contact surface angle of claim 1.
        #  It ranked 142nd. The loop is quadratic and the list is a few hundred; that is nothing.
        best_i, best_new = 0, -1
        for i, d in enumerate(remaining):
            new = len(set(d.get("covers") or []) - seen)
            if new > best_new:
                best_i, best_new = i, new
        d = remaining.pop(best_i)
        d["new_limitations"] = len(set(d.get("covers") or []) - seen)
        seen |= set(d.get("covers") or [])
        out.append(d)
    return out


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

    AND A FALLBACK, because that order has a hole. Counsel, 2026-08-24: on a target where nothing
    anticipates and the Office has applied nothing, the first two keys are ties for every
    candidate, and the ranking falls through to weaker tie-breakers before construed-limitation
    coverage gets a vote at all. A Schunk family reading on 16 of 32 limitations, ahead of nine of
    the thirteen selected, dropped out of the package entirely. Coverage IS the signal in a
    103-only case, it is already computed, and it now leads the order whenever nothing in the set
    anticipates anything.
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
    #  WHAT THE SEARCH COULD NOT READ. A reference the corpus holds only a title and an abstract
    #  for was screened as worth reading and then never read, so any description of it rests on
    #  the abstract alone. Counsel, 2026-08-24: US 8,991,263 is a fibre-testing snubbing clamp and
    #  its mapping onto "pole shoes guide a magnetic field portion" is a reach. Unreadable is a
    #  hard exclusion from a filing set, whatever it scores.
    unread = {r.get("pub") for r in
              (((report or {}).get("deep_rank") or {}).get("not_readable") or [])
              if isinstance(r, dict) and r.get("pub")}
    #  The number the claim grid shows, and the one a practitioner reconciles against: how many
    #  construed limitations this reference reads on. `n_rows` is not it, because dependent claims
    #  collapse to one row each.
    n_limitations_of = {}
    #  WHICH LIMITATIONS ONLY ONE REFERENCE IN THE WHOLE SEARCH REACHES. This is the single most
    #  valuable thing a search can say and it had no representation anywhere: on
    #  adhoc-efbf2979420b exactly one document of 233 is not absent on claim 1[e], the 170 to 190
    #  degree contact surface angle, and it ranked 142nd because the one cell it has is unquoted.
    #  A document like that may be unfilable here and decisive somewhere else, and either way the
    #  practitioner has to be told it exists.
    reach = {}
    for ref in (deep.get("references") or []):
        for c in (ref.get("claims") or []):
            if isinstance(c, dict) and c.get("item") and (c.get("verdict") or "") != "absent":
                reach.setdefault(c["item"], set()).add(ref.get("pub"))
    sole = {next(iter(pubs)): lab for lab, pubs in reach.items() if len(pubs) == 1}
    sole_of = {}
    for pub, lab in sole.items():
        sole_of.setdefault(pub, []).append(lab)

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
        cells = [c for c in (ref.get("claims") or []) if isinstance(c, dict) and c.get("item")]
        #  TWO COVERAGE NUMBERS, and the difference between them is a thing to say out loud.
        #  `reads_on` is what the claim grid on the report page shows: every limitation this
        #  reference is not simply absent from. `n_limitations` is what could actually be FILED:
        #  a chart row needs a usable verdict AND a passage that was found in the document. A
        #  reference can read on sixteen limitations and support nine, and a practitioner
        #  reconciling the package against the grid deserves to be told which number is which.
        covered = {c.get("item") for c in cells
                   if (c.get("verdict") or "") in _USABLE_VERDICTS
                   and (c.get("grounding") or "") == "verified"}
        n_limitations = len(covered)
        n_limitations_of[pub] = n_limitations
        out.append({
            "pub": pub, "title": ref.get("title") or "",
            "family": ref.get("family"),
            "n_rows": len(rows), "n_strong": sum(1 for r in rows if r["strong"]),
            "n_limitations": n_limitations,
            "reads_on": len({c.get("item") for c in cells
                             if (c.get("verdict") or "") != "absent"}),
            "covers": sorted(covered, key=lambda x: (_claim_no(x), str(x))),
            "sole_reach": sorted(sole_of.get(pub) or [], key=lambda x: (_claim_no(x), str(x))),
            "claims": sorted({r["claim_no"] for r in rows}),
            "anticipates": sorted(w.get("anticipates") or [], key=_claim_no),
            "adds": sorted(w.get("adds") or [], key=_claim_no),
            "strong_indep": strong_indep,
            #  Authority, not similarity: an examiner used this document against this family.
            "office": ("applied" if pub in applied else
                       "considered" if pub in considered else ""),
            "readable": pub not in unread,
            "rank": ref.get("rank") or 9999,
        })
    #  ANTICIPATION DECIDES WHEN THERE IS ANY. When there is none, the first two keys are ties for
    #  everything and coverage has to lead or it never votes at all.
    if any(d["anticipates"] for d in out):
        out.sort(key=lambda d: (-len(d["anticipates"]), d["office"] != "applied",
                                -len(d["adds"]), -d["strong_indep"], d["office"] != "considered",
                                -d["n_strong"], -d["n_rows"], d["rank"]))
        for d in out:
            d["new_limitations"] = d["n_limitations"]
    else:
        #  UNREADABLE GOES LAST, AND GOES LAST FIRST. A reference the corpus holds only an
        #  abstract for scores HIGH on coverage, not low: a short text gets mapped generously
        #  onto many limitations and every cell verifies against the abstract it came from. On
        #  adhoc-efbf2979420b, US 6,332,502 was never read in full and came out top of the
        #  coverage order with 25 of 32. Left in, it also swallows the marginal-coverage pass and
        #  starves every readable document of anything new to add. So it is separated before the
        #  greedy runs, not filtered out of the picker: it is still shown, still choosable, and
        #  never chosen for you.
        readable = [d for d in out if d["readable"]]
        unreadable = [d for d in out if not d["readable"]]
        key = (lambda d: (d["office"] != "applied", -d["n_limitations"], -len(d["adds"]),
                          -d["strong_indep"], d["office"] != "considered",
                          -d["n_strong"], -d["n_rows"], d["rank"]))
        out = (_by_marginal_coverage(sorted(readable, key=key))
               + _by_marginal_coverage(sorted(unreadable, key=key)))
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
    head = kept[:limit]
    #  TWO KINDS OF DOCUMENT COME BACK PAST THE LIMIT, because both are things a practitioner has
    #  to be told exist and neither can earn its place on coverage.
    #
    #  The unreadable ones, because sorting them to the bottom also sorts them out of the picker
    #  and out of the "considered and not selected" table computed from it: a reference the claim
    #  grid ranks at the very top would then vanish with no explanation anywhere, which is the
    #  silent drop this whole change is about.
    #
    #  And the sole-reach ones, because "no other document in this search reaches that limitation"
    #  is the most valuable sentence a search produces and it does not correlate with rank at all.
    #  DE 10 2024 105 114 A1 is the only one of 233 not absent on claim 1[e] and ranked 142nd.
    if len(head) >= limit:
        shown = {d["pub"] for d in head}
        extra = sorted((d for d in kept if d["pub"] not in shown and not d["readable"]),
                       key=lambda d: -int(d.get("reads_on") or 0))[:UNREADABLE_SHOWN]
        shown |= {d["pub"] for d in extra}
        extra += [d for d in kept if d["pub"] not in shown and d.get("sole_reach")]
        head = head + extra
    return head


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
    analysis does the arguing that the submitter may not do. (The no-argument constraint is the
    concise-description requirement of 1.290(d)(2) as construed by MPEP 1134.01, NOT 1.290(b),
    which is the timing provision. This said (b) until 2026-08-27, and it said it on a paper
    written to be read by an examiner.)
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
            #  It states what the document SAYS, which 1.290(d)(2) asks for; it does not argue
            #  that the pending claims are unpatentable, which 1.290(d)(2) as construed by
            #  MPEP 1134.01 does not permit. (1.290(b) is the TIMING provision, and citing it for
            #  this was wrong on a paper an examiner reads.)
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


def build(deep, pubs, subject, start_at=1, do_phrase=True, on_progress=None,
          report=None, model=None):
    """-> [document model] ready to render, one per requested publication.

    A `pub` prefixed `OA:` is a file-wrapper document, not a publication: it is built from the
    prosecution record on `report` and never looked up in the corpus.

    THE DOCUMENTS ARE INDEPENDENT , one enrichment fetch and one model call each , and they
    used to build one after another, which priced a ten-document package at the sum of its
    parts. They build concurrently now; numbering still follows the caller's order, because the
    document number is assigned from the input position, never from completion order.
    """
    claims = deep.get("claims") or []
    refs = {r.get("pub"): r for r in (deep.get("references") or [])}
    oas = {c["pub"]: c for c in office_action_candidates(report)}

    def one(i, pub):
        if str(pub).startswith(OA_PREFIX):
            cand = oas.get(pub)
            return office_action_doc(cand, subject, n=start_at + i) if cand else None
        ref = refs.get(pub)
        if not ref:
            return None
        rows = rows_for_reference(ref, claims)
        if not rows:
            return None
        b = biblio(pub)
        b["issue_date_pretty"] = _pretty_date(b["publication_date"])
        b["priority_date_pretty"] = _pretty_date(b["priority_date"])
        doc = {
            "n": start_at + i,
            "pub": pub,
            #  Carried for the family collapse: two members of one DOCDB family are one
            #  disclosure, and a submission that cites both spends two document slots to say
            #  one thing.
            "family_id": ref.get("family"),
            "biblio": b,
            "subject": subject,
            "summary": "",
            "rows": rows,
        }
        if do_phrase:
            phrase(doc, model=model)
        if not doc["summary"]:
            t = (b.get("title") or "").strip()
            doc["summary"] = ("This document discloses %s%s." % (t[0].lower(), t[1:]) if t
                              else "This document is cited for the disclosure set out below.")
        return doc

    try:
        workers = int(os.environ.get("CONCISE_BUILD_WORKERS", "4"))
    except (TypeError, ValueError):
        workers = 4
    workers = max(1, min(workers, 8, len(pubs) or 1))
    results = [None] * len(pubs)
    if workers == 1:
        for i, pub in enumerate(pubs):
            if on_progress:
                on_progress(i, "Reading %s (%d of %d)" % (pub, i + 1, len(pubs)))
            results[i] = one(i, pub)
    else:
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        finished = 0
        lock = threading.Lock()
        if on_progress:
            #  Say something the moment the pool starts: reporting only completions left the bar
            #  on "Starting" until the first document landed, up to a minute of apparent hang.
            on_progress(0, "Reading %d references, up to %d at a time" % (len(pubs), workers))

        def tracked(i, pub):
            if on_progress:
                with lock:
                    on_progress(finished, "Reading %s" % pub)
            return one(i, pub)

        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="concise-build") as ex:
            futures = {ex.submit(tracked, i, pub): i for i, pub in enumerate(pubs)}
            for future in as_completed(futures):
                i = futures[future]
                results[i] = future.result()
                with lock:
                    finished += 1
                    if on_progress:
                        #  Named, not just counted: which document is costing the wait matters
                        #  when one reference is slow to enrich.
                        on_progress(finished,
                                    "Read %s (%d of %d)" % (pubs[i], finished, len(pubs)))
    return [d for d in results if d]
