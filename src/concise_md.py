"""The editable form of a concise description, and the way back.

A generated paper is a draft. The practitioner has to be able to change a sentence, delete a row
that overreaches, or fix a citation, and then get the SAME PDF they reviewed — so markdown is a
real round trip here, not a pretty export. `to_markdown` and `from_markdown` are inverses over
everything the renderer uses, and a test holds them to that.

The grammar is deliberately rigid, because the alternative to rigid is an edit that silently drops
a row:

    # CONCISE DESCRIPTION ...            (fixed heading, ignored on parse)
    **Application:** <subject line>
    **Document N:** <reference label>
    - First Named Inventor: ...          (biblio, one per line, `- Key: value`)
    ## Summary
    <one paragraph>
    ## Claim chart
    ### Claim 1: "verbatim limitation"   (a quoted title means the claim text is quoted verbatim)
    ### Claim 2 (paraphrase)             (parenthesised means a dependent-claim paraphrase)
    <disclosure prose, one or more lines>
    > quoted passage                     (optional, blockquote)
    - Paragraph [0012]                   (citations, one per line)
    ## Filing notes
    - **Label.** text                    (advisory, not part of the filing)

A parse that cannot find the claim chart raises rather than returning an empty document: losing
every row because a heading was renamed is the failure this format exists to prevent.
"""
from __future__ import annotations

import re

H_MAIN = "# CONCISE DESCRIPTION OF RELEVANCE: THIRD-PARTY SUBMISSION UNDER 37 CFR § 1.290"


class MarkdownShapeError(ValueError):
    """The edited file no longer matches the grammar; nothing is overwritten."""


def to_markdown(doc):
    import concise_render
    b = doc["biblio"]
    out = [H_MAIN, "",
           "**Application:** %s" % concise_render.subject_line(doc["subject"]), "",
           "**Document %s:** %s" % (doc["n"], b["label"]), ""]
    for label, val in (("First Named Inventor", b.get("inventor")),
                       ("Assignee", b.get("assignee")),
                       ("Issue Date" if b.get("kind") == "patent" else "Publication Date",
                        b.get("issue_date_pretty")),
                       ("Title", b.get("title")),
                       ("Earliest Priority Date", b.get("priority_date_pretty"))):
        if val:
            out.append("- %s: %s" % (label, val))
    out += ["", "## Summary", "", doc.get("summary", ""), "", "## Claim chart", ""]
    for r in doc["rows"]:
        if r.get("quote_claim") and r.get("claim_text"):
            out.append('### Claim %s: "%s"' % (r["claim_no"], r["claim_text"]))
        else:
            out.append("### Claim %s (%s)" % (r["claim_no"],
                                              r.get("claim_paraphrase") or r.get("claim_text", "")))
        out += ["", (r.get("disclosure") or r.get("note") or "").strip(), ""]
        if r.get("quote"):
            out += ["> %s" % " ".join(str(r["quote"]).split()), ""]
        for c in (r.get("cites") or []):
            out.append("- %s" % c)
        out.append("")
    notes = _filing_notes(doc)
    if notes:
        out += ["## Filing notes", ""]
        for label, text in notes:
            out.append("- **%s.** %s" % (label, text))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _filing_notes(doc):
    try:
        import concise_render
        return concise_render.filing_notes(doc)
    except Exception:
        return []


_H3 = re.compile(r'^###\s+Claim\s+(\d+)\s*(?::\s*"(.*)"|\((.*)\))\s*$', re.S)


def from_markdown(md, base):
    """Rebuild a document model from edited markdown, using `base` for anything not represented.

    `base` is the model the markdown was generated from: it carries the compliance record, the
    family id and the strong/weak bar, none of which the practitioner edits and none of which
    should be lost by editing prose.
    """
    text = str(md or "").replace("\r\n", "\n")
    if "## Claim chart" not in text:
        raise MarkdownShapeError(
            "No '## Claim chart' heading found. The claim rows are read from under that heading, "
            "so saving this would discard every row. Restore the heading and try again.")
    doc = dict(base)
    doc["rows"] = []

    m = re.search(r"^\*\*Document\s+(\d+):\*\*\s*(.+?)\s*$", text, re.M)
    if m:
        doc["n"] = int(m.group(1))
        doc["biblio"] = dict(base.get("biblio") or {}, label=m.group(2).strip())

    biblio_map = {"First Named Inventor": "inventor", "Assignee": "assignee",
                  "Issue Date": "issue_date_pretty", "Publication Date": "issue_date_pretty",
                  "Title": "title", "Earliest Priority Date": "priority_date_pretty"}
    head = text.split("## Summary", 1)[0]
    for line in head.splitlines():
        mm = re.match(r"^-\s+([A-Za-z ]+):\s*(.+?)\s*$", line)
        if mm and mm.group(1).strip() in biblio_map:
            doc["biblio"][biblio_map[mm.group(1).strip()]] = mm.group(2).strip()

    ms = re.search(r"## Summary\s*\n+(.*?)\n+##", text, re.S)
    if ms:
        doc["summary"] = " ".join(ms.group(1).split())

    chart = text.split("## Claim chart", 1)[1]
    chart = chart.split("\n## Filing notes", 1)[0]
    blocks = re.split(r"\n(?=###\s+Claim\s)", chart)
    by_claim_seen = {}
    old_rows = list(base.get("rows") or [])
    for blk in blocks:
        blk = blk.strip("\n")
        if not blk.startswith("###"):
            continue
        first, _, body = blk.partition("\n")
        h = _H3.match(first.strip())
        if not h:
            raise MarkdownShapeError(
                "Could not read this claim heading: %r. Use '### Claim 1: \"text\"' for a quoted "
                "limitation or '### Claim 2 (text)' for a paraphrase." % first.strip()[:80])
        claim_no = int(h.group(1))
        quoted, para = h.group(2), h.group(3)
        prose, quote, cites = [], "", []
        for line in body.splitlines():
            t = line.strip()
            if not t:
                continue
            if t.startswith("> "):
                quote = t[2:].strip()
            elif t.startswith("- "):
                cites.append(t[2:].strip())
            else:
                prose.append(t)
        #  Keep the invisible fields from the row this one came from: which bar it sits on and
        #  whether its quotation survived verification are findings, not prose.
        idx = by_claim_seen.get(claim_no, 0)
        by_claim_seen[claim_no] = idx + 1
        same = [r for r in old_rows if r.get("claim_no") == claim_no]
        src = same[idx] if idx < len(same) else (same[0] if same else {})
        doc["rows"].append({
            "claim_no": claim_no,
            "label": src.get("label", "claim %d" % claim_no),
            "quote_claim": quoted is not None,
            "claim_text": (quoted if quoted is not None else src.get("claim_text", "")),
            "claim_paraphrase": (para if para is not None else src.get("claim_paraphrase", "")),
            "verdict": src.get("verdict"),
            "bar": src.get("bar", ""),
            "strong": bool(src.get("strong")) and bool(quote),
            "quote": quote,
            "quote_original": src.get("quote_original"),
            "quote_translated": src.get("quote_translated"),
            "note": src.get("note", ""),
            "disclosure": " ".join(" ".join(prose).split()),
            "cites": cites,
            #  WHICH PUBLICATION THIS ROW'S PINPOINTS BELONG TO survives a hand edit, because a
            #  citation is (publication including kind code, location, text) and an edited row that
            #  cannot say which publication is a row nothing can check. See citation.py.
            "cite_pub": src.get("cite_pub", ""),
            "confidence": src.get("confidence", 0),
        })
    if not doc["rows"]:
        raise MarkdownShapeError(
            "The claim chart has no '### Claim N' rows. Saving this would produce an empty "
            "submission, so nothing was written.")
    return doc
