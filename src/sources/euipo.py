"""EUIPO adapter, registered Community designs (EU design-search API).

Registered Community Designs are the one class of EU industrial-property right that neither
Google Patents nor BigQuery carries, and they are real prior art for appearance: an RCD
published before a subject's priority date discloses that appearance to the public. This is
the source that closes that hole. It searches DESIGNS only. The same subscription also
covers trademark-search, which is not prior art for a patent and is deliberately not wired
here; the IP monitor on grabo-systems is the consumer for that.

VERIFIED AGAINST THE LIVE PRODUCTION API 2026-08-23 (account nemograbo2026, app
"GRABO prior-art & IP monitoring", both Production subscriptions approved 2026-07-20).
Things that are not guessable from the portal and are asserted in code so this cannot ship
dark:

  1. THE TOKEN HOST IS NOT auth.euipo.europa.eu. That host is an F5 that returns a bare
     `502 Bad Gateway` for every path, from our egress and from unrelated networks alike,
     and it had been recorded as our endpoint for a month, which read as an IP block and
     kept the whole subscription shelved. The live endpoint is the one the product page's
     own OpenAPI spec advertises under `x-ibm-configuration.oauth-servers`:
     `https://euipo.europa.eu/cas-server-webapp/oidc/accessToken`. `login.euipo.europa.eu`
     is a live WSO2 server but a different tenant and rejects these credentials.
  2. BOTH headers are required on every call: `Authorization: Bearer <token>` AND
     `X-IBM-Client-Id: <client_id>`. The gateway 401s with "Invalid client id or secret"
     when either is missing, which reads like a credential problem and is not one.
  3. `size` BELOW 10 IS A HARD 400 ("Page size must be greater than or equal to 10"). A
     caller asking for the top 5 therefore gets zero, not five.
  4. THE SEARCH RESPONSE CARRIES NO TEXT AND NO NAMES. A search row has designNumber,
     locarnoClasses, dates, status and applicants as bare `{office, identifier}` references
     with no `name`. The product indication, the applicant's name and the drawings only
     exist on `/designs/{designNumber}`. A candidate built from the search row alone has an
     empty title and is unreadable, so the top `EUIPO_HYDRATE` rows are hydrated (see
     `_hydrate`). This is also why the ip-monitor's copy of this code dropped every design
     it fetched: it filtered on an applicant name the search response never contained.
  5. RSQL WILDCARDS MATCH THE INDICATION VERBATIM. `productIndications=="*vacuum lifter*"`
     returns 0 while `*suction*` returns 546, because indications are short curated phrases
     ("Suction cups for attachment") and a two-word wildcard rarely spans one. Terms are
     therefore OR'd individually rather than sent as one phrase.
  6. Locarno, not CPC. Designs are classified under Locarno; `SubQuery.cpc` is a CPC list
     and there is no honest mapping between the two, so it is ignored rather than
     mistranslated. `sq.date_to` IS used, because a design published after the subject's
     priority date is not prior art and fetching it wastes budget.

QUOTA: 35,000 calls/day, reported live on every response as `x-ratelimit-remaining`
(name=default,NNNNN). Read back into `self.remaining` so the source can say how much it has
left instead of discovering the wall mid-run.

DELIBERATELY NOT WIRED INTO THE PATENT FAN-OUT. `external.plan()` does not emit euipo
sub-queries and must not: `external.materialise()` inserts every surviving candidate into
the `publications` corpus, and an RCD has no claims, no description and no abstract, so
thousands of permanently text-less rows would land in the corpus whose text-depth
accounting (`corpus_facts`, REACHABILITY.md) is the engine's whole quality story. Designs
are answered on their own terms. `Candidate.kind` is set to "S" so that every existing
design-aware guard (`schema.is_design`, `webview._is_design`) recognises these for what
they are if one ever does reach a patent report.

Auth: EUIPO_KEY / EUIPO_SECRET (advisor credential `euipo`).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Optional

import httpx

from .schema import Candidate, SubQuery, iso_date
from .base import Adapter, cached_json

TOKEN_URL = "https://euipo.europa.eu/cas-server-webapp/oidc/accessToken"
BASE = "https://api.euipo.europa.eu/design-search"
SEARCH_URL = f"{BASE}/designs"

#  The gateway's floor. Asking for fewer is a 400, not a short page.
MIN_PAGE = 10
PAGE_SIZE = max(MIN_PAGE, int(os.environ.get("EUIPO_PAGE_SIZE", "50")))
#  How many of the returned rows get their product indication, applicant and drawing count
#  read from the details endpoint. One call each, against a 35k/day budget.
HYDRATE = int(os.environ.get("EUIPO_HYDRATE", "10"))
#  How many OR'd terms one query carries. Each term widens the result set; past a handful
#  the query stops describing the invention and starts describing the language.
MAX_TERMS = int(os.environ.get("EUIPO_MAX_TERMS", "6"))

#  Words that carry no product meaning. Deliberately short and fixed: a long curated list
#  would be a second vocabulary to maintain, and the RSQL is an OR so a weak term costs
#  recall nothing, only noise.
_STOP = frozenset("""
a an and are as at be by for from has have in into is it its of on or that the their there
this to was were which with without use used using device apparatus system method means
assembly unit arrangement general purpose other others improved novel new
""".split())

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
#  x-ratelimit-remaining: name=default,34861;
_REMAIN_RE = re.compile(r"(\d+)")


def _terms(text: str) -> list[str]:
    """The distinctive words of a query, longest first, de-duplicated.

    Longest-first because a design indication is a short noun phrase: "gripper" and
    "suction" select, "part" and "unit" do not, and length is a good enough proxy for that
    without a corpus statistic this adapter has no access to.
    """
    seen: set[str] = set()
    out: list[str] = []
    for w in _WORD_RE.findall(str(text or "")):
        lw = w.lower()
        if lw in _STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(lw)
    out.sort(key=len, reverse=True)
    return out[:MAX_TERMS]


def _rsql(terms: list[str], date_to: Optional[str] = None) -> str:
    """OR the terms over productIndications, optionally bounded to prior art.

    Quoting: RSQL takes double-quoted arguments and the terms are `[a-z-]+` by
    construction, so there is no quote to escape. Kept as an assertion rather than a
    comment because a future caller passing raw user text here would build a broken query.
    """
    for t in terms:
        if '"' in t or "\\" in t:
            raise ValueError(f"euipo term is not wildcard-safe: {t!r}")
    q = " or ".join(f'productIndications=="*{t}*"' for t in terms)
    d = iso_date(date_to)
    if d:
        #  A design published on or after the subject's priority date is not prior art.
        #  Bounding here rather than downstream keeps the wasted rows off the wire.
        q = f"({q}) and applicationDate<{d}"
    return q


def _english(indications) -> str:
    """The English product indication, else the first language present.

    `productIndications` is a list of {language, terms[]} across every EU language, and the
    translations are NOT synonyms of each other: the record verified on 2026-08-23 reads
    "Suction cups for attachment" in en and "Robots de nettoyage (partie de -)" in fr. Picking
    a language at random would therefore change what the result appears to be about.
    """
    if not isinstance(indications, list):
        return ""
    first = ""
    for block in indications:
        if not isinstance(block, dict):
            continue
        terms = [str(t) for t in (block.get("terms") or []) if t]
        if not terms:
            continue
        joined = "; ".join(terms)
        if str(block.get("language") or "").lower() == "en":
            return joined
        first = first or joined
    return first


def _names(people) -> list[str]:
    out = []
    for p in people or []:
        if isinstance(p, dict) and p.get("name"):
            out.append(str(p["name"]))
    return out


def esearch_url(design_number: str) -> str:
    """EUIPO's own record page for an RCD. Verified 200 on 2026-08-23.

    NOT `schema.designview_url`, which runs the number through `canonical_pub` and strips
    the hyphen; DesignView needs the hyphenated form and the office's own eSearch is the
    authoritative page anyway.
    """
    return f"https://euipo.europa.eu/eSearch/#details/designs/{design_number}"


class EUIPO(Adapter):
    name = "euipo"
    #: NOT part of a patent report's fan-out. See the module docstring: designs are
    #: answered on their own page so they never reach `external.materialise`.
    in_report_fanout = False

    def __init__(self):
        self.key = os.environ.get("EUIPO_KEY", "")
        self.secret = os.environ.get("EUIPO_SECRET", "")
        self._token = ""
        self._token_exp = 0.0
        #: calls left today, read off the gateway's own header; None until a call is made
        self.remaining: Optional[int] = None
        self._warnings: list[str] = []

    # ---------------------------------------------------------------- plumbing
    def _warn(self, msg: str) -> None:
        if len(self._warnings) < 16 and msg not in self._warnings:
            self._warnings.append(msg)

    def pop_warnings(self) -> list[str]:
        w, self._warnings = self._warnings, []
        return w

    def enabled(self) -> bool:
        return bool(self.key and self.secret)

    def disabled_reason(self) -> str:
        return ("EUIPO_KEY / EUIPO_SECRET not set (advisor credential `euipo`; "
                "Production design-search subscription approved 2026-07-20)")

    def search_note(self) -> str:
        left = "" if self.remaining is None else f", {self.remaining} calls left today"
        return ("EU registered Community designs, the appearance right neither Google Patents "
                f"nor BigQuery carries. 35,000 calls/day{left}")

    def _headers(self, token: str) -> dict:
        #  BOTH of these, every call. See gotcha 2.
        return {"Authorization": f"Bearer {token}",
                "X-IBM-Client-Id": self.key,
                "Accept": "application/json"}

    def _note_quota(self, resp) -> None:
        raw = resp.headers.get("x-ratelimit-remaining") or ""
        nums = _REMAIN_RE.findall(raw)
        if nums:
            #  "name=default,34861;" -> the LAST number is the remaining count; the first is
            #  part of the plan name when the plan is numbered.
            try:
                self.remaining = int(nums[-1])
            except ValueError:
                pass

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and self._token_exp > time.monotonic() + 60:
            return self._token
        #  Let auth errors propagate. A dead credential must surface as a source_error, not
        #  as "no designs matched", which is what a swallowed exception looks like.
        r = await client.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"client_id": self.key, "client_secret": self.secret,
                  "grant_type": "client_credentials", "scope": "uid"},
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        tok = j.get("access_token")
        if not tok:
            raise ValueError(f"euipo token response has no access_token (keys={sorted(j)[:8]})")
        self._token = tok
        #  Observed 28800s. Refresh a minute early.
        self._token_exp = time.monotonic() + int(j.get("expires_in", 3600))
        return self._token

    # ---------------------------------------------------------------- search
    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        if not self.enabled():
            return []
        terms = _terms(sq.native if isinstance(sq.native, str) else str(sq.native or ""))
        if not terms:
            #  Refuse to send a query that would match the whole register rather than
            #  returning a plausible-looking empty list.
            raise ValueError("empty euipo query (no distinctive terms in the sub-query)")

        query = _rsql(terms, sq.date_to)
        token = await self._get_token(client)
        r = await client.get(SEARCH_URL, params={"query": query, "size": PAGE_SIZE},
                             headers=self._headers(token))
        self._note_quota(r)
        #  Propagate 400 (query contract drift), 401 (auth), 429 (quota). Visible beats quiet.
        r.raise_for_status()
        data = r.json()

        rows = data.get("designs")
        if rows is None:
            raise ValueError(
                f"euipo response has no 'designs' key (keys={sorted(data)[:8]}) "
                "- the wire contract changed")
        total = data.get("totalElements")
        if not rows and total:
            self._warn(f"euipo reported totalElements={total} but returned 0 rows for "
                       f"{query[:80]!r} - pagination contract changed")

        out: list[Candidate] = []
        for i, d in enumerate(rows):
            num = str(d.get("designNumber") or "").strip()
            if not num:
                self._warn("euipo row without designNumber - shape changed")
                continue
            #  Application date is the disclosure-relevant one for an RCD; registration and
            #  publication follow it. iso_date normalises at the boundary like every other
            #  adapter, so downstream date comparisons cannot meet two spellings.
            app_date = iso_date(d.get("applicationDate"))
            out.append(Candidate(
                pub_number=f"EM{num}",
                source=self.name,
                source_rank=i + 1,
                date=app_date,
                priority_date=app_date,
                #  "S" is the design kind code. It makes schema.is_design() true, so any
                #  design-aware guard downstream treats this as what it is.
                kind="S",
                url=esearch_url(num),
                found_by=[sq.rationale or sq.element],
                extra={
                    "design_number": num,
                    "application_number": str(d.get("applicationNumber") or ""),
                    "locarno": list(d.get("locarnoClasses") or []),
                    "status": str(d.get("status") or ""),
                    "registration_date": iso_date(d.get("registrationDate")),
                    "expiry_date": iso_date(d.get("expiryDate")),
                    "representation": str(d.get("designRepresentationMeans") or ""),
                    "hydrated": False,
                },
            ))

        await self._hydrate(out[:HYDRATE], client, token)
        return out

    async def _hydrate(self, cands: list[Candidate], client: httpx.AsyncClient,
                       token: str) -> None:
        """Fill in title, applicant and drawing count from the details endpoint.

        The search response carries none of them (gotcha 4), so an unhydrated candidate has
        an empty title. Best effort per candidate: one design whose detail call fails must
        not cost the caller the rest of the page, and the row is still usable as a number
        plus a Locarno class. A failure is recorded as a warning so it is visible.
        """
        if not cands:
            return
        results = await asyncio.gather(
            *(self.details(c.extra["design_number"], client, token) for c in cands),
            return_exceptions=True)
        for c, res in zip(cands, results):
            if isinstance(res, BaseException):
                self._warn(f"euipo details failed for {c.extra['design_number']}: {res}")
                continue
            c.title = res.get("title") or ""
            c.abstract = res.get("title") or ""      # an RCD's indication IS its whole text
            c.assignee = "; ".join(res.get("applicants") or [])[:200]
            c.inventors = list(res.get("designers") or [])
            c.extra.update({k: v for k, v in res.items()
                            if k not in ("title", "applicants", "designers")})
            c.extra["hydrated"] = True

    # ---------------------------------------------------------------- details
    async def details(self, pub_number: str, client: httpx.AsyncClient,
                      token: str = "") -> dict:
        """One design's full record. `pub_number` may be the bare design number or EM-prefixed."""
        if not self.enabled():
            return {}
        num = str(pub_number or "").strip()
        if num.upper().startswith("EM"):
            num = num[2:]
        if not num:
            return {}
        token = token or await self._get_token(client)
        data = await cached_json(
            client, "GET", f"{BASE}/designs/{num}",
            cache_key=f"euipo:detail:{num}",
            headers=self._headers(token), ttl=86400,
        )
        views = [v for v in (data.get("views") or []) if isinstance(v, dict)]
        pubs = [p for p in (data.get("publications") or []) if isinstance(p, dict)]
        return {
            "design_number": str(data.get("designNumber") or num),
            "title": _english(data.get("productIndications")),
            "applicants": _names(data.get("applicants")),
            "designers": _names(data.get("designers")),
            "representatives": _names(data.get("representatives")),
            "locarno": list(data.get("locarnoClasses") or []),
            "status": str(data.get("status") or ""),
            "application_date": iso_date(data.get("applicationDate")),
            "registration_date": iso_date(data.get("registrationDate")),
            "expiry_date": iso_date(data.get("expiryDate")),
            #  The date the appearance actually became public, which is the one that decides
            #  whether it is prior art. It is NOT the application date when publication was
            #  deferred, and deferment is a real EUIPO option, so take the earliest bulletin.
            "publication_date": min((iso_date(p.get("publicationDate")) for p in pubs
                                     if iso_date(p.get("publicationDate"))), default=""),
            "deferred": bool(data.get("publicationDefermentIndicator")),
            "views": [int(v.get("order")) for v in views if str(v.get("order") or "").isdigit()],
            "url": esearch_url(str(data.get("designNumber") or num)),
        }

    async def view_bytes(self, design_number: str, order: int = 1, *,
                         client: httpx.AsyncClient, thumbnail: bool = False) -> bytes:
        """One drawing of a design, as image bytes.

        A design IS its drawings, so a result without one is barely a result. The full view is
        large (5000x4110, ~1.5 MB on the record verified 2026-08-23); `thumbnail=True` asks the
        gateway for its own reduced copy instead of us shipping megabytes to a browser.
        """
        num = str(design_number or "").strip()
        if num.upper().startswith("EM"):
            num = num[2:]
        token = await self._get_token(client)
        path = f"{BASE}/designs/{num}/views/{int(order)}"
        if thumbnail:
            path += "/thumbnail"
        r = await client.get(path, headers={"Authorization": f"Bearer {token}",
                                            "X-IBM-Client-Id": self.key})
        self._note_quota(r)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# sync entry points for the web layer
# ---------------------------------------------------------------------------
#  The adapters are async and run on the sources package's one long-lived background loop
#  (see sources/__init__ for why there is exactly one). Flask handlers are threads, so they
#  cross over through `_submit`, the same bridge every other sync caller uses. Imported
#  lazily inside the functions because `sources/__init__` imports `registry`, which imports
#  this module: a top-level import here would be circular.

WEB_TIMEOUT = float(os.environ.get("EUIPO_WEB_TIMEOUT", "60"))


def _adapter() -> "EUIPO":
    from . import registry
    a = registry().get("euipo")
    if a is None:                                        # pragma: no cover - registry drift
        raise RuntimeError("the euipo adapter is not registered")
    return a


def search_designs(query: str, before: str = "", limit: int = 0) -> dict:
    """Registered Community designs matching `query`, optionally published before a date.

    -> {"designs": [...], "query": rsql, "remaining": int|None, "warnings": [...]}

    `before` is the subject's priority date when this is being used to find prior art: a
    design applied for on or after it cannot anticipate. Returns rows as plain dicts because
    the caller is a JSON route, not the ranking pipeline.
    """
    from . import _submit, _new_client
    a = _adapter()
    if not a.enabled():
        return {"designs": [], "query": "", "remaining": None,
                "warnings": [a.disabled_reason()], "enabled": False}
    sq = SubQuery(source="euipo", native=str(query or ""), rationale="design search",
                  element="designs", date_to=(before or None))

    async def go():
        async with _new_client(WEB_TIMEOUT) as c:
            return await a.search(sq, c)

    rows = _submit(go(), timeout=WEB_TIMEOUT + 10)
    out = []
    for c in rows:
        e = c.extra
        num = e.get("design_number") or ""
        out.append({
            "design_number": num,
            "pub_number": c.pub_number,
            "title": c.title,
            "applicant": c.assignee,
            "designers": list(c.inventors or []),
            "application_date": c.date,
            "registration_date": e.get("registration_date", ""),
            "publication_date": e.get("publication_date", ""),
            "expiry_date": e.get("expiry_date", ""),
            "locarno": list(e.get("locarno") or []),
            "status": e.get("status", ""),
            "views": list(e.get("views") or []),
            "hydrated": bool(e.get("hydrated")),
            "url": c.url,
        })
    if limit:
        out = out[:limit]
    return {"designs": out, "query": _rsql(_terms(query), before or None),
            "remaining": a.remaining, "warnings": a.pop_warnings(), "enabled": True}


def design_view(design_number: str, order: int = 1, thumbnail: bool = True) -> bytes:
    """One drawing, fetched server-side.

    The image endpoints need our OAuth token and our client id, so a browser cannot fetch
    them directly and the page has to be served through here.
    """
    from . import _submit, _new_client
    a = _adapter()

    async def go():
        async with _new_client(WEB_TIMEOUT) as c:
            return await a.view_bytes(design_number, order, client=c, thumbnail=thumbnail)

    return _submit(go(), timeout=WEB_TIMEOUT + 10)
