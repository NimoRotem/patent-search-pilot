from __future__ import annotations

from corpus.niche.discover import DiscoveryEngine
from corpus.niche.domains import (
    DOMAIN_GROUPS,
    all_ipc_prefixes,
    in_niche,
    priority_for_record,
)
from corpus.niche.identifiers import source_publication_variants
from corpus.niche.manifest import (
    PublicationRecord,
    choose_family_fetch_targets,
    family_key,
    merge_publications,
)
from corpus.niche.providers.local import LocalDiscoverySource


def _record(publication_number: str, **values) -> PublicationRecord:
    return PublicationRecord(publication_number=publication_number, **values)


def test_manifest_deduplication_merges_richer_fields_and_signals():
    records = [
        _record(
            "US-2020-0123456-A1",
            title="Vacuum gripper",
            cpc_codes=("B25J15/0616",),
            discovery_signals=("cpc",),
        ),
        _record(
            "US20200123456A1",
            abstract="A suction end effector.",
            cpc_codes=("B65G47/91",),
            discovery_signals=("citation",),
        ),
    ]

    merged = merge_publications(records)

    assert list(merged) == ["US20200123456A1"]
    assert merged["US20200123456A1"].title == "Vacuum gripper"
    assert merged["US20200123456A1"].abstract == "A suction end effector."
    assert merged["US20200123456A1"].cpc_codes == ("B25J15/0616", "B65G47/91")
    assert merged["US20200123456A1"].discovery_signals == ("citation", "cpc")


def test_manifest_preserves_terminal_status_and_marks_owned_full_text_complete():
    failed = _record("US1234567A1", fetch_status="failed")
    rediscovered = _record("US1234567A1")
    complete = _record(
        "EP1234567A1",
        has_complete_claims=True,
        has_complete_description=True,
    )

    merged = merge_publications((failed, rediscovered, complete))

    assert merged["US1234567A1"].fetch_status == "failed"
    assert merged["EP1234567A1"].fetch_status == "completed"


def test_family_normalization_is_stable_with_and_without_provider_family():
    assert family_key(" 07128644 ", "EP-1234567-A1") == "family:07128644"
    assert family_key("", "EP-1234567-A1") == "publication:EP1234567A1"


def test_source_publication_variants_include_indexed_hyphenated_and_us_forms():
    assert source_publication_variants("DE1286275B")[:2] == (
        "DE1286275B",
        "DE-1286275-B",
    )
    variants = source_publication_variants("US20190168875A1")
    assert "US-20190168875-A1" in variants
    assert "US-2019168875-A1" in variants


def test_preferred_family_publication_values_full_text_then_english():
    records = [
        _record(
            "DE1000001A1",
            family_id="42",
            language="de",
            has_claims=True,
            has_complete_claims=True,
            has_description=True,
            has_complete_description=True,
        ),
        _record(
            "US20200000001A1",
            family_id="42",
            language="en",
            has_claims=True,
            has_complete_claims=True,
            has_description=True,
            has_complete_description=True,
        ),
        _record(
            "EP1000001A1",
            family_id="42",
            language="en",
            has_claims=True,
            has_description=False,
        ),
    ]

    targets = choose_family_fetch_targets(records)

    assert [r.publication_number for r in targets] == ["US20200000001A1"]


def test_unclassified_citation_is_inside_the_manifest_universe():
    record = _record(
        "WO2024000001A1",
        title="Unclassified transfer head",
        discovery_signals=("citation",),
        priority=2,
    )

    assert in_niche(record) is True


def test_domain_groups_cover_the_required_initial_scope():
    names = {group.name for group in DOMAIN_GROUPS}

    assert {
        "vacuum_generation",
        "vacuum_gripping",
        "suction_cups",
        "vacuum_lifting",
        "material_handling",
        "conveying",
        "robotic_manipulation",
        "pick_and_place",
        "pneumatic_handling",
        "handling_controls",
    } <= names
    assert all(group.ipc_prefixes for group in DOMAIN_GROUPS)
    assert "B66C1/02" in all_ipc_prefixes()


def test_ipc_is_an_independent_seed_signal():
    record = _record(
        "GB2000001A",
        title="Unrelated wording",
        ipc_codes=("B66C1/02",),
        discovery_signals=("ipc",),
    )

    assert in_niche(record) is True


def test_broad_handling_classification_stays_priority_four():
    broad = _record("EP1000001A1", cpc_codes=("B65G1/00",))
    strong = _record("EP1000002A1", cpc_codes=("B25J15/06",))
    textual = _record("EP1000003A1", title="Vacuum gripper for glass sheets")

    assert priority_for_record(broad) == 4
    assert priority_for_record(strong) == 1
    assert priority_for_record(textual) == 1


def test_discovery_scan_controls_have_hard_upper_bounds():
    source = LocalDiscoverySource(
        lambda: None,
        id_window=10**12,
        graph_limit=10**12,
    )

    assert source.id_window <= 250_000
    assert source.graph_limit <= 20_000


class _DiscoverySource:
    def seed_records(self, _groups, _watermarks, _limit):
        return [
            _record(
                "EP1000001A1",
                family_id="F1",
                title="Vacuum gripper",
                cpc_codes=("B25J15/06",),
                discovery_signals=("cpc",),
            )
        ], {"B25J15/06": 1}

    def family_members(self, _records):
        return [
            _record(
                "WO1000001A1",
                family_id="F1",
                title="Transfer head",
                discovery_signals=("family",),
            )
        ]

    def citations(self, _records):
        return [
            _record(
                "JP1000001A",
                title="Unclassified suction controller",
                discovery_signals=("citation",),
            )
        ]

    def co_classified(self, _records):
        return [
            _record(
                "DE1000002A1",
                title="Pneumatic transfer device",
                discovery_signals=("co_classification",),
            )
        ]


class _ManifestSink:
    def __init__(self):
        self.rows = {}
        self.watermarks = {}

    def load_watermarks(self):
        return dict(self.watermarks)

    def upsert_publications(self, records):
        self.rows.update(merge_publications([*self.rows.values(), *records]))

    def save_watermarks(self, watermarks):
        self.watermarks.update(watermarks)


def test_discovery_expands_cpc_to_family_citations_and_co_classification_idempotently():
    sink = _ManifestSink()
    engine = DiscoveryEngine(source=_DiscoverySource(), manifest=sink, batch_size=100)

    first = engine.run()
    second = engine.run()

    assert first.publications_seen == 4
    assert second.publications_seen == 4
    assert set(sink.rows) == {
        "EP1000001A1", "WO1000001A1", "JP1000001A", "DE1000002A1"
    }
    assert sink.rows["JP1000001A"].priority == 2
    assert sink.rows["DE1000002A1"].priority == 3


def test_discovery_expands_families_of_cited_and_adjacent_publications():
    class Source:
        def seed_records(self, _groups, _watermarks, _limit):
            return [_record("EP1000001A1", family_id="CORE", priority=1)], {"publication_id": 1}

        def family_members(self, records):
            families = {record.family_id for record in records}
            output = []
            if "CITED" in families:
                output.append(_record("US2000001A1", family_id="CITED"))
            if "ADJACENT" in families:
                output.append(_record("WO3000001A1", family_id="ADJACENT"))
            return output

        def citations(self, _records):
            return [_record("JP2000001A", family_id="CITED")]

        def co_classified(self, _records):
            return [_record("DE3000001A1", family_id="ADJACENT")]

    sink = _ManifestSink()

    DiscoveryEngine(source=Source(), manifest=sink).run()

    assert sink.rows["US2000001A1"].priority == 2
    assert sink.rows["WO3000001A1"].priority == 3
