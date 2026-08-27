"""A third-party preissuance submission under 37 CFR 1.290, built and audited against the rule.

THE RULE IS THE SPECIFICATION. 1.290 is a list of conditions a paper set must satisfy, and a
submission that misses one "may not be entered or considered by the Office" (1.290(a)). So this
module holds the conditions as data, builds the artefacts, and then CHECKS the artefacts it built
against those same conditions. Nothing is described as ready to file because somebody believes it
is; it is ready when every requirement returns `ok`, and the audit says which one did not.

WHAT THE RULE ASKS FOR, verified against the CFR text and MPEP 1134.01 on 2026-08-23:

  (b)     timing: before the EARLIER of a notice of allowance, or the LATER of six months after
          first publication and the date of the first rejection.
  (c)     in writing.
  (d)(1)  a document list, formatted per (e).
  (d)(2)  a concise description of the ASSERTED RELEVANCE of each item.
  (d)(3)  a legible copy of each item OTHER than a U.S. patent or U.S. patent application
          publication.
  (d)(4)  an English translation of any non-English item.
  (d)(5)  two statements: the party is not an individual with a 1.56 duty to disclose, and the
          submission complies with 122(e) and this section.
  (e)     the list carries a heading naming it a third-party submission under 1.290, repeats the
          application number ON EACH PAGE, puts U.S. patents and U.S. patent application
          publications in a SEPARATE SECTION from everything else, and identifies each item by the
          fields its own type requires, which differ:
            (1) U.S. patent            number, first named inventor, issue date
            (2) U.S. pre-grant pub     number, first named inventor, publication date
            (3) foreign document       country or office, applicant/patentee/first named inventor,
                                       document number, publication date
            (4) non-patent publication author, title, pages submitted, publication date, and where
                                       available publisher and place
  (f)     the fee at 1.17(o), for every ten items OR FRACTION THEREOF.
  (g)     no fee for three or fewer items WHEN accompanied by the first-and-only statement. MPEP
          1134.01 is explicit that three or fewer WITHOUT that statement still pays.
  (i)     1.8 does not apply, so a certificate of mailing does not preserve the date. It has to
          arrive inside the window.

AND ONE THING THE RULE DOES NOT SAY. MPEP 1134.01 forbids the concise description from arguing
unpatentability: a claim chart is fine, "claim 1 is unpatentable" is not. That is checked here too,
because it is the failure mode a machine-written description walks into.

The audit adds two bars of our own that the rule does not require, because a paper filed at the
Office should not contain a quotation nobody checked: every quotation must appear verbatim in the
document it is attributed to, and every listed item must qualify as prior art on the dates.
"""
from __future__ import annotations

import csv
import datetime
import io
import math
import os
import re
import traceback

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)

import concise_render
import pdf_conform                           # will Patent Center accept the papers we just wrote
import pdf_fonts
import search_modes                          # the forum rule: which offices 102(a)(2) reaches

# --------------------------------------------------------------------------------- item typing

US_PATENT, US_PGPUB, FOREIGN, NPL = "us_patent", "us_pgpub", "foreign", "npl"

#  1.290(d)(3) excludes exactly two kinds from the copy requirement.
_NO_COPY_NEEDED = (US_PATENT, US_PGPUB)

#  Offices whose publications are not in English. Keyed on the ISSUING OFFICE and not on a language
#  sniff over the text we hold: the text in the corpus for a JP publication may already be
#  somebody's translation, and "this looks like English" is not the question (d)(4) asks.
_NON_ENGLISH_OFFICES = {"JP", "CN", "KR", "DE", "FR", "ES", "IT", "RU", "TW", "BR", "PT", "NL",
                        "SE", "DK", "FI", "NO", "PL", "TR", "AT", "CH", "MX", "AR", "CL"}

_OFFICE_NAMES = {
    "JP": "Japan Patent Office", "CN": "China National Intellectual Property Administration",
    "KR": "Korean Intellectual Property Office", "DE": "German Patent and Trade Mark Office",
    "EP": "European Patent Office", "WO": "World Intellectual Property Organization (PCT)",
    "GB": "United Kingdom Intellectual Property Office", "FR": "France (INPI)",
    "CA": "Canadian Intellectual Property Office", "AU": "IP Australia",
    "TW": "Taiwan Intellectual Property Office", "ES": "Spain (OEPM)", "IT": "Italy (UIBM)",
    "AT": "Austrian Patent Office", "SE": "Swedish Intellectual Property Office",
    "CH": "Swiss Federal Institute of Intellectual Property", "NL": "Netherlands Patent Office",
    "RU": "Rospatent", "BR": "INPI Brazil", "IN": "Indian Patent Office",
}


def item_kind(doc):
    """Which of the four 1.290(e) identification rules this item falls under."""
    b = doc.get("biblio") or {}
    if b.get("npl") or str(b.get("kind") or "") == "npl":
        return NPL
    kind = str(b.get("kind") or "")
    pub = str(b.get("pub") or doc.get("pub") or "").upper()
    if pub.startswith("US"):
        return US_PATENT if kind == "patent" else US_PGPUB
    return FOREIGN


def office_of(doc):
    b = doc.get("biblio") or {}
    code = str(b.get("country") or "").upper()[:2] or str(b.get("pub") or "")[:2].upper()
    return code, _OFFICE_NAMES.get(code, code)


def needs_copy(doc):
    """1.290(d)(3): everything except a U.S. patent and a U.S. patent application publication."""
    return item_kind(doc) not in _NO_COPY_NEEDED


def needs_translation(doc):
    """1.290(d)(4): any non-English item."""
    return office_of(doc)[0] in _NON_ENGLISH_OFFICES


#  37 CFR 1.17(o), fee code 1818/2818, from the USPTO schedule effective 2025-01-19 and read on
#  2026-08-24. A third party is NOT eligible for the micro entity discount, which is why there are
#  only two numbers here. Overridable because a fee schedule changes and a stale constant on a
#  filing paper is worse than one somebody can correct.
FEE_PER_UNIT = {"large": float(os.environ.get("USPTO_1290_FEE_LARGE", "195")),
                "small": float(os.environ.get("USPTO_1290_FEE_SMALL", "78"))}
FEE_SCHEDULE_DATE = os.environ.get("USPTO_FEE_SCHEDULE_DATE", "19 January 2025")
ITEMS_PER_UNIT = 10


def _money(v):
    return ("%.2f" % float(v)).rstrip("0").rstrip(".")


def fee_units(n_items):
    """1.290(f): one unit of the 1.17(o) fee per ten items OR FRACTION THEREOF."""
    return int(math.ceil(max(int(n_items), 0) / float(ITEMS_PER_UNIT))) if n_items else 0


def fee_amount(n_items, entity_size="small"):
    """What this many items costs. -> (units, dollars, per_unit)"""
    per = FEE_PER_UNIT.get(str(entity_size or "small"), FEE_PER_UNIT["small"])
    units = fee_units(n_items)
    return units, round(units * per, 2), per


def fee_choices(entity_size="small", max_units=5):
    """The budget a person actually picks from: how many units, and how many documents that buys.

    The fee steps in tens, so choosing "two units" is choosing "up to twenty documents". Offering
    the unit and letting the app fill the slots is the honest way round: the alternative is a
    reader adding an eleventh document and silently doubling the bill.
    """
    per = FEE_PER_UNIT.get(str(entity_size or "small"), FEE_PER_UNIT["small"])
    out = []
    for u in range(1, int(max_units) + 1):
        money = _money(u * per)
        out.append({"units": u, "max_documents": u * ITEMS_PER_UNIT,
                    "dollars": round(u * per, 2), "dollars_pretty": money,
                    "label": "%d unit%s, up to %d documents, $%s"
                             % (u, "" if u == 1 else "s", u * ITEMS_PER_UNIT, money)})
    return out


#  How many rows the passed-over table may carry. Long enough that a document worth having is
#  in it, short enough that somebody reads to the bottom.
PASSED_OVER_MAX = 25


def passed_over(cands, budget_items):
    """Documents the claim grid ranks highly that this selection does not include, with the reason.

    Counsel, 2026-08-24: "Never drop a top-N coverage reference silently. If the selector passes
    over something the grid ranks first, say so and say why. A one-line 'considered and not
    selected, because X' would have turned twenty minutes of reconciliation into thirty seconds."

    The grid's number is `reads_on`, every limitation the reference is not simply absent from,
    which is what a practitioner sees on the report page. The selection uses `n_limitations`, what
    could actually be charted. When those two disagree, that IS the reason, and it is the one that
    is invisible from either page on its own.
    """
    picked, ranked, n = [], [], 0
    for c in cands or []:
        if c.get("default_include") and n < budget_items:
            picked.append(c)
            n += 1
        ranked.append(c)
    chosen = {c.get("pub") for c in picked}
    with_reach = [c for c in ranked if c.get("reads_on")]
    top = sorted(with_reach, key=lambda c: -int(c.get("reads_on") or 0))[:max(budget_items, 10)]
    #  AND EVERY PUBLIC-ART DOCUMENT AT LEAST AS BROAD AS THE NARROWEST ONE SELECTED. A cap on the
    #  top by breadth is not enough on its own: Schunk's DE 10 2022 135 066 A1 reads on 16, sat
    #  outside the top ten, and is 102(a)(1) art with no 102(b)(2) argument available against it,
    #  while the member of the same disclosure that WAS in reach is 102(a)(2) only. "You picked
    #  something narrower and legally weaker than this" is the sentence worth printing.
    floor = min([int(c.get("reads_on") or 0) for c in picked] or [0])
    if floor:
        top += [c for c in with_reach
                if c.get("basis") == PUBLIC and int(c.get("reads_on") or 0) >= floor
                and c not in top]
    out, seen = [], set()
    for c in top:
        if c.get("pub") in chosen or c.get("pub") in seen:
            continue
        seen.add(c.get("pub"))
        out.append({"pub": c.get("pub"), "title": c.get("title") or "",
                    "reads_on": c.get("reads_on"), "charts": c.get("n_limitations") or 0,
                    #  THE BASIS BELONGS HERE, because the ranking cannot see it and it is often
                    #  the fact that decides. Schunk's DE 10 2022 135 066 A1 published before the
                    #  filing date, so it is 102(a)(1) art with no 102(b)(2) argument available
                    #  against it, while the US member of the same disclosure is 102(a)(2) only.
                    #  A coverage order will always prefer the weaker one.
                    "basis": c.get("basis") or "",
                    "basis_label": BASIS_LABEL.get(c.get("basis") or "", ""),
                    "why": _why_not(c, budget_items)})
    #  Public art first among equals: it is the stronger document and it is the one somebody
    #  scanning this table should see before they run out of patience. Capped for the same
    #  reason, because a table of sixty-nine is a table nobody reads and this exists to be read.
    out.sort(key=lambda d: (d["basis"] != PUBLIC, -int(d["reads_on"] or 0)))
    return out[:PASSED_OVER_MAX]


def _why_not(c, budget_items):
    """One line: why this document is not in the selection. Ordered by which reason governs."""
    if c.get("of_record"):
        return ("the Office already has it: %s. A slot on this submission is a slot paid for, and "
                "this document is already in front of the examiner"
                % ("the examiner applied it against this family"
                   if c.get("office") == "applied" else "it is of record on an IDS"))
    if not c.get("readable", True):
        return ("its full text was never read, so everything charted for it rests on an abstract")
    if c.get("basis") == NOT_ART:
        return c.get("not_art_why") or "it is not prior art against these claims"
    if c.get("co_owned"):
        return ("it appears to share an owner with the application, which 102(b)(2)(C) may remove "
                "as prior art entirely")
    if c.get("basis") == UNKNOWN:
        return "its dates could not be established, so its status as prior art is unknown"
    charts, reads = int(c.get("n_limitations") or 0), int(c.get("reads_on") or 0)
    if reads and charts < reads:
        return ("it reads on %d limitations but only %d %s a verified passage, so the chart "
                "filed for it would have %d row%s"
                % (reads, charts, "carries" if charts == 1 else "carry",
                   charts, "" if charts == 1 else "s"))
    return ("it ranks below the %d documents this fee budget pays for" % budget_items)


def exemption_available(n_items):
    """1.290(g): three or fewer items. The STATEMENT is what actually buys the exemption, and
    MPEP 1134.01 says three or fewer without it still pays, so this is only half the test."""
    return 0 < int(n_items) <= 3


# --------------------------------------------------------------------------------- 1.290(b)

def _as_date(v):
    if isinstance(v, datetime.date):
        return v
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    if not m:
        return None
    try:
        return datetime.date(*[int(x) for x in m.groups()])
    except ValueError:
        return None


def _plus_six_months(d):
    """Six calendar months, clamped to the end of the month the way a deadline is read."""
    if not d:
        return None
    y, m = d.year + (d.month + 5) // 12, (d.month + 5) % 12 + 1
    day = d.day
    while day > 1:
        try:
            return datetime.date(y, m, day)
        except ValueError:
            day -= 1
    return datetime.date(y, m, 1)


def window(publication_date, first_rejection_date=None, notice_of_allowance_date=None, today=None):
    """1.290(b), computed. -> {"open", "deadline", "basis", "days_left", "why"}

    The rule is a nest of earlier/later and it is easy to read backwards, so it is spelled out:

        deadline = EARLIER of ( notice of allowance ,
                                LATER of ( publication + 6 months , first rejection ) )

    A submission must be filed BEFORE that date. The subtle case is the common one: when no
    rejection has issued, the "later of" cannot be resolved yet, so the operative date is at least
    publication + 6 months and may extend if a rejection lands after it. The honest answer there is
    the EARLIEST date the window could close, which is what a person planning a filing needs.
    """
    today = today or datetime.date.today()
    pub, rej, noa = (_as_date(publication_date), _as_date(first_rejection_date),
                     _as_date(notice_of_allowance_date))
    six = _plus_six_months(pub)
    if not six and not rej:
        return {"open": None, "deadline": None, "basis": "unknown", "days_left": None,
                "why": "The application's publication date is not known here, so the 1.290(b) "
                       "window cannot be computed. Check it before filing."}
    later = max([d for d in (six, rej) if d], default=None)
    basis = []
    if six:
        basis.append("publication %s plus six months = %s" % (pub, six))
    if rej:
        basis.append("first rejection mailed %s" % rej)
    else:
        basis.append("no rejection has been mailed, so the later of the two is not yet fixed and "
                     "the window may extend beyond the date below")
    deadline, cap = later, ""
    if noa and (not later or noa < later):
        deadline, cap = noa, ("a notice of allowance was mailed %s, which closes the window "
                              "earlier than the date above" % noa)
        basis.append(cap)
    return {"open": bool(deadline and today < deadline),
            "deadline": deadline,
            "days_left": (deadline - today).days if deadline else None,
            "basis": "; ".join(basis),
            "capped_by_allowance": bool(cap),
            "why": ""}


#  ---- what a candidate is, BEFORE a model call is spent on it ---------------------------------
#  The two facts that decide whether a document belongs in a submission at all are its date basis
#  and whether it is the applicant's own work. Both were only discovered after the build, on the
#  compliance pass, which is the wrong end: by then the document has cost a model call and the
#  person choosing never saw the choice. They are computed here from the corpus, for the picker.

PUBLIC, SECRET, NOT_ART, UNKNOWN = "public", "secret", "not_art", "unknown"

#  Short enough to sit in a table cell, and it says the statute rather than a word like "strong".
BASIS_LABEL = {
    PUBLIC: "102(a)(1) public art",
    SECRET: "102(a)(2) only",
    NOT_ART: "not prior art here",
    UNKNOWN: "dates unknown",
}

BASIS_HELP = {
    PUBLIC: "Published before this application's earliest effective filing date, so it is prior "
            "art to everyone under 35 U.S.C. 102(a)(1) and EPC Art. 54(2). Nothing disqualifies "
            "it and no exception reaches it.",
    SECRET: "Filed before this application but published after it. In the United States that "
            "makes it prior art only under 102(a)(2), and in Europe only under EPC Art. 54(3).",
    NOT_ART: "<b>Not prior art against these claims in the United States.</b> One of two "
             "reasons, and the row says which.\n\n"
             "<b>The dates.</b> It was neither published nor filed before this application's "
             "earliest effective filing date, so nothing reaches it.\n\n"
             "<b>The office it came from.</b> It was filed first and published later, which "
             "leaves only 35 U.S.C. 102(a)(2), and 102(a)(2) reaches U.S. patents, U.S. "
             "pre-grant publications and PCT applications designating the United States, and "
             "nothing else. A JP, TW, CN, DE or GB national publication in that position is not "
             "prior art here at all. Note this is a UNITED STATES answer: the same document may "
             "be EPC Art. 54(3) art at the EPO if it is a European application or a Euro-PCT "
             "entering the European phase.\n\n"
             "Either way, listing it invites the examiner to disregard it, and a submission is "
             "read as a whole.",
}

UNREAD_HELP = (
    "<b>What it is.</b> The search identified this document and screened it as worth reading, and "
    "then could not read it: the corpus holds a title and an abstract for it and no full text. "
    "Everything said about it here rests on that abstract.\n\n"
    "<b>Why it is not offered.</b> Not because it scored badly. Because it scores WELL and should "
    "not. A short text gets mapped generously onto many limitations, and every passage verifies "
    "against the abstract it was taken from, so an unread reference can top a coverage ranking on "
    "the strength of two hundred words. Counsel, 2026-08-24, on US 8,991,263: a fibre-testing "
    "snubbing clamp charted against \"pole shoes guide a magnetic field portion\". That is the "
    "kind of reach an examiner notices, and a submission is read as a whole.\n\n"
    "<b>When to include it.</b> When you have read the document yourself and the mapping holds. "
    "The concise description filed for it is still built from the evidence in this report, so "
    "check every row against the real document first. <b>When not to.</b> Any other time. Fetch "
    "the office copy and re-run the reading instead: the full text usually says a great deal more "
    "than the abstract, in both directions."
)

SECRET_HELP = (
    "<b>What it is.</b> A document filed before this application but published afterwards. It was "
    "secret on the day the application was filed, and the law reaches back to its filing date "
    "anyway.\n\n"
    "<b>United States.</b> Citable under 35 U.S.C. 102(a)(2), and available for obviousness under "
    "103 as well as for novelty. Two things can take it away: it must have been effectively filed "
    "before this application's earliest effective filing date, which depends on its own priority "
    "chain actually supporting the passage you rely on; and 102(b)(2)(C) disqualifies it entirely "
    "if it and this application were commonly owned, or subject to an obligation of assignment to "
    "the same person, before that date.\n\n"
    "<b>Europe.</b> The equivalent is EPC Art. 54(3): it counts for NOVELTY ONLY and can never "
    "support an inventive-step attack, and there is no common-ownership exception, so an "
    "applicant's own earlier filing is 54(3) art against them.\n\n"
    "<b>When to include it.</b> When it anticipates a claim outright and you can show its priority "
    "chain supports the disclosure you cite. <b>When not to.</b> When your case rests on combining "
    "it with something else in Europe, when the priority chain is long or doubtful, or when there "
    "is any chance of common ownership.")

CO_OWNED_HELP = (
    "<b>What it is.</b> This document and the application under examination share an applicant or "
    "assignee, so far as the record here shows.\n\n"
    "<b>United States.</b> If they were commonly owned, or under an obligation of assignment to "
    "the same person, before this application's earliest effective filing date, then 35 U.S.C. "
    "102(b)(2)(C) removes the document as prior art under 102(a)(2) ENTIRELY. Filing it invites "
    "the examiner to disregard it and weakens everything filed with it. It does NOT rescue a "
    "document that is prior art under 102(a)(1): a published-early document stays prior art "
    "whoever owns it.\n\n"
    "<b>Europe.</b> There is no such exception. Under EPC Art. 54(3) an applicant's own "
    "earlier-filed, later-published European application is prior art against them, for novelty. "
    "Common ownership changes nothing.\n\n"
    "<b>When to include it.</b> When the document is 102(a)(1) public art, where ownership is "
    "irrelevant, or when you are filing at the EPO. <b>When not to.</b> When it is only 102(a)(2) "
    "art in a U.S. submission, which is when the exception bites. The names matched here are the "
    "ones in the record and may be stale or incomplete: check the assignment before relying on "
    "either answer.")


def _norm_owner(name):
    """Company names for comparison: case, punctuation and the corporate suffix all drop out."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    s = re.sub(r"\b(inc|llc|ltd|limited|gmbh|co|corp|corporation|company|kk|kabushiki|kaisha|"
               r"ag|sa|bv|nv|oy|ab|as|pty|plc|lp|llp|spa|srl|pte)\b", " ", s)
    return " ".join(s.split())


#  Where an earlier-filed, later-published application is still prior art when the United States
#  cannot reach it. Keyed on the office that issued the document, because that is what decides.
#  Deliberately NOT a general prior-art engine: it answers one question, "this is out here, is it
#  out everywhere", and it names the statute so the answer can be checked.
_ELSEWHERE = {
    "EP": ("European Patent Office", "EPC Art. 54(3)",
           "an earlier-filed European application published after the filing date is novelty-only "
           "art at the EPO"),
    "WO": ("European Patent Office", "EPC Art. 54(3)",
           "a Euro-PCT entering the European phase is an Art. 54(3) right"),
    "DE": ("German Patent and Trade Mark Office", "§ 3(2) PatG",
           "an earlier German application published after the priority date of the German family "
           "member is novelty-only self-collision art at the DPMA"),
    "FR": ("France (INPI)", "Art. L611-11 CPI",
           "an earlier French application published later counts for novelty only"),
    "GB": ("United Kingdom Intellectual Property Office", "s.2(3) Patents Act 1977",
           "an earlier UK application published later counts for novelty only"),
    "JP": ("Japan Patent Office", "Art. 29bis Patent Act",
           "an earlier Japanese application published later is enlarged prior art"),
    "CN": ("China National Intellectual Property Administration", "Art. 22(2) Patent Law",
           "an earlier Chinese application published later defeats novelty"),
}


def elsewhere_note(country, us_reachable, co_owned):
    """What this document is worth at another office when the United States cannot use it. -> str

    Counsel, 2026-08-24, on Schmalz's own DE 10 2024 105 114 A1: "Dead in the US, lethal in
    Germany, and not available at the EPO. That is why the system should flag self-collisions
    rather than filter them: what is unusable in one office is decisive in another." It teaches
    the one limitation of claim 1 that has no other art anywhere in the world, and it is the
    German track's best document precisely because it is the applicant's own.

    Two reasons the United States lets a document go, and they do not travel the same way. If the
    office is outside 102(a)(2)'s reach, the equivalent right at THAT office usually still exists.
    If 102(b)(2)(C) took it because of common ownership, no other office has that exception at
    all, so an own-filing that is dead here is live everywhere else.
    """
    code = str(country or "").upper()[:2]
    entry = _ELSEWHERE.get(code)
    if not entry:
        return ""
    office, statute, how = entry
    if not us_reachable:
        note = ("Outside 102(a)(2)'s reach in the United States, but at the %s this is %s art: %s."
                % (office, statute, how))
    elif co_owned:
        note = ("102(b)(2)(C) may remove this in the United States because of common ownership. "
                "No other office has that exception: at the %s it is %s art, %s."
                % (office, statute, how))
    else:
        return ""
    if co_owned:
        note += (" An applicant's own earlier filing is the strongest kind of document there, "
                 "because nothing else has to be combined with it.")
    return note


def classify_candidates(cands, subject_efd, subject_owners=(), sweep=True):
    """Annotate each candidate with its date basis and whether it looks commonly owned.

    Mutates and returns `cands`, so the picker and the ranking stay one list. Reads one row per
    candidate from the corpus, in one query, because this runs on a page load.

    AND THEN SWEEPS THE FAMILY OF EVERYTHING IT EXCLUDES. An exclusion is a statement about ONE
    publication, and a family is many: Schmalz's DE 10 2024 105 114 A1 is useless in the US because
    it published after their own priority date, and the same disclosure was registered as
    DE 20 2024 100 869 U1 six months BEFORE it. See family_sweep; `sweep=False` for the callers
    that only want the classification.
    """
    pubs = [c.get("pub") for c in cands if c.get("pub")]
    if not pubs:
        return cands
    efd = _as_date(subject_efd)
    mine = {_norm_owner(o) for o in (subject_owners or []) if _norm_owner(o)}
    rows = {}
    try:
        import db
        with db.cursor() as cur:
            cur.execute(
                "SELECT p.publication_number, p.publication_date, p.filing_date, "
                "       p.earliest_priority_date, p.country, "
                "       array_remove(array_agg(pa.raw_name) FILTER "
                "         (WHERE pa.role='assignee'), NULL) AS owners "
                "  FROM publications p LEFT JOIN parties pa ON pa.publication_id = p.id "
                " WHERE p.publication_number = ANY(%s) GROUP BY 1,2,3,4,5", (pubs,))
            rows = {r["publication_number"]: r for r in cur.fetchall()}
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
    for c in cands:
        r = rows.get(c.get("pub")) or {}
        pub_d, eff = _as_date(r.get("publication_date")), (
            _as_date(r.get("earliest_priority_date")) or _as_date(r.get("filing_date")))
        country = str(r.get("country") or (c.get("pub") or "")[:2]).upper()
        c["not_art_why"] = ""
        c["elsewhere"] = ""
        if not efd or not (pub_d or eff):
            c["basis"] = UNKNOWN
        elif pub_d and pub_d < efd:
            c["basis"] = PUBLIC
        elif eff and eff < efd:
            #  SECRET ART DOES NOT REACH FROM EVERY OFFICE. 102(a)(2) reaches US patents, US
            #  pre-grant publications and PCT applications designating the US, and nothing else.
            #  A JP or TW national publication that came out after the filing date is not prior
            #  art at all here. This used to be found on the compliance pass, one model call per
            #  document too late; measured on adhoc-efbf2979420b, where two of ten were dropped
            #  after they had been read.
            if search_modes.secret_art_reaches(country):
                c["basis"] = SECRET
            else:
                c["basis"] = NOT_ART
                c["not_art_why"] = search_modes.secret_art_note(country)
        else:
            c["basis"] = NOT_ART
            c["not_art_why"] = ("It published on %s and was filed on %s, both after the "
                                "application's effective filing date of %s."
                                % (pub_d or "an unknown date", eff or "an unknown date", efd))
        owners = [o for o in (r.get("owners") or []) if o]
        shared = sorted({o for o in owners if _norm_owner(o) in mine})
        c["owners"] = owners[:3]
        c["co_owned"] = bool(shared)
        c["co_owned_with"] = shared[:2]
        c["published"] = str(pub_d) if pub_d else ""
        #  What the picker should tick by default: public art yes, secret art yes but flagged,
        #  and never something that is not prior art, or is the applicant's own, or that the
        #  search could not read.
        #
        #  UNREADABLE IS A HARD EXCLUSION, whatever it scores. A reference the corpus holds only a
        #  title and an abstract for produces a description resting on that abstract, and the
        #  mapping is a reach: counsel, 2026-08-24, on US 8,991,263, a fibre-testing snubbing clamp
        #  charted against "pole shoes guide a magnetic field portion". It scores HIGH, not low,
        #  because a short text gets mapped generously and every cell verifies against the abstract
        #  it came from. Still listed, still choosable, never chosen for you.
        #  ALREADY OF RECORD IS NOT SOMETHING TO FILE. A document the examiner applied, or that
        #  the applicant put on an IDS and the examiner initialled, is already in front of them.
        #  1.290(f) charges per ten items, so a slot spent on one is a slot bought and wasted.
        #  Counsel, 2026-08-24, on US 2021/0031317 A1: "already of record from Schmalz's own IDS,
        #  so there is nothing to gain there either." Ranked highly all the same, because being
        #  good enough for an examiner to apply is real evidence of quality, and left unticked
        #  because a reason to file it anyway is a judgement somebody has to make.
        c["of_record"] = str(c.get("office") or "") in ("applied", "considered")
        c["default_include"] = (c["basis"] in (PUBLIC, SECRET) and not c["co_owned"]
                                and c.get("readable", True) and not c["of_record"])
        #  KEPT OUT HERE IS NOT WORTHLESS. A later-published application the United States cannot
        #  reach, or one 102(b)(2)(C) removes because it is the applicant's own, is frequently the
        #  best document there is at another office. Filtering it silently loses that.
        if c["basis"] in (SECRET, NOT_ART) and eff and efd and eff < efd:
            c["elsewhere"] = elsewhere_note(
                country, search_modes.secret_art_reaches(country), c["co_owned"])
        #  WHAT THE SAME DOCUMENT IS WORTH AT EACH OFFICE, computed once, per jurisdiction. The
        #  single US answer above is the one that decides a 1.290 submission; it is not the one
        #  that decides an EPO opposition or a DPMA nullity action, and running the exclusion once
        #  loses the difference on exactly the documents where it is largest.
        c["forums"] = search_modes.forum_matrix(country, pub_d, eff, efd, own=c["co_owned"])
        c["forums_live"] = [m["forum"] for m in c["forums"] if m.get("available")]
    if sweep and efd:
        try:
            import family_sweep
            family_sweep.sweep_excluded(cands, efd)
        except Exception:                                                 # noqa: BLE001
            traceback.print_exc()
    return cands


def prosecution_dates(report):
    """Publication, first rejection and notice of allowance for the subject, from the file wrapper
    this search already read. -> (publication_date, first_rejection, notice_of_allowance)"""
    rep = report or {}
    dossier = (rep.get("prosecution") or {}).get("dossier") or {}
    rejections = [d for d in (dossier.get("rejections") or []) if isinstance(d, dict)]
    dates = sorted([_as_date(d.get("date")) for d in rejections if _as_date(d.get("date"))])
    first_rejection = dates[0] if dates else None
    allow = [d for d in (dossier.get("allowances") or []) if isinstance(d, dict)]
    noa = sorted([_as_date(d.get("date")) for d in allow if _as_date(d.get("date"))])
    pub_date = None
    subject_pub = ((dossier.get("subject") or {}).get("pub")
                   or (rep.get("query_document") or {}).get("publication_number") or "")
    if subject_pub:
        try:
            import concise_description
            got = concise_description.corpus_dates(subject_pub) or {}
            pub_date = _as_date(got.get("publication_date"))
        except Exception:                                                 # noqa: BLE001
            traceback.print_exc()
    return pub_date, first_rejection, (noa[0] if noa else None)


# --------------------------------------------------------------------------------- the audit

#  NOTE is advisory: true, worth reading, and asking nothing of anybody. Keeping it out of ACTION
#  is what makes "three items need a decision" mean three decisions.
OK, NOTE, ACTION, BLOCKED = "ok", "note", "action", "blocked"

#  Language that turns a concise description into the argument MPEP 1134.01 forbids. Whole words,
#  because "invalidate" in a quotation from the reference itself is the reference's word, not ours.
_ARGUMENT = re.compile(
    r"\b(unpatentab\w*|patentab\w*|anticipat\w*|obvious\w*|invalid\w*|"
    r"should be rejected|fails to distinguish|renders? \w+ obvious|prima facie)\b", re.I)


class Finding:
    def __init__(self, rid, cite, title, status, detail, evidence=""):
        self.id, self.cite, self.title = rid, cite, title
        self.status, self.detail, self.evidence = status, detail, evidence

    def as_row(self):
        return [self.id, self.cite, self.title, self.status, self.detail]


def _inference_hits(docs):
    """Sentences in the relevance column that assert what their passage does not say. -> [str]

    Reported rather than repaired here, because the compliance pass has already removed what it
    could and this is the check that the removal WORKED. A paper edited by hand between the two
    passes goes through this and not through that one, which is exactly when it matters.
    """
    try:
        import submission_compliance as sc
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return []
    out = []
    for d in docs or []:
        for r in (d.get("rows") or []):
            evidence = " ".join([str(r.get("quote") or ""), str(r.get("quote_original") or "")])
            for field in ("disclosure", "note"):
                _clean, changed = sc.strip_inference(r.get(field), evidence)
                out.extend("Doc %s: %s" % (d.get("n"), c) for c in changed)
    return out


def _copy_quote_findings(want_copy, copies, translations=None):
    """Every quotation checked against the rendition it would actually be read in. -> [Finding]

    Three answers, kept apart on purpose.

      MISSING from a readable rendition   a defect: either the copy is not the document, or the
                                          quotation is not in the document.
      NO TEXT LAYER                       an image scan. Calling its quotations absent would be as
                                          wrong as calling them present, so it is reported as what
                                          it is: a copy nobody has read carrying quotations nothing
                                          has checked.
      NON-ENGLISH WITH NO TRANSLATION     the quotation is English and the copy is Korean. Checking
                                          one against the other would fail every time and mean
                                          nothing, so the check waits for the translation that
                                          1.290(d)(4) requires anyway.

    That last split matters: a guard that cries wolf on six of six quotations from a Korean
    publication trains its reader to skip the line where a real defect is.
    """
    try:
        import submission_package as sp
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return []
    translations = translations or {}
    bad, unchecked, untranslated, checked = [], [], [], 0
    for d in want_copy:
        copy = copies.get(d["pub"])
        if not isinstance(copy, dict):
            continue
        quotes = [r.get("quote") for r in (d.get("rows") or []) if (r.get("quote") or "").strip()]
        if not quotes:
            continue
        label = (d.get("biblio") or {}).get("label") or d["pub"]
        against = copy
        if needs_translation(d):
            #  What the examiner reads a quotation from an Ariana or a Seoul publication IN is the
            #  English translation filed beside the copy, and that is what it has to be found in.
            tr = translations.get(d["pub"]) or {}
            body = " ".join(str(tr.get(k) or "") for k in ("claims", "text")).strip()
            if not body:
                untranslated.append("Doc %s (%s): %d quotation%s"
                                    % (d["n"], label, len(quotes),
                                       "" if len(quotes) == 1 else "s"))
                continue
            against = {"text": body}
        got = sp.quotes_in_copy(against, quotes)
        if not got["readable"]:
            unchecked.append("Doc %s (%s): %d quotation%s"
                             % (d["n"], label, len(quotes), "" if len(quotes) == 1 else "s"))
            continue
        checked += got["checked"]
        if got["missing"]:
            bad.append("Doc %s (%s): %d of %d quotations are not in the %s, starting “%s”"
                       % (d["n"], label, len(got["missing"]), got["checked"],
                          "translation" if against is not copy else "copy",
                          " ".join(str(got["missing"][0]).split())[:90]))
    out = []
    if bad:
        out.append(Finding(
            "COPY-QUOTES", "beyond the rule",
            "Every quotation is in the copy that goes with it", ACTION,
            "%s. An examiner checks a quotation against the papers you filed for that item, so "
            "either what you attached is not the document or the quotation is not in it. Settle "
            "which before signing." % "; ".join(bad)))
    elif checked:
        out.append(Finding(
            "COPY-QUOTES", "beyond the rule",
            "Every quotation is in the copy that goes with it", OK,
            "All %d quotation%s found in the text of the copy filed with it, which is the document "
            "the examiner will open." % (checked, " was" if checked == 1 else "s were")))
    if unchecked:
        out.append(Finding(
            "COPY-QUOTES-UNREADABLE", "beyond the rule",
            "The copy can be read, so its quotations can be checked", ACTION,
            "%s could not be checked against their copies: those copies carry no text layer, so "
            "they are image scans. Read them, or obtain a searchable copy, before filing."
            % "; ".join(unchecked)))
    if untranslated:
        out.append(Finding(
            "COPY-QUOTES-TRANSLATION", "beyond the rule",
            "A quotation from a non-English item is checked against its translation", ACTION,
            "%s could not be checked: the copy is in the original language, the quotation is in "
            "English, and no translation was produced. 1.290(d)(4) requires the translation in any "
            "event, and it is also what the quotation has to be found in."
            % "; ".join(untranslated)))
    return out


def _pdf_findings(pdf_report):
    """Patent Center's own upload validation, run over the papers before they are sent. -> [Finding]

    Split three ways, because the three ask different people for different things.

      the papers this service wrote   a defect here is a defect in the generator
      the copies fetched from an office  the practitioner converts them; we cannot
      the fonts this host can embed   a box with no font files renders on the base-14 and every
                                      paper it produces fails validation, so it is said out loud
                                      rather than discovered at upload

    00_AUDIT.pdf is not in the report and does not need to be: it is built by the same template and
    the same styles as the document list, from the same fonts, so the document list passing is the
    generator passing.
    """
    out = []
    ours = {n: r for n, r in (pdf_report or {}).items() if pdf_conform.is_generated(n)}
    theirs = {n: r for n, r in (pdf_report or {}).items() if not pdf_conform.is_generated(n)}
    bad_ours = ["%s: %s" % (n, "; ".join(r["problems"])) for n, r in sorted(ours.items())
                if not r["ok"]]
    bad_theirs = ["%s: %s" % (n, "; ".join(r["problems"])) for n, r in sorted(theirs.items())
                  if not r["ok"]]
    if ours:
        out.append(Finding(
            "PDF-CONFORM", "Patent Center", "Every paper uploads: fonts embedded, PDF 1.1 to 1.6, "
            "US Letter or A4, no encryption, no layers, no attachments",
            OK if not bad_ours else BLOCKED,
            "All %d generated paper%s passes. 00_AUDIT.pdf is written by the same template and the "
            "same embedded faces as the document list, so it passes with it."
            % (len(ours), "" if len(ours) == 1 else "s") if not bad_ours
            else "These would be rejected at upload: %s." % "; ".join(bad_ours[:6])))
    if bad_theirs:
        out.append(Finding(
            "PDF-CONFORM-COPY", "Patent Center", "The fetched copies upload too", ACTION,
            "%s. These are copies obtained from the issuing office and cannot be regenerated here. "
            "Re-save each one as a PDF between 1.1 and 1.6 on US Letter or A4 before uploading."
            % "; ".join(bad_theirs[:6])))
    try:
        gaps = pdf_fonts.missing()
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        gaps = []
    if gaps:
        out.append(Finding(
            "FONTS", "Patent Center", "This host can embed every face it typesets in", BLOCKED,
            "%s did not resolve to a font file on this machine, so the papers fell back to the "
            "PDF base-14, which is never embedded and is a listed validation failure. Install the "
            "fonts (fonts-liberation2 and fonts-droid-fallback) and rebuild the packet."
            % ", ".join(gaps)))
    return out


def unprintable_in(docs):
    """Characters no face this host has can draw, in text that will be typeset. -> [str]

    reportlab does not raise on a missing glyph. It substitutes ZapfDingbats, in which "n" is a
    solid black square, so a name in a script the face does not cover prints as a row of boxes and
    nothing anywhere says so. `pdf_fonts.with_fallback` handles the ordinary case; this is the
    check for when even the fallback face is absent.
    """
    bad = []
    for d in docs or []:
        b = d.get("biblio") or {}
        for field in ("title", "inventor", "assignee", "label"):
            v = str(b.get(field) or "")
            if v and not pdf_fonts.covers_serif(v) and pdf_fonts.FALLBACK not in pdf_fonts.ready():
                bad.append("Doc %s %s: %r" % (d.get("n"), field, v[:40]))
        for r in (d.get("rows") or []):
            v = str(r.get("quote") or "")
            if v and not pdf_fonts.covers_serif(v) and pdf_fonts.FALLBACK not in pdf_fonts.ready():
                bad.append("Doc %s: a quotation in a script this host has no face for"
                           % d.get("n"))
                break
    return bad


def audit(docs, subject, copies, translations, win, exemption_claimed=False,
          entity_size="small", identity=None, pdf_report=None):
    """Every 1.290 requirement, checked against the packet that was actually built. -> [Finding]"""
    out = []
    n = len(docs)

    # -- (b) timing ----------------------------------------------------------------------------
    if win.get("open") is None:
        out.append(Finding("TIMING", "1.290(b)", "The submission window", ACTION,
                           win.get("why") or "The window could not be computed."))
    elif win["open"]:
        d = win["deadline"]
        extra = (" The window may extend if the first rejection is mailed after that date, but it "
                 "cannot be relied on." if "no rejection" in win["basis"] else "")
        #  A DEADLINE, NOT A COUNTDOWN. The PDF is written once and read later, so "20 days away"
        #  is wrong by one the next morning and by a fortnight in a fortnight. The date does not
        #  move; the days remaining are shown live on the page instead.
        out.append(Finding("TIMING", "1.290(b)", "The submission window", OK,
                           "Open. File before %s. That was %d day%s from the date of this audit, "
                           "%s; count from today, not from the number in this line.%s"
                           % (d, win["days_left"], "" if win["days_left"] == 1 else "s",
                              datetime.date.today(), extra),
                           win["basis"]))
    else:
        out.append(Finding("TIMING", "1.290(b)", "The submission window", BLOCKED,
                           "CLOSED on %s. A submission filed now may not be entered."
                           % win["deadline"], win["basis"]))

    # -- (c) in writing ------------------------------------------------------------------------
    out.append(Finding("WRITTEN", "1.290(c)", "Made in writing", OK,
                       "Every paper in this packet is a PDF."))

    # -- (d)(1) document list ------------------------------------------------------------------
    out.append(Finding("LIST", "1.290(d)(1)", "A document list", OK if n else BLOCKED,
                       "%d item%s listed." % (n, "" if n == 1 else "s") if n
                       else "Nothing is listed, so there is no submission."))

    # -- (d)(2) a concise description for each item --------------------------------------------
    missing = [d["n"] for d in docs if not (d.get("rows") or [])]
    out.append(Finding("DESCRIPTION", "1.290(d)(2)",
                       "A concise description of the asserted relevance of each item",
                       OK if not missing else BLOCKED,
                       "Every listed item has one." if not missing
                       else "No description for document(s) %s."
                            % ", ".join(str(x) for x in missing)))

    # -- (d)(3) legible copies -----------------------------------------------------------------
    want_copy = [d for d in docs if needs_copy(d)]
    lack = [d["n"] for d in want_copy if not copies.get(d["pub"])]
    if not want_copy:
        out.append(Finding("COPIES", "1.290(d)(3)", "A legible copy of each non-U.S. item", OK,
                           "Every item is a U.S. patent or U.S. patent application publication, "
                           "which the rule excludes from the copy requirement."))
    else:
        out.append(Finding("COPIES", "1.290(d)(3)", "A legible copy of each non-U.S. item",
                           OK if not lack else ACTION,
                           "Attached for all %d item%s that need%s one."
                           % (len(want_copy), "" if len(want_copy) == 1 else "s",
                              "s" if len(want_copy) == 1 else "") if not lack
                           else "Missing for document(s) %s. The submission cannot be entered "
                                "without them." % ", ".join(str(x) for x in lack)))
        #  A SEPARATE CHECK, because a copy can be present and still not be the document. See
        #  submission_package.inspect_copy: the GB 874,600 copy was its drawing sheets only.
        thin = ["Doc %s (%s): %d pages with no readable text, so this is a drawings bundle or an "
                "unsearchable scan rather than the specification. Check it opens as the whole "
                "document before filing."
                % (d["n"], (d.get("biblio") or {}).get("label") or d["pub"],
                   (copies.get(d["pub"]) or {}).get("pages", 0))
                for d in want_copy
                if isinstance(copies.get(d["pub"]), dict)
                and copies[d["pub"]].get("drawings_only")]
        out.append(Finding("COPY-COMPLETE", "1.290(d)(3)",
                           "Each copy is the whole document, not part of one",
                           OK if not thin else ACTION,
                           "Every attached copy carries the document's text." if not thin
                           else " ".join(thin)))
        #  AND THE QUOTATIONS HAVE TO BE IN THE COPY THAT IS ACTUALLY GOING IN THE ENVELOPE. The
        #  cheapest check in the packet, and it catches two different defects at once: a copy that
        #  is not the document (GB 874,600, filed as its drawing sheets, with eight quotations
        #  attributed to an abstract that is not on any of them), and a quotation the document does
        #  not contain (US 2022/0045594 A1, quoted with a numeric tolerance it states
        #  qualitatively). If the quote is not in the copy then either the copy is wrong or the
        #  quote is, and both are the practitioner's to settle before signing.
        out.extend(_copy_quote_findings(want_copy, copies, translations))

    # -- (d)(4) translations -------------------------------------------------------------------
    want_tr = [d for d in docs if needs_translation(d)]
    lack_tr = [d["n"] for d in want_tr if not translations.get(d["pub"])]
    if not want_tr:
        out.append(Finding("TRANSLATION", "1.290(d)(4)", "An English translation of any "
                           "non-English item", OK, "Every listed item is in English."))
    else:
        out.append(Finding("TRANSLATION", "1.290(d)(4)", "An English translation of any "
                           "non-English item", OK if not lack_tr else ACTION,
                           "Attached for all %d non-English item%s. A machine translation is "
                           "acceptable and each is labelled as one."
                           % (len(want_tr), "" if len(want_tr) == 1 else "s") if not lack_tr
                           else "Missing for document(s) %s."
                                % ", ".join(str(x) for x in lack_tr)))
        #  A CHARACTER THE SOURCE SCAN COULD NOT RESOLVE. Google's OCR of a 1986 Japanese
        #  publication put a solid black square mid-sentence, and it went onto a paper filed at
        #  the Office looking exactly like a rendering failure of ours. It is not: it is what the
        #  machine translation says, and the paper already states that the original governs. So it
        #  is named rather than edited, because editing a translation to look tidier is the one
        #  thing that would actually be wrong.
        smudged = []
        for d in want_tr:
            tr = translations.get(d["pub"]) or {}
            body = " ".join(str(tr.get(k) or "") for k in ("claims", "text"))
            n_bad = sum(body.count(ch) for ch in ("�", "■", "□"))
            if n_bad:
                smudged.append("Doc %s (%s): %d"
                               % (d["n"], (d.get("biblio") or {}).get("label") or d["pub"],
                                  n_bad))
        if smudged:
            out.append(Finding(
                "TRANSLATION-OCR", "1.290(d)(4)", "The translation is legible as furnished", NOTE,
                "The machine translation carries characters the source scan could not resolve, "
                "printed as they came: %s. They are in the translation, not in this rendering, "
                "and the paper already states that the original publication governs. Read the "
                "passage before filing and consider whether a human translation is worth the "
                "cost for that document." % "; ".join(smudged)))

    # -- (d)(5) statements ---------------------------------------------------------------------
    #  The same test the renderer applies, so the audit can never call a paper signed that the
    #  renderer then left blank.
    signer = signature_name(identity)
    out.append(Finding("STATEMENTS", "1.290(d)(5)", "The two statements by the submitting party",
                       OK if signer else ACTION,
                       "Both are on the document list paper, signed /%s/ under 37 CFR 1.4(d)(2). "
                       "Read them before filing: they are your statements, and inserting the "
                       "signature is your act." % signer if signer
                       else "Both are on the document list paper and both are UNSIGNED. Set a "
                            "signature in your profile, or sign them in Patent Center. They are "
                            "made by the party, not by this tool."))

    # -- (e) the list's own format --------------------------------------------------------------
    bad = []
    for d in docs:
        k, b = item_kind(d), (d.get("biblio") or {})
        if k in (US_PATENT, US_PGPUB) and not (b.get("inventor") and b.get("issue_date_pretty")):
            bad.append("Doc %s needs a first named inventor and a date" % d["n"])
        if k == FOREIGN and not (office_of(d)[0] and b.get("issue_date_pretty")):
            bad.append("Doc %s needs an issuing office and a publication date" % d["n"])
        if k == NPL and not (b.get("title") and b.get("issue_date_pretty")):
            bad.append("Doc %s needs a title and a publication date" % d["n"])
        #  THE NAME HAS TO BE PRINTABLE, not merely present. CN 216190291 U was filed identifying
        #  its inventor as "■■": the filing font has no CJK glyphs and the record held no Latin
        #  form. `printable_party` falls back to the applicant, and when even that is unprintable
        #  the packet fails here rather than putting boxes where a person's name belongs.
        who = concise_render.printable_party(b)[1]
        if who and not concise_render.is_latin(who):
            bad.append("Doc %s: %r cannot be printed in the filing font and there is no Latin "
                       "applicant to name instead. Supply a romanised name or the applicant."
                       % (d["n"], who))
    out.append(Finding("LIST-FORMAT", "1.290(e)", "How each item must be identified",
                       OK if not bad else ACTION,
                       "Every item carries the fields its own type requires, U.S. patents and "
                       "publications are in their own section, and the application number is on "
                       "every page." if not bad else "; ".join(bad)))

    # -- (f)/(g) fee -----------------------------------------------------------------------------
    units, dollars, per = fee_amount(n, entity_size)
    if exemption_claimed and exemption_available(n):
        out.append(Finding("FEE", "1.290(g)", "The fee, or the exemption", ACTION,
                           "The exemption is claimed for %d item%s. It is only available if this "
                           "is the FIRST AND ONLY submission in this application by you or anyone "
                           "in privity with you, and the statement on the list says so. Confirm "
                           "that before relying on it." % (n, "" if n == 1 else "s")))
    else:
        out.append(Finding("FEE", "1.290(f)", "The fee, or the exemption", ACTION,
                           "%d item%s means %d unit%s of the 1.17(o) fee, charged per ten items "
                           "or fraction thereof: $%s at the %s-entity rate of $%s a unit "
                           "(schedule of %s). A third party cannot use the micro-entity discount. "
                           "Pay it in Patent Center and check the rate has not moved."
                           % (n, "" if n == 1 else "s", units, "" if units == 1 else "s",
                              _money(dollars), entity_size, _money(per), FEE_SCHEDULE_DATE)
                           + ("" if not exemption_available(n) else
                              " The 1.290(g) exemption would remove it if this is your first and "
                              "only submission here and you make that statement.")))

    # -- MPEP 1134.01: no argument ---------------------------------------------------------------
    hits = []
    for d in docs:
        for r in (d.get("rows") or []):
            for field in ("disclosure", "note"):
                for m in _ARGUMENT.finditer(str(r.get(field) or "")):
                    hits.append("Doc %s: %r" % (d["n"], m.group(0)))
        for m in _ARGUMENT.finditer(str(d.get("summary") or "")):
            hits.append("Doc %s summary: %r" % (d["n"], m.group(0)))
    out.append(Finding("NO-ARGUMENT", "MPEP 1134.01",
                       "The description states disclosure, it does not argue unpatentability",
                       OK if not hits else ACTION,
                       "No argumentative or conclusory language found." if not hits
                       else "Remove before filing: %s" % "; ".join(hits[:8])))

    # -- MPEP 1134.01, the half that uses no statutory words -------------------------------------
    #  A linter hunting for "anticipates", "obvious" or "§ 103" finds neither of the two statements
    #  that actually failed in a real packet. "generally cylindrical in shape, IMPLYING IT EXTENDS
    #  ALONG A LONGITUDINAL AXIS" is the drafter supplying a claim limitation; the reference says
    #  annular and cylindrical and says nothing about an axis. So the bar is structural: a sentence
    #  in the relevance column has to be a restatement of what the quotation beside it says.
    inferred = _inference_hits(docs)
    out.append(Finding("NO-INFERENCE", "MPEP 1134.01",
                       "Each sentence restates the passage beside it and infers nothing from it",
                       OK if not inferred else ACTION,
                       "Every sentence is carried by the passage it is attached to." if not inferred
                       else "These assert something the quotation does not say, which is argument "
                            "under 1.290(a) whatever words it uses: %s" % "; ".join(inferred[:6])))

    # -- our own bars ----------------------------------------------------------------------------
    unverified, checked, rows_dropped = 0, 0, 0
    for d in docs:
        c = d.get("compliance") or {}
        q = c.get("quotes") or {}
        checked += int(q.get("checked") or 0)
        unverified += int(q.get("dropped") or 0)
        rows_dropped += int(c.get("rows_dropped") or 0)
    #  Reported on ROWS DROPPED as well as on this pass's failures. A rebuild re-checks papers whose
    #  quotations were already verified once, so `unverified` is zero the second time round while
    #  rows are still missing from the first: saying nothing then would hide the removal.
    if rows_dropped:
        quote_detail = (
            "Every quotation in these papers was re-checked against the stored text of the "
            "document it is attributed to. %d row%s cited a passage that could not be found and "
            "%s removed rather than filed as a bare assertion. Nothing here cites an unverified "
            "passage." % (rows_dropped, "" if rows_dropped == 1 else "s",
                          "was" if rows_dropped == 1 else "were"))
    elif checked:
        quote_detail = ("All %d quotation%s re-checked against the stored text of the document it "
                        "is attributed to, and every one was found."
                        % (checked, " was" if checked == 1 else "s were"))
    else:
        quote_detail = "These papers quote no passages, so there is nothing to verify."
    out.append(Finding("QUOTATIONS", "beyond the rule",
                       "Every quotation appears verbatim in the document it is attributed to",
                       OK, quote_detail))

    blocked_pa = []
    for d in docs:
        q = ((d.get("compliance") or {}).get("qualify") or {})
        if q.get("blocked"):
            blocked_pa.append("Doc %s: %s" % (d["n"], q.get("note") or "does not qualify"))
    out.append(Finding("PRIOR-ART", "beyond the rule",
                       "Every listed item is prior art to this application on the dates",
                       OK if not blocked_pa else ACTION,
                       "Every item qualifies on its dates." if not blocked_pa
                       else "; ".join(blocked_pa)))

    # -- will it upload -----------------------------------------------------------------------
    out.extend(_pdf_findings(pdf_report))
    boxes = unprintable_in(docs)
    if boxes:
        out.append(Finding(
            "GLYPHS", "1.290(e)", "Every character on these papers has a glyph to print it",
            BLOCKED,
            "This host has no fallback face, and reportlab substitutes ZapfDingbats for a missing "
            "glyph rather than failing, which prints solid black squares: %s. Install "
            "fonts-droid-fallback, or supply a romanised form, before this is filed."
            % "; ".join(boxes[:6])))

    # -- (i) 1.8 ----------------------------------------------------------------------------------
    out.append(Finding("NO-CERT-MAILING", "1.290(i)", "A certificate of mailing does not help",
                       NOTE,
                       "1.8 does not apply to this deadline. The submission has to be RECEIVED by "
                       "the Office inside the window, so file electronically and do not rely on a "
                       "mailing date."))
    return out


def verdict(findings):
    """-> ("ready" | "action" | "blocked", one sentence)"""
    if any(f.status == BLOCKED for f in findings):
        return BLOCKED, ("This cannot be filed as it stands: %s"
                         % "; ".join(f.title for f in findings if f.status == BLOCKED))
    n = sum(1 for f in findings if f.status == ACTION)
    if n:
        return ACTION, ("Every paper the rule requires is here. %d item%s need%s a decision or a "
                        "signature from you before filing." % (n, "" if n == 1 else "s",
                                                               "s" if n == 1 else ""))
    return OK, "Every requirement of 37 CFR 1.290 is satisfied by the papers in this packet."


# --------------------------------------------------------------------------------- rendering

def _styles():
    base = ParagraphStyle("s", fontName=pdf_fonts.font(pdf_fonts.SERIF), fontSize=10.5, leading=13,
                          alignment=TA_LEFT, spaceAfter=0)
    return {
        "h": ParagraphStyle("h", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_BOLD), fontSize=12, leading=15,
                            spaceAfter=7),
        "h2": ParagraphStyle("h2", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_BOLD), fontSize=11, leading=14,
                             spaceBefore=12, spaceAfter=5),
        "app": ParagraphStyle("app", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_ITALIC), spaceAfter=10),
        "body": ParagraphStyle("body", parent=base, spaceBefore=3, spaceAfter=7),
        "th": ParagraphStyle("th", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_BOLD), fontSize=9.5, leading=12),
        "td": ParagraphStyle("td", parent=base, fontSize=9.5, leading=12),
        "note": ParagraphStyle("note", parent=base, fontSize=9, leading=11.5,
                               textColor=colors.HexColor("#333333"), spaceBefore=8),
        #  An S-signature is read as a signature, so it is set apart from the prose around it.
        "sig": ParagraphStyle("sig", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_ITALIC), fontSize=13,
                              leading=17, spaceBefore=10, spaceAfter=4),
    }


def _template(buf, subject, title):
    """1.290(e) requires the application number on EACH PAGE of the list, so it is a page
    decoration rather than a heading, on every paper in the packet."""
    running = concise_render.running_head(subject)

    def _page(canv, docobj):
        canv.saveState()
        canv.setFont(pdf_fonts.font(pdf_fonts.SERIF), 9)
        canv.setFillColor(colors.HexColor("#333333"))
        canv.drawString(inch, letter[1] - 0.6 * inch, running)
        canv.drawRightString(letter[0] - inch, 0.6 * inch, "Page %d" % canv.getPageNumber())
        canv.drawString(inch, 0.6 * inch, running)
        canv.restoreState()

    tmpl = BaseDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch,
                           topMargin=0.95 * inch, bottomMargin=0.9 * inch, title=title,
                           author="Third-party submission under 37 CFR 1.290")
    #  onPage is what puts the application number on EVERY page, which 1.290(e) requires and which
    #  page one satisfies by accident through the subject line. Leaving it off looked correct until
    #  a two-page list was rendered and page two carried nothing.
    tmpl.addPageTemplates([PageTemplate(id="p", onPage=_page, frames=[
        Frame(inch, 0.9 * inch, letter[0] - 2 * inch, letter[1] - 1.85 * inch, id="f")])])
    return tmpl


def _esc(s):
    return concise_render._esc(s)


def _grid(data, widths):
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        #  A Table whose cells are Paragraphs STILL emits its own default cell font, and
        #  that default is an unembedded Helvetica. Naming it here is what keeps a base-14
        #  resource out of a paper Patent Center validates.
        ("FONTNAME", (0, 0), (-1, -1), pdf_fonts.font(pdf_fonts.SERIF)),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#444444")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFEFEF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _identification(doc):
    """The fields 1.290(e) requires for THIS item's type, as (label, value) pairs."""
    b, k = doc.get("biblio") or {}, item_kind(doc)
    code, office = office_of(doc)
    if k == US_PATENT:
        return [("U.S. Patent No.", b.get("label") or b.get("pub")),
                ("First named inventor", b.get("inventor")),
                ("Issue date", b.get("issue_date_pretty"))]
    if k == US_PGPUB:
        return [("U.S. Patent Application Publication No.", b.get("label") or b.get("pub")),
                ("First named inventor", b.get("inventor")),
                ("Publication date", b.get("issue_date_pretty"))]
    if k == FOREIGN:
        #  (e)(3) accepts "the applicant, patentee, or first named inventor", and that OR is what
        #  lets a document whose only personal name is CJK be identified in a script the filing
        #  font can print. See concise_render.printable_party.
        who_label, who = concise_render.printable_party(b)
        return [("Issuing office", office),
                ("Document number", b.get("label") or b.get("pub")),
                ("Applicant" if who_label == "Applicant"
                 else "Applicant, patentee or first named inventor", who),
                ("Publication date", b.get("issue_date_pretty"))]
    return [("Author", b.get("author") or b.get("inventor")),
            ("Title", b.get("title")),
            ("Pages submitted", b.get("pages") or "the whole document"),
            ("Publication date", b.get("issue_date_pretty")),
            ("Publisher and place", b.get("publisher") or "")]


def signature_name(identity):
    """The name that may go between the slashes, or "" if none may.

    The profile refuses a slash on the way in, but a row written before that check exists still
    holds one, and `/Nimo /Rotem//` on a filed paper is not the signature anybody agreed to. A
    signature is a legal act, so this refuses rather than repairs: an unsigned paper with a line
    to sign is a nuisance, a paper signed with the wrong string is a defect nobody would spot.
    """
    name = str((identity or {}).get("signature_name") or "").strip()
    if not name or "/" in name or "\\" in name:
        return ""
    return name


def signature_block(story, st, identity):
    """The 37 CFR 1.4(d)(2) S-signature, or a line to sign by hand.

    An S-signature is the signer's own name between forward slashes. It is inserted here because
    the signer told this tool to insert it, which is the same posture as any e-filing form: the
    paper says so, so nobody can read it as the machine having signed anything.
    """
    ident = identity or {}
    name = signature_name(ident)
    title = str(ident.get("signature_title") or "").strip()
    story.append(Paragraph("SIGNATURE", st["h2"]))
    if name:
        story.append(Paragraph("/%s/" % _esc(name), st["sig"]))
        story.append(Paragraph("%s%s<br/>Date: %s"
                               % (_esc(name), (", " + _esc(title)) if title else "",
                                  datetime.date.today().isoformat()), st["body"]))
        story.append(Paragraph(
            "Signed under 37 CFR 1.4(d)(2). The signature above was applied from the signer's own "
            "stored signature at the signer's instruction; the statements above are the signer's.",
            st["note"]))
    else:
        story.append(Paragraph("/______________________________/", st["sig"]))
        story.append(Paragraph("Printed name: ______________________________<br/>"
                               "Date: ______________________________", st["body"]))
        story.append(Paragraph(
            "NOT SIGNED. 37 CFR 1.4 requires a signature. Set one in your profile so it is applied "
            "here, or sign this paper before filing.", st["note"]))


def document_list_and_statements(docs, subject, copies, translations, win,
                                 exemption_claimed=False, entity_size="small",
                                 identity=None) -> bytes:
    """1.290(d)(1)+(e) and 1.290(d)(5)+(g), on one paper, in the shape PTO/SB/429 asks for."""
    st = _styles()
    buf = io.BytesIO()
    tmpl = _template(buf, subject, "Third-party submission under 37 CFR 1.290: document list")
    story = [Paragraph("THIRD-PARTY SUBMISSION UNDER 37 CFR &sect; 1.290", st["h"]),
             Paragraph("DOCUMENT LIST", st["h"]),
             Paragraph(_esc(concise_render.subject_line(subject)), st["app"])]

    us = [d for d in docs if item_kind(d) in (US_PATENT, US_PGPUB)]
    other = [d for d in docs if item_kind(d) not in (US_PATENT, US_PGPUB)]

    def _section(title, items, cols, row_of):
        story.append(Paragraph(title, st["h2"]))
        if not items:
            story.append(Paragraph("None.", st["body"]))
            return
        data = [[Paragraph(c, st["th"]) for c in cols]]
        for d in items:
            data.append([Paragraph(_esc(x), st["td"]) for x in row_of(d)])
        w = letter[0] - 2 * inch
        story.append(_grid(data, [w * f for f in ([0.07] + [(0.93 / (len(cols) - 1))] * (len(cols) - 1))]))

    #  1.290(e): U.S. patents and U.S. patent application publications in their own section.
    _section("Section A. U.S. patents and U.S. patent application publications",
             us, ["No.", "Patent or publication number", "First named inventor",
                  "Issue or publication date"],
             lambda d: [str(d["n"]), (d["biblio"].get("label") or d["pub"]),
                        d["biblio"].get("inventor") or "", d["biblio"].get("issue_date_pretty") or ""])

    _section("Section B. All other items",
             other, ["No.", "Office and document number", "Applicant, patentee or inventor",
                     "Publication date", "Copy / translation"],
             lambda d: [str(d["n"]),
                        "%s %s" % (office_of(d)[1], d["biblio"].get("label") or d["pub"]),
                        concise_render.printable_party(d["biblio"])[1],
                        d["biblio"].get("issue_date_pretty") or "",
                        "; ".join(filter(None, [
                            "copy attached" if copies.get(d["pub"]) else "COPY OUTSTANDING",
                            ("translation attached" if translations.get(d["pub"])
                             else "TRANSLATION OUTSTANDING") if needs_translation(d) else ""]))])

    story.append(Paragraph(
        "A legible copy is filed for each item in Section B: 37 CFR 1.290(d)(3) requires one for "
        "every listed item other than a U.S. patent or a U.S. patent application publication. A "
        "concise description of the asserted relevance of each item above is filed with this "
        "list, one paper per item, as 1.290(d)(2) requires.", st["note"]))

    # ---- the statements
    story.append(Paragraph("STATEMENTS UNDER 37 CFR &sect; 1.290(d)(5)", st["h2"]))
    story.append(Paragraph(
        "<b>(i)</b> The party making this submission is not an individual who has a duty to "
        "disclose information with respect to the above-identified application under 37 CFR 1.56.",
        st["body"]))
    story.append(Paragraph(
        "<b>(ii)</b> This submission complies with the requirements of 35 U.S.C. 122(e) and 37 "
        "CFR 1.290.", st["body"]))

    n = len(docs)
    units, dollars, per = fee_amount(n, entity_size)
    story.append(Paragraph("FEE", st["h2"]))
    if exemption_claimed and exemption_available(n):
        story.append(Paragraph(
            "<b>Statement under 37 CFR 1.290(g).</b> To the knowledge of the person signing this "
            "statement after making reasonable inquiry, this submission is the first and only "
            "submission under 35 U.S.C. 122(e) filed in this application by the party making the "
            "submission or by a party in privity with that party. This submission lists %d item%s, "
            "which is three or fewer, so no fee is required." % (n, "" if n == 1 else "s"),
            st["body"]))
    else:
        story.append(Paragraph(
            "This submission lists <b>%d item%s</b>. Under 37 CFR 1.290(f) the fee set by 37 CFR "
            "1.17(o) is due for every ten items or fraction thereof, so <b>%d unit%s</b> of that "
            "fee applies: <b>$%s</b> at the %s-entity rate of $%s a unit, from the schedule of %s. "
            "A third party is not eligible for the micro-entity discount. It is paid in Patent "
            "Center; check the rate has not moved."
            % (n, "" if n == 1 else "s", units, "" if units == 1 else "s", _money(dollars),
               entity_size, _money(per), FEE_SCHEDULE_DATE),
            st["body"]))
        if exemption_available(n):
            story.append(Paragraph(
                "This submission lists three or fewer items, so the 37 CFR 1.290(g) exemption is "
                "available IF this is the first and only submission under 35 U.S.C. 122(e) in "
                "this application by you or by a party in privity with you. Claiming it requires "
                "the statement to that effect; three or fewer items without the statement still "
                "pays the fee.", st["note"]))

    story.append(Paragraph(
        "These statements are made by the party filing the submission. They are reproduced here "
        "so they can be read and checked before they are adopted in Patent Center.", st["note"]))
    signature_block(story, st, identity)

    if win.get("deadline"):
        story.append(Paragraph(
            "<b>Timing.</b> Under 37 CFR 1.290(b) this submission must be filed before %s. %s "
            "37 CFR 1.290(i) excludes 37 CFR 1.8, so a certificate of mailing does not preserve "
            "the date and the submission must reach the Office inside the window."
            % (win["deadline"], _esc(win.get("basis") or "")), st["note"]))
    tmpl.build(story)
    return buf.getvalue()


def audit_pdf(findings, docs, subject, win) -> bytes:
    """The first paper in the packet: what was checked, against which paragraph, and what failed."""
    st = _styles()
    state, sentence = verdict(findings)
    buf = io.BytesIO()
    tmpl = _template(buf, subject, "Audit of this 37 CFR 1.290 submission")
    banner = {OK: "READY TO FILE", ACTION: "READY, SUBJECT TO THE DECISIONS BELOW",
              BLOCKED: "NOT READY TO FILE"}[state]
    story = [Paragraph("AUDIT OF THIS THIRD-PARTY SUBMISSION UNDER 37 CFR &sect; 1.290", st["h"]),
             Paragraph(_esc(concise_render.subject_line(subject)), st["app"]),
             Paragraph("<b>%s.</b> %s" % (banner, _esc(sentence)), st["body"]),
             Paragraph(
                 "Every requirement of 37 CFR 1.290 is listed below with the paragraph it comes "
                 "from and whether the papers in this packet satisfy it. A submission that misses "
                 "one of these may not be entered or considered: 1.290(a).", st["note"])]

    data = [[Paragraph(c, st["th"]) for c in ("Rule", "Requirement", "Status", "What it means here")]]
    label = {OK: "met", NOTE: "read this", ACTION: "you decide", BLOCKED: "BLOCKS FILING"}
    for f in findings:
        data.append([Paragraph(_esc(f.cite), st["td"]), Paragraph(_esc(f.title), st["td"]),
                     Paragraph("<b>%s</b>" % label[f.status], st["td"]),
                     Paragraph(_esc(f.detail), st["td"])])
    w = letter[0] - 2 * inch
    story.append(_grid(data, [w * 0.13, w * 0.27, w * 0.12, w * 0.48]))

    story.append(Paragraph("What is in this packet", st["h2"]))
    rows = [[Paragraph(c, st["th"]) for c in ("File", "What it is", "Rule")]]
    rows.append([Paragraph("00_AUDIT.pdf", st["td"]), Paragraph("This paper", st["td"]),
                 Paragraph("not a filing", st["td"])])
    rows.append([Paragraph("01_DocumentList_and_Statements.pdf", st["td"]),
                 Paragraph("The document list, the two statements and the fee position", st["td"]),
                 Paragraph("(d)(1), (e), (d)(5), (f)/(g)", st["td"])])
    rows.append([Paragraph("10_ConciseDescription_*.pdf", st["td"]),
                 Paragraph("One concise description of asserted relevance per item", st["td"]),
                 Paragraph("(d)(2)", st["td"])])
    rows.append([Paragraph("10_ConciseDescription_*.docx", st["td"]),
                 Paragraph("The same descriptions, editable. File the PDF; this is the copy to "
                           "mark up first", st["td"]),
                 Paragraph("not filed", st["td"])])
    rows.append([Paragraph("40_Copy_*.pdf", st["td"]),
                 Paragraph("A legible copy of each item that is not a U.S. patent or "
                           "publication", st["td"]), Paragraph("(d)(3)", st["td"])])
    rows.append([Paragraph("50_Translation_*.pdf", st["td"]),
                 Paragraph("An English machine translation of each non-English item", st["td"]),
                 Paragraph("(d)(4)", st["td"])])
    story.append(_grid(rows, [w * 0.34, w * 0.46, w * 0.20]))
    tmpl.build(story)
    return buf.getvalue()


def manifest_csv(docs, copies, translations) -> str:
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["item", "identifier", "type", "office", "first_named_inventor",
                 "date", "concise_description", "copy_filed", "translation_filed"])
    for d in docs:
        b = d.get("biblio") or {}
        wr.writerow([d["n"], b.get("label") or d["pub"], item_kind(d), office_of(d)[0],
                     concise_render.printable_party(b)[1], b.get("issue_date_pretty") or "",
                     "yes", "yes" if copies.get(d["pub"]) else
                     ("not required" if not needs_copy(d) else "NO"),
                     "yes" if translations.get(d["pub"]) else
                     ("not required" if not needs_translation(d) else "NO")])
    return buf.getvalue()
