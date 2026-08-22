"""Cheapest-first provider execution with error isolation and hard budgets."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .limits import PaidBudget
from .models import FetchRequest, ProviderResult
from .providers.base import BaseProvider

_DEFAULT_ORDER = (
    "local",
    "marec",
    "uspto",
    "epo",
    "google_patents",
    "self_serp",
    "firecrawl",
    "scrapingbee",
    "serpapi",
)


class PipelineFatalError(RuntimeError):
    """A local durability failure that must stop provider fallback immediately."""


def default_provider_names() -> list[str]:
    return list(_DEFAULT_ORDER)


@dataclass(frozen=True)
class FetchAttempt:
    provider: str
    status: str
    latency_ms: int = 0
    http_status: int | None = None
    credits_used: int = 0
    bytes_received: int = 0
    error_class: str = ""
    error: str = ""


@dataclass(frozen=True)
class FetchOutcome:
    status: str
    result: ProviderResult | None
    attempts: tuple[FetchAttempt, ...]
    artifacts: tuple[ProviderResult, ...] = ()


def _satisfies(request: FetchRequest, result: ProviderResult) -> bool:
    completeness = result.completeness
    if "claims" in request.missing_fields and not completeness.has_complete_claims:
        return False
    if "description" in request.missing_fields and not completeness.has_complete_description:
        return False
    return bool(result.content)


class ProviderWaterfall:
    def __init__(
        self,
        providers: Iterable[BaseProvider],
        budget: PaidBudget,
        *,
        on_result: Callable[[FetchRequest, ProviderResult], None] | None = None,
        validator: Callable[[FetchRequest, ProviderResult], ProviderResult] | None = None,
    ):
        self.providers = list(providers)
        self.budget = budget
        self.on_result = on_result
        self.validator = validator

    def fetch(self, request: FetchRequest) -> FetchOutcome:
        if (
            request.completeness.full_text_complete or not request.missing_fields
        ) and not request.require_artifact:
            return FetchOutcome("already_complete", None, ())

        attempts = []
        artifacts = []
        for provider in self.providers:
            if (
                (request.local_only and provider.name != "local")
                or provider.name in request.skip_providers
                or not provider.enabled()
                or not provider.supports(request)
            ):
                continue
            reserve = provider.estimated_credits(request) if provider.paid else 0
            if provider.paid and (reserve <= 0 or not self.budget.reserve(provider.name, reserve)):
                attempts.append(FetchAttempt(provider.name, "budget_exhausted"))
                continue

            started = time.monotonic()
            actual = reserve if provider.paid else 0
            settled = False
            try:
                result = provider.fetch(request)
                latency = max(0, round((time.monotonic() - started) * 1000))
                actual = result.credits_used if result else 0
                if provider.paid:
                    self.budget.settle(provider.name, reserve, actual)
                    settled = True
                if result is None or not result.content:
                    attempts.append(FetchAttempt(provider.name, "empty", latency_ms=latency,
                                                 credits_used=actual))
                    continue
                artifacts.append(result)
                if self.on_result:
                    self.on_result(request, result)
                if self.validator:
                    result = self.validator(request, result)
                status = "success" if _satisfies(request, result) else "partial"
                attempts.append(FetchAttempt(
                    provider.name,
                    status,
                    latency_ms=result.latency_ms or latency,
                    http_status=result.http_status,
                    credits_used=result.credits_used,
                    bytes_received=len(result.content),
                ))
                if status == "success":
                    return FetchOutcome("completed", result, tuple(attempts), tuple(artifacts))
            except Exception as exc:  # noqa: BLE001 - each provider is an isolated fallback rung
                latency = max(0, round((time.monotonic() - started) * 1000))
                reported_credits = getattr(exc, "credits_used", None)
                if reported_credits is not None:
                    actual = max(0, int(reported_credits or 0))
                if provider.paid and not settled:
                    self.budget.settle(provider.name, reserve, min(actual, reserve))
                attempts.append(FetchAttempt(
                    provider.name,
                    "error",
                    latency_ms=latency,
                    http_status=getattr(exc, "http_status", None),
                    credits_used=actual,
                    bytes_received=0,
                    error_class=type(exc).__name__,
                    error=str(exc)[:500],
                ))
                if isinstance(exc, PipelineFatalError):
                    return FetchOutcome(
                        "fatal", None, tuple(attempts), tuple(artifacts)
                    )
        return FetchOutcome("missing", None, tuple(attempts), tuple(artifacts))
