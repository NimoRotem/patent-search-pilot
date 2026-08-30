"""USPTO Open Data Portal (ODP) adapter — authoritative US application/patent data.

api.uspto.gov, header X-API-KEY. The search endpoint queries the patent file-wrapper
(applicationMetaData: title, filing/grant dates, patent/publication numbers, status)
and — for a known application — exposes prosecution EVENTS and office-action DOCUMENTS.
For prior-art we use it as a title-recall channel over US filings; it also powers a
US legal-status / office-action detail capability.

Key: USPTO_ODP_KEY (alias ODP_API_KEY). Free from data.uspto.gov/myodp.

NOTE ON THE PREDECESSOR: this adapter previously spoke to PatentsView
(search.patentsview.org), whose host is now NXDOMAIN. Query contracts must
describe the ODP contract (2-3 plain title keywords), NOT PatentsView field
syntax — see _looks_like_legacy_syntax() below, which turns that mismatch into a
loud source_error instead of a silently-empty result set.

Ported from patents-app `adapters/uspto.py` unchanged in behavior.
"""
from __future__ import annotations

import os
import re

import httpx

from .schema import Candidate, SubQuery, normalize_pub, canonical_pub, best_source_url, iso_date
from .base import Adapter, cached_json

BASE = "https://api.uspto.gov/api/v1/patent/applications"

# Ordinary English stopwords, plus — critically — the PatentsView/USPTO legacy field
# prefixes (ABST, TTL, ACLN, ...). Those are not content words: if one survives into
# _terms() it becomes the first AND-term of a title query and guarantees zero hits.
_FIELD_PREFIXES = {
    "abst", "ttl", "aclm", "acln", "spec", "aanm", "aaci", "aast", "aaco",
    "innm", "inci", "inst", "inco", "asnm", "asci", "asst", "asco",
    "ccls", "ccor", "cpc", "cpcl", "ipc", "icl", "kd", "pd", "apd", "isd",
    "pn", "an", "din", "ref", "lrep", "govt", "pta", "exp", "text",
}
_STOP = set("a an the of and or for to in on with without by from as at is are be this that "
            "using use method system apparatus device having comprising said wherein which "
            "these those means configured able about into via per not no more most any one".split()
            ) | _FIELD_PREFIXES

# Markers of the retired PatentsView contract leaking in from the planner.
_LEGACY_RE = re.compile(
    r"(?i)\b(?:ABST|TTL|ACLM|ACLN|SPEC|AANM|ASNM|INNM|CCLS|ISD|APD|PN|AN)\s*/"
    r"|_text_any|_text_all|_text_phrase|patent_abstract|patent_title"
)


def _looks_like_legacy_syntax(raw: str) -> bool:
    """True if the planner emitted PatentsView field syntax into this ODP adapter."""
    return bool(_LEGACY_RE.search(raw or ""))


def _terms(text, k: int = 5) -> list:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", (text or "").lower())
    seen, out = set(), []
    for w in words:
        if w in _STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:k]


def _pub_from_meta(md: dict, app: str) -> str:
    pn = md.get("earliestPublicationNumber") or ""
    if pn:
        return normalize_pub(pn if pn.upper().startswith("US") else f"US{pn}")
    pat = md.get("patentNumber") or ""
    if pat:
        return normalize_pub(f"US{pat}")
    return normalize_pub(f"US{app}") if app else ""


class USPTO(Adapter):
    name = "uspto"

    def __init__(self):
        self.key = os.environ.get("USPTO_ODP_KEY", "") or os.environ.get("ODP_API_KEY", "")
        # Non-fatal contract violations, drained by the facade into the errors
        # list. This is what stops the adapter going dark unnoticed.
        self._warnings: list[str] = []

    def _warn(self, msg: str) -> None:
        # This adapter is a process-wide singleton, so the buffer is bounded: if
        # nothing ever drains it (adapter used outside the facade) it must not grow.
        if len(self._warnings) < 16 and msg not in self._warnings:
            self._warnings.append(msg)

    def pop_warnings(self) -> list[str]:
        w, self._warnings = self._warnings, []
        return w

    def enabled(self) -> bool:
        return bool(self.key)

    def disabled_reason(self) -> str:
        return "USPTO_ODP_KEY not set (free key from data.uspto.gov/myodp)"

    def search_note(self) -> str:
        return "US filings (title recall + prosecution/office-actions)"

    async def search(self, sq: SubQuery, client: httpx.AsyncClient) -> list[Candidate]:
        if not self.key:
            return []
        raw = str(sq.native)

        # FAIL LOUDLY, NOT DARKLY. Legacy PatentsView syntax is a planner-contract bug.
        # _STOP now strips the field prefixes, so we can still recover usable keywords —
        # recovering beats discarding the round's US candidates — but we refuse to do it
        # silently: the warning is drained into the caller's errors list.
        # Silence is exactly how this adapter stayed dark on every search in V2.
        if _looks_like_legacy_syntax(raw):
            self._warn(
                f"planner emitted retired PatentsView field syntax ({raw[:60]!r}); "
                "recovered plain keywords, but the uspto query contract has regressed"
            )

        # ODP searches inventionTitle ONLY — titles are short, so an AND of the 2 most
        # salient terms is the recall sweet spot (2 terms ~hundreds of hits; 3+ -> ~0).
        terms = _terms(raw, 2)
        if not terms:
            # Unrecoverable: nothing but stopwords/field prefixes. Raise so the executor
            # emits source_error rather than returning a plausible-looking empty list.
            raise ValueError(
                f"no usable title keywords in query {raw[:60]!r} "
                "(all tokens were stopwords or field prefixes)"
            )
        q = f"applicationMetaData.inventionTitle:({' AND '.join(terms)})"
        params = {"q": q, "limit": "25", "sort": "applicationMetaData.filingDate desc"}
        # Let transport/HTTP errors propagate: the executor catches them and emits a
        # source_error event, so a broken key or a 5xx is visible rather than silent.
        data = await cached_json(
            client, "GET", f"{BASE}/search",
            cache_key=f"odp:{sq.cache_key()}",
            params=params, headers={"X-API-KEY": self.key}, ttl=3600,
        )
        out: list[Candidate] = []
        for i, w in enumerate(data.get("patentFileWrapperDataBag", []) or []):
            md = w.get("applicationMetaData", {}) or {}
            app = w.get("applicationNumberText", "") or ""
            pub = _pub_from_meta(md, app)
            if not pub:
                continue
            out.append(Candidate(
                pub_number=canonical_pub(pub),
                source=self.name,
                source_rank=i + 1,
                title=md.get("inventionTitle", "") or "",
                assignee=(md.get("firstApplicantName") or ""),
                date=iso_date(md.get("grantDate") or md.get("filingDate")),
                priority_date=iso_date(md.get("effectiveFilingDate") or md.get("filingDate")),
                url=best_source_url(pub),
                found_by=[sq.rationale or sq.element],
                extra={"application": app,
                       "status": md.get("applicationStatusDescriptionText", "")},
            ))
        return out

    async def details(self, pub_number: str, client: httpx.AsyncClient) -> dict:
        """US application status + prosecution events for a known application number."""
        if not self.key:
            return {}
        app = re.sub(r"\D", "", pub_number)
        if not app:
            return {}
        try:
            data = await cached_json(
                client, "GET", f"{BASE}/{app}",
                cache_key=f"odp:app:{app}",
                headers={"X-API-KEY": self.key}, ttl=86400,
            )
        except Exception:
            # Detail enrichment is best-effort and runs per-card: a miss here must not
            # break the caller. (Unlike search(), this is not a contract bug.)
            return {}
        w = (data.get("patentFileWrapperDataBag", [{}]) or [{}])[0]
        md = w.get("applicationMetaData", {}) or {}
        events = [{"date": e.get("eventDate"), "desc": e.get("eventDescriptionText")}
                  for e in (w.get("eventDataBag") or [])][:40]
        return {"status": md.get("applicationStatusDescriptionText", ""),
                "filing_date": md.get("filingDate", ""), "events": events}
