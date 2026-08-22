"""Fail-closed paid-provider and retry limits."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock

_ENV_TO_PROVIDER = {
    "MAX_FIRECRAWL_CREDITS_PER_RUN": "firecrawl",
    "MAX_SCRAPINGBEE_CREDITS_PER_RUN": "scrapingbee",
    "MAX_SERPAPI_REQUESTS_PER_RUN": "serpapi",
}


def _nonnegative_int(value, *, default: int = 0) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def bounded_positive(value, *, default: int, maximum: int) -> int:
    parsed = _nonnegative_int(value, default=default)
    if parsed <= 0:
        return 0
    return min(parsed, maximum)


@dataclass(frozen=True)
class PaidLimits:
    caps: dict[str, int]
    invalid: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PaidLimits:
        caps = {}
        invalid = []
        for variable, provider in _ENV_TO_PROVIDER.items():
            raw = env.get(variable)
            cap = _nonnegative_int(raw, default=0)
            if raw is None or str(raw).strip() != str(cap) or cap < 0:
                invalid.append(variable)
                cap = 0
            caps[provider] = cap
        return cls(caps, tuple(invalid))


class PaidBudget:
    """A per-run hard ledger that reserves worst-case credits before each call."""

    def __init__(self, caps: Mapping[str, int]):
        self.caps = {str(key): max(0, int(value)) for key, value in caps.items()}
        self.spent = {key: 0 for key in self.caps}
        self._lock = Lock()

    def remaining(self, provider: str) -> int:
        with self._lock:
            return max(0, self.caps.get(provider, 0) - self.spent.get(provider, 0))

    def reserve(self, provider: str, credits: int) -> bool:
        credits = max(0, int(credits))
        with self._lock:
            remaining = max(
                0, self.caps.get(provider, 0) - self.spent.get(provider, 0)
            )
            if credits > remaining:
                return False
            self.spent[provider] = self.spent.get(provider, 0) + credits
            return True

    def settle(self, provider: str, reserved: int, actual: int) -> None:
        reserved, actual = max(0, int(reserved)), max(0, int(actual))
        if actual > reserved:
            raise RuntimeError(
                f"{provider} used {actual} credits after reserving only {reserved}; "
                "the provider estimate is not a safe upper bound"
            )
        with self._lock:
            self.spent[provider] = max(
                0, self.spent.get(provider, 0) - reserved + actual
            )

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.spent)


class RunJobBudget:
    """One process-wide job ceiling shared by every worker thread."""

    def __init__(self, maximum: int = 0):
        self.maximum = max(0, int(maximum))
        self._processed = 0
        self._lock = Lock()

    @property
    def processed(self) -> int:
        with self._lock:
            return self._processed

    def acquire(self) -> bool:
        with self._lock:
            if self.maximum and self._processed >= self.maximum:
                return False
            self._processed += 1
            return True

    def release_unprocessed(self) -> None:
        with self._lock:
            self._processed = max(0, self._processed - 1)
