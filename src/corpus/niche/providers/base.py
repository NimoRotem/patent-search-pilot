"""Provider interface shared by free, local, and paid acquisition sources."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Completeness, FetchRequest, ProviderResult


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None, credits_used: int = 0):
        super().__init__(message)
        self.http_status = http_status
        self.credits_used = max(0, int(credits_used))


class ProviderTemporaryError(ProviderError):
    """A retryable provider failure, such as throttling or a transient outage."""


class ProviderPermanentError(ProviderError):
    """A request-specific failure that another provider may still satisfy."""


class BaseProvider(ABC):
    name = "base"
    paid = False
    authorities: frozenset[str] = frozenset()

    def enabled(self) -> bool:
        return True

    def disabled_reason(self) -> str:
        return ""

    def supports(self, request: FetchRequest) -> bool:
        return not self.authorities or request.authority.upper() in self.authorities

    def estimated_credits(self, request: FetchRequest) -> int:
        return 0

    @abstractmethod
    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        raise NotImplementedError


def quick_completeness(content: bytes | str) -> Completeness:
    """Conservative source-markup signal used before the full parser runs."""
    if isinstance(content, bytes):
        text = content.decode("utf-8", "replace")
    else:
        text = str(content or "")
    lowered = text.lower()
    claims = any(marker in lowered for marker in (
        'itemprop="claims"', "<claims", "<claim ", "\"claims\"",
    ))
    description = any(marker in lowered for marker in (
        'itemprop="description"', "<description", "description_paragraphs",
    ))
    return Completeness(
        has_claims=claims,
        has_complete_claims=claims,
        has_description=description,
        has_complete_description=description,
    )
