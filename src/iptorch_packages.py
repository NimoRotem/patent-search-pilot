"""Every package our own accounts build on iptorch.com, kept as a zip on the docket row it concerns.

iptorch.com (a separate process on this box, supervisor program patent-v3-nimo7) is where the
filing packages are built now: a search, then BUILD PACKAGE, then a zip. Nothing told the docket.
A package for a Schmalz case could be built on Tuesday, rebuilt on Thursday and handed to the
filing app on Friday, and the docket row for that case said "nothing prepared" throughout.

WHOSE PACKAGES. Only the accounts named in `IPTORCH_PACKAGE_ACCOUNTS`: the owner's own admin
account and the two people who build for this docket. iptorch.com takes public signups and a
stranger's package on a competitor's patent is none of this docket's business. The owner of a
package is not written anywhere in its folder, so it is read from iptorch's own search table,
slug by slug, read-only.

A REBUILD DELETES THE PACKAGE IT REPLACES. iptorch keeps one package per search and a rebuild
overwrites it in place, so "which version did we hand over" has no answer on iptorch itself a day
later. So every finished build is copied here the first time the sync sees it, as the exact zip
iptorch serves for download, and kept under the build's own timestamp. A rebuild becomes a second
zip on the row, never a replacement of the first.

FILES AND LOOPBACK, NOT A PUBLIC API. Both processes run as one user on one box. The package
folders are read directly, and the zip is fetched from iptorch's own loopback port, which serves
any report to an on-box caller: that is the one place the archive is assembled, so the copy here
is byte for byte what a person downloads.
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import threading
import time
import traceback
import urllib.request
from pathlib import Path

import observation_links

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))

#  iptorch.com's own home directory: its .env names its database, its data/ holds the packages.
IPTORCH_HOME = Path(os.environ.get("IPTORCH_HOME", "/home/nimrod_rotem/patent-v3-nimo7"))
IPTORCH_LOCAL = os.environ.get("IPTORCH_LOCAL_URL", "http://127.0.0.1:8647").rstrip("/")
IPTORCH_PUBLIC = os.environ.get("IPTORCH_PUBLIC_URL", "https://iptorch.com").rstrip("/")
#  Whose packages are copied: iptorch.com accounts, by email.
ACCOUNTS = tuple(a.strip().lower() for a in os.environ.get(
    "IPTORCH_PACKAGE_ACCOUNTS",
    "nimo@rotem.ai,nimo@grabo.com,ahmed@intellentpatents.com").split(",") if a.strip())
#  Whose DOCKET shows them: accounts on this app. A guest working on that docket sees them too,
#  because a guest is handed the owner's docket (see observations._user).
DOCKET_OWNERS = tuple(a.strip().lower() for a in os.environ.get(
    "IPTORCH_DOCKET_OWNERS", os.environ.get("OBSERVATIONS_OWNER_EMAIL", "nimo@rotem.ai")).split(",")
    if a.strip())
ARCHIVE = Path(os.environ.get("IPTORCH_ARCHIVE_DIR",
                              str(_HERE.parent / "data" / "observations" / "iptorch")))
SYNC_SECONDS = int(os.environ.get("IPTORCH_SYNC_SECONDS", "300"))
#  A page load asks for a sync when the last one is older than this, so a package built a minute
#  ago is on the row the next time somebody looks, without waiting for the timer.
KICK_SECONDS = int(os.environ.get("IPTORCH_KICK_SECONDS", "60"))
FETCH_TIMEOUT = float(os.environ.get("IPTORCH_FETCH_TIMEOUT", "180"))

INSTRUMENT_LABEL = {
    "us_1290": "US third-party preissuance submission (37 CFR 1.290)",
    "us_1291": "US protest (37 CFR 1.291)",
    "us_301": "US citation of prior art (35 U.S.C. 301)",
    "ep_obs": "EP third-party observations (Art. 115 EPC)",
    "ep_opposition": "EP opposition (Art. 99 EPC)",
    "de_einspruch": "DE Einspruch (opposition, § 59 PatG)",
    "de_43_3": "DE Einwendung (§ 43(3) PatG)",
}
FORUM_LABEL = {"US": "USPTO", "EP": "EPO", "DE": "DPMA", "WO": "WIPO"}
#  The filing app's store for each pipeline a SENT.json names (see observation_links.STORES).
FILING_STORE = {pipeline: sub for sub, pipeline, _ in observation_links.STORES}

_SLUG_OK = re.compile(r"^[A-Za-z0-9._-]{3,120}$")
_STAMP_OK = re.compile(r"^[0-9A-Za-z-]{8,40}$")


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------

def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _parse_when(value):
    """An ISO string or an epoch -> aware UTC datetime, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if hasattr(value, "astimezone"):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def _stamp(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _env_file(path):
    out = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------------------------
# reading iptorch
# ---------------------------------------------------------------------------------------------

def owned_searches(accounts=ACCOUNTS):
    """Every iptorch search the named accounts own. -> [{slug, email, subject, title, created}]

    Read-only, on a connection Postgres itself holds read-only. The credentials are iptorch's own,
    read from its .env each time, so a rotated password there is never stale here.
    """
    import psycopg
    from psycopg.rows import dict_row
    env = _env_file(IPTORCH_HOME / ".env")
    if not env.get("PGHOST"):
        raise RuntimeError("no PGHOST in %s" % (IPTORCH_HOME / ".env"))
    path = re.sub(r"[^a-zA-Z0-9_,]", "", env.get("PG_SEARCH_PATH") or "nimo7,public")
    with psycopg.connect(host=env["PGHOST"], port=env.get("PGPORT") or 5432,
                         dbname=env.get("PGDATABASE"), user=env.get("PGUSER"),
                         password=env.get("PGPASSWORD"), row_factory=dict_row, connect_timeout=10,
                         options="-c search_path=%s -c default_transaction_read_only=on "
                                 "-c statement_timeout=20000" % path) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.slug, s.subject, s.title, s.query, s.created_at, lower(u.email) AS email
                     FROM app_saved_searches s JOIN app_users u ON u.id = s.user_id
                    WHERE lower(u.email) = ANY(%s)
                    ORDER BY s.created_at""", (list(accounts),))
            rows = cur.fetchall()
    return [{"slug": r["slug"], "email": r["email"], "subject": r.get("subject") or "",
             "title": r.get("title") or r.get("query") or "",
             "created": _iso(_parse_when(r.get("created_at")))} for r in rows]


def active_slugs():
    """Slugs iptorch is building right now. A build in progress is never copied half-done."""
    try:
        with urllib.request.urlopen(IPTORCH_LOCAL + "/api/internal/active-runs", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return set(data.get("slugs") or []), True
    except Exception:
        return set(), False


def build_of(slug, concise_dir=None):
    """The finished build a package folder holds right now, or None.

    `built_at` is the build's own clock: BUILT.json on every build since 2026-09-11, PACKET.json's
    stamp on the older ones. A folder without either has no finished build in it (a rebuild
    clears it first), and a JOB.json still saying "running" is a build that is not over.
    """
    d = (Path(concise_dir) if concise_dir else IPTORCH_HOME / "data" / "reports" / "concise") / slug
    if not d.is_dir():
        return None
    built = _read(d / "BUILT.json") or {}
    packet = _read(d / "PACKET.json") or {}
    job = _read(d / "JOB.json") or {}
    stamps = [_parse_when(built.get("built_at")), _parse_when(packet.get("rebuilt_at")),
              _parse_when(packet.get("built_at"))]
    stamps = [s for s in stamps if s]
    if not stamps:
        return None
    return {
        "slug": slug,
        "built_at": max(stamps),
        "running": str(job.get("state") or "").lower() in ("running", "queued", "starting"),
        "instrument": str(built.get("instrument") or packet.get("instrument")
                          or job.get("instrument") or ""),
        "label": str(packet.get("label") or ""),
        "forum": str(packet.get("forum") or packet.get("office") or ""),
        "docs": int(built.get("docs") or packet.get("shipped") or 0),
        "pipeline": str(built.get("pipeline") or ("fourphase" if (d / "FOURPHASE" / "final").is_dir()
                                                  else "classic")),
        "verdict": str(job.get("verdict") or "")[:400],
        "sent": _read(d / "SENT.json") or {},
    }


def _subject(slug, reports=None):
    """The publication a search was run from and its title, from iptorch's report meta."""
    reports = Path(reports) if reports else IPTORCH_HOME / "data" / "reports"
    meta = _read(reports / ("%s.meta.json" % slug)) or {}
    inp = meta.get("input") if isinstance(meta.get("input"), dict) else {}
    stats = _read(reports / ("%s.stats.json" % slug)) or {}
    pub = (inp.get("publication_number") or stats.get("subject_pub") or "")
    title = inp.get("title") or stats.get("subject_title") or ""
    return str(pub).strip(), str(title).strip()


def fetch_zip(slug):
    """The package as iptorch serves it for download. -> (bytes, headers) or raises."""
    req = urllib.request.Request(IPTORCH_LOCAL + "/report/%s/concise.zip" % slug,
                                 headers={"User-Agent": "rotem-docket-sync/1"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        body = r.read()
        headers = {k.lower(): v for k, v in r.headers.items()}
    if not body.startswith(b"PK"):
        raise RuntimeError("not a zip (%s bytes, %s)" % (len(body), headers.get("content-type")))
    return body, headers


def _filename(headers, fallback):
    m = re.search(r'filename="?([^";]+)"?', headers.get("content-disposition") or "")
    name = m.group(1) if m else fallback
    return re.sub(r"[^A-Za-z0-9._() -]", "_", name)[:160]


# ---------------------------------------------------------------------------------------------
# the archive
# ---------------------------------------------------------------------------------------------

def versions(slug, archive=None):
    """Every copy kept for one slug, oldest first."""
    d = (Path(archive) if archive else ARCHIVE) / slug
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            meta = _read(p)
            if isinstance(meta, dict) and (d / meta.get("file", "")).is_file():
                out.append(meta)
    out.sort(key=lambda m: m.get("built_at") or "")
    return out


def _save(slug, stamp, body, meta, archive):
    d = archive / slug
    d.mkdir(parents=True, exist_ok=True)
    zpath = d / ("%s.zip" % stamp)
    tmp = zpath.with_suffix(".zip.tmp")
    tmp.write_bytes(body)
    os.replace(tmp, zpath)
    meta.update(file=zpath.name, bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                archived_at=_iso(datetime.datetime.now(datetime.timezone.utc)))
    _write_json(d / ("%s.json" % stamp), meta)
    return meta


def _mark_sent(slug, sent, archive, filing_data):
    """Say which kept copy was the one handed to the filing app.

    The copy built last BEFORE the hand-off is the one that went. When no copy that old exists,
    because the package was sent before this sync existed and rebuilt afterwards, the filing app's
    own intake copy is kept instead: it is the zip exactly as it arrived there.
    """
    events = [sent] + [h for h in (sent.get("history") or []) if isinstance(h, dict)]
    changed = 0
    for ev in events:
        at = _parse_when(ev.get("at"))
        if not at or ev.get("error"):
            continue
        info = {"at": _iso(at), "by": str(ev.get("by") or ""), "id": str(ev.get("id") or sent.get("id") or ""),
                "url": str(ev.get("url") or sent.get("url") or ""),
                "pipeline": str(ev.get("pipeline") or sent.get("pipeline") or ""),
                "kind": str(ev.get("filing_kind") or sent.get("filing_kind") or "")}
        vs = versions(slug, archive)
        if any((v.get("sent") or {}).get("at") == info["at"] for v in vs):
            continue
        before = [v for v in vs if (v.get("built_at") or "") <= info["at"] and v.get("origin") == "iptorch"]
        if before:
            v = before[-1]
            v["sent"] = info
            _write_json(archive / slug / v["file"].replace(".zip", ".json"), v)
            changed += 1
            continue
        store = FILING_STORE.get(info["pipeline"])
        intake = (Path(filing_data) / store / info["id"] / "in") if (store and info["id"]) else None
        zips = sorted(intake.glob("*.zip")) if intake and intake.is_dir() else []
        if not zips:
            continue
        body = zips[0].read_bytes()
        stamp = "sent-" + _stamp(at)
        inst = str(sent.get("instrument") or "")
        meta = {"id": "%s@%s" % (slug, stamp), "slug": slug, "stamp": stamp, "origin": "handed-copy",
                "built_at": info["at"], "download_name": zips[0].name, "sent": info,
                "instrument": inst, "forum": inst.split("_")[0].upper() if inst else "",
                "note": "The zip as the filing app received it. iptorch has rebuilt this package "
                        "since, so this copy is the only one of the version that was handed over."}
        _save(slug, stamp, body, meta, archive)
        changed += 1
    return changed


_SYNC_LOCK = threading.Lock()
_STATE = {"at": 0.0, "ok": None, "error": "", "added": 0, "seen": 0, "running": False}


def sync(archive=None, concise_dir=None, reports=None, filing_data=None, searches=None,
         fetch=None, active=None):
    """Copy every finished build not yet kept. Safe to call from any thread or process.

    Returns a summary. One process at a time holds a lock file beside the archive, so two
    gunicorn workers or a timer and a page load never download the same zip twice.
    """
    archive = Path(archive) if archive else ARCHIVE
    filing_data = Path(filing_data) if filing_data else observation_links.FILING_DATA
    fetch = fetch or fetch_zip
    archive.mkdir(parents=True, exist_ok=True)
    summary = {"seen": 0, "added": 0, "skipped_running": 0, "errors": []}
    with open(archive / ".lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            summary["busy"] = True
            return summary
        rows = owned_searches() if searches is None else searches
        if active is None:
            active, active_ok = active_slugs()
        else:
            active_ok = True
        index = {}
        for row in rows:
            slug = str(row.get("slug") or "")
            if not _SLUG_OK.match(slug):
                continue
            b = build_of(slug, concise_dir)
            if not b:
                continue
            summary["seen"] += 1
            pub, title = _subject(slug, reports)
            pub = pub or row.get("subject") or ""
            index[slug] = {"account": row.get("email") or "", "search_created": row.get("created") or "",
                           "subject": pub, "subject_title": title, "search_title": row.get("title") or ""}
            #  NOT WHILE IT IS BEING BUILT. iptorch's own list of live runs is the authority; a
            #  JOB.json left at "running" by a crashed worker is only believed when that list
            #  cannot be read.
            if slug in active or (b["running"] and not active_ok):
                summary["skipped_running"] += 1
                continue
            stamp = _stamp(b["built_at"])
            have = versions(slug, archive)
            if not any(v.get("stamp") == stamp for v in have):
                try:
                    body, headers = fetch(slug)
                except Exception as exc:
                    summary["errors"].append("%s: %s" % (slug, str(exc)[:200]))
                    continue
                digest = hashlib.sha256(body).hexdigest()
                if not any(v.get("sha256") == digest for v in have):
                    inst = headers.get("x-packet-instrument") or b["instrument"]
                    forum = headers.get("x-packet-forum") or b["forum"]
                    meta = {"id": "%s@%s" % (slug, stamp), "slug": slug, "stamp": stamp,
                            "origin": "iptorch", "built_at": _iso(b["built_at"]),
                            "instrument": inst, "forum": forum, "docs": b["docs"],
                            "pipeline": b["pipeline"], "label": b["label"],
                            "qa": headers.get("x-packet-qa") or "",
                            "qa_notes": headers.get("x-packet-qa-notes") or "",
                            "verdict": b["verdict"],
                            "download_name": _filename(headers, "%s.zip" % slug)}
                    meta.update(index[slug])
                    _save(slug, stamp, body, meta, archive)
                    summary["added"] += 1
            if b["sent"]:
                try:
                    summary["added"] += _mark_sent(slug, b["sent"], archive, filing_data)
                except Exception as exc:
                    summary["errors"].append("%s sent: %s" % (slug, str(exc)[:200]))
        #  Who owns each slug and what it was run on, for copies that predate a field.
        _write_json(archive / "index.json", index)
    return summary


def run_sync():
    """One sync, recorded for the page. Never raises."""
    with _SYNC_LOCK:
        if _STATE["running"]:
            return dict(_STATE)
        _STATE["running"] = True
    try:
        s = sync()
        _STATE.update(ok=not s.get("errors"), error="; ".join(s.get("errors") or [])[:600],
                      added=s.get("added", 0), seen=s.get("seen", 0))
    except Exception as exc:
        traceback.print_exc()
        _STATE.update(ok=False, error=str(exc)[:300])
    finally:
        _STATE["at"] = time.time()
        _STATE["running"] = False
    return dict(_STATE)


def kick():
    """Ask for a sync in the background when the last one is older than KICK_SECONDS."""
    if _STATE["running"] or time.time() - _STATE["at"] < KICK_SECONDS:
        return False
    threading.Thread(target=run_sync, name="iptorch-sync-kick", daemon=True).start()
    return True


def status():
    s = dict(_STATE)
    s["at_iso"] = _iso(_parse_when(s["at"])) if s["at"] else ""
    return s


_LOOP = {"started": False}


def start_background():
    """The timer. Started once per process; the lock file keeps two processes from colliding."""
    if _LOOP["started"] or SYNC_SECONDS <= 0:
        return
    _LOOP["started"] = True

    def loop():
        time.sleep(20)
        while True:
            run_sync()
            time.sleep(SYNC_SECONDS)
    threading.Thread(target=loop, name="iptorch-sync", daemon=True).start()


# ---------------------------------------------------------------------------------------------
# onto the docket
# ---------------------------------------------------------------------------------------------

_KEPT = {"sig": None, "rows": []}
_KEPT_LOCK = threading.Lock()


def kept(archive=None):
    """Every kept copy, newest first, each with the keys its row is found by."""
    archive = Path(archive) if archive else ARCHIVE
    metas = sorted(archive.glob("*/*.json")) if archive.is_dir() else []
    sig = tuple((str(p), p.stat().st_mtime) for p in metas if p.exists())
    if archive == ARCHIVE:
        with _KEPT_LOCK:
            if _KEPT["sig"] == sig:
                return [dict(r) for r in _KEPT["rows"]]
    index = _read(archive / "index.json") or {}
    rows = []
    for p in metas:
        meta = _read(p)
        if not isinstance(meta, dict) or not (p.parent / str(meta.get("file") or "")).is_file():
            continue
        slug = meta.get("slug") or p.parent.name
        for k, v in (index.get(slug) or {}).items():
            meta.setdefault(k, v)
        inst = meta.get("instrument") or ""
        forum = (meta.get("forum") or inst.split("_")[0]).upper()
        meta["instrument_label"] = (INSTRUMENT_LABEL.get(inst) or meta.get("label")
                                    or ("%s package" % FORUM_LABEL.get(forum, forum)).strip()
                                    or "Package")
        meta["office"] = FORUM_LABEL.get(forum, forum)
        meta["report_url"] = "%s/report/%s" % (IPTORCH_PUBLIC, slug)
        meta["history_url"] = "%s/history" % IPTORCH_PUBLIC
        meta["_pubs"] = observation_links.pub_keys(meta.get("subject"))
        rows.append(meta)
    rows.sort(key=lambda m: m.get("built_at") or "", reverse=True)
    if archive == ARCHIVE:
        with _KEPT_LOCK:
            _KEPT["sig"], _KEPT["rows"] = sig, [dict(r) for r in rows]
    return rows


def visible_to(user):
    return bool(user) and str(user.get("email") or "").lower() in DOCKET_OWNERS


def attach(cases, rows=None):
    """Pin each kept package to the row whose publication it was run from. Sets `iptorch`."""
    rows = kept() if rows is None else rows
    by_pub = {}
    for r in rows:
        for k in r["_pubs"]:
            by_pub.setdefault(k, []).append(r)
    for c in cases:
        pubs, _ = observation_links.case_keys(c)
        found, seen = [], set()
        for k in sorted(pubs):
            for r in by_pub.get(k, []):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    found.append({k2: v for k2, v in r.items() if not k2.startswith("_")})
        found.sort(key=lambda r: r.get("built_at") or "", reverse=True)
        c["iptorch"] = found
    return cases


def unmatched(all_keys, rows=None):
    """Kept packages whose patent is on none of the owner's docket rows, newest first."""
    rows = kept() if rows is None else rows
    return [{k: v for k, v in r.items() if not k.startswith("_")}
            for r in rows if not (r["_pubs"] & all_keys)]


def zip_path(slug, stamp, archive=None):
    """The kept zip for one version, or None. Both parts are checked before touching the disk."""
    if not _SLUG_OK.match(slug or "") or not _STAMP_OK.match(stamp or ""):
        return None
    archive = Path(archive) if archive else ARCHIVE
    meta = _read(archive / slug / ("%s.json" % stamp))
    if not isinstance(meta, dict):
        return None
    p = archive / slug / str(meta.get("file") or "")
    return (p, meta) if p.is_file() and p.parent == archive / slug else None
