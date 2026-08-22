"""SerpApi Google Patents Details final fallback."""
from __future__ import annotations

import json
import os
import time

import requests

from ..identifiers import google_patent_url, normalize_publication_number
from ..models import Completeness, FetchRequest, ProviderResult
from .base import BaseProvider, ProviderPermanentError, ProviderTemporaryError


class SerpApiProvider(BaseProvider):
    name = "serpapi"
    paid = True
    endpoint = "https://serpapi.com/search.json"

    def __init__(self, api_key: str | None = None, http=None, timeout: float = 60):
        self.api_key = api_key if api_key is not None else (
            os.environ.get("SERPAPI_KEY", "") or os.environ.get("SERPAPI_API_KEY", "")
        )
        self.http = http or requests.Session()
        self.timeout = timeout

    def enabled(self) -> bool:
        return bool(self.api_key)

    def disabled_reason(self) -> str:
        return "SERPAPI_KEY or SERPAPI_API_KEY is not set" if not self.api_key else ""

    def estimated_credits(self, request: FetchRequest) -> int:
        return 1

    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        publication = normalize_publication_number(request.publication_number)
        target = google_patent_url(publication, request.language)
        patent_id = target.split("patents.google.com/", 1)[1]
        started = time.monotonic()
        response = self.http.get(
            self.endpoint,
            params={
                "engine": "google_patents_details",
                "patent_id": patent_id,
                "api_key": self.api_key,
            },
            timeout=self.timeout,
        )
        latency = round((time.monotonic() - started) * 1000)
        status = int(response.status_code)
        if status == 429 or status >= 500:
            raise ProviderTemporaryError(
                f"SerpApi returned HTTP {status}", http_status=status, credits_used=1
            )
        if status < 200 or status >= 300:
            raise ProviderPermanentError(
                f"SerpApi returned HTTP {status}", http_status=status, credits_used=1
            )
        data = response.json() or {}
        if data.get("error"):
            raise ProviderPermanentError(str(data["error"]), http_status=status, credits_used=1)
        claims = data.get("claims") or []
        description = data.get("description") or ""
        completeness = Completeness(
            has_title=bool(data.get("title")),
            has_abstract=bool(data.get("abstract")),
            has_claims=bool(claims),
            has_complete_claims=bool(claims),
            has_description=bool(description),
            has_complete_description=bool(description),
            has_figures=bool(data.get("images")),
            has_citations=bool(data.get("patent_citations") or data.get("cited_by")),
        )
        return ProviderResult(
            provider=self.name,
            content=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
            media_type="application/json",
            source_url=target,
            http_status=status,
            latency_ms=latency,
            credits_used=1,
            completeness=completeness,
            response_headers=dict(response.headers or {}),
            metadata={"serpapi_patent_id": patent_id},
        )
