"""Lens.org patent API client — authoritative biblio, abstract, claims, INPADOC legal status and
patent family (jurisdictions) for a reference.

Zero-step activation (the M6 OPS pattern): the whole app runs without it; the moment a
`LENS_TOKEN` lands in `.env`, `available()` flips True and enrichment starts folding Lens legal
status + family into the display. Without a token every call returns None and callers fall back to
the SerpApi/DB path, so nothing breaks.

Get a token: lens.org → sign in → "API & data" → issue a token (free 14-day trial or an
institutional/commercial subscription). Put it in `.env` as `LENS_TOKEN=...`.

API: POST https://api.lens.org/patent/search  (Bearer token).  Docs: https://docs.api.lens.org/
"""
from __future__ import annotations
import os, re, json
import requests

TOKEN = os.environ.get("LENS_TOKEN", "").strip()
BASE = "https://api.lens.org/patent"
TIMEOUT = 30

_PUB_RE = re.compile(r"^([A-Za-z]{2})-?([A-Za-z0-9]+?)-?([A-Za-z]\d?)?$")


def available():
    return bool(TOKEN)


def _parts(pub):
    """Split 'US-10815075-B2' -> ('US','10815075','B2')."""
    m = _PUB_RE.match((pub or "").strip())
    if not m:
        return None, None, None
    return m.group(1).upper(), m.group(2), (m.group(3) or "").upper() or None


def _post(path, body):
    if not TOKEN:
        return None
    try:
        r = requests.post(f"{BASE}{path}",
                          headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                          data=json.dumps(body), timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (401, 403):
            raise LensAuthError(f"Lens auth failed ({r.status_code}); token invalid/expired.")
    except requests.RequestException:
        return None
    return None


class LensAuthError(RuntimeError):
    pass


def search_by_pub(pub, size=3):
    """Query Lens for a publication number. Returns the raw JSON (or None)."""
    jur, num, kind = _parts(pub)
    if not (jur and num):
        return None
    must = [{"term": {"jurisdiction": jur}}, {"term": {"doc_number": num}}]
    if kind:
        must.append({"term": {"kind": kind}})
    body = {
        "query": {"bool": {"must": must}},
        "size": size,
        "include": ["lens_id", "jurisdiction", "doc_number", "kind", "date_published",
                    "biblio", "abstract", "claims", "legal_status", "families"],
    }
    return _post("/search", body)


def _first_hit(raw):
    for key in ("data", "results", "hits"):
        v = (raw or {}).get(key)
        if isinstance(v, list) and v:
            return v[0]
    return None


def normalize(pub, raw):
    """Fold a Lens hit into the fields the display cares about: legal status + family + text.
    Defensive against schema variance (Lens nests deeply and versions its shapes)."""
    hit = _first_hit(raw)
    if not hit:
        return None
    biblio = hit.get("biblio") or {}
    # legal status
    ls = hit.get("legal_status") or {}
    status = ls.get("patent_status") or ls.get("legal_status")            # e.g. "Active"/"Granted"
    term_date = ls.get("anticipated_term_date") or ls.get("calculation_expiry_date")
    # abstract (list of {lang,text})
    abstract = None
    for a in (hit.get("abstract") or []):
        if isinstance(a, dict) and a.get("text"):
            abstract = a["text"]; break
    # claims (Lens nests claims[].claims[].claim_text)
    claims = []
    for group in (hit.get("claims") or []):
        for cl in (group.get("claims") or []):
            t = cl.get("claim_text")
            if isinstance(t, list):
                t = " ".join(t)
            if t:
                claims.append(t)
    # family members -> pub numbers with jurisdiction/kind
    members = []
    fam = (hit.get("families") or {}).get("simple_family") or {}
    for mem in (fam.get("members") or []):
        did = mem.get("document_id") or {}
        j, dn, k = did.get("jurisdiction"), did.get("doc_number"), did.get("kind")
        if j and dn:
            members.append({"pub": f"{j}-{dn}" + (f"-{k}" if k else ""), "country": j, "kind": k,
                            "date": did.get("date")})
    return {
        "lens_id": hit.get("lens_id"),
        "legal_status": status,
        "anticipated_term_date": term_date,
        "abstract": abstract,
        "claims": claims or None,
        "family_members": members,
        "granted": bool(status and str(status).lower() in ("granted", "active")),
    }


def fetch(pub):
    """One-call enrichment: publication number -> normalized Lens fields, or None if unavailable."""
    if not available():
        return None
    raw = search_by_pub(pub)
    if not raw:
        return None
    return normalize(pub, raw)


if __name__ == "__main__":
    import sys
    if not available():
        print("LENS_TOKEN not set — Lens is dormant (add it to .env to activate).")
        raise SystemExit(0)
    for p in sys.argv[1:] or ["US-10815075-B2"]:
        d = fetch(p)
        print(p, "->", {k: (v if not isinstance(v, list) else f"[{len(v)}]") for k, v in (d or {}).items()})
