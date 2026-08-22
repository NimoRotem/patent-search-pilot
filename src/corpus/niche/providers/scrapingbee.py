"""ScrapingBee fallback with a no-JavaScript request before one JS retry."""
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


def _header_int(headers, name: str, default: int) -> int:
    lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
    try:
        return max(0, int(lowered.get(name.lower(), default)))
    except (TypeError, ValueError):
        return default


class ScrapingBeeProvider(BaseProvider):
    name = "scrapingbee"
    paid = True
    endpoint = "https://app.scrapingbee.com/api/v1/"

    def __init__(self, api_key: str | None = None, http=None, timeout: float = 90):
        self.api_key = api_key if api_key is not None else os.environ.get("SCRAPINGBEE_API_KEY", "")
        self.http = http or requests.Session()
        self.timeout = timeout

    def enabled(self) -> bool:
        return bool(self.api_key)

    def disabled_reason(self) -> str:
        return "SCRAPINGBEE_API_KEY is not set" if not self.api_key else ""

    def estimated_credits(self, request: FetchRequest) -> int:
        # Google-domain requests cost 15 credits. Reserve both the plain and JS calls.
        return 30

    def _one(self, target: str, render_js: bool):
        return self.http.get(
            self.endpoint,
            params={
                "api_key": self.api_key,
                "url": target,
                "custom_google": "true",
                "render_js": "true" if render_js else "false",
                "premium_proxy": "false",
            },
            timeout=self.timeout,
        )

    @staticmethod
    def _usable(content: bytes) -> bool:
        lowered = content.lower()
        return b'itemprop="claims"' in lowered or b'itemprop="description"' in lowered

    def fetch(self, request: FetchRequest) -> ProviderResult:
        target = google_patent_url(request.publication_number, request.language)
        started = time.monotonic()
        total_credits = 0
        response = None
        content = b""
        for render_js in (False, True):
            response = self._one(target, render_js)
            status = int(response.status_code)
            total_credits += _header_int(response.headers, "spb-cost", 15 if status == 200 else 0)
            if status == 429 or status >= 500:
                raise ProviderTemporaryError(
                    f"ScrapingBee returned HTTP {status}",
                    http_status=status,
                    credits_used=total_credits,
                )
            if status < 200 or status >= 300:
                raise ProviderPermanentError(
                    f"ScrapingBee returned HTTP {status}",
                    http_status=status,
                    credits_used=total_credits,
                )
            content = bytes(response.content or b"")
            if self._usable(content):
                break
        latency = round((time.monotonic() - started) * 1000)
        return ProviderResult(
            provider=self.name,
            content=content,
            media_type="text/html",
            source_url=target,
            http_status=int(response.status_code) if response is not None else None,
            latency_ms=latency,
            credits_used=total_credits,
            completeness=quick_completeness(content),
            response_headers=dict(getattr(response, "headers", {}) or {}),
            metadata={"render_js_used": len(content) > 0 and total_credits > 15},
        )
