"""Adapter interface + a shared async TTL cache + budget guard.

Ported from patents-app `adapters/base.py`. Every source implements `Adapter`.
All calls are async, rate-limit aware, cached, and fail-soft: a raising adapter
degrades the federation, it never takes the whole search down (the facade's
executor catches).

Port note (Python 3.9 / dedicated loop): asyncio primitives are created LAZILY,
inside the running loop, never at import time. On 3.9 `asyncio.Lock()` binds
whatever loop `get_event_loop()` returns at construction; built at import time
on the caller's thread and then awaited on the facade's dedicated loop it dies
with "attached to a different loop". All facade work runs on one long-lived
background loop (see sources/__init__), so lazily-built primitives are safe.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from .schema import Candidate, SubQuery


# ---------------------------------------------------------------------------
# tiny async TTL cache (Redis stand-in — one small box, no extra service)
# ---------------------------------------------------------------------------
class TTLCache:
    def __init__(self, maxsize: int = 4096):
        self._d: dict[str, tuple[float, Any]] = {}
        self._maxsize = maxsize
        self._lock: Optional[asyncio.Lock] = None   # lazy: see module docstring

    async def get(self, key: str):
        item = self._d.get(key)
        if not item:
            return None
        exp, val = item
        if exp < time.monotonic():
            self._d.pop(key, None)
            return None
        return val

    async def set(self, key: str, val: Any, ttl: float):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if len(self._d) >= self._maxsize:
                # drop ~10% oldest by expiry
                for k in sorted(self._d, key=lambda k: self._d[k][0])[: self._maxsize // 10]:
                    self._d.pop(k, None)
            self._d[key] = (time.monotonic() + ttl, val)

    def clear(self) -> None:
        self._d.clear()


CACHE = TTLCache()


class Budget:
    """Per-source soft counters so one source can't blow the whole run."""
    def __init__(self, caps: Optional[dict] = None):
        self.caps = caps or {}
        self.used: dict[str, int] = {}

    def allow(self, source: str) -> bool:
        cap = self.caps.get(source)
        if cap is None:
            return True
        return self.used.get(source, 0) < cap

    def charge(self, source: str, n: int = 1):
        self.used[source] = self.used.get(source, 0) + n


class Adapter:
    name: str = "base"

    #: is this source part of the fan-out a PATENT REPORT runs? Almost every source is.
    #: EUIPO is not: registered designs answer a different question and are served from
    #: their own page, so a status table that said "searched" for it would be claiming a
    #: report consulted the EU design register when it did not.
    in_report_fanout: bool = True

    #: does this adapter have what it needs (key/reachability) to run?
    def enabled(self) -> bool:
        return True

    #: is this adapter usable for SEARCH right now? (may differ from enabled() when
    #: a source's search is quota-exhausted but its detail endpoints still work)
    def search_available(self) -> bool:
        return self.enabled()

    #: optional informational note about search availability (not an error)
    def search_note(self) -> str:
        return ""

    #: human-readable reason it's off (shown in UI)
    def disabled_reason(self) -> str:
        return ""

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        raise NotImplementedError

    # optional capabilities — default to empty, executor checks hasattr
    async def details(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        return {}

    async def family(self, pub_number: str, client: httpx.AsyncClient) -> list[str]:
        return []

    async def citations(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        return {"backward": [], "forward": []}


async def cached_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    cache_key: str,
    ttl: float = 3600,
    **kw,
) -> Any:
    hit = await CACHE.get(cache_key)
    if hit is not None:
        return hit
    r = await client.request(method, url, **kw)
    r.raise_for_status()
    data = r.json()
    await CACHE.set(cache_key, data, ttl)
    return data
