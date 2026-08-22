"""Conservative direct Google Patents document fetch with permanent raw HTML output."""
from __future__ import annotations

import os
import threading
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

_PACE_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


class GooglePatentsProvider(BaseProvider):
    name = "google_patents"

    def __init__(self, http=None, timeout: float = 45, min_interval: float | None = None):
        self.http = http or requests.Session()
        self.timeout = timeout
        self.min_interval = float(
            min_interval if min_interval is not None
            else os.environ.get("NICHE_GOOGLE_MIN_INTERVAL", "1.0")
        )

    def _pace(self):
        global _LAST_REQUEST
        with _PACE_LOCK:
            delay = self.min_interval - (time.monotonic() - _LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
            _LAST_REQUEST = time.monotonic()

    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        target = google_patent_url(request.publication_number, request.language)
        self._pace()
        started = time.monotonic()
        response = self.http.get(
            target,
            headers={
                "User-Agent": os.environ.get(
                    "GPATENTS_UA",
                    "Mozilla/5.0 (compatible; NichePatentCorpus/1.0; +https://nimo.iptorch.com/)",
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=self.timeout,
        )
        latency = round((time.monotonic() - started) * 1000)
        status = int(response.status_code)
        if status == 404:
            return None
        if status in {403, 429, 503} or status >= 500:
            raise ProviderTemporaryError(
                f"Google Patents returned HTTP {status}", http_status=status
            )
        if status < 200 or status >= 300:
            raise ProviderPermanentError(
                f"Google Patents returned HTTP {status}", http_status=status
            )
        content = bytes(response.content or b"")
        return ProviderResult(
            provider=self.name,
            content=content,
            media_type="text/html",
            source_url=target,
            http_status=status,
            latency_ms=latency,
            credits_used=0,
            completeness=quick_completeness(content),
            response_headers=dict(response.headers or {}),
        )
