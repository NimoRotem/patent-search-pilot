"""EPO OPS (Open Patent Services v3.2) client — the fix for the EP/WO/DE full-text hole.

Fills descriptions + claims + drawings + INPADOC legal status that BigQuery lacks for EP/WO/DE.
Credentials: `OPS_CONSUMER_KEY` + `OPS_CONSUMER_SECRET` in `.env` (EPO developer portal ->
My Apps -> Keys). Without them everything runs in mock mode against `src/ops_samples/`.

OPS REST, XML responses (namespaces: ops=http://ops.epo.org, exch=http://www.epo.org/exchange,
ftxt=http://www.epo.org/fulltext). Auth = OAuth2 client-credentials (Basic base64(key:secret) ->
access token). Free tier: 4 GB/week, enforced here by a persisted byte budget + a per-service
throttle driven by the X-Throttling-Control response header.

WIRE-FORMAT NOTES (learned against live OPS 2026-07-19, the fixtures in ops_samples/real_*.xml
are verbatim captures):

  * Full-text and biblio endpoints want the epodoc number WITHOUT the kind code.
    `EP2496850/claims` -> 200; `EP2496850A1/claims` -> 404 SERVER.EntityNotFound.
  * A claims response carries ONE <claims lang="XX"> block PER LANGUAGE (EP publishes DE/EN/FR).
    Inside a block there is typically a SINGLE <claim> element holding MANY <claim-text> children
    (one per claim) — NOT one <claim> per claim. Parsing by <claim> therefore yields one
    mega-blob per language with all languages mixed together. We select ONE language block
    (LANG_PREF) and split on <claim-text>.
  * Description paragraphs carry their number inline as "[0001] ..." rather than in a num=
    attribute, and unnumbered <p> elements are section headings.
  * INPADOC legal events put the event code and description in ATTRIBUTES of <legal>
    (code=, desc=, infl=), with child elements named by field code (L007EP = 'Gazette DATE',
    L525EP = 'Effective DATE'). There is no <legal-event>/<code> child structure.
  * COVERAGE: the OPS full-text service serves EP and WO only. Every national DE publication
    tested returns 404 SERVER.EntityNotFound for /claims and /description (verified over a
    random sample of the corpus, 100% 404). DE *images* and *legal* DO resolve. So OPS closes
    the EP+WO half of the full-text hole; German national text needs a different source
    (see enrich_de_batch.py, the path that doubled grabo_de recall@500).
"""
from __future__ import annotations
import os, re, sys, time, json, base64, threading
from pathlib import Path
import xml.etree.ElementTree as ET
import requests
from config import DATA

OPS_BASE = "https://ops.epo.org/3.2/rest-services"
OPS_TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SAMPLES = DATA.parent / "src" / "ops_samples"   # bundled sample XML for the mock path

# Which language to keep when OPS returns the same text in several languages. English first for
# embedding consistency with the rest of the corpus; DE next (most of the thin gold is German).
LANG_PREF = ("EN", "DE", "FR")

# Jurisdictions the OPS FULL-TEXT service actually serves. Anything else 404s on
# /claims and /description, so we do not spend requests (or 4 GB/week budget) asking.
FULLTEXT_COUNTRIES = ("EP", "WO")
ALL_WANT = ("claims", "description", "images", "legal")


def want_for(pubnum):
    """What is worth requesting for this publication. National docs (DE, ...) have no OPS full
    text but do have INPADOC legal status and drawings."""
    if (pubnum or "")[:2].upper() in FULLTEXT_COUNTRIES:
        return ALL_WANT
    return ("images", "legal")

# Free-tier fair-use allowance. We stop at a fraction of it so a run can never get the account
# throttled to black or suspended.
WEEK_BYTE_LIMIT = 4 * 1024 ** 3
BUDGET_SOFT_FRAC = float(os.environ.get("OPS_BUDGET_SOFT_FRAC", "0.80"))
BUDGET_FILE = DATA / "ops_budget.json"

_token = {"value": None, "exp": 0}
_token_lock = threading.Lock()


class OpsBudgetExceeded(RuntimeError):
    """Raised when the weekly OPS byte budget is spent — stops a backfill cleanly."""


def have_creds():
    return bool(os.environ.get("OPS_CONSUMER_KEY") and os.environ.get("OPS_CONSUMER_SECRET"))


# `available()` is the name the callers use; keep both.
available = have_creds


# ---- weekly byte budget --------------------------------------------------------------------
def _week_key():
    return time.strftime("%G-W%V", time.gmtime())


def budget_state():
    try:
        st = json.loads(BUDGET_FILE.read_text())
    except Exception:
        st = {}
    if st.get("week") != _week_key():
        st = {"week": _week_key(), "bytes": 0, "requests": 0}
    return st


def budget_note(nbytes):
    st = budget_state()
    st["bytes"] = st.get("bytes", 0) + int(nbytes)
    st["requests"] = st.get("requests", 0) + 1
    try:
        BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        BUDGET_FILE.write_text(json.dumps(st))
    except Exception:
        pass
    return st


def budget_remaining():
    st = budget_state()
    return int(WEEK_BYTE_LIMIT * BUDGET_SOFT_FRAC) - st.get("bytes", 0)


def budget_check():
    if budget_remaining() <= 0:
        st = budget_state()
        raise OpsBudgetExceeded(
            f"OPS weekly budget spent: {st['bytes']/1e9:.2f} GB used of "
            f"{WEEK_BYTE_LIMIT*BUDGET_SOFT_FRAC/1e9:.2f} GB soft cap (week {st['week']}). "
            f"Resume after the week rolls over, or raise OPS_BUDGET_SOFT_FRAC.")


# ---- throttle ------------------------------------------------------------------------------
# OPS returns e.g.
#   X-Throttling-Control: busy (images=green:100, inpadoc=green:45, other=green:1000,
#                               retrieval=green:100, search=green:15)
# i.e. per-service colour + allowance in requests/minute. We keep a minimum spacing per service
# and inflate it as the colour degrades.
_COLOUR_FACTOR = {"green": 1.0, "yellow": 2.5, "red": 8.0, "black": 60.0}
_throttle = {"svc": {}, "last": {}, "lock": threading.Lock()}

_SERVICE_OF = [
    (re.compile(r"/images|/fullimage|published-data/images"), "images"),
    (re.compile(r"^legal/"), "inpadoc"),
    (re.compile(r"/(claims|description|fulltext|biblio|abstract)"), "retrieval"),
    (re.compile(r"published-data/search"), "search"),
]


def _service_for(path):
    for rx, name in _SERVICE_OF:
        if rx.search(path):
            return name
    return "other"


def parse_throttle(header_value):
    """'busy (retrieval=green:100, ...)' -> ('busy', {'retrieval': ('green', 100), ...})."""
    if not header_value:
        return None, {}
    m = re.match(r"\s*([A-Za-z]+)\s*\((.*)\)\s*$", header_value.strip())
    if not m:
        return header_value.strip().lower(), {}
    svcs = {}
    for part in m.group(2).split(","):
        pm = re.match(r"\s*([\w-]+)\s*=\s*([A-Za-z]+)\s*:\s*(\d+)", part)
        if pm:
            svcs[pm.group(1).lower()] = (pm.group(2).lower(), int(pm.group(3)))
    return m.group(1).lower(), svcs


def _note_throttle(headers):
    _, svcs = parse_throttle(headers.get("X-Throttling-Control"))
    if svcs:
        with _throttle["lock"]:
            _throttle["svc"].update(svcs)


def _throttle_wait(path):
    """Sleep just enough to respect the advertised per-minute allowance for this service."""
    svc = _service_for(path)
    with _throttle["lock"]:
        colour, per_min = _throttle["svc"].get(svc, ("green", 30))
        per_min = max(1, per_min)
        interval = (60.0 / per_min) * _COLOUR_FACTOR.get(colour, 4.0)
        now = time.monotonic()
        wait = _throttle["last"].get(svc, 0.0) + interval - now
        _throttle["last"][svc] = now + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


# ---- number formatting ---------------------------------------------------------------------
def to_epodoc(pubnum: str):
    """'EP-2496850-A1' -> ('EP2496850A1' with kind, 'EP2496850' without).

    OPS full-text/biblio/legal endpoints all want the SECOND form; the kind-coded form 404s.
    """
    n = pubnum.replace("-", "").replace(" ", "").upper()
    m = re.match(r"^([A-Z]{2})(\w+?)([A-Z]\d?)$", n)
    return n, (m.group(1) + m.group(2) if m else n)


# ---- auth + fetch --------------------------------------------------------------------------
def _get_token():
    with _token_lock:
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
    """GET an OPS REST path with token + throttle + weekly budget accounting.
    Returns (status, content_bytes, headers). 404 is a normal 'no such document' answer and is
    returned immediately rather than retried."""
    budget_check()
    url = f"{OPS_BASE}/{path.lstrip('/')}"
    r = None
    for i in range(retries):
        _throttle_wait(path)
        tok = _get_token()
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {tok}", "Accept": accept},
                             params=params, timeout=45)
        except requests.RequestException:
            time.sleep(min(60, 4 * (i + 1) ** 2))
            continue
        _note_throttle(r.headers)
        budget_note(len(r.content))
        if r.status_code == 404:
            return r.status_code, r.content, r.headers
        if r.status_code in (403, 429, 500, 503):
            if r.status_code == 403 and b"invalid_access_token" in r.content.lower():
                with _token_lock:
                    _token["value"] = None      # force refresh, retry immediately
                continue
            time.sleep(min(120, 4 * (i + 1) ** 2))
            continue
        return r.status_code, r.content, r.headers
    if r is None:
        return 0, b"", {}
    return r.status_code, r.content, r.headers


def _ops_get_ft(kindless, suffix):
    """Full-text style GET with a kind-coded fallback (some legacy docs only resolve with kind)."""
    st, body, h = _ops_get(f"published-data/publication/epodoc/{kindless}/{suffix}")
    return st, body, h


# ---- XML parsers (namespace-agnostic via local-name) ---------------------------------------
def _lname(tag):
    return tag.rsplit("}", 1)[-1]


def _findall_local(root, name):
    return [e for e in root.iter() if _lname(e.tag) == name]


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _pick_lang(blocks):
    """blocks: list of (lang, payload). Choose by LANG_PREF, else first non-empty."""
    blocks = [(l, p) for l, p in blocks if p]
    if not blocks:
        return None, []
    for want in LANG_PREF:
        for lang, payload in blocks:
            if (lang or "").upper() == want:
                return lang, payload
    return blocks[0]


def _claim_blocks(root):
    """-> [(lang, [claim-text strings])]. Handles both the real OPS shape (one <claim> holding
    many <claim-text>) and the one-<claim>-per-claim shape."""
    out = []
    for cel in _findall_local(root, "claims"):
        lang = (cel.get("lang") or "").upper() or None
        texts = [_norm("".join(t.itertext())) for t in cel.iter() if _lname(t.tag) == "claim-text"]
        if not texts:   # no claim-text children at all -> fall back to <claim> bodies
            texts = [_norm("".join(c.itertext())) for c in cel.iter() if _lname(c.tag) == "claim"]
        out.append((lang, [t for t in texts if t]))
    if not out:         # bare <claim> elements with no <claims> wrapper
        texts = [_norm("".join(c.itertext())) for c in _findall_local(root, "claim")]
        if texts:
            out.append((None, [t for t in texts if t]))
    return out


def _assemble_claims(texts):
    """Turn a flat list of claim-text strings into numbered claims. A fragment that does not
    start with 'N.' is a continuation of the previous claim (OPS splits long claims across
    several <claim-text> elements)."""
    claims = []
    for t in texts:
        m = re.match(r"^(\d{1,4})\s*[.)]\s*(.+)$", t)
        if m and (not claims or int(m.group(1)) >= claims[-1]["claim_no"]):
            claims.append({"claim_no": int(m.group(1)), "text": t})
        elif claims:
            claims[-1]["text"] += " " + t
        else:
            claims.append({"claim_no": 1, "text": t})
    return claims


def parse_claims(xml_bytes):
    """-> ([{claim_no, text}], lang). Selects a SINGLE language block (see module docstring)."""
    root = ET.fromstring(xml_bytes)
    lang, texts = _pick_lang(_claim_blocks(root))
    return _assemble_claims(texts), (lang or "en").lower()


def _description_blocks(root):
    out = []
    for d in _findall_local(root, "description"):
        lang = (d.get("lang") or "").upper() or None
        ps = [(p.get("num"), _norm("".join(p.itertext()))) for p in d.iter() if _lname(p.tag) == "p"]
        out.append((lang, [(n, t) for n, t in ps if t]))
    if not out:
        ps = [(p.get("num"), _norm("".join(p.itertext()))) for p in _findall_local(root, "p")]
        if ps:
            out.append((None, [(n, t) for n, t in ps if t]))
    return out


def parse_description_full(xml_bytes):
    """-> ([{para_no, heading, text}], lang). Unnumbered short <p> elements become the heading
    carried onto following paragraphs; inline '[0001]' markers become para_no."""
    root = ET.fromstring(xml_bytes)
    lang, ps = _pick_lang(_description_blocks(root))
    out, heading, seq = [], None, 0
    for num, t in ps:
        m = re.match(r"^\[(\d{3,6})\]\s*(.*)$", t)
        if num:
            seq += 1
            out.append({"para_no": str(num), "heading": heading, "text": m.group(2) if m else t})
        elif m:
            seq += 1
            out.append({"para_no": m.group(1), "heading": heading, "text": m.group(2) or t})
        elif len(t) <= 80 and not any(ch in t for ch in ".;:"):
            # Short, punctuation-free, unnumbered line = section heading. Deliberately strict:
            # anything else (e.g. the "Fig. 1 eine Ansicht ..." drawing list) is kept as real
            # text, because dropping content would defeat the whole point of the backfill.
            heading = t
        else:
            seq += 1
            out.append({"para_no": f"{seq:04d}", "heading": heading, "text": t})
    return out, (lang or "en").lower()


def parse_description(xml_bytes):
    """Backwards-compatible wrapper: paragraphs only."""
    return parse_description_full(xml_bytes)[0]


# The desc values OPS actually emits on <ops:document-instance>. "FullDocument" is the real
# wire value for the complete facsimile — an earlier guess of "fullimage" (the last path
# SEGMENT of its link, not its desc) matched nothing, so every full-document instance was
# silently dropped and only publications with a separate "Drawing" instance were seen. That
# is invisible in the common case, because most publications have both.
IMAGE_DESCS = ("drawing", "fulldocument", "fullimage")


def parse_images(xml_bytes):
    """-> list of {link, pages, desc, sections} drawing/facsimile instances.

    Ordered with the "Drawing" instance FIRST: it is drawing sheets by construction, so a
    consumer can crop it without running a drawing-vs-text classifier, whereas a
    "FullDocument" facsimile also contains cover and description pages.
    """
    root = ET.fromstring(xml_bytes)
    out = []
    for inst in _findall_local(root, "document-instance"):
        desc = inst.get("desc", "")
        link = inst.get("link")
        pages = int(inst.get("number-of-pages", "0") or 0)
        secs = [s.get("name") for s in inst.iter() if _lname(s.tag) == "document-section"]
        if link and (desc.lower() in IMAGE_DESCS or "DRAWINGS" in (secs or [])):
            out.append({"link": link, "pages": pages, "desc": desc, "sections": secs})
    out.sort(key=lambda i: 0 if (i["desc"] or "").lower() == "drawing" else 1)
    return out


# ---- facsimile page retrieval ---------------------------------------------------------------
# OPS serves facsimiles ONE PAGE PER REQUEST:
#   GET {link}.pdf?Range=<n>   Accept: application/pdf   -> a one-page PDF
# There is no whole-document form, so an N-page facsimile costs N requests and N pages of
# quota. Everything here is therefore capped and cached on disk forever (drawings are
# immutable once published, so there is no TTL to reason about).
IMAGE_CACHE = DATA / "ops_images"
MAX_IMAGE_PAGES = int(os.environ.get("OPS_MAX_IMAGE_PAGES", "12"))


def fetch_image_page(link, page):
    """One facsimile page as PDF bytes (b'' if unavailable). Disk-cached forever."""
    import hashlib
    key = hashlib.sha1(f"{link}|{page}".encode()).hexdigest()
    dest = IMAGE_CACHE / f"{key}.pdf"
    try:
        if dest.exists() and dest.stat().st_size > 0:
            return dest.read_bytes()
    except Exception:
        pass
    st, body, _ = _ops_get(f"{link}.pdf", accept="application/pdf",
                           params={"Range": str(page)})
    if st != 200 or not (body or b"")[:5].startswith(b"%PDF"):
        return b""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    except Exception:
        pass
    return body


def fetch_facsimile(pubnum, max_pages=MAX_IMAGE_PAGES):
    """Best available facsimile for a publication.

    -> {"pages": [pdf_bytes, ...], "desc": "Drawing"|"FullDocument", "link": ..., "bytes": n}
    or {} when OPS has no imagery. Prefers the Drawing instance.
    """
    data = ops_fetch(pubnum, want=("images",))
    insts = data.get("images") or []
    if not insts:
        return {}
    inst = insts[0]
    n = min(int(inst.get("pages") or 1) or 1, max_pages)
    pages, total = [], 0
    for i in range(1, n + 1):
        try:
            b = fetch_image_page(inst["link"], i)
        except OpsBudgetExceeded:
            break                      # keep whatever pages we already paid for
        if not b:
            break
        pages.append(b)
        total += len(b)
    if not pages:
        return {}
    return {"pages": pages, "desc": inst.get("desc") or "", "link": inst.get("link"),
            "bytes": total, "total_pages": inst.get("pages") or len(pages)}


def _iso_date(s):
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", s or "")
    if not m or m.group(1) == "0001":
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _parse_legal_inpadoc(root):
    """Real OPS INPADOC shape: <legal code="17P " desc="REQUEST FOR EXAMINATION FILED" infl="+">
    with child elements named by field code (L007EP='Gazette DATE', L525EP='Effective DATE', ...).
    The event code and description live in ATTRIBUTES, not child tags."""
    out = []
    for ev in _findall_local(root, "legal"):
        code, desc = _norm(ev.get("code")), _norm(ev.get("desc"))
        fields = {}
        for ch in ev.iter():
            if ch is ev or _lname(ch.tag) == "pre":
                continue
            fields[ch.get("desc") or _lname(ch.tag)] = _norm("".join(ch.itertext()))
        date = None
        for key in ("Effective DATE", "Gazette DATE", "DATE first created"):
            date = _iso_date(fields.get(key))
            if date:
                break
        if code or desc:
            out.append({"code": code or None, "date": date, "desc": desc or None,
                        "infl": ev.get("infl"), "fields": fields})
    return out


def _parse_legal_events(root):
    """Legacy/simple <legal-event> shape with code/date/desc child elements."""
    out = []
    for ev in _findall_local(root, "legal-event"):
        code = date = desc = None
        for ch in ev.iter():
            ln = _lname(ch.tag)
            if ln in ("code", "legal-code"):
                code = _norm(ch.text) or code
            elif "date" in ln:
                date = _iso_date(ch.text) or date
            elif ln in ("legal-event-value", "desc", "description"):
                desc = _norm(ch.text) or desc
        if code or desc:
            out.append({"code": code, "date": date, "desc": desc})
    return out


def parse_legal(xml_bytes):
    """-> list of {code, date, desc, ...} INPADOC legal events (handles both wire shapes)."""
    root = ET.fromstring(xml_bytes)
    return _parse_legal_events(root) or _parse_legal_inpadoc(root)


# ---- top-level fetch -----------------------------------------------------------------------
def ops_fetch(pubnum, mock=False, want=("description", "claims", "images", "legal")):
    """Fetch OPS full-text + drawings + legal for a publication -> normalized dict."""
    with_kind, kindless = to_epodoc(pubnum)
    use_mock = mock or not have_creds()
    result = {"pub": pubnum, "epodoc": kindless, "source": "mock" if use_mock else "ops",
              "paragraphs": [], "claims": [], "claims_lang": "en", "desc_lang": "en",
              "images": [], "legal_events": [], "bytes": 0}
    if use_mock:
        return _mock_fetch(pubnum, result, want)

    def _try(suffix):
        st, body, _ = _ops_get(f"published-data/publication/epodoc/{kindless}/{suffix}")
        if st == 404 and with_kind != kindless:
            st, body, _ = _ops_get(f"published-data/publication/epodoc/{with_kind}/{suffix}")
        result["bytes"] += len(body or b"")
        return (body if st == 200 else None)

    if "claims" in want:
        b = _try("claims")
        if b:
            try:
                result["claims"], result["claims_lang"] = parse_claims(b)
            except ET.ParseError:
                pass
    if "description" in want:
        b = _try("description")
        if b:
            try:
                result["paragraphs"], result["desc_lang"] = parse_description_full(b)
            except ET.ParseError:
                pass
    if "images" in want:
        b = _try("images")
        if b:
            try:
                result["images"] = parse_images(b)
            except ET.ParseError:
                pass
    if "legal" in want:
        st, body, _ = _ops_get(f"legal/publication/epodoc/{kindless}/")
        result["bytes"] += len(body or b"")
        if st == 200:
            try:
                result["legal_events"] = parse_legal(body)
            except ET.ParseError:
                pass
    return result


def _mock_fetch(pubnum, result, want):
    """Parse the bundled sample OPS XML — proves the parsers + schema mapping without creds.
    Prefers the verbatim live captures (real_*.xml) and falls back to the hand-built samples."""
    def _read(*names):
        for name in names:
            p = SAMPLES / name
            if p.exists():
                return p.read_bytes()
        return None
    if "description" in want and (b := _read("real_description.xml", "description.xml")):
        result["paragraphs"], result["desc_lang"] = parse_description_full(b)
    if "claims" in want and (b := _read("real_claims.xml", "claims.xml")):
        result["claims"], result["claims_lang"] = parse_claims(b)
    if "images" in want and (b := _read("real_images.xml", "images.xml")):
        result["images"] = parse_images(b)
    if "legal" in want and (b := _read("real_legal.xml", "legal.xml")):
        result["legal_events"] = parse_legal(b)
    return result


# ---- schema mapping ------------------------------------------------------------------------
CHUNK_MAX = 8000


def _chunk_rows(pid, claim_rows, para_rows):
    """Build the chunk tuples for freshly inserted claims/paragraphs, mirroring the kinds the
    rest of the corpus uses (claim_own / claim_resolved / paragraph). embedding stays NULL so
    embed.run() picks them up with the corpus-standard settings."""
    import patent_text as pt
    rows = []
    for c in claim_rows:
        coord = json.dumps({"claim_no": c["claim_no"]})
        own = (c["text"] or "")[:CHUNK_MAX]
        if own:
            rows.append((pid, "claim_own", c["id"], coord, c["lang"], own, max(1, len(own) // 4)))
        res = (c["resolved_text"] or "")[:CHUNK_MAX]
        if res and res != own:
            rows.append((pid, "claim_resolved", c["id"], coord, c["lang"], res, max(1, len(res) // 4)))
    for p in para_rows:
        text = p["text"] or ""
        parts = pt.split_paragraphs(text) if len(text) > 1600 else [text]
        for i, part in enumerate(parts):
            part = (part if isinstance(part, str) else part.get("text", ""))[:CHUNK_MAX]
            if not part.strip():
                continue
            coord = json.dumps({"para_no": p["para_no"], "heading": p["heading"],
                                **({"part": i} if len(parts) > 1 else {})})
            rows.append((pid, "paragraph", p["id"], coord, p["lang"], part, max(1, len(part) // 4)))
    return rows


def ops_enrich_publication(pubnum, mock=False, cur=None, force=False):
    """Map an OPS fetch into the DB schema (claims, paragraphs, chunks, legal_events, facsimile)
    with provenance. Idempotent: skips a publication already marked ops_fulltext unless force."""
    import db, patent_text as pt

    def _work(cur):
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pubnum,))
        row = cur.fetchone()
        if not row:
            return {"pub": pubnum, "ok": False, "reason": "not_in_corpus"}
        pid = row["id"]
        # Provenance is honest about WHAT was retrievable: full text for EP/WO, legal status only
        # for national docs OPS has no full text for.
        want = want_for(pubnum)
        prov_field = "ops_fulltext" if "claims" in want else "ops_legal"
        if not force:
            cur.execute("SELECT 1 FROM field_provenance WHERE entity='publication' AND entity_id=%s "
                        "AND field=%s AND ocr_status='authoritative' LIMIT 1", (pid, prov_field))
            if cur.fetchone():
                return {"pub": pubnum, "ok": True, "skipped": "already_backfilled"}

        data = ops_fetch(pubnum, mock=mock, want=want)
        src = db.get_source_id("epo:ops", "3.2" + ("-mock" if data["source"] == "mock" else ""))
        added = {"claims": 0, "paragraphs": 0, "chunks": 0, "legal": 0}
        claim_rows, para_rows = [], []

        cur.execute("SELECT count(*) c FROM claims WHERE publication_id=%s", (pid,))
        if cur.fetchone()["c"] == 0 and data["claims"]:
            blob = "\n".join(c["text"] if re.match(r"^\s*\d", c["text"])
                             else f'{c["claim_no"]}. {c["text"]}' for c in data["claims"])
            for c in pt.resolve_claims(pt.split_claims(blob)):
                cur.execute("INSERT INTO claims(publication_id,claim_no,is_independent,lang,text,"
                            "resolved_text) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                            (pid, c["claim_no"], c["is_independent"], data["claims_lang"],
                             c["text"], c["resolved_text"]))
                claim_rows.append({"id": cur.fetchone()["id"], "claim_no": c["claim_no"],
                                   "lang": data["claims_lang"], "text": c["text"],
                                   "resolved_text": c["resolved_text"]})
                added["claims"] += 1

        cur.execute("SELECT count(*) c FROM paragraphs WHERE publication_id=%s", (pid,))
        if cur.fetchone()["c"] == 0:
            for p in data["paragraphs"]:
                cur.execute("INSERT INTO paragraphs(publication_id,para_no,heading,page_no,lang,text)"
                            " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                            (pid, p["para_no"], p.get("heading"), None, data["desc_lang"], p["text"]))
                para_rows.append({"id": cur.fetchone()["id"], "para_no": p["para_no"],
                                  "heading": p.get("heading"), "lang": data["desc_lang"],
                                  "text": p["text"]})
                added["paragraphs"] += 1

        chunks = _chunk_rows(pid, claim_rows, para_rows)
        for ch in chunks:
            cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s)", ch)
        added["chunks"] = len(chunks)

        if data["images"]:
            cur.execute("UPDATE publications SET facsimile_path=%s WHERE id=%s AND "
                        "(facsimile_path IS NULL OR facsimile_path='')",
                        (f'ops:{data["images"][0]["link"]}', pid))

        for ev in data["legal_events"]:
            cur.execute("SELECT 1 FROM legal_events WHERE publication_id=%s AND event_code=%s AND "
                        "event_date IS NOT DISTINCT FROM %s LIMIT 1",
                        (pid, ev.get("code"), ev.get("date")))
            if cur.fetchone():
                continue
            cur.execute("INSERT INTO legal_events(publication_id,event_code,event_date,raw) "
                        "VALUES (%s,%s,%s,%s)", (pid, ev.get("code"), ev.get("date"), json.dumps(ev)))
            added["legal"] += 1

        cur.execute("INSERT INTO field_provenance(entity,entity_id,field,source_id,ocr_status) "
                    "VALUES ('publication',%s,%s,%s,%s)",
                    (pid, prov_field, src, "authoritative" if data["source"] == "ops" else "mock"))
        return {"pub": pubnum, "ok": True, "source": data["source"], "bytes": data.get("bytes", 0),
                **added}

    if cur is not None:
        return _work(cur)
    with db.cursor() as c:
        return _work(c)


def backfill(pubnums, mock=False, reembed=True, verbose=True, sleep_every=0.0):
    """Backfill a list of publication numbers, then embed everything new in ONE embed pass.

    Each publication commits on its own so the run is resumable and never holds a long
    transaction against the live app's database.
    """
    import db
    out, totals = [], {"claims": 0, "paragraphs": 0, "chunks": 0, "legal": 0, "bytes": 0}
    conn = db.connect()
    try:
        for i, p in enumerate(pubnums, 1):
            try:
                with conn.cursor() as cur:
                    r = ops_enrich_publication(p, mock=mock, cur=cur)
                conn.commit()
            except OpsBudgetExceeded as e:
                conn.rollback()
                print(f"[ops] STOPPING: {e}")
                break
            except Exception as e:
                conn.rollback()
                r = {"pub": p, "ok": False, "reason": f"{type(e).__name__}: {e}"[:200]}
            out.append(r)
            for k in totals:
                totals[k] += r.get(k, 0) or 0
            if verbose and (i % 25 == 0 or i == len(pubnums)):
                print(f"  [{i}/{len(pubnums)}] +{totals['claims']} claims "
                      f"+{totals['paragraphs']} paras +{totals['chunks']} chunks "
                      f"{totals['bytes']/1e6:.0f} MB", flush=True)
            if sleep_every:
                time.sleep(sleep_every)
    finally:
        conn.close()
    print(f"[ops] backfilled {sum(1 for r in out if r.get('ok'))}/{len(pubnums)} pubs; "
          f"{totals['claims']:,} claims, {totals['paragraphs']:,} paragraphs, "
          f"{totals['chunks']:,} chunks, {totals['bytes']/1e6:.0f} MB downloaded")
    if reembed and totals["chunks"]:
        import embed
        print("[ops] embedding new chunks (Vertex gemini-embedding-001, 768d)...")
        embed.run(order_priority=False)
    return out, totals


# ---- target selection ----------------------------------------------------------------------
def gold_families():
    """Simple-family ids named in the frozen gold set — used to prioritise the backfill so the
    recall change is measurable on the existing eval."""
    try:
        g = json.loads((DATA / "goldset" / "goldset.json").read_text())
    except Exception:
        return set()
    fams = set()
    for e in g.get("entries", []):
        fams.update(str(f) for f in e.get("gold_families", []))
        if e.get("anchor_family"):
            fams.add(str(e["anchor_family"]))
    return fams


def select_targets(limit=None, countries=FULLTEXT_COUNTRIES, tier="core", priority_first=True,
                   field="ops_fulltext"):
    """Claimless publications to backfill, gold-relevant families first.

    Defaults to EP/WO because those are the only jurisdictions OPS serves full text for; pass
    countries=('DE',) with field='ops_legal' for the legal-status-only sweep.
    """
    import db
    fams = gold_families() if priority_first else set()
    with db.cursor() as cur:
        cur.execute("""SELECT p.publication_number, p.simple_family_id
                       FROM publications p
                       WHERE p.tier=%s AND p.country = ANY(%s)
                         AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=p.id)
                         AND NOT EXISTS (SELECT 1 FROM field_provenance f
                                         WHERE f.entity='publication' AND f.entity_id=p.id
                                           AND f.field=%s)
                       ORDER BY p.publication_number""", (tier, list(countries), field))
        rows = cur.fetchall()
    prio = [r["publication_number"] for r in rows if str(r["simple_family_id"]) in fams]
    rest = [r["publication_number"] for r in rows if str(r["simple_family_id"]) not in fams]
    ordered = prio + rest
    print(f"[ops] {len(rows):,} claimless {'/'.join(countries)} {tier} pubs pending "
          f"({len(prio):,} in gold families -> done first)")
    return ordered[:limit] if limit else ordered


def backfill_core(limit=None, mock=False, gold_only=False, reembed=True,
                  countries=FULLTEXT_COUNTRIES, field="ops_fulltext"):
    """Production run: fill every claimless EP/WO core publication, gold families first."""
    pubs = select_targets(limit=limit, countries=countries, field=field)
    if gold_only:
        fams = gold_families()
        import db
        with db.cursor() as cur:
            cur.execute("SELECT publication_number FROM publications WHERE simple_family_id = ANY(%s)",
                        (list(fams),))
            keep = {r["publication_number"] for r in cur.fetchall()}
        pubs = [p for p in pubs if p in keep]
    st = budget_state()
    print(f"[ops] mode={'MOCK' if (mock or not have_creds()) else 'LIVE'} "
          f"targets={len(pubs):,} budget_used={st.get('bytes',0)/1e9:.2f} GB "
          f"remaining={budget_remaining()/1e9:.2f} GB")
    return backfill(pubs, mock=mock, reembed=reembed)


if __name__ == "__main__":
    args = sys.argv[1:]

    def _opt(name, default=None, cast=str):
        return cast(args[args.index(name) + 1]) if name in args else default

    if "--dry-run" in args:
        d = ops_fetch("EP-2496850-A1", mock=True)
        print("DRY-RUN (mock, no creds needed):")
        print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in d.items()}, indent=1))
    elif "--status" in args:
        st = budget_state()
        print(f"creds={have_creds()} week={st.get('week')} "
              f"used={st.get('bytes',0)/1e9:.3f} GB requests={st.get('requests',0)} "
              f"remaining={budget_remaining()/1e9:.3f} GB of {WEEK_BYTE_LIMIT/1e9:.1f} GB/week")
    elif "--backfill-core" in args:
        if not have_creds():
            print("No OPS creds in .env — add OPS_CONSUMER_KEY/OPS_CONSUMER_SECRET to go live.")
        backfill_core(limit=_opt("--limit", None, int), gold_only="--gold-only" in args,
                      reembed="--no-embed" not in args)
    elif "--legal-sweep" in args:
        # National docs (DE, ...) have no OPS full text but do have INPADOC legal status —
        # the authoritative replacement for the SerpApi scraping the UI currently relies on.
        backfill_core(limit=_opt("--limit", None, int), reembed=False,
                      countries=("DE",), field="ops_legal")
    else:
        pubs = [a for a in args if not a.startswith("--")]
        if not pubs:
            print("usage: python ops.py --dry-run | --status | "
                  "--backfill-core [--limit N] [--gold-only] [--no-embed] | "
                  "--legal-sweep [--limit N] | <PUB...>")
        else:
            backfill(pubs, mock=not have_creds(), reembed="--reembed" in args)
