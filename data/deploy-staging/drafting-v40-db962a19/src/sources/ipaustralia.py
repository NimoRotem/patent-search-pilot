"""IP Australia adapter — authoritative AU patents (Australian Patent Search API).

OAuth2 client-credentials against the IP Australia API gateway (MuleSoft). The
`external-token-api` mints a Bearer token (valid 24h); the Australian Patent
Search API `/search/quick` returns AU patent/application records by keyword.

VERIFIED AGAINST THE LIVE PRODUCTION API 2026-07-20 (account nemograbo2026,
app "b2b_GRABO prior-art and IP monitoring_prod", Base Tier). Things that were
non-obvious and are asserted in code so this can't ship dark:

  1. `/search/quick` REQUIRES both keys `searchType` and `query`. `searchType`
     is an enum: "DETAILS" (full records) or "ID" (numbers only). Omitting
     either — or any other value — is a hard HTTP 400 "Bad request" with NO
     field detail (the gateway masks it). We send {"searchType":"DETAILS",
     "query": ...}; a 400 is surfaced as a source_error, never swallowed.
  2. The response is {totalHits, results:[{applicationNumber, title:[...],
     filingDate:"YYYYMMDD", applicants:[...], applicationStatus, pctNumber}]}.
     `title` and `applicants` are ARRAYS; `filingDate` is compact YYYYMMDD.
  3. IP Australia does NOT return a DOCDB/INPADOC family id. We therefore do
     NOT populate Candidate.family_id (a digit-bearing family_id would be
     treated downstream as an authoritative DOCDB id and would forge a wrong
     Espacenet family URL). The native AU application number goes in
     `extra["au_application"]` instead.

Auth: IPA_CLIENT_ID / IPA_CLIENT_SECRET. Self-service, search APIs auto-approved.
No commercial-use restriction (AU government open API, standard T&C).

Ported from patents-app `adapters/ipaustralia.py` unchanged in behavior.
"""
from __future__ import annotations

import os
import time

import httpx

from .schema import Candidate, SubQuery, normalize_pub, espacenet_url
from .base import Adapter, cached_json

GATEWAY = "https://production.api.ipaustralia.gov.au/public"
TOKEN_URL = f"{GATEWAY}/external-token-api/v1/access_token"
SEARCH_URL = f"{GATEWAY}/australian-patent-search-api/v1/search/quick"


def _fmt_date(d: str) -> str:
    """'20200520' -> '2020-05-20'; pass through anything else."""
    d = str(d or "").strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


class IPAustralia(Adapter):
    name = "ipaustralia"

    def __init__(self):
        self.client_id = os.environ.get("IPA_CLIENT_ID", "")
        self.client_secret = os.environ.get("IPA_CLIENT_SECRET", "")
        self._token = ""
        self._token_exp = 0.0
        # Soft contract violations drained by the facade into the errors list —
        # stops the adapter going dark unnoticed.
        self._warnings: list[str] = []

    def _warn(self, msg: str) -> None:
        if len(self._warnings) < 16 and msg not in self._warnings:
            self._warnings.append(msg)

    def pop_warnings(self) -> list[str]:
        w, self._warnings = self._warnings, []
        return w

    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def disabled_reason(self) -> str:
        return ("IPA_CLIENT_ID / IPA_CLIENT_SECRET not set "
                "(self-service at portal.api.ipaustralia.gov.au, search APIs auto-approved)")

    def search_note(self) -> str:
        return ("AU patents/applications (authoritative national office) — "
                "Base Tier 600 req/min; no commercial-use restriction")

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and self._token_exp > time.monotonic() + 60:
            return self._token
        # Let auth errors propagate: a dead/rotated credential must be VISIBLE
        # (source_error), not reported as "no matches".
        r = await client.post(
            TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            content="grant_type=client_credentials",
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        self._token = j["access_token"]
        # tokens are 24h; refresh a minute early
        self._token_exp = time.monotonic() + int(j.get("expires_in", 3600))
        return self._token

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        if not self.enabled():
            return []
        query = str(sq.native or "").strip()
        if not query:
            # Unrecoverable: refuse to send an empty query rather than return a
            # plausible-looking empty list.
            raise ValueError("empty ipaustralia query (planner emitted no keywords)")

        token = await self._get_token(client)
        # BOTH keys are required by the RAML; searchType is a strict enum
        # (DETAILS|ID). A wrong/absent value is a hard 400 with no field detail.
        body = {"searchType": "DETAILS", "query": query}
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/json",
                   "Content-Type": "application/json"}
        # Let HTTP/quota errors propagate: the executor turns them into a
        # source_error event, so a 400 (contract drift), 401/403 (auth/contract)
        # or 429 (rate limit) is VISIBLE rather than silently reported as zero.
        data = await cached_json(
            client, "POST", SEARCH_URL,
            cache_key=f"ipa:{sq.cache_key()}",
            json=body, headers=headers, ttl=3600,
        )

        rows = data.get("results")
        if rows is None:
            raise ValueError(
                f"ipaustralia response has no 'results' key (keys={sorted(data)[:8]}) "
                "— the wire contract changed")
        total = data.get("totalHits")
        if not rows and total:
            self._warn(
                f"ipaustralia reported totalHits={total} but returned 0 results for "
                f"{query[:60]!r} — pagination/response contract changed")

        out: list[Candidate] = []
        for i, r in enumerate(rows):
            app = str(r.get("applicationNumber") or "").strip()
            if not app:
                self._warn("ipaustralia result without applicationNumber — shape changed")
                continue
            pub = normalize_pub(f"AU{app}")
            titles = r.get("title") or []
            title = titles[0] if isinstance(titles, list) and titles else (
                titles if isinstance(titles, str) else "")
            applicants = r.get("applicants") or []
            date = _fmt_date(r.get("filingDate"))
            extra = {"au_application": app,
                     "status": r.get("applicationStatus", "") or ""}
            if r.get("pctNumber"):
                extra["pct_number"] = r["pctNumber"]
            out.append(Candidate(
                pub_number=pub,
                source=self.name,
                source_rank=i + 1,
                title=str(title or ""),
                assignee="; ".join([str(a) for a in applicants[:3]]) if isinstance(applicants, list) else str(applicants),
                date=date,
                priority_date=date,
                # AU numbers carry no kind code and often 404 on Google's direct
                # /patent/ route; Espacenet pn= is an exact, reliable lookup.
                url=espacenet_url(pub),
                found_by=[sq.rationale or sq.element],
                extra=extra,
            ))
        return out
