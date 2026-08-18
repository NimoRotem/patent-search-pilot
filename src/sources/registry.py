"""Adapter registry.

`all_adapters()` returns PROCESS-WIDE SINGLETONS, built once on first use.

In App A this used to construct a fresh set of adapter objects on every call —
and it was called once per search and once per /api/health hit. Any state an
adapter caches on `self` was therefore thrown away immediately: most importantly
`EPO_OPS._token`, so the OAuth token would have been re-fetched on every single
request once OPS is enabled (OPS bills per call and rate-limits aggressively).
Adapters take the httpx client as a parameter and hold no per-request state, so
sharing one instance across requests is safe.

Ported from patents-app `adapters/__init__.py`. Not ported: Lens (dead trial
key), KIPRIS and EUIPO (never approved), gpatents_scrape (superseded by
gpatents_direct).
"""
from __future__ import annotations

import threading
from typing import Optional

from .base import Adapter, Budget, CACHE
from .serpapi_gpatents import SerpApiGooglePatents
from .bigquery_gpatents import BigQueryGPatents
from .pqai import PQAI
from .uspto import USPTO
from .openalex import OpenAlex
from .epo_ops import EPO_OPS
from .ipaustralia import IPAustralia
from .gpatents_direct import GooglePatentsDirect
from .himmpat import HimmPat
from .web_patent_fallback import WebPatentFallback

_ADAPTER_CLASSES = (
    SerpApiGooglePatents,
    BigQueryGPatents,
    PQAI,
    EPO_OPS,
    USPTO,
    OpenAlex,
    IPAustralia,
    GooglePatentsDirect,
    HimmPat,
    WebPatentFallback,
)

_instances: Optional[list] = None
_lock = threading.Lock()


def all_adapters() -> list:
    """The shared adapter instances (built once, in registry order)."""
    global _instances
    if _instances is None:
        with _lock:
            if _instances is None:  # re-check under lock
                _instances = [cls() for cls in _ADAPTER_CLASSES]
    return _instances


def registry() -> dict:
    return {a.name: a for a in all_adapters()}


def reset_adapters() -> None:
    """Drop the singletons so the next call rebuilds them — for tests and for
    picking up changed credentials without a restart."""
    global _instances
    with _lock:
        _instances = None
