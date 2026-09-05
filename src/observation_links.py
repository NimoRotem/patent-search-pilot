"""What has already been built for a case: the filing packets and the prior-art searches.

Two other systems already hold the answer to "have we started on this one". The filing app
(rotem.ai/patents/filing, a separate process on this box) keeps every 1.290 packet, every Art. 115
observation and every other submission as a directory with a meta.json in it. This app's own
search history keeps every report, and each report remembers which publication it was run from.
Neither knew about the docket, so the docket could list a case as untouched while a signed packet
for it sat one tab away.

This module reads both and pins what it finds to the case it concerns, by application number and
by publication number, NEVER by family. A packet for the US member is not a packet for the German
one, and the whole point of the docket is that one of those can be open while the other is shut.

FILES, NOT HTTP. The filing app is behind its own login, and both processes run as one user on one
box, so its data directory is read directly; `OBS_FILING_DATA` points elsewhere for a deployment
where that is not so. Nothing here writes into it.

THE SEARCH INDEX IS PERSISTED. The publication a report was run from lives in its document stash,
a megabyte of chunk vectors and drawings, and there are dozens of them. Reading those on every
page load is out of the question, so each slug is read once and the answer is kept in a small
JSON file beside the docket data. A slug whose meta file changes is read again.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))

#  The filing app's data directory. Its supervisor env calls this PATENT_DATA_DIR; here it is
#  named for what it is to this app so the two cannot be confused in one environment.
FILING_DATA = Path(os.environ.get("OBS_FILING_DATA",
                                  "/home/nimrod_rotem/builder4-home/.patent-filing"))
#  Where the filing app's page is, root-relative, so a link works behind the same domain.
FILING_URL = os.environ.get("OBS_FILING_URL", "/patents/filing/")
#  This app's own report store and the docket's data directory.
REPORTS = Path(os.environ.get("OBS_REPORTS_DIR", str(_HERE.parent / "data" / "reports")))
INDEX_PATH = Path(os.environ.get("OBS_SEARCH_INDEX",
                                 str(_HERE.parent / "data" / "observations" / "search_index.json")))

#  The filing app's three stores, and the pipeline name its page routes on (#/sub/<pipeline>/<id>).
STORES = (
    ("observations", "us-observation", "us-1290"),
    ("ep_observations", "ep-observation", "ep-art115"),
    ("submissions", "submission", ""),
)
KIND_LABEL = {
    "us-1290": "US 1.290 submission", "us-1291": "US protest", "us-301": "US § 301 citation",
    "us-pgr": "US post-grant review", "us-ipr": "US inter partes review",
    "us-reexam": "US reexamination", "us-application": "US application",
    "ep-art115": "EP Art. 115 observations", "ep-opposition": "EP opposition",
    "de-43-3": "DE § 43(3) Einwendung", "de-opposition": "DE Einspruch",
}
#  Ranks a packet's state so the row shows the furthest one along.
STATE_RANK = {"filed": 4, "handed off": 3, "built": 2, "draft": 1, "started": 0}

_SLUG = re.compile(r"\badhoc-[0-9a-f]{6,}\b")


# ---------------------------------------------------------------------------------------------
# numbers as keys
# ---------------------------------------------------------------------------------------------

def pub_keys(text):
    """Every spelling of one publication number reduced to what they share. -> set of keys.

    Country plus digits, no punctuation, no kind code. A US pre-grant number has two spellings
    in the wild: the office's own with a zero after the year (US20260070232) and DOCDB's without
    it (US2026070232). Both are returned for either input so the two sides can always meet.
    """
    key = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    key = re.sub(r"(?:[ABCTU]\d?|[ABC])$", "", key) if len(key) > 4 else key
    m = re.match(r"^([A-Z]{2})(\d+)$", key)
    if not m:
        return {key} if key else set()
    cc, digits = m.group(1), m.group(2)
    out = {cc + digits}
    if cc == "US" and digits[:2] in ("19", "20"):
        if len(digits) == 10:
            out.add(cc + digits[:4] + "0" + digits[4:])
        elif len(digits) == 11 and digits[4] == "0":
            out.add(cc + digits[:4] + digits[5:])
    return out


def app_key(text):
    """A US application number as digits only: 19/318,450 and 19318450 are one number."""
    s = str(text or "").strip()
    if not s:
        return ""
    if re.match(r"^[A-Z]{2}", s.upper()):
        return re.sub(r"[^A-Z0-9]", "", s.upper())
    return re.sub(r"\D", "", s)


def case_keys(case):
    """The keys a docket row answers to: its publications and its application."""
    pubs = set()
    for field in ("publication", "granted_as", "patent_number"):
        pubs |= pub_keys(case.get(field))
    pubs.discard("")
    apps = {app_key(case.get("application"))}
    apps.discard("")
    return pubs, apps


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _iso(ts):
    try:
        return datetime.datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(ts or "")[:16]


# ---------------------------------------------------------------------------------------------
# the filing app's packets
# ---------------------------------------------------------------------------------------------

def _package(meta, pipeline, default_kind, path):
    """One packet directory's meta.json -> the row the docket shows for it."""
    items = meta.get("items") or []
    pkg = meta.get("package") or {}
    files = meta.get("files") or []
    forms = meta.get("forms") or []
    status = str(meta.get("status") or "").strip().lower()
    if meta.get("filing_date"):
        state = "filed"
    elif status.startswith("handed"):
        state = "handed off"
    elif status.startswith("demo"):
        state = "built"
    elif items or files or forms:
        state = "built" if (pkg or forms) else "draft"
    else:
        state = "started"
    kind = meta.get("submission_type") or default_kind or ""
    target = meta.get("target") if isinstance(meta.get("target"), dict) else {}
    number = str(meta.get("publication_number") or meta.get("number")
                 or target.get("number") or "").strip()
    application = str(meta.get("application_number") or "").strip()
    if not application and str(meta.get("number_kind") or "").lower() == "application":
        application = number
        number = ""
    pubs = set()
    if number:
        #  The EP store keeps the bare number ("4506111") and says what it is beside it.
        if number.isdigit() and kind.startswith("ep"):
            pubs = pub_keys("EP" + number)
        elif not number.isdigit():
            pubs = pub_keys(number)
    return {
        "id": meta.get("id") or path.name,
        "pipeline": pipeline,
        "kind": kind,
        "label": KIND_LABEL.get(kind, kind or "submission"),
        "title": str(meta.get("title") or ""),
        "number": number,
        "application": application,
        #  A 1.290 packet lists its references as items; a generic submission lists the papers
        #  it will file. Either is "how many documents", and a row that says 0 for a packet of
        #  twenty is worse than no column.
        "items": len(items) or (len(pkg.get("filed") or []) if isinstance(pkg, dict) else 0),
        "concise": len(pkg.get("concise_files") or []) if isinstance(pkg, dict) else 0,
        "files": len(files),
        "forms": len(forms),
        "state": state,
        "status": status,
        "session": str(meta.get("session") or ""),
        "filing_date": str(meta.get("filing_date") or ""),
        "created": _iso(meta.get("created")),
        "created_ts": float(meta.get("created") or 0) if str(meta.get("created") or "").replace(".", "", 1).isdigit() else 0.0,
        "demo": bool(meta.get("demo")) or path.name.endswith("-demo"),
        "signer": str(meta.get("signer_name") or meta.get("filer_name") or ""),
        "url": "%s#/sub/%s/%s" % (FILING_URL, pipeline, meta.get("id") or path.name),
        "_pubs": pubs,
        "_app": app_key(application),
    }


_PKG_LOCK = threading.Lock()
_PKG_CACHE = {"sig": None, "rows": []}


def packages(data_dir=None):
    """Every packet the filing app holds, newest first. Re-read only when a meta.json changed."""
    root = Path(data_dir) if data_dir else FILING_DATA
    metas = []
    for sub, pipeline, kind in STORES:
        d = root / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            m = p / "meta.json"
            if p.is_dir() and m.is_file():
                try:
                    metas.append((p, pipeline, kind, m.stat().st_mtime))
                except OSError:
                    pass
    sig = tuple(sorted((str(p), mt) for p, _, _, mt in metas))
    with _PKG_LOCK:
        if _PKG_CACHE["sig"] == sig and data_dir is None:
            return list(_PKG_CACHE["rows"])
    rows = []
    for p, pipeline, kind, _ in metas:
        meta = _read(p / "meta.json")
        if isinstance(meta, dict):
            rows.append(_package(meta, pipeline, kind, p))
    rows.sort(key=lambda r: (r["demo"], -r["created_ts"]))
    if data_dir is None:
        with _PKG_LOCK:
            _PKG_CACHE["sig"], _PKG_CACHE["rows"] = sig, list(rows)
    return rows


def attach_packages(cases, rows=None):
    """Pin each packet to the one case it names. Sets `packages` and `package_state` on the row."""
    rows = packages() if rows is None else rows
    by_pub, by_app = {}, {}
    for r in rows:
        for k in r["_pubs"]:
            by_pub.setdefault(k, []).append(r)
        if r["_app"]:
            by_app.setdefault(r["_app"], []).append(r)
    for c in cases:
        pubs, apps = case_keys(c)
        found, seen = [], set()
        for k in sorted(apps) + sorted(pubs):
            for r in by_app.get(k, []) + by_pub.get(k, []):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                found.append({k2: v for k2, v in r.items() if not k2.startswith("_")})
        found.sort(key=lambda r: (r["demo"], -STATE_RANK.get(r["state"], 0), -r["created_ts"]))
        c["packages"] = found
        real = [r for r in found if not r["demo"]]
        c["package_state"] = real[0]["state"] if real else ("demo" if found else "none")
    return cases


# ---------------------------------------------------------------------------------------------
# this app's own searches
# ---------------------------------------------------------------------------------------------

_IDX_LOCK = threading.Lock()


def _load_index():
    data = _read(INDEX_PATH)
    return data if isinstance(data, dict) else {}


def _save_index(index):
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, INDEX_PATH)
    except OSError:
        pass


def index_entry(slug, reports=None):
    """What one report was run from, read off its meta and, once, its document stash."""
    reports = Path(reports) if reports else REPORTS
    meta_path = reports / ("%s.meta.json" % slug)
    meta = _read(meta_path) or {}
    try:
        mtime = meta_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    token = re.sub(r"[^0-9a-f]", "", str(meta.get("doc_token") or ""))[:64]
    subject = str(meta.get("subject") or "").strip()
    if subject.lower() == "none":
        subject = ""
    pub, title, source = "", "", ""
    if token:
        doc = _read(reports / ("doc-%s.json" % token)) or {}
        pub = str(doc.get("publication_number") or doc.get("label") or "").strip()
        title = str(doc.get("title") or "")
        source = str(doc.get("source") or "")
    if not pub and subject:
        pub = subject
    return {"pub": pub, "title": title[:160], "source": source, "token": token,
            "mode": str(meta.get("mode") or ""), "depth": str(meta.get("depth") or ""),
            "meta_mtime": mtime}


def concise_count(slug, reports=None):
    """How many 1.290 concise descriptions are built for a slug. A directory listing only."""
    d = (Path(reports) if reports else REPORTS) / "concise" / slug
    try:
        return len([p for p in d.iterdir()
                    if p.name.startswith("ConciseDescription_") and p.suffix == ".pdf"])
    except OSError:
        return 0


def searches_for(user_id, rows=None, reports=None, index_path=None):
    """This person's searches, each with the publication it was run from and its zip state."""
    if rows is None:
        import accounts
        rows = accounts.list_searches(user_id, limit=1000)
    reports = Path(reports) if reports else REPORTS
    with _IDX_LOCK:
        index = _load_index() if index_path is None else (_read(index_path) or {})
        dirty = False
        out = []
        for row in rows:
            slug = str(row.get("slug") or "")
            if not slug or not re.match(r"^[A-Za-z0-9._-]+$", slug):
                continue
            meta_path = reports / ("%s.meta.json" % slug)
            try:
                mtime = meta_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            entry = index.get(slug)
            if not isinstance(entry, dict) or entry.get("meta_mtime") != mtime:
                entry = index_entry(slug, reports)
                index[slug] = entry
                dirty = True
            when = row.get("updated_at") or row.get("created_at")
            if hasattr(when, "strftime"):
                when = when.strftime("%Y-%m-%d")
            pub = entry.get("pub") or (row.get("subject") or "")
            out.append({
                "slug": slug,
                "when": str(when or "")[:10],
                "mode": row.get("mode") or entry.get("mode") or "",
                "status": row.get("status") or "",
                "query": (row.get("title") or row.get("query") or "")[:140],
                "pub": pub,
                "doc_title": entry.get("title") or "",
                "concise": concise_count(slug, reports),
                "_pubs": pub_keys(pub) if pub else set(),
            })
        if dirty:
            if index_path is None:
                _save_index(index)
            else:
                try:
                    Path(index_path).write_text(json.dumps(index), encoding="utf-8")
                except OSError:
                    pass
    return out


def attach_searches(cases, user_id, rows=None):
    """Pin each search to the case it was run from, or that its counsel note names.

    Two links. The strong one is the publication the report was run FROM, kept in the
    report's document stash. The other is a slug written into the docket by hand, counsel's
    "report" column, which is how the shipped rows point at the searches that were done for
    them before any of this was wired up.
    """
    rows = searches_for(user_id) if rows is None else rows
    by_pub, by_slug = {}, {}
    for s in rows:
        by_slug[s["slug"]] = s
        for k in s["_pubs"]:
            by_pub.setdefault(k, []).append(s)
    for c in cases:
        pubs, _ = case_keys(c)
        found, seen = [], set()
        named = _SLUG.findall(" ".join(str(c.get(k) or "") for k in ("counsel_report", "report")))
        for slug in named:
            s = by_slug.get(slug)
            if s and slug not in seen:
                seen.add(slug)
                found.append(s)
        for k in sorted(pubs):
            for s in by_pub.get(k, []):
                if s["slug"] not in seen:
                    seen.add(s["slug"])
                    found.append(s)
        found.sort(key=lambda s: (-s["concise"], s["when"]), reverse=False)
        found.sort(key=lambda s: s["when"], reverse=True)
        c["searches"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in found]
        if any(s["concise"] for s in found):
            c["search_state"] = "zip"
        elif any(str(s.get("status") or "").startswith("complete") for s in found):
            c["search_state"] = "done"
        elif found:
            c["search_state"] = "running"
        else:
            c["search_state"] = "none"
    return cases


def attach(cases, user_id):
    """Both, each failing on its own: a missing filing directory must not blank the searches."""
    try:
        attach_packages(cases)
    except Exception:
        for c in cases:
            c.setdefault("packages", [])
            c.setdefault("package_state", "none")
    try:
        attach_searches(cases, user_id)
    except Exception:
        for c in cases:
            c.setdefault("searches", [])
            c.setdefault("search_state", "none")
    return cases


def summary(cases):
    return {
        "packets": sum(1 for c in cases if c.get("package_state") not in ("none", "demo")),
        "searched": sum(1 for c in cases if c.get("search_state") in ("done", "zip")),
        "zipped": sum(1 for c in cases if c.get("search_state") == "zip"),
    }
