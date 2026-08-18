"""SerpApi Google Patents adapter — the "smart Google search" channel.

engine=google_patents for search; engine=google_patents_details for the
on-demand full record (claims, description, citations, family, figures).
This is the primary always-on channel: Google's worldwide index reached
through sophisticated boolean/CPC/assignee/date queries emitted by the planner.

Ported from patents-app `adapters/serpapi_gpatents.py`. Port change: the key is
read from SERPAPI_KEY (App A's supervisor conf) with SERPAPI_API_KEY accepted as
an alias (the pilot's existing .env spelling).
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import time

import httpx

from .schema import Candidate, SubQuery, normalize_pub, google_url, iso_date
from .base import Adapter, cached_json


SERP = "https://serpapi.com/search.json"

# Global SerpApi throttle: cap concurrent SerpApi calls + back off on 429 so a
# single search (many subqueries + detail fetches) can't hammer the credit-limited
# endpoint into "429 Too Many Requests".
_SEM = None
_BLOCKED_UNTIL = 0.0
_BLOCK_REASON = ""


class SerpApiQuotaExhausted(RuntimeError):
    pass


# Concurrency was 2, chosen when the plan was small. The account is a Big Data plan:
# 30,000 searches/month and a 6,000/hour rate limit, against ~1,000 used in a month.
# Two-at-a-time was the single biggest serialisation point in the fan-out and it was
# protecting a budget that is not remotely under pressure. The 429 backoff below is
# the real guard; this is just a politeness cap.
def _sem():
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(int(os.environ.get("SERPAPI_CONCURRENCY", "16")))
    return _SEM


# Results per search call. google_patents accepts 10-100 and bills ONE credit
# whatever the page size, so asking for 20 was throwing away 80% of every call.
SERP_NUM = max(10, min(100, int(os.environ.get("SERPAPI_NUM", "100"))))
# Extra pages to pull per query (each is a further credit). 1 = first page only.
SERP_PAGES = max(1, int(os.environ.get("SERPAPI_PAGES", "1")))


async def _serp_get(client, params, cache_key, ttl):
    global _BLOCKED_UNTIL, _BLOCK_REASON
    from .base import CACHE
    hit = await CACHE.get(cache_key)
    if hit is not None:
        return hit
    if _BLOCKED_UNTIL > time.monotonic():
        raise SerpApiQuotaExhausted(_BLOCK_REASON or "SerpApi quota is temporarily unavailable")
    async with _sem():
        last = None
        for attempt in range(4):
            try:
                r = await client.get(SERP, params=params)
                if r.status_code == 429:
                    try:
                        reason = str((r.json() or {}).get("error") or "serpapi 429")
                    except Exception:
                        reason = "serpapi 429"
                    if any(token in reason.lower() for token in ("run out", "quota", "searches")):
                        _BLOCK_REASON = reason[:160]
                        _BLOCKED_UNTIL = time.monotonic() + float(
                            os.environ.get("SERPAPI_QUOTA_COOLDOWN", "900"))
                        raise SerpApiQuotaExhausted(_BLOCK_REASON)
                    last = RuntimeError("serpapi 429")
                    await asyncio.sleep(1.2 * (2 ** attempt))   # 1.2, 2.4, 4.8, 9.6s
                    continue
                r.raise_for_status()
                d = r.json()
                await CACHE.set(cache_key, d, ttl)
                return d
            except SerpApiQuotaExhausted:
                raise
            except Exception as e:
                last = e
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last if isinstance(last, Exception) else RuntimeError("serpapi failed")


def _u(s):
    """SerpApi returns HTML-escaped text (e.g. 'A &amp; B'); decode to plain text
    so the frontend's own escaper doesn't double-escape it."""
    return html.unescape(s) if isinstance(s, str) else s


def _legal_status(data: dict) -> str:
    """Current legal-status string from SerpApi google_patents_details `events`
    (the entry whose date is 'Status', e.g. 'Active', 'Expired - Fee Related')."""
    for e in (data.get("events") or []):
        if not isinstance(e, dict):
            continue
        if e.get("type") == "legal-status" and str(e.get("date", "")).strip().lower() == "status":
            return _u(e.get("title", "") or "")
    return ""


def _legal_state(data: dict) -> str:
    """Normalize to a badge state: in_force | expired | pending | unknown.
    Uses the status string, then falls back to the anticipated-expiration date,
    then the grant/pending signal."""
    import datetime
    status = _legal_status(data).lower()
    if status:
        if any(w in status for w in ("expired", "lapsed", "ceased", "not in force")):
            return "expired"
        if any(w in status for w in ("abandoned", "withdrawn", "revoked", "refused")):
            return "abandoned"
        if "active" in status or "in force" in status or "granted" in status:
            return "in_force"
        if "pending" in status or "application" in status:
            return "pending"
    # fallback: anticipated expiration date vs today
    today = datetime.date.today().isoformat().replace("-", "")
    granted = False
    for e in (data.get("events") or []):
        if not isinstance(e, dict):
            continue
        t = (e.get("title", "") or "").lower()
        if e.get("type") == "granted":
            granted = True
        if "anticipated expiration" in t or "expir" in t:
            d = re.sub(r"\D", "", str(e.get("date", "")))[:8]
            if len(d) == 8:
                return "expired" if d <= today else "in_force"
    return "in_force" if granted else "unknown"


async def fetch_description_text(url: str, client: httpx.AsyncClient) -> str:
    """SerpApi google_patents_details gives description as a link to an HTML file,
    not inline. Fetch it and strip to plain text. Cached on the shared TTL cache."""
    from .base import CACHE
    if not url:
        return ""
    hit = await CACHE.get(f"serp:descr:{url}")
    if hit is not None:
        return hit
    try:
        r = await client.get(url)
        r.raise_for_status()
        raw = r.text
    except Exception:
        return ""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    txt = re.sub(r"\s+", " ", txt).strip()[:40000]
    await CACHE.set(f"serp:descr:{url}", txt, 86400)
    return txt


class SerpApiGooglePatents(Adapter):
    name = "serpapi_gpatents"

    def __init__(self):
        # App A's supervisor conf sets SERPAPI_KEY; the pilot's .env has
        # SERPAPI_API_KEY. Accept both so the same keys work unchanged.
        self.key = os.environ.get("SERPAPI_KEY", "") or os.environ.get("SERPAPI_API_KEY", "")

    def enabled(self) -> bool:
        return bool(self.key)

    def disabled_reason(self) -> str:
        return "SERPAPI_KEY / SERPAPI_API_KEY not set"

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        base = {
            "engine": "google_patents",
            "q": str(sq.native),
            "num": str(SERP_NUM),
            "api_key": self.key,
        }
        # Google Patents date filters use after=priority:YYYYMMDD / before=...
        # Only send a bound we actually have: an empty `before` is not a no-op,
        # it is a malformed filter sent whenever date_from is set without date_to.
        if sq.date_from:
            base["after"] = f"priority:{sq.date_from.replace('-', '')}"
        if sq.date_to:
            base["before"] = f"priority:{sq.date_to.replace('-', '')}"

        async def _page(page_no: int) -> list[dict]:
            params = dict(base)
            if page_no > 1:
                params["page"] = str(page_no)
            data = await _serp_get(
                client, params, f"serp:search:{sq.cache_key()}:{SERP_NUM}:{page_no}", 3600)
            return data.get("organic_results", []) or []

        # Pages are independent GETs, so fetch them together rather than walking
        # them. Page 1 alone is the default; SERPAPI_PAGES>1 buys depth per credit.
        pages = await asyncio.gather(*[_page(p) for p in range(1, SERP_PAGES + 1)],
                                     return_exceptions=True)
        rows: list[dict] = []
        for p in pages:
            if isinstance(p, list):
                rows.extend(p)
            elif isinstance(p, SerpApiQuotaExhausted) and not rows:
                # Nothing recovered at all — surface it, don't report an empty result set.
                raise p

        out: list[Candidate] = []
        seen: set[str] = set()
        for i, r in enumerate(rows):
            pub = r.get("publication_number") or r.get("patent_id") or ""
            npub = normalize_pub(pub)
            if not npub or npub in seen:
                continue
            seen.add(npub)
            out.append(Candidate(
                pub_number=npub,
                source=self.name,
                source_rank=i + 1,
                title=_u(r.get("title", "").strip()),
                snippet=_u(r.get("snippet", "") or ""),
                assignee=_u(r.get("assignee", "") or ""),
                inventors=[_u(r["inventor"])] if r.get("inventor") else [],
                date=iso_date(r.get("publication_date") or r.get("filing_date")),
                priority_date=iso_date(r.get("priority_date")),
                cpc=[],
                url=r.get("patent_link") or google_url(pub),
                found_by=[sq.rationale or sq.element],
                extra={"serpapi_link": r.get("serpapi_link", ""),
                       "figures": r.get("figures", []),
                       "thumbnail": r.get("thumbnail", "") or "",
                       "raw_id": r.get("patent_id", "")},
            ))
        return out

    async def details(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        # google_patents_details wants patent_id like "patent/US9876543B2/en"
        pid = pub_number
        if not pid.startswith("patent/"):
            pid = f"patent/{normalize_pub(pub_number)}/en"
        params = {"engine": "google_patents_details", "patent_id": pid, "api_key": self.key}
        try:
            data = await _serp_get(client, params, f"serp:details:{pid}", 86400)
        except Exception:
            return {}
        claims = data.get("claims") or []
        if isinstance(claims, list):
            claims_text = "\n".join(c if isinstance(c, str) else (c or {}).get("text", "")
                                    for c in claims)
        else:
            claims_text = str(claims)

        # inventors / assignees can be list[str] or list[dict]
        def _names(v):
            out = []
            for x in (v or []):
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    out.append(x.get("name") or x.get("assignee") or x.get("inventor") or "")
            return [n for n in out if n]
        cpc = [c.get("code") for c in (data.get("classifications") or [])
               if isinstance(c, dict) and c.get("code")]
        images = []
        for im in (data.get("images") or []):
            if isinstance(im, str):
                images.append(im)
            elif isinstance(im, dict) and im.get("full"):
                images.append(im["full"])
            elif isinstance(im, dict) and im.get("thumbnail"):
                images.append(im["thumbnail"])
        return {
            "title": _u(data.get("title", "") or ""),
            "claims": _u(claims_text[:20000]),
            "description": _u((data.get("description") or "")[:40000]),
            "description_link": data.get("description_link", "") or "",
            "cpc": cpc,
            "inventors": [_u(x) for x in _names(data.get("inventors") or data.get("inventor"))],
            "assignee": _u(", ".join(_names(data.get("assignees")))
                           or (data.get("assignee") if isinstance(data.get("assignee"), str) else "")),
            "priority_date": data.get("priority_date", "") or "",
            "filing_date": data.get("filing_date", "") or "",
            "publication_date": data.get("publication_date", "") or "",
            "pdf": data.get("pdf", "") or "",
            "family": [normalize_pub(x.get("publication_number", ""))
                       for x in (data.get("worldwide_applications", {}) or {}).get("all", [])
                       if isinstance(x, dict) and x.get("publication_number")],
            "cited_by": [normalize_pub(x.get("publication_number", ""))
                         for x in (data.get("cited_by", {}) or {}).get("original", [])
                         if isinstance(x, dict) and x.get("publication_number")],
            "patent_citations": [normalize_pub(x.get("publication_number", ""))
                                 for x in (data.get("patent_citations", {}) or {}).get("original", [])
                                 if isinstance(x, dict) and x.get("publication_number")],
            "abstract": _u(data.get("abstract", "") or ""),
            "images": images,
            "legal_status": _legal_status(data),
            "legal_state": _legal_state(data),
        }

    async def citations(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        d = await self.details(pub_number, client)
        return {"backward": d.get("patent_citations", []), "forward": d.get("cited_by", [])}

    async def family(self, pub_number: str, client: httpx.AsyncClient) -> list[str]:
        d = await self.details(pub_number, client)
        return d.get("family", [])
