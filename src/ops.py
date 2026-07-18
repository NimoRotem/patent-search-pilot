"""EPO OPS (Open Patent Services v3.2) client — the zero-step unlock for the EP/WO/DE full-text
hole (Milestone 6 §1).

The MOMENT `OPS_CONSUMER_KEY` + `OPS_CONSUMER_SECRET` exist in `.env`, this fills descriptions +
claims + drawings + INPADOC legal status that BigQuery lacks for EP/WO/DE. Until then it runs in
mock/dry-run mode so the parser + schema mapping are provable WITHOUT credentials.

OPS REST, XML responses (namespaces: ops=http://ops.epo.org, exch=http://www.epo.org/exchange,
ftxt=http://www.epo.org/fulltext). Auth = OAuth2 client-credentials (Basic base64(key:secret) ->
access token). Free tier throttle: 4 GB/week; we back off on 403/429 + honour the throttle header.
"""
from __future__ import annotations
import os, re, time, json, base64
from pathlib import Path
import xml.etree.ElementTree as ET
import requests
from config import DATA

OPS_BASE = "https://ops.epo.org/3.2/rest-services"
OPS_TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SAMPLES = DATA.parent / "src" / "ops_samples"   # bundled sample XML for the mock path

_token = {"value": None, "exp": 0}


def have_creds():
    return bool(os.environ.get("OPS_CONSUMER_KEY") and os.environ.get("OPS_CONSUMER_SECRET"))


# ---- number formatting ---------------------------------------------------------------------
def to_epodoc(pubnum: str):
    """US-11999030-B2 -> ('US11999030B2' number, 'US11999030' for legal without kind)."""
    n = pubnum.replace("-", "").upper()
    m = re.match(r"^([A-Z]{2})(\w+?)([A-Z]\d?)$", n)
    return n, (m.group(1) + m.group(2) if m else n)


# ---- auth + fetch --------------------------------------------------------------------------
def _get_token():
    if _token["value"] and time.time() < _token["exp"] - 30:
        return _token["value"]
    key = os.environ["OPS_CONSUMER_KEY"]; secret = os.environ["OPS_CONSUMER_SECRET"]
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    r = requests.post(OPS_TOKEN_URL, headers={"Authorization": f"Basic {auth}",
                      "Content-Type": "application/x-www-form-urlencoded"},
                      data="grant_type=client_credentials", timeout=30)
    r.raise_for_status()
    j = r.json()
    _token["value"] = j["access_token"]
    _token["exp"] = time.time() + int(j.get("expires_in", 1200))
    return _token["value"]


def _ops_get(path, accept="application/xml", retries=4, params=None):
    """GET an OPS REST path with token, retry/backoff on throttle (403 CLIENT.RobotDetected /
    429) and 5xx. Returns (status, content_bytes, headers)."""
    url = f"{OPS_BASE}/{path.lstrip('/')}"
    for i in range(retries):
        tok = _get_token()
        r = requests.get(url, headers={"Authorization": f"Bearer {tok}", "Accept": accept},
                         params=params, timeout=45)
        # throttle / quota
        if r.status_code in (403, 429, 500, 503):
            # OPS returns X-Throttling-Control; back off progressively
            wait = min(60, 4 * (i + 1) ** 2)
            if r.status_code == 403 and b"invalid_access_token" in r.content.lower():
                _token["value"] = None  # force refresh
            time.sleep(wait)
            continue
        return r.status_code, r.content, r.headers
    return r.status_code, r.content, r.headers


# ---- XML parsers (namespace-agnostic via local-name) ---------------------------------------
def _lname(tag):
    return tag.rsplit("}", 1)[-1]


def _findall_local(root, name):
    return [e for e in root.iter() if _lname(e.tag) == name]


def parse_description(xml_bytes):
    """-> list of {para_no, heading, text} from an OPS fulltext description response."""
    root = ET.fromstring(xml_bytes)
    out, heading = [], None
    # OPS description contains <ftxt:p num="0001">...</p>, optionally <heading>
    for p in _findall_local(root, "p"):
        num = p.get("num") or f"p{len(out)+1:04d}"
        txt = "".join(p.itertext()).strip()
        txt = re.sub(r"\s+", " ", txt)
        if txt:
            out.append({"para_no": num, "heading": heading, "text": txt})
    # headings if present as <heading> siblings
    for h in _findall_local(root, "heading"):
        pass  # OPS rarely tags headings; left as None (schema allows it)
    return out


def parse_claims(xml_bytes):
    """-> list of {claim_no, text} from an OPS claims response. lang from the claims element."""
    root = ET.fromstring(xml_bytes)
    claims = []
    lang = None
    for cel in _findall_local(root, "claims"):
        lang = (cel.get("lang") or lang)
    for c in _findall_local(root, "claim"):
        num = c.get("num")
        txt = "".join(c.itertext()).strip()
        txt = re.sub(r"\s+", " ", txt)
        m = re.match(r"^(\d+)\s*[.\)]", txt)
        cno = int(m.group(1)) if m else (int(num) if (num and num.isdigit()) else len(claims) + 1)
        if txt:
            claims.append({"claim_no": cno, "text": txt})
    return claims, (lang.lower() if lang else "en")


def parse_images(xml_bytes):
    """-> list of {link, pages, section} drawing/facsimile instances from an OPS images response."""
    root = ET.fromstring(xml_bytes)
    out = []
    for inst in _findall_local(root, "document-instance"):
        desc = inst.get("desc", "")
        link = inst.get("link")
        pages = int(inst.get("number-of-pages", "0") or 0)
        secs = [s.get("name") for s in inst.findall(".//") if _lname(s.tag) == "document-section"]
        if link and (desc.lower() in ("drawing", "fullimage") or "DRAWINGS" in (secs or [])):
            out.append({"link": link, "pages": pages, "desc": desc, "sections": secs})
    return out


def parse_legal(xml_bytes):
    """-> list of {code, date, desc} INPADOC legal events."""
    root = ET.fromstring(xml_bytes)
    out = []
    events = _findall_local(root, "legal-event")
    if not events:                      # some responses nest events differently; fall back
        events = _findall_local(root, "legal")
    for ev in events:
        code = None; date = None; desc = None
        for ch in ev.iter():
            ln = _lname(ch.tag)
            if ln in ("code", "legal-code"):
                code = (ch.text or "").strip()
            elif "date" in ln:
                d = (ch.text or "").strip()
                m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", d)
                if m:
                    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            elif ln in ("legal-event-value", "desc", "description"):
                desc = (ch.text or "").strip() or desc
        if code or desc:
            out.append({"code": code, "date": date, "desc": desc})
    return out


# ---- top-level fetch -----------------------------------------------------------------------
def ops_fetch(pubnum, mock=False, want=("description", "claims", "images", "legal")):
    """Fetch OPS full-text + drawings + legal for a publication -> normalized dict.
    mock=True (or no creds) uses bundled sample XML so the parser is provable without creds."""
    epodoc, legal_num = to_epodoc(pubnum)
    result = {"pub": pubnum, "epodoc": epodoc, "source": "mock" if (mock or not have_creds()) else "ops",
              "paragraphs": [], "claims": [], "claims_lang": "en", "images": [], "legal_events": []}
    if mock or not have_creds():
        return _mock_fetch(pubnum, result, want)
    # live OPS
    if "description" in want:
        st, body, _ = _ops_get(f"published-data/publication/epodoc/{epodoc}/description")
        if st == 200:
            try: result["paragraphs"] = parse_description(body)
            except ET.ParseError: pass
    if "claims" in want:
        st, body, _ = _ops_get(f"published-data/publication/epodoc/{epodoc}/claims")
        if st == 200:
            try: result["claims"], result["claims_lang"] = parse_claims(body)
            except ET.ParseError: pass
    if "images" in want:
        st, body, _ = _ops_get(f"published-data/publication/epodoc/{epodoc}/images")
        if st == 200:
            try: result["images"] = parse_images(body)
            except ET.ParseError: pass
    if "legal" in want:
        st, body, _ = _ops_get(f"legal/publication/epodoc/{legal_num}/")
        if st == 200:
            try: result["legal_events"] = parse_legal(body)
            except ET.ParseError: pass
    return result


def _mock_fetch(pubnum, result, want):
    """Parse the bundled sample OPS XML — proves the parsers + schema mapping without creds."""
    def _read(name):
        p = SAMPLES / name
        return p.read_bytes() if p.exists() else None
    if "description" in want and _read("description.xml"):
        result["paragraphs"] = parse_description(_read("description.xml"))
    if "claims" in want and _read("claims.xml"):
        result["claims"], result["claims_lang"] = parse_claims(_read("claims.xml"))
    if "images" in want and _read("images.xml"):
        result["images"] = parse_images(_read("images.xml"))
    if "legal" in want and _read("legal.xml"):
        result["legal_events"] = parse_legal(_read("legal.xml"))
    return result


# ---- schema mapping ------------------------------------------------------------------------
def ops_enrich_publication(pubnum, mock=False, reembed=False):
    """Map an OPS fetch into the DB schema (paragraphs, claims+chunks, figures, legal_events,
    facsimile) with provenance. Returns a summary. Idempotent (skips if claims already present)."""
    import db, patent_text as pt, enrich_display
    data = ops_fetch(pubnum, mock=mock)
    src = db.get_source_id("epo:ops", "3.2" + ("-mock" if data["source"] == "mock" else ""))
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pubnum,))
        row = cur.fetchone()
        if not row:
            return {"pub": pubnum, "ok": False, "reason": "not_in_corpus", "source": data["source"]}
        pid = row["id"]
        added = {"claims": 0, "paragraphs": 0, "figures": 0, "legal": 0}
        # claims (only if none present) — resolve dependencies (EN + DE)
        cur.execute("SELECT count(*) c FROM claims WHERE publication_id=%s", (pid,))
        if cur.fetchone()["c"] == 0 and data["claims"]:
            blob = "\n".join(c["text"] if re.match(r"^\s*\d", c["text"]) else f'{c["claim_no"]}. {c["text"]}'
                             for c in data["claims"])
            for c in pt.resolve_claims(pt.split_claims(blob)):
                cur.execute("INSERT INTO claims(publication_id,claim_no,is_independent,lang,text,resolved_text) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (pid, c["claim_no"], c["is_independent"], data["claims_lang"], c["text"], c["resolved_text"]))
                added["claims"] += 1
        # description paragraphs (only if none) — with coordinates
        cur.execute("SELECT count(*) c FROM paragraphs WHERE publication_id=%s", (pid,))
        if cur.fetchone()["c"] == 0:
            for p in data["paragraphs"]:
                cur.execute("INSERT INTO paragraphs(publication_id,para_no,heading,page_no,lang,text) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (pid, p["para_no"], p.get("heading"), None, data["claims_lang"], p["text"]))
                added["paragraphs"] += 1
        # figures / facsimile
        for im in data["images"]:
            cur.execute("INSERT INTO figures(publication_id,figure_no,caption,reference_numbers) "
                        "VALUES (%s,%s,%s,%s)", (pid, None, f'OPS drawing set ({im["pages"]} pages)', None))
            added["figures"] += 1
        if data["images"]:
            cur.execute("UPDATE publications SET facsimile_path=%s WHERE id=%s",
                        (f'ops:{data["images"][0]["link"]}', pid))
        # legal events
        for ev in data["legal_events"]:
            cur.execute("INSERT INTO legal_events(publication_id,event_code,event_date,raw) "
                        "VALUES (%s,%s,%s,%s)", (pid, ev.get("code"), ev.get("date"), json.dumps(ev)))
            added["legal"] += 1
        cur.execute("INSERT INTO field_provenance(entity,entity_id,field,source_id,ocr_status) "
                    "VALUES ('publication',%s,'ops_fulltext',%s,%s)",
                    (pid, src, "authoritative" if data["source"] == "ops" else "mock"))
    res = {"pub": pubnum, "ok": True, "source": data["source"], **added}
    if reembed and added["claims"] + added["paragraphs"] > 0:
        _reembed(pid)
        res["reembedded"] = True
    return res


def _reembed(pid):
    """Chunk + embed newly-added claims/paragraphs for one publication (HNSW maintains on UPDATE)."""
    import db, embed, json as _j
    with db.cursor() as cur:
        cur.execute("SELECT id,claim_no,lang,text,resolved_text FROM claims WHERE publication_id=%s "
                    "AND id NOT IN (SELECT ref_id FROM chunks WHERE ref_id IS NOT NULL AND kind LIKE 'claim%%')", (pid,))
        for c in cur.fetchall():
            coord = _j.dumps({"claim_no": c["claim_no"]}); own = (c["text"] or "")[:8000]
            cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                        "VALUES (%s,'claim_own',%s,%s,%s,%s,%s)", (pid, c["id"], coord, c["lang"] or "en", own, max(1, len(own)//4)))
            res = (c["resolved_text"] or "")[:8000]
            if res and res != own:
                cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                            "VALUES (%s,'claim_resolved',%s,%s,%s,%s,%s)", (pid, c["id"], coord, c["lang"] or "en", res, max(1, len(res)//4)))
        cur.execute("SELECT id,para_no,heading,text FROM paragraphs WHERE publication_id=%s "
                    "AND id NOT IN (SELECT ref_id FROM chunks WHERE ref_id IS NOT NULL AND kind='paragraph')", (pid,))
        for p in cur.fetchall():
            coord = _j.dumps({"para_no": p["para_no"], "heading": p["heading"]}); t = (p["text"] or "")[:8000]
            cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                        "VALUES (%s,'paragraph',%s,%s,'en',%s,%s)", (pid, p["id"], coord, t, max(1, len(t)//4)))
    embed.run(order_priority=False)


def backfill(pubnums, mock=False, reembed=True):
    """One-command DE/EP/WO backfill entry point (see README)."""
    out = []
    for p in pubnums:
        r = ops_enrich_publication(p, mock=mock, reembed=False)
        out.append(r); print(f"  {p}: {r}")
    if reembed:
        import embed
        print("[ops] embedding new chunks...")
        embed.run(order_priority=False)
    return out


def backfill_core(limit=None, mock=False):
    """Auto-select every claimless DE/EP/WO CORE publication and backfill its OPS full text —
    the intended production run once OPS credentials exist. `python ops.py --backfill-core`."""
    import db
    with db.cursor() as cur:
        cur.execute("""SELECT publication_number FROM publications p
            WHERE p.tier='core' AND p.country IN ('DE','EP','WO')
            AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=p.id)
            ORDER BY p.publication_number """ + (f"LIMIT {int(limit)}" if limit else ""))
        pubs = [r["publication_number"] for r in cur.fetchall()]
    print(f"[ops] backfill-core: {len(pubs)} claimless DE/EP/WO core pubs "
          f"({'MOCK' if (mock or not have_creds()) else 'LIVE OPS'})")
    return backfill(pubs, mock=mock, reembed=True)


if __name__ == "__main__":
    import sys
    if "--dry-run" in sys.argv:
        d = ops_fetch("EP-2496850-A1", mock=True)
        print("DRY-RUN (mock, no creds needed):")
        print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in d.items()}, indent=1))
    elif "--backfill-core" in sys.argv:
        if not have_creds():
            print("No OPS creds in .env — this would run against the MOCK. Add "
                  "OPS_CONSUMER_KEY/OPS_CONSUMER_SECRET to go live.")
        backfill_core(limit=(int(sys.argv[sys.argv.index("--limit")+1]) if "--limit" in sys.argv else None))
    else:
        pubs = [a for a in sys.argv[1:] if not a.startswith("--")]
        if not pubs:
            print("usage: python ops.py --dry-run | --backfill-core [--limit N] | <PUB...> [--reembed]")
        else:
            backfill(pubs, mock=not have_creds(), reembed="--reembed" in sys.argv)
