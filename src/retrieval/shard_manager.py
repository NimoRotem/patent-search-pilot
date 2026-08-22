"""SEAM: the lifecycle of the dormant domain shards. Workstream E owns the VMs.

`shard_router` decides WHICH domains a query needs. This decides whether those shards are up, how
long to wait for one that is starting, and what a channel gets while it is not up: an empty list,
never an exception and never a block.

CONTRACT.

    manager.state(domain)                 -> "hot" | "waking" | "cold" | "unknown"
    manager.ensure(domains, timeout=...)  -> {domain: state} after at most `timeout` seconds
    manager.connection(domain)            -> a psycopg connection to that shard, or None
    manager.release(domain, conn)         -> give it back

WHY `connection` AND NOT `search`. The cold shards hold the same schema as the hot corpus, so the
cold dense and cold lexical channels are the SAME SQL this package already runs, pointed at a
different host. Handing back a connection keeps one implementation of each channel instead of two
that drift.

THE RULE THE ORCHESTRATOR ENFORCES: a shard that is not hot within the timeout is not waited for.
The local answer is complete without it, and the architecture runs the cold tier in parallel with
the hot one precisely so that a cold miss costs nothing but the art it would have added.

UNTIL WORKSTREAM E LANDS: every domain is "unknown", `ensure` returns immediately, `connection`
returns None, and the cold channels degrade to no-ops.
"""
from __future__ import annotations

import os

#  How long a search will wait for a waking shard before giving up on it. Seconds.
WAKE_TIMEOUT = float(os.environ.get("SHARD_WAKE_TIMEOUT", "20"))


class ShardManagerBackend:
    """Workstream E implements this; `register_backend` installs it."""

    def state(self, domain) -> str:
        raise NotImplementedError

    def ensure(self, domains, timeout=WAKE_TIMEOUT) -> dict:
        raise NotImplementedError

    def connection(self, domain):
        raise NotImplementedError

    def release(self, domain, conn):
        raise NotImplementedError


class _NoShards(ShardManagerBackend):
    def state(self, domain):
        return "unknown"

    def ensure(self, domains, timeout=WAKE_TIMEOUT):
        return {d: "unknown" for d in (domains or [])}

    def connection(self, domain):
        return None

    def release(self, domain, conn):
        return None


_BACKEND = _NoShards()


def register_backend(backend):
    global _BACKEND
    _BACKEND = backend or _NoShards()


def backend():
    return _BACKEND


def available() -> bool:
    return not isinstance(_BACKEND, _NoShards)


def state(domain) -> str:
    try:
        return str(_BACKEND.state(domain) or "unknown")
    except Exception:
        return "unknown"


def ensure(domains, timeout=WAKE_TIMEOUT) -> dict:
    """Bring these domains up, waiting at most `timeout`. Never raises."""
    try:
        return dict(_BACKEND.ensure(list(domains or []), timeout=timeout) or {})
    except Exception:
        return {d: "unknown" for d in (domains or [])}


def connection(domain):
    """A connection to `domain`'s shard, or None when it is not available. Never raises."""
    try:
        return _BACKEND.connection(domain)
    except Exception:
        return None


def release(domain, conn):
    try:
        _BACKEND.release(domain, conn)
    except Exception:
        pass


def hot_domains(routes, timeout=WAKE_TIMEOUT) -> list:
    """The subset of `routes` (shard_router.route output) that is actually queryable now."""
    doms = [r["domain"] for r in (routes or [])]
    if not doms:
        return []
    states = ensure(doms, timeout=timeout)
    return [d for d in doms if states.get(d) == "hot"]
