"""Explicitly configured self-hosted full-text endpoint.

The endpoint is disabled by default. Operators should point it only at a bounded,
cache-first service that does not hide unmetered paid fallbacks.
"""
from __future__ import annotations

import base64
import json
import os

import requests

from ..models import FetchRequest, ProviderResult
from .base import BaseProvider, ProviderTemporaryError, quick_completeness


class SelfSerpProvider(BaseProvider):
    name = "self_serp"

    def __init__(self, url: str | None = None, http=None, timeout: float = 60):
        self.url = url if url is not None else os.environ.get("NICHE_SELF_SERP_URL", "")
        self.http = http or requests.Session()
        self.timeout = timeout

    def enabled(self) -> bool:
        return self.url.startswith(("http://", "https://"))

    def disabled_reason(self) -> str:
        return "NICHE_SELF_SERP_URL is not configured" if not self.enabled() else ""

    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        response = self.http.post(
            self.url,
            json={"publication_number": request.publication_number, "want": sorted(request.missing_fields)},
            timeout=self.timeout,
        )
        status = int(response.status_code)
        if status == 404:
            return None
        if status == 429 or status >= 500:
            raise ProviderTemporaryError(f"self-hosted fetch returned HTTP {status}", http_status=status)
        if status < 200 or status >= 300:
            return None
        data = response.json()
        if data.get("content_base64"):
            content = base64.b64decode(data["content_base64"])
        elif data.get("content") is not None:
            content = str(data["content"]).encode("utf-8")
        else:
            content = json.dumps(data.get("document") or data, ensure_ascii=False).encode("utf-8")
        media_type = str(data.get("media_type") or "application/json")
        return ProviderResult(
            provider=self.name,
            content=content,
            media_type=media_type,
            source_url=str(data.get("source_url") or self.url),
            http_status=status,
            credits_used=0,
            completeness=quick_completeness(content),
            response_headers=dict(response.headers or {}),
        )
