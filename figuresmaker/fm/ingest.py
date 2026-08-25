"""Getting a draft in.

Four ways in, one thing out: the text of a specification. A pasted draft is taken as given. A
patent number or a Google Patents link is resolved to the published text. Any other link is
fetched and stripped. A PDF or a Word file is read locally.

The normaliser at the bottom matters more than it looks. Published patent text carries the marks
of the machine that produced it: a space before every comma, hyphenation broken across lines,
non-breaking spaces inside reference numerals. Every one of those defeats the numeral regex, and
a numeral the registry never sees is an element that never reaches a drawing.
"""
from __future__ import annotations

import io
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

import requests

USER_AGENT = os.environ.get(
    "FM_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36")
TIMEOUT = float(os.environ.get("FM_FETCH_TIMEOUT", "45"))
MAX_BYTES = 25 * 1024 * 1024


class IngestError(RuntimeError):
    """The draft could not be obtained. Says which step failed, never returns empty text."""


@dataclass
class Ingested:
    text: str
    source: str            # text | url | patent_number | pdf | docx
    source_ref: str = ""
    title: str = ""


# --------------------------------------------------------------------------- patent numbers

_KIND = r"(?:[ABCUTSPEH][0-9]?)"
_NUMBER_PATTERNS = (
    # US 10,123,456 B2 / US10123456B2 / 10,123,456
    re.compile(r"^\s*(?P<cc>US|EP|WO|GB|DE|FR|JP|CN|KR|CA|AU|IN)?\s*"
               r"(?P<num>[0-9][0-9,\. ]{4,14})\s*(?P<kind>" + _KIND + r")?\s*$", re.I),
    # US 2021/0123456 A1, publication numbers
    re.compile(r"^\s*(?P<cc>US|EP|WO)\s*(?P<num>[0-9]{4}[/ ]?[0-9]{6,7})\s*"
               r"(?P<kind>" + _KIND + r")?\s*$", re.I),
    # design and reissue: USD1234567, RE45678, PP12345
    re.compile(r"^\s*(?P<cc>US)?\s*(?P<pfx>D|RE|PP|H|T)\s*(?P<num>[0-9,\. ]{3,10})\s*"
               r"(?P<kind>" + _KIND + r")?\s*$", re.I),
)

_GOOGLE_PATENT_URL = re.compile(r"patents\.google\.com/patent/([A-Za-z0-9]+)", re.I)


def looks_like_patent_number(value: str) -> bool:
    return normalise_patent_number(value) is not None


def normalise_patent_number(value: str) -> Optional[str]:
    """A Google Patents document id, or None if this is not a patent number.

    Kept narrow on purpose. A string of digits that is really a paragraph number should fall
    through to being treated as text, not be silently resolved to somebody else's patent.
    """
    raw = (value or "").strip()
    if not raw or "\n" in raw:
        return None
    # A Google Patents link carries the number, and is longer than any number would be, so it is
    # matched before the length guard rather than after it.
    match = _GOOGLE_PATENT_URL.search(raw)
    if match:
        return match.group(1).upper()
    if len(raw) > 32:
        return None
    for pattern in _NUMBER_PATTERNS:
        hit = pattern.match(raw)
        if not hit:
            continue
        groups = hit.groupdict()
        country = (groups.get("cc") or "US").upper()
        digits = re.sub(r"[^0-9]", "", groups.get("num") or "")
        if not digits:
            continue
        prefix = (groups.get("pfx") or "").upper()
        kind = (groups.get("kind") or "").upper()
        if country == "US" and not prefix and len(digits) == 11:
            # A US pre-grant publication: 2021 0123456 -> US20210123456A1
            return f"US{digits}{kind or 'A1'}"
        if not kind:
            kind = "A1" if country in ("EP", "WO") else "B2"
        return f"{country}{prefix}{digits}{kind}"
    return None


# --------------------------------------------------------------------------------- fetching


def _get(url: str) -> requests.Response:
    response = requests.get(url, timeout=TIMEOUT, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }, stream=True)
    response.raise_for_status()
    body = b""
    for chunk in response.iter_content(65536):
        body += chunk
        if len(body) > MAX_BYTES:
            raise IngestError(f"{url} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    response._fm_body = body  # noqa: SLF001 - carried so callers do not re-read the stream
    return response


def from_patent_number(number: str) -> Ingested:
    doc_id = normalise_patent_number(number)
    if not doc_id:
        raise IngestError(f"{number!r} does not look like a patent number")
    url = f"https://patents.google.com/patent/{doc_id}/en"
    try:
        response = _get(url)
    except requests.HTTPError as exc:
        raise IngestError(f"Google Patents returned {exc.response.status_code} for {doc_id}. "
                          "Check the number, including its kind code") from exc
    except requests.RequestException as exc:
        raise IngestError(f"could not reach Google Patents for {doc_id}: {exc}") from exc
    title, text = parse_google_patents(response._fm_body.decode("utf-8", "replace"))
    if not text.strip():
        raise IngestError(f"{doc_id} was fetched but carries no description text on Google "
                          "Patents. A very old or an image-only document will do this")
    return Ingested(text=text, source="patent_number", source_ref=doc_id, title=title)


def from_url(url: str) -> Ingested:
    if _GOOGLE_PATENT_URL.search(url):
        return from_patent_number(url)
    try:
        response = _get(url)
    except requests.RequestException as exc:
        raise IngestError(f"could not fetch {url}: {exc}") from exc
    body = response._fm_body
    ctype = (response.headers.get("content-type") or "").lower()
    if "pdf" in ctype or body[:5] == b"%PDF-":
        return Ingested(text=from_pdf_bytes(body), source="url", source_ref=url)
    html = body.decode(response.encoding or "utf-8", "replace")
    title, text = parse_google_patents(html)
    if text.strip():
        return Ingested(text=text, source="url", source_ref=url, title=title)
    return Ingested(text=html_to_text(html), source="url", source_ref=url,
                    title=_html_title(html))


# ------------------------------------------------------------------------------------ parsing


def _lxml():
    try:
        from lxml import html as lxml_html
    except Exception as exc:  # pragma: no cover - deployment dependent
        raise IngestError(f"lxml is not installed, so no page can be parsed: {exc}") from exc
    return lxml_html


def parse_google_patents(html: str) -> tuple[str, str]:
    """Title and specification text from a Google Patents page, or ("", "") if it is not one."""
    if "patents.google.com" not in html and 'itemprop="description"' not in html:
        return "", ""
    lxml_html = _lxml()
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return "", ""

    title = ""
    for xpath in ('//meta[@name="DC.title"]/@content', '//title/text()'):
        found = tree.xpath(xpath)
        if found:
            title = str(found[0]).strip()
            break
    title = re.sub(r"\s*-\s*Google Patents\s*$", "", title).strip()

    parts: list[str] = []
    abstract = tree.xpath('//abstract//text() | //div[@class="abstract"]//text()')
    if abstract:
        body = _collapse("".join(abstract))
        if body:
            parts.append("Abstract\n" + body)

    description = tree.xpath('//section[@itemprop="description"]')
    if description:
        parts.append(_section_text(description[0]))
    claims = tree.xpath('//section[@itemprop="claims"]')
    if claims:
        parts.append("\n\nClaims\n\n" + _section_text(claims[0]))
    text = "\n\n".join(p for p in parts if p.strip())
    return title, normalise_text(text)


# Tags that end a run of text. Everything else is inline and its words belong to the paragraph
# they sit in. Getting this wrong is expensive in a way that leaves no trace: Google Patents puts
# every figure reference in an inline <figref>, and an extractor that takes only a block's direct
# text turns "FIG. 1 is a block diagram of a system" into "is a block diagram of a system". The
# brief description then parses to nothing, the planner has no ground truth, and the figure set
# is invented rather than read.
_BLOCK_TAGS = {"div", "p", "li", "ul", "ol", "table", "tr", "td", "th", "section", "article",
               "h1", "h2", "h3", "h4", "h5", "h6", "heading", "claim", "claims", "abstract",
               "description", "blockquote", "pre", "dl", "dt", "dd", "figure", "caption"}


def _section_text(node) -> str:
    """Block-aware text that keeps inline elements.

    Each block emits its own text, taken up to its first block-level child; the children are then
    walked in turn. Nested claim text comes out in the right order, and inline markup stays in
    the sentence it belongs to.
    """
    chunks: list[str] = []
    _walk(node, chunks)
    out: list[str] = []
    for chunk in chunks:
        text = _collapse(chunk)
        if not text:
            continue
        # Google Patents repeats a heading as the first words of the block under it.
        if out and len(text) < 60 and out[-1].endswith(text):
            continue
        out.append(text)
    return "\n\n".join(out)


def _walk(node, chunks: list[str]) -> None:
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            chunks.append("".join(buffer))
            buffer.clear()

    if node.text:
        buffer.append(node.text)
    for child in node:
        tag = str(child.tag).lower() if isinstance(child.tag, str) else ""
        if tag in ("script", "style", "noscript"):
            pass
        elif tag == "br":
            buffer.append(" ")
        elif tag in _BLOCK_TAGS:
            flush()
            _walk(child, chunks)
        else:
            buffer.append("".join(child.itertext()))
        if child.tail:
            buffer.append(child.tail)
    flush()


def _collapse(text: str) -> str:
    return re.sub(r"[ \t ]+", " ", (text or "")).strip()


def _html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return _collapse(re.sub(r"<[^>]+>", " ", match.group(1))) if match else ""


def html_to_text(html: str) -> str:
    lxml_html = _lxml()
    try:
        tree = lxml_html.fromstring(html)
    except Exception as exc:
        raise IngestError(f"the page could not be parsed as HTML: {exc}") from exc
    for bad in tree.xpath("//script | //style | //nav | //footer | //header | //noscript"):
        bad.getparent().remove(bad)
    blocks: list[str] = []
    for node in tree.xpath("//p | //li | //h1 | //h2 | //h3 | //h4 | //pre | //td"):
        text = _collapse(" ".join(node.itertext()))
        if len(text) > 1:
            blocks.append(text)
    if not blocks:
        blocks = [_collapse(" ".join(tree.itertext()))]
    seen: set[str] = set()
    kept: list[str] = []
    for block in blocks:
        if block in seen:
            continue
        seen.add(block)
        kept.append(block)
    return normalise_text("\n\n".join(kept))


def from_pdf_bytes(blob: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - deployment dependent
        raise IngestError(f"pypdf is not installed, so a PDF cannot be read: {exc}") from exc
    try:
        reader = PdfReader(io.BytesIO(blob))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise IngestError(f"the PDF could not be read: {exc}") from exc
    text = normalise_text("\n\n".join(pages))
    if len(text.strip()) < 200:
        raise IngestError("the PDF has no usable text layer. A scanned document needs OCR "
                          "before it can be read as a draft")
    return text


def from_docx_bytes(blob: bytes) -> str:
    try:
        import docx
    except Exception as exc:  # pragma: no cover
        raise IngestError(f"python-docx is not installed: {exc}") from exc
    try:
        document = docx.Document(io.BytesIO(blob))
    except Exception as exc:
        raise IngestError(f"the Word file could not be read: {exc}") from exc
    return normalise_text("\n\n".join(p.text for p in document.paragraphs))


# --------------------------------------------------------------------------------- normalising

_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(\w)")
_SPACE_BEFORE_PUNCT = re.compile(r"[  ]+([,;:.\)\]])")
_SPACE_AFTER_OPEN = re.compile(r"([\(\[])[  ]+")
_MULTI_BLANK = re.compile(r"\n{3,}")
_SPLIT_NUMERAL = re.compile(r"\b(\d)[  ](\d\d)\b")


def normalise_text(text: str) -> str:
    """Undo the damage a text layer does to a specification.

    Each substitution here is one that was found to hide reference numerals: a numeral split
    across a line break stops being a numeral, and a non-breaking space inside "1 02" makes the
    registry believe there is no housing 102 in the draft at all.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("­", "")                       # soft hyphen
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = re.sub(r"[‐-―]", "-", text)            # dash family, to a plain hyphen
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _SPACE_AFTER_OPEN.sub(r"\1", text)
    text = _SPLIT_NUMERAL.sub(r"\1\2", text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


# -------------------------------------------------------------------------------- entry point


def ingest(*, text: str = "", url: str = "", upload: Optional[tuple[str, bytes]] = None
           ) -> Ingested:
    """Whatever the form supplied, resolved to a specification."""
    if upload:
        name, blob = upload
        lower = name.lower()
        if lower.endswith(".pdf") or blob[:5] == b"%PDF-":
            return Ingested(text=from_pdf_bytes(blob), source="pdf", source_ref=name)
        if lower.endswith(".docx"):
            return Ingested(text=from_docx_bytes(blob), source="docx", source_ref=name)
        body = normalise_text(blob.decode("utf-8", "replace"))
        if not body.strip():
            raise IngestError(f"{name} is empty")
        return Ingested(text=body, source="text", source_ref=name)

    candidate = (url or text or "").strip()
    if url:
        if not re.match(r"^https?://", url, re.I):
            number = normalise_patent_number(url)
            if number:
                return from_patent_number(url)
            raise IngestError(f"{url!r} is neither a http(s) link nor a patent number")
        return from_url(url)

    if not text or not text.strip():
        raise IngestError("no draft text, link or file was supplied")

    # A single short line that is really a patent number is a link, not a draft.
    if len(candidate) <= 32 and looks_like_patent_number(candidate):
        return from_patent_number(candidate)
    if re.match(r"^https?://\S+$", candidate):
        return from_url(candidate)
    body = normalise_text(text)
    if len(body) < 200:
        raise IngestError("the draft is too short to plan figures from. Paste the specification, "
                          "or give a patent number or a link")
    return Ingested(text=body, source="text")
