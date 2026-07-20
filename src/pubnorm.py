"""Canonical publication-number normalizer + variant generator.

WHY THIS EXISTS
---------------
The same physical publication is written many ways across the sources we join, and one
difference is silently destructive: BigQuery stores US pre-grant application publications
with the LEADING ZERO of the 7-digit serial dropped. The "Flying Power-Washing Drone"
disclosure publication is a live example —

    corpus / BigQuery / report store it as   US2019168875A1   (US + 2019 + 168875)
    Google Patents & the lemad Mongo key it   US20190168875A1  (US + 2019 + 0168875)

A US pre-grant number is  US + YYYY(4) + serial(7) + kind. The serial is 7 digits, so when
its leading digit is a zero BigQuery's numeric handling collapses 11 digits to 10. A
`find_one({"publicationNumber": "US2019168875A1"})` therefore returns NOTHING while the
padded key returns the document with all four figures. That single format gap is why our
live reports showed zero drawings for a publication whose sketches exist.

WHAT IT DOES
------------
Given ANY spelling of a publication number (hyphenated corpus form `US-2019168875-A1`, the
concatenated report form `US2019168875A1`, the padded Google/Mongo form `US20190168875A1`,
spaced `US 2019168875 A1`, mixed case, with or without a kind code) it produces:

  * `canonical(pub)`  -> our filesystem/DB-safe hyphenated key `CC-NUMBER[-KIND]` (identical
                         to ingest_input.normalize_pub; NO padding, digits preserved as given).
  * `mongo_candidates(pub)` -> the ORDERED, de-duplicated list of concatenated keys to try
                         against Mongo / Google, most-likely first. For US pre-grant numbers
                         this puts the ZERO-PADDED 11-digit key first, which is the fix.

Pure string work. NEVER fetches anything (SSRF-safe), NEVER raises on junk (returns None /
an empty list), so it is safe to call from the request path.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# CC + digits + optional kind (letter then up to two digits, e.g. A, A1, B2, C0, U1, T3).
_PUB_RE = re.compile(r"^([A-Z]{2})([0-9]{2,})([A-Z][0-9]{0,2})?$")

# US pre-grant application publications are  US + 4-digit year + 7-digit serial.
# The first were published in 2001; keep the window wide so we never mis-classify a real one.
_US_PREGRANT_YEAR_MIN = 1999
_US_PREGRANT_YEAR_MAX = 2035
_US_PREGRANT_SERIAL_LEN = 7


def _strip(s) -> str:
    """Uppercase and drop everything that is not a letter or digit."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def parse(pub) -> Optional[Tuple[str, str, str]]:
    """(country, digits, kind) from any spelling, or None if it is not a plausible pub number.

    `kind` is "" when absent. Digits are returned exactly as written (no padding)."""
    t = _strip(pub)
    if not t or len(t) > 40:
        return None
    m = _PUB_RE.match(t)
    if not m:
        return None
    cc, num, kind = m.groups()
    return cc, num, (kind or "")


def canonical(pub) -> Optional[str]:
    """Our corpus-canonical hyphenated key `CC-NUMBER[-KIND]`.

    Matches ingest_input.normalize_pub exactly so the same string names the Postgres row, the
    figure directory and the on-disk cache. Digits are NOT padded here — this preserves whatever
    form the corpus stored, which is what the filesystem / DB already use as keys."""
    p = parse(pub)
    if not p:
        return None
    cc, num, kind = p
    return f"{cc}-{num}-{kind}" if kind else f"{cc}-{num}"


def _us_pregrant_num_variants(cc: str, num: str) -> List[str]:
    """Zero-padding variants of a US pre-grant serial (the dropped-leading-zero fix).

    Returns num spellings most-canonical first: the zero-PADDED 11-digit form, the given form,
    and the zero-STRIPPED form. Empty when this is not a US pre-grant number, so callers can
    unconditionally splice it in."""
    if cc != "US" or not (10 <= len(num) <= 11):
        return []
    year = num[:4]
    try:
        y = int(year)
    except ValueError:
        return []
    if not (_US_PREGRANT_YEAR_MIN <= y <= _US_PREGRANT_YEAR_MAX):
        return []
    serial = num[4:]
    if len(serial) > _US_PREGRANT_SERIAL_LEN:
        return []
    padded = year + serial.zfill(_US_PREGRANT_SERIAL_LEN)          # US + 2019 + 0168875
    stripped = year + (serial.lstrip("0") or "0")                 # US + 2019 + 168875
    out = []
    for v in (padded, num, stripped):
        if v not in out:
            out.append(v)
    return out


def _num_variants(cc: str, num: str) -> List[str]:
    """All digit spellings to try for this number, most-canonical first."""
    variants = _us_pregrant_num_variants(cc, num)
    if num not in variants:
        variants.append(num)
    return variants


def mongo_candidates(pub) -> List[str]:
    """Ordered, de-duplicated concatenated keys to try against Mongo / Google.

    Mongo (`publicationNumber`) and Google key on the concatenated form WITH kind code, e.g.
    `US20190168875A1`. We therefore emit, in priority order:
      1. concatenated WITH kind, for each number variant (padded first) — the real Mongo keys;
      2. concatenated WITHOUT kind, for each number variant — a few docs omit the kind;
      3. the hyphenated forms last, as a cheap belt-and-braces in case a source stored those.
    """
    p = parse(pub)
    if not p:
        return []
    cc, num, kind = p
    nums = _num_variants(cc, num)
    out: List[str] = []

    def add(x: str):
        if x and x not in out:
            out.append(x)

    if kind:
        for n in nums:
            add(f"{cc}{n}{kind}")            # US20190168875A1  <-- the fix, tried first
    for n in nums:
        add(f"{cc}{n}")                      # US20190168875 (kindless fallback)
    if kind:
        for n in nums:
            add(f"{cc}-{n}-{kind}")          # US-20190168875-A1 (hyphenated, unlikely)
    for n in nums:
        add(f"{cc}-{n}")
    return out


def variants(pub) -> List[str]:
    """Alias kept for callers that want every candidate spelling (same as mongo_candidates)."""
    return mongo_candidates(pub)
