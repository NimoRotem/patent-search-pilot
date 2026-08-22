"""HimmPat adapter — worldwide patents with ENGLISH full text for CN/JP/KR art.

Why this source is worth paying for: every other adapter we have is strong on US
and thin-to-absent on Asian art, and where it does surface a CN/JP/KR document it
surfaces it in the original language. HimmPat carries machine-translated English
title/abstract/claims/description alongside the originals (`tie`/`abe`/`clme`/
`desce`), so a CN utility model can actually be READ and cited as prior art. It
also has a semantic (embedding) search that takes the raw disclosure, which is
the same shape as PQAI and is the highest-recall channel we have for non-Western
art.

VERIFIED AGAINST THE LIVE API 2026-08-12 with the trial key. Non-obvious things,
asserted in code so this cannot ship dark:

  1. AUTH IS A BARE KEY. `Authorization: <apiKey>` — NOT `Bearer <apiKey>`.
  2. HTTP 200 DOES NOT MEAN SUCCESS. Every response is
     `{"code": <int>, "message": "...", "data": ...}` and the transport is 200 for
     business errors too. code 200 = ok, 201 = no results, 202 = bad parameters,
     100 = rate limited, 101/102/105 = key dead/suspended/expired, 103/104/106 =
     quota or balance exhausted, 109 = key never bought this interface, 500 =
     failure. Reading `data` without checking `code` reports an exhausted key as
     "no matches found", which is the exact failure this comment exists to stop.
  3. SEARCH RETURNS IDs ONLY. Both search endpoints return opaque HimmPat ids
     (32- or 64-hex; the 64-hex ones are family-merged pseudo-ids produced by
     `mergeBy`). A publication number requires a SECOND, SEPARATELY BILLED call to
     `get_patent_publication_by_patent_ids`, which is a "special interface" that
     deducts ONE UNIT PER ID, not one per call. Hydration, not search, is the cost
     driver — so ids are hydrated once and cached, and the per-query id cap is low.
  4. `mergeBy=HFA` de-duplicates by HimmPat family server-side. That is both
     cheaper (we do not pay to hydrate five members of one family) and better
     (the kept member is the earliest publication, which is the prior-art-relevant
     one). Default on; `HIMMPAT_MERGE_BY=""` disables it.
  5. QUERY EXPRESSIONS ARE POSTFIX: `value/field`, e.g. `"vacuum lifter"/tac`,
     `2015-2026/pd`, `B66C1/02/ic`. The field goes AFTER the term, which is the
     reverse of every other source we talk to. An expression with no `/field`
     suffix searches `/all` (title+abstract+claims+description+biblio).

SPENDING GUARD. The key is a low-volume trial and the owner's instruction was not
to overuse it, so this adapter refuses to spend rather than degrade quietly:
a rolling 24h unit ledger AND a lifetime unit ledger, both persisted to disk so a
restart does not reset them, checked BEFORE each call and charged in the same
units the vendor deducts (1 per ordinary call, len(ids) for the per-id "special"
interfaces). When either is exhausted the adapter reports itself unavailable with
the numbers in the reason, so the caller says why instead of showing an empty
source. The ledger file is the SAME file App A uses (~/.patents/himmpat_usage.json),
so both apps' spend counts against one per-host budget.

Ported from patents-app `adapters/himmpat.py`. Auth: HIMMPAT_API_KEY.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional

import httpx

import realtime_only

from .schema import Candidate, SubQuery, iso_date, normalize_pub, best_source_url
from .base import Adapter, CACHE

BASE = os.environ.get(
    "HIMMPAT_BASE_URL", "https://himmpat.com/api/service/himmuc_api").rstrip("/")

SEARCH_EXPR_URL = f"{BASE}/patent_search/query_patent_ids_by_query_expression"
SEARCH_TEXT_URL = f"{BASE}/patent_search/new_search_similar_patents_by_text"
SEARCH_PUBNO_URL = f"{BASE}/patent_search/search_similar_patents_by_public_number"
MATCH_NUMBERS_URL = f"{BASE}/patent_search/search_patent_by_patent_numbers"
BIBLIO_URL = f"{BASE}/patent_basic_data/get_patent_publication_by_patent_ids"
CLAIMS_URL = f"{BASE}/patent_text_data/get_claims_by_patent_id"
DESCRIPTION_URL = f"{BASE}/patent_text_data/get_description_by_patent_id"

# How many ids one query may hydrate. Hydration bills PER ID, so this is the main
# cost lever — keep it small and let several diverse queries do the widening.
SEARCH_SIZE = int(os.environ.get("HIMMPAT_SEARCH_SIZE", "15"))
# Vendor caps a single hydrate call at 100 ids.
HYDRATE_MAX = 100
MERGE_BY = os.environ.get("HIMMPAT_MERGE_BY", "HFA").strip().upper()
# Country/authority filter. Empty = the whole database, which is the point of this
# source (CN/JP/KR coverage nothing else in the stack has).
AUTHORITIES = [a.strip().upper() for a in
               os.environ.get("HIMMPAT_AUTHORITIES", "").split(",") if a.strip()]
# Full text through this source is expensive (claims + description + biblio + an id
# lookup = 4 units for one document), so the fulltext ladder only reaches for it when
# the free sources produced nothing readable. Off entirely with HIMMPAT_DETAILS=0.
DETAILS_ENABLED = os.environ.get("HIMMPAT_DETAILS", "1") != "0"

TIMEOUT = float(os.environ.get("HIMMPAT_TIMEOUT", "60"))
MIN_INTERVAL = float(os.environ.get("HIMMPAT_MIN_INTERVAL", "0.35"))

# ---------------------------------------------------------------------------
# spend ledger — rolling 24h + lifetime, persisted
# ---------------------------------------------------------------------------
DAILY_UNITS = int(os.environ.get("HIMMPAT_DAILY_UNITS", "250"))
# Lifetime ceiling for a trial key of unknown size. 0 disables the lifetime check.
TOTAL_UNITS = int(os.environ.get("HIMMPAT_TOTAL_UNITS", "2000"))

_STATE_FILE = os.path.join(os.path.expanduser("~"), ".patents", "himmpat_usage.json")
_STATE_LOCK: Optional[asyncio.Lock] = None   # lazy — see base.py port note
_CALL_LOCK: Optional[asyncio.Lock] = None
_LAST_CALL = 0.0


def _state_lock() -> asyncio.Lock:
    global _STATE_LOCK
    if _STATE_LOCK is None:
        _STATE_LOCK = asyncio.Lock()
    return _STATE_LOCK


def _call_lock() -> asyncio.Lock:
    global _CALL_LOCK
    if _CALL_LOCK is None:
        _CALL_LOCK = asyncio.Lock()
    return _CALL_LOCK


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("calls", [])        # [[epoch, units], ...] pruned to 24h
    s.setdefault("total", 0)         # lifetime units
    s.setdefault("block", {})        # {"until": epoch, "code": int, "detail": str}
    return s


def _save_state(s: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, _STATE_FILE)
    except Exception:
        pass


def _prune(s: dict, now: float) -> list:
    cutoff = now - 86400
    s["calls"] = [c for c in s["calls"] if len(c) == 2 and float(c[0]) >= cutoff]
    return s["calls"]


def _spent_today(s=None, now=None) -> int:
    s = s if s is not None else _load_state()
    return sum(int(c[1]) for c in _prune(s, now if now is not None else time.time()))


def usage() -> dict:
    """Spend so far, for the health endpoint and the source note."""
    s = _load_state()
    now = time.time()
    day = _spent_today(s, now)
    blk = s.get("block") or {}
    blocked = bool(blk) and float(blk.get("until", 0)) > now
    return {
        "day_units": day,
        "day_cap": DAILY_UNITS,
        "day_left": max(0, DAILY_UNITS - day),
        "total_units": int(s.get("total", 0)),
        "total_cap": TOTAL_UNITS,
        "total_left": (max(0, TOTAL_UNITS - int(s.get("total", 0)))
                       if TOTAL_UNITS > 0 else -1),
        "blocked": blocked,
        "block_code": blk.get("code") if blocked else None,
        "block_detail": blk.get("detail", "") if blocked else "",
        "block_until": blk.get("until") if blocked else None,
    }


def _affordable(units: int) -> bool:
    u = usage()
    if u["blocked"]:
        return False
    if u["day_left"] < units:
        return False
    if TOTAL_UNITS > 0 and u["total_left"] < units:
        return False
    return True


async def _charge(units: int) -> None:
    async with _state_lock():
        s = _load_state()
        now = time.time()
        _prune(s, now)
        s["calls"].append([now, int(units)])
        s["total"] = int(s.get("total", 0)) + int(units)
        _save_state(s)


async def _block(code: int, detail: str, hours: float) -> None:
    """Stop calling for `hours` after a key/quota error, and say why."""
    async with _state_lock():
        s = _load_state()
        s["block"] = {"until": time.time() + hours * 3600,
                      "code": int(code), "detail": detail[:200]}
        _save_state(s)


# ---------------------------------------------------------------------------
# response envelope
# ---------------------------------------------------------------------------
CODE_MEANING = {
    100: "rate limited (too many requests)",
    101: "API key invalid or deleted",
    102: "API key suspended",
    103: "API key call allowance exhausted",
    104: "daily allowance for this interface exhausted",
    105: "API key expired",
    106: "account balance exhausted",
    107: "too many rejected requests — contact the vendor",
    108: "this IP is blocked",
    109: "this key has not purchased this interface",
    110: "this interface cannot be billed per call on this key",
    198: "interface retired",
    199: "unknown vendor error",
    201: "no results",
    202: "bad request parameters",
    500: "request failed",
}
# codes that mean "stop calling" and for how long
_BLOCK_HOURS = {100: 0.1, 101: 24, 102: 24, 103: 24, 104: 6,
                105: 24, 106: 24, 107: 24, 108: 24}


class HimmPatError(RuntimeError):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(f"HimmPat code {code}: {message}")


class HimmPat(Adapter):
    name = "himmpat"

    def __init__(self):
        self.key = os.environ.get("HIMMPAT_API_KEY", "")
        self._warnings: list[str] = []

    # -- plumbing ----------------------------------------------------------
    def _warn(self, msg: str) -> None:
        if len(self._warnings) < 16 and msg not in self._warnings:
            self._warnings.append(msg)

    def pop_warnings(self) -> list[str]:
        w, self._warnings = self._warnings, []
        return w

    def enabled(self) -> bool:
        #  Two layers, and the second one is the load-bearing one. This makes the bar visible in
        #  /api/health and in the cascade's startup banner; `_post` refuses regardless, so a
        #  caller that ignores this still cannot spend a unit.
        return bool(self.key) and realtime_only.allowed(self.name)

    def search_available(self) -> bool:
        # One search plus a minimal hydrate has to fit, or there is no point
        # starting: a search we cannot hydrate returns ids nobody can read.
        return self.enabled() and _affordable(2)

    def disabled_reason(self) -> str:
        if not realtime_only.allowed(self.name):
            return realtime_only.blocked_reason(self.name)
        if not self.key:
            return ("HIMMPAT_API_KEY not set (himmpat.com — global patent search with "
                    "English full text for CN/JP/KR)")
        u = usage()
        if u["blocked"]:
            code = u["block_code"]
            return (f"HimmPat paused: {CODE_MEANING.get(code, u['block_detail'] or 'error')} "
                    f"(vendor code {code})")
        if u["day_left"] < 2:
            return (f"HimmPat daily budget spent ({u['day_units']}/{DAILY_UNITS} units "
                    f"in the last 24h) — raise HIMMPAT_DAILY_UNITS to spend more")
        if TOTAL_UNITS > 0 and u["total_left"] < 2:
            return (f"HimmPat lifetime budget spent ({u['total_units']}/{TOTAL_UNITS} units) "
                    f"— raise HIMMPAT_TOTAL_UNITS once the key's real allowance is known")
        return ""

    def search_note(self) -> str:
        u = usage()
        note = (f"worldwide incl. CN/JP/KR with English full text — "
                f"{u['day_left']}/{DAILY_UNITS} units left today")
        if TOTAL_UNITS > 0:
            note += f", {u['total_left']}/{TOTAL_UNITS} lifetime"
        return note

    async def _post(self, client: httpx.AsyncClient, url: str, body: dict,
                    units: int, *, cache_key: str = "", ttl: float = 21600):
        """One billed call. Budget-checked, rate-spaced, envelope-checked, cached.

        Returns the `data` payload, or None when the vendor said "no results" (201).
        Raises HimmPatError for anything else — the executor turns that into a
        visible source_error rather than a silent empty result.
        """
        #  THE BAR. Before the cache, before the key check, before the ledger: a process that
        #  is not a live search has no business holding any part of this allowance, and putting
        #  the check at the one boundary every billed call passes through is what makes it a
        #  property of the code rather than of the cascade's configuration.
        realtime_only.check(self.name)
        if cache_key:
            hit = await CACHE.get(cache_key)
            if hit is not None:
                return hit
        if not self.key:
            raise HimmPatError(101, "HIMMPAT_API_KEY not set")
        if not _affordable(units):
            u = usage()
            raise HimmPatError(
                103, f"local spend guard: {units} unit(s) needed, "
                     f"{u['day_left']} left today / "
                     f"{u['total_left'] if TOTAL_UNITS > 0 else 'unlimited'} lifetime")

        global _LAST_CALL
        async with _call_lock():
            gap = MIN_INTERVAL - (time.monotonic() - _LAST_CALL)
            if gap > 0:
                await asyncio.sleep(gap)
            _LAST_CALL = time.monotonic()

        r = await client.post(url, json=body, timeout=TIMEOUT, headers={
            "Authorization": self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        r.raise_for_status()
        try:
            payload = r.json()
        except Exception as exc:
            raise HimmPatError(199, f"non-JSON response from {url}: {exc}") from exc
        if not isinstance(payload, dict) or "code" not in payload:
            raise HimmPatError(199, f"response envelope changed (keys={sorted(payload)[:8]})"
                               if isinstance(payload, dict) else "response is not an object")

        code = int(payload.get("code") or 0)
        message = str(payload.get("message") or "")
        if code == 200:
            # Only a successful call is billed — the vendor does not charge for a
            # call that returned nothing, and neither do we.
            await _charge(units)
            data = payload.get("data")
            if cache_key and data is not None:
                await CACHE.set(cache_key, data, ttl)
            return data
        if code == 201:
            return None
        hours = _BLOCK_HOURS.get(code)
        if hours:
            await _block(code, f"{CODE_MEANING.get(code, message)} — {message}", hours)
        raise HimmPatError(code, CODE_MEANING.get(code, message or "unknown"))

    # -- query expression --------------------------------------------------
    # A term followed by /field is HimmPat's whole syntax. Recognising an existing
    # field suffix is what stops us wrapping an already-valid expression.
    _FIELD_SUFFIX_RE = re.compile(r"/[a-z][a-z0-9_]{0,13}\s*(?:\)|$|\s+(?:and|or|not)\b)",
                                  re.IGNORECASE)
    _TOKEN_RE = re.compile(r'"[^"]+"|\S+')

    @classmethod
    def build_expression(cls, q: str, default_field: str = "tac") -> str:
        """Accept either a real HimmPat expression or a bare keyword string.

        The planner is told to emit `("vacuum lifter" or "suction lifter")/tac`.
        The keyword-only fallback plan (used when the LLM is unavailable) emits
        `vacuum lifter suction pump`, which is NOT valid — an unfielded bare string
        would be read as one loose /all query. Turn it into an explicit AND of its
        terms against title+abstract+claims so the fallback still searches something
        defensible instead of whatever the vendor guesses.
        """
        q = str(q or "").strip()
        if not q:
            return ""
        if cls._FIELD_SUFFIX_RE.search(q):
            return q
        toks = [t for t in cls._TOKEN_RE.findall(q) if t not in ("(", ")")]
        if not toks:
            return ""
        if len(toks) == 1:
            return f"{toks[0]}/{default_field}"
        return "(" + " and ".join(toks) + f")/{default_field}"

    def _search_body(self, expression: str, size: int) -> dict:
        body = {"queryExpression": expression, "page": 1, "size": size,
                "sort": "SCORE", "order": "DESC"}
        if MERGE_BY:
            body["mergeBy"] = MERGE_BY
        if AUTHORITIES:
            body["authority"] = AUTHORITIES
        return body

    # -- hydration ---------------------------------------------------------
    async def _hydrate(self, ids: list, client: httpx.AsyncClient) -> dict:
        """id -> bibliographic record. Cached per id; only misses are billed.

        Billed PER ID (a vendor "special interface"), so the cache is what keeps a
        multi-query run affordable: the same family surfaces from several queries
        and is paid for once.
        """
        out: dict[str, dict] = {}
        missing: list[str] = []
        for pid in ids:
            hit = await CACHE.get(f"himm:bib:{pid}")
            if hit is not None:
                out[pid] = hit
            else:
                missing.append(pid)
        for i in range(0, len(missing), HYDRATE_MAX):
            chunk = missing[i:i + HYDRATE_MAX]
            truncated = 0
            if not _affordable(len(chunk)):
                # Spend what is left rather than nothing — but SAY that the list was
                # cut. A silently truncated result set reads as "this is everything
                # the source had", which is the one thing it is not.
                u = usage()
                left = u["day_left"] if TOTAL_UNITS <= 0 else min(u["day_left"], u["total_left"])
                left = max(0, left)
                truncated = len(missing) - i - left
                chunk = chunk[:left]
            if truncated > 0:
                self._warn(
                    f"himmpat: spend budget reached — {truncated} of {len(missing)} "
                    f"result(s) left un-hydrated, so this list is truncated, not complete "
                    f"(raise HIMMPAT_DAILY_UNITS/HIMMPAT_TOTAL_UNITS to see the rest)")
            if not chunk:
                break
            data = await self._post(client, BIBLIO_URL, {"ids": chunk}, len(chunk))
            for pid, rec in (data or {}).items():
                if isinstance(rec, dict):
                    out[pid] = rec
                    await CACHE.set(f"himm:bib:{pid}", rec, 86400)
        return out

    # -- mapping -----------------------------------------------------------
    @staticmethod
    def _first(seq, *keys) -> str:
        for item in (seq or []):
            if not isinstance(item, dict):
                continue
            for k in keys:
                v = item.get(k)
                if v:
                    return str(v)
        return ""

    @classmethod
    def to_candidate(cls, pid: str, rec: dict, rank: int, sq: SubQuery,
                     relevancy=None) -> Optional[Candidate]:
        pub_ref = rec.get("publicationReferenceModel") or {}
        app_ref = rec.get("applicationReferenceModel") or {}
        pub = normalize_pub(pub_ref.get("pn") or "")
        if not pub:
            return None
        titles = rec.get("inventionTitleModel") or {}
        abstracts = rec.get("abstractModel") or {}
        parties = rec.get("partiesModel") or {}
        cls_model = rec.get("classificationModel") or {}

        # English first — the reason this source is here at all — then the original.
        title = titles.get("tie") or titles.get("tio") or titles.get("tic") or ""
        abstract = abstracts.get("abe") or abstracts.get("abo") or abstracts.get("abc") or ""
        assignee = (cls._first(parties.get("assignee"), "as")
                    or cls._first(parties.get("applicant"), "pa"))
        inventors = [str(i.get("in")) for i in (parties.get("inventor") or [])
                     if isinstance(i, dict) and i.get("in")]

        pub_date = iso_date(pub_ref.get("pd") or pub_ref.get("pdf"))
        filing_date = iso_date(app_ref.get("apd"))
        priority = ""
        for pr in (rec.get("priorityClaimsModelList") or []):
            if isinstance(pr, dict):
                d = iso_date(pr.get("prd"))
                if d and (not priority or d < priority):
                    priority = d
        cpc = [str(c) for c in ((cls_model.get("cpc") or cls_model.get("ic")) or [])]

        extra: dict = {"himmpat_id": pid}
        if relevancy is not None:
            extra["himm_relevancy"] = round(float(relevancy), 4)
        if titles.get("tio") and titles.get("tie"):
            extra["title_original"] = str(titles["tio"])
        if abstracts.get("abo") and abstracts.get("abe"):
            extra["abstract_original"] = str(abstracts["abo"])
        if filing_date:
            extra["filing_date"] = filing_date

        return Candidate(
            pub_number=pub,
            source="himmpat",
            source_rank=rank,
            title=str(title),
            abstract=str(abstract),
            assignee=str(assignee),
            inventors=inventors[:8],
            date=pub_date,
            # A priority claim if there is one, else the filing date — never the
            # publication date, which is ~18 months late and would wrongly exclude
            # the document from a prior-art window.
            priority_date=priority or filing_date,
            cpc=cpc[:12],
            url=best_source_url(pub),
            # HimmPat has no DOCDB family id. Leaving family_id empty is deliberate:
            # a digit-bearing internal HimmPat id would be mistaken downstream for an
            # authoritative DOCDB id and build a wrong Espacenet family URL.
            family_id="",
            found_by=[sq.rationale or sq.element or "himmpat"],
            extra=extra,
        )

    # -- search ------------------------------------------------------------
    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        if not self.enabled():
            return []
        native = sq.native
        semantic = False
        text = ""
        expression = ""
        if isinstance(native, dict):
            semantic = str(native.get("mode", "")).lower() == "semantic"
            text = str(native.get("text") or "").strip()
            expression = self.build_expression(native.get("q") or "")
        else:
            expression = self.build_expression(native)

        if semantic and not text:
            raise ValueError("himmpat semantic query with no text")
        if not semantic and not expression:
            raise ValueError("empty himmpat query (planner emitted no expression)")

        size = max(1, min(SEARCH_SIZE, 1000))
        if semantic:
            body: dict = {"text": text[:15000], "page": 1, "size": size}
            if AUTHORITIES:
                body["authority"] = AUTHORITIES
            if expression:
                body["queryExpression"] = expression
            for key, val in (("apdFrom", sq.date_from), ("pdTo", sq.date_to)):
                digits = re.sub(r"\D", "", str(val or ""))[:8]
                if len(digits) == 8:
                    body[key] = int(digits)
            data = await self._post(client, SEARCH_TEXT_URL, body, 1,
                                    cache_key=f"himm:sem:{sq.cache_key()}:{size}")
            rows = (data or {}).get("similarPatentsList") or []
            ranked = [(str(r.get("id")), r.get("relevancy")) for r in rows
                      if isinstance(r, dict) and r.get("id")]
        else:
            data = await self._post(client, SEARCH_EXPR_URL,
                                    self._search_body(expression, size), 1,
                                    cache_key=f"himm:expr:{sq.cache_key()}:{size}")
            ids = (data or {}).get("ids") or []
            ranked = [(str(i), None) for i in ids if i]

        if data is not None and not ranked:
            total = (data or {}).get("total")
            if total and str(total) != "0":
                self._warn(f"himmpat reported total={total} but returned no ids for "
                           f"{(text or expression)[:60]!r} — response contract changed")
        if not ranked:
            return []

        records = await self._hydrate([pid for pid, _ in ranked], client)
        out: list[Candidate] = []
        for i, (pid, rel) in enumerate(ranked):
            rec = records.get(pid)
            if not rec:
                continue
            cand = self.to_candidate(pid, rec, i + 1, sq, rel)
            if cand:
                out.append(cand)
        return out

    # -- single-document full text (English) --------------------------------
    async def resolve_id(self, pub_number: str, client: httpx.AsyncClient) -> str:
        """Publication number -> HimmPat id (1 unit, cached)."""
        pub = normalize_pub(pub_number)
        if not pub:
            return ""
        cached = await CACHE.get(f"himm:id:{pub}")
        if cached is not None:
            return str(cached)
        data = await self._post(client, MATCH_NUMBERS_URL,
                                {"patentList": [pub], "matchingMethod": ["PN", "AP"]}, 1)
        ids = (data or {}).get(pub) or []
        pid = str(ids[0]) if ids else ""
        await CACHE.set(f"himm:id:{pub}", pid, 86400)
        return pid

    async def details(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        """Biblio + claims + description, English where a translation exists.

        Up to 4 units for one document (id lookup + biblio + claims + description),
        so the fulltext ladder only calls this when the free sources gave nothing
        readable. Fail-soft: whatever resolves is returned, the rest is omitted.
        """
        if not DETAILS_ENABLED or not self.enabled() or not _affordable(4):
            return {}
        pid = await self.resolve_id(pub_number, client)
        if not pid:
            return {}
        out: dict = {"himmpat_id": pid}
        records = await self._hydrate([pid], client)
        rec = records.get(pid) or {}
        if rec:
            titles = rec.get("inventionTitleModel") or {}
            abstracts = rec.get("abstractModel") or {}
            parties = rec.get("partiesModel") or {}
            pub_ref = rec.get("publicationReferenceModel") or {}
            app_ref = rec.get("applicationReferenceModel") or {}
            cls_model = rec.get("classificationModel") or {}
            out.update({
                "title": titles.get("tie") or titles.get("tio") or "",
                "abstract": abstracts.get("abe") or abstracts.get("abo") or "",
                "assignee": self._first(parties.get("assignee"), "as")
                            or self._first(parties.get("applicant"), "pa"),
                "inventors": [str(i.get("in")) for i in (parties.get("inventor") or [])
                              if isinstance(i, dict) and i.get("in")][:8],
                "publication_date": iso_date(pub_ref.get("pd")),
                "filing_date": iso_date(app_ref.get("apd")),
                "cpc": [str(c) for c in ((cls_model.get("cpc") or cls_model.get("ic")) or [])][:12],
            })
        for url, want, keys in (
            (CLAIMS_URL, "claims", ("clmse", "clmsc", "clmso")),
            (DESCRIPTION_URL, "description", ("desce", "descc", "desco")),
        ):
            if not _affordable(1):
                break
            try:
                data = await self._post(client, url, {"id": pid}, 1,
                                        cache_key=f"himm:{want}:{pid}", ttl=86400)
            except HimmPatError as exc:
                self._warn(f"himmpat {want} for {pub_number}: {exc}")
                continue
            text = ""
            for k in keys:
                if (data or {}).get(k):
                    text = strip_markup(str(data[k]))
                    if text:
                        # Record which language actually came back: an English
                        # translation and the Chinese original are not interchangeable
                        # for an attorney reading a claim.
                        out[f"{want}_lang"] = "en" if k.endswith("e") else (
                            "zh" if k.endswith("c") else "original")
                        break
            if text:
                out[want] = text
        return {k: v for k, v in out.items() if v}


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]{2,}")


def strip_markup(xml: str) -> str:
    """HimmPat returns claims/description as WIPO ST.36 XML. Flatten to text.

    Each <Claim>/<Paragraph> becomes its own line so claim boundaries survive —
    a naive tag strip runs all nine claims into one paragraph and makes the text
    useless for element-by-element comparison.
    """
    if not xml:
        return ""
    s = re.sub(r"</(Claim|Paragraph|ClaimText|InventionTitle|P)>", "\n", xml)
    s = _TAG_RE.sub("", s)
    s = (s.replace("&lt;", "<").replace("&gt;", ">")
          .replace("&amp;", "&").replace("&quot;", '"').replace("&apos;", "'"))
    s = _WS_RE.sub(" ", s)
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()
