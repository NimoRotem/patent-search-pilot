"""Jurisdiction-neutral date/status engine (spec §5).

`priority_date <= cutoff` is insufficient. Each mode has its OWN date+status rules and
tags each candidate with the legal *basis* on which it qualifies as prior art, so the
report can caveat correctly (e.g. secret prior art is novelty-only, never inventive-step).

We deliberately avoid US-only labels like "102"/"103"; the concepts below map to EPC
Art.54(2) (public prior art), Art.54(3) (earlier-filed-later-published / whole-contents),
and Art.56 (inventive step) but are applied jurisdiction-neutrally.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from enum import Enum


class Mode(str, Enum):
    NOVELTY = "novelty"
    INVENTIVE_STEP = "inventive_step"
    INVALIDITY = "invalidity"      # stub
    FTO = "fto"                    # stub
    LANDSCAPE = "landscape"        # stub


class Basis(str, Enum):
    PUBLIC_PRIOR_ART = "public_prior_art"          # Art.54(2): published before EFD
    SECRET_PRIOR_ART = "secret_prior_art"          # Art.54(3): filed before, published after EFD
    PRIORITY_INTERVAL = "priority_interval"        # published between priority date and filing date
    NOT_PRIOR_ART = "not_prior_art"


@dataclass
class Subject:
    """The document under assessment (whose novelty/inventive-step we test)."""
    number: str
    efd: date                      # effective filing date = earliest priority date
    filing_date: date | None = None
    publication_date: date | None = None
    jurisdiction: str | None = None
    # If True, restrict Art.54(3) secret prior art to same jurisdiction (strict EPC-style).
    strict_secret_jurisdiction: bool = False


def citable_where(mode: Mode, s: Subject, pub_alias: str = "p"):
    """Return (sql_fragment, params) selecting corpus publications citable under `mode`.

    Operates on a `publications` row aliased as `pub_alias`. Uses publication_date and
    earliest_priority_date (both populated at ingest).
    """
    a = pub_alias
    if mode == Mode.NOVELTY:
        # Art.54(2) public art (published strictly before EFD) OR
        # Art.54(3) secret art (own priority/filing before EFD but published on/after EFD).
        frag = (
            f"( {a}.publication_date < %s "
            f"  OR ( COALESCE({a}.earliest_priority_date, {a}.filing_date) < %s "
            f"       AND {a}.publication_date >= %s ) )"
        )
        params = [s.efd, s.efd, s.efd]
        if s.strict_secret_jurisdiction and s.jurisdiction:
            # secret art only counts within the same patent system
            frag = (
                f"( {a}.publication_date < %s "
                f"  OR ( COALESCE({a}.earliest_priority_date, {a}.filing_date) < %s "
                f"       AND {a}.publication_date >= %s AND {a}.country = %s ) )"
            )
            params = [s.efd, s.efd, s.efd, s.jurisdiction]
        return frag, params

    if mode == Mode.INVENTIVE_STEP:
        # Only public prior art (Art.54(2)); secret prior art is EXCLUDED from inventive step.
        return f"{a}.publication_date < %s", [s.efd]

    if mode == Mode.INVALIDITY:
        # TODO: invalidity anchors to a GRANTED claim set + jurisdiction-specific grounds
        # (added matter, sufficiency, etc.) and its own date rules per ground. For the pilot
        # we conservatively reuse the novelty window; flagged so callers know it's provisional.
        return citable_where(Mode.NOVELTY, s, pub_alias)

    if mode == Mode.FTO:
        # TODO: FTO needs IN-FORCE, enforceable claims in the SELECTED jurisdictions as of a
        # date (requires normalized legal_status + term/lapse/SPC data we don't ingest in the
        # pilot). Do NOT use a prior-art window here. Stubbed.
        raise NotImplementedError(
            "FTO mode requires normalized legal-status (in-force claims per jurisdiction). "
            "Ingest legal_events + term data before enabling. (spec §5 stub)"
        )

    if mode == Mode.LANDSCAPE:
        # TODO: landscape is classification/assignee/time-trend driven, not a citability test.
        raise NotImplementedError("Landscape mode is a stub (spec §5).")

    raise ValueError(mode)


def classify_basis(candidate: dict, s: Subject) -> Basis:
    """Given a candidate publication row (dict with publication_date, earliest_priority_date,
    filing_date), tag WHY it is prior art vs the subject. Drives report caveats."""
    pub = candidate.get("publication_date")
    prio = candidate.get("earliest_priority_date") or candidate.get("filing_date")
    if pub is None:
        return Basis.NOT_PRIOR_ART
    if pub < s.efd:
        return Basis.PUBLIC_PRIOR_ART
    if prio is not None and prio < s.efd and pub >= s.efd:
        return Basis.SECRET_PRIOR_ART
    # priority-interval: subject has a priority date earlier than its own filing; a doc
    # published in that window matters only if the subject's priority claim is challenged.
    if s.filing_date and s.efd < s.filing_date and s.efd <= pub < s.filing_date:
        return Basis.PRIORITY_INTERVAL
    return Basis.NOT_PRIOR_ART


def usable_for(basis: Basis, mode: Mode) -> bool:
    """Whether a candidate with `basis` may be used under `mode`."""
    if mode == Mode.INVENTIVE_STEP:
        return basis == Basis.PUBLIC_PRIOR_ART           # secret art never for inventive step
    if mode in (Mode.NOVELTY, Mode.INVALIDITY):
        return basis in (Basis.PUBLIC_PRIOR_ART, Basis.SECRET_PRIOR_ART)
    return False


# --- Inventive-step combination scaffold (jurisdiction-neutral) -----------------------------
@dataclass
class ElementMapping:
    element: str                 # a claim element / feature
    publication_number: str      # reference that discloses it
    basis: Basis
    coord: dict                  # {claim_no|para_no|page_no}
    score: float                 # retrieval/rerank confidence


class CombinationBuilder:
    """Reports which reference supplies which claim element — the combinational view the
    inventive-step mode must output (spec §5, §7). Not '103'; jurisdiction-neutral."""

    def __init__(self, elements: list[str]):
        self.elements = elements
        self.mappings: dict[str, list[ElementMapping]] = {e: [] for e in elements}

    def add(self, m: ElementMapping):
        self.mappings.setdefault(m.element, []).append(m)

    def best_single_reference(self):
        """Fewest references covering the most elements — a greedy primary-reference pick."""
        by_ref: dict[str, set] = {}
        for el, ms in self.mappings.items():
            for m in ms:
                by_ref.setdefault(m.publication_number, set()).add(el)
        if not by_ref:
            return None, set()
        ref = max(by_ref, key=lambda r: len(by_ref[r]))
        return ref, by_ref[ref]

    def combination(self):
        """Greedy set-cover: primary reference + secondary refs supplying missing elements."""
        primary, covered = self.best_single_reference()
        result = {"primary": primary, "covers": sorted(covered), "secondaries": []}
        missing = set(self.elements) - covered
        by_ref: dict[str, set] = {}
        for el in missing:
            for m in self.mappings.get(el, []):
                by_ref.setdefault(m.publication_number, set()).add(el)
        while missing and by_ref:
            ref = max(by_ref, key=lambda r: len(by_ref[r] & missing))
            got = by_ref[ref] & missing
            if not got:
                break
            result["secondaries"].append({"ref": ref, "supplies": sorted(got)})
            missing -= got
        result["uncovered_elements"] = sorted(missing)
        return result
