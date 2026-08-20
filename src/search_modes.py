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
    INVALIDITY = "invalidity"      # NOT AVAILABLE — see MODE_STATUS
    FTO = "fto"                    # NOT AVAILABLE — see MODE_STATUS
    LANDSCAPE = "landscape"        # NOT AVAILABLE — see MODE_STATUS


# --- mode availability ----------------------------------------------------------------------
# DECISION (M9): INVALIDITY and FTO REFUSE TO RUN rather than returning a plausible-looking
# wrong answer.
#
# Previously INVALIDITY silently delegated to the NOVELTY date window. That is dangerous in a
# way the user cannot detect: the window it produced was legally *defensible* for the novelty
# ground alone, but the answer was presented as an invalidity opinion, implying grounds
# (added matter, sufficiency, entitlement to priority, formal grounds) that this system never
# evaluates. A user reading "invalidity: no results" would reasonably conclude the patent is
# not invalidatable, when in fact only one of several grounds was ever tested.
#
# FTO is worse: it is not a prior-art question at all. FTO asks which claims are IN FORCE and
# ENFORCEABLE in chosen jurisdictions today, which needs normalized legal status — term,
# lapse, annuity payments, SPC extensions, oppositions. The corpus holds 1,618 legal_events
# rows against 107,795 publications (1.5% coverage), so it cannot be answered even
# approximately. A prior-art date window here is not an approximation, it is a category error.
#
# We refuse EXPLICITLY and STRUCTURALLY (a typed exception + a machine-readable registry)
# rather than by raising a bare NotImplementedError, because the caller needs to turn this into
# a user-visible message. ModeNotAvailable subclasses NotImplementedError so any existing
# `except NotImplementedError` handler keeps working.


class ModeNotAvailable(NotImplementedError):
    """A mode that is defined but deliberately refuses to run, because it cannot be computed
    correctly with the data available. Carries a user-facing explanation."""

    def __init__(self, mode, message: str, missing: list[str] | None = None):
        self.mode = mode
        self.message = message
        self.missing = missing or []
        super().__init__(message)


@dataclass(frozen=True)
class ModeCapability:
    available: bool
    label: str
    summary: str                        # what the mode does / would do
    reason: str = ""                    # why it is unavailable (empty when available)
    missing: tuple = ()                 # data/features required before it can be enabled


MODE_STATUS = {
    Mode.NOVELTY: ModeCapability(
        available=True, label="Novelty",
        summary="Art.54(2) public prior art plus Art.54(3) secret / earlier-filed art."),
    Mode.INVENTIVE_STEP: ModeCapability(
        available=True, label="Inventive step",
        summary="Art.56 — public prior art only; secret art is excluded by law."),
    Mode.INVALIDITY: ModeCapability(
        available=False, label="Invalidity",
        summary="A full invalidity opinion against a granted claim set.",
        reason=("Invalidity is not implemented. It requires the granted claim set as the "
                "anchor and evaluates jurisdiction-specific grounds beyond novelty and "
                "inventive step. Running it on the novelty date window alone would return a "
                "confident-looking answer that silently ignores most grounds. "
                "Use Novelty or Inventive step, which are computed correctly."),
        missing=("granted claim-set anchoring", "added-matter / sufficiency grounds",
                 "priority-entitlement analysis", "per-ground date rules")),
    Mode.FTO: ModeCapability(
        available=False, label="Freedom to operate",
        summary="Which in-force claims in chosen jurisdictions a product might infringe.",
        reason=("Freedom-to-operate is not implemented. It is not a prior-art search: it needs "
                "the legal status of every candidate — in force, lapsed, term, SPC extension, "
                "per jurisdiction. Legal-status coverage in this corpus is sparse and is not "
                "normalized for enforceability, so an FTO answer would be unsound rather than merely "
                "incomplete. No date window can substitute for it."),
        missing=("normalized legal_events coverage", "patent term + lapse/annuity data",
                 "SPC / term-extension data", "per-jurisdiction claim scope")),
    Mode.LANDSCAPE: ModeCapability(
        available=False, label="Landscape",
        summary="Classification / assignee / time-trend view of a technology area.",
        reason=("Landscape is not implemented. It is an aggregation view, not a citability "
                "test, and shares none of this engine's date logic."),
        missing=("aggregation + trend pipeline",)),
}


def available_modes() -> list:
    """Machine-readable capability list — drives the UI's mode picker and the API's allowlist.
    -> [{"mode","label","summary","available","reason","missing"}]"""
    return [{"mode": m.value, "label": c.label, "summary": c.summary,
             "available": c.available, "reason": c.reason, "missing": list(c.missing)}
            for m, c in MODE_STATUS.items()]


def require_available(mode) -> "Mode":
    """Validate a mode and refuse the unavailable ones. Call this at the API boundary BEFORE
    starting any work, so the refusal is immediate and user-visible.
    Raises ValueError for an unknown mode, ModeNotAvailable for a defined-but-refused one."""
    try:
        m = Mode(mode) if not isinstance(mode, Mode) else mode
    except ValueError:
        raise ValueError(f"unknown mode {mode!r}; valid modes: "
                         f"{', '.join(x.value for x in Mode)}")
    cap = MODE_STATUS[m]
    if not cap.available:
        raise ModeNotAvailable(m, cap.reason, list(cap.missing))
    return m


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
        # Art.54(2) public art (published STRICTLY BEFORE EFD) OR Art.54(3) secret art (own
        # priority/filing STRICTLY BEFORE EFD AND published STRICTLY AFTER EFD). Same-day-as-EFD
        # publication is NOT prior art (matches classify_basis; conservative, never over-flags).
        frag = (
            f"( {a}.publication_date < %s "
            f"  OR ( COALESCE({a}.earliest_priority_date, {a}.filing_date) < %s "
            f"       AND {a}.publication_date > %s ) )"
        )
        params = [s.efd, s.efd, s.efd]
        if s.strict_secret_jurisdiction and s.jurisdiction:
            # secret art only counts within the same patent system
            frag = (
                f"( {a}.publication_date < %s "
                f"  OR ( COALESCE({a}.earliest_priority_date, {a}.filing_date) < %s "
                f"       AND {a}.publication_date > %s AND {a}.country = %s ) )"
            )
            params = [s.efd, s.efd, s.efd, s.jurisdiction]
        return frag, params

    if mode == Mode.INVENTIVE_STEP:
        # Only public prior art (Art.54(2)); secret prior art is EXCLUDED from inventive step.
        return f"{a}.publication_date < %s", [s.efd]

    # INVALIDITY / FTO / LANDSCAPE: refuse. Previously INVALIDITY silently returned the
    # NOVELTY window here, which produced an answer the user could not tell was partial.
    # See MODE_STATUS above for the full reasoning.
    try:                                    # coerce so a bare string key still resolves
        cap = MODE_STATUS.get(Mode(mode))
    except ValueError:
        cap = None
    if cap is not None and not cap.available:
        raise ModeNotAvailable(mode, cap.reason, list(cap.missing))

    raise ValueError(mode)


def classify_basis(candidate: dict, s: Subject) -> Basis:
    """Given a candidate publication row (dict with publication_date, earliest_priority_date,
    filing_date), tag WHY it is prior art vs the subject `s`. Drives report caveats.

    DATE RULE (decided M8, conservative + defensible — never OVER-flags prior art):
      * Public prior art (Art.54(2)): published STRICTLY BEFORE the subject's effective filing
        date (EFD). A same-day publication is NOT "before", so NOT prior art.
      * Secret prior art (Art.54(3), novelty-only): the reference's own effective filing date is
        STRICTLY BEFORE the subject EFD, AND it was published STRICTLY AFTER the subject EFD (an
        earlier-filed, later-published conflicting application). Same-day publication is excluded.
      * A reference published EXACTLY on the subject EFD is NOT prior art (may still be
        priority-interval art if the subject claims priority and the reference sits strictly
        inside (priority, filing)).
    Own-family members are handled by the caller (retrieval excludes the subject's own family);
    by date logic a same-priority family member returns NOT_PRIOR_ART here anyway.
    """
    pub = candidate.get("publication_date")
    prio = candidate.get("earliest_priority_date") or candidate.get("filing_date")
    if pub is None or s.efd is None:
        return Basis.NOT_PRIOR_ART
    if pub < s.efd:                                            # strictly before EFD
        return Basis.PUBLIC_PRIOR_ART
    if prio is not None and prio < s.efd and pub > s.efd:      # Art.54(3): filed before, pub AFTER
        return Basis.SECRET_PRIOR_ART
    # priority-interval: subject claims priority (efd < filing); a doc published STRICTLY inside
    # (priority, filing) matters only if the subject's priority claim is later found invalid.
    if s.filing_date and s.efd < s.filing_date and s.efd < pub < s.filing_date:
        return Basis.PRIORITY_INTERVAL
    return Basis.NOT_PRIOR_ART


def usable_for(basis: Basis, mode: Mode) -> bool:
    """Whether a candidate with `basis` may be used under `mode`."""
    if mode == Mode.INVENTIVE_STEP:
        return basis == Basis.PUBLIC_PRIOR_ART           # secret art never for inventive step
    if mode in (Mode.NOVELTY, Mode.INVALIDITY):
        return basis in (Basis.PUBLIC_PRIOR_ART, Basis.SECRET_PRIOR_ART)
    return False


#  Which offices' later-published applications count as secret prior art IN A GIVEN FORUM.
#  Dates alone do not decide this and the classification above is deliberately date-only, so the
#  jurisdiction question is asked separately and by whoever knows the forum.
#
#  US, 35 U.S.C. 102(a)(2): "an application for patent, published or deemed published under
#  section 122(b)" plus patents issued under 151. That is a US patent, a US pre-grant publication,
#  and a PCT application designating the United States (deemed published under 122(b); the AIA
#  dropped the pre-AIA 102(e) requirement that it be in English). It is NOT a JP, CN, DE, EP or KR
#  national publication: those are prior art in the US only if they PUBLISHED before the subject's
#  effective filing date, which makes them 102(a)(1) art and not this.
#
#  EPO, EPC Art. 54(3): only a European patent application filed before and published after. A US
#  or JP later-published application is not Art. 54(3) art at all.
_SECRET_ART_FORUM = {
    "US": {"US", "WO"},
    "EP": {"EP", "WO"},          # a Euro-PCT entering the EP phase is an Art. 54(3) right
}


def secret_art_reaches(country, forum="US") -> bool:
    """Is a later-published application from `country` secret prior art in `forum`?

    Only asked of a candidate already classified SECRET_PRIOR_ART on the dates. A candidate that is
    PUBLIC_PRIOR_ART published before the subject's effective filing date is prior art everywhere
    and this question does not arise.
    """
    allowed = _SECRET_ART_FORUM.get(str(forum or "US").upper())
    if allowed is None:
        return True                                     # unknown forum: do not invent a bar
    return str(country or "").upper() in allowed


def secret_art_note(country, forum="US") -> str:
    """Why a later-published application from `country` is unavailable in `forum`, or ""."""
    if secret_art_reaches(country, forum):
        return ""
    c = str(country or "?").upper()
    if str(forum or "US").upper() == "US":
        return ("This document published AFTER the target's effective filing date, so it can only "
                "be reached as 35 U.S.C. 102(a)(2) art — and 102(a)(2) reaches US patents, US "
                "pre-grant publications and PCT applications designating the United States. A %s "
                "national publication is not among them, so it is not prior art in the United "
                "States on these dates." % c)
    return ("This document published after the target's effective filing date. Secret prior art in "
            "the %s forum does not extend to a %s national publication."
            % (str(forum).upper(), c))


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
