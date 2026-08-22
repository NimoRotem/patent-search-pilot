"""Official USPTO ODP wrapper for US publication metadata and available text."""
from __future__ import annotations

import json

import httpx

from ..models import Completeness, FetchRequest, ProviderResult
from .base import BaseProvider, ProviderTemporaryError


async def _details(adapter, publication_number: str, timeout: float):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await adapter.details(publication_number, client)


class USPTOProvider(BaseProvider):
    name = "uspto"
    authorities = frozenset({"US"})

    def __init__(self, adapter=None, timeout: float = 60):
        self._adapter = adapter
        self.timeout = timeout

    @property
    def adapter(self):
        if self._adapter is None:
            from sources.uspto import USPTO
            self._adapter = USPTO()
        return self._adapter

    def enabled(self) -> bool:
        try:
            return bool(self.adapter.enabled())
        except (ImportError, AttributeError, RuntimeError, ValueError):
            return False

    def disabled_reason(self) -> str:
        return "USPTO_ODP_KEY or ODP_API_KEY is not set" if not self.enabled() else ""

    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        try:
            from sources import _submit

            data = _submit(
                _details(self.adapter, request.publication_number, self.timeout),
                timeout=self.timeout + 10,
            )
        except Exception as exc:
            raise ProviderTemporaryError(f"USPTO ODP request failed: {type(exc).__name__}") from exc
        if not data:
            return None
        claims = data.get("claims") or []
        description = data.get("description") or ""
        completeness = Completeness(
            has_title=bool(data.get("title")),
            has_abstract=bool(data.get("abstract")),
            has_claims=bool(claims),
            has_complete_claims=bool(claims),
            has_description=bool(description),
            has_complete_description=bool(description),
            has_citations=bool(data.get("citations")),
        )
        return ProviderResult(
            provider=self.name,
            content=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
            media_type="application/json",
            source_url="https://data.uspto.gov/",
            http_status=200,
            credits_used=0,
            completeness=completeness,
            metadata={"official_source": True},
        )
