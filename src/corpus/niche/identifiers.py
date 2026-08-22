"""Canonical publication and family identifiers."""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_US_APPLICATION = re.compile(r"^US(19\d\d|20\d\d)(\d{4,7})(A[19])$")
_KIND = re.compile(r"^[A-Z]{2}[A-Z]{0,3}\d+([A-Z]\d?)$")
_LANGUAGE = re.compile(r"^[a-z]{2}$")
_PARTS = re.compile(r"^([A-Z]{2})(\d+)([A-Z]\d?)$")


def normalize_publication_number(value: str) -> str:
    """Normalize punctuation and repair shortened US pre-grant sequence numbers."""
    text = str(value or "").strip()
    if "patent/" in text:
        text = text.split("patent/", 1)[1].split("/", 1)[0]
    normalized = _NON_ALNUM.sub("", text.upper())
    match = _US_APPLICATION.match(normalized)
    if match:
        return f"US{match.group(1)}{int(match.group(2)):07d}{match.group(3)}"
    return normalized


def authority_of(publication_number: str) -> str:
    normalized = normalize_publication_number(publication_number)
    match = re.match(r"^([A-Z]{2})", normalized)
    return match.group(1) if match else ""


def kind_code_of(publication_number: str) -> str:
    match = _KIND.match(normalize_publication_number(publication_number))
    return match.group(1) if match else ""


def normalize_classification(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").upper()).strip(".;,")


def normalize_family_id(value: str, publication_number: str) -> str:
    family = re.sub(r"\s+", "", str(value or "").strip())
    if family:
        return f"family:{family.upper()}"
    return f"publication:{normalize_publication_number(publication_number)}"


def google_patent_url(publication_number: str, language: str = "") -> str:
    normalized = normalize_publication_number(publication_number)
    source_language = str(language or "").strip().lower().split("-", 1)[0]
    if not _LANGUAGE.fullmatch(source_language):
        source_language = "en"
    return f"https://patents.google.com/patent/{normalized}/{source_language}"


def source_publication_variants(publication_number: str) -> tuple[str, ...]:
    """Exact indexed spellings used by the active corpus, without a table scan."""
    normalized = normalize_publication_number(publication_number)
    match = _PARTS.match(normalized)
    if not match:
        return (normalized,) if normalized else ()
    authority, number, kind = match.groups()
    numbers = [number]
    if authority == "US" and len(number) == 11 and number[:4].isdigit():
        serial = number[4:]
        while serial.startswith("0") and len(serial) > 1:
            serial = serial[1:]
            numbers.append(f"{number[:4]}{serial}")
    values = []
    for candidate in numbers:
        values.extend((
            f"{authority}{candidate}{kind}",
            f"{authority}-{candidate}-{kind}",
        ))
    return tuple(dict.fromkeys(values))
