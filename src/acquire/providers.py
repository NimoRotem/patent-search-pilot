"""The cascade: cheapest and most complete first, falling through on a miss.

    0. corpus        we may already hold the disclosure under another publication in the same
                     family. Free, one indexed read, and it answers 22,099 of the 52,176 starved
                     niche publications (42.4%, measured on the live corpus 2026-08-22).
    1. marec         EP/WO bulk XML, from a configured mirror. Inert until one exists.
    2. uspto_bulk    the USPTO's own PTGRXML / APPXML full-text bulk products, same mirror shape.
    3. epo_ops       the official EPO API. EP and WO only (verified 2026-08-14: US, DE, CN and JP
                     all 404 on /claims and /description). Free inside the 4 GB/week tier, which
                     is of the order of 200,000 documents a week.
    4. pqai          free, US only, and its per-publication route is NOT quota counted.
    5. serp_self     Google Patents fetched from our own IP. Every jurisdiction, already in
                     English, free, and the one rung that can get this box cut off, so it is
                     paced at 2 in flight with 0.7 s between calls and it self-disables.
    6. scrapingbee   the same pages through somebody else's IPs. 15 credits a page.
    7. himmpat       CN / JP / KR / TW translations, metered trial key.
    8. serpapi       paid, and LAST.

ORDER NOTE, deliberate and measured. The brief lists SerpApi at rung 5 and "the other adapters" at
rung 6, while also calling SerpApi the last resort. Those cannot both hold, so cost decides.
SerpApi bills $0.0092 a document (30,000 a month, 12,501 left on 2026-08-22). ScrapingBee bills 15
credits a page against 829,464 remaining of 1,000,000, renewing 2026-09-02, and the two channels
fetch THE SAME Google Patents page: the paid one is strictly more expensive for an identical
document. So SerpApi sits at the bottom, and it is the only rung with a hard reserved budget.

RATE LIMITS AND TIMEOUTS ARE ENFORCED HERE, NOT TRUSTED FROM THE PROVIDER. Every rung runs behind
a `Gate`: a semaphore, a minimum interval between calls, a wall-clock timeout, and a breaker that
opens after consecutive failures. A provider that hangs costs one publication its timeout and is
then skipped for the whole partition until the breaker closes again. That is the difference
between a slow provider and a stalled worker.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import time
from dataclasses import dataclass, field

import httpx

MIN_DESC_CHARS = int(os.environ.get("PATENTS_MIN_DESC_CHARS", "800"))
MIN_CLAIMS_CHARS = int(os.environ.get("PATENTS_MIN_CLAIMS_CHARS", "200"))


@dataclass
class FetchResult:
    provider: str
    claims: str = ""
    description: str = ""
    abstract: str = ""
    title: str = ""
    claims_lang: str = ""
    desc_lang: str = ""
    meta: dict = field(default_factory=dict)
    raw: bytes = b""
    raw_ext: str = "json"
    credits: float = 0.0
    usd: float = 0.0

    def complete(self) -> bool:
        """Enough to count as read in full. Below this we hold a stub (an abstract, a filing
        receipt with no body) and the next rung should still be tried."""
        return (len(self.description or "") >= MIN_DESC_CHARS
                or len(self.claims or "") >= MIN_CLAIMS_CHARS)

    def record(self) -> dict:
        """The shape `sources.docstore.put` understands."""
        rec = {"source": self.provider, "claims": self.claims, "description": self.description,
               "abstract": self.abstract, "title": self.title,
               "claims_lang": self.claims_lang, "desc_lang": self.desc_lang}
        rec.update(self.meta or {})
        return rec


class BudgetRefused(RuntimeError):
    """The cap for this provider is spent. A normal cascade outcome, not a failure."""


# ---------------------------------------------------------------------------------------------
# the gate: our rate limit, our timeout, our breaker
# ---------------------------------------------------------------------------------------------
class Gate:
    def __init__(self, name: str, concurrency: int, min_interval: float, timeout: float,
                 breaker_after: int = 8, breaker_seconds: float = 300.0):
        self.name = name
        self.concurrency = max(1, int(concurrency))
        self.min_interval = float(min_interval)
        self.timeout = float(timeout)
        self.breaker_after = int(breaker_after)
        self.breaker_seconds = float(breaker_seconds)
        self._sem = None
        self._pace = None
        self._last = 0.0
        self._fails = 0
        self._open_until = 0.0
        self.reason = ""

    def _sems(self):
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
            self._pace = asyncio.Lock()
        return self._sem, self._pace

    def open(self) -> bool:
        """True when the breaker has tripped and this rung must be skipped."""
        return time.monotonic() < self._open_until

    def note_ok(self) -> None:
        self._fails = 0

    def note_fail(self, why: str) -> None:
        self._fails += 1
        if self._fails >= self.breaker_after:
            self._open_until = time.monotonic() + self.breaker_seconds
            self.reason = f"{self._fails} consecutive failures ({why[:80]})"
            self._fails = 0

    async def run(self, coro_factory):
        """One paced, bounded, timed call. Raises asyncio.TimeoutError on a hang."""
        sem, pace = self._sems()
        async with sem:
            if self.min_interval > 0:
                async with pace:
                    gap = self.min_interval - (time.monotonic() - self._last)
                    if gap > 0:
                        await asyncio.sleep(gap)
                    self._last = time.monotonic()
            return await asyncio.wait_for(coro_factory(), timeout=self.timeout)


# ---------------------------------------------------------------------------------------------
# provider base
# ---------------------------------------------------------------------------------------------
class Provider:
    name = "base"
    concurrency = 4
    min_interval = 0.0
    timeout = 45.0
    credits = 0.0
    usd = 0.0
    budget_key = ""          # set to enable the reserved hard budget

    def __init__(self):
        self.gate = Gate(self.name, self.concurrency, self.min_interval, self.timeout)

    def available(self):
        return True, ""

    def covers(self, pub: str) -> bool:
        return True

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        raise NotImplementedError


def _country(pub: str) -> str:
    from sources.schema import country_of
    return country_of(pub)


def _adapter(name: str):
    """The PROCESS-WIDE adapter singleton, not a fresh instance. EPO OPS caches its OAuth token
    on the instance and OPS bills per call, so a per-provider copy would re-authenticate on every
    document; the SerpApi and HimmPat quota latches are on the instance too."""
    from sources.registry import registry
    a = registry().get(name)
    if a is None:
        raise KeyError(f"no source adapter named {name!r}")
    return a


# ---------------------------------------------------------------------------------------------
# 0. the corpus we already hold
# ---------------------------------------------------------------------------------------------
class CorpusProvider(Provider):
    """Read only, and the cheapest rung by a distance.

    Two lookups. The publication itself, in case the manifest is stale or another process filled
    it in; then its simple family, because a Chinese publication with no text in the corpus very
    often has a US or EP sibling whose claims and description ARE held. The disclosure is the
    same document; only the language and the office differ. `donor_publication` goes into the
    record's meta so a reader can always tell whose words these are.
    """
    name = "corpus"
    concurrency = 4
    timeout = 30.0
    MAX_SIBLINGS = 20

    def __init__(self, allow_family_donor: bool = None):
        super().__init__()
        if allow_family_donor is None:
            allow_family_donor = os.environ.get(
                "FULLTEXT_ALLOW_FAMILY_DONOR", "1").lower() not in ("0", "false", "no")
        self.allow_family_donor = allow_family_donor

    @staticmethod
    def _text_for(cur, pub_id: int) -> tuple:
        cur.execute("SELECT claim_no, COALESCE(NULLIF(resolved_text, ''), text) AS t, lang "
                    "FROM claims WHERE publication_id=%s ORDER BY claim_no", (pub_id,))
        rows = cur.fetchall() or []
        claims = "\n".join(f"{r['claim_no']}. {r['t']}" for r in rows if r["t"])
        clang = next((r["lang"] for r in rows if r.get("lang")), "")
        cur.execute("SELECT text, lang FROM paragraphs WHERE publication_id=%s ORDER BY id",
                    (pub_id,))
        rows = cur.fetchall() or []
        desc = "\n".join(r["text"] for r in rows if r["text"])
        dlang = next((r["lang"] for r in rows if r.get("lang")), "")
        return claims, clang, desc, dlang

    def _lookup(self, pub: str):
        import db
        with db.cursor(autocommit=True, readonly=True) as cur:
            cur.execute(
                "SELECT id, publication_number, simple_family_id, title, abstract, abstract_lang "
                "FROM publications WHERE upper(regexp_replace(publication_number, "
                "'[^A-Za-z0-9]', '', 'g')) = %s LIMIT 1", (pub.upper(),))
            row = cur.fetchone()
            if row is None:
                return None
            claims, clang, desc, dlang = self._text_for(cur, row["id"])
            if claims or desc:
                return {"claims": claims, "description": desc, "claims_lang": clang,
                        "desc_lang": dlang, "title": row["title"] or "",
                        "abstract": row["abstract"] or "", "donor": ""}
            if not self.allow_family_donor or not row["simple_family_id"]:
                return None
            cur.execute(
                "SELECT id, publication_number FROM publications WHERE simple_family_id=%s "
                "AND id <> %s LIMIT %s",
                (row["simple_family_id"], row["id"], self.MAX_SIBLINGS))
            best, best_len = None, 0
            for sib in cur.fetchall() or []:
                c, cl, d, dl = self._text_for(cur, sib["id"])
                n = len(c) + len(d)
                if n > best_len:
                    best, best_len = {
                        "claims": c, "description": d, "claims_lang": cl, "desc_lang": dl,
                        "title": row["title"] or "", "abstract": row["abstract"] or "",
                        "donor": sib["publication_number"],
                        "family_id": str(row["simple_family_id"])}, n
            return best

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        found = await asyncio.to_thread(self._lookup, pub)
        if not found:
            return None
        meta = {}
        if found.get("donor"):
            meta = {"donor_publication": found["donor"],
                    "family_id": found.get("family_id", ""),
                    "text_is_family_donor": True}
        return FetchResult(
            provider=(f"corpus:family" if found.get("donor") else "corpus"),
            claims=found["claims"], description=found["description"],
            abstract=found.get("abstract", ""), title=found.get("title", ""),
            claims_lang=found.get("claims_lang", ""), desc_lang=found.get("desc_lang", ""),
            meta=meta, raw=b"", raw_ext="")


# ---------------------------------------------------------------------------------------------
# 1-2. bulk XML mirrors (MAREC, USPTO full-text products)
# ---------------------------------------------------------------------------------------------
def parse_st36(xml_bytes: bytes) -> dict:
    """WIPO ST.36 / EPO OPS / USPTO grant XML -> {claims, description, abstract, title}.

    One parser for all three because they agree on the element names that matter: `claims` holds
    `claim`/`claim-text`, `description` holds `p`, `abstract` holds `p`, and the title is
    `invention-title`. Namespaces are ignored by matching on the local name, because the same
    office changes its namespace between DTD versions and a namespace-exact match is how a parser
    silently returns nothing for a whole decade of documents.
    """
    from lxml import etree

    def local(tag) -> str:
        if not isinstance(tag, str):
            return ""
        return tag.rsplit("}", 1)[-1].lower()

    try:
        root = etree.fromstring(xml_bytes, parser=etree.XMLParser(recover=True, huge_tree=True))
    except Exception:
        return {}
    if root is None:
        return {}

    def gather(node) -> str:
        """Paragraph structure kept: one line per <p>, because the chunker downstream splits on
        paragraphs and a single flattened blob would give it nothing to split on."""
        parts = []
        kids = [k for k in node.iter() if local(k.tag) in ("p", "paragraph")]
        for k in (kids or [node]):
            t = " ".join(x.strip() for x in k.itertext() if x and x.strip())
            if t:
                parts.append(" ".join(t.split()))
        return "\n".join(parts)

    out = {"claims": "", "description": "", "abstract": "", "title": ""}
    for el in root.iter():
        name = local(el.tag)
        if name == "claims" and not out["claims"]:
            chunks = []
            kids = [k for k in el if local(k.tag) in ("claim", "claim-text")]
            for k in (kids or [el]):
                t = " ".join(x.strip() for x in k.itertext() if x and x.strip())
                if t:
                    chunks.append(" ".join(t.split()))
            out["claims"] = "\n".join(chunks)
        elif name == "description" and not out["description"]:
            out["description"] = gather(el)
        elif name == "abstract" and not out["abstract"]:
            out["abstract"] = gather(el)
        elif name == "invention-title" and not out["title"]:
            out["title"] = " ".join(" ".join(el.itertext()).split())
    return out


class BulkXmlProvider(Provider):
    """A mirror of bulk full-text XML, laid out one file per publication.

    Looks for `{root}/**/{PUB}.xml` (also `.XML`, `.xml.gz`), so a mirror can be organised by
    office, by year or flat and this still finds the file. `root` may be a local directory or a
    `gs://bucket/prefix`; the GCS form reads through the same token path as the blob store.

    NOT CONFIGURED IS NOT AN ERROR. MAREC is a licensed research collection distributed by the
    Information Retrieval Facility, which no longer operates, and no mirror of it exists on this
    fleet: `find / -iname '*marec*'` on the patents VM returns nothing. The USPTO's own
    equivalents do exist and are listed by its bulk product API (PTGRXML "Patent Grant Full-Text
    Data (No Images) - XML", APPXML "Patent Application Full-Text Data (No Images)"), but they are
    weekly ZIP archives, not a per-publication API, so they need a mirror to be built before this
    rung can answer. Until a root is configured this returns "not configured" and the cascade
    falls through, which is the documented inert default.
    """
    concurrency = 8
    timeout = 60.0

    def __init__(self, name: str, root_env: str, countries=None):
        self.name = name
        super().__init__()
        self.root_env = root_env
        self.countries = tuple(countries or ())
        self._index = None

    @property
    def root(self) -> str:
        return os.environ.get(self.root_env, "")

    def available(self):
        if not self.root:
            return False, (f"{self.root_env} is not set: no {self.name} mirror exists on this "
                           f"fleet, so this rung is inert and the cascade falls through")
        return True, ""

    def covers(self, pub: str) -> bool:
        return not self.countries or _country(pub) in self.countries

    def _local_paths(self, pub: str) -> list:
        root = self.root
        out = []
        for ext in ("xml", "XML", "xml.gz"):
            out.append(os.path.join(root, f"{pub}.{ext}"))
            out.append(os.path.join(root, _country(pub), f"{pub}.{ext}"))
        out.extend(sorted(glob.glob(os.path.join(root, "**", f"{pub}.xml"), recursive=True))[:1])
        return out

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        root = self.root
        if not root:
            return None
        if root.startswith("gs://"):
            bucket, _, prefix = root[5:].partition("/")
            from . import blobstore
            for name in (f"{prefix.rstrip('/')}/{pub}.xml", f"{prefix.rstrip('/')}/"
                         f"{_country(pub)}/{pub}.xml"):
                try:
                    tok = await blobstore.token(client)
                    import urllib.parse
                    r = await client.get(
                        blobstore.OBJECT.format(bucket=bucket,
                                                name=urllib.parse.quote(name, safe="")),
                        params={"alt": "media"},
                        headers={"Authorization": f"Bearer {tok}"}, timeout=self.timeout)
                except Exception:
                    continue
                if r.status_code == 200:
                    return self._result(r.content)
            return None
        data = await asyncio.to_thread(self._read_local, pub)
        return self._result(data) if data else None

    def _read_local(self, pub: str):
        import gzip
        for p in self._local_paths(pub):
            if not p or not os.path.exists(p):
                continue
            try:
                if p.endswith(".gz"):
                    with gzip.open(p, "rb") as fh:
                        return fh.read()
                with open(p, "rb") as fh:
                    return fh.read()
            except Exception:
                continue
        return None

    def _result(self, data: bytes):
        parsed = parse_st36(data)
        if not parsed:
            return None
        return FetchResult(provider=self.name, claims=parsed["claims"],
                           description=parsed["description"], abstract=parsed["abstract"],
                           title=parsed["title"], raw=data, raw_ext="xml")


# ---------------------------------------------------------------------------------------------
# 3. EPO OPS
# ---------------------------------------------------------------------------------------------
class EpoOpsProvider(Provider):
    name = "epo_ops"
    concurrency = int(os.environ.get("FULLTEXT_OPS_CONCURRENCY", "6"))
    min_interval = float(os.environ.get("FULLTEXT_OPS_MIN_INTERVAL", "0.15"))
    timeout = 60.0

    def __init__(self):
        super().__init__()
        self.adapter = _adapter("epo_ops")

    def available(self):
        return (self.adapter.enabled(), self.adapter.disabled_reason())

    def covers(self, pub: str) -> bool:
        from sources.epo_ops import FULLTEXT_COUNTRIES
        return _country(pub) in FULLTEXT_COUNTRIES

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        d = await self.adapter.fulltext(pub, client)
        if not d:
            return None
        return FetchResult(provider=self.name, claims=d.get("claims", ""),
                           description=d.get("description", ""),
                           claims_lang=d.get("claims_lang", ""),
                           desc_lang=d.get("desc_lang", ""),
                           raw=json.dumps(d, ensure_ascii=False).encode("utf-8"),
                           raw_ext="json")


# ---------------------------------------------------------------------------------------------
# 4. PQAI
# ---------------------------------------------------------------------------------------------
class PqaiProvider(Provider):
    """US only, free, and its per-publication route is not counted against the search quota.
    Verified in the ladder it is ported from: full claims and description for US, nothing at all
    for EP / WO / CN / DE / JP."""
    name = "pqai"
    concurrency = int(os.environ.get("FULLTEXT_PQAI_CONCURRENCY", "8"))
    timeout = 45.0

    def __init__(self):
        super().__init__()
        self.adapter = _adapter("pqai")

    def available(self):
        return (self.adapter.enabled(), self.adapter.disabled_reason())

    def covers(self, pub: str) -> bool:
        return _country(pub) == "US"

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        d = await self.adapter.details(pub, client)
        if not d:
            return None
        return FetchResult(provider=self.name, claims=d.get("claims", "") or "",
                           description=d.get("description", "") or "",
                           abstract=d.get("abstract", "") or "", title=d.get("title", "") or "",
                           claims_lang="en", desc_lang="en",
                           raw=json.dumps(d, ensure_ascii=False, default=str).encode("utf-8"),
                           raw_ext="json")


# ---------------------------------------------------------------------------------------------
# 5. the self-hosted SERP path: Google Patents from our own IP
# ---------------------------------------------------------------------------------------------
class SelfSerpProvider(Provider):
    """Free, every jurisdiction, already translated into English by Google, and the one rung that
    can get this box cut off. Measured 2026-08-14: a burst of ~80 requests at concurrency 8-20 ran
    at 50 docs/second with every response 200, and then the whole IP took a plain 503 on both the
    document pages and the XHR search, still refusing 2.5 minutes later. So the throughput this
    endpoint appears to offer is not throughput you can spend.

    The page is fetched through the adapter's own paced `_get`, which owns the block latch, so the
    cooldown is shared with anything else in the process that uses it; the HTML is kept because
    the raw bytes are what makes a parser fix a re-parse rather than a re-fetch.
    """
    name = "serp_self"
    concurrency = int(os.environ.get("FULLTEXT_SELFSERP_CONCURRENCY", "2"))
    min_interval = float(os.environ.get("FULLTEXT_SELFSERP_MIN_INTERVAL", "1.0"))
    timeout = 45.0

    def available(self):
        from sources import gpatents_direct as G
        if not G.available():
            return False, f"cooling down: {G.block_reason()}"
        return True, ""

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        from sources import gpatents_direct as G
        r = await G._get(client, G.DOC.format(pn=pub))
        if r.status_code != 200:
            return None
        rec = G.parse_document(r.text, pub)
        if not rec:
            return None
        return _from_gpatents(self.name, rec, r.text)


def _from_gpatents(provider: str, rec: dict, page_html: str) -> FetchResult:
    meta = {k: rec[k] for k in ("assignee", "date", "priority_date", "filing_date", "kind",
                                "country", "inventors", "cpc", "images", "pdf", "family",
                                "backward", "forward", "legal_state", "legal_status")
            if rec.get(k)}
    return FetchResult(provider=provider, claims=rec.get("claims", "") or "",
                       description=rec.get("description", "") or "",
                       abstract=rec.get("abstract", "") or "", title=rec.get("title", "") or "",
                       claims_lang=rec.get("claims_lang", "") or "",
                       desc_lang=rec.get("desc_lang", "") or "",
                       meta=meta, raw=(page_html or "").encode("utf-8", "replace"),
                       raw_ext="html")


# ---------------------------------------------------------------------------------------------
# 6. ScrapingBee: the same pages, their IPs
# ---------------------------------------------------------------------------------------------
class ScrapingBeeProvider(Provider):
    """ScrapingBee classes patents.google.com as Google, so it needs `custom_google=True` and
    bills 15 credits a page rather than 1. Balance on 2026-08-22: 829,464 of 1,000,000 left,
    renewing 2026-09-02, which is ~55,000 pages. That is the rung that makes reading tens of
    thousands of documents a real option instead of an aspiration."""
    name = "scrapingbee"
    concurrency = int(os.environ.get("FULLTEXT_SB_CONCURRENCY", "8"))
    timeout = 120.0
    credits = 15.0
    budget_key = "scrapingbee"

    def available(self):
        return (bool(os.environ.get("SCRAPINGBEE_API_KEY", "")),
                "SCRAPINGBEE_API_KEY not set")

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        key = os.environ.get("SCRAPINGBEE_API_KEY", "")
        if not key:
            return None
        r = await client.get("https://app.scrapingbee.com/api/v1/",
                             params={"api_key": key,
                                     "url": f"https://patents.google.com/patent/{pub}/en",
                                     "custom_google": "True", "render_js": "False"},
                             timeout=self.timeout)
        if r.status_code != 200:
            return None
        from sources.gpatents_direct import parse_document
        rec = parse_document(r.text, pub)
        if not rec:
            return None
        out = _from_gpatents(self.name, rec, r.text)
        out.credits = self.credits
        return out


# ---------------------------------------------------------------------------------------------
# 7. HimmPat: CN / JP / KR / TW
# ---------------------------------------------------------------------------------------------
class HimmPatProvider(Provider):
    name = "himmpat"
    concurrency = 2
    min_interval = 0.5
    timeout = 60.0
    credits = 1.0
    budget_key = "himmpat"

    def __init__(self):
        super().__init__()
        self.adapter = _adapter("himmpat")

    def available(self):
        return (self.adapter.enabled(), self.adapter.disabled_reason())

    def covers(self, pub: str) -> bool:
        return _country(pub) in ("CN", "JP", "KR", "TW")

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        d = await self.adapter.details(pub, client)
        if not d:
            return None
        out = FetchResult(provider=self.name, claims=d.get("claims", "") or "",
                          description=d.get("description", "") or "",
                          abstract=d.get("abstract", "") or "", title=d.get("title", "") or "",
                          claims_lang=d.get("claims_lang", "") or "",
                          desc_lang=d.get("description_lang", "") or "",
                          raw=json.dumps(d, ensure_ascii=False, default=str).encode("utf-8"),
                          raw_ext="json")
        out.credits = self.credits
        return out


# ---------------------------------------------------------------------------------------------
# 8. SerpApi: paid, capped, last
# ---------------------------------------------------------------------------------------------
class SerpApiProvider(Provider):
    """The backstop that makes every rung above it optional rather than load-bearing, and the
    only one that spends real money per document. $0.0092 a document on the Big Data plan;
    30,000 searches a month, 6,000 an hour, and 12,501 left when this was written. Every credit
    is reserved through `ledger.reserve` before the call, so four workers share one cap rather
    than holding four copies of it."""
    name = "serpapi"
    concurrency = int(os.environ.get("FULLTEXT_SERPAPI_CONCURRENCY", "4"))
    min_interval = float(os.environ.get("FULLTEXT_SERPAPI_MIN_INTERVAL", "0.6"))
    timeout = 90.0
    credits = 1.0
    usd = 0.00917
    budget_key = "serpapi"

    def __init__(self):
        super().__init__()
        self.adapter = _adapter("serpapi_gpatents")

    def available(self):
        return (self.adapter.enabled(), self.adapter.disabled_reason())

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        d = await self.adapter.details(pub, client)
        if not d:
            return None
        meta = {k: d[k] for k in ("assignee", "inventors", "cpc", "images", "pdf", "family",
                                  "legal_state", "legal_status") if d.get(k)}
        if d.get("patent_citations"):
            meta["backward"] = d["patent_citations"]
        if d.get("cited_by"):
            meta["forward"] = d["cited_by"]
        out = FetchResult(provider=self.name, claims=d.get("claims", "") or "",
                          description=d.get("description", "") or "",
                          abstract=d.get("abstract", "") or "", title=d.get("title", "") or "",
                          meta=meta,
                          raw=json.dumps(d, ensure_ascii=False, default=str).encode("utf-8"),
                          raw_ext="json")
        out.credits, out.usd = self.credits, self.usd
        return out


# ---------------------------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------------------------
DEFAULT_ORDER = ["corpus", "marec", "uspto_bulk", "epo_ops", "pqai", "serp_self",
                 "scrapingbee", "himmpat", "serpapi"]

#  Per-provider hard caps for one calendar month, enforced by ledger.reserve(). Names are the
#  provider names; units are that provider's own credits.
DEFAULT_CAPS = {
    "serpapi": float(os.environ.get("FULLTEXT_SERPAPI_BUDGET", "2000")),
    "scrapingbee": float(os.environ.get("FULLTEXT_SCRAPINGBEE_BUDGET", "300000")),
    "himmpat": float(os.environ.get("FULLTEXT_HIMMPAT_BUDGET", "200")),
}


def build(order=None) -> list:
    """The cascade, in order. `FULLTEXT_CASCADE` overrides it (comma separated provider names),
    which is how an operator turns off a rung without a deploy."""
    names = order or [n for n in os.environ.get("FULLTEXT_CASCADE", "").split(",") if n.strip()]
    names = names or DEFAULT_ORDER
    made = []
    for n in names:
        n = n.strip()
        if n == "corpus":
            made.append(CorpusProvider())
        elif n == "marec":
            made.append(BulkXmlProvider("marec", "MAREC_ROOT", countries=("EP", "WO")))
        elif n == "uspto_bulk":
            made.append(BulkXmlProvider("uspto_bulk", "USPTO_FULLTEXT_ROOT", countries=("US",)))
        elif n == "epo_ops":
            made.append(EpoOpsProvider())
        elif n == "pqai":
            made.append(PqaiProvider())
        elif n == "serp_self":
            made.append(SelfSerpProvider())
        elif n == "scrapingbee":
            made.append(ScrapingBeeProvider())
        elif n == "himmpat":
            made.append(HimmPatProvider())
        elif n == "serpapi":
            made.append(SerpApiProvider())
        else:
            raise ValueError(f"unknown provider {n!r} in the cascade")
    return made
