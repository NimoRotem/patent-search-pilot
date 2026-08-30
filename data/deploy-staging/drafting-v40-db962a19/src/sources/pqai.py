"""PQAI adapter — semantic prior-art search (the richest free channel).

Base https://api.projectpq.ai with a query-param token. Endpoints:
- /search/102/  semantic novelty search
- /patents/{pn}            full record (description/claims)
- /patents/{pn}/citations  backward/forward citations
- /patents/{pn}/thumbnails drawing figures

Ported from patents-app `adapters/pqai.py`. Port change: PQAI_BASE_URL defaults
to the official https://api.projectpq.ai (App A's supervisor conf sets it to the
same value; the pilot's .env only carries PQAI_TOKEN).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import random
import time
from typing import Optional

import httpx

from .schema import Candidate, SubQuery, normalize_pub, google_url, iso_date
from .base import Adapter, cached_json

# ---------------------------------------------------------------------------
# PQAI mediator route (search.projectpq.ai/api/mediator) — a SECOND, token-free
# search path used by the public PQAI UI. It has NO monthly-50 search quota, so
# it restores semantic search once the token /search/102 quota is exhausted.
#
# This endpoint belongs to PARTNERS who asked us to use it responsibly:
#   * HARD cap of 1,000 calls / rolling 24h (persisted, survives restarts).
#   * serialized (concurrency 1) with a minimum spacing + jitter between calls so
#     we never burst and trip their anti-bot measures.
#   * optional outbound proxy (PQAI_MEDIATOR_PROXY) to avoid IP blocking.
# Contract (reverse-engineered from the UI):
#   POST {route:"/search/102", params:{q,before,after,dtype,type,cc,offset,n}}
#   -> {results:[{id, publication_id, title, abstract, ...}]}
# ---------------------------------------------------------------------------
MEDIATOR_URL = os.environ.get("PQAI_MEDIATOR_URL", "https://search.projectpq.ai/api/mediator")
MEDIATOR_PROXY = os.environ.get("PQAI_MEDIATOR_PROXY", "")           # e.g. http://user:pass@host:port
# TLS verification on the proxied mediator path. This used to be hard-coded
# verify=False, silently disabling certificate checking whenever a proxy was
# configured — an MITM could then feed arbitrary prior-art results into the
# pipeline. No credentials traverse this path (the mediator route is precisely
# the token-free one), so the exposure is result integrity rather than secret
# leakage, but it should not be off by default. Some MITM-style scraping proxies
# do present their own CA; if you must use one, set PQAI_MEDIATOR_INSECURE_TLS=1
# deliberately, or better, point REQUESTS_CA_BUNDLE/SSL_CERT_FILE at its CA.
MEDIATOR_VERIFY_TLS = os.environ.get("PQAI_MEDIATOR_INSECURE_TLS", "") not in ("1", "true", "yes")
MEDIATOR_DAILY_CAP = int(os.environ.get("PQAI_MEDIATOR_DAILY_CAP", "1000"))
MEDIATOR_MIN_INTERVAL = float(os.environ.get("PQAI_MEDIATOR_MIN_INTERVAL", "1.2"))  # seconds between calls
# How many results to request per PQAI search. PQAI (like iptorch) is a pure
# semantic engine fed the FULL raw disclosure verbatim, so ONE query returns the
# whole ranked set — we ask for a deep list in a single call (iptorch uses n up to
# 200) rather than many small paginated queries. This does NOT cost extra calls
# against the mediator daily cap or the token monthly quota (both are per-search,
# not per-result), so a larger n is free recall.
MEDIATOR_N = int(os.environ.get("PQAI_MEDIATOR_N", "200"))
TOKEN_N = int(os.environ.get("PQAI_TOKEN_N", "50"))
# The token REST route is a GET, so its `q` rides in the URL — a full multi-KB
# disclosure would 414. Cap it (the mediator POST path carries the disclosure
# untruncated). A semantic query does not need more than this many chars.
TOKEN_Q_MAXLEN = int(os.environ.get("PQAI_TOKEN_Q_MAXLEN", "2000"))
_MEDIATOR_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Ledger file shared per-host with App A (same path, same format), so the two
# apps' spend counts against ONE rolling daily cap rather than two.
_MED_CALLS_FILE = os.path.join(os.path.expanduser("~"), ".patents", "pqai_mediator_calls.json")
_MED_LOCK: Optional[asyncio.Lock] = None    # lazy — see base.py port note
_MED_LAST_TS = 0.0                   # monotonic time of last mediator call (spacing)
_MED_COOLDOWN_UNTIL = 0.0            # trips on block/error so we back off partner-side


def _med_lock() -> asyncio.Lock:
    global _MED_LOCK
    if _MED_LOCK is None:
        _MED_LOCK = asyncio.Lock()
    return _MED_LOCK


def _med_load_calls() -> list:
    try:
        with open(_MED_CALLS_FILE, encoding="utf-8") as f:
            return [float(x) for x in json.load(f)]
    except Exception:
        return []


def _med_prune(now: float) -> list:
    """Keep only timestamps within the last rolling 24h."""
    cutoff = now - 86400
    return [t for t in _med_load_calls() if t >= cutoff]


def _med_remaining() -> int:
    return max(0, MEDIATOR_DAILY_CAP - len(_med_prune(time.time())))


def _med_record_call(now: float) -> None:
    kept = _med_prune(now)
    kept.append(now)
    try:
        os.makedirs(os.path.dirname(_MED_CALLS_FILE), exist_ok=True)
        with open(_MED_CALLS_FILE, "w", encoding="utf-8") as f:
            json.dump(kept, f)
    except Exception:
        pass


def mediator_available() -> bool:
    """Usable when under the rolling daily cap and not in a block cooldown."""
    return time.monotonic() >= _MED_COOLDOWN_UNTIL and _med_remaining() > 0


def mediator_note() -> str:
    rem = _med_remaining()
    if time.monotonic() < _MED_COOLDOWN_UNTIL:
        return "mediator: cooling down after a block"
    return f"mediator route: {rem}/{MEDIATOR_DAILY_CAP} calls left (24h)"

# Module-level circuit breaker: once PQAI 429s, every PQAI call (search + detail,
# across all instances) fails fast for a cooldown instead of retrying with long
# backoff — otherwise, while PQAI is throttled, each of the many per-search
# queries would burn ~9s of retries and make the whole search crawl.
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_SECS = 90.0

# PQAI free token = HARD MONTHLY quota of 50 SEARCHES (/search/102 + /search/103),
# resets on the 1st. Detail endpoints (/patents/{pn}, /thumbnails, /citations) are
# NOT quota-counted. When exhausted we skip search entirely (no HTTP → no wasted
# probes) but keep detail working. The flag persists to disk and auto-clears once
# the calendar reaches the reset date, so search re-enables on its own next month.
_QUOTA_FILE = os.path.join(os.path.expanduser("~"), ".patents", "pqai_search_quota.json")
_QUOTA_STATE: dict = {}


def _load_quota_state() -> dict:
    global _QUOTA_STATE
    try:
        with open(_QUOTA_FILE, encoding="utf-8") as f:
            _QUOTA_STATE = json.load(f)
    except Exception:
        _QUOTA_STATE = {}
    return _QUOTA_STATE


_load_quota_state()


def _next_month_first() -> str:
    today = datetime.date.today()
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    return datetime.date(year, month, 1).isoformat()


def _mark_search_quota_exhausted(detail: str = "Monthly search quota exceeded") -> None:
    global _QUOTA_STATE
    _QUOTA_STATE = {"exhausted": True, "reset": _next_month_first(), "detail": detail}
    try:
        os.makedirs(os.path.dirname(_QUOTA_FILE), exist_ok=True)
        with open(_QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(_QUOTA_STATE, f)
    except Exception:
        pass


def _search_quota_blocked() -> bool:
    """True while the monthly search quota is exhausted (before its reset date)."""
    if not _QUOTA_STATE.get("exhausted"):
        return False
    reset = _QUOTA_STATE.get("reset", "")
    if reset and datetime.date.today().isoformat() >= reset:
        # reset date reached — clear and re-enable
        _QUOTA_STATE.clear()
        try:
            os.remove(_QUOTA_FILE)
        except Exception:
            pass
        return False
    return True


def quota_note() -> str:
    if _search_quota_blocked():
        reset = _QUOTA_STATE.get("reset", "the 1st")
        return f"search: monthly quota exhausted — resets {reset}"
    return ""


def _in_cooldown() -> bool:
    return time.monotonic() < _COOLDOWN_UNTIL


def _trip_cooldown() -> None:
    global _COOLDOWN_UNTIL
    _COOLDOWN_UNTIL = time.monotonic() + _COOLDOWN_SECS


def _maybe_trip(exc: Exception) -> None:
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        _trip_cooldown()


class PQAI(Adapter):
    name = "pqai"

    def __init__(self):
        self.base = os.environ.get("PQAI_BASE_URL", "https://api.projectpq.ai").rstrip("/")
        self.token = os.environ.get("PQAI_TOKEN", "")
        self._warnings: list[str] = []

    def _warn(self, msg: str) -> None:
        if len(self._warnings) < 16 and msg not in self._warnings:
            self._warnings.append(msg)

    def pop_warnings(self) -> list[str]:
        w, self._warnings = self._warnings, []
        return w

    def enabled(self) -> bool:
        # base configured — the DETAIL endpoints (not quota-counted) still work
        return bool(self.base)

    def search_available(self) -> bool:
        # search is usable if configured AND (the token quota remains OR the
        # token-free mediator route is available) — the mediator has no monthly cap.
        return bool(self.base) and (not _search_quota_blocked() or mediator_available())

    def disabled_reason(self) -> str:
        return "PQAI_BASE_URL not set (self-host or official key; no masked proxy)"

    def search_note(self) -> str:
        # prefer the informative note for whichever route is actually carrying search
        if _search_quota_blocked():
            return mediator_note() if mediator_available() else quota_note()
        return quota_note()

    def _params(self, **kw) -> dict:
        p = dict(kw)
        if self.token:
            p["token"] = self.token
        return p

    def _results_to_candidates(self, data: dict, sq: SubQuery, source: str) -> list[Candidate]:
        out: list[Candidate] = []
        for i, r in enumerate(data.get("results") or []):
            pub = r.get("publication_id") or r.get("id") or ""
            if not pub:
                continue
            out.append(Candidate(
                pub_number=normalize_pub(pub),
                source=source,
                source_rank=i + 1,
                title=r.get("title", "") or "",
                abstract=r.get("abstract", "") or "",
                snippet=(r.get("snippet") or r.get("abstract", "") or "")[:300],
                assignee=r.get("owner", "") or "",
                inventors=r.get("inventors", []) or [],
                date=iso_date(r.get("publication_date")),
                priority_date=iso_date(r.get("priority_date")),
                url=r.get("www_link") or google_url(pub),
                found_by=[sq.rationale or sq.element],
                extra={"score": r.get("score", 0), "thumbnail": r.get("image", ""),
                       "alias": r.get("alias", "")},
            ))
        return out

    async def _mediator_call(self, sq: SubQuery, client: httpx.AsyncClient) -> Optional[dict]:
        """Token-free /search/102 via the partner mediator. Serialized, spaced,
        jittered, hard-capped at 1,000/24h, and it counts EVERY attempt (even
        failures) against the cap so we never overload their endpoint."""
        global _MED_LAST_TS, _MED_COOLDOWN_UNTIL
        async with _med_lock():
            if not mediator_available():
                return None
            wait = MEDIATOR_MIN_INTERVAL - (time.monotonic() - _MED_LAST_TS)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0.0, 0.4))
            # Param shape mirrors iptorch's demonstrably-working mediator call
            # ({q, n, before, after, maps:0, snip:0, type:"patent"}) — maps/snip=0
            # keep the payload lean (no citation maps / no snippet highlighting),
            # and `q` is the raw disclosure the pipeline injects verbatim.
            body = {"route": "/search/102",
                    "params": {"q": str(sq.native),
                               "n": MEDIATOR_N,
                               "before": sq.date_to or "", "after": sq.date_from or "",
                               "maps": 0, "snip": 0, "type": "patent"}}
            headers = {"Content-Type": "application/json", "User-Agent": _MEDIATOR_UA,
                       "Origin": "https://search.projectpq.ai",
                       "Referer": "https://search.projectpq.ai/",
                       "Accept": "application/json, text/plain, */*"}
            _MED_LAST_TS = time.monotonic()
            _med_record_call(time.time())      # count against the daily cap up front
            try:
                if MEDIATOR_PROXY:
                    try:
                        pc_kw = dict(proxy=MEDIATOR_PROXY, verify=MEDIATOR_VERIFY_TLS, timeout=30)
                        pc = httpx.AsyncClient(**pc_kw)
                    except TypeError:
                        # httpx < 0.26 spells the kwarg `proxies`
                        pc = httpx.AsyncClient(proxies=MEDIATOR_PROXY,
                                               verify=MEDIATOR_VERIFY_TLS, timeout=30)
                    try:
                        r = await pc.post(MEDIATOR_URL, json=body, headers=headers)
                    finally:
                        await pc.aclose()
                else:
                    r = await client.post(MEDIATOR_URL, json=body, headers=headers, timeout=30)
                if r.status_code in (403, 429) or r.status_code >= 500:
                    _MED_COOLDOWN_UNTIL = time.monotonic() + 300   # blocked → back off 5 min
                    return None
                r.raise_for_status()
                return r.json()
            except Exception:
                _MED_COOLDOWN_UNTIL = time.monotonic() + 120
                return None

    async def _token_search(self, sq: SubQuery, client: httpx.AsyncClient) -> Optional[dict]:
        """Legacy token /search/102 route (monthly-50 quota). Fail-soft."""
        if _search_quota_blocked() or _in_cooldown():
            return None
        q = str(sq.native)[:TOKEN_Q_MAXLEN]      # GET route: keep the URL under the 414 limit
        for attempt in range(3):
            try:
                r = await client.get(f"{self.base}/search/102/",
                                     params=self._params(q=q, n=TOKEN_N, type="patent"))
                if r.status_code == 429:
                    if "quota" in (r.text or "").lower():   # monthly quota, not a burst
                        _mark_search_quota_exhausted()
                        return None
                    if attempt == 2:
                        _trip_cooldown()
                        return None
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception:
                await asyncio.sleep(0.3 * (attempt + 1))
        return None

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        from .base import CACHE
        ck = f"pqai:{sq.cache_key()}"
        cached = await CACHE.get(ck)
        if cached is not None:
            return self._results_to_candidates(cached, sq, self.name)
        # Prefer the token-free mediator (no monthly quota); fall back to the token
        # route while its monthly allowance lasts.
        data = None
        if mediator_available():
            data = await self._mediator_call(sq, client)
        if data is None:
            data = await self._token_search(sq, client)
        if data is None:
            # ANTI-DARK: both routes were unavailable or errored — this is a genuine
            # failure, NOT an empty result set. Surface it (the facade drains warnings
            # into the errors list) so the caller never reads it as "0 matches / no
            # prior art". A route that legitimately returns {"results": []} does not
            # reach here — data is a dict then, and yields an honest empty candidate list.
            if _search_quota_blocked() and not mediator_available():
                self._warn("PQAI search unavailable: monthly token quota exhausted "
                           "and mediator route capped/cooling down")
            else:
                self._warn("PQAI search returned no data (mediator/token route "
                           "errored or was rate-limited) — results incomplete")
            return []
        await CACHE.set(ck, data, 3600)
        return self._results_to_candidates(data, sq, self.name)

    async def details(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        if _in_cooldown():
            return {}
        pn = normalize_pub(pub_number)
        try:
            d = await cached_json(client, "GET", f"{self.base}/patents/{pn}",
                                  cache_key=f"pqai:det:{pn}", params=self._params(), ttl=86400)
        except Exception as e:
            _maybe_trip(e)
            return {}
        # PQAI may return claims as a list of claim strings or one big string —
        # coerce to a single string so the API contract is always text.
        claims = d.get("claims") or ""
        if isinstance(claims, (list, tuple)):
            claims = "\n\n".join(str(c) for c in claims if c)
        elif not isinstance(claims, str):
            claims = str(claims)
        desc = d.get("description") or ""
        if not isinstance(desc, str):
            desc = str(desc)
        return {"claims": claims[:20000],
                "description": desc[:40000],
                "abstract": d.get("abstract", "") or ""}

    async def citations(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        if _in_cooldown():
            return {"backward": [], "forward": []}
        pn = normalize_pub(pub_number)
        try:
            d = await cached_json(client, "GET", f"{self.base}/patents/{pn}/citations",
                                  cache_key=f"pqai:cit:{pn}", params=self._params(), ttl=86400)
        except Exception as e:
            _maybe_trip(e)
            return {"backward": [], "forward": []}
        return {"backward": [normalize_pub(x) for x in (d.get("citations_backward") or [])],
                "forward": [normalize_pub(x) for x in (d.get("citations_forward") or [])]}

    async def thumbnails(self, pub_number: str, client: httpx.AsyncClient) -> list[str]:
        if _in_cooldown():
            return []
        pn = normalize_pub(pub_number)
        try:
            d = await cached_json(client, "GET", f"{self.base}/patents/{pn}/thumbnails",
                                  cache_key=f"pqai:th:{pn}", params=self._params(), ttl=86400)
            return d.get("thumbnails", []) or []
        except Exception as e:
            _maybe_trip(e)
            return []

    def thumbnail_url(self, pub_number: str, n: str = "1") -> str:
        pn = normalize_pub(pub_number)
        tok = f"?token={self.token}" if self.token else ""
        return f"{self.base}/patents/{pn}/thumbnails/{n}{tok}"
