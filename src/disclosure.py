"""One source of disclosure wording for every surface that leaves the browser.

WHY THIS MODULE EXISTS
----------------------
Every accuracy safeguard added in the "fix(accuracy)" pass landed in templates/report.html only.
The web page told the reader that the element x reference grid is an unverified drafting aid, that
the corpus is bounded, that recall is measured at 19%, and that absence of results is not evidence
of absence. The artefacts that actually get filed, emailed to a client, or re-read three months
later -- the print view, the exported PDF and the exported DOCX -- carried none of it. They were
still headed "Prior-Art Search Report", still titled the grid "Element x reference claim chart",
and still shaded every cell green in proportion to its RETRIEVAL score, so the most confidently
retrieved false positive was the most visually emphatic thing on the page.

An exported document has to stand alone: assume the reader never saw the web page and cannot ask
what the colours mean. So all four surfaces now render from the strings below, and a change to the
disclosure can no longer reach one surface and miss another.

Everything numeric comes from corpus_facts.facts() -- the same live DB reading and the same
measured constants the web UI uses -- so the exports cannot quote a stale corpus size or a
superseded error rate.
"""
from __future__ import annotations

import corpus_facts

# The document is a retrieval + drafting artefact, not a search opinion. "Prior-Art Search Report"
# is the title a professional search firm puts on a work product they stand behind; this one is
# machine-generated and unverified, so it must not borrow that authority.
DOC_TITLE = "Prior-art retrieval report"
DOC_SUBTITLE = "Machine-generated drafting aid — not a prior-art search opinion"

CHART_TITLE = "Element × reference retrieval map"
CHART_TAG = "drafting aid — verify every cell"

# Verification state -> (mark, label, fill hex, is_coverage).
# Colour encodes VERIFICATION STATE, never retrieval score. Only a confirmed cell may look like
# coverage; everything else is deliberately drained of colour so the eye is not drawn to a cell
# merely because the retriever was confident about it.
CELL_STATES = {
    "discloses": ("✓", "verified — passage checked and it does teach the element", "c9e7d5", True),
    "weak":      ("~", "weak — same topic, does not clearly show the element", "fdf0d5", False),
    "unrelated": ("✕", "unrelated — cited passage is about something else", "f6d9d7", False),
    "no-coord":  ("○", "whole-document match — no specific passage exists to verify", "eceff3", False),
    "unchecked": ("?", "unchecked — verifier unavailable when this report was built", "eceff3", False),
}
CELL_ORDER = ["discloses", "weak", "unrelated", "no-coord", "unchecked"]

# Word forms for the EXPORTS. Two reasons not to reuse the symbols there:
#   1. reportlab's built-in Helvetica is WinAnsi-encoded and has no glyph for U+2713 / U+25CB, so
#      "✓" prints as a black box -- an unreadable mark is worse than no mark on a filed document.
#   2. A standalone document should not require the reader to find a legend to learn that a tick
#      means "verified". The word says it in the cell.
CELL_WORDS = {"discloses": "verified", "weak": "weak", "unrelated": "unrelated",
              "no-coord": "no passage", "unchecked": "unchecked"}


def cell_state(cell: dict):
    """(mark, label, fill_hex, is_coverage) for one chart cell. Unknown/missing verdict is treated
    as UNCHECKED, never as coverage -- an export built before the verifier ran must not silently
    render as though every cell had passed."""
    return CELL_STATES.get(cell.get("verify") or "unchecked", CELL_STATES["unchecked"])


def cell_word(cell: dict) -> str:
    """Export-safe verdict word for one chart cell."""
    return CELL_WORDS.get(cell.get("verify") or "unchecked", "unchecked")


def chart_warning(f=None) -> str:
    """The paragraph that must sit directly under the retrieval map on every surface."""
    f = f or corpus_facts.facts()
    s = ("This grid is NOT a verified claim chart and must not be filed or relied on as one. "
         "Cells come from semantic retrieval; each cited passage is then checked by a separate "
         "model pass asked to refute it. A cell is marked 'verified' only if that check "
         "confirmed the passage teaches the element — and that check is itself imperfect. ")
    if f.get("chart_fp_pre_pct") is not None:
        s += (f"In an audit of this feature {f['chart_fp_pre_pct']}% of {f['chart_fp_pre_n']} cells "
              f"asserting coverage were wrong before the verification pass existed. ")
    if f.get("chart_indep_checked"):
        s += (f"After it, an independent review re-read the {f['chart_indep_checked']} surviving "
              f"confirmed cells against the verbatim cited passages and judged "
              f"{f['chart_indep_overclaim']} of them ({f['chart_indep_pct']}%) to be overclaims. "
              f"The verification pass had reported {f.get('chart_fp_post_bad', 0)} of "
              f"{f.get('chart_fp_post_n', 0)} rejected, but that is the verifier grading its own "
              f"work. Sample sizes are small, and the verifier shares a model family with the "
              f"auditing judge, so treat the independent figure as a lower bound on the error "
              f"rate. ")
    s += "Open each cited reference and read the passage before using any cell."
    return s


def legend_lines(chart: dict):
    """[(mark, count, label)] for the verification legend, in a fixed order, omitting empty
    states so a clean report does not carry five zeroes."""
    vs = (chart or {}).get("verification") or {}
    out = []
    for k in CELL_ORDER:
        n = vs.get(k) or 0
        if n:
            _mark, label, _fill, _cov = CELL_STATES[k]
            #  The label already opens with the verdict word ("verified — passage checked ..."),
            #  and the count line prints that word itself, so keep only the explanatory half:
            #  otherwise the legend reads "3 verified — verified — passage checked ...".
            gloss = label.split("—", 1)[1].strip() if "—" in label else label
            out.append((CELL_WORDS[k], n, gloss))
    return out


def verification_summary(chart: dict) -> str:
    vs = (chart or {}).get("verification") or {}
    if not vs.get("covered"):
        return ""
    rate = vs.get("confirmed_rate")
    if rate is None:
        return (f"{vs.get('covered', 0)} cells are filled in this report; none could be "
                f"individually verified.")
    s = (f"Of {vs.get('checked', 0)} verifiable cells in this report, "
         f"{round(rate * 100)}% survived verification.")
    if rate < 0.8:
        s += " The rest did not — treat the grid as a list of passages to read, not as a finding."
    return s


#  The major patent-issuing offices a reader would expect a "worldwide" search to have covered.
#  Whichever of these the corpus does NOT hold get named explicitly, because a reader discounts a
#  named gap and glosses over an unnamed one.
_MAJOR_OFFICES = ["US", "EP", "WO", "DE", "CN", "JP", "KR", "GB", "FR", "CA", "AU", "IN", "RU", "BR"]


def _juris_sentence(f) -> str:
    """One sentence stating which offices are in and, by name, which major ones are not.

    Built from the live corpus (corpus_facts reads distinct countries from the table) rather than
    from a constant. The previous wording was the literal string "US, EP, WO, DE only — no JP, CN,
    KR, GB, FR or other national collections", which silently became false the moment the corpus
    was widened past those four.
    """
    have = [c for c in (f.get("jurisdictions") or []) if c]
    if not have:
        return "The indexed jurisdictions could not be read from the database; treat coverage as unknown."
    missing = [c for c in _MAJOR_OFFICES if c not in have]
    s = ", ".join(have) + " only."
    if missing:
        s += (f" No {', '.join(missing)} or other national collections — a search here cannot "
              f"see art published only in those offices.")
    else:
        s += (" Coverage spans the major offices, but national collections outside them are still "
              "absent.")
    trace = [c for c in (f.get("jurisdictions_trace") or []) if c]
    if trace:
        s += (f" ({len(trace)} further offices appear in trace amounts via family and citation "
              f"expansion and are not indexed coverage.)")
    return s


def scope_paragraphs(f=None):
    """[(heading, body)] — the full scope + measured-reliability disclosure, in the order it
    should be printed. Mirrors templates/_scope.html; both read the same facts()."""
    f = f or corpus_facts.facts()
    pubs = f"{f['publications']:,}" if f.get("publications") else "an unpublished number of"
    juris = ", ".join(f.get("jurisdictions") or [])
    date_s = f.get("max_date_str")
    out = [
        ("What this document is",
         "This is the output of a BOUNDED TECHNICAL-FIELD search tool, not a clearance search and "
         "not a professional prior-art search opinion. It searched one indexed field, to a fixed "
         "date, with measured recall well below completeness. Every conclusion below is a "
         "machine-generated drafting aid that must be checked against the cited documents before "
         "it is relied on."),
        ("Corpus",
         f"{pubs} publications, limited to {f.get('cpc_count')} CPC classes covering "
         f"{f.get('field_summary')}. {_juris_sentence(f)} No non-patent literature."),
        ("Current to",
         (f"{date_s}. Nothing published after this date was searched. For a novelty or validity "
          f"question this is a live gap, and it widens every day until the next ingest."
          if date_s else
          "The corpus date ceiling could not be read from the database. Treat currency as unknown.")),
        ("Measured recall",
         f"{f.get('recall_pct')}% @100. On a {f.get('recall_queries')}-query internal benchmark, "
         f"{f.get('recall_pct')}% of known-relevant families appeared in the top 100 results — "
         f"roughly four in five known-relevant families were NOT surfaced. Basis: "
         f"{f.get('recall_basis')}."),
    ]
    ai = ("Rationales and claim charts in this document are machine-generated drafting aids, not "
          "findings. ")
    if f.get("rationale_error_pct") is not None:
        ai += (f"Rationales overclaimed in {f['rationale_error_pct']}% of {f['rationale_n']} "
               f"audited cases. ")
    if f.get("chart_indep_checked"):
        ai += (f"Of the {f['chart_indep_checked']} retrieval-map cells that passed verification "
               f"and were then independently re-read, {f['chart_indep_overclaim']} "
               f"({f['chart_indep_pct']}%) were judged overclaims. ")
    ai += "Assume residual error and check each citation against the reference text."
    out.append(("AI output accuracy — verify every cell", ai))
    out.append((
        "Absence of results is not evidence of absence of prior art",
        "A short result list means this corpus did not surface art — it does not mean none "
        "exists. Given the recall figure, the date ceiling, and the class and jurisdiction limits "
        "above, a negative result here cannot support a clearance, freedom-to-operate, or "
        "patentability opinion. Those require a professional search across full collections."))
    return out


def cpc_lines(f=None):
    """['<code> — <title>'] for the indexed-class list."""
    f = f or corpus_facts.facts()
    return [f"{c['code']} — {c['title']}" for c in (f.get("cpc") or [])]


def not_indexed(f=None) -> str:
    f = f or corpus_facts.facts()
    juris = ", ".join(f.get("jurisdictions") or [])
    return (f"Not indexed: anything outside the CPC classes listed above; jurisdictions other "
            f"than {juris}; non-patent literature, standards, product manuals and datasheets; and "
            f"anything published after {f.get('max_date_str') or 'the corpus ceiling'}.")


def one_line(f=None) -> str:
    """The compact scope line for the search page — same facts, no wall of prose."""
    f = f or corpus_facts.facts()
    bits = []
    if f.get("publications"):
        bits.append(f"{f['publications']:,} pubs")
    bits.append(f"{f.get('cpc_count')} CPC classes")
    if f.get("max_date_str"):
        bits.append(f"current to {f['max_date_str']}")
    if f.get("recall_pct") is not None:
        bits.append(f"measured recall {f['recall_pct']}% @100")
    return "Bounded corpus: " + " · ".join(bits)
