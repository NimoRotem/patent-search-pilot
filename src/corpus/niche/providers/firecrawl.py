"""Firecrawl single-page fallback for deterministic patent URLs."""
from __future__ import annotations

import os
import time

import requests

from ..identifiers import google_patent_url
from ..models import FetchRequest, ProviderResult
from .base import (
    BaseProvider,
    ProviderPermanentError,
    ProviderTemporaryError,
    quick_completeness,
)


class FirecrawlProvider(BaseProvider):
    name = "firecrawl"
    paid = True
    endpoint = "https://api.firecrawl.dev/v2/scrape"

    def __init__(self, api_key: str | None = None, http=None, timeout: float = 90):
        self.api_key = api_key if api_key is not None else os.environ.get("FIRECRAWL_API_KEY", "")
        self.http = http or requests.Session()
        self.timeout = timeout

    def enabled(self) -> bool:
        return bool(self.api_key)

    def disabled_reason(self) -> str:
        return "FIRECRAWL_API_KEY is not set" if not self.api_key else ""

    def estimated_credits(self, request: FetchRequest) -> int:
        return 1

    def fetch(self, request: FetchRequest) -> ProviderResult:
        target = google_patent_url(request.publication_number, request.language)
        started = time.monotonic()
        response = self.http.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "url": target,
                "formats": ["rawHtml"],
                "onlyMainContent": False,
                "proxy": "basic",
                "timeout": int(self.timeout * 1000),
            },
            timeout=self.timeout + 5,
        )
        latency = round((time.monotonic() - started) * 1000)
        status = int(response.status_code)
        if status == 429 or status >= 500:
            raise ProviderTemporaryError(
                f"Firecrawl returned HTTP {status}", http_status=status, credits_used=0
            )
        if status < 200 or status >= 300:
            raise ProviderPermanentError(
                f"Firecrawl returned HTTP {status}", http_status=status, credits_used=0
            )
        payload = response.json() or {}
        data = payload.get("data") or {}
        if not payload.get("success", True):
            raise ProviderPermanentError(str(payload.get("error") or "Firecrawl scrape failed"))
        content = data.get("rawHtml") or data.get("html") or data.get("markdown") or ""
        metadata = data.get("metadata") or {}
        credits = int(
            metadata.get("creditsUsed")
            or payload.get("creditsUsed")
            or (payload.get("metadata") or {}).get("creditsUsed")
            or 1
        )
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return ProviderResult(
            provider=self.name,
            content=raw,
            media_type="text/html",
            source_url=target,
            http_status=status,
            latency_ms=latency,
            credits_used=credits,
            completeness=quick_completeness(raw),
            response_headers=dict(getattr(response, "headers", {}) or {}),
            metadata={"firecrawl_format": "rawHtml"},
        )
