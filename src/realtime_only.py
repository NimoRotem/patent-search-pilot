"""Providers that exist for a live search and may never be used to fill the corpus.

WHY
---
HimmPat is the only adapter in this tree that carries machine-translated English full text for
CN / JP / KR / TW, and its ledger is 250 units a day. One deep search spends about 48 of them, so
the whole allowance is roughly five searches. Bulk acquisition against the same key is not a
throughput problem to tune: 900,463 niche families at 250 a day is about 3,600 days, and every
unit a bulk job takes is a unit a live search cannot have.

So the rule is not "prefer another provider for bulk". It is "a bulk process cannot reach this
provider at all", and like `corpus_guard` it has to be a property of the code rather than
something an operator remembers. `corpus_guard` arms the ONE process that must not write; the
shape here is the mirror of that, because the process that MAY call is the one there is only one
of. This module therefore DEFAULTS TO DENY: nothing may call a real-time-only provider until a
process has declared itself a live search, and the only two callers of `enable()` in the tree are
the web app and the durable search worker.

    denied     every offline process: the acquisition worker, every `ops/` script, a cron job,
               a notebook, the test suite. No declaration is needed and none can be forgotten.
    enabled    `enable(reason)`, called once at startup by `webapp` and by `runner.worker`. The
               reason is printed, so a log line always says who took the allowance.

WHERE IT BITES
--------------
At the provider's own HTTP boundary (`sources.himmpat.HimmPat._post`), not at the call site. A
new bulk path written next year does not have to know this module exists; it gets a
`BulkUseBlocked` the first time it tries to spend a unit. The acquisition cascade in
`src/acquire/providers.py` additionally has no HimmPat rung at all and `build()` refuses to make
one, so the bulk fetcher cannot reach it even by configuration.

`tests/test_cjk_acquisition.py::test_defect_injection_removing_the_guard_lets_bulk_call_himmpat`
removes the check and asserts the call goes through, which is what makes the green test above it
mean something.
"""
from __future__ import annotations

#  The providers whose budget is reserved for a search that a person is waiting on. Keyed by the
#  adapter name, so `sources.registry()` and this set cannot drift apart silently.
REALTIME_ONLY = frozenset({"himmpat"})

_enabled = False
_reason = ""


class BulkUseBlocked(RuntimeError):
    """A bulk or backfill process tried to spend a real-time-only provider's allowance."""


def enable(reason: str) -> None:
    """Declare this process a live search. Idempotent. `reason` is required and is printed."""
    global _enabled, _reason
    if not reason:
        raise ValueError("realtime_only.enable needs a reason")
    if not _enabled:
        _enabled, _reason = True, reason
        print(f"[realtime_only] live search process: {reason} - "
              f"{', '.join(sorted(REALTIME_ONLY))} may be called here", flush=True)


def disable() -> None:
    """Back to the default. For tests, and for a batch job forked from an enabled parent."""
    global _enabled, _reason
    _enabled, _reason = False, ""


def enabled() -> bool:
    return _enabled


def reason() -> str:
    return _reason


def blocked_reason(provider: str) -> str:
    """Why `provider` is refused here, phrased for a health payload or a log line."""
    return (f"{provider} is reserved for real-time use during a search and this process has not "
            f"declared itself one. Its ledger is a few searches a day; a bulk job that spends it "
            f"leaves live searches with nothing. See src/realtime_only.py")


def allowed(provider: str) -> bool:
    return provider not in REALTIME_ONLY or _enabled


def check(provider: str) -> None:
    """Raise BulkUseBlocked unless `provider` may be called from this process."""
    if not allowed(provider):
        raise BulkUseBlocked(blocked_reason(provider))
