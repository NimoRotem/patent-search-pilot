"""Bounded graph expansion for the niche publication universe."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from .domains import DOMAIN_GROUPS, priority_for_record
from .manifest import PublicationRecord, merge_publications


@dataclass(frozen=True)
class DiscoverySummary:
    publications_seen: int
    families_seen: int
    watermarks: dict[str, int]


def _with_priority(records: Iterable[PublicationRecord], priority: int, signal: str):
    for record in records or ():
        signals = tuple(sorted(set(record.discovery_signals) | {signal}))
        yield replace(record, priority=min(record.priority, priority), discovery_signals=signals)


def _family_expansion(source, records: Iterable[PublicationRecord], default_priority: int):
    roots = list(records)
    priorities = {
        record.family_id: min(record.priority, default_priority)
        for record in roots
        if record.family_id
    }
    for member in source.family_members(roots):
        priority = priorities.get(member.family_id, default_priority)
        yield from _with_priority((member,), priority, "family")


class DiscoveryEngine:
    """Expand seed CPC/IPC hits without treating classification as a hard boundary."""

    def __init__(self, source, manifest, *, groups=DOMAIN_GROUPS, batch_size: int = 1000,
                 extra_records=()):
        self.source = source
        self.manifest = manifest
        self.groups = tuple(groups)
        self.batch_size = max(1, int(batch_size))
        self.extra_records = tuple(extra_records)

    def run(self) -> DiscoverySummary:
        watermarks = dict(self.manifest.load_watermarks())
        seeds, advanced = self.source.seed_records(self.groups, watermarks, self.batch_size)
        seeded = [
            replace(record, priority=priority_for_record(record, self.groups))
            for record in _with_priority(seeds, 4, "seed")
        ]
        seeded.extend(_with_priority(self.extra_records, 1, "known_result"))
        family = list(_family_expansion(self.source, seeded, 4))
        core = list(merge_publications((*seeded, *family)).values())
        strong_core = [record for record in core if record.priority == 1]
        cited = list(_with_priority(self.source.citations(strong_core), 2, "citation"))
        cited_family = list(_family_expansion(self.source, cited, 2))
        adjacent = list(_with_priority(
            self.source.co_classified(strong_core), 3, "co_classification"
        ))
        adjacent_family = list(_family_expansion(self.source, adjacent, 3))
        records = list(merge_publications((
            *core,
            *cited,
            *cited_family,
            *adjacent,
            *adjacent_family,
        )).values())
        self.manifest.upsert_publications(records)
        self.manifest.save_watermarks(advanced)
        families = {
            record.family_id or f"publication:{record.publication_number}" for record in records
        }
        return DiscoverySummary(len(records), len(families), dict(advanced))


def main(argv=None) -> int:
    from .cli import run_discover
    return run_discover(argv)


if __name__ == "__main__":
    raise SystemExit(main())
