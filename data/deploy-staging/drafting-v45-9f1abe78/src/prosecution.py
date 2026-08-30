"""What the examiner already decided, read off the file wrapper itself.

WHY, MEASURED
-------------
`family_dossier` finds the family and lists the wrapper documents. It stops at the door: every one
of those documents is a SCANNED IMAGE with no text layer (`pdftotext` returns zero characters on
all seven of the subject's), so the thing worth having — what the examiner applied, under which
statute, to which claims — stayed locked inside a PDF nobody read.

Verified 2026-08-20 against the real application, US 2025/0033224 A1 (app 18/915,337):

    17/724,791  Non-Final Rejection, 2025-09-16
      claims 1-3, 5-9, 11-12, 14-15   102(a)(2)   over US 11,413,727 (Rotem)
      claim 4                          103         over US 11,413,727 in view of US 7,690,610
      claims 1-20                      ODP         over US 12,115,659
    17/724,791  Applicant's 1449, considered by the examiner, listing US 9,550,298 / 6,419,291 /
                7,690,610 / 11,413,727 / 8,382,174 / 10,549,405 / 4,750,768 / 7,240,935 /
                US 2004/0050205

Counsel filed five patents against this application. All five are in those two documents. A search
that reads them starts from the examiner's own answer instead of trying to rediscover it — and the
office action is itself a filable document under 37 CFR 1.290, one whose limitation-by-limitation
findings do the arguing the rule forbids the submitter from doing.

HOW
---
Gemini reads the scanned PDF directly (Vertex, no key needed from a VM). Results are cached on
disk because a wrapper document never changes, and the whole module is fail-soft: an unreachable
USPTO or a refused model costs its own findings and nothing else.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
import urllib.error
import urllib.request

import family_dossier

CACHE = os.environ.get("PROSECUTION_CACHE",
                       os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "data", "prosecution"))
#  Wrapper scans run to hundreds of pages on a long file history. The interesting part is the
#  front of the document — the rejection's own findings — and the rest is claim listings and forms.
MAX_PDF_BYTES = int(os.environ.get("PROSECUTION_MAX_PDF", str(12 * 1024 * 1024)))
ENABLED = os.environ.get("PROSECUTION_ENABLED", "1") not in ("0", "false", "no")

_READ_SYS = (
    "You are reading a scanned USPTO file-wrapper document. Return JSON only:\n"
    '{"applied":[{"number":"...","statute":"102(a)(1)|102(a)(2)|103|double patenting",'
    '"claims":"1-3, 5-9","note":"one clause on what it was applied for"}],'
    '"considered":["every patent or publication number listed as cited or considered"],'
    '"summary":"two sentences, factual, on what this document decides"}\n'
    "Transcribe every number exactly as printed. NEVER invent, complete or correct a number you "
    "cannot fully read — omit the row instead. `applied` is only for references the examiner "
    "actually applied in a rejection; a number that merely appears in a citation list belongs in "
    "`considered`."
)


def _cache_path(rec):
    key = hashlib.sha1(("%s|%s|%s|%s" % (rec.get("app"), rec.get("code"), rec.get("date"),
                                         rec.get("id"))).encode()).hexdigest()[:20]
    return os.path.join(CACHE, "%s.json" % key)


def fetch_pdf(rec, log=print):
    """The wrapper document's bytes, or b"". Never raises."""
    url = rec.get("pdf") or ""
    if not url:
        return b""
    key = family_dossier._key()
    if not key:
        return b""
    try:
        req = urllib.request.Request(url, headers={"X-API-KEY": key,
                                                   "Accept": "application/pdf"})
        with urllib.request.urlopen(req, timeout=120) as fh:
            blob = fh.read(MAX_PDF_BYTES + 1)
        if len(blob) > MAX_PDF_BYTES:
            log("[prosecution] %s %s: %d bytes, over the cap; skipped"
                % (rec.get("app"), rec.get("code"), len(blob)))
            return b""
        return blob
    except urllib.error.HTTPError as e:
        log("[prosecution] %s %s: HTTP %s" % (rec.get("app"), rec.get("code"), e.code))
    except Exception as e:
        log("[prosecution] %s %s: %s" % (rec.get("app"), rec.get("code"), str(e)[:120]))
    return b""


def read_document(rec, log=print, use_cache=True):
    """OCR one wrapper document into {applied, considered, summary}. -> {} when unreadable."""
    if not ENABLED:
        return {}
    path = _cache_path(rec)
    if use_cache and os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            pass
    blob = fetch_pdf(rec, log=log)
    if not blob:
        return {}
    try:
        import llm
        from google.genai import types
        parts = [types.Part.from_bytes(data=blob, mime_type="application/pdf"),
                 "Which references did the examiner apply, under which statute, to which claims?"]
        resp = llm._call_vision(_READ_SYS, parts, max_tokens=6000)
        txt = (getattr(resp, "text", "") or "").strip()
        got = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        traceback.print_exc()
        return {}
    out = {"app": rec.get("app"), "code": rec.get("code"), "date": rec.get("date"),
           "description": rec.get("description"), "pdf": rec.get("pdf"),
           "applied": [a for a in (got.get("applied") or []) if isinstance(a, dict)],
           "considered": [str(x) for x in (got.get("considered") or []) if x],
           "summary": str(got.get("summary") or "")}
    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(out, fh)
        os.replace(tmp, path)                    # atomic: a half-written cache reads as garbage
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- numbers


_PUB = re.compile(r"^(?:US)?\s*(\d{4})\s*[/-]?\s*(\d{6,7})\s*(?:A\d?)?$", re.I)
_PAT = re.compile(r"^(?:US)?\s*(\d[\d,]{5,10})\s*(?:[AB]\d?)?$", re.I)
_FOREIGN = re.compile(r"^([A-Z]{2})\s*[-/ ]?\s*([0-9][0-9 ,./-]{3,})\s*([A-Z]\d?)?$", re.I)


def normalise(number):
    """A number as printed on a USPTO form -> corpus spelling, or "".

    "US 11,413,727" -> "US-11413727", "2004/0050205" -> "US-20040050205", "DE 10 2013 106 004" ->
    "DE-102013106004". The kind code is left off: the corpus lookup resolves it, and guessing B1
    against B2 turns a real reference into a miss.
    """
    t = " ".join(str(number or "").strip().split())
    if not t:
        return ""
    m = _PUB.match(t)
    if m and len(m.group(2)) == 7:               # a pre-grant pub is year + 7-digit serial
        return "US-%s%s" % (m.group(1), m.group(2))
    m = _PAT.match(t)
    if m:
        digits = m.group(1).replace(",", "")
        if 6 <= len(digits) <= 8:
            return "US-%s" % digits
    m = _FOREIGN.match(t)
    if m:
        return "%s-%s" % (m.group(1).upper(), re.sub(r"\D", "", m.group(2)))
    return ""


def resolve(numbers, log=print):
    """Corpus-spelling numbers -> the publication_numbers this corpus actually holds.

    A form prints "US 2020/0338695" and the corpus holds "US-2020338695-A1", one leading zero
    lighter, because BigQuery's numeric handling drops them. `pubnorm` already owns that ladder and
    every other spelling gap in this repo — a second, simpler matcher here missed three of the
    nineteen numbers on the subject's own wrapper, one of which was the reference the family
    representative fix had just chosen.

    Prefers a member that can actually be read: the corpus holds both a bare `US-12115659` stub row
    and the real `US-12115659-B1`, and a match that returns the stub costs the reading.
    """
    import pubnorm
    want, keys = {}, set()
    for x in (numbers or []):
        n = normalise(x)
        if not n:
            continue
        cands = [c.replace("-", "").upper() for c in pubnorm.mongo_candidates(n)] or \
                [n.replace("-", "").upper()]
        want[n] = cands
        keys.update(cands)
    if not want:
        return {}, []
    rows = {}
    try:
        import db
        with db.cursor() as cur:
            #  One query for every spelling of every number, then pick per number below. LIKE with
            #  a trailing wildcard catches the kind code the form does not print.
            cur.execute(
                "SELECT p.publication_number, "
                "       replace(upper(p.publication_number),'-','') AS bare, "
                "       (SELECT count(*) FROM chunks ch WHERE ch.publication_id=p.id) AS n_chunks "
                "FROM publications p "
                "WHERE replace(upper(p.publication_number),'-','') = ANY(%s) "
                "   OR replace(upper(p.publication_number),'-','') LIKE ANY(%s)",
                (sorted(keys), sorted(k + "%" for k in keys)))
            for r in cur.fetchall():
                rows.setdefault(r["bare"], []).append(r)
    except Exception:
        traceback.print_exc()
        return {}, sorted(want)
    found = {}
    for n, cands in want.items():
        hits = []
        for c in cands:
            for bare, rs in rows.items():
                if bare == c or bare.startswith(c):
                    hits.extend(rs)
        if not hits:
            continue
        #  Most text first — that is the row the reader can open — then the longer number, which is
        #  the one carrying a kind code.
        hits.sort(key=lambda r: (-(r["n_chunks"] or 0), -len(r["publication_number"])))
        found[n] = hits[0]["publication_number"]
    missing = sorted(n for n in want if n not in found)
    if log:
        log("[prosecution] %d numbers off the wrapper: %d in the corpus, %d not"
            % (len(want), len(found), len(missing)))
    return found, missing


# --------------------------------------------------------------------------- the whole picture


def mine(dossier, log=print, limit_docs=6, emit=None):
    """Read the family's rejections and citation lists. -> what the examiner already found.

    -> {"documents": [read_document...], "applied": [{pub, statute, claims, source}],
        "considered": [pub], "seeds": [pub], "missing": [number], "error": ""}

    `seeds` is what the search should force into its reading list: references a USPTO examiner
    applied or considered against this very family, which is a better starting set than any model
    guesses and an authoritative benchmark besides.
    """
    out = {"documents": [], "applied": [], "considered": [], "seeds": [], "missing": [],
           "error": ""}
    if not ENABLED:
        out["error"] = "prosecution mining disabled"
        return out
    if not dossier or dossier.get("error"):
        out["error"] = (dossier or {}).get("error") or "no dossier"
        return out
    #  Rejections first — they carry the statute and the claim mapping — then the citation lists,
    #  which are wide but flat. Both are worth reading and the budget is small either way.
    recs = (dossier.get("rejections") or []) + (dossier.get("citation_lists") or [])
    recs = [r for r in recs if r.get("pdf")][:limit_docs]
    raw_numbers = []
    for i, rec in enumerate(recs):
        if emit:
            emit("prosecution", done=i, total=len(recs), doc=rec.get("code") or "")
        got = read_document(rec, log=log)
        if not got:
            continue
        out["documents"].append(got)
        for a in got.get("applied") or []:
            n = normalise(a.get("number"))
            if not n:
                continue
            raw_numbers.append(a.get("number"))
            out["applied"].append({"number": n, "statute": a.get("statute") or "",
                                   "claims": a.get("claims") or "", "note": a.get("note") or "",
                                   "source": "%s %s %s" % (rec.get("app"), rec.get("code"),
                                                           rec.get("date"))})
        raw_numbers.extend(got.get("considered") or [])
    found, missing = resolve(raw_numbers, log=log)
    out["missing"] = missing
    #  APPLIED FIRST. A reference an examiner used in a rejection outranks one that merely sat in
    #  an information disclosure statement, and the seed order is the order the reading budget is
    #  spent in.
    applied_pubs, seen = [], set()
    for a in out["applied"]:
        pub = found.get(a["number"])
        if pub:
            a["pub"] = pub
            if pub not in seen:
                seen.add(pub)
                applied_pubs.append(pub)
    #  The same resolution back onto the DOCUMENTS. A form prints "11,413,727" as often as
    #  "US 11,413,727", and the office action is itself a filable document whose rows have to name
    #  the reference in full — so the corpus spelling has to reach the document, not only the
    #  flat list beside it.
    for d in out["documents"]:
        for a in (d.get("applied") or []):
            pub = found.get(normalise(a.get("number")))
            if pub:
                a["pub"] = pub
    rest = [p for p in found.values() if p not in seen]
    out["considered"] = sorted(set(found.values()))
    out["seeds"] = applied_pubs + sorted(rest)
    if log:
        log("[prosecution] %d wrapper document(s) read: %d reference(s) APPLIED in a rejection, "
            "%d considered, %d seeds resolved in the corpus"
            % (len(out["documents"]), len(out["applied"]), len(out["considered"]),
               len(out["seeds"])))
    return out


def summarise(mined):
    """One paragraph for the report, or "" when there is nothing to say."""
    if not mined or mined.get("error") or not mined.get("documents"):
        return ""
    bits = []
    applied = [a for a in (mined.get("applied") or []) if a.get("pub")]
    if applied:
        by_statute = {}
        for a in applied:
            by_statute.setdefault(a["statute"] or "a rejection", []).append(a["pub"])
        bits.append("An examiner has already applied " + "; ".join(
            "%s under %s" % (", ".join(sorted(set(v))), k) for k, v in by_statute.items()))
    n = len(mined.get("considered") or [])
    if n:
        bits.append("%d reference(s) in this family's file wrapper were cited or considered by "
                    "the Office" % n)
    return ". ".join(bits) + "." if bits else ""


def for_subject(publication, log=print, emit=None):
    """Publication number -> {"dossier", "mined"}. The whole path, fail-soft."""
    try:
        d = family_dossier.dossier(publication=publication, log=log, emit=emit)
    except Exception:
        traceback.print_exc()
        return {"dossier": {"error": "dossier failed"}, "mined": {"error": "dossier failed"}}
    try:
        m = mine(d, log=log, emit=emit)
    except Exception:
        traceback.print_exc()
        m = {"error": "mining failed", "documents": [], "applied": [], "considered": [],
             "seeds": [], "missing": []}
    return {"dossier": d, "mined": m}
