"""EPO OPS adapter — authoritative worldwide biblio + INPADOC family + full text.

OAuth2 client-credentials (consumer key/secret from the EPO OPS portal).
Query language is CQL. Free tier 4 GB/week.

Ported from patents-app `adapters/epo_ops.py`. Port change: credentials read
from EPO_OPS_KEY / EPO_OPS_SECRET (App A's supervisor conf) with
OPS_CONSUMER_KEY / OPS_CONSUMER_SECRET accepted as aliases (the pilot's .env
spelling), so the same keys work on both sides unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import time

import httpx

from .schema import Candidate, SubQuery, normalize_pub, iso_date
from .base import Adapter, cached_json


AUTH = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"
FAMILY = "https://ops.epo.org/3.2/rest-services/family/publication/docdb/{pub}/biblio"
FULLTEXT = "https://ops.epo.org/3.2/rest-services/published-data/publication/epodoc/{pub}/{part}"
# Verified live 2026-08-14: OPS serves full text for EP and WO only. US, DE, CN and
# JP all answer 404 on /claims and /description, so asking for them is a wasted
# round trip against a quota that EP/WO genuinely need.
FULLTEXT_COUNTRIES = ("EP", "WO")


def _flatten_text(node, out=None) -> str:
    """Pull every text node out of an OPS JSON tree, in order.

    OPS renders XML to JSON, so the full-text payload is a nest of dicts and lists
    whose leaves are {"$": "the actual sentence"}. The exact nesting differs between
    claims and description and between offices, so walking for "$" is more robust
    than indexing a path that holds for one document and KeyErrors on the next.
    """
    top = out is None
    out = [] if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$" and isinstance(v, str):
                out.append(v)
            elif not k.startswith("@"):
                _flatten_text(v, out)
    elif isinstance(node, list):
        for v in node:
            _flatten_text(v, out)
    if not top:
        return ""
    lines = [ln.strip() for ln in out if isinstance(ln, str) and ln.strip()]
    return "\n".join(lines)


# --- OPS JSON shape helpers -------------------------------------------------
# OPS renders XML into JSON: text nodes become {"$": "value"}, attributes become
# "@attr", and ANY repeated element is a list while a single one stays a dict.
# Every accessor below must therefore tolerate dict-or-list.
def _txt(node) -> str:
    """Value of an OPS text node, taking the first if it is a list."""
    if isinstance(node, list):
        node = node[0] if node else None
    if isinstance(node, dict):
        return str(node.get("$", "") or "")
    return str(node or "")


def _as_list(node) -> list:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _doc_ids(pubref) -> list:
    return _as_list((pubref or {}).get("document-id"))


def _pub_from_docids(docids: list) -> tuple:
    """(pub_number, kind, date) preferring the docdb id (country+number+kind)."""
    for d in docids:
        if d.get("@document-id-type") == "docdb":
            cc = _txt(d.get("country"))
            num = _txt(d.get("doc-number"))
            kind = _txt(d.get("kind"))
            if cc and num:
                return f"{cc}{num}{kind}", kind, _txt(d.get("date"))
    for d in docids:  # epodoc fallback: doc-number already carries the country
        num = _txt(d.get("doc-number"))
        if num:
            return num, _txt(d.get("kind")), _txt(d.get("date"))
    return "", "", ""


def _applicant(biblio: dict) -> str:
    apps = _as_list(((biblio.get("parties") or {}).get("applicants") or {}).get("applicant"))
    # Prefer the normalized 'epodoc' form ("KROHNE AG [CH]") over the raw original.
    for want in ("epodoc", "original"):
        for a in apps:
            if a.get("@data-format") == want:
                n = _txt(((a.get("applicant-name") or {}).get("name")))
                if n:
                    return n
    return ""


def _cpc(biblio: dict) -> list:
    out: list[str] = []
    for c in _as_list((biblio.get("patent-classifications") or {}).get("patent-classification")):
        sec, cls = _txt(c.get("section")), _txt(c.get("class"))
        sub, grp = _txt(c.get("subclass")), _txt(c.get("main-group"))
        sg = _txt(c.get("subgroup"))
        if sec and cls and sub:
            out.append(f"{sec}{cls}{sub}{grp}/{sg}" if grp and sg else f"{sec}{cls}{sub}")
    return list(dict.fromkeys(out))[:8]


class EPO_OPS(Adapter):
    name = "epo_ops"

    def __init__(self):
        self.key = (os.environ.get("EPO_OPS_KEY", "")
                    or os.environ.get("OPS_CONSUMER_KEY", ""))
        self.secret = (os.environ.get("EPO_OPS_SECRET", "")
                       or os.environ.get("OPS_CONSUMER_SECRET", ""))
        self._token = ""
        self._token_exp = 0.0

    def enabled(self) -> bool:
        return bool(self.key and self.secret)

    def disabled_reason(self) -> str:
        return ("EPO_OPS_KEY / EPO_OPS_SECRET (or OPS_CONSUMER_KEY / OPS_CONSUMER_SECRET) "
                "not set (free OPS registration)")

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and self._token_exp > time.monotonic() + 30:
            return self._token
        cred = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
        r = await client.post(
            AUTH,
            headers={"Authorization": f"Basic {cred}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            content="grant_type=client_credentials",
        )
        r.raise_for_status()
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = time.monotonic() + int(j.get("expires_in", 1200))
        return self._token

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        try:
            token = await self._get_token(client)
        except Exception:
            return []
        # native should be CQL; wrap a bare string into a txt= CQL query
        cql = str(sq.native)
        if "=" not in cql:
            cql = f'txt="{cql}"'
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        # Let HTTP/quota errors propagate: the executor turns them into a source_error
        # event. OPS rate-limits and bills hard, so a 403 "quota exceeded" is something
        # the operator must SEE — swallowing it renders identically to "no results".
        # The ONE exception is 404, which is how OPS reports a genuinely empty result
        # set; that is a real "no matches" and must stay quiet.
        try:
            data = await cached_json(
                client, "GET", SEARCH,
                cache_key=f"ops:{sq.cache_key()}",
                params={"q": cql, "Range": "1-20"}, headers=headers, ttl=3600,
            )
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return []
            raise
        # The /search/biblio endpoint returns 'exchange-documents' (full bibliographic
        # records). This parser previously looked for 'ops:publication-reference', which
        # is what the PLAIN /search endpoint returns — so every response parsed to zero
        # and the adapter reported "no matches" for queries with thousands of hits. It
        # had never been exercised against a live key (OPS was only approved 2026-07-18).
        # Reading biblio is strictly better: we get title, applicant, date and CPC here
        # instead of a bare publication number.
        result = ((data.get("ops:world-patent-data") or {}).get("ops:biblio-search") or {}
                  ).get("ops:search-result") or {}
        entries = _as_list(result.get("exchange-documents"))
        if not entries and result.get("ops:publication-reference") is not None:
            # Defensive: tolerate the plain-/search shape too, so a future endpoint
            # switch degrades instead of going dark again.
            return self._parse_publication_refs(result, sq)
        if not entries:
            total = _txt(((data.get("ops:world-patent-data") or {}).get("ops:biblio-search") or {})
                         .get("@total-result-count")) or "0"
            if total not in ("", "0"):
                # OPS says there ARE hits but we parsed none -> shape drift, not "no
                # matches". Raise so the caller emits source_error rather than lying.
                raise ValueError(
                    f"OPS reported {total} hits but no exchange-documents parsed "
                    "— response shape changed")
            return []

        out: list[Candidate] = []
        for i, e in enumerate(entries):
            for doc in _as_list(e.get("exchange-document") if isinstance(e, dict) else None) or [e]:
                if not isinstance(doc, dict):
                    continue
                biblio = doc.get("bibliographic-data") or {}
                pub, kind, date = _pub_from_docids(_doc_ids(biblio.get("publication-reference")))
                if not pub:
                    # attributes on the element itself are the other place OPS puts it
                    cc, num = doc.get("@country", ""), doc.get("@doc-number", "")
                    kind = doc.get("@kind", "") or kind
                    pub = f"{cc}{num}{kind}" if cc and num else ""
                if not pub:
                    continue
                title = ""
                for t in _as_list(biblio.get("invention-title")):
                    if isinstance(t, dict) and t.get("@lang") == "en":
                        title = _txt(t)
                        break
                if not title:
                    title = _txt(biblio.get("invention-title"))
                if date and len(date) == 8:
                    date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
                out.append(Candidate(
                    pub_number=normalize_pub(pub),
                    source=self.name,
                    source_rank=i + 1,
                    kind=kind,
                    title=title,
                    assignee=_applicant(biblio),
                    date=iso_date(date),
                    cpc=_cpc(biblio),
                    family_id=str(doc.get("@family-id", "") or ""),
                    url=f"https://worldwide.espacenet.com/patent/search?q={pub}",
                    found_by=[sq.rationale or sq.element],
                ))
        return out

    def _parse_publication_refs(self, result: dict, sq: SubQuery) -> list[Candidate]:
        """Fallback parser for the plain /published-data/search response shape."""
        out: list[Candidate] = []
        for i, d in enumerate(_as_list(result.get("ops:publication-reference"))):
            pub, kind, _ = _pub_from_docids(_as_list((d or {}).get("document-id")))
            if not pub:
                continue
            out.append(Candidate(
                pub_number=normalize_pub(pub),
                source=self.name,
                source_rank=i + 1,
                kind=kind,
                family_id=str((d or {}).get("@family-id", "") or ""),
                url=f"https://worldwide.espacenet.com/patent/search?q={pub}",
                found_by=[sq.rationale or sq.element],
            ))
        return out

    async def fulltext(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        """Claims + description for EP/WO, free within the 4 GB/week tier.

        A full-text response is ~20 KB, so the weekly allowance is on the order of
        200,000 documents — effectively unlimited at our volume, and unlike scraping
        it is a published quota that will not blacklist the box.
        """
        pub = normalize_pub(pub_number)
        if not pub or pub[:2].upper() not in FULLTEXT_COUNTRIES:
            return {}
        # OPS wants the number without its kind code on this route.
        epodoc = re.sub(r"([A-Z]{2}\d+)[A-Z]\d?$", r"\1", pub)
        try:
            token = await self._get_token(client)
        except Exception:
            return {}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        async def part(name: str) -> str:
            try:
                data = await cached_json(
                    client, "GET", FULLTEXT.format(pub=epodoc, part=name),
                    cache_key=f"ops:{name}:{epodoc}", headers=headers, ttl=604800)
            except Exception:
                return ""
            return _flatten_text(data)

        claims, desc = await asyncio.gather(part("claims"), part("description"))
        out: dict = {}
        if claims:
            out["claims"] = claims[:200000]
            out["claims_lang"] = "en"
        if desc:
            out["description"] = desc[:400000]
            out["desc_lang"] = "en"
        return out

    async def family(self, pub_number: str, client: httpx.AsyncClient) -> list[str]:
        try:
            token = await self._get_token(client)
        except Exception:
            return []
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        pub = normalize_pub(pub_number)
        try:
            data = await cached_json(
                client, "GET", FAMILY.format(pub=pub),
                cache_key=f"ops:fam:{pub}", headers=headers, ttl=86400,
            )
        except Exception:
            return []
        members: list[str] = []
        try:
            fam = data["ops:world-patent-data"]["ops:patent-family"]["ops:family-member"]
            if isinstance(fam, dict):
                fam = [fam]
            for m in fam:
                doc = m["publication-reference"]["document-id"]
                if isinstance(doc, list):
                    doc = doc[0]
                cc = doc.get("country", {}).get("$", "")
                num = doc.get("doc-number", {}).get("$", "")
                members.append(normalize_pub(f"{cc}{num}"))
        except Exception:
            pass
        return members
