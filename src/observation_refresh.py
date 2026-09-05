"""Pull the docket's register facts again, live, from the offices that publish an API.

The docket used to be a snapshot. Somebody ran four scripts on another machine, one of them
driving a real Chrome at DPMAregister for an hour, merged the results into a JSON file and shipped
it; `ASOF` was a constant in that script and every "days left" on the page was counted from it.
That is fine on the day it is built and wrong on every day after, and the failure is silent: a
window that closed last week still reads as open because nothing recomputed it.

Two things changed that make a button possible. The EPO OPS account came through, so the European
Register and the INPADOC legal file are an HTTP call rather than a scraped page, and INPADOC
carries German national events, which is what the Chrome sweep was for. And the USPTO Open Data
Portal serves the file wrapper, which is where our own submissions actually appear. So:

  USPTO   ODP  /patent/applications/{app}          status, publication, filing
               /patent/applications/{app}/documents the file wrapper, including OUR filings
               /patent/applications/search          new Schmalz applications
  EPO     OPS  /register/publication/epodoc/{pub}/biblio    status, A1 and B1 publication dates
  DPMA    OPS  /legal/publication/epodoc/{pub}/     INPADOC events: R012 examination requested,
                                                    R018 grant decision, and the B publication
                                                    that starts the nine months
  new     OPS  /published-data/search?q=pa=...      publications this docket has never seen

WHAT THIS IS ALLOWED TO OVERWRITE. The register facts, and nothing else. `user_state` and
`user_note` belong to the person, the counsel columns come from counsel's own tracker, and a
refresh that reset either of those would be worse than a stale date. Every merge here is a
whitelist, not a replace.

INPADOC IS FRESHER THAN DOCDB, which is not obvious and cost a day. DE 10 2024 133 318 granted on
20 August 2026; on 4 September the biblio search still returned only the A1, while the legal
service already listed the B4 with its date. So grant is detected from the legal family, never
from a bibliographic search.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import db
import observation_actions as acts

#  WHO A DOCKET IS ABOUT IS A PROPERTY OF THE DOCKET, NOT OF THIS MODULE. It was a pair of
#  constants naming Schmalz, which is correct for the one docket that ships and wrong for every
#  other: this app takes signups, each account keeps its own private docket, and a hard-coded
#  competitor would have seeded somebody else's list of targets into a stranger's account the
#  first time they pressed the button. The applicants are read off the rows the person already
#  has, so an empty docket discovers nothing and a docket about somebody else discovers them.
MAX_APPLICANT_QUERIES = 4
#  A name has to appear on this many rows before it is worth a search: one row is as likely to be
#  a co-applicant or a typo as a target.
MIN_ROWS_PER_APPLICANT = 2
#  Corporate furniture, stripped before searching. OPS matches on the words, and "GmbH" alone
#  would return every German company there is.
_APPLICANT_NOISE = re.compile(
    r"\b(gmbh|mbh|ag|kg|co|kgaa|se|ltd|limited|llc|inc|corp|corporation|company|holding|"
    r"holdings|group|und|and|the|of)\b\.?", re.I)

#  Offices this docket can actually act against. A Chinese family member or a Polish translation of
#  a European patent is not a window we can file into, and adding them would bury the ones we can.
TRACKED_COUNTRIES = ("EP", "DE", "US", "WO")
OFFICE_OF = {"EP": "EPO", "DE": "DPMA", "US": "USPTO", "WO": "WIPO (PCT)"}
#  T is a national translation of an EP patent, not a publication in its own right.
SKIP_KINDS = ("T1", "T2", "T3", "T4", "T5", "T8")

#  How far back a discovery sweep looks. A year is comfortably more than the gap between refreshes
#  and cheap: the search returns publication numbers only.
DISCOVERY_MONTHS = 12

USPTO_REJECTIONS = ("CTNF", "CTFR")
USPTO_ALLOWANCE = ("N/=.", "NOA")
USPTO_QUAYLE = ("CTEQ", "MCTEQ")
#  What OUR OWN filing looks like from the office's side. IDS.3P is the submission itself,
#  3P.RELEVANCE is one concise description of relevance per document, N417.PYMT the fee.
US_SUBMISSION_DOCS = {"IDS.3P": "Third-party submission under 37 CFR 1.290",
                      "3P.RELEVANCE": "Concise description of relevance",
                      "M327": "Office communication about the submission"}
US_SUBMISSION_EVENT = re.compile(r"third.?part|pre.?issuance|preissuance|protest", re.I)

#  Only these keys are written back onto a case. Everything else on the row is either the person's
#  or counsel's and a sweep has no business touching it.
MERGE_FIELDS = (
    "register_status", "register_updated", "register_url", "posture", "deadline", "deadline_kind",
    "closing_soon", "closing_note", "grant_published", "opposition_deadline", "opposition_opens",
    "opposition_opens_est", "opposition_pending", "scheduled_grant", "decision_on", "refused_on",
    "lapsed_on", "exam_requested", "exam_request_deadline", "pubDate", "six_months",
    "first_rejection", "allowance", "quayle", "grant_date", "patent_number", "filing_date",
    "priority_date", "granted_as", "our_submissions", "file_events", "refreshed_at",
    "refresh_source",
    #  Only ever written by the self-heal below, which fills them in on a row that has none.
    "title", "title_full", "applicant", "applicants", "inventors", "ipc", "family_id",
    "application",
)

#  AND THESE ARE CLEARED WHEN THE SWEEP NO LONGER FINDS THEM. `payload.update(patch)` only ever
#  writes the keys a patch carries, so a flag that was true and has stopped being true simply
#  stays. Four German applications had been refused and were dead, and each of them kept the
#  `closing_soon` the August sweep had set while they were merely wobbling, so the docket put
#  four corpses at the top of the page under "any day". Every field derived purely from a
#  register is set to None here unless this pull found it.
SWEEP_OWNED = (
    "posture", "register_status", "deadline", "deadline_kind", "closing_soon", "closing_note",
    "grant_published", "opposition_deadline", "opposition_opens", "opposition_opens_est",
    "opposition_pending", "scheduled_grant", "decision_on", "refused_on", "lapsed_on",
    "exam_requested", "granted_as", "first_rejection", "allowance", "quayle", "grant_date",
    "patent_number", "our_submissions", "file_events",
)

_JOBS = {}
_JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------

def _iso(value):
    """OPS gives 20260820, ODP gives 2026-08-20, and both mean the same day."""
    s = re.sub(r"\D", "", str(value or ""))
    if len(s) != 8 or s[:4] in ("0000", "0001"):
        return None
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])


def _first(node, *names):
    """Walk OPS's JSON, which nests every value under `$` and repeats a node as a bare dict when
    there is exactly one of it."""
    cur = node
    for n in names:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(n)
    if isinstance(cur, dict) and "$" in cur:
        return cur["$"]
    return cur


def _aslist(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _epodoc(publication):
    """US20260109053A1 -> US20260109053, DE102024133318A1 -> DE102024133318. OPS 404s on a
    number that still carries its kind code, which is the single most common way to waste a call."""
    s = re.sub(r"[^A-Z0-9]", "", str(publication or "").upper())
    return re.sub(r"[A-Z]\d?$", "", s)


def _kind(publication):
    m = re.search(r"([A-Z]\d?)$", re.sub(r"[^A-Z0-9]", "", str(publication or "").upper()))
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------------------------
# EPO OPS
# ---------------------------------------------------------------------------------------------

def _ops_json(path):
    """One OPS call, as JSON. -> (status, dict). Uses the repo's OPS client so the weekly byte
    budget and the throttle header are honoured by this sweep too."""
    import ops
    st, body, _ = ops._ops_get(path, accept="application/json", retries=3)
    if st != 200:
        return st, {}
    try:
        return st, json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return st, {}


def _register_document(payload):
    doc = _first(payload, "ops:world-patent-data", "ops:register-search",
                 "reg:register-documents", "reg:register-document")
    if isinstance(doc, list):
        return doc[0] if doc else None
    return doc


def ep_case(publication):
    """One EP application from the European Patent Register."""
    st, payload = _ops_json("register/publication/epodoc/%s/biblio" % _epodoc(publication))
    if st != 200:
        return {"_error": "EP register HTTP %s" % st}
    doc = _register_document(payload)
    if not doc:
        return {"_error": "EP register returned no document"}
    status = (doc.get("@status") or "").strip()
    out = {"register_status": status, "refresh_source": "EPO OPS register"}

    statuses = _aslist(_first(doc, "reg:ep-patent-statuses", "reg:ep-patent-status"))
    changed = [s.get("@change-date") for s in statuses if isinstance(s, dict) and s.get("@change-date")]
    if changed:
        out["register_updated"] = _iso(max(changed))

    bib = doc.get("reg:bibliographic-data") or {}
    a1 = b1 = None
    for ref in _aslist(bib.get("reg:publication-reference")):
        did = ref.get("reg:document-id") if isinstance(ref, dict) else None
        if not isinstance(did, dict):
            continue
        kind = _first(did, "reg:kind") or ""
        date = _iso(_first(did, "reg:date"))
        num = _first(did, "reg:doc-number")
        if kind.startswith("B"):
            b1 = b1 or (date, "EP%s%s" % (num, kind))
        elif kind.startswith("A"):
            a1 = a1 or date

    low = status.lower()
    if "granted" in low or b1:
        out["posture"] = "granted"
        if b1:
            #  The B1 publication IS the mention of grant in the Bulletin, and it is what the
            #  nine months of Art. 99 run from. Nothing else on the register is that date.
            out["grant_published"] = b1[0]
            out["granted_as"] = b1[1]
            close = acts.plus_months(acts._date(b1[0]), acts.OPPOSITION_MONTHS)
            out["opposition_deadline"] = close.isoformat() if close else None
            out["deadline"] = out["opposition_deadline"]
            out["deadline_kind"] = "hard"
    elif re.search(r"deemed to be withdrawn|refused|withdrawn|revoked|lapsed", low):
        out["posture"] = "lapsed"
        out["deadline"] = None
        out["deadline_kind"] = "none"
    else:
        out["posture"] = "pending"
        out["deadline"] = None
        out["deadline_kind"] = "none"
        if re.search(r"intention to grant|grant of patent is intended", low):
            out["closing_soon"] = True
            when = None
            for s in statuses:
                if isinstance(s, dict) and str(s.get("@status-code")) == "12":
                    when = acts._date(_iso(s.get("@change-date")))
            est = acts.plus_months(when, 5) if when else None
            if est:
                out["opposition_opens_est"] = est.isoformat()
            out["closing_note"] = (
                "The Rule 71(3) communication of intention to grant issued%s. Grant normally "
                "publishes four to five months later%s, so observations filed now are unlikely to "
                "reach the examining division; the Art. 99 opposition is the remedy worth planning."
                % ((" on %s" % when.isoformat()) if when else "",
                   (", around %s" % est.isoformat()) if est else ""))

    if re.search(r"opposition", low) and "no opposition" not in low:
        out["opposition_pending"] = True
    out["pubDate"] = a1
    app = _first(bib, "reg:application-reference", "reg:document-id", "reg:doc-number")
    if app:
        out["register_url"] = "https://register.epo.org/application?number=EP%s" % app
    #  A SECOND CALL, and it earns itself three times over: whether observations are already on
    #  this file (ours or anybody's), whether an opposition is pending, which is what decides
    #  three of the six cells in the instrument table, and it is the only place either shows up.
    out.update(ep_procedural(publication))
    return out


#  Matched on the register's own DESCRIPTION rather than on a step code. The EPO's code for
#  observations by third parties does not appear on any of our own 36 cases, because we have not
#  filed any there yet, so there is nothing to read a code off; the description is served in
#  English on every step and cannot be guessed wrong.
EP_OBSERVATION_STEP = re.compile(r"third part(?:y|ies)|observation", re.I)
EP_OPPOSITION_STEP = re.compile(r"opposition", re.I)


def ep_procedural(publication):
    """Procedural steps for one EP case: what has already been filed against it, by anyone."""
    st, payload = _ops_json("register/publication/epodoc/%s/procedural-steps" % _epodoc(publication))
    if st != 200:
        return {}
    doc = _register_document(payload)
    if not doc:
        return {}
    out, events = {}, []
    for step in _aslist(_first(doc, "reg:procedural-data", "reg:procedural-step")):
        if not isinstance(step, dict):
            continue
        code = _first(step, "reg:procedural-step-code") or ""
        desc = ""
        for txt in _aslist(step.get("reg:procedural-step-text")):
            if isinstance(txt, dict) and txt.get("@step-text-type") == "STEP_DESCRIPTION":
                desc = txt.get("$") or ""
        when = None
        for dt in _aslist(step.get("reg:procedural-step-date")):
            if isinstance(dt, dict):
                when = _iso(_first(dt, "reg:date")) or when
        if EP_OPPOSITION_STEP.search(desc):
            out["opposition_pending"] = True
        if EP_OBSERVATION_STEP.search(desc):
            events.append({
                "date": when or "",
                "instrument": desc.strip() or "Observations by a third party, Art. 115 EPC",
                "documents": 0,
                "fee_paid": False,
                "acknowledged": False,
                #  NEVER "ours". Art. 115 observations may be filed anonymously and the register
                #  does not say whose they are, so the page must not claim them.
                "whose": "unknown",
                "evidence": "European Patent Register, procedural step %s." % (code or desc),
            })
    if events:
        out["file_events"] = sorted(events, key=lambda e: e["date"], reverse=True)
    return out


def biblio_for(publication):
    """Title, applicant and application number for a publication the docket has never seen.

    A newly discovered case arrived with its own number as its title, which makes it unreadable
    in a list of a hundred. One published-data call fixes that for every office at once.
    """
    st, payload = _ops_json("published-data/publication/epodoc/%s/biblio" % _epodoc(publication))
    if st != 200:
        return {}
    ex = _first(payload, "ops:world-patent-data", "exchange-documents", "exchange-document")
    ex = (ex[0] if isinstance(ex, list) else ex) or {}
    bib = ex.get("bibliographic-data") or {}
    out = {}
    #  The title comes in every procedural language. English if it is there, else whatever is.
    titles = {t.get("@lang"): t.get("$") for t in _aslist(bib.get("invention-title"))
              if isinstance(t, dict)}
    title = titles.get("en") or next(iter(titles.values()), "")
    if title:
        out["title_full"] = title
        out["title"] = title if len(title) < 96 else title[:93] + "..."
    #  EVERY party, not only the first. Whether a discovered publication really belongs to a
    #  target is decided against these lists: a co-applicant's case names the target second, and
    #  an inventor target is matched on the inventors, which the first applicant says nothing
    #  about. The "original" spelling is the one a person typed; the epodoc one is upper-cased
    #  and reordered ("SCHMALZ J GMBH [DE]") and matches nothing anyone would enter.
    applicants = [n for n in (_first(a, "applicant-name", "name")
                              for a in _aslist(_first(bib, "parties", "applicants", "applicant"))
                              if isinstance(a, dict) and a.get("@data-format") == "original")
                  if n]
    if applicants:
        out["applicant"] = applicants[0]
        out["applicants"] = applicants
    inventors = [n for n in (_first(i, "inventor-name", "name")
                             for i in _aslist(_first(bib, "parties", "inventors", "inventor"))
                             if isinstance(i, dict) and i.get("@data-format") == "original")
                 if n]
    if inventors:
        out["inventors"] = inventors
    ipc = _aslist(_first(bib, "classifications-ipcr", "classification-ipcr"))
    ipc_text = _first(ipc[0], "text") if ipc and isinstance(ipc[0], dict) else None
    m = re.match(r"\s*([A-H]\d\d[A-Z])\s*(\d+)\s*/\s*(\d+)", str(ipc_text or ""))
    if m:
        out["ipc"] = "%s %s/%s" % (m.group(1), m.group(2), m.group(3))
    for did in _aslist(_first(bib, "application-reference", "document-id")):
        if isinstance(did, dict) and did.get("@document-id-type") == "docdb":
            out["application"] = "%s%s" % (_first(did, "country") or "",
                                           _first(did, "doc-number") or "")
    if ex.get("@family-id"):
        out["family_id"] = ex["@family-id"]
    for ref in _aslist(_first(bib, "publication-reference", "document-id")):
        if isinstance(ref, dict) and ref.get("@document-id-type") == "docdb":
            out["pubDate"] = _iso(_first(ref, "date")) or out.get("pubDate")
    priorities = []
    for claim in _aslist(_first(bib, "priority-claims", "priority-claim")):
        for did in _aslist(claim.get("document-id") if isinstance(claim, dict) else None):
            if isinstance(did, dict) and did.get("@document-id-type") == "docdb":
                when = _iso(_first(did, "date"))
                if when:
                    priorities.append(when)
    if priorities:
        out["priority_date"] = min(priorities)
    return out


def pct_case(row):
    """A PCT publication. There is no register to read, but the one window it has is arithmetic:
    28 months from the earliest priority date, which the bibliographic record carries."""
    facts = biblio_for(row.get("publication") or "")
    out = {k: v for k, v in facts.items() if k in ("title", "title_full", "applicant",
                                                   "application", "family_id", "pubDate",
                                                   "priority_date")}
    out["posture"] = "pending"
    out["register_status"] = "Published PCT application"
    priority = acts._date(out.get("priority_date"))
    close = acts.plus_months(priority, PCT_OBSERVATION_MONTHS) if priority else None
    out["deadline"] = close.isoformat() if close else None
    out["deadline_kind"] = "hard" if close else "none"
    out["refresh_source"] = "EPO OPS published data"
    return out


#  INPADOC codes that matter to a German case. R012 is the answer to "can we still force
#  examination"; R018 says a grant decision is made and the Patentschrift is coming; R002 and
#  R003 are a refusal and a refusal that has become final, and the difference between the two is
#  the difference between a case still worth watching and a dead one.
DE_EXAM_REQUESTED = ("R012",)
DE_GRANT_DECISION = ("R018",)
DE_REFUSED = ("R002",)
DE_REFUSED_FINAL = ("R003",)
#  Named phrases, not a word list. "withdraw" alone also matches a withdrawn request for a change
#  of representative, which would have killed four live applications on a clerical entry.
DE_DEAD_PHRASE = re.compile(
    r"deemed to be withdrawn|application (?:is )?withdrawn|lapse of the patent|"
    r"patent (?:has )?ceased|non-?payment of (?:the )?renewal", re.I)
DE_OPPOSITION = re.compile(r"opposition", re.I)


def de_case(publication):
    """One German national case from the INPADOC legal file. No browser, no DPMAregister."""
    epodoc = _epodoc(publication)
    st, payload = _ops_json("legal/publication/epodoc/%s/" % epodoc)
    if st != 200:
        return {"_error": "INPADOC legal HTTP %s" % st}
    fam = _first(payload, "ops:world-patent-data", "ops:patent-family") or {}
    members = _aslist(fam.get("ops:family-member"))
    out = {"refresh_source": "EPO OPS INPADOC legal",
           "register_url": "https://register.dpma.de/DPMAregister/pat/experte"}
    codes, events, grant_pub, grant_kind, appl_pub = set(), [], None, None, None

    for m in members:
        docnum = kind = date = None
        for did in _aslist(_first(m, "publication-reference", "document-id")):
            if not isinstance(did, dict) or did.get("@document-id-type") != "docdb":
                continue
            docnum = "%s%s" % (_first(did, "country") or "", _first(did, "doc-number") or "")
            kind = _first(did, "kind") or ""
            date = _iso(_first(did, "date"))
        #  Only this application's own publications. A family member in another country has its
        #  own life and its own windows and must not move this row.
        if not docnum or docnum != epodoc:
            continue
        if kind.startswith("B") and date:
            if grant_pub is None or date < grant_pub:
                grant_pub, grant_kind = date, kind
        elif kind.startswith("A") and date:
            appl_pub = appl_pub or date
        for leg in _aslist(m.get("ops:legal")):
            if not isinstance(leg, dict):
                continue
            code = (leg.get("@code") or "").strip()
            desc = (leg.get("@desc") or "").strip()
            codes.add(code)
            when = _iso(_first(leg, "ops:L007EP") or _first(leg, "ops:L525EP"))
            if not when:
                m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(_first(leg, "ops:pre") or ""))
                when = m2.group(0) if m2 else None
            events.append({"code": code, "desc": desc, "date": when})

    events.sort(key=lambda e: e.get("date") or "")
    out["register_events"] = events[-12:]
    if events:
        out["register_updated"] = max((e["date"] for e in events if e.get("date")), default=None)
    out["pubDate"] = appl_pub
    out["exam_requested"] = True if codes & set(DE_EXAM_REQUESTED) else (False if events else None)

    if grant_pub:
        out["posture"] = "granted"
        out["register_status"] = "Patent erteilt (Patentschrift %s, %s)" % (grant_kind, grant_pub)
        out["grant_published"] = grant_pub
        out["granted_as"] = "%s%s" % (epodoc, grant_kind)
        close = acts.plus_months(acts._date(grant_pub), acts.OPPOSITION_MONTHS)
        out["opposition_deadline"] = close.isoformat() if close else None
        out["deadline"] = out["opposition_deadline"]
        out["deadline_kind"] = "hard"
        out["closing_note"] = (
            "The Patentschrift published on %s, so Sec. 43(3) is spent and the Einspruch window "
            "under Sec. 59 runs to %s." % (grant_pub, out["opposition_deadline"]))
    elif codes & set(DE_GRANT_DECISION):
        decision = next((e["date"] for e in reversed(events)
                         if e["code"] in DE_GRANT_DECISION and e.get("date")), None)
        out["posture"] = "pending"
        out["register_status"] = "Erteilungsbeschluss ergangen"
        out["decision_on"] = decision
        est = acts.plus_months(acts._date(decision), 3) if decision else None
        out["deadline"] = est.isoformat() if est else None
        out["deadline_kind"] = "practical"
        out["closing_soon"] = True
        out["closing_note"] = (
            "A grant decision is on the register%s with no publication date set yet. The "
            "Patentschrift normally follows about three months later and Sec. 43(3) closes with "
            "it; the Sec. 59 opposition window opens on that publication. The date shown is an "
            "estimate, not a register date." % ((" dated %s" % decision) if decision else ""))
    elif codes & set(DE_REFUSED_FINAL):
        when = _last_date(events, DE_REFUSED_FINAL)
        out["posture"] = "lapsed"
        out["register_status"] = "zurückgewiesen, rechtskräftig%s" % ((" seit %s" % when) if when else "")
        out["refused_on"] = _last_date(events, DE_REFUSED) or when
        out["deadline"] = None
        out["deadline_kind"] = "none"
        out["closing_note"] = (
            "The examining section refused the application%s and the refusal became final%s. "
            "There is nothing left to file against: no examination is running, so § 43(3) has no "
            "reader, and no patent exists, so § 59 has no target."
            % ((" on %s" % out["refused_on"]) if out["refused_on"] else "",
               (" on %s" % when) if when else ""))
    elif codes & set(DE_REFUSED):
        when = _last_date(events, DE_REFUSED)
        out["posture"] = "pending"
        out["register_status"] = "zurückgewiesen%s, noch nicht rechtskräftig" % ((" am %s" % when) if when else "")
        out["refused_on"] = when
        out["deadline"] = None
        out["deadline_kind"] = "none"
        out["closing_soon"] = True
        out["closing_note"] = (
            "Refused by the examining section%s, and the refusal is not yet final on the "
            "register. DPMAregister keeps the status pending while the appeal period runs or a "
            "Beschwerde is on foot, which is why databases still call it live. Only worth an "
            "Einwendung if an appeal turns out to be running."
            % ((" on %s" % when) if when else ""))
    elif any(DE_DEAD_PHRASE.search(e.get("desc") or "") for e in events[-4:]):
        out["posture"] = "lapsed"
        out["register_status"] = "nicht mehr in Kraft"
        out["deadline"] = None
        out["deadline_kind"] = "none"
    else:
        out["posture"] = "pending"
        out["register_status"] = ("anhängig (INPADOC: kein Erteilungsbeschluss)" if events
                                  else "anhängig (kein INPADOC-Ereignis auf der Akte)")
        out["deadline"] = None
        out["deadline_kind"] = "none"

    if any(DE_OPPOSITION.search(e.get("desc") or "") for e in events):
        out["opposition_pending"] = True
    return out


def _last_date(events, codes):
    """The most recent date carried by any of these event codes."""
    return next((e["date"] for e in reversed(events)
                 if e.get("code") in codes and e.get("date")), None)


# ---------------------------------------------------------------------------------------------
# USPTO ODP
# ---------------------------------------------------------------------------------------------

ODP_BASE = os.environ.get("USPTO_ODP_BASE", "https://api.uspto.gov/api/v1").rstrip("/")
ODP_TIMEOUT = float(os.environ.get("USPTO_ODP_TIMEOUT", "45"))
ODP_TRIES = int(os.environ.get("USPTO_ODP_TRIES", "5"))
#  THE OPEN DATA PORTAL RATE-LIMITS, HARD. Six workers against it returned 429 on sixteen of
#  sixty calls, measured 2026-09-04. Two at a time is comfortably under it and costs nothing:
#  there are ten US cases on this docket and the European half runs in parallel anyway.
_ODP_GATE = threading.Semaphore(int(os.environ.get("USPTO_ODP_CONCURRENCY", "2")))
#  Answers that mean "ask again", as opposed to "there is no such thing".
ODP_RETRY_CODES = (408, 425, 429, 500, 502, 503, 504)


class OdpUnavailable(RuntimeError):
    """The office could not be asked. NOT the same as the office answering "nothing"."""


def _odp(path, body=None):
    """GET or POST against the Open Data Portal. -> dict for an answer, {} for a real 404.

    Raises OdpUnavailable when it could not be asked, and that distinction is the whole point.
    This used to return {} for every failure below 500, so a 429 rate-limit answer read as "this
    application does not exist"; on the /documents call that means "nothing has been filed on
    this file", which would have quietly erased the record of our own submission. An empty answer
    and an unanswered question must never be the same value.

    Deliberately NOT `family_dossier._call`, which is otherwise the same request: that module is
    gated by FAMILY_DOSSIER_ENABLED, and a flag turned off for the drafting pipeline would take
    the American half of this docket down without a word in the log. The key is read at call time
    for the same reason the dossier does: a module constant binds before config loads .env.
    """
    key = os.environ.get("USPTO_ODP_KEY", "") or os.environ.get("ODP_API_KEY", "")
    if not key:
        raise OdpUnavailable("no USPTO_ODP_KEY in the environment")
    data = json.dumps(body).encode() if body is not None else None
    last = "no attempt made"
    for attempt in range(ODP_TRIES):
        req = urllib.request.Request(
            "%s/%s" % (ODP_BASE, path.lstrip("/")), data=data,
            headers={"X-API-KEY": key, "Accept": "application/json",
                     **({"Content-Type": "application/json"} if data else {})})
        try:
            with _ODP_GATE:
                with urllib.request.urlopen(req, timeout=ODP_TIMEOUT) as fh:
                    return json.loads(fh.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            last = "HTTP %s" % exc.code
            if exc.code == 404:
                return {}                     # a real answer: no such application
            if exc.code not in ODP_RETRY_CODES:
                raise OdpUnavailable(last)
            wait = 0.0
            try:
                wait = float(exc.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0.0
            time.sleep(max(wait, 1.5 * (attempt + 1)))
            continue
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, str(exc)[:60])
        time.sleep(1.5 * (attempt + 1))
    raise OdpUnavailable(last)


def us_case(application):
    """One US application: where it stands, when 1.290 shuts, and what WE have already filed."""
    app = re.sub(r"\D", "", str(application or ""))
    if not app:
        return {"_error": "no application number"}
    try:
        payload = _odp("patent/applications/%s" % app)
    except OdpUnavailable as exc:
        #  Reported, and no patch written, so the row keeps whatever the last good sweep said.
        return {"_error": "USPTO ODP unreachable (%s)" % exc}
    bag = (payload or {}).get("patentFileWrapperDataBag") or []
    if not bag:
        return {"_error": "ODP has no wrapper for %s" % app}
    wrapper = bag[0]
    md = wrapper.get("applicationMetaData") or {}
    out = {
        "refresh_source": "USPTO ODP",
        "register_status": md.get("applicationStatusDescriptionText") or "",
        "register_updated": md.get("applicationStatusDate") or None,
        "register_url": "https://patentcenter.uspto.gov/applications/%s" % app,
        "pubDate": md.get("earliestPublicationDate") or None,
        "filing_date": md.get("filingDate") or None,
        "patent_number": md.get("patentNumber") or None,
        "grant_date": md.get("grantDate") or None,
    }
    status = out["register_status"]
    events = []
    for w in ([wrapper] + (payload.get("patentFileWrapperDataBag") or [])[1:]):
        for e in (w.get("eventDataBag") or []):
            if e.get("eventDate"):
                events.append((e["eventDate"][:10], e.get("eventCode") or "",
                               e.get("eventDescriptionText") or ""))
    events.sort()

    def first_of(*codes):
        for when, code, _ in events:
            if code in codes:
                return when
        return None

    out["first_rejection"] = first_of(*USPTO_REJECTIONS)
    out["allowance"] = first_of(*USPTO_ALLOWANCE)
    out["quayle"] = first_of(*USPTO_QUAYLE)

    if out["pubDate"]:
        six = acts.plus_months(acts._date(out["pubDate"]), 6)
        out["six_months"] = six.isoformat() if six else None

    if out["patent_number"] or re.search(r"patented case|issue notification", status, re.I):
        out["posture"] = "granted"
        out["deadline"] = None
        out["deadline_kind"] = "none"
        if out["grant_date"]:
            close = acts.plus_months(acts._date(out["grant_date"]), acts.OPPOSITION_MONTHS)
            out["deadline"] = close.isoformat() if close else None
            out["deadline_kind"] = "hard"
    elif re.search(r"abandon|expired|withdrawn", status, re.I):
        out["posture"] = "lapsed"
        out["deadline"] = None
        out["deadline_kind"] = "none"
    else:
        out["posture"] = "pending"
        if out["allowance"]:
            out["deadline"] = out["allowance"]
            out["deadline_kind"] = "closed"
            out["closing_note"] = "Notice of allowance mailed. The 1.290 window is closed."
        elif out["first_rejection"]:
            six = out.get("six_months")
            out["deadline"] = max(six, out["first_rejection"]) if six else out["first_rejection"]
            out["deadline_kind"] = "hard"
        else:
            out["deadline"] = out.get("six_months")
            out["deadline_kind"] = "open_ended"
            #  A SIX-MONTH DATE IN THE PAST IS NOT A CLOSED WINDOW. 1.290(b) shuts on the LATER
            #  of that date and the first rejection, so with no rejection on file the window is
            #  live and simply has no date yet. Leaving the passed date in `deadline` made the
            #  docket print "closed" beside an instrument the same page reported as open, which
            #  is the exact failure this docket exists to prevent. No date, and flagged urgent,
            #  because it now shuts the moment the examiner does anything at all.
            if out["deadline"] and out["deadline"] < datetime.date.today().isoformat():
                out["deadline"] = None
                out["closing_soon"] = True
                out["closing_note"] = (
                    "The six-month date of %s has passed with no rejection on file, so the 1.290 "
                    "window is still open and has no date left to count to. It shuts the day a "
                    "rejection or a notice of allowance is mailed."
                    % (out.get("six_months") or "the publication"))
        if out["quayle"] and not out["first_rejection"]:
            out["closing_soon"] = True
            out["deadline_kind"] = "open_ended"
            out["closing_note"] = (
                "An Ex parte Quayle action issued on %s: prosecution on the merits is closed and "
                "the claims are already indicated allowable, but a Quayle action is not a "
                "rejection under 1.290(b)(2)(ii), so the window is arguably open. Treat it as "
                "expiring any day." % out["quayle"])

    try:
        out["our_submissions"] = us_submissions(app, events)
    except OdpUnavailable as exc:
        #  The file wrapper is the ONLY evidence of what we have filed. If it cannot be read,
        #  the honest patch is no patch: an empty list here would say "nothing was ever filed".
        return {"_error": "USPTO file wrapper unreachable (%s)" % exc}
    return out


def us_submissions(app, events=None):
    """What the office's own file wrapper says WE filed. Correspondence is not evidence.

    Grouped by the day the submission was filed, because one submission is one IDS.3P plus one
    3P.RELEVANCE per document plus a fee payment, and counting documents is how you check that
    all ten references actually landed.
    """
    payload = _odp("patent/applications/%s/documents" % app)
    docs = (payload or {}).get("documentBag") or (payload or {}).get("documents") or []
    by_day = {}
    for d in docs:
        code = str(d.get("documentCode") or "").upper()
        if code not in US_SUBMISSION_DOCS and code != "N417.PYMT":
            continue
        when = str(d.get("officialDate") or d.get("mailRoomDate") or "")[:10]
        if not when:
            continue
        slot = by_day.setdefault(when, {"date": when, "codes": {}})
        slot["codes"][code] = slot["codes"].get(code, 0) + 1
    out = []
    for when in sorted(by_day, reverse=True):
        codes = by_day[when]["codes"]
        if "IDS.3P" not in codes and "3P.RELEVANCE" not in codes:
            continue          # a lone fee payment on some other day is not a submission
        #  NOT A REFERENCE COUNT. The office files each concise description TWICE, once as filed
        #  and once as its own scan, so a ten-reference submission shows up here as twenty-one
        #  documents. Deduping properly means reading the "Document N:" number out of each PDF,
        #  which is not worth doing on a page load, so the raw count is reported as what it is.
        n_rel = codes.get("3P.RELEVANCE", 0)
        out.append({
            "date": when,
            "instrument": "Third-party submission, 37 CFR 1.290",
            "documents": n_rel,
            "references_about": max(1, n_rel // 2) if n_rel > 1 else n_rel,
            "fee_paid": bool(codes.get("N417.PYMT")),
            "acknowledged": bool(codes.get("M327")),
            "evidence": ("USPTO file wrapper: "
                         + ", ".join("%s x%d" % (c, n) for c, n in sorted(codes.items()))
                         + (". The office stores each concise description twice, as filed and as "
                            "its own scan, so the reference count is about half of that."
                            if n_rel > 1 else "")),
        })
    if out:
        return out
    #  ONLY when the wrapper shows nothing. The transaction history logs a submission three times
    #  over (filed, mailed, communicated), so reading both sources always reports one filing as
    #  three, and the wrapper is the better of the two: it counts the documents.
    for when, code, text in sorted(events or [], reverse=True):
        if US_SUBMISSION_EVENT.search(text):
            out.append({"date": when, "instrument": text.strip(), "documents": 0,
                        "fee_paid": False, "acknowledged": False,
                        "evidence": "USPTO transaction history: %s" % code})
    return out


# ---------------------------------------------------------------------------------------------
# discovery: what this company has filed since the last sweep
# ---------------------------------------------------------------------------------------------

def normalise_applicant(raw):
    """One applicant string as a name an office will match on, or "".

    The registers do not agree on what an applicant name is. The USPTO gives "J. Schmalz GmbH";
    the EP register gives the name with its full postal address appended, and where there is a
    co-applicant it gives BOTH names and both addresses in one string. Searching any of that
    verbatim returns either nothing or, in the case of "technische universitat munchen", a
    hundred and fifty unrelated applications. So: drop the corporate furniture, cut at the first
    token carrying a digit, which is always the start of an address, and keep at most the first
    three words, which is where a company's distinctive part lives.
    """
    name = _APPLICANT_NOISE.sub(" ", raw or "")
    name = re.sub(r"[^\w\s-]", " ", name, flags=re.UNICODE).lower()
    words = []
    for word in name.split():
        if any(ch.isdigit() for ch in word):
            break
        words.append(word)
        if len(words) >= 3:
            break
    name = " ".join(words).strip()
    return name if len(name) >= 3 else ""


def _applicant_filter(applicants):
    """A test for "is this publication really theirs", from the docket's own names."""
    words = sorted({query_word(n) for n in applicants} - {""})
    if not words:
        return re.compile(r"(?!)")            # matches nothing
    return re.compile("|".join(re.escape(w) for w in words), re.I)


def query_word(name):
    """The one token an office index is worth searching on: the first substantive word.

    A phrase search misses "J.Schmalz GmbH" for want of a space, and the LONGEST word of
    "schmalz flexible gripping" is "gripping", which is the trade and not the company.
    """
    for word in (name or "").split():
        if len(word) >= 3:
            return word
    return ""


def applicants_of(rows):
    """The names this docket is actually about. Most common first, at most four."""
    counts = {}
    for row in rows:
        #  Normalise before counting, or "J. SCHMALZ GMBH", "J. Schmalz GmbH" and
        #  "J.Schmalz GmbH" are three applicants with one row each and none of them clears
        #  the threshold.
        name = normalise_applicant(row.get("applicant") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, n in ranked[:MAX_APPLICANT_QUERIES] if n >= MIN_ROWS_PER_APPLICANT]


def discover_ops(since, applicants):
    """Publications by these applicants in a date window, from OPS. -> [publication numbers]"""
    found = []
    window = '%s %s' % (since.strftime("%Y%m%d"), datetime.date.today().strftime("%Y%m%d"))
    #  The distinctive WORD, not the whole name, and for the same reason the ODP half uses it:
    #  a subsidiary filing under its own name ("Schmalz Flexible Gripping") appears on too few
    #  rows to become a search term of its own, and the parent's full name will not find it.
    #  Measured over the last year on the live docket: `pa="j schmalz"` returns 27 publications,
    #  `pa="schmalz"` returns 28 and every one of the 27.
    for word in sorted({query_word(name) for name in applicants} - {""}):
        q = 'pa="%s" and pd within "%s"' % (word, window)
        st, payload = _ops_json("published-data/search?q=%s&Range=1-100"
                                % urllib.parse.quote(q))
        if st != 200:
            continue
        res = _first(payload, "ops:world-patent-data", "ops:biblio-search", "ops:search-result",
                     "ops:publication-reference")
        for x in _aslist(res):
            did = x.get("document-id") if isinstance(x, dict) else None
            if not isinstance(did, dict):
                continue
            num = "%s%s%s" % (_first(did, "country") or "", _first(did, "doc-number") or "",
                              _first(did, "kind") or "")
            if num and num not in found:
                found.append(num)
    return found


def discover_odp(applicants):
    """US applications by these applicants. -> [{application, publication, ...}]"""
    out = []
    if not applicants:
        return out
    words = sorted({query_word(name) for name in applicants} - {""})
    if not words:
        return out
    keep = re.compile("|".join(re.escape(w) for w in words), re.I)
    seen = set()
    for word in words:
        payload = _odp("patent/applications/search",
                       {"q": 'applicationMetaData.firstApplicantName:"%s"' % word,
                        "pagination": {"offset": 0, "limit": 100}})
        out.extend(_odp_rows(payload, keep, seen))
    return out


def _odp_rows(payload, keep, seen):
    out = []
    for w in (payload or {}).get("patentFileWrapperDataBag") or []:
        md = w.get("applicationMetaData") or {}
        names = " ".join([md.get("firstApplicantName") or ""] +
                         [a.get("applicantNameText") or "" for a in (md.get("applicantBag") or [])])
        if not keep.search(names):
            continue
        if (w.get("applicationNumberText") or "") in seen:
            continue
        seen.add(w.get("applicationNumberText") or "")
        out.append(_odp_hit(w))
    return out


def _odp_hit(wrapper):
    """One search hit as the row-to-be needs it. No default applicant: a row labelled with a
    name the office did not give it is worse than an unlabelled one."""
    md = wrapper.get("applicationMetaData") or {}
    return {
        "application": re.sub(r"\D", "", str(wrapper.get("applicationNumberText") or "")),
        "publication": md.get("earliestPublicationNumber") or "",
        "title": md.get("inventionTitle") or "",
        "applicant": md.get("firstApplicantName") or "",
        "applicants": [n for n in ([md.get("firstApplicantName") or ""]
                                   + [a.get("applicantNameText") or ""
                                      for a in (md.get("applicantBag") or [])]) if n],
        "inventors": [i.get("inventorNameText") or "" for i in (md.get("inventorBag") or [])
                      if i.get("inventorNameText")],
        "status": md.get("applicationStatusDescriptionText") or "",
        "filed": md.get("filingDate") or "",
        "filing_date": md.get("filingDate") or "",
        "pubDate": md.get("earliestPublicationDate") or "",
        "grant_date": md.get("grantDate") or "",
        "patent_number": md.get("patentNumber") or "",
    }


# ---------------------------------------------------------------------------------------------
# discovery for a named target: assignees and inventors, at the offices it tracks
# ---------------------------------------------------------------------------------------------

#  How many publications one search is allowed to return for one target. OPS pages by 100 and
#  ODP by 100; three pages is more than a year of a large company's output and keeps a mistyped
#  name ("GmbH") from pulling ten thousand rows into somebody's docket.
MAX_DISCOVERY = 300
#  Utility models and corrected reprints are not applications to file against: a Gebrauchsmuster
#  is never examined and has no opposition, and an A8 is the A1 with a typo fixed.
DISCOVERY_KINDS = re.compile(r"^[AB][1-7]?$")


def name_words(name):
    """The words of a name worth searching an office index on, lower-cased, corporate furniture
    and address gone, anything under three letters gone. "J. Schmalz GmbH" -> ["schmalz"],
    "Stockburger, Ralf" -> ["stockburger", "ralf"], "Vacuum Technologies Inc" ->
    ["vacuum", "technologies"]. Searched AND-ed, so every word has to be on the record."""
    return [w for w in normalise_applicant(name).split() if len(w) >= 3]


def _plain(text):
    return re.sub(r"[^\w\s]", " ", str(text or "").lower(), flags=re.UNICODE)


def name_matches(words_list, candidates):
    """True when any searched name has every one of its words, as WHOLE words, in one candidate.

    Whole words, because a substring is how "Schmalz" came to own SchmalzTech, LLC's trademarks
    for a moment. "J.Schmalz GmbH" still matches: punctuation is turned to spaces first.
    """
    cands = [" %s " % " ".join(_plain(c).split()) for c in (candidates or []) if c]
    for words in words_list or []:
        if not words:
            continue
        if any(all((" %s " % w) in c for w in words) for c in cands):
            return True
    return False


def _months_ago(today, months):
    y, m = today.year, today.month - int(months)
    while m <= 0:
        y, m = y - 1, m + 12
    return datetime.date(y, m, 1)


def target_words(target):
    """-> (assignee word lists, inventor word lists), empty names dropped."""
    a = [w for w in (name_words(n) for n in (target.get("assignees") or [])) if w]
    i = [w for w in (name_words(n) for n in (target.get("inventors") or [])) if w]
    return a, i


def discover_target_ops(target, known, since, today):
    """EP, DE and WO publications by this target in the window, from OPS, each checked against
    its own bibliographic record before it is accepted. -> ([row], [str rejected])

    One query for all the names, OR-ed, because OPS charges per call and a target is a handful
    of names. Each name is its words AND-ed: `pa="vacuum" and pa="technologies"` finds the
    company however the office spelled the rest; a phrase search misses "J.Schmalz GmbH" for
    want of a space. A B publication of a case whose A is also in the window is the same case,
    so hits are folded by document number and the row is keyed by the earliest kind.
    """
    a_words, i_words = target_words(target)
    offices = [o for o in (target.get("offices") or TRACKED_COUNTRIES)
               if o in TRACKED_COUNTRIES and o != "US"]
    if not offices or not (a_words or i_words):
        return [], []
    clauses = (['(%s)' % " and ".join('pa="%s"' % w for w in ws) for ws in a_words]
               + ['(%s)' % " and ".join('in="%s"' % w for w in ws) for ws in i_words])
    q = '(%s) and pd within "%s %s" and (%s)' % (
        " or ".join(clauses), since.strftime("%Y%m%d"), today.strftime("%Y%m%d"),
        " or ".join("pn=%s" % o for o in offices))
    by_doc = {}
    for start in range(1, MAX_DISCOVERY + 1, 100):
        st, payload = _ops_json("published-data/search?q=%s&Range=%d-%d"
                                % (urllib.parse.quote(q), start, start + 99))
        if st != 200:
            break
        res = _aslist(_first(payload, "ops:world-patent-data", "ops:biblio-search",
                             "ops:search-result", "ops:publication-reference"))
        for x in res:
            did = x.get("document-id") if isinstance(x, dict) else None
            if not isinstance(did, dict):
                continue
            cc, num = _first(did, "country") or "", _first(did, "doc-number") or ""
            kind = _first(did, "kind") or ""
            if not cc or not num or not DISCOVERY_KINDS.match(kind):
                continue
            doc = "%s%s" % (cc, num)
            if doc not in by_doc or kind < _kind(by_doc[doc]):
                by_doc[doc] = "%s%s" % (doc, kind)
        if len(res) < 100:
            break
    new, rejected = [], []
    for doc, pub in by_doc.items():
        if pub.upper() in known or doc.upper() in known:
            continue
        office = _office_of(pub)
        #  US publications come from the ODP sweep instead. OPS serves them in the DOCDB spelling
        #  (US2025332743A1, one digit short of the office's own US20250332743A1) and without an
        #  application number, which is the only key the file wrapper answers to.
        if not office or office == "USPTO":
            continue
        facts = {}
        try:
            facts = biblio_for(pub)
        except Exception:
            pass
        applicants = facts.get("applicants") or ([facts["applicant"]] if facts.get("applicant") else [])
        if not (name_matches(a_words, applicants) or name_matches(i_words, facts.get("inventors"))):
            rejected.append("%s (%s)" % (pub, facts.get("applicant") or "no applicant on the record"))
            continue
        new.append(_new_row(pub, office, facts))
        known.add(pub.upper())
        known.add(doc.upper())
    return new, rejected


def discover_target_odp(target, known, since, today):
    """US applications by this target published in the window, from the Open Data Portal.
    -> ([row], [str rejected]). Raises OdpUnavailable when the office could not be asked."""
    a_words, i_words = target_words(target)
    if "US" not in (target.get("offices") or TRACKED_COUNTRIES) or not (a_words or i_words):
        return [], []
    queries = ([" AND ".join('applicationMetaData.applicantBag.applicantNameText:%s' % w
                             for w in ws) for ws in a_words]
               + [" AND ".join('applicationMetaData.inventorBag.inventorNameText:%s' % w
                               for w in ws) for ws in i_words])
    new, rejected, seen = [], [], set()
    for q in queries:
        for offset in range(0, MAX_DISCOVERY, 100):
            payload = _odp("patent/applications/search", {
                "q": q, "pagination": {"offset": offset, "limit": 100},
                "rangeFilters": [{"field": "applicationMetaData.earliestPublicationDate",
                                  "valueFrom": since.isoformat(),
                                  "valueTo": today.isoformat()}]})
            bag = (payload or {}).get("patentFileWrapperDataBag") or []
            for w in bag:
                hit = _odp_hit(w)
                app = hit["application"]
                if not app or app in seen or app in known:
                    continue
                seen.add(app)
                if not (name_matches(a_words, hit["applicants"])
                        or name_matches(i_words, hit["inventors"])):
                    rejected.append("%s (%s)" % (hit["publication"] or app,
                                                 hit["applicant"] or "no applicant on the record"))
                    continue
                #  Abandoned is over. Patented is NOT skipped here, unlike the docket's own
                #  refresh: a patent that issued inside the window is inside its post-grant
                #  review window, and that is the most expensive door on the page.
                if re.search(r"abandon|expired", hit["status"], re.I):
                    continue
                pub = hit["publication"]
                if not pub or pub.upper() in known:
                    continue
                new.append(_new_row(pub, "USPTO", hit))
                known.add(pub.upper())
                known.add(app)
            if len(bag) < 100:
                break
    return new, rejected


#  PCT Rule 114: observations may be filed until 28 months from the priority date.
PCT_OBSERVATION_MONTHS = 28


def _new_row(publication, office, extra=None):
    row = {
        "publication": publication,
        "office": office,
        "title": (extra or {}).get("title") or publication,
        "title_full": (extra or {}).get("title") or "",
        #  NOT a default of the applicant this docket happens to be about. A discovered row wears
        #  whatever name the register gives it, and if that is nobody's, it says nobody's: a row
        #  labelled with a competitor it does not belong to is worse than an unlabelled one.
        "applicant": (extra or {}).get("applicant") or "",
        "application": (extra or {}).get("application") or "",
        "baseline_route": {"EPO": "epo_obs", "DPMA": "dpma_obs", "USPTO": "us_tps",
                           "WIPO (PCT)": "pct_obs"}.get(office, ""),
        "baseline_route_label": {
            "EPO": "Third-party observations, Art. 115 EPC (no deadline while pending)",
            "DPMA": "Third-party observations, §43(3) PatG (no deadline while pending)",
            "USPTO": "Preissuance submission, 37 CFR 1.290",
            "WIPO (PCT)": "Third-party observation via ePCT (until 28 months from priority)",
        }.get(office, ""),
        "google": "https://patents.google.com/patent/%s/en" % publication,
        "family": [],
        "filings": [],
        "filed": False,
        "new_since_baseline": True,
        "found_by": "Live refresh, %s." % datetime.date.today().isoformat(),
        "why_new": "Found by the live refresh; it was not on the docket before.",
    }
    #  What the search already knows about the case, so a row is readable before its first
    #  register read and the panel can say who invented it and where it is classified.
    for key in ("applicants", "inventors", "ipc", "pubDate", "filing_date", "priority_date",
                "grant_date", "patent_number", "family_id"):
        value = (extra or {}).get(key)
        if value:
            row[key] = value
    return row


# ---------------------------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------------------------

def _office_of(publication):
    cc = re.sub(r"[^A-Z]", "", str(publication or "").upper()[:2])
    return OFFICE_OF.get(cc)


def refresh_case(row):
    """One row, refetched from whichever office owns it. Never raises."""
    office = (row.get("office") or "").upper()
    pub = row.get("publication") or ""
    try:
        if office == "USPTO":
            return us_case(row.get("application") or "")
        if office == "EPO":
            return ep_case(pub)
        if office == "DPMA":
            return de_case(pub)
        if office.startswith("WIPO"):
            return pct_case(row)
    except Exception as exc:
        return {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    return {"_skipped": "no live source for %s" % (office or "an unknown office")}


def sweep(rows, progress=None, workers=6, discover=True, target=None):
    """Refetch every row, then look for cases the docket has never seen.

    With a `target`, discovery searches that target's own assignee and inventor names at the
    offices it tracks, over its own lookback window; a target with no rows yet is exactly a
    first sweep. Without one it falls back to the names the rows themselves carry, which is the
    path the shipped docket used before targets existed.

    -> {"patches": {publication: patch}, "new": [row], "errors": [...], "sources": {...},
        "changes": [str]}
    """
    today = datetime.date.today()
    patches, errors, changes = {}, [], []
    done = [0]
    #  A list, because discovery ADDS to the work: every case it finds is then read from its
    #  register, and a first sweep of a new target is nothing but that.
    total = [len(rows) + (2 if discover else 0)]

    def tick(label):
        done[0] += 1
        if progress:
            progress(done[0], total[0], label)

    def one(row):
        patch = refresh_case(row)
        #  SELF-HEAL A ROW WITH NO NAME. The first discovery sweep added a case carrying its own
        #  publication number as its title, and nothing would ever have replaced it, because a
        #  title is not a register fact and the merge quite rightly refuses to overwrite one.
        #  Filling in an ABSENT title is a different thing from overwriting a curated one.
        #  Also when the APPLICATION number is missing, which the first version of this could
        #  never fix: it only looked at the title, so healing the title stopped it looking again
        #  and the number it had already fetched in the same call was dropped on the floor. A
        #  handful of rows ask for this on every sweep and never get an answer; that is fifteen
        #  cheap calls, against a page that otherwise cannot link a German patent to its file.
        if (not row.get("title") or row.get("title") == row.get("publication")
                or not row.get("application")):
            try:
                for key, value in biblio_for(row.get("publication") or "").items():
                    if not row.get(key) or row.get(key) == row.get("publication"):
                        patch.setdefault(key, value)
            except Exception:
                pass
        tick(row.get("publication") or "")
        return row, patch

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, patch in pool.map(one, rows):
            pub = row.get("publication")
            if not pub:
                continue
            if patch.get("_error"):
                errors.append("%s: %s" % (pub, patch["_error"]))
                continue
            if patch.get("_skipped"):
                continue
            patch = {k: v for k, v in patch.items() if k in MERGE_FIELDS or k == "register_events"}
            for key in SWEEP_OWNED:
                patch.setdefault(key, None)
            patch["refreshed_at"] = today.isoformat()
            for line in _describe(row, patch):
                changes.append(line)
            patches[pub] = patch

    new, rejected = [], []
    applicants = applicants_of(rows) if target is None else []
    if discover and target is None and not applicants:
        #  Nothing to search for. An empty docket, or one whose rows carry no applicant, must not
        #  fall back to a name this module happens to know.
        discover = False
    if discover:
        known = {(r.get("publication") or "").upper() for r in rows}
        known |= {_epodoc(r.get("publication") or "").upper() for r in rows}
        known |= {(r.get("granted_as") or "").upper() for r in rows}
        known |= {re.sub(r"\D", "", str(r.get("application") or "")) for r in rows if r.get("application")}
        known.discard("")
    if discover and target is not None:
        since = _months_ago(today, target.get("lookback_months") or DISCOVERY_MONTHS)
        try:
            found, gone = discover_target_ops(target, known, since, today)
            new.extend(found)
            rejected.extend(gone)
        except Exception as exc:
            errors.append("OPS discovery: %s" % str(exc)[:140])
        tick("new EP, DE and WO publications by %s" % (target.get("name") or "the target"))
        try:
            found, gone = discover_target_odp(target, known, since, today)
            new.extend(found)
            rejected.extend(gone)
        except OdpUnavailable as exc:
            errors.append("USPTO discovery could not run (%s); no US case was added or ruled "
                          "out this time." % exc)
        except Exception as exc:
            errors.append("ODP discovery: %s" % str(exc)[:140])
        tick("new US applications by %s" % (target.get("name") or "the target"))
        if rejected:
            changes.append("Found and set aside, the names on the record do not match this "
                           "target: %s." % ", ".join(rejected[:8])
                           + (" And %d more." % (len(rejected) - 8) if len(rejected) > 8 else ""))
    elif discover:
        try:
            since = today.replace(year=today.year - 1) if today.month != 2 or today.day != 29 \
                else datetime.date(today.year - 1, 3, 1)
            keep = _applicant_filter(applicants)
            for pub in discover_ops(since, applicants):
                if pub.upper() in known or _kind(pub) in SKIP_KINDS:
                    continue
                office = _office_of(pub)
                #  US publications come from the ODP sweep instead. OPS serves them in the DOCDB
                #  spelling (US2025332743A1, one digit short of the office's own
                #  US20250332743A1) and without an application number, which is the only key the
                #  file wrapper answers to, so a US row found here could never be refreshed.
                if not office or office == "USPTO":
                    continue
                #  CHECK WHOSE IT IS BEFORE ADDING IT. Searching the distinctive word is what
                #  finds a subsidiary filing under its own name, and it is also what found
                #  WO 2026/164863, "Centralized protection scheme for DC power distribution
                #  system", which belongs to a different Schmalz entirely. The search is a
                #  shortlist; the bibliographic record is the answer.
                facts = {}
                try:
                    facts = biblio_for(pub)
                except Exception:
                    pass
                owner = facts.get("applicant") or ""
                if not keep.search(owner):
                    rejected.append("%s (%s)" % (pub, owner or "no applicant on the record"))
                    continue
                new.append(_new_row(pub, office, facts))
                known.add(pub.upper())
        except Exception as exc:
            errors.append("OPS discovery: %s" % str(exc)[:140])
        tick("new EP and DE publications")
        try:
            for hit in discover_odp(applicants):
                if not hit["publication"] or hit["publication"].upper() in known:
                    continue
                if hit["application"] and hit["application"] in known:
                    continue
                if re.search(r"abandon|expired|patented case", hit["status"], re.I):
                    continue
                new.append(_new_row(hit["publication"], "USPTO", hit))
                known.add(hit["publication"].upper())
        except OdpUnavailable as exc:
            #  Said plainly. "No new US applications" and "the USPTO would not answer" look
            #  identical on the page otherwise, and only one of them means you are up to date.
            errors.append("USPTO discovery could not run (%s); no US case was added or ruled "
                          "out this time." % exc)
        except Exception as exc:
            errors.append("ODP discovery: %s" % str(exc)[:140])
        tick("new US applications")
        if rejected:
            #  Said out loud rather than dropped silently: a search term throwing away half its
            #  hits is a search term that needs looking at.
            changes.append("Found and set aside, the applicant does not match this docket: %s."
                           % ", ".join(rejected[:6]))

    #  A newly found case is worth nothing until it has a posture and a window, so it goes round
    #  the same loop once before it is ever shown. In parallel, and counted: a first sweep of a
    #  new target is a hundred of these and nothing else, and a bar stuck at "2 of 2" for five
    #  minutes reads as a hang.
    total[0] += len(new)

    def settle(row):
        patch = refresh_case(row)
        if not patch.get("_error") and not patch.get("_skipped"):
            row.update({k: v for k, v in patch.items()
                        if k in MERGE_FIELDS or k == "register_events"})
        elif patch.get("_error"):
            row["refresh_error"] = patch["_error"]
        #  Only what the row does not already have. The ODP discovery hands over a title and an
        #  application number; the OPS one hands over a publication number and nothing else, and
        #  a docket row whose title is its own number cannot be read at all.
        if not row.get("title_full") or row.get("title") == row["publication"]:
            try:
                for key, value in biblio_for(row["publication"]).items():
                    if not row.get(key) or row.get(key) == row["publication"]:
                        row[key] = value
            except Exception as exc:
                row["biblio_error"] = str(exc)[:100]
        row["refreshed_at"] = today.isoformat()
        tick(row["publication"])
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pool.map(settle, new):
            err = row.pop("refresh_error", None)
            if err:
                errors.append("%s: %s" % (row["publication"], err))
            err = row.pop("biblio_error", None)
            if err:
                errors.append("%s biblio: %s" % (row["publication"], err))
            changes.append("New on the docket: %s (%s), %s."
                           % (row["publication"], row["office"],
                              row.get("register_status") or "status unread"))

    return {"patches": patches, "new": new, "errors": errors, "changes": changes,
            "as_of": today.isoformat(), "applicants": applicants,
            "sources": {"USPTO": "Open Data Portal", "EPO": "OPS European Patent Register",
                        "DPMA": "OPS INPADOC legal status"}}


_WATCH = (("posture", "posture"), ("register_status", "register status"),
          ("deadline", "deadline"), ("grant_published", "grant published"),
          ("first_rejection", "first rejection"), ("allowance", "notice of allowance"))


def _describe(row, patch):
    """What actually moved, in a sentence a person can check. Silence when nothing moved."""
    out = []
    for key, label in _WATCH:
        before, after = row.get(key), patch.get(key)
        if after in (None, "") or before == after:
            continue
        if before in (None, ""):
            out.append("%s: %s is now %s." % (row.get("publication"), label, after))
        else:
            out.append("%s: %s moved from %s to %s." % (row.get("publication"), label,
                                                        before, after))
    subs = patch.get("our_submissions") or []
    old = {s.get("date") for s in (row.get("our_submissions") or [])}
    for s in subs:
        if s["date"] not in old:
            out.append("%s: our own submission of %s is on the file wrapper (%d documents)."
                       % (row.get("publication"), s["date"], s.get("documents") or 0))
    return out


# ---------------------------------------------------------------------------------------------
# writing it back, per user
# ---------------------------------------------------------------------------------------------

def apply_to_user(user_id, result, target_id=None, kind="patent"):
    """Merge a sweep onto one of a person's targets. Only the register fields move.

    The refresh record (when, what moved, what could not be read) lives on the target, one per
    KIND: the trademark sweep and the design sweep are separate jobs and the page shows one kind
    at a time, so a design page must not report what the trademark sweep found. The per-user
    meta keeps only the shipped file's own notes.
    """
    import observations
    observations.ensure_schema()
    n_updated = n_new = 0
    with db.cursor(autocommit=True) as cur:
        if target_id is None:
            target_id = observations.default_target_id(cur, user_id)
        for pub, patch in (result.get("patches") or {}).items():
            cur.execute("SELECT payload FROM app_observation_cases "
                        "WHERE user_id = %s AND target_id = %s AND publication = %s",
                        (user_id, target_id, pub))
            got = cur.fetchone()
            if not got:
                continue
            payload = dict(got["payload"])
            payload.update(patch)
            cur.execute("UPDATE app_observation_cases SET payload = %s, updated_at = now() "
                        "WHERE user_id = %s AND target_id = %s AND publication = %s",
                        (json.dumps(payload), user_id, target_id, pub))
            n_updated += cur.rowcount
        for row in (result.get("new") or []):
            cur.execute(
                """INSERT INTO app_observation_cases (user_id, target_id, publication, payload)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id, target_id, publication) DO NOTHING""",
                (user_id, target_id, row["publication"], json.dumps(row)))
            n_new += cur.rowcount
        record = {"as_of": result["as_of"],
                  "refreshed_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat(),
                  "sources": result.get("sources") or {},
                  "errors": (result.get("errors") or [])[:40],
                  "changes": (result.get("changes") or [])[:200],
                  "counts": {"updated": n_updated, "new": n_new,
                             "errors": len(result.get("errors") or [])}}
        kind = kind if kind in ("design", "trademark") else "patent"
        #  `refreshed_at` on the row stays the PATENT pull, which is what the row meant before
        #  kinds existed; the other kinds keep theirs inside the record.
        cur.execute("""UPDATE app_observation_targets
                          SET refresh = (CASE WHEN refresh ? 'changes' THEN jsonb_build_object('patent', refresh)
                                              ELSE COALESCE(refresh, '{}'::jsonb) END)
                                        || jsonb_build_object(%s, %s::jsonb),
                              refreshed_at = CASE WHEN %s = 'patent' THEN now() ELSE refreshed_at END,
                              updated_at = now()
                        WHERE user_id = %s AND id = %s""",
                    (kind, json.dumps(record), kind, user_id, target_id))
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (user_id,))
        got = cur.fetchone()
        meta = dict(got["payload"]) if got else {}
        meta["as_of"] = result["as_of"]
        meta["source_note"] = (
            "Refreshed live from the USPTO Open Data Portal, the EPO Register and the INPADOC "
            "legal file on %s." % result["as_of"])
        cur.execute(
            """INSERT INTO app_observation_meta (user_id, payload) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET payload = EXCLUDED.payload,
                                                   updated_at = now()""",
            (user_id, json.dumps(meta)))
    return {"updated": n_updated, "new": n_new}


# ---------------------------------------------------------------------------------------------
# the job behind the button
# ---------------------------------------------------------------------------------------------

def _job_key(user_id, target_id, kind="patent"):
    return (int(user_id), int(target_id) if target_id is not None else None,
            kind if kind in ("design", "trademark") else "patent")


def state(user_id, target_id=None, kind="patent"):
    """What this target's refresh is doing. `known` is False when there is no record of one.

    The job lives in this process's memory, which is right for a single-worker app and wrong
    across a restart: a page polling through a `supervisorctl restart` used to be told
    `{"running": False}` with no result, which it rendered as "Done. 0 cases re-read", a
    successful-looking answer to a job that had been killed. The absence of a record is a third
    state and has to be reported as one.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(_job_key(user_id, target_id, kind))
        if not job:
            return {"running": False, "known": False}
        out = dict(job)
        out["known"] = True
        return out


def _set(key, **kw):
    with _JOBS_LOCK:
        job = _JOBS.setdefault(key, {})
        job.update(kw)


def start(user_id, rows, target=None, kind="patent"):
    """Kick off a refresh of one target's docket of one kind. Returns False when one is already
    running for it. Designs and marks go through observation_marks; patents through sweep()."""
    target_id = (target or {}).get("id")
    key = _job_key(user_id, target_id, kind)
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job and job.get("running"):
            return False
        _JOBS[key] = {"running": True, "done": 0, "total": len(rows) + 2,
                      "label": "starting", "started": time.time(), "error": "",
                      "target_id": target_id, "result": None}

    def run():
        try:
            progress = lambda d, t, label: _set(key, done=d, total=t, label=label)  # noqa: E731
            if kind in ("design", "trademark"):
                import observation_marks
                res = observation_marks.sweep(rows, target or {}, kind, progress=progress)
            else:
                res = sweep(rows, progress=progress, target=target)
            counts = apply_to_user(user_id, res, target_id=target_id, kind=kind)
            _set(key, running=False, label="done",
                 result={"cases": len(rows) + counts["new"],
                         "updated": counts["updated"], "new": counts["new"],
                         "errors": res["errors"][:20], "changes": res["changes"][:60],
                         "as_of": res["as_of"]})
        except Exception as exc:
            traceback.print_exc()
            _set(key, running=False, error=str(exc)[:200], label="failed")

    threading.Thread(target=run, name="obs-refresh-%s-%s" % (user_id, target_id),
                     daemon=True).start()
    return True
