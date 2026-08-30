"""Worldwide patent family (INPADOC) -> Google-Patents-style year -> jurisdiction timeline.

WHY
---
A result card should show the geographic + temporal spread of its patent family the way
Google Patents' "Worldwide applications" strip does, e.g. for US11999030B2:

  2018 IL · 2019 US ES CN EP CN AU CN CN CN DE WO CN · 2022 US · 2023 US · 2024 US US · 2025 US

The local DB is NOT sufficient: `simple_family_id` only covers family members that happen to
be inside our corpus, and `extended_family_id` is NULL for every row. The authoritative
worldwide family comes from EPO OPS INPADOC family
(`/rest-services/family/publication/docdb/<CC.NUMBER.KIND>`), supplemented/verified by a Lens
family when one is already on the search response, and finally falling back to the corpus
`simple_family_id` members (marked "corpus-only") when neither is available.

WHAT GOOGLE GROUPS BY
---------------------
Google's "Worldwide applications" reflects APPLICATION FILINGS, not publications: multiple
publications of one application (an A1 and its B2 grant) collapse to a single entry, while
genuinely distinct national filings stay (so a year can legitimately show "CN CN CN"). We
therefore group each family member by its APPLICATION filing year, label it with the
application's country, and de-duplicate on the application docdb id (country+number). Verified
against the live INPADOC family for US11999030B2: this reproduces Google's shape (IL anchor in
2018, a ~12-member 2019 cluster dominated by CN, then a tail of US continuations).

QUOTA / CACHING
---------------
Family XML is small text but still goes through `ops._ops_get`, which enforces the shared EPO
OPS 4 GB/week byte budget and the throttle. A family rarely changes, so every result is cached
on disk forever at `data/families/<pub>.json` and never refetched.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import ops
from config import DATA

FAMILY_CACHE = DATA / "families"

_PUB_RE = re.compile(r"^[A-Za-z]{2}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_SPLIT_RE = re.compile(r"^([A-Z]{2})[- ]?(\d+)[- ]?([A-Z]\d*)?$")


def _pubkey(pub: str) -> str:
    """Only a clean publication number may name a cache file (traversal defense-in-depth)."""
    if not pub or len(str(pub)) > 40 or not _PUB_RE.match(str(pub)):
        raise ValueError(f"unsafe publication key: {pub!r}")
    return str(pub)


def to_docdb(pubnum: str):
    """'US-11999030-B2' -> 'US.11999030.B2' (the form the family endpoint wants), plus the
    kindless 'US.11999030' fallback. Returns (with_kind, kindless)."""
    n = (pubnum or "").replace("-", "").replace(" ", "").upper()
    m = _SPLIT_RE.match(n)
    if not m:
        return n, n
    cc, num, kind = m.group(1), m.group(2), m.group(3)
    kindless = f"{cc}.{num}"
    return (f"{cc}.{num}.{kind}" if kind else kindless), kindless


# ---- XML parsing (namespace-agnostic via local-name) ---------------------------------------
def _lname(tag):
    return tag.rsplit("}", 1)[-1]


def _children(el, name):
    """Direct-ish descendants by local-name (iter over the whole subtree)."""
    return [x for x in el.iter() if _lname(x.tag) == name]


def _docdb(ref):
    """Pull the docdb {country, number, kind, date} out of a *-reference element."""
    if ref is None:
        return None
    for d in _children(ref, "document-id"):
        if (d.get("document-id-type") or "").lower() != "docdb":
            continue
        get = lambda t: (_children(d, t)[0].text if _children(d, t) else None)  # noqa: E731
        return {"country": get("country"), "number": get("doc-number"),
                "kind": get("kind"), "date": (get("date") or "").strip() or None}
    return None


def _first_child(member, name):
    for x in member:
        if _lname(x.tag) == name:
            return x
    return None


def _year(datestr):
    m = re.match(r"\s*(\d{4})", datestr or "")
    y = m.group(1) if m else None
    return y if (y and y != "0000" and y != "0001") else None


def parse_family_members(xml_bytes) -> list[dict]:
    """-> [{pub, country, kind, pub_date, app_country, app_number, app_kind, app_date, prio_date}].

    One dict per <ops:family-member>. Publication + application + earliest priority are read
    from the docdb document-ids (see module docstring for the wire shape)."""
    root = ET.fromstring(xml_bytes)
    out = []
    for m in root.iter():
        if _lname(m.tag) != "family-member":
            continue
        pub = _docdb(_first_child(m, "publication-reference")) or {}
        app = _docdb(_first_child(m, "application-reference")) or {}
        # earliest priority date across all priority-claim entries
        prio = None
        for pc in m:
            if _lname(pc.tag) != "priority-claim":
                continue
            d = _docdb(pc) or {}
            if d.get("date") and (prio is None or d["date"] < prio):
                prio = d["date"]
        pubnum = None
        if pub.get("country") and pub.get("number"):
            pubnum = f"{pub['country']}{pub['number']}" + (pub.get("kind") or "")
        out.append({
            "pub": pubnum,
            "country": pub.get("country"),
            "kind": pub.get("kind"),
            "pub_date": pub.get("date"),
            "app_country": app.get("country"),
            "app_number": app.get("number"),
            "app_kind": app.get("kind"),
            "app_date": app.get("date"),
            "prio_date": prio,
        })
    return out


# ---- timeline grouping (pure, unit-tested) -------------------------------------------------
def group_timeline(members: list[dict]) -> list[dict]:
    """Group family members into Google's 'Worldwide applications' shape.

    Grouped by APPLICATION filing year -> application country, chronological. De-duplicated on
    the application docdb id (country+number+kind) so multiple publications of one application
    collapse to a single entry, while genuinely distinct national filings (e.g. six separate CN
    applications) are preserved. Members with no application info fall back to their publication
    country/date so nothing is silently dropped.

    -> [{"year": "2019", "codes": [{"cc": "CN", "pub": "CN...", "app": "CN.123", "date": "2019-.."}]}]
       in ascending year order; codes within a year are ordered by filing date.
    """
    seen = set()
    rows = []  # (year, sort_date, cc, entry)
    for mem in members:
        cc = mem.get("app_country") or mem.get("country")
        num = mem.get("app_number") or (mem.get("pub") or "")
        kind = mem.get("app_kind") or ""
        date = mem.get("app_date") or mem.get("pub_date") or mem.get("prio_date")
        year = _year(date)
        if not cc or not year:
            continue
        dedup = (cc, str(num), str(kind))
        if dedup in seen:
            continue
        seen.add(dedup)
        pub = mem.get("pub") or (f"{cc}{num}" if num else cc)
        app_id = f"{cc}.{num}" if num else None
        iso = None
        if date and len(date) >= 8:
            iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        rows.append((year, date or year, cc, {"cc": cc, "pub": pub, "app": app_id, "date": iso}))
    rows.sort(key=lambda r: (r[0], r[1] or ""))
    out, cur = [], None
    for year, _sd, _cc, entry in rows:
        if cur is None or cur["year"] != year:
            cur = {"year": year, "codes": []}
            out.append(cur)
        cur["codes"].append(entry)
    return out


def family_summary(timeline: list[dict]) -> dict:
    """-> {'n_members': total codes, 'n_jurisdictions': distinct country codes}."""
    n = sum(len(g["codes"]) for g in timeline)
    juris = {c["cc"] for g in timeline for c in g["codes"]}
    return {"n_members": n, "n_jurisdictions": len(juris)}


def _timeline_result(pub, timeline, source, partial):
    summ = family_summary(timeline)
    return {"pub": pub, "source": source, "partial": bool(partial),
            "timeline": timeline, **summ}


def corpus_timeline(pub, rows: list[dict]) -> dict:
    """Build a (partial) 'corpus-only' timeline from local rows we already have.

    `rows`: [{country, date}] — the card's own row plus its corpus simple-family members. Used
    as a zero-cost baseline and as the final fallback when neither OPS nor Lens is available.
    Marked partial=True / source='corpus'.
    """
    members = []
    for r in rows:
        d = r.get("date")
        d = (str(d).replace("-", "")[:8] or None) if d else None
        members.append({"pub": r.get("pub"), "country": r.get("country"),
                        "app_country": r.get("country"), "app_number": r.get("pub"),
                        "app_date": d, "pub_date": d})
    return _timeline_result(pub, group_timeline(members), "corpus", True)


def _lens_members(lens_family):
    """Lens family_members -> the member shape group_timeline expects."""
    out = []
    for m in (lens_family or []):
        d = (m.get("date") or "").replace("-", "")[:8] or None
        out.append({"pub": m.get("pub"), "country": m.get("country"), "kind": m.get("kind"),
                    "app_country": m.get("country"), "app_number": m.get("pub"),
                    "app_date": d, "pub_date": d})
    return out


# ---- top-level fetch (cached) --------------------------------------------------------------
def _cache_path(pub):
    return FAMILY_CACHE / f"{_pubkey(pub)}.json"


def load_cached(pub):
    try:
        p = _cache_path(pub)
    except ValueError:
        return None
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _save(pub, result):
    try:
        p = _cache_path(pub)
    except ValueError:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, default=str))
    except Exception:
        pass


def fetch_family(pub, mock=False, lens_family=None, corpus_rows=None, force=False) -> dict:
    """Authoritative worldwide family timeline for a publication, cached on disk forever.

    Order of preference: EPO OPS INPADOC family (authoritative) -> Lens family (if supplied on
    the search response) -> local corpus simple-family (partial, 'corpus-only'). Returns an empty
    timeline (never raises) when nothing resolves, so the UI can render 'family: —' quietly.
    """
    if not force:
        cached = load_cached(pub)
        if cached is not None:
            return cached

    members = []
    source = None
    # 1. EPO OPS INPADOC family (authoritative, small text, budget-guarded, cached).
    if not mock and ops.have_creds():
        try:
            with_kind, kindless = to_docdb(pub)
            st, body, _ = ops._ops_get(f"family/publication/docdb/{with_kind}")
            if st == 404 and with_kind != kindless:
                st, body, _ = ops._ops_get(f"family/publication/docdb/{kindless}")
            if st == 200 and body:
                members = parse_family_members(body)
                if members:
                    source = "ops"
        except ops.OpsBudgetExceeded:
            members = []
        except Exception:
            members = []

    # 2. Lens family already on the search response — supplement/verify, no second network call.
    if not members and lens_family:
        members = _lens_members(lens_family)
        if members:
            source = "lens"

    if members:
        result = _timeline_result(pub, group_timeline(members), source, partial=False)
        _save(pub, result)
        return result

    # 3. Fall back to the local corpus simple-family (partial, corpus-only).
    if corpus_rows:
        result = corpus_timeline(pub, corpus_rows)
        _save(pub, result)
        return result

    # Nothing resolved — cache the empty answer so we do not retry OPS on every card open.
    result = _timeline_result(pub, [], "none", partial=True)
    _save(pub, result)
    return result


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:] or ["US-11999030-B2"]:
        r = fetch_family(p)
        print(f"{p}: source={r['source']} family of {r['n_members']} in "
              f"{r['n_jurisdictions']} jurisdictions (partial={r['partial']})")
        for g in r["timeline"]:
            print("  ", g["year"], " ".join(c["cc"] for c in g["codes"]))
