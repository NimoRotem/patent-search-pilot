"""Resolve a cited publication to something that actually exists, and say where it was found.

A drafted Background that cites ``US-9,123,456`` reads exactly the same whether that publication
exists or not, which is why "the citations are real" cannot be left to the model that wrote them.
Every citation in a draft is checked here against, in order:

  1. the local corpus (millions of publications with title, dates, claims and description) —
     authoritative and free;
  2. the display-enrichment cache, which already holds the records the search rendered;
  3. the enrichment path itself (lemad Mongo, then a metered external lookup), used only when the
     first two miss, and skipped entirely when ``allow_remote`` is false.

A miss is reported as a MISS with a reason, never smoothed over.  The distinction that matters to
a reader is "this publication does not exist" versus "this publication exists and we hold no text
for it": the first is a fabricated citation and the second is a coverage gap, and only the first is
a defect in the draft.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Mapping

import pubnorm

_CACHE: dict[tuple[str, bool], tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 6 * 3600
_CACHE_MAX = 4000

# `[REF:US-9108319-B2]` is the drafting citation token.  Kept deliberately narrow: a country code,
# then digits and separators, then an optional kind code.  Anything else is a malformed token and
# is reported as such rather than being silently normalised into a plausible-looking number.
#  The lookahead is what stops "[REF:the Smith patent]" reading as a valid publication: two
#  letters and some word characters describe most English phrases, so the token must also contain
#  a DIGIT.  Without it a fabricated citation validated cleanly and was never reported.
CITATION_RE = re.compile(
    r"\[REF:\s*([A-Za-z]{2}(?=[A-Za-z0-9./-]*\d)[A-Za-z0-9./-]{2,40})\s*\]")
LOOSE_TOKEN_RE = re.compile(r"\[REF:([^\]]{0,80})\]")

# Bare publication numbers written into prose, e.g. "US 9,108,319 B2" or "EP 3 707 092 B1".
# Used only to WARN that a reference was named without a citation token, so it is allowed to be
# generous: a false positive costs one advisory line, a false negative hides an uncited reference.
BARE_PUB_RE = re.compile(
    r"\b([A-Z]{2})[\s ]?((?:\d[\d,\s ]{4,14}\d))[\s ]?([A-Z]\d?)?\b")


def _cached(key: tuple[str, bool]) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return dict(hit[1])
        if hit:
            _CACHE.pop(key, None)
    return None


def _store(key: tuple[str, bool], value: Mapping[str, Any]) -> dict[str, Any]:
    with _CACHE_LOCK:
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (time.time(), dict(value))
    return dict(value)


def normalize(publication: Any) -> str:
    """Our canonical hyphenated key, or ''."""
    return pubnorm.canonical(publication) or ""


def resolve(publication: Any, *, with_text: bool = False,
            allow_remote: bool = True) -> dict[str, Any]:
    """Look one publication up.  Never raises: a failure is a MISS with a reason."""
    canonical = normalize(publication)
    if not canonical:
        return {"found": False, "publication_number": str(publication or "")[:64],
                "reason": "not a recognisable publication number", "source": ""}
    key = (canonical, bool(with_text))
    hit = _cached(key)
    if hit is not None:
        return hit

    record = _from_corpus(canonical, with_text=with_text)
    if not record.get("found"):
        cached_display = _from_display_cache(canonical)
        if cached_display.get("found"):
            record = cached_display
    if not record.get("found") and allow_remote:
        record = _from_enrichment(canonical) or record
    if not record.get("found"):
        record.setdefault("reason", "not present in the corpus or any reachable source")
    record.setdefault("publication_number", canonical)
    record["url"] = record.get("url") or pubnorm.google_url(canonical) or ""
    #  A miss is not cached for as long as a hit: the enrichment path can start answering for a
    #  publication that was previously unreachable, and a stale "does not exist" is the one wrong
    #  answer here that changes what the user does about it.
    if record.get("found"):
        return _store(key, record)
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() - _CACHE_TTL + 900, dict(record))
    return record


def _from_corpus(canonical: str, *, with_text: bool) -> dict[str, Any]:
    try:
        import db
    except Exception:                                          # noqa: BLE001 - no DB in unit tests
        return {"found": False, "reason": "corpus unavailable", "source": ""}
    candidates = [canonical] + [v for v in pubnorm.variants(canonical) if v != canonical]
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, publication_number, kind_code, country, title, abstract, "
                "publication_date, filing_date, earliest_priority_date, simple_family_id "
                "FROM publications WHERE publication_number = ANY(%s) "
                "ORDER BY publication_date DESC NULLS LAST LIMIT 1", (candidates,))
            row = cur.fetchone()
            if not row:
                return {"found": False, "reason": "not in the local corpus", "source": ""}
            out = {
                "found": True, "source": "corpus",
                "publication_number": row["publication_number"],
                "title": (row.get("title") or "").strip(),
                "abstract": (row.get("abstract") or "").strip(),
                "publication_date": _date(row.get("publication_date")),
                "filing_date": _date(row.get("filing_date")),
                "priority_date": _date(row.get("earliest_priority_date")),
                "country": row.get("country") or "",
                "kind_code": row.get("kind_code") or "",
                "family_id": row.get("simple_family_id") or "",
            }
            if with_text:
                cur.execute("SELECT claim_no, text FROM claims WHERE publication_id=%s "
                            "ORDER BY claim_no NULLS LAST, id LIMIT 200", (row["id"],))
                claims = [f"{r['claim_no']}. {(r['text'] or '').strip()}"
                          if r.get("claim_no") else (r["text"] or "").strip()
                          for r in cur.fetchall() if (r.get("text") or "").strip()]
                out["claims"] = "\n\n".join(claims)
                cur.execute("SELECT text FROM paragraphs WHERE publication_id=%s "
                            "ORDER BY id LIMIT 400", (row["id"],))
                out["description"] = "\n\n".join(
                    (r["text"] or "").strip() for r in cur.fetchall() if (r.get("text") or "").strip())
            return out
    except Exception as exc:                                   # noqa: BLE001 - fail soft, say why
        return {"found": False, "source": "",
                "reason": f"corpus lookup failed ({type(exc).__name__})"}


def _from_display_cache(canonical: str) -> dict[str, Any]:
    try:
        import enrich_display
        cached = enrich_display.load_cached(canonical)
    except Exception:                                          # noqa: BLE001
        return {"found": False}
    return _from_display_record(canonical, cached, "display-cache")


def _from_enrichment(canonical: str) -> dict[str, Any] | None:
    try:
        import enrich_display
        record = enrich_display.enrich_for_display(canonical)
    except Exception:                                          # noqa: BLE001
        return None
    out = _from_display_record(canonical, record, "enrichment")
    return out if out.get("found") else None


def _from_display_record(canonical: str, record: Any, source: str) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"found": False}
    biblio = record.get("biblio") if isinstance(record.get("biblio"), Mapping) else record
    title = str(biblio.get("title") or record.get("title") or "").strip()
    if not title and not record.get("abstract"):
        return {"found": False}
    claims = record.get("claims")
    if isinstance(claims, (list, tuple)):
        claims = "\n\n".join(str(c) for c in claims)
    return {
        "found": True, "source": source, "publication_number": canonical,
        "title": title,
        "abstract": str(record.get("abstract") or biblio.get("abstract") or "").strip(),
        "publication_date": str(biblio.get("publication_date") or
                                record.get("publication_date") or "")[:10],
        "filing_date": str(biblio.get("filing_date") or record.get("filing_date") or "")[:10],
        "priority_date": str(biblio.get("priority_date") or record.get("priority_date") or "")[:10],
        "assignee": str(biblio.get("assignee") or record.get("assignee") or "").strip()[:300],
        "claims": str(claims or "").strip(),
        "url": str(record.get("url") or biblio.get("url") or "").strip(),
    }


def _date(value: Any) -> str:
    if not value:
        return ""
    return str(value)[:10]


def citations_in(text: str) -> list[str]:
    """Every well-formed citation token in ``text``, in order of appearance, with duplicates."""
    return [match.group(1).strip() for match in CITATION_RE.finditer(text or "")]


def malformed_citations_in(text: str) -> list[str]:
    """Tokens that look like citations but are not usable — the fabrication tell."""
    good = set(citations_in(text or ""))
    out = []
    for match in LOOSE_TOKEN_RE.finditer(text or ""):
        raw = match.group(1).strip()
        if raw not in good:
            out.append(raw)
    return out


def bare_publication_numbers(text: str) -> list[str]:
    """Publication numbers written into prose without a citation token.

    A patent number sitting in a sentence with no ``[REF:...]`` around it is not necessarily
    wrong, but it is invisible to every downstream check — the citation list, the IDS export and
    the reachability audit all key on the token.  Surfacing it is the point.
    """
    out: list[str] = []
    for match in BARE_PUB_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(2))
        if len(digits) < 5:
            continue
        candidate = f"{match.group(1)}{digits}{match.group(3) or ''}"
        canonical = normalize(candidate)
        if canonical and canonical not in out:
            out.append(canonical)
    return out


def check_all(citations: list[str], *, allow_remote: bool = True) -> dict[str, dict[str, Any]]:
    """Resolve a list of citations once each, preserving the caller's spelling as the key."""
    out: dict[str, dict[str, Any]] = {}
    for citation in citations:
        if citation in out:
            continue
        out[citation] = resolve(citation, allow_remote=allow_remote)
    return out
