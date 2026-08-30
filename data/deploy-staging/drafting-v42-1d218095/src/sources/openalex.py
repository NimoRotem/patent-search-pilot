"""OpenAlex adapter — non-patent prior art (papers, dated disclosures).

Free, generous, no key. Provides the NPL channel that patent-only sources
miss. Dates matter for novelty, so we surface publication_date.

Ported from patents-app `adapters/openalex.py` unchanged in behavior.
"""
from __future__ import annotations

import datetime
import os

import httpx

from .schema import Candidate, SubQuery
from .base import Adapter, cached_json


OA = "https://api.openalex.org/works"

# OpenAlex now enforces a DAILY budget (429 "Insufficient budget … Resets at
# midnight UTC"). When hit, skip search for the rest of the UTC day (no repeated
# 429 probing); it auto-clears at the next UTC midnight since we key on the date.
_EXHAUSTED_UTC_DATE = ""


def _utc_today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _budget_blocked() -> bool:
    return _EXHAUSTED_UTC_DATE == _utc_today()


def _mark_budget_exhausted() -> None:
    global _EXHAUSTED_UTC_DATE
    _EXHAUSTED_UTC_DATE = _utc_today()


class OpenAlex(Adapter):
    name = "openalex"

    def __init__(self):
        self.mailto = os.environ.get("OPENALEX_MAILTO", "nimo@rotem.ai")

    def search_available(self) -> bool:
        return not _budget_blocked()

    def search_note(self) -> str:
        return "daily budget used — resets midnight UTC" if _budget_blocked() else ""

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        if _budget_blocked():
            return []
        params = {"search": str(sq.native), "per_page": "15", "mailto": self.mailto}
        filt = []
        if sq.date_to:
            filt.append(f"to_publication_date:{sq.date_to}")
        if sq.date_from:
            filt.append(f"from_publication_date:{sq.date_from}")
        if filt:
            params["filter"] = ",".join(filt)
        try:
            data = await cached_json(
                client, "GET", OA,
                cache_key=f"oa:{sq.cache_key()}:{params.get('filter','')}",
                params=params, ttl=3600,
            )
        except Exception as e:
            resp = getattr(e, "response", None)
            if resp is not None and getattr(resp, "status_code", None) == 429:
                _mark_budget_exhausted()
            return []
        out: list[Candidate] = []
        # OpenAlex freely returns null for present keys (author, primary_location,
        # source, id, ...), so `.get(k, {})` still yields None -> guard with `x or {}`
        # everywhere, and isolate each record so one malformed work can't drop the batch.
        for i, w in enumerate(data.get("results") or []):
            if not isinstance(w, dict):
                continue
            try:
                authors = [((a or {}).get("author") or {}).get("display_name", "")
                           for a in (w.get("authorships") or [])][:6]
                abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
                doi = w.get("doi") or ""
                oa_id = (w.get("id") or "").split("/")[-1]
                src = ((w.get("primary_location") or {}).get("source") or {})
                out.append(Candidate(
                    pub_number=(oa_id or doi),
                    source=self.name,
                    source_rank=i + 1,
                    kind="NPL",
                    title=w.get("title") or w.get("display_name") or "",
                    abstract=abstract,
                    snippet=abstract[:300],
                    assignee=src.get("display_name", "") or "",
                    inventors=[a for a in authors if a],
                    date=w.get("publication_date") or str(w.get("publication_year") or ""),
                    url=doi or (w.get("id") or ""),
                    found_by=[sq.rationale or sq.element],
                    family_id=oa_id,  # NPL: each work is its own family
                    extra={"cited_by_count": w.get("cited_by_count") or 0,
                           "type": w.get("type") or ""},
                ))
            except Exception:
                continue
        return out


def _reconstruct_abstract(inv) -> str:
    if not inv:
        return ""
    positions: list = []
    for word, idxs in inv.items():
        for p in idxs:
            positions.append((p, word))
    positions.sort()
    return " ".join(w for _, w in positions)[:1500]
