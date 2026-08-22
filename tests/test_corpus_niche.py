"""The niche boundary, the manifest record and the manifest reader. No database.

Every test here is about a decision that has already been got wrong once in this repo or that a
consumer depends on: an indexing code counted as a field, an applicant IDS dump counted as a search
result, "claims in one member and a description in another" counted as complete text, and a reader
opening a part file that is still being written.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import corpus_niche as cn  # noqa: E402
from corpus_niche import Boundary, ManifestWriter, build_record  # noqa: E402

CONFIG = os.path.join(ROOT, "config", "niche_boundary.json")


def boundary(**over):
    spec = {
        "core_subclasses": ["B65G", "B25J"],
        "adjacent_groups": ["F04F5", "H10P72"],
        "closures": {"family": True,
                     "citation": {"enabled": True, "categories": ["SEA", "EXA", "ISR"],
                                  "origins": ["X", "Y"]}},
    }
    spec.update(over)
    return Boundary(spec)


# ---------------------------------------------------------------- symbols
@pytest.mark.parametrize("raw,norm,sub,group", [
    ("B25J 15/0616", "B25J15/0616", "B25J", "B25J15"),
    ("b65g47/91", "B65G47/91", "B65G", "B65G47"),
    ("F16B47/00", "F16B47/00", "F16B", "F16B47"),
    ("H10P72/00", "H10P72/00", "H10P", "H10P72"),
    ("  B66C1/0225 ", "B66C1/0225", "B66C", "B66C1"),
    ("B25", "", "unclassified", ""),
    (None, "", "unclassified", ""),
])
def test_symbol_parsing(raw, norm, sub, group):
    assert cn.normalise_symbol(raw) == norm
    assert cn.subclass_of(raw) == sub
    assert cn.main_group_of(raw) == group


def test_subclass_of_is_domain_of_to_the_letter():
    """The integration hazard workstream G found. `shard_router` emits the literal
    "unclassified" for a publication with no symbols; a manifest that named the same shard `""`
    would register a shard `hot_domains` never matches, and 1,024,320 publications, 20.6% of the
    corpus, would go unreachable in the tier built to reach them. Defect injection: change
    `subclass_of` to return `""` and the last two cases here go red."""
    from retrieval import shard_router
    assert cn.UNCLASSIFIED == shard_router.UNCLASSIFIED == "unclassified"
    for sym in ["B65G47/91", "b25j 15/0616", "Y02P70/50", "F16B47/00", "B25", "", None, "  "]:
        assert cn.subclass_of(sym) == shard_router.domain_of(sym), sym


def test_a_family_with_no_symbols_names_the_unclassified_shard():
    assert cn.shard_domains_of([]) == ["unclassified"]
    assert cn.shard_domains_of(None) == ["unclassified"]
    assert cn.shard_domains_of(["B65G47/91", "B25J15/06"]) == ["B25J", "B65G"]
    assert cn.shard_domains_of(["B25", ""]) == ["unclassified"]


def test_a_y_tagging_code_is_not_a_shard():
    """Y10T carries 4.3M cross-sectionally tagged documents with nothing technical in common. A
    shard map built straight from `subclass_of` would create one, which is why the family-to-shard
    helper drops them and `subclass_of` itself, which must match `domain_of`, does not."""
    assert cn.shard_domains_of(["B23Q7/03", "Y10T29/49826", "B65G2201/02"]) == ["B23Q"]
    assert cn.shard_domains_of(["Y10T29/49826"]) == ["unclassified"]
    assert cn.shard_domains_of(["Y10T29/49826"], drop_indexing_codes=False) == ["Y10T"]


@pytest.mark.parametrize("sym", ["Y02P70/50", "Y10T29/49826", "Y10S901/30",
                                 "B65G2201/02", "F16B2200/50", "B65G2249/045",
                                 "G05B2219/40", "B65H2701/1752"])
def test_indexing_codes_are_not_fields(sym):
    """Y tagging codes and the 2000-series orthogonal subgroups describe a document, they do not
    classify it. Y02E alone carries 5.2M publications: counting it as a field would put most of
    the patent system in the niche."""
    assert cn.is_indexing_code(sym) is True


@pytest.mark.parametrize("sym", ["B65G47/91", "B25J15/0616", "F04F5/46", "H10P72/00", "B66C1/02"])
def test_real_symbols_are_fields(sym):
    assert cn.is_indexing_code(sym) is False


# ---------------------------------------------------------------- family identity
@pytest.mark.parametrize("fam,num,expected", [
    ("37026742", "US-7789357-B2", "37026742"),
    ("", "US-7789357-B2", "US-7789357-B2"),
    (None, "US-7789357-B2", "US-7789357-B2"),
    ("-1", "US-7789357-B2", "US-7789357-B2"),
    ("0", "US-7789357-B2", "US-7789357-B2"),
    ("  -1 ", "US-7789357-B2", "US-7789357-B2"),
])
def test_no_family_sentinels_do_not_become_a_family(fam, num, expected):
    """DOCDB writes -1 for "no simple family" and the ingest stored it verbatim. MEASURED: 21,862
    publications carry it, so a key of COALESCE(NULLIF(simple_family_id,''), publication_number)
    merges 21,862 unrelated documents into one family. The first manifest built that way produced
    a family carrying every CPC symbol from ploughs to harvesters."""
    assert cn.family_key(fam, num) == expected


def test_two_publications_with_no_family_are_two_families():
    assert cn.family_key("-1", "US-1-A") != cn.family_key("-1", "US-2-A")


# ---------------------------------------------------------------- boundary predicate
def test_symbol_tier():
    b = boundary()
    assert b.symbol_tier("B65G47/91") == "core"
    assert b.symbol_tier("B25J15/0616") == "core"
    assert b.symbol_tier("F04F5/46") == "adjacent"
    assert b.symbol_tier("F04F1/00") is None          # sibling main group, not admitted
    assert b.symbol_tier("F01N1/02") is None
    assert b.symbol_tier("B65G2201/02") is None       # indexing code inside a core subclass


def test_core_beats_adjacent_and_nothing_beats_nothing():
    b = boundary()
    assert b.tier_of_symbols(["F04F5/46", "B65G47/91"]) == "core"
    assert b.tier_of_symbols(["F04F5/46", "F01N1/02"]) == "adjacent"
    assert b.tier_of_symbols(["F01N1/02", "G10K11/16"]) is None
    assert b.tier_of_symbols([]) is None


def test_shard_domains_are_subclasses_plus_the_unclassified_route():
    """Every niche node must roll up to exactly one shard domain, so retrieval.shard_router and the
    niche cannot disagree about which shard holds a family. The unclassified route is in the list
    because the closures put 294,327 unclassified families in the niche."""
    b = boundary()
    assert b.shard_domains() == ["B25J", "B65G", "F04F", "H10P", "unclassified"]
    assert b.shard_domains(include_unclassified=False) == ["B25J", "B65G", "F04F", "H10P"]
    for d in b.shard_domains(include_unclassified=False):
        assert len(d) == 4


def test_citation_origin_filter_is_load_bearing():
    """The applicant's IDS is not a search result: one US patent in this corpus carries 5,771 APP
    citations against 11 from the search report. And an A code is background, not prior art that
    threatens a claim."""
    b = boundary()
    assert b.citation_admitted("SEA", "X") is True
    assert b.citation_admitted("ISR", "Y") is True
    assert b.citation_admitted("SEA", "A") is False
    assert b.citation_admitted("APP", "X") is False
    assert b.citation_admitted("PRS", "X") is False


def test_removing_the_origin_filter_admits_background_citations():
    """Defect injection: drop `origins` from the config and the A-coded background citations come
    straight back in. MEASURED on the live corpus, that is the difference between a niche of 62.2%
    of the corpus and one of 94.6%, which is not a niche."""
    wide = boundary(closures={"family": True,
                              "citation": {"enabled": True, "categories": ["SEA", "EXA", "ISR"]}})
    assert wide.citation_admitted("SEA", "A") is True
    assert wide.citation_admitted("APP", "X") is False   # the category filter still holds


# ---------------------------------------------------------------- text state and sources
def test_text_state_uses_the_fetchers_own_floors():
    assert cn.MIN_CLAIMS_CHARS == 200 and cn.MIN_DESC_CHARS == 800
    assert cn.text_state(199, 799) == (False, False)
    assert cn.text_state(200, 799) == (True, False)
    assert cn.text_state(200, 800) == (True, True)
    assert cn.text_state(None, None) == (False, False)


@pytest.mark.parametrize("countries,expected", [
    (["US"], "pqai"),
    (["EP", "DE"], "epo_ops"),
    (["WO"], "epo_ops"),
    #  THERE IS NO HIMMPAT RUNG. It used to be named here for any CJK-only family, which put
    #  900,463 families (62.6% of the job) behind a 250-a-day ledger on paper while the cascade
    #  was already answering them from Google Patents at 99.99%. See docs/cjk_acquisition.md.
    (["CN"], "gpatents_direct"),
    (["JP", "KR"], "gpatents_direct"),
    (["TW"], "gpatents_direct"),
    (["DE"], "gpatents_direct"),
    (["FR", "GB"], "gpatents_direct"),
    ([], "gpatents_direct"),
    (["US", "EP", "CN"], "pqai"),      # cheapest rung wins, not the first listed
])
def test_best_source_is_the_ladder(countries, expected):
    assert cn.best_source(countries, complete_member_exists=False) == expected


def test_a_family_that_already_holds_the_text_is_not_an_acquisition():
    assert cn.best_source(["US"], complete_member_exists=True) == cn.LOCAL_MEMBER


# ---------------------------------------------------------------- the record
def _m(num, country="US", title="t", abstract="a", claims=0, desc=0):
    return {"publication_number": num, "country": country, "title": title,
            "abstract": abstract, "claims_chars": claims, "desc_chars": desc}


def test_record_has_exactly_the_contracted_fields():
    rec = build_record("123", [_m("US-1-A")], ["B65G47/91"])
    assert tuple(sorted(rec)) == tuple(sorted(cn.RECORD_FIELDS))


def test_complete_text_needs_one_member_with_both():
    """The failure this guards: claims under the US sibling and a description under the EP one is
    two half documents, not a readable one. Nothing can be quoted from it as a single disclosure."""
    split = build_record("f", [_m("US-1-A", claims=5000, desc=0),
                               _m("EP-1-A1", country="EP", claims=0, desc=40000)],
                         ["B65G47/91"])
    assert split["has_claims"] is True
    assert split["has_description"] is True
    assert split["has_complete_text"] is False
    assert split["best_source"] is None or split["best_source"] == cn.LOCAL_MEMBER

    joined = build_record("f", [_m("US-1-A", claims=5000, desc=40000)], ["B65G47/91"])
    assert joined["has_complete_text"] is True
    assert joined["missing_fields"] == []
    assert joined["best_source"] is None


def test_missing_fields_and_source_for_a_stub():
    rec = build_record("f", [_m("DE-1-A1", country="DE", title="", abstract="")], ["B65G47/91"])
    assert rec["missing_fields"] == ["abstract", "claims", "description", "title"]
    assert rec["best_source"] == "gpatents_direct"


def test_family_member_substitution_is_reported_as_such():
    rec = build_record("f", [_m("US-1-A", claims=5000, desc=40000),
                             _m("DE-1-A1", country="DE", title="", abstract="")],
                       ["B65G47/91"])
    assert rec["has_complete_text"] is True
    assert rec["missing_fields"] == []
    assert rec["best_source"] is None


def test_representative_is_pinned_and_deterministic():
    members = [_m("US-9-A", title="thin", abstract="", claims=0, desc=0),
               _m("US-1-A", title="full", abstract="A", claims=5000, desc=40000)]
    rec = build_record("f", members, ["B65G47/91"])
    assert rec["title"] == "full" and rec["abstract"] == "A"
    assert build_record("f", list(reversed(members)), ["B65G47/91"])["title"] == "full"
    pinned = build_record("f", members, ["B65G47/91"], rep_number="US-9-A")
    assert pinned["title"] == "thin"


def test_ties_break_on_the_lowest_publication_number():
    a, b = _m("US-2-A", title="two"), _m("US-1-A", title="one")
    assert build_record("f", [a, b], [])["title"] == "one"
    assert build_record("f", [b, a], [])["title"] == "one"


def test_has_abstract_flag_stands_in_for_text_the_caller_did_not_carry():
    """The enumeration keeps only the representative's abstract in memory. A sibling that has one
    must still stop `abstract` being reported missing."""
    members = [_m("US-1-A", abstract="here", claims=5000, desc=40000),
               {"publication_number": "DE-1-A1", "country": "DE", "title": "",
                "abstract": "", "has_abstract": True, "claims_chars": 0, "desc_chars": 0}]
    assert "abstract" not in build_record("f", members, [])["missing_fields"]
    members[1]["has_abstract"] = False
    members[0]["abstract"] = ""
    assert "abstract" in build_record("f", members, [])["missing_fields"]


def test_indexing_codes_stay_in_the_record():
    rec = build_record("f", [_m("US-1-A")], ["B65G47/91", "Y02P70/50", "B65G2201/02"])
    assert rec["cpc"] == ["B65G2201/02", "B65G47/91", "Y02P70/50"]


# ---------------------------------------------------------------- manifest read/write
def _write(tmp_path, n=5, batch=2):
    b = boundary()
    w = ManifestWriter(str(tmp_path / "rel"), "rel-1", b, batch_size=batch)
    for i in range(n):
        w.add(build_record(f"{i:04d}", [_m(f"US-{i}-A")], ["B65G47/91"]))
    return w


def test_a_part_is_indexed_only_once_it_is_finished(tmp_path):
    w = _write(tmp_path, n=5, batch=2)
    d = str(tmp_path / "rel")
    idx = cn.read_index(d)
    assert idx["state"] == "in_progress"
    assert [p["name"] for p in idx["parts"]] == ["part-00000.jsonl", "part-00001.jsonl"]
    # the fifth record is still buffered: it is in neither a part file nor the index
    assert idx["totals"]["families"] == 4
    assert len(list(cn.read_manifest(d))) == 4
    w.close()
    assert cn.read_index(d)["state"] == "complete"
    assert os.path.exists(os.path.join(d, "COMPLETE"))
    assert len(list(cn.read_manifest(d))) == 5


def test_a_reader_ignores_a_part_file_the_index_does_not_name(tmp_path):
    """Defect injection for rule 1 of the contract. A part being written right now is on disk with
    its final name; a reader that globs instead of reading the index gets a truncated record."""
    w = _write(tmp_path, n=4, batch=2)
    d = str(tmp_path / "rel")
    with open(os.path.join(d, "part-00002.jsonl"), "w") as fh:
        fh.write('{"family_id": "9999", "publica')   # a half-written line
    assert len(list(cn.read_manifest(d))) == 4        # not 5, and no JSON error
    w.close()


def test_resume_from_the_last_consumed_part(tmp_path):
    w = _write(tmp_path, n=6, batch=2)
    w.close()
    d = str(tmp_path / "rel")
    seen = [r["family_id"] for _n, r in cn.read_manifest(d, after_part="part-00000.jsonl")]
    assert seen == ["0002", "0003", "0004", "0005"]


def test_parts_are_ordered_disjoint_and_checksummed(tmp_path):
    w = _write(tmp_path, n=6, batch=2)
    w.close()
    d = str(tmp_path / "rel")
    parts = cn.read_index(d)["parts"]
    assert [p["first_family_id"] for p in parts] == ["0000", "0002", "0004"]
    assert [p["last_family_id"] for p in parts] == ["0001", "0003", "0005"]
    for p in parts:
        assert cn.verify_part(d, p)
    with open(os.path.join(d, parts[0]["name"]), "a") as fh:
        fh.write("{}\n")
    assert not cn.verify_part(d, parts[0])


def test_follow_returns_when_the_release_closes(tmp_path):
    w = _write(tmp_path, n=4, batch=2)
    w.close()
    d = str(tmp_path / "rel")
    got = list(cn.follow(d, poll=0, sleep=lambda _s: None))
    assert [r["family_id"] for r in got] == ["0000", "0001", "0002", "0003"]


def test_latest_release_pointer(tmp_path):
    os.makedirs(tmp_path / "niche-2026-01-01")
    os.makedirs(tmp_path / "niche-2026-08-22")
    assert cn.latest_release_dir(str(tmp_path)).endswith("niche-2026-08-22")
    (tmp_path / "LATEST").write_text("niche-2026-01-01\n")
    assert cn.latest_release_dir(str(tmp_path)).endswith("niche-2026-01-01")


# ---------------------------------------------------------------- the checked-in boundary
def test_checked_in_boundary_loads_and_is_self_consistent():
    b = Boundary.load(CONFIG)
    assert b.core_subclasses == frozenset({"B25B", "B25J", "B65G", "B66C", "B66F", "F16B"})
    assert len(b.adjacent_groups) == 22
    ev = json.load(open(CONFIG))["derived"]["adjacent_evidence"]
    assert set(ev) == set(b.adjacent_groups), "every admitted group must carry its evidence"
    for g, e in ev.items():
        assert cn.main_group_of(g) == g, f"{g} is not a main group"
        assert not cn.is_indexing_code(g), f"{g} is an indexing code"
        assert cn.subclass_of(g) not in b.core_subclasses, f"{g} is already inside the core"
        assert e["seed"] + e["cited"] >= b.min_support, f"{g} is below min_support"
        assert e["density"] >= b.min_density, f"{g} is below min_density"


def test_every_seed_cpc_subgroup_is_inside_the_boundary():
    """SEED_CPC is the field definition the rest of the app calibrates against. A boundary that
    does not contain it is not this field's boundary."""
    from config import SEED_CPC
    b = Boundary.load(CONFIG)
    for s in SEED_CPC:
        assert b.symbol_tier(s) == "core", s


def test_rejected_groups_really_are_below_the_bar():
    """The branches the workstream brief named and the rule refused. If someone lowers the bar to
    let acoustics in, this test tells them what else comes with it."""
    spec = json.load(open(CONFIG))
    b = Boundary.load(CONFIG)
    rej = spec["derived"]["measured_and_rejected"]
    for g, e in rej.items():
        if g == "note":
            continue
        assert g not in b.adjacent_groups
        support = e["seed"] + e["cited"]
        assert support < b.min_support or e["density"] < b.min_density, g
