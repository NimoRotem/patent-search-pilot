"""Deterministic patent XML, HTML, and JSON normalization.

Technical text is copied from the source. No LLM is called and no claim ancestry is
inferred when the source text does not identify exact parent claim numbers.
"""
from __future__ import annotations

import hashlib
import html as html_module
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from .identifiers import normalize_classification, normalize_publication_number

_SPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_SOURCE_TRANSLATION = re.compile(
    r'<span class="google-src-text"[^>]*>[\s\S]*?</span>', re.IGNORECASE
)


def _local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _language(element, fallback="") -> str:
    return str(
        element.attrib.get("lang")
        or element.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
        or fallback
        or ""
    ).lower()


def _clean_text(value: str) -> str:
    return _SPACE.sub(" ", str(value or "")).strip()


def _element_text(element) -> str:
    return _clean_text(" ".join(element.itertext()))


def _html_text(fragment: str) -> str:
    source = _SOURCE_TRANSLATION.sub(" ", fragment or "")
    source = re.sub(r"</(?:p|li|div|claim|claim-text|heading|h[1-6])>", "\n", source,
                    flags=re.IGNORECASE)
    source = _TAG.sub(" ", source)
    lines = [_clean_text(html_module.unescape(line)) for line in source.splitlines()]
    return "\n".join(line for line in lines if line)


_DEPENDENCY_EXACT = (
    re.compile(r"\bclaims?\s+((?:\d+\s*(?:,|and|or|to|-)\s*)*\d+)", re.IGNORECASE),
    re.compile(r"\banspr(?:uch|ueche|üche|uechen|üchen)\s+"
               r"((?:\d+\s*(?:,|und|oder|bis|-)\s*)*\d+)", re.IGNORECASE),
)
_DEPENDENCY_WORDS = re.compile(
    r"\b(?:claim|claims|preceding claim|previous claim|anspruch|ansprueche|ansprüche|"
    r"vorhergehenden anspr)", re.IGNORECASE
)


def _expand_numbers(expression: str) -> list[int]:
    values: list[int] = []
    for start, end in re.findall(r"(\d+)\s*(?:-|to|bis)\s*(\d+)", expression, re.IGNORECASE):
        first, last = int(start), int(end)
        if 0 < first <= last and last - first <= 100:
            values.extend(range(first, last + 1))
    without_ranges = re.sub(r"\d+\s*(?:-|to|bis)\s*\d+", " ", expression,
                            flags=re.IGNORECASE)
    values.extend(int(value) for value in re.findall(r"\d+", without_ranges))
    return list(dict.fromkeys(value for value in values if value > 0))


def claim_dependencies(text: str, claim_number: int) -> tuple[list[int], bool]:
    dependencies = []
    for pattern in _DEPENDENCY_EXACT:
        for match in pattern.finditer(text or ""):
            dependencies.extend(_expand_numbers(match.group(1)))
    dependencies = list(dict.fromkeys(
        number for number in dependencies if number != int(claim_number)
    ))
    mentions_dependency = bool(_DEPENDENCY_WORDS.search(text or ""))
    exact = bool(dependencies) or not mentions_dependency
    return dependencies, exact


def normalize_claims(raw_claims: Iterable[dict | tuple | str], language: str = "") -> list[dict]:
    claims = []
    for index, raw in enumerate(raw_claims, 1):
        if isinstance(raw, dict):
            number = int(raw.get("number") or raw.get("num") or index)
            text = _clean_text(raw.get("raw_text") or raw.get("text") or "")
            claim_language = str(raw.get("language") or language or "")
            explicit = raw.get("depends_on")
            explicit_independent = raw.get("independent")
        elif isinstance(raw, tuple):
            number, text = int(raw[0]), _clean_text(raw[1])
            claim_language, explicit = language, None
            explicit_independent = None
        else:
            number, text = index, _clean_text(raw)
            claim_language, explicit = language, None
            explicit_independent = None
        if not text:
            continue
        if explicit is None:
            depends_on, exact = claim_dependencies(text, number)
        else:
            depends_on = [int(value) for value in explicit if int(value) != number]
            exact = bool(raw.get("chain_complete", True))
        mentions_dependency = bool(_DEPENDENCY_WORDS.search(text))
        independent = (
            bool(explicit_independent)
            if explicit_independent is not None
            else not depends_on and not mentions_dependency
        )
        if not independent and not depends_on:
            exact = False
        claims.append({
            "number": number,
            "text": text,
            "raw_text": text,
            "independent": independent,
            "dependent": not independent,
            "depends_on": depends_on,
            "resolved_text": "",
            "chain_complete": exact,
            "language": claim_language,
        })

    by_number = {claim["number"]: claim for claim in claims}

    def resolve(number: int, path: tuple[int, ...] = ()) -> tuple[str, bool]:
        claim = by_number.get(number)
        if claim is None or number in path or not claim["chain_complete"]:
            return "", False
        if not claim["depends_on"]:
            if claim["independent"]:
                return claim["raw_text"], True
            return "", False
        inherited = []
        for parent in claim["depends_on"]:
            text, complete = resolve(parent, (*path, number))
            if not complete:
                return "", False
            if text not in inherited:
                inherited.append(text)
        return "\n".join((*inherited, claim["raw_text"])), True

    for claim in claims:
        resolved, complete = resolve(claim["number"])
        claim["resolved_text"] = resolved
        claim["chain_complete"] = complete
    return claims


def _document_ids(root, within: str | None = None) -> list[str]:
    results = []
    parents = [node for node in root.iter() if within is None or _local(node.tag) == within]
    for parent in parents:
        for document in parent.iter():
            if _local(document.tag) != "document-id":
                continue
            values = {}
            for child in document:
                values[_local(child.tag)] = _element_text(child)
            publication = normalize_publication_number(
                f"{values.get('country', '')}{values.get('doc-number', '')}{values.get('kind', '')}"
            )
            if publication:
                results.append(publication)
        if within is not None:
            break
    return list(dict.fromkeys(results))


def _first_text(root, names: set[str]) -> str:
    for element in root.iter():
        if _local(element.tag) in names:
            value = _element_text(element)
            if value:
                return value
    return ""


def _classifications(root, container_name: str) -> list[str]:
    codes = []
    for container in root.iter():
        if _local(container.tag) != container_name:
            continue
        candidates = []
        for element in container.iter():
            if _local(element.tag) in {"text", "classification-symbol", "main-classification", "further-classification"}:
                candidates.append(_element_text(element))
        if not candidates:
            candidates.append(_element_text(container))
        for candidate in candidates:
            code = normalize_classification(candidate)
            if code and code not in codes:
                codes.append(code)
    return codes


def _parse_xml(raw: bytes, publication_number: str, provider: str, media_type: str) -> dict:
    root = ET.fromstring(raw)
    language = _language(root)
    title = _first_text(root, {"invention-title", "title-of-invention"})
    abstract = _first_text(root, {"abstract"})

    claim_rows = []
    for element in root.iter():
        if _local(element.tag) != "claim":
            continue
        raw_number = element.attrib.get("num") or element.attrib.get("number") or element.attrib.get("id") or ""
        match = re.search(r"\d+", str(raw_number))
        number = int(match.group()) if match else len(claim_rows) + 1
        claim_rows.append({
            "number": number,
            "text": _element_text(element),
            "language": _language(element, language),
        })
    claims = normalize_claims(claim_rows, language)

    paragraphs = []
    current_section = ""
    description_language = language
    description = next((node for node in root.iter() if _local(node.tag) in {"description", "description-of-invention"}), None)
    if description is not None:
        description_language = _language(description, language)
        for element in description.iter():
            name = _local(element.tag)
            if name in {"heading", "title"}:
                current_section = _element_text(element)
                continue
            if name not in {"p", "para", "paragraph"}:
                continue
            text = _element_text(element)
            if not text:
                continue
            paragraph_id = str(
                element.attrib.get("id") or element.attrib.get("num") or f"p{len(paragraphs) + 1:05d}"
            )
            raw_page = element.attrib.get("page") or element.attrib.get("page-no")
            try:
                page = int(raw_page) if raw_page not in (None, "") else None
            except ValueError:
                page = None
            location = f"paragraph:{paragraph_id}"
            if page is not None:
                location += f";page:{page}"
            paragraphs.append({
                "id": paragraph_id,
                "text": text,
                "section": current_section,
                "page": page,
                "language": _language(element, description_language),
                "source_location": location,
            })

    captions = []
    for figure in root.iter():
        if _local(figure.tag) != "figure":
            continue
        caption = _element_text(figure)
        if caption:
            captions.append(caption)

    citations = _document_ids(root, "references-cited")
    dates = {}
    for reference in root.iter():
        name = _local(reference.tag)
        if name not in {"publication-reference", "application-reference", "priority-claim"}:
            continue
        date = _first_text(reference, {"date"})
        if name == "publication-reference":
            dates["publication_date"] = date
        elif name == "application-reference":
            dates["filing_date"] = date
        elif name == "priority-claim" and date:
            old = dates.get("earliest_priority_date", "")
            dates["earliest_priority_date"] = min(value for value in (old, date) if value)

    complete_claims = bool(claims) and all(claim["raw_text"] for claim in claims)
    complete_description = bool(paragraphs)
    return {
        "publication_number": normalize_publication_number(publication_number),
        "publication_id": normalize_publication_number(publication_number),
        "family_id": "",
        "title": title,
        "abstract": abstract,
        "language": language,
        "claims": claims,
        "description_paragraphs": paragraphs,
        "figure_captions": captions,
        "citations": citations,
        "cpc": _classifications(root, "classification-cpc"),
        "ipc": _classifications(root, "classification-ipc"),
        "dates": dates,
        "source": {
            "provider": provider,
            "media_type": media_type,
            "content_hash": hashlib.sha256(raw).hexdigest(),
        },
        "completeness": {
            "has_title": bool(title),
            "has_abstract": bool(abstract),
            "has_claims": bool(claims),
            "has_complete_claims": complete_claims,
            "has_description": bool(paragraphs),
            "has_complete_description": complete_description,
            "has_figures": bool(captions),
            "has_citations": bool(citations),
        },
    }


def _section(html: str, itemprop: str) -> str:
    match = re.search(
        rf'<section[^>]*itemprop=["\']{re.escape(itemprop)}["\'][^>]*>([\s\S]*?)</section>',
        html,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _attributes(fragment: str) -> dict[str, str]:
    return {
        key.lower(): html_module.unescape(value)
        for key, _quote, value in re.findall(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", fragment)
    }


_HTML_CLAIM_START = re.compile(r"^\s*(?:claim\s+)?(\d+)\s*[.):]\s*", re.IGNORECASE)


def _html_claims(fragment: str) -> list[dict | str]:
    """Group nested Google claim-text continuations under their numbered claim."""
    lines = [line for line in _html_text(fragment).splitlines() if line]
    grouped = []
    current_number = None
    current_lines = []
    for line in lines:
        match = _HTML_CLAIM_START.match(line)
        number = int(match.group(1)) if match else None
        starts_claim = number is not None and (
            current_number is None or number == current_number + 1
        )
        if starts_claim:
            if current_lines:
                grouped.append({
                    "number": current_number,
                    "text": " ".join(current_lines),
                })
            current_number = number
            current_lines = [line]
        elif current_number is not None:
            current_lines.append(line)
    if current_lines:
        grouped.append({
            "number": current_number,
            "text": " ".join(current_lines),
        })
    return grouped or lines


def _html_description_blocks(fragment: str):
    standard = list(re.finditer(
        r'<(heading|h[1-6]|p|para|paragraph)\b([^>]*)>([\s\S]*?)</\1>',
        fragment,
        re.IGNORECASE,
    ))
    description_lines = list(re.finditer(
        r'<div\b([^>]*\bclass\s*=\s*["\'][^"\']*\bdescription-line\b'
        r'[^"\']*["\'][^>]*)>([\s\S]*?)</div>',
        fragment,
        re.IGNORECASE,
    ))
    covered = [(match.start(), match.end()) for match in description_lines]
    blocks = [
        (match.start(), "div", match.group(1), match.group(2))
        for match in description_lines
    ]
    blocks.extend(
        (match.start(), match.group(1), match.group(2), match.group(3))
        for match in standard
        if not any(start <= match.start() and match.end() <= end for start, end in covered)
    )
    return sorted(blocks, key=lambda block: block[0])


def _parse_html(raw: bytes, publication_number: str, provider: str, media_type: str) -> dict:
    page = raw.decode("utf-8", "replace")
    title_match = re.search(
        r'<meta[^>]+name=["\']DC\.title["\'][^>]+content=["\']([^"\']*)', page,
        re.IGNORECASE,
    )
    title = _clean_text(html_module.unescape(title_match.group(1))) if title_match else ""
    language_match = re.search(r'<html[^>]+lang=["\']([^"\']+)', page, re.IGNORECASE)
    language = language_match.group(1).lower() if language_match else "en"
    abstract = _html_text(_section(page, "abstract")).replace("\n", " ")

    claims_html = _section(page, "claims")
    claims = normalize_claims(_html_claims(claims_html), language)

    paragraphs = []
    section = ""
    description_html = _section(page, "description")
    for _offset, tag, attribute_text, fragment in _html_description_blocks(description_html):
        text = _html_text(fragment).replace("\n", " ")
        if not text:
            continue
        if tag.lower() in {"heading", "h1", "h2", "h3", "h4", "h5", "h6"} \
                or re.search(r"<(?:heading|h[1-6])\b", fragment, re.IGNORECASE):
            section = text
            continue
        attributes = _attributes(attribute_text)
        paragraph_id = attributes.get("id") or attributes.get("num") or f"p{len(paragraphs) + 1:05d}"
        raw_page = attributes.get("page") or attributes.get("page-no")
        try:
            page_number = int(raw_page) if raw_page else None
        except ValueError:
            page_number = None
        location = f"paragraph:{paragraph_id}"
        if page_number is not None:
            location += f";page:{page_number}"
        paragraphs.append({
            "id": paragraph_id,
            "text": text,
            "section": section,
            "page": page_number,
            "language": language,
            "source_location": location,
        })

    cpc = [normalize_classification(value.split(" ", 1)[0]) for value in re.findall(
        r'<meta[^>]+name=["\']citation_patent_classification["\'][^>]+content=["\']([^"\']+)',
        page,
        re.IGNORECASE,
    )]
    citation_blocks = re.findall(
        r'<table\b[^>]*itemprop=["\'](?:patentCitations|citedBy)["\'][^>]*>'
        r'([\s\S]*?)</table>',
        page,
        re.IGNORECASE,
    )
    citations = [
        normalize_publication_number(value)
        for block in citation_blocks
        for value in re.findall(
            r'itemprop=["\']publicationNumber["\'][^>]*>([^<]+)',
            block,
            re.IGNORECASE,
        )
    ]
    publication = normalize_publication_number(publication_number)
    citations = list(dict.fromkeys(value for value in citations if value and value != publication))
    figure_captions = [
        _html_text(fragment).replace("\n", " ")
        for fragment in re.findall(r"<figure\b[^>]*>([\s\S]*?)</figure>", page, re.IGNORECASE)
        if _html_text(fragment)
    ]
    return {
        "publication_number": publication,
        "publication_id": publication,
        "family_id": "",
        "title": title,
        "abstract": abstract,
        "language": language,
        "claims": claims,
        "description_paragraphs": paragraphs,
        "figure_captions": figure_captions,
        "citations": citations,
        "cpc": list(dict.fromkeys(value for value in cpc if value)),
        "ipc": [],
        "dates": {},
        "source": {
            "provider": provider,
            "media_type": media_type,
            "content_hash": hashlib.sha256(raw).hexdigest(),
        },
        "completeness": {
            "has_title": bool(title),
            "has_abstract": bool(abstract),
            "has_claims": bool(claims),
            "has_complete_claims": bool(claims),
            "has_description": bool(paragraphs),
            "has_complete_description": bool(paragraphs),
            "has_figures": bool(figure_captions),
            "has_citations": bool(citations),
        },
    }


def _split_plain_paragraphs(text: str, language: str) -> list[dict]:
    lines = [_clean_text(line) for line in re.split(r"\n\s*\n|\r?\n", text or "")]
    return [
        {
            "id": f"p{index:05d}",
            "text": line,
            "section": "",
            "page": None,
            "language": language,
            "source_location": f"paragraph:p{index:05d}",
        }
        for index, line in enumerate((line for line in lines if line), 1)
    ]


def _parse_json(raw: bytes, publication_number: str, provider: str, media_type: str) -> dict:
    data = json.loads(raw.decode("utf-8"))
    publication = normalize_publication_number(
        data.get("publication_number") or data.get("pub_number") or publication_number
    )
    language = str(data.get("language") or data.get("claims_lang") or data.get("desc_lang") or "")
    raw_claims = data.get("claims") or []
    if isinstance(raw_claims, str):
        raw_claims = [line for line in raw_claims.splitlines() if _clean_text(line)]
    claims = normalize_claims(raw_claims, language)
    paragraphs = data.get("description_paragraphs") or []
    if not paragraphs:
        paragraphs = _split_plain_paragraphs(str(data.get("description") or ""), language)
    normalized_paragraphs = []
    for index, paragraph in enumerate(paragraphs, 1):
        if isinstance(paragraph, str):
            paragraph = {"text": paragraph}
        text = _clean_text(paragraph.get("text") or "")
        if not text:
            continue
        paragraph_id = str(paragraph.get("id") or paragraph.get("paragraph_id") or f"p{index:05d}")
        page = paragraph.get("page")
        location = str(paragraph.get("source_location") or f"paragraph:{paragraph_id}")
        normalized_paragraphs.append({
            "id": paragraph_id,
            "text": text,
            "section": str(paragraph.get("section") or ""),
            "page": page,
            "language": str(paragraph.get("language") or language),
            "source_location": location,
        })
    source = dict(data.get("source") or {})
    source.update(provider=provider, media_type=media_type, content_hash=hashlib.sha256(raw).hexdigest())
    return {
        "publication_number": publication,
        "publication_id": str(data.get("publication_id") or publication),
        "family_id": str(data.get("family_id") or ""),
        "title": _clean_text(data.get("title") or ""),
        "abstract": _clean_text(data.get("abstract") or ""),
        "language": language,
        "claims": claims,
        "description_paragraphs": normalized_paragraphs,
        "figure_captions": list(data.get("figure_captions") or []),
        "citations": [normalize_publication_number(value) for value in data.get("citations", []) if value],
        "cpc": [normalize_classification(value) for value in data.get("cpc", []) if value],
        "ipc": [normalize_classification(value) for value in data.get("ipc", []) if value],
        "dates": dict(data.get("dates") or {}),
        "source": source,
        "completeness": dict(data.get("completeness") or {
            "has_title": bool(data.get("title")),
            "has_abstract": bool(data.get("abstract")),
            "has_claims": bool(claims),
            "has_complete_claims": bool(claims),
            "has_description": bool(normalized_paragraphs),
            "has_complete_description": bool(normalized_paragraphs),
            "has_figures": bool(data.get("figure_captions")),
            "has_citations": bool(data.get("citations")),
        }),
    }


def parse_source(
    content: bytes | str,
    media_type: str,
    publication_number: str,
    provider: str,
) -> dict:
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    normalized_type = (media_type or "").split(";", 1)[0].strip().lower()
    leading = raw.lstrip()[:32].lower()
    if normalized_type in {"application/json", "text/json"} or leading.startswith((b"{", b"[")):
        return _parse_json(raw, publication_number, provider, normalized_type or "application/json")
    if normalized_type in {"text/html", "application/xhtml+xml"} or b"<html" in leading:
        return _parse_html(raw, publication_number, provider, normalized_type or "text/html")
    if normalized_type in {"application/xml", "text/xml"} or leading.startswith(b"<"):
        return _parse_xml(raw, publication_number, provider, normalized_type or "application/xml")
    raise ValueError(f"unsupported patent source media type: {media_type or 'unknown'}")


def merge_parsed(existing: dict | None, incoming: dict) -> dict:
    """Merge complementary parsed sources without blanking richer technical text."""
    if not existing:
        return incoming
    merged = dict(existing)
    for key in ("title", "abstract"):
        old, new = str(existing.get(key) or ""), str(incoming.get(key) or "")
        merged[key] = new if len(new) > len(old) else old
    merged["family_id"] = incoming.get("family_id") or existing.get("family_id") or ""
    merged["language"] = existing.get("language") or incoming.get("language") or ""

    def richer(key):
        old = list(existing.get(key) or [])
        new = list(incoming.get(key) or [])
        old_score = (sum(len(str(item.get("text") or item.get("raw_text") or item))
                         for item in old), len(old))
        new_score = (sum(len(str(item.get("text") or item.get("raw_text") or item))
                         for item in new), len(new))
        return new if new_score > old_score else old

    merged["claims"] = richer("claims")
    merged["description_paragraphs"] = richer("description_paragraphs")

    def deduplicated(values):
        seen = set()
        output = []
        for value in values:
            identity = (
                json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                if isinstance(value, (dict, list))
                else repr(value)
            )
            if identity in seen:
                continue
            seen.add(identity)
            output.append(value)
        return output

    for key in ("figure_captions", "citations", "cpc", "ipc"):
        merged[key] = deduplicated([
            *(existing.get(key) or []), *(incoming.get(key) or [])
        ])
    dates = dict(existing.get("dates") or {})
    for key, value in (incoming.get("dates") or {}).items():
        if value and not dates.get(key):
            dates[key] = value
    merged["dates"] = dates
    histories = list(existing.get("source_history") or [existing.get("source") or {}])
    new_source = incoming.get("source") or {}
    if new_source and new_source not in histories:
        histories.append(new_source)
    merged["source"] = incoming.get("source") or existing.get("source") or {}
    merged["source_history"] = histories
    old_complete = existing.get("completeness") or {}
    new_complete = incoming.get("completeness") or {}
    merged["completeness"] = {
        key: bool(old_complete.get(key) or new_complete.get(key))
        for key in (
            "has_title", "has_abstract", "has_claims", "has_complete_claims",
            "has_description", "has_complete_description", "has_figures", "has_citations",
        )
    }
    return merged


def main(argv=None) -> int:
    from .cli import run_parse
    return run_parse(argv)


if __name__ == "__main__":
    raise SystemExit(main())
