"""EPO OPS full-text wrapper for EP and WO publications."""
from __future__ import annotations

import json

import httpx

from ..models import Completeness, FetchRequest, ProviderResult
from .base import BaseProvider, ProviderTemporaryError


async def _fulltext(adapter, publication_number: str, timeout: float):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await adapter.fulltext(publication_number, client)


class EPOProvider(BaseProvider):
    name = "epo"
    authorities = frozenset({"EP", "WO"})

    def __init__(self, adapter=None, timeout: float = 60):
        self._adapter = adapter
        self.timeout = timeout

    @property
    def adapter(self):
        if self._adapter is None:
            from sources.epo_ops import EPO_OPS
            self._adapter = EPO_OPS()
        return self._adapter

    def enabled(self) -> bool:
        try:
            return bool(self.adapter.enabled() and hasattr(self.adapter, "fulltext"))
        except (ImportError, AttributeError, RuntimeError, ValueError):
            return False

    def disabled_reason(self) -> str:
        return "EPO OPS credentials are not set" if not self.enabled() else ""

    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        try:
            from sources import _submit

            data = _submit(
                _fulltext(self.adapter, request.publication_number, self.timeout),
                timeout=self.timeout + 10,
            )
        except Exception as exc:
            raise ProviderTemporaryError(f"EPO OPS request failed: {type(exc).__name__}") from exc
        if not data:
            return None
        completeness = Completeness(
            has_title=bool(data.get("title")),
            has_abstract=bool(data.get("abstract")),
            has_claims=bool(data.get("claims")),
            has_complete_claims=bool(data.get("claims")),
            has_description=bool(data.get("description") or data.get("description_paragraphs")),
            has_complete_description=bool(data.get("description") or data.get("description_paragraphs")),
            has_citations=bool(data.get("citations") or data.get("backward")),
        )
        return ProviderResult(
            provider=self.name,
            content=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
            media_type="application/json",
            source_url="https://ops.epo.org/",
            http_status=200,
            credits_used=0,
            completeness=completeness,
            metadata={"official_source": True},
        )
