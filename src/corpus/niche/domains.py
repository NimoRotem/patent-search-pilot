"""Initial niche definition and deterministic inclusion signals.

Classifications seed discovery, but no classification is an inclusion wall. Family,
citation, co-classification, known-result, and terminology signals can all add a
publication that lacks CPC or IPC data.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .identifiers import normalize_classification


@dataclass(frozen=True)
class DomainGroup:
    name: str
    terms: tuple[str, ...]
    cpc_prefixes: tuple[str, ...] = ()
    ipc_prefixes: tuple[str, ...] = ()
    term_priority: int = 1
    classification_priority: int = 1


DOMAIN_GROUPS: tuple[DomainGroup, ...] = (
    DomainGroup(
        "vacuum_generation",
        ("vacuum generator", "vacuum pump", "ejector pump", "venturi vacuum"),
        ("F04B", "F04C", "F04F5", "B01D46"),
        ("F04B", "F04C", "F04F5", "B01D46"),
        classification_priority=4,
    ),
    DomainGroup(
        "vacuum_gripping",
        ("vacuum gripper", "vacuum gripping", "vacuum end effector"),
        ("B25J15/06", "B25B11/005"),
        ("B25J15/06", "B25B11/00"),
    ),
    DomainGroup(
        "suction_cups",
        ("suction cup", "suction cups", "suction gripper", "suction pad"),
        ("F16B47/00", "B25J15/0616"),
        ("F16B47/00", "B25J15/06"),
    ),
    DomainGroup(
        "vacuum_lifting",
        ("vacuum lifter", "vacuum lifting", "suction lifting"),
        ("B66C1/02", "B66C1/0225"),
        ("B66C1/02",),
    ),
    DomainGroup(
        "material_handling",
        ("material handling", "workpiece handling", "load handling"),
        ("B65G", "B66C1", "B25J"),
        ("B65G", "B66C1", "B25J"),
        classification_priority=4,
    ),
    DomainGroup(
        "conveying",
        ("conveyor", "conveying", "transfer conveyor", "pneumatic conveyor"),
        ("B65G47", "B65G49", "B65G53"),
        ("B65G47", "B65G49", "B65G53"),
        term_priority=4,
        classification_priority=4,
    ),
    DomainGroup(
        "robotic_manipulation",
        ("robotic manipulator", "robot gripper", "end effector", "robotic handling"),
        ("B25J9", "B25J15"),
        ("B25J9", "B25J15"),
    ),
    DomainGroup(
        "pick_and_place",
        ("pick and place", "pick-and-place", "pickup position", "placement head"),
        ("B25J9/16", "B65G47/90"),
        ("B25J9/16", "B65G47/90"),
    ),
    DomainGroup(
        "pneumatic_handling",
        ("pneumatic gripper", "pneumatic handling", "fluid pressure gripper"),
        ("B25J15/00", "B65G47/91"),
        ("B25J15/00", "B65G47/91"),
    ),
    DomainGroup(
        "handling_controls",
        ("gripper control", "vacuum monitoring", "grip detection", "handling controller"),
        ("B25J9/16", "G05B19", "G01L"),
        ("B25J9/16", "G05B19", "G01L"),
        term_priority=3,
        classification_priority=3,
    ),
)


_GRAPH_SIGNALS = frozenset(
    {"citation", "family", "known_result", "search_result", "co_classification"}
)


def all_cpc_prefixes(groups: Iterable[DomainGroup] = DOMAIN_GROUPS) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        normalize_classification(code)
        for group in groups
        for code in group.cpc_prefixes
        if normalize_classification(code)
    ))


def all_ipc_prefixes(groups: Iterable[DomainGroup] = DOMAIN_GROUPS) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        normalize_classification(code)
        for group in groups
        for code in group.ipc_prefixes
        if normalize_classification(code)
    ))


def priority_for_record(
    record,
    groups: Iterable[DomainGroup] = DOMAIN_GROUPS,
) -> int:
    groups = tuple(groups)
    cpc = [normalize_classification(code) for code in (
        getattr(record, "cpc_codes", ()) or ()
    )]
    ipc = [normalize_classification(code) for code in (
        getattr(record, "ipc_codes", ()) or ()
    )]
    text = f"{getattr(record, 'title', '')} {getattr(record, 'abstract', '')}".lower()
    priorities = []
    for group in groups:
        if any(term in text for term in group.terms):
            priorities.append(group.term_priority)
        if any(code.startswith(normalize_classification(prefix))
               for code in cpc for prefix in group.cpc_prefixes):
            priorities.append(group.classification_priority)
        if any(code.startswith(normalize_classification(prefix))
               for code in ipc for prefix in group.ipc_prefixes):
            priorities.append(group.classification_priority)
    fallback = min(4, max(1, int(getattr(record, "priority", 4) or 4)))
    return min((*priorities, fallback)) if priorities else fallback


def in_niche(record, groups: Iterable[DomainGroup] = DOMAIN_GROUPS) -> bool:
    signals = set(getattr(record, "discovery_signals", ()) or ())
    if signals & _GRAPH_SIGNALS:
        return True
    cpc = tuple(getattr(record, "cpc_codes", ()) or ())
    ipc = tuple(getattr(record, "ipc_codes", ()) or ())
    text = f"{getattr(record, 'title', '')} {getattr(record, 'abstract', '')}".lower()
    return any(
        any(term in text for term in group.terms)
        or any(normalize_classification(code).startswith(
            normalize_classification(prefix)
        ) for code in cpc for prefix in group.cpc_prefixes)
        or any(normalize_classification(code).startswith(
            normalize_classification(prefix)
        ) for code in ipc for prefix in group.ipc_prefixes)
        for group in groups
    )
