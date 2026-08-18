"""Common candidate / query schema shared by every source adapter.

Ported from patents-app `schema.py` (the subset the adapters and the fulltext
ladder actually use). The federation layer speaks these dataclasses: adapters
map their heterogeneous JSON into `Candidate`; the sync facade flattens
`Candidate` back into plain dicts for the pilot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Query planning
# ---------------------------------------------------------------------------

@dataclass
class SubQuery:
    """One native query bundle aimed at one source.

    `native` is already in the source's own syntax (Google Patents boolean,
    OPS CQL, PQAI natural language, HimmPat postfix expressions, ...).
    """
    source: str
    native: Any                    # str for most; dict for JSON-query sources
    rationale: str = ""            # why the planner emitted this query
    element: str = ""              # which invention element it targets
    cpc: list[str] = field(default_factory=list)
    date_from: Optional[str] = None
    date_to: Optional[str] = None

    def cache_key(self) -> str:
        native = self.native
        if isinstance(native, (dict, list)):
            import json
            native = json.dumps(native, sort_keys=True, separators=(",", ":"))
        norm = re.sub(r"\s+", " ", str(native).strip().lower())
        return f"{self.source}::{norm}"


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    pub_number: str
    source: str
    source_rank: int
    title: str = ""
    abstract: str = ""
    snippet: str = ""
    assignee: str = ""
    inventors: list[str] = field(default_factory=list)
    date: str = ""                 # best available date (publication/priority)
    priority_date: str = ""
    kind: str = ""                 # kind code / doc type
    cpc: list[str] = field(default_factory=list)
    url: str = ""
    family_id: str = ""            # INPADOC / simple family id if the source gives one
    found_by: list[str] = field(default_factory=list)  # sub-query rationales
    extra: dict = field(default_factory=dict)          # source-specific detail/claims

    def norm_pub(self) -> str:
        return normalize_pub(self.pub_number)


def dataclass_to_dict(obj) -> dict:
    return asdict(obj)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_PUB_RE = re.compile(r"[^A-Z0-9]")


def normalize_pub(pub: str) -> str:
    """US-9876543-B2 / US9876543B2 / patent/US9876543B2/en -> US9876543B2."""
    if not pub:
        return ""
    pub = str(pub)
    if "patent/" in pub:
        pub = pub.split("patent/", 1)[1].split("/")[0]
    return _PUB_RE.sub("", pub.upper())


_US_APP_RE = re.compile(r"^US(19\d\d|20\d\d)(\d{4,7})(A[19])$")


def canonical_pub(pub: str) -> str:
    """Normalize + repair US pre-grant publication numbers. The patents-public-data
    dataset drops the leading zero of the 7-digit sequence (stores US-2019183307-A1
    for US-2019-0183307-A1), which 404s on Google Patents. Zero-pad the sequence
    back to 7 digits so US20190183307A1 is produced. Other formats pass through."""
    pn = normalize_pub(pub)
    m = _US_APP_RE.match(pn)
    if m:
        return f"US{m.group(1)}{int(m.group(2)):07d}{m.group(3)}"
    return pn


def google_url(pub: str) -> str:
    """Canonical Google Patents URL (repaired number + /en, which Google requires
    for the direct /patent/ route)."""
    return f"https://patents.google.com/patent/{canonical_pub(pub)}/en"


_KIND_SUFFIX_RE = re.compile(r"^[A-Z]{2}\d+([A-Z]\d?)$")


def kind_of(pub: str, kind: str = "") -> str:
    """Kind code for a publication: explicit `kind` else parsed from the number."""
    if kind:
        return kind.upper()
    m = _KIND_SUFFIX_RE.match(normalize_pub(pub))
    return m.group(1) if m else ""


def country_of(pub: str) -> str:
    m = re.match(r"^([A-Z]{2})", normalize_pub(pub))
    return m.group(1) if m else ""


def is_design(pub: str, kind: str = "") -> bool:
    """Design patent / registration? US design patents are 'USDnnnnnnnS' (kind S);
    other offices use S/design kind codes."""
    np = normalize_pub(pub)
    k = kind_of(pub, kind)
    if k.startswith("S"):
        return True
    return bool(re.match(r"^USD\d", np))


def espacenet_url(pub: str, family_id: str = "") -> str:
    """Espacenet deep link for a publication.

    Two forms, both live:

      family-scoped (preferred when we know the DOCDB simple family):
        .../patent/search/family/007128644/publication/DE1286275B?q=pn%3DDE1286275B
      publication-scoped (fallback):
        .../patent/search/publication/DE1286275B?q=pn%3DDE1286275B

    The family form lands on the same document but with the family panel already
    populated. Espacenet zero-pads the family id to NINE digits in the path — OPS
    returns it unpadded ('07128644' for DE1286275B), so pad it here or the link
    breaks. The query is `pn=<number>`, not a bare term: a bare term is a full-text
    search that can land on the wrong document, whereas `pn=` is exact.
    """
    p = canonical_pub(pub)
    q = f"?q=pn%3D{p}"
    fid = re.sub(r"\D", "", str(family_id or ""))
    if fid:
        return (f"https://worldwide.espacenet.com/patent/search/family/{fid.zfill(9)}"
                f"/publication/{p}{q}")
    return f"https://worldwide.espacenet.com/patent/search/publication/{p}{q}"


def designview_url(pub: str) -> str:
    """EUIPO DesignView search for a design registration/number."""
    p = canonical_pub(pub)
    return f"https://www.tmdn.org/tmdsview-web/welcome?st={p}#/dsview/results?st={p}"


def best_source_url(pub: str, kind: str = "") -> str:
    """The source that actually carries full text + drawings for this document —
    NOT always Google Patents. Espacenet has complete EP/WO originals (incl. the
    drawings Google drops); EU design registrations go to EUIPO DesignView; US and
    the rest stay on Google Patents (good coverage, fast)."""
    cc = country_of(pub)
    if is_design(pub, kind) and cc not in ("US",):
        return designview_url(pub)
    if cc in ("EP", "WO"):
        return espacenet_url(pub)
    return google_url(pub)


def source_name_for(pub: str, kind: str = "") -> str:
    cc = country_of(pub)
    if is_design(pub, kind) and cc not in ("US",):
        return "EUIPO DesignView"
    if cc in ("EP", "WO"):
        return "Espacenet"
    return "Google Patents"


_DATE_RE = re.compile(r"(\d{4})\D?(\d{2})\D?(\d{2})")
_YEAR_RE = re.compile(r"(1[6-9]\d\d|20\d\d|21\d\d)")


def iso_date(v) -> str:
    """Any source's date spelling -> YYYY-MM-DD, or "" .

    Every adapter used to pass through whatever its source emitted: SerpApi ISO strings, USPTO
    grantDate, PQAI publication_date, BigQuery integers. Downstream code then compared them as
    strings and sorted them, which silently mis-orders as soon as two formats meet, and the date
    filter that decides whether a document is even prior art parses them. One normaliser, applied
    at the adapter boundary, is the only place this can be got right once.

    Falls back to a bare year when that is all the source gave, because a year is still usable for
    an eligibility window and dropping it entirely is worse.
    """
    s = str(v or "").strip()
    if not s:
        return ""
    m = _DATE_RE.search(s)
    if m:
        y, mo, d = m.groups()
        try:
            if 1600 <= int(y) <= 2200 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{mo}-{d}"
        except ValueError:
            pass
    m = _YEAR_RE.search(s)
    return m.group(1) if m else ""
