"""The cascade: cheapest and most complete first, falling through on a miss.

    0. corpus        we may already hold the disclosure under another publication in the same
                     family. Free, one indexed read, and it answers 22,099 of the 52,176 starved
                     niche publications (42.4%, measured on the live corpus 2026-08-22).
    1. bq_cjk        BigQuery `patents-public-data`, through a clustered cache we own. CN / JP /
                     KR / TW English TITLE AND ABSTRACT, never claims or description: measured
                     2026-08-22, no BigQuery snapshot from 201710 to 202511 carries a single CJK
                     claim or description. It answers 93.2% of the pending CJK work list at
                     $0.0000033 a document, with no rate limit at all.
    2. marec         EP/WO bulk XML, from a configured mirror. Inert until one exists.
    3. uspto_bulk    the USPTO's own PTGRXML / APPXML full-text bulk products, same mirror shape.
    4. epo_ops       the official EPO API. EP and WO only (re-verified live 2026-08-22: CN, JP
                     and KR return CLIENT.InvalidCountryCode on /claims and /description, which
                     is a refusal by office, not a missing document). Free inside the 4 GB/week
                     tier, which is of the order of 200,000 documents a week.
    5. pqai          free, US only, and its per-publication route is NOT quota counted.
    6. serp_self     Google Patents fetched from our own IP. Every jurisdiction, already in
                     English, free, and the one rung that can get this box cut off, so it is
                     paced at 2 in flight with 0.7 s between calls and it self-disables. It is
                     also THE rung that answers the CJK half of the niche: measured on the live
                     pool 2026-08-22, 9,359 of 9,360 CN attempts (99.99%) returned English full
                     text averaging 23,049 characters of description.
    7. scrapingbee   the same pages through somebody else's IPs. 15 credits a page.
    8. serpapi       paid, and LAST.

NO HIMMPAT RUNG, AND `build()` REFUSES TO MAKE ONE. HimmPat is the only adapter here with
machine-translated English full text for CN/JP/KR/TW, and its ledger is 250 units a day against
about 48 for one deep search. Bulk acquisition against it is not slow, it is arithmetically
impossible: 900,463 families at 250 a day is about 3,600 days, and every unit a bulk job takes is
a unit a live search does not have. The rung is gone, the cap is gone, and `src/realtime_only.py`
refuses the call at the adapter's own HTTP boundary so a bulk path written later cannot reach it
either. It contributed 45 hits to the live pool before this, against serp_self's 16,392.

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
    #  True when the upstream answered, EVEN IF the answer was "this document has no full text".
    #  That distinction is worth real money: see `Provider.upstream` below.
    reached: bool = True

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
    #  Which upstream corpus this rung reads. Rungs that share an upstream see the SAME document:
    #  once one of them has reached it and found no full text, the others will fetch the identical
    #  page and produce the identical nothing, and two of them charge for it. Measured 2026-08-22
    #  before this existed: 1,018 old FR/SE/GB/NL/AT documents that Google Patents serves with no
    #  claims and no description section cost 15,480 ScrapingBee credits and the whole $4.58
    #  SerpApi budget, confirming what the free rung had already established.
    upstream = ""

    def __init__(self):
        self.gate = Gate(self.name, self.concurrency, self.min_interval, self.timeout)

    def available(self):
        return True, ""

    def covers(self, pub: str) -> bool:
        return True

    async def prefetch(self, pubs, client: httpx.AsyncClient) -> None:
        """Optional: warm this rung for a whole leased batch in one round trip.

        A no-op for every rung that is already one call per publication. It exists for the rungs
        whose upstream charges per QUERY rather than per row: a BigQuery lookup bills a 10 MB
        minimum however few keys it carries, so 24 separate point queries cost 24 times what one
        24-key query costs for the identical answer. The worker calls it once per lease batch and
        ignores any failure, because a rung that could not warm itself must still be allowed to
        miss rather than take the batch down with it.
        """
        return None

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
            #  Only siblings that ACTUALLY hold text. Without the EXISTS filter the 20 row
            #  budget is spent on whichever siblings the planner happens to return first, and a
            #  large family whose one texted member is not among them reads as a miss. Both
            #  EXISTS clauses are index driven (ix_claims_pub, ix_para_pub).
            cur.execute(
                "SELECT id, publication_number FROM publications q "
                "WHERE q.simple_family_id=%s AND q.id <> %s "
                "  AND (EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=q.id) "
                "    OR EXISTS (SELECT 1 FROM paragraphs g WHERE g.publication_id=q.id)) "
                "ORDER BY q.id LIMIT %s",
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
# 1. BigQuery patents-public-data, through a cache we cluster ourselves
# ---------------------------------------------------------------------------------------------
_BQ_TAG = None


def _clean_bq_text(text: str) -> str:
    """Undo the markup BigQuery ships inside an abstract string.

    The JPO abstracts arrive as `&lt;P&gt;PROBLEM TO BE SOLVED: ...&lt;P&gt;SOLUTION: ...`, which
    is an escaped `<P>` and not a stray angle bracket: left alone it is indexed as the literal
    tokens `lt` and `gt` on every Japanese abstract in the corpus. Entities are unescaped first,
    then the tags they turn into are replaced by the paragraph break they always meant.
    """
    global _BQ_TAG
    import html
    import re as _re
    if _BQ_TAG is None:
        _BQ_TAG = _re.compile(r"</?[A-Za-z][A-Za-z0-9]{0,7}\s*/?>")
    out = html.unescape(str(text or ""))
    out = _BQ_TAG.sub("\n", out)
    return "\n".join(line.strip() for line in out.split("\n") if line.strip())


class BigQueryCjkProvider(Provider):
    """English title and abstract for CN / JP / KR / TW, at a scale nothing else here approaches.

    WHAT IT HOLDS, AND WHAT IT DOES NOT. `patents-public-data.patents.publications` carries
    `claims_localized` and `description_localized` for the United States and for NOBODY ELSE.
    Censused 2026-08-22 over the whole table: 21,993,541 US descriptions and 18,760,680 US claims
    against exactly ZERO for CN, JP, KR, TW, EP, WO and DE, and the same query against the
    `publications_201710` snapshot returns zero as well, so this is not a regression to work
    around by reading an older release. What it does hold for CJK is Google's own English
    machine-translated ABSTRACT, and it holds it almost everywhere:

        CN  54,729,311 English abstracts of 54,743,394 publications   (100.0%)
        JP  12,770,474 of 28,175,668                                   (45.3%)
        KR   3,227,226 of  8,027,935                                   (40.2%)
        TW   1,682,074 of  2,595,882                                   (64.8%)

    So this rung can never satisfy `complete()` for a CJK publication and is not meant to: it is
    the rung that guarantees a CJK document is at least READABLE and therefore retrievable, in
    English, before the cascade goes on to spend a Google Patents fetch on its full text.

    WHY A CACHE TABLE AND NOT THE PUBLIC ONE. A single publication-number lookup against
    `patents-public-data.patents.publications` dry-runs at 228 GB, because the table is neither
    partitioned nor clustered on the number: $1.43 to read one abstract. `ops/bq_cjk_cache.py`
    builds `nimo-gpt.patents_cache.cjk_text` once, CLUSTERED BY the normalised publication number,
    and a lookup then prunes to the blocks that can contain the keys. MEASURED on the live pool
    2026-08-22: a 24-key batch bills BigQuery's 10 MB per-query minimum ($0.000079, $0.0000033 a
    publication) and a 4,000-key batch bills 864 MB ($0.0000014 a publication). At 24 keys a
    batch the whole 898,377-family CJK job is about $2.94.

    THE FIRST FEW QUERIES AFTER A BUILD ARE NOT THE COST. Immediately after the CTAS the same
    6-key lookup billed 334 MB and then 10.5 MB once the table had settled, which is a 32-fold
    difference and exactly the kind of number that gets a provider rejected on a cold reading.
    Splitting a mixed batch into one query per office is also WORSE, not better: measured, four
    single-office queries bill 4 x 10.5 MB against one mixed query's 10.5 MB, because the floor
    is per query. One query per batch, offices mixed.

    HIT RATE against the real work list, 2026-08-22, a random 4,000 of the pending CJK rows
    (`ORDER BY md5(publication_number)`, not a LIMIT, which on this pool returns one physically
    clustered block and reads 20 points low): 93.2% overall, CN 2,857/2,857 (100.0%),
    JP 542/664 (81.6%), KR 299/433 (69.1%), TW 29/46 (63.0%). The JP, KR and TW shortfall is age,
    not format: that tail is pre-2000 and Google has no English abstract for it either.
    """
    name = "bq_cjk"
    concurrency = int(os.environ.get("FULLTEXT_BQ_CONCURRENCY", "4"))
    timeout = float(os.environ.get("FULLTEXT_BQ_TIMEOUT", "90"))
    #  Billed in MEGABYTES SCANNED, not documents, because megabytes are what BigQuery charges
    #  for and a budget denominated in anything else would not be a budget. The reservation below
    #  is an ESTIMATE per publication; what the ledger records is the megabytes the batch query
    #  actually billed, divided over the keys it carried, so the two can be compared. MEASURED
    #  2026-08-22 on a settled table: 12.6 MB for a 24-key batch (0.53 MB a publication, near the
    #  10 MB per-query floor), 864 MB for 4,000 keys (0.22 MB each), 1,016 MB for 5,000 (0.20 MB
    #  each). The floor dominates small batches, so 8 MB a publication is a deliberately
    #  pessimistic reservation.
    credits = float(os.environ.get("FULLTEXT_BQ_MB_PER_PUB", "8"))
    budget_key = "bq_cjk"
    COUNTRIES = ("CN", "JP", "KR", "TW")
    #  Bounded so a long-running worker cannot grow a cache the size of the pool.
    MAX_CACHED = int(os.environ.get("FULLTEXT_BQ_MAX_CACHED", "20000"))

    def __init__(self, table: str = ""):
        super().__init__()
        self.table = table or os.environ.get("BQ_CJK_TABLE",
                                             "nimo-gpt.patents_cache.cjk_text")
        self.project = os.environ.get("GCP_PROJECT", "nimo-gpt")
        self._cache: dict = {}
        self._cost_mb: dict = {}
        self._client = None
        self._client_err = ""
        self.bytes_billed = 0

    # -- plumbing ------------------------------------------------------------------------------
    @staticmethod
    def _key(pub: str) -> str:
        """The same normalisation the cache table was built with: strip punctuation, upper-case.
        `CN-101234567-A` and `CN101234567A` are one key, which is the whole point: BigQuery
        publishes the hyphenated form and the pool holds the compact one."""
        return "".join(ch for ch in str(pub or "") if ch.isalnum()).upper()

    def _bq(self):
        if self._client is None and not self._client_err:
            try:
                from google.cloud import bigquery
                self._client = bigquery.Client(project=self.project)
            except Exception as exc:  # pragma: no cover - exercised only without the SDK
                self._client_err = f"{type(exc).__name__}: {str(exc)[:160]}"
        return self._client

    def available(self):
        if os.environ.get("FULLTEXT_BQ_DISABLED", "").lower() in ("1", "true", "yes"):
            return False, "FULLTEXT_BQ_DISABLED is set"
        if self._bq() is None:
            return False, (self._client_err or
                           "no BigQuery client: google-cloud-bigquery is not importable")
        return True, ""

    def covers(self, pub: str) -> bool:
        return _country(pub) in self.COUNTRIES

    # -- the batch round trip ------------------------------------------------------------------
    def _query(self, keys: list) -> dict:
        from google.cloud import bigquery
        cli = self._bq()
        if cli is None:
            return {}
        sql = (f"SELECT pn_key, publication_number, country, title_en, abstract_en, src_lang "
               f"FROM `{self.table}` WHERE pn_key IN UNNEST(@k)")
        job = cli.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("k", "STRING", keys)]))
        rows = list(job.result())
        billed = int(job.total_bytes_billed or 0)
        self.bytes_billed += billed
        return billed, {r["pn_key"]: {"publication_number": r["publication_number"],
                                      "country": r["country"], "title": r["title_en"] or "",
                                      "abstract": r["abstract_en"] or "",
                                      "src_lang": r["src_lang"] or ""} for r in rows}

    async def prefetch(self, pubs, client: httpx.AsyncClient) -> None:
        """One query for the whole leased batch. 24 point queries would cost 24 minimums."""
        want = [self._key(p) for p in pubs if self.covers(p)]
        want = [k for k in dict.fromkeys(want) if k and k not in self._cache]
        if not want or self._bq() is None:
            return
        if len(self._cache) > self.MAX_CACHED:
            self._cache.clear()
            self._cost_mb.clear()
        billed, found = await asyncio.to_thread(self._query, want)
        per_key = billed / 1e6 / max(1, len(want))
        for k in want:
            #  A key that is absent is cached as a MISS, so a retry of the same batch does not
            #  buy the same nothing twice.
            self._cache[k] = found.get(k)
            self._cost_mb[k] = per_key

    async def fetch(self, pub: str, client: httpx.AsyncClient):
        key = self._key(pub)
        if key not in self._cache:
            await self.prefetch([pub], client)
        row = self._cache.get(key)
        cost = float(self._cost_mb.get(key, 0.0))
        if not row:
            #  REACHED. BigQuery answered "this publication has no English abstract here", which
            #  is an answer, not an outage.
            return FetchResult(provider=self.name, reached=True, credits=cost)
        return FetchResult(
            provider=self.name, title=_clean_bq_text(row["title"]),
            abstract=_clean_bq_text(row["abstract"]), claims_lang="", desc_lang="",
            meta={"bq_table": self.table, "source_language": row["src_lang"],
                  "text_is_machine_translation": True},
            raw=json.dumps(row, ensure_ascii=False).encode("utf-8"), raw_ext="json",
            credits=cost)


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
        """Two layouts, both direct lookups: flat and one directory per office.

        Deliberately NOT a recursive glob. `glob(root/**/PUB.xml, recursive=True)` walks the whole
        mirror once per publication, which on a bulk corpus is a directory tree walk per document
        and would be slower than fetching the page over the network. A mirror in a third layout
        should be symlinked or re-laid-out, not searched.
        """
        root = self.root
        out = []
        for ext in ("xml", "XML", "xml.gz"):
            out.append(os.path.join(root, f"{pub}.{ext}"))
            out.append(os.path.join(root, _country(pub), f"{pub}.{ext}"))
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
    upstream = "google_patents"
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
        #  404 is Google ANSWERING: it does not hold this publication, and neither will the two
        #  paid rungs that resell the same index. Anything else non-200 is a refusal, not an
        #  answer, and the paid rungs are exactly the fallback that outage is meant to have.
        if r.status_code == 404:
            return FetchResult(provider=self.name, reached=True)
        if r.status_code != 200:
            return FetchResult(provider=self.name, reached=False)
        rec = G.parse_document(r.text, pub)
        if not rec:
            #  Reached, and the page carries no full text. That is Google's answer too.
            return FetchResult(provider=self.name, reached=True)
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
    upstream = "google_patents"
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
        if r.status_code == 404:
            return FetchResult(provider=self.name, reached=True, credits=self.credits)
        if r.status_code != 200:
            return FetchResult(provider=self.name, reached=False, credits=self.credits)
        from sources.gpatents_direct import parse_document
        rec = parse_document(r.text, pub)
        if not rec:
            return FetchResult(provider=self.name, reached=True, credits=self.credits)
        out = _from_gpatents(self.name, rec, r.text)
        out.credits = self.credits
        return out


# ---------------------------------------------------------------------------------------------
# 7. SerpApi: paid, capped, last
# ---------------------------------------------------------------------------------------------
class SerpApiProvider(Provider):
    """The backstop that makes every rung above it optional rather than load-bearing, and the
    only one that spends real money per document. $0.0092 a document on the Big Data plan;
    30,000 searches a month, 6,000 an hour, and 12,501 left when this was written. Every credit
    is reserved through `ledger.reserve` before the call, so four workers share one cap rather
    than holding four copies of it."""
    name = "serpapi"
    upstream = "google_patents"
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
            return FetchResult(provider=self.name, reached=True, credits=self.credits,
                               usd=self.usd)
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
DEFAULT_ORDER = ["corpus", "bq_cjk", "marec", "uspto_bulk", "epo_ops", "pqai", "serp_self",
                 "scrapingbee", "serpapi"]

#  Providers this module refuses to build at all, whatever an operator puts in FULLTEXT_CASCADE,
#  with the reason a caller sees. Removing a name from DEFAULT_ORDER only changes a default;
#  `FULLTEXT_CASCADE=corpus,himmpat` would have put the rung straight back, which is the exact
#  shape of accident this is here to make impossible. `src/realtime_only.py` refuses the call at
#  the adapter's HTTP boundary as well, so this is the outer of two independent doors.
BARRED = {
    "himmpat": ("HimmPat is reserved for real-time use during a search: 250 units a day against "
                "about 48 for one deep search. A bulk cascade may not have a rung on it. See "
                "src/realtime_only.py and docs/cjk_acquisition.md"),
}

#  Per-provider hard caps for one calendar month, enforced by ledger.reserve(). Names are the
#  provider names; units are that provider's own credits. `bq_cjk` is denominated in megabytes
#  scanned, which is what BigQuery bills: 2,000,000 MB is 2 TB, $12.50 at $6.25/TB, and the
#  first 1 TB of every calendar month is free. At the measured 0.42 MB a publication that is
#  the whole 898,377-family CJK job several times over.
DEFAULT_CAPS = {
    "serpapi": float(os.environ.get("FULLTEXT_SERPAPI_BUDGET", "2000")),
    "scrapingbee": float(os.environ.get("FULLTEXT_SCRAPINGBEE_BUDGET", "300000")),
    "bq_cjk": float(os.environ.get("FULLTEXT_BQ_BUDGET", "2000000")),
}


def build(order=None) -> list:
    """The cascade, in order. `FULLTEXT_CASCADE` overrides it (comma separated provider names),
    which is how an operator turns off a rung without a deploy."""
    names = order or [n for n in os.environ.get("FULLTEXT_CASCADE", "").split(",") if n.strip()]
    names = names or DEFAULT_ORDER
    made = []
    for n in names:
        n = n.strip()
        if n in BARRED:
            raise ValueError(f"{n} may not be a rung of a bulk cascade: {BARRED[n]}")
        if n == "corpus":
            made.append(CorpusProvider())
        elif n == "bq_cjk":
            made.append(BigQueryCjkProvider())
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
        elif n == "serpapi":
            made.append(SerpApiProvider())
        else:
            raise ValueError(f"unknown provider {n!r} in the cascade")
    return made
