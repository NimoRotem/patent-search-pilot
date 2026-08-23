"""Bridge to the prior-art search application's document code.

Fetching a publication, downloading its facsimile, reconstructing the two columns of a granted
patent into readable text, and lifting the drawing sheets out of the PDF are all solved in the
search app and were measured against real grants there. This compiler reuses that work rather
than writing a second, worse copy of it: ``patent_pdf`` alone carries the column reconstruction
that took a 64-page US grant from twelve shredded claims to twenty whole ones.

Everything is imported lazily and every entry point degrades to an empty result, so the pure
stages of this package (parsing, graph building, layout, rendering, validation) can be unit
tested on a machine that has none of the search app's dependencies installed.

Degrading quietly is right for a stage that has another way to get what it needs and wrong for
one that is about to tell a human what it found. Every lookup here therefore takes ``strict``:
pass it when the answer is going into a note, and the difference between "asked, holds nothing"
and "could not be asked" arrives as :class:`SourceUnavailable` instead of as an empty dict that
reads like a fact.
"""
from __future__ import annotations

import functools
import os
import sys
from pathlib import Path
from typing import Any, Optional

PILOT_ROOT = Path(os.environ.get(
    "PILOT_ROOT", os.path.expanduser("~/patent-search-pilot"))).resolve()
PILOT_SRC = PILOT_ROOT / "src"


class PilotUnavailable(RuntimeError):
    """The search app's document code is not importable in this process."""


@functools.lru_cache(maxsize=1)
def _ensure_path() -> bool:
    src = str(PILOT_SRC)
    if not PILOT_SRC.is_dir():
        return False
    if src not in sys.path:
        # Appended, not prepended: this package's own modules must win a name collision.
        sys.path.append(src)
    return True


def module(name: str):
    """Import one search-app module, or raise :class:`PilotUnavailable`."""
    if not _ensure_path():
        raise PilotUnavailable(f"{PILOT_SRC} is not present")
    try:
        return __import__(name)
    except Exception as exc:  # pragma: no cover - depends on the deployment
        raise PilotUnavailable(f"could not import {name}: {exc}") from exc


def available() -> bool:
    try:
        module("patent_pdf")
        return True
    except PilotUnavailable:
        return False


# ---------------------------------------------------------------------------
# the four things this compiler actually needs
# ---------------------------------------------------------------------------
def parse_patent_ref(raw: str) -> Optional[str]:
    """A Google Patents / Espacenet URL or bare number -> a canonical publication number.

    Pure string parsing on the search app's side: the pasted URL is never fetched, so a hostile
    link cannot reach an internal address through this app either.
    """
    return module("ingest_input").parse_patent_ref(raw)


def sniff_kind(data: bytes, filename: str) -> str:
    return module("ingest_input").sniff_kind(data, filename)


def safe_label(filename: str) -> str:
    return module("ingest_input").safe_label(filename)


def read_pdf(path: str) -> dict[str, Any]:
    """Layout-aware text from a PDF path: ``{text, n_pages, text_layer, notes, ...}``."""
    return module("patent_pdf").extract(path)


def figures_from_pdf(path: str) -> list[bytes]:
    """Drawing sheets from a PDF, as PNG bytes, via the search app's calibrated extractor."""
    try:
        return module("drawings").figures_from_pdf(path) or []
    except Exception:
        return []


def display_record(pub: str, *, strict: bool = False) -> dict[str, Any]:
    """The cached publication record: title, abstract, claims, description, figures, links.

    ``strict`` separates "asked, and it holds nothing" from "could not be asked". A caller that
    reports what it found to a human wants the second raised: this call returning ``{}`` once
    for a record that holds 43 citations is what told a whole reference-guided job that the
    publication had no neighbouring art, and it drew four schematics instead without anyone
    being able to see why. See :class:`SourceUnavailable`.
    """
    try:
        return module("enrich_display").enrich_for_display(pub) or {}
    except Exception as exc:
        if strict:
            raise SourceUnavailable(
                f"the publication record could not be read: {type(exc).__name__}: "
                f"{str(exc)[:200]}") from exc
        return {}


def corpus_record(pub: str, *, strict: bool = False) -> dict[str, Any]:
    """The pre-built patent corpus, if it holds this publication."""
    try:
        return dict(module("mongo_corpus").get_detail(pub) or {})
    except Exception as exc:
        if strict:
            raise SourceUnavailable(
                f"our own corpus could not be read: {type(exc).__name__}: "
                f"{str(exc)[:200]}") from exc
        return {}


def docstore_record(pub: str, *, strict: bool = False) -> dict[str, Any]:
    """The shared full-text docstore, under either spelling of the publication number.

    The store is keyed by the canonical compact form, and callers hold the hyphenated one, so
    asking with the wrong spelling reports an empty cache that is not empty.
    """
    import asyncio

    try:
        docstore = module("sources").docstore
    except Exception as exc:
        if strict:
            raise SourceUnavailable(
                f"the docstore could not be opened: {type(exc).__name__}: "
                f"{str(exc)[:200]}") from exc
        return {}
    failure: Optional[Exception] = None
    for key in dict.fromkeys([pub, pub.replace("-", "")]):
        try:
            record = asyncio.run(docstore.get(key)) or {}
        except Exception as exc:
            failure = exc
            continue
        if record.get("description") or record.get("claims"):
            return dict(record)
    if failure is not None and strict:
        raise SourceUnavailable(
            f"the docstore could not be read: {type(failure).__name__}: "
            f"{str(failure)[:200]}") from failure
    return {}


class SourceUnavailable(RuntimeError):
    """A source could not be ASKED. Distinct from a source that was asked and had nothing.

    Collapsing the two is how `sources.registry` being a function rather than a module — an
    AttributeError, a plain wiring mistake — was reported for an hour as "our Google Patents
    reader does not hold this publication". A source that cannot be reached has to say so.
    """


def adapter_details(pub: str, adapter_name: str, timeout: float = 90.0) -> dict[str, Any]:
    """Ask ONE source adapter for one document.

    The compiler drives its own ladder rather than the search app's, so it addresses the
    adapters individually. Every one of them exposes ``details(publication, client)``.

    Returns the record, or ``{}`` when the source genuinely has nothing. Raises
    :class:`SourceUnavailable` when it could not be asked at all, so the caller can tell a
    missing document from a broken pipe.
    """
    import asyncio

    try:
        # `sources.registry` is a FUNCTION returning {name: adapter}, not a module.
        adapters = module("sources").registry()
    except Exception as exc:
        raise SourceUnavailable(f"the source registry could not be built: {exc}") from exc
    adapter = adapters.get(adapter_name)
    if adapter is None:
        raise SourceUnavailable(f"there is no source called {adapter_name!r}")
    if not hasattr(adapter, "details"):
        raise SourceUnavailable(f"{adapter_name} cannot fetch a single document")
    try:
        if not adapter.enabled():
            raise SourceUnavailable(f"{adapter_name} is not configured on this host")
    except SourceUnavailable:
        raise
    except Exception as exc:
        raise SourceUnavailable(f"{adapter_name} could not report whether it is usable: "
                                f"{exc}") from exc

    async def run() -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return dict(await adapter.details(pub, client) or {})

    try:
        return asyncio.run(run())
    except Exception as exc:
        raise SourceUnavailable(f"{adapter_name} failed: {type(exc).__name__}: "
                                f"{str(exc)[:120]}") from exc


def fetch_fulltext(pub: str, timeout: float = 120.0) -> dict[str, Any]:
    """The search app's full-text acquisition ladder, for one publication.

    Docstore, then PQAI, then EPO OPS, then Google Patents direct, then the paid backstop —
    cheapest first, everything it finds persisted for the next caller. This is the answer to a
    publication whose facsimile is an image-only scan: the text exists, in English, in a source
    this box already reaches, and the compiler's job is to ask rather than to give up.

    Returns ``{title, abstract, claims, description, source}``; empty on any failure.
    """
    try:
        sources = module("sources")
        payload = sources.fetch_fulltext([pub], timeout=timeout) or {}
    except Exception:
        return {}
    # The ladder keys its result by the CANONICAL spelling, which is not always the one it was
    # asked for: "US-20240324075-A1" comes back as "US20240324075A1". Take the one record that
    # is not the summary rather than guessing the spelling.
    for key, record in payload.items():
        if key == "_summary" or not isinstance(record, dict):
            continue
        if record.get("description") or record.get("claims"):
            return dict(record)
    return {}


def fetch_image(url: str, timeout: float = 30.0) -> bytes:
    """One drawing sheet from a patent image CDN.

    Only the hosts the search app already fetches from. The URL comes out of a publication
    record this box built, never out of anything a user typed, so there is no path from a pasted
    string to an arbitrary address; the allow-list is belt and braces on top of that.
    """
    from urllib.parse import urlparse

    allowed = {"patentimages.storage.googleapis.com", "storage.googleapis.com",
               "worldwide.espacenet.com", "ops.epo.org"}
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return b""
    if host not in allowed:
        return b""
    try:
        import requests

        response = requests.get(url, timeout=timeout,
                                headers={"User-Agent": "rotem-patent-figure-compiler/1.0"})
        if response.status_code != 200:
            return b""
        blob = response.content or b""
        return blob if blob[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1",
                                    b"GIF8") or blob[:2] == b"\xff\xd8" else b""
    except Exception:
        return b""


def figure_dir(pub: str) -> Optional[Path]:
    try:
        return module("enrich_display").FIGDIR / pub
    except Exception:
        return None


def pdf_dir() -> Optional[Path]:
    try:
        return module("enrich_display").PDFDIR
    except Exception:
        return None


def scrape_pdf_url(pub: str) -> str:
    try:
        return module("enrich_display")._scrape_google_pdf(pub) or ""
    except Exception:
        return ""


def download(url: str, dest: Path) -> bool:
    try:
        return bool(module("enrich_display")._download(url, dest))
    except Exception:
        return False


def docx_text(data: bytes) -> str:
    try:
        return module("ingest_input")._docx_text(data) or ""
    except Exception:
        return ""


def is_independent_claim(text: str) -> bool:
    try:
        return bool(module("patent_doc").is_independent(text))
    except Exception:
        return True


def split_claims(blob: str) -> list[dict[str, Any]]:
    try:
        return module("patent_doc").split_claims(blob) or []
    except Exception:
        return []
