"""What the file wrapper already says, before we search for anything.

WHY, MEASURED
-------------
A patent attorney filed six documents against U.S. App. 18/915,337 on 2026-07-24. Five were
patents. The sixth — and it is the one a search engine can never produce — was the examiner's own
Non-Final Rejection from the PARENT application, in which the examiner had already applied one of
those five under 35 U.S.C. 102(a)(2) to thirteen claims, and the applicant had then abandoned
rather than respond.

That document is not prior art in the ordinary sense. It is proof that a USPTO examiner, given the
same specification, already reached the conclusion we are trying to argue. Nothing in a patent
corpus contains it: it lives in a file wrapper, it is non-patent literature, and finding it
requires knowing the parent exists.

All of that is one API call away, and we already hold the key. Verified 2026-08-16 against the
real application:

    18/915,337  "Docketed New Case - Ready for Exam"       <- the subject
      is a Continuation of 18/513,573 "Patented Case"       -> US 12,115,659
        is a Continuation of 17/724,791 "Abandoned -- Failure to Respond to an Office Action"
          claims priority from provisional 63/176,890
    17/724,791 file wrapper, 44 documents, including:
      CTNF 2025-09-16  Non-Final Rejection                  <- the attorney's Document 6
      892  2025-09-16  List of references cited by examiner <- a gold set, with authority
      1449 2025-09-16  List of references cited by applicant and considered

WHAT THIS IS FOR, AND WHAT IT IS NOT
------------------------------------
It is not a retrieval channel and it does not rank anything. It answers three questions the search
never asks, and each of them changes what the search should do:

  * has an examiner already rejected these claims, and over what? An office action in the family is
    the strongest single piece of evidence a submission can carry, and it is free.
  * what did the examiner cite? The 892 is a reference list chosen by a professional who read the
    application. As a seed for query-by-example and citation expansion it beats anything a model
    guesses, and as an evaluation set it is authoritative.
  * what else is in the family? A granted sibling is a double-patenting argument (the office action
    above raises exactly that over US 12,115,659), and a continuation still pending is the next
    window to file.

Fail-soft everywhere and OFF without a key: this runs beside a search that already has an answer,
and an unreachable USPTO must cost its own findings and nothing else.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

try:                                     # side effect: config loads .env, where the key lives
    import config                        # noqa: F401
except Exception:
    pass

BASE = os.environ.get("USPTO_ODP_BASE", "https://api.uspto.gov/api/v1").rstrip("/")
#  Never a literal. The repo is public; the value belongs in .env (0600, gitignored) or the
#  supervisor config, and in the advisor as `uspto-odp`.
#
#  Read through `_key()` rather than used directly, because a module constant binds at IMPORT time
#  and `config` is what loads .env — so a module imported before config, or an env set after
#  import, silently has no key and the dossier reports "no USPTO_ODP_KEY" while the key is sitting
#  in the file. Tests still monkeypatch KEY, which is why `_key` prefers it when it is set.
KEY = os.environ.get("USPTO_ODP_KEY", "")


def _key() -> str:
    return KEY or os.environ.get("USPTO_ODP_KEY", "") or os.environ.get("ODP_API_KEY", "")
TIMEOUT = float(os.environ.get("USPTO_ODP_TIMEOUT", "40"))
ENABLED = os.environ.get("FAMILY_DOSSIER_ENABLED", "1") != "0"
#  How far up and down the continuity chain to walk. The measured case needed two hops up to reach
#  the office action; three is one more than that and bounds a pathological family.
MAX_HOPS = int(os.environ.get("FAMILY_DOSSIER_HOPS", "3"))
MAX_RELATIVES = int(os.environ.get("FAMILY_DOSSIER_MAX", "12"))

#  Document codes worth pulling out of a 44-document wrapper. CTNF/CTFR/CTAV are rejections, 892 is
#  the examiner's own citation list, 1449/IDS is the applicant's, NOA is an allowance.
_REJECTIONS = ("CTNF", "CTFR", "CTAV", "CTMS")
_CITATIONS = ("892", "1449", "IDS")
_ALLOWANCE = ("NOA", "N417")


def _norm_app(s) -> str:
    """"18/915,337" -> "18915337". The API wants digits."""
    return re.sub(r"\D", "", str(s or ""))


def _norm_pub(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _call(path, body=None, log=print):
    """GET or POST against ODP. -> dict, or {} on any failure. Never raises."""
    if not ENABLED:
        return {}
    key = _key()
    if not key:
        log("[dossier] no USPTO_ODP_KEY in the environment; skipping the file wrapper")
        return {}
    url = f"{BASE}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "X-API-KEY": key, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
                return json.loads(fh.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            #  404 is a real answer — that application has no such record — and must not be retried.
            if e.code == 404:
                return {}
            log(f"[dossier] {path}: HTTP {e.code}")
            if e.code < 500:
                return {}
        except Exception as e:
            log(f"[dossier] {path}: {str(e)[:120]}")
        if attempt == 0:
            time.sleep(1.5)
    return {}


# ---------------------------------------------------------------------------
# finding the application
# ---------------------------------------------------------------------------
def application_for(publication="", patent="", log=print) -> dict:
    """A publication or patent number -> {app, status, title, pub, patent} for that application.

    The subject arrives as a publication number everywhere else in this pipeline; the file wrapper
    is keyed by application number, and nothing else in the repo bridges the two.
    """
    q = ""
    if publication:
        q = f"applicationMetaData.earliestPublicationNumber:{_norm_pub(publication)}"
    elif patent:
        q = f"applicationMetaData.patentNumber:{re.sub(r'[^0-9]', '', str(patent))}"
    if not q:
        return {}
    d = _call("patent/applications/search",
              {"q": q, "pagination": {"offset": 0, "limit": 3}}, log=log)
    for r in (d.get("patentFileWrapperDataBag") or []):
        m = r.get("applicationMetaData") or {}
        return {"app": _norm_app(r.get("applicationNumberText")),
                "status": m.get("applicationStatusDescriptionText") or "",
                "title": m.get("inventionTitle") or "",
                "pub": m.get("earliestPublicationNumber") or "",
                "patent": m.get("patentNumber") or "",
                "filed": m.get("filingDate") or ""}
    return {}


def continuity(app, log=print) -> list:
    """The family chain. -> [{app, relation, status, patent, filed, direction}]

    Both directions matter and for different reasons: a PARENT may carry an office action on
    substantially these claims, and a CHILD still pending is the next window to file against.
    """
    app = _norm_app(app)
    if not app:
        return []
    d = _call(f"patent/applications/{app}/continuity", log=log)
    out, seen = [], {app}
    for bag, direction in (("parentContinuityBag", "parent"),
                           ("childContinuityBag", "child")):
        for row in _rows(d, bag):
            num = _norm_app(row.get("parentApplicationNumberText") if direction == "parent"
                            else row.get("childApplicationNumberText"))
            if not num or num in seen:
                continue
            seen.add(num)
            out.append({
                "app": num, "direction": direction,
                "relation": row.get("claimParentageTypeCodeDescriptionText") or "",
                "status": (row.get("parentApplicationStatusDescriptionText")
                           or row.get("childApplicationStatusDescriptionText") or ""),
                "patent": row.get("parentPatentNumber") or row.get("childPatentNumber") or "",
                "filed": (row.get("parentApplicationFilingDate")
                          or row.get("childApplicationFilingDate") or "")})
    return out[:MAX_RELATIVES]


def _rows(payload, key):
    """ODP nests the continuity bags one level down in some responses and not others."""
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get(key), list):
        return payload[key]
    for v in payload.values():
        if isinstance(v, dict) and isinstance(v.get(key), list):
            return v[key]
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and isinstance(it.get(key), list):
                    return it[key]
    return []


def documents(app, log=print) -> dict:
    """The file wrapper, reduced to what an invalidity argument can use.

    -> {"rejections": [...], "citation_lists": [...], "allowances": [...], "n_documents": int}
    """
    app = _norm_app(app)
    if not app:
        return {"rejections": [], "citation_lists": [], "allowances": [], "n_documents": 0}
    d = _call(f"patent/applications/{app}/documents", log=log)
    docs = d.get("documentBag") or d.get("documents") or []
    out = {"rejections": [], "citation_lists": [], "allowances": [], "n_documents": len(docs)}
    for x in docs:
        code = str(x.get("documentCode") or "").upper()
        rec = {"app": app, "code": code,
               "description": x.get("documentCodeDescriptionText") or "",
               "date": str(x.get("officialDate") or x.get("mailRoomDate") or "")[:10],
               "id": x.get("documentIdentifier") or "",
               "pdf": next((u.get("downloadUrl") for u in (x.get("downloadOptionBag") or [])
                            if str(u.get("mimeTypeIdentifier") or "").upper() == "PDF"), "")}
        if code in _REJECTIONS:
            out["rejections"].append(rec)
        elif code in _CITATIONS:
            out["citation_lists"].append(rec)
        elif code in _ALLOWANCE:
            out["allowances"].append(rec)
    for k in ("rejections", "citation_lists", "allowances"):
        out[k].sort(key=lambda r: r["date"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# the whole picture
# ---------------------------------------------------------------------------
def dossier(publication="", application="", log=print, emit=None) -> dict:
    """Everything the USPTO already holds about this application's family.

    -> {"subject", "family", "rejections", "citation_lists", "siblings_granted", "error"}

    Never raises. Returns `error` and empty lists when the key is absent or the API is unreachable,
    because this runs beside a search that already has an answer.
    """
    out = {"subject": {}, "family": [], "rejections": [], "citation_lists": [],
           "siblings_granted": [], "n_documents": 0, "error": ""}
    if not ENABLED:
        out["error"] = "family dossier disabled"
        return out
    if not _key():
        out["error"] = "no USPTO_ODP_KEY"
        return out
    app = _norm_app(application)
    if not app and publication:
        found = application_for(publication=publication, log=log)
        out["subject"] = found
        app = found.get("app", "")
    elif app:
        out["subject"] = {"app": app}
    if not app:
        out["error"] = f"no US application found for {publication or application}"
        return out

    #  Walk the chain, not just one hop. The measured case needed TWO hops up to reach the office
    #  action: the subject's parent was itself a continuation, and the abandonment was above that.
    family, frontier, seen = [], [app], {app}
    for _hop in range(MAX_HOPS):
        nxt = []
        for a in frontier:
            for rel in continuity(a, log=log):
                if rel["app"] in seen or len(family) >= MAX_RELATIVES:
                    continue
                seen.add(rel["app"])
                family.append(rel)
                nxt.append(rel["app"])
        frontier = nxt
        if not frontier:
            break
    out["family"] = family
    out["siblings_granted"] = [f for f in family if f.get("patent")]

    #  The subject's own wrapper first, then every relative's. A rejection on the subject itself is
    #  the most relevant of all, and it is also the thing that tells us whether we are too late.
    for a in [app] + [f["app"] for f in family]:
        docs = documents(a, log=log)
        out["n_documents"] += docs["n_documents"]
        out["rejections"].extend(docs["rejections"])
        out["citation_lists"].extend(docs["citation_lists"])
    out["rejections"].sort(key=lambda r: r["date"], reverse=True)
    out["citation_lists"].sort(key=lambda r: r["date"], reverse=True)

    abandoned = [f for f in family
                 if "abandon" in (f.get("status") or "").lower()]
    log(f"[dossier] {app}: {len(family)} relatives, {out['n_documents']} wrapper documents, "
        f"{len(out['rejections'])} office actions, {len(out['citation_lists'])} examiner/applicant "
        f"citation lists"
        + (f", {len(abandoned)} relative(s) ABANDONED after an office action" if abandoned else "")
        + (f", granted siblings: {', '.join(f['patent'] for f in out['siblings_granted'])}"
           if out["siblings_granted"] else ""))
    if emit:
        emit("dossier", family=len(family), rejections=len(out["rejections"]),
             siblings=len(out["siblings_granted"]))
    return out


def summarise(d) -> str:
    """One paragraph for the report, or "" when there is nothing to say."""
    if not d or d.get("error") or not (d.get("rejections") or d.get("family")):
        return ""
    bits = []
    if d.get("rejections"):
        r = d["rejections"][0]
        bits.append(f"An examiner has already issued {len(d['rejections'])} office action(s) in "
                    f"this family, most recently {r['description']} on {r['date']} in application "
                    f"{r['app']}")
    ab = [f for f in d.get("family") or [] if "abandon" in (f.get("status") or "").lower()]
    if ab:
        bits.append(f"{len(ab)} application(s) in the family were ABANDONED "
                    f"({ab[0].get('status')})")
    if d.get("siblings_granted"):
        bits.append("granted siblings in the family: "
                    + ", ".join(f"US {f['patent']}" for f in d["siblings_granted"]))
    return ". ".join(bits) + "."
