"""Bridge to the prior-art search application's document code.

Fetching a publication, downloading its facsimile, reconstructing the two columns of a granted
patent into readable text, and lifting the drawing sheets out of the PDF are all solved in the
search app and were measured against real grants there. This compiler reuses that work rather
than writing a second, worse copy of it: ``patent_pdf`` alone carries the column reconstruction
that took a 64-page US grant from twelve shredded claims to twenty whole ones.

Everything is imported lazily and every entry point degrades to an empty result, so the pure
stages of this package (parsing, graph building, layout, rendering, validation) can be unit
tested on a machine that has none of the search app's dependencies installed.
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


def display_record(pub: str) -> dict[str, Any]:
    """The cached publication record: title, abstract, claims, description, figures, links."""
    try:
        return module("enrich_display").enrich_for_display(pub) or {}
    except Exception:
        return {}


def corpus_record(pub: str) -> dict[str, Any]:
    """The pre-built patent corpus, if it holds this publication."""
    try:
        return dict(module("mongo_corpus").get_detail(pub) or {})
    except Exception:
        return {}


def docstore_record(pub: str) -> dict[str, Any]:
    """The shared full-text docstore, under either spelling of the publication number.

    The store is keyed by the canonical compact form, and callers hold the hyphenated one, so
    asking with the wrong spelling reports an empty cache that is not empty.
    """
    import asyncio

    try:
        docstore = module("sources").docstore
    except Exception:
        return {}
    for key in dict.fromkeys([pub, pub.replace("-", "")]):
        try:
            record = asyncio.run(docstore.get(key)) or {}
        except Exception:
            continue
        if record.get("description") or record.get("claims"):
            return dict(record)
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
