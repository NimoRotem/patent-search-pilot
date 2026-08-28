"""Google Patents, fetched directly. The cheapest source we have, by a distance.

Measured on the builder box 2026-08-14, no key, no proxy, no rate limiting observed:

    search   patents.google.com/xhr/query   100 results/page, 9 pages -> 900/query
    document patents.google.com/patent/..   50 docs/second at concurrency 8-20,
                                            every response HTTP 200

That is the same index SerpApi resells. SerpApi bills one credit per search (100
results) and one per document; a search that reads 120 documents in full was
costing $1.10 of detail plus $0.39 of search. Here both are zero and the depth
per query is 9x.

THE PART THAT MATTERS MOST: non-US documents carry `itemprop="claims"` and
`itemprop="description"` sections that Google has ALREADY TRANSLATED INTO ENGLISH
("Translated from German", "Translated from Chinese"). So one free parser yields
readable full text for DE, CN, JP, KR, EP and WO alike — the gap the whole stack
was built around. The US-specific `class="claim-text"` markup this codebase used
before is a subset; parse the itemprop sections and every jurisdiction works.

WHAT THIS IS NOT. It is an undocumented endpoint, so it can change or start
refusing us without notice. Every method fails soft and the pipeline keeps SerpApi
wired behind it: this adapter going dark must cost money, not results. Anything
unexpected is raised so the executor reports it rather than returning a thin
answer that looks like a real one.

Ported from patents-app `adapters/gpatents_direct.py` unchanged in behavior.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import time
from typing import Optional

import httpx

from .schema import Candidate, SubQuery, normalize_pub, canonical_pub, google_url, iso_date
from .base import Adapter, CACHE

XHR = "https://patents.google.com/xhr/query"
DOC = "https://patents.google.com/patent/{pn}/en"

UA = os.environ.get(
    "GPATENTS_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

PAGE_SIZE = int(os.environ.get("GPATENTS_PAGE_SIZE", "100"))
# The index stops serving at page 9 (0-indexed 8); asking for more returns nothing.
MAX_PAGES = max(1, min(9, int(os.environ.get("GPATENTS_MAX_PAGES", "2"))))

# RATE LIMITS, MEASURED THE HARD WAY (2026-08-14). A burst of ~80 requests at
# concurrency 8-20 ran at 50 docs/second and every response was 200 — and then the
# whole box was cut off. Not 429, not per-endpoint: plain 503 on BOTH the document
# pages and the XHR search, from that IP, still refusing 2.5 minutes later. So the
# throughput this endpoint appears to offer is not throughput you can spend. It is
# a supplementary source to be used gently, never the backbone; the free APIs with
# real published quotas (PQAI, EPO OPS) are what the acquisition layer leans on.
CONCURRENCY = int(os.environ.get("GPATENTS_CONCURRENCY", "2"))
MIN_INTERVAL = float(os.environ.get("GPATENTS_MIN_INTERVAL", "0.7"))
TIMEOUT = float(os.environ.get("GPATENTS_TIMEOUT", "30"))
DOC_TTL = float(os.environ.get("GPATENTS_DOC_TTL", "604800"))   # 7 days in the RAM cache

_SEM: Optional[asyncio.Semaphore] = None
_PACE_LOCK: Optional[asyncio.Lock] = None
_LAST_CALL = 0.0
# Trips on a block so a whole run does not sit retrying an endpoint that has
# decided it dislikes us; the paid path takes over while this cools down.
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_SECS = float(os.environ.get("GPATENTS_COOLDOWN", "900"))
_BLOCK_REASON = ""


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(CONCURRENCY)
    return _SEM


def _pace_lock() -> asyncio.Lock:
    global _PACE_LOCK
    if _PACE_LOCK is None:
        _PACE_LOCK = asyncio.Lock()
    return _PACE_LOCK


def available() -> bool:
    return time.monotonic() >= _COOLDOWN_UNTIL


def block_reason() -> str:
    return _BLOCK_REASON if not available() else ""


def _trip(reason: str) -> None:
    global _COOLDOWN_UNTIL, _BLOCK_REASON
    _COOLDOWN_UNTIL = time.monotonic() + _COOLDOWN_SECS
    _BLOCK_REASON = reason[:160]


def reset_cooldown() -> None:
    """Clear the block latch (tests / operator override)."""
    global _COOLDOWN_UNTIL, _BLOCK_REASON
    _COOLDOWN_UNTIL = 0.0
    _BLOCK_REASON = ""


async def _get(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
    global _LAST_CALL
    if not available():
        raise RuntimeError(f"gpatents_direct is cooling down: {_BLOCK_REASON}")
    async with _sem():
        # Space the calls. Concurrency alone is not a rate limit: 2 in flight with
        # no spacing still bursts, and bursting is what got us cut off.
        async with _pace_lock():
            gap = MIN_INTERVAL - (time.monotonic() - _LAST_CALL)
            if gap > 0:
                await asyncio.sleep(gap)
            _LAST_CALL = time.monotonic()
        r = await client.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    # 503 here is not "this document is unavailable", it is "this IP is cut off" —
    # it arrives on every path at once. Treat it as a block, not a missing page,
    # or the run spends its whole budget confirming the same refusal.
    if r.status_code in (403, 429, 503):
        _trip(f"HTTP {r.status_code} from patents.google.com (IP-level block)")
        r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
#  The XHR endpoint takes the query string of the human-facing page, url-encoded
#  once and passed whole as `url=`, e.g.
#      q="vacuum lifter"&country=EP,WO&before=priority:20200101&num=100&page=2
#  The planner already emits Google Patents syntax, so `native` goes in verbatim.


class GooglePatentsDirect(Adapter):
    name = "gpatents_direct"

    def __init__(self):
        self._warnings: list[str] = []

    def _warn(self, msg: str) -> None:
        if len(self._warnings) < 16 and msg not in self._warnings:
            self._warnings.append(msg)

    def pop_warnings(self) -> list[str]:
        w, self._warnings = self._warnings, []
        return w

    def enabled(self) -> bool:
        return available()

    def disabled_reason(self) -> str:
        return (f"Google Patents direct is cooling down after {block_reason()} — "
                f"the paid SerpApi path is carrying search meanwhile") if not available() else ""

    def search_note(self) -> str:
        return (f"free, no key — {PAGE_SIZE} results/page x {MAX_PAGES} pages per query "
                f"({PAGE_SIZE * MAX_PAGES} max), full text for every jurisdiction in English")

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        if not available():
            return []
        query = str(sq.native or "").strip()
        if not query:
            raise ValueError("empty gpatents_direct query (planner emitted no terms)")
        from urllib.parse import quote

        base = f"q={quote(query, safe='')}"
        if sq.date_from:
            base += f"&after=priority:{sq.date_from.replace('-', '')}"
        if sq.date_to:
            base += f"&before=priority:{sq.date_to.replace('-', '')}"

        async def page(n: int) -> list[dict]:
            url_param = base + f"&num={PAGE_SIZE}" + (f"&page={n}" if n else "")
            key = f"gpd:s:{url_param}"
            hit = await CACHE.get(key)
            if hit is not None:
                return hit
            r = await _get(client, XHR, params={"url": url_param})
            r.raise_for_status()
            data = r.json()
            res = (data or {}).get("results") or {}
            rows = [x for cl in (res.get("cluster") or []) for x in (cl.get("result") or [])]
            await CACHE.set(key, rows, 3600)
            return rows

        pages = await asyncio.gather(*[page(n) for n in range(MAX_PAGES)],
                                     return_exceptions=True)
        rows: list[dict] = []
        for p in pages:
            if isinstance(p, list):
                rows.extend(p)
            elif isinstance(p, Exception) and not rows:
                raise p

        out, seen = [], set()
        for i, row in enumerate(rows):
            p = row.get("patent") or {}
            pn = normalize_pub(p.get("publication_number") or "")
            if not pn or pn in seen:
                continue
            seen.add(pn)
            out.append(Candidate(
                pub_number=pn, source=self.name, source_rank=i + 1,
                title=_clean(p.get("title")),
                snippet=_clean(p.get("snippet")),
                assignee=_clean(p.get("assignee")),
                inventors=[_clean(p["inventor"])] if p.get("inventor") else [],
                date=iso_date(p.get("publication_date") or p.get("filing_date")),
                priority_date=iso_date(p.get("priority_date")),
                url=google_url(pn),
                found_by=[sq.rationale or sq.element or "google patents"],
                extra={"thumbnail": p.get("thumbnail", "") or "",
                       "pdf": ("https://patentimages.storage.googleapis.com/" + p["pdf"])
                              if p.get("pdf") else ""},
            ))
        if not out and rows:
            self._warn("gpatents_direct returned rows with no publication numbers — "
                       "the XHR response shape changed")
        return out

    # -----------------------------------------------------------------------
    # full text
    # -----------------------------------------------------------------------
    async def details(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        """Everything the document page carries: biblio, English claims and
        description, the description split into sections, figures, PDF, citations."""
        if not available():
            return {}
        pn = canonical_pub(pub_number)
        if not pn:
            return {}
        key = f"gpd:d:{pn}"
        hit = await CACHE.get(key)
        if hit is not None:
            return hit
        try:
            r = await _get(client, DOC.format(pn=pn))
        except Exception:
            return {}
        if r.status_code != 200:
            return {}
        rec = parse_document(r.text, pn)
        if rec:
            await CACHE.set(key, rec, DOC_TTL)
        return rec


# ---------------------------------------------------------------------------
# parsing — one parser, every jurisdiction
# ---------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")
# Google wraps the untranslated original alongside the translation in
# <span class="google-src-text">. Keeping it would interleave German and English
# sentence by sentence, which is worse than either language alone.
_SRC_TEXT = re.compile(r'<span class="google-src-text"[\s\S]*?</span>', re.I)
_SECTION = re.compile(r'<section[^>]*itemprop="(claims|description|abstract)"[^>]*>([\s\S]*?)</section>', re.I)
_TRANSLATED = re.compile(r'Translated from <span itemprop="translatedLanguage">([^<]+)</span>', re.I)
_HEADING = re.compile(r"<heading[^>]*>([\s\S]{0,200}?)</heading>", re.I)
_IMG = re.compile(r'(https://patentimages\.storage\.googleapis\.com/[^"\'\s)]+\.(?:png|jpg|jpeg))', re.I)
_PDF = re.compile(r'(https://patentimages\.storage\.googleapis\.com/[^"\'\s)]+\.pdf)', re.I)
_META = re.compile(r'<meta name="(DC\.[a-zA-Z]+|citation_[a-z_]+)"[^>]*content="([^"]*)"', re.I)
_CPC = re.compile(r'<meta name="citation_patent_classification"[^>]*content="([^"]*)"', re.I)
_CITED = re.compile(r'itemprop="(patentCitations|citedBy)"[\s\S]*?</table>', re.I)
_PUBNUM_IN_ROW = re.compile(r'itemprop="publicationNumber">([^<]+)<')


def _text(fragment: str) -> str:
    s = _SRC_TEXT.sub(" ", fragment or "")
    s = re.sub(r"</(p|li|div|claim|claim-text|heading|h2|h3)>", "\n", s, flags=re.I)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def parse_document(page_html: str, pn: str) -> dict:
    """Google Patents HTML -> the record shape docstore.put understands."""
    if not page_html or "itemprop=" not in page_html:
        return {}
    rec: dict = {"pub_number": pn, "source": "gpatents_direct"}

    body = {}
    for m in _SECTION.finditer(page_html):
        body[m.group(1).lower()] = m.group(2)

    lang_m = _TRANSLATED.search(page_html)
    # A page carrying a "Translated from X" banner is showing us English.
    translated_from = lang_m.group(1).strip() if lang_m else ""
    lang = "en"

    if "claims" in body:
        rec["claims"] = _text(body["claims"])
        rec["claims_lang"] = lang
    if "description" in body:
        desc_html = body["description"]
        rec["description"] = _text(desc_html)
        rec["desc_lang"] = lang
        # Split on the document's own headings so a later stage can quote
        # "Background" or "Summary" without re-parsing 100 KB of prose.
        sections, last, ord_ = [], None, 0
        pieces = re.split(r"(<heading[^>]*>[\s\S]{0,200}?</heading>)", desc_html, flags=re.I)
        for piece in pieces:
            h = _HEADING.match(piece or "")
            if h:
                last = _text(h.group(1))[:80] or None
                continue
            txt = _text(piece)
            if txt and last:
                sections.append((last, ord_, txt))
                ord_ += 1
        if sections:
            rec["sections"] = sections[:60]
    if "abstract" in body:
        rec["abstract"] = _text(body["abstract"])
    if translated_from:
        rec["translated_from"] = translated_from

    meta = {}
    for m in _META.finditer(page_html):
        meta.setdefault(m.group(1).lower(), html.unescape(m.group(2)))
    rec["title"] = meta.get("dc.title", "").strip() or rec.get("title", "")
    if meta.get("citation_patent_publication_date") or meta.get("dc.date"):
        rec["date"] = iso_date(meta.get("citation_patent_publication_date") or meta.get("dc.date"))
    cpcs = [html.unescape(c).split(" ")[0] for c in _CPC.findall(page_html)]
    if cpcs:
        rec["cpc"] = list(dict.fromkeys(cpcs))[:12]

    imgs = list(dict.fromkeys(_IMG.findall(page_html)))
    if imgs:
        rec["images"] = imgs[:40]
    pdfs = _PDF.findall(page_html)
    if pdfs:
        rec["pdf"] = pdfs[0]

    for m in _CITED.finditer(page_html):
        block = m.group(0)
        pubs = [normalize_pub(x) for x in _PUBNUM_IN_ROW.findall(block)]
        pubs = [p for p in dict.fromkeys(pubs) if p and p != pn]
        if "patentcitations" in block[:60].lower():
            rec["backward"] = pubs[:80]
        else:
            rec["forward"] = pubs[:80]
    return rec


def _clean(s) -> str:
    if not isinstance(s, str):
        return ""
    return html.unescape(_TAG.sub("", s)).strip()
