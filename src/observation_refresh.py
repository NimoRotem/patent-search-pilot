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

#  Who the docket is about. Two names because Schmalz bought Soft Robotics and the USPTO still
#  files half the portfolio under the acquired company's name.
APPLICANT_QUERIES = ('pa="j. schmalz"', 'pa="schmalz flexible gripping"')
APPLICANT_PATTERN = re.compile(r"schmalz|soft robotics", re.I)

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
    "priority_date", "granted_as", "our_submissions", "refreshed_at", "refresh_source",
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
    "patent_number", "our_submissions",
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


def _odp(path, body=None):
    """GET or POST against the Open Data Portal. -> dict, {} on any failure. Never raises.

    Deliberately NOT `family_dossier._call`, which is otherwise the same request: that module is
    gated by FAMILY_DOSSIER_ENABLED, and a flag turned off for the drafting pipeline would take
    the American half of this docket down without a word in the log. The key is read at call time
    for the same reason the dossier does: a module constant binds before config loads .env.
    """
    key = os.environ.get("USPTO_ODP_KEY", "") or os.environ.get("ODP_API_KEY", "")
    if not key:
        return {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "%s/%s" % (ODP_BASE, path.lstrip("/")), data=data,
        headers={"X-API-KEY": key, "Accept": "application/json",
                 **({"Content-Type": "application/json"} if data else {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=ODP_TIMEOUT) as fh:
                return json.loads(fh.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or exc.code < 500:
                return {}
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return {}


def us_case(application):
    """One US application: where it stands, when 1.290 shuts, and what WE have already filed."""
    app = re.sub(r"\D", "", str(application or ""))
    if not app:
        return {"_error": "no application number"}
    payload = _odp("patent/applications/%s" % app)
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

    out["our_submissions"] = us_submissions(app, events)
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
            "references_about": (n_rel + 1) // 2 if n_rel > 1 else n_rel,
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

def discover_ops(since):
    """Publications by the applicant in a date window, from OPS. -> [publication numbers]"""
    found = []
    window = '%s %s' % (since.strftime("%Y%m%d"), datetime.date.today().strftime("%Y%m%d"))
    for base in APPLICANT_QUERIES:
        q = '%s and pd within "%s"' % (base, window)
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


def discover_odp():
    """Schmalz applications the USPTO knows about. -> [{application, publication, ...}]"""
    out = []
    payload = _odp("patent/applications/search",
                   {"q": 'applicationMetaData.firstApplicantName:"Schmalz"',
                    "pagination": {"offset": 0, "limit": 100}})
    for w in (payload or {}).get("patentFileWrapperDataBag") or []:
        md = w.get("applicationMetaData") or {}
        names = " ".join([md.get("firstApplicantName") or ""] +
                         [a.get("applicantNameText") or "" for a in (md.get("applicantBag") or [])])
        if not APPLICANT_PATTERN.search(names):
            continue
        out.append({
            "application": re.sub(r"\D", "", str(w.get("applicationNumberText") or "")),
            "publication": md.get("earliestPublicationNumber") or "",
            "title": md.get("inventionTitle") or "",
            "applicant": md.get("firstApplicantName") or "J. Schmalz GmbH",
            "status": md.get("applicationStatusDescriptionText") or "",
            "filed": md.get("filingDate") or "",
        })
    return out


def _new_row(publication, office, extra=None):
    row = {
        "publication": publication,
        "office": office,
        "title": (extra or {}).get("title") or publication,
        "title_full": (extra or {}).get("title") or "",
        "applicant": (extra or {}).get("applicant") or "J. Schmalz GmbH",
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
    except Exception as exc:
        return {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    return {"_skipped": "no live source for %s" % (office or "an unknown office")}


def sweep(rows, progress=None, workers=6, discover=True):
    """Refetch every row, then look for cases the docket has never seen.

    -> {"patches": {publication: patch}, "new": [row], "errors": [...], "sources": {...},
        "changes": [str]}
    """
    today = datetime.date.today()
    patches, errors, changes = {}, [], []
    done = [0]
    total = len(rows) + (2 if discover else 0)

    def tick(label):
        done[0] += 1
        if progress:
            progress(done[0], total, label)

    def one(row):
        patch = refresh_case(row)
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

    new = []
    if discover:
        known = {(r.get("publication") or "").upper() for r in rows}
        known |= {re.sub(r"\D", "", str(r.get("application") or "")) for r in rows if r.get("application")}
        known.discard("")
        try:
            since = today.replace(year=today.year - 1) if today.month != 2 or today.day != 29 \
                else datetime.date(today.year - 1, 3, 1)
            for pub in discover_ops(since):
                if pub.upper() in known or _kind(pub) in SKIP_KINDS:
                    continue
                office = _office_of(pub)
                #  US publications come from the ODP sweep instead. OPS serves them in the DOCDB
                #  spelling (US2025332743A1, one digit short of the office's own
                #  US20250332743A1) and without an application number, which is the only key the
                #  file wrapper answers to, so a US row found here could never be refreshed.
                if not office or office == "USPTO":
                    continue
                new.append(_new_row(pub, office))
                known.add(pub.upper())
        except Exception as exc:
            errors.append("OPS discovery: %s" % str(exc)[:140])
        tick("new EP and DE publications")
        try:
            for hit in discover_odp():
                if not hit["publication"] or hit["publication"].upper() in known:
                    continue
                if hit["application"] and hit["application"] in known:
                    continue
                if re.search(r"abandon|expired|patented case", hit["status"], re.I):
                    continue
                new.append(_new_row(hit["publication"], "USPTO", hit))
                known.add(hit["publication"].upper())
        except Exception as exc:
            errors.append("ODP discovery: %s" % str(exc)[:140])
        tick("new US applications")

    #  A newly found case is worth nothing until it has a posture and a window, so it goes round
    #  the same loop once before it is ever shown.
    for row in new:
        patch = refresh_case(row)
        if not patch.get("_error") and not patch.get("_skipped"):
            row.update({k: v for k, v in patch.items()
                        if k in MERGE_FIELDS or k == "register_events"})
        row["refreshed_at"] = today.isoformat()
        changes.append("New on the docket: %s (%s), %s."
                       % (row["publication"], row["office"],
                          row.get("register_status") or "status unread"))

    return {"patches": patches, "new": new, "errors": errors, "changes": changes,
            "as_of": today.isoformat(),
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

def apply_to_user(user_id, result):
    """Merge a sweep onto one person's docket. Only the register fields move."""
    import observations
    observations.ensure_schema()
    n_updated = n_new = 0
    with db.cursor(autocommit=True) as cur:
        for pub, patch in (result.get("patches") or {}).items():
            cur.execute("SELECT payload FROM app_observation_cases "
                        "WHERE user_id = %s AND publication = %s", (user_id, pub))
            got = cur.fetchone()
            if not got:
                continue
            payload = dict(got["payload"])
            payload.update(patch)
            cur.execute("UPDATE app_observation_cases SET payload = %s, updated_at = now() "
                        "WHERE user_id = %s AND publication = %s",
                        (json.dumps(payload), user_id, pub))
            n_updated += cur.rowcount
        for row in (result.get("new") or []):
            cur.execute(
                """INSERT INTO app_observation_cases (user_id, publication, payload)
                   VALUES (%s, %s, %s) ON CONFLICT (user_id, publication) DO NOTHING""",
                (user_id, row["publication"], json.dumps(row)))
            n_new += cur.rowcount
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (user_id,))
        got = cur.fetchone()
        meta = dict(got["payload"]) if got else {}
        meta["as_of"] = result["as_of"]
        meta["refreshed_at"] = datetime.datetime.now().replace(microsecond=0).isoformat()
        meta["refresh_sources"] = result.get("sources") or {}
        meta["refresh_errors"] = (result.get("errors") or [])[:40]
        meta["refresh_changes"] = (result.get("changes") or [])[:200]
        meta["refresh_counts"] = {"updated": n_updated, "new": n_new,
                                  "errors": len(result.get("errors") or [])}
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

def state(user_id):
    with _JOBS_LOCK:
        job = _JOBS.get(user_id)
        return dict(job) if job else {"running": False}


def _set(user_id, **kw):
    with _JOBS_LOCK:
        job = _JOBS.setdefault(user_id, {})
        job.update(kw)


def start(user_id, rows):
    """Kick off a refresh for one person. Returns False when one is already running for them."""
    with _JOBS_LOCK:
        job = _JOBS.get(user_id)
        if job and job.get("running"):
            return False
        _JOBS[user_id] = {"running": True, "done": 0, "total": len(rows) + 2,
                          "label": "starting", "started": time.time(), "error": "",
                          "result": None}

    def run():
        try:
            res = sweep(rows, progress=lambda d, t, label: _set(
                user_id, done=d, total=t, label=label))
            counts = apply_to_user(user_id, res)
            _set(user_id, running=False, label="done", done=len(rows) + 2,
                 result={"updated": counts["updated"], "new": counts["new"],
                         "errors": res["errors"][:20], "changes": res["changes"][:60],
                         "as_of": res["as_of"]})
        except Exception as exc:
            traceback.print_exc()
            _set(user_id, running=False, error=str(exc)[:200], label="failed")

    threading.Thread(target=run, name="obs-refresh-%s" % user_id, daemon=True).start()
    return True
