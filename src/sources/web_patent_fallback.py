"""SerpApi-independent Google Patents search fallbacks.

Tries ScrapingBee -> Firecrawl -> Tavily serially and stops on usable patent
hits. Ported from patents-app `adapters/web_patent_fallback.py` unchanged in
behavior.
"""
from __future__ import annotations

import os
import re
import hashlib
import asyncio
import random
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import httpx

from .schema import Candidate, SubQuery, canonical_pub, google_url
from .base import Adapter, CACHE


_PATENT_URL = re.compile(r"/patent/([^/?#]+)", re.I)
_VALID_PUB = re.compile(r"^[A-Z]{2}[A-Z]{0,3}\d{4,14}[A-Z]\d?$")
_SOURCE_IDS = {
    "scrapingbee": "scrapingbee_patents",
    "firecrawl": "firecrawl_patents",
    "tavily": "tavily_patents",
}


class WebPatentFallback(Adapter):
    """Try paid web-search providers serially and stop on usable patent hits."""

    name = "web_patent_fallback"
    planner_visible = False

    def __init__(self, *, keys: Optional[dict] = None,
                 order: Optional[tuple] = None):
        self.keys = keys or {
            "scrapingbee": os.environ.get("SCRAPINGBEE_API_KEY", ""),
            "firecrawl": os.environ.get("FIRECRAWL_API_KEY", ""),
            "tavily": os.environ.get("TAVILY_API_KEY", ""),
        }
        self.order = order or ("scrapingbee", "firecrawl", "tavily")
        self._providers: dict[str, Callable[..., Awaitable[list]]] = {
            "scrapingbee": self._scrapingbee,
            "firecrawl": self._firecrawl,
            "tavily": self._tavily,
        }

    def enabled(self) -> bool:
        return any(self.keys.get(name) for name in self.order)

    def disabled_reason(self) -> str:
        return "No Tavily, ScrapingBee, or Firecrawl key is configured"

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        query = str(sq.native or "").strip()
        if not query:
            return []
        for provider in self.order:
            fn = self._providers.get(provider)
            if not self.keys.get(provider) or fn is None:
                continue
            fingerprint = hashlib.sha256(
                f"{query}\n{sq.date_from or ''}\n{sq.date_to or ''}".encode("utf-8")
            ).hexdigest()
            cache_key = f"web-patent-search:{provider}:{fingerprint}"
            rows = await CACHE.get(cache_key)
            try:
                if rows is None:
                    rows = await self._call_provider(fn, query, sq, client)
                    await CACHE.set(cache_key, rows, 3600)
            except Exception:
                continue
            candidates = self._candidates(rows, provider, sq)
            if candidates:
                return candidates
        return []

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status == 429 or status >= 500
        return False

    async def _call_provider(self, fn, query: str, sq: SubQuery,
                             client: httpx.AsyncClient) -> list:
        for attempt in range(3):
            try:
                return await fn(query, sq, client)
            except Exception as exc:
                if not self._retryable(exc) or attempt == 2:
                    raise
                await asyncio.sleep(min(60.0, (2 ** attempt) + random.random()))
        return []

    @staticmethod
    def _web_query(query: str) -> str:
        query = re.sub(r"\bCPC\s*=\s*([A-Z0-9/]+)", r'"\1"', query, flags=re.I)
        query = re.sub(r"\s+", " ", query).strip()
        return f"site:patents.google.com/patent/ {query}"

    async def _scrapingbee(self, query: str, sq: SubQuery,
                           client: httpx.AsyncClient) -> list:
        response = await client.get(
            "https://app.scrapingbee.com/api/v1/fast_search",
            headers={"Authorization": f"Bearer {self.keys['scrapingbee']}"},
            params={
                "search": self._web_query(query),
                "country_code": "us",
                "language": "en",
            },
        )
        response.raise_for_status()
        return response.json().get("organic") or []

    async def _firecrawl(self, query: str, sq: SubQuery,
                         client: httpx.AsyncClient) -> list:
        response = await client.post(
            "https://api.firecrawl.dev/v2/search",
            headers={
                "Authorization": f"Bearer {self.keys['firecrawl']}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "limit": 10,
                "sources": ["web"],
                "includeDomains": ["patents.google.com"],
                "country": "US",
                "timeout": 30000,
            },
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        return data.get("web") or []

    async def _tavily(self, query: str, sq: SubQuery,
                      client: httpx.AsyncClient) -> list:
        payload = {
            "query": query,
            "search_depth": "basic",
            "max_results": 10,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_domains": ["patents.google.com"],
            "auto_parameters": False,
        }
        if sq.date_from:
            payload["start_date"] = sq.date_from
        if sq.date_to:
            payload["end_date"] = sq.date_to
        response = await client.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": f"Bearer {self.keys['tavily']}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json().get("results") or []

    @staticmethod
    def _candidates(rows: list, provider: str, sq: SubQuery) -> list[Candidate]:
        out, seen = [], set()
        for row in rows or []:
            url = str(row.get("url") or row.get("link") or "")
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or parsed.hostname != "patents.google.com":
                continue
            match = _PATENT_URL.search(url)
            if not match:
                continue
            pub = canonical_pub(match.group(1))
            if not _VALID_PUB.fullmatch(pub) or pub in seen:
                continue
            seen.add(pub)
            title = re.sub(r"\s*[-|]\s*Google Patents\s*$", "", str(row.get("title") or ""),
                           flags=re.I).strip()
            out.append(Candidate(
                pub_number=pub,
                source=_SOURCE_IDS[provider],
                source_rank=len(out) + 1,
                title=title,
                snippet=str(row.get("snippet") or row.get("description") or row.get("content") or ""),
                url=google_url(pub),
                found_by=[sq.rationale or sq.element],
                extra={"fallback_from": "serpapi_gpatents", "fallback_provider": provider},
            ))
        return out
