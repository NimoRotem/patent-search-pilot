"""The canonical gold set: one denominator, every exclusion inspectable, deterministic.

Two different denominators (83 and 69) were in use and neither was written down. 69 silently
dropped the citations whose documents the corpus does not hold, which are guaranteed misses, so
reporting against it flattered the system by excluding its worst cases.
"""
import csv
import os
import sys

import pytest

#  eval/ is not on the suite's path (it holds harnesses, not shipped code), so add it here rather
#  than making the whole eval directory importable for every test.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "eval"))
import benchmark_gold as BG  # noqa: E402

CSV = os.path.join(os.path.dirname(BG.__file__), "benchmark_gold.csv")


@pytest.fixture(scope="module")
def rows():
    if not os.path.exists(CSV):
        pytest.skip("benchmark_gold.csv not built on this box")
    with open(CSV) as fh:
        return list(csv.DictReader(fh))


def test_every_row_has_every_field(rows):
    for r in rows:
        assert set(r) == set(BG.FIELDS)


def test_every_citation_maps_to_exactly_one_family(rows):
    for r in rows:
        assert r["gold_family_id"], r["source_record_id"]
    ids = [r["source_record_id"] for r in rows]
    assert len(ids) == len(set(ids)), "source_record_id must be unique"


def test_no_eligible_row_duplicates_a_family_within_a_subject(rows):
    seen = set()
    for r in rows:
        if r["eligible"] != "true":
            continue
        key = (r["subject_id"], r["gold_family_id"])
        assert key not in seen, f"duplicate family survived: {key}"
        seen.add(key)


def test_every_exclusion_has_a_reason_from_the_fixed_set(rows):
    allowed = {"UNRESOLVED", "SUBJECT_FAMILY", "PUBLISHED_AFTER_EFD",
               "NOT_X_OR_Y", "DUPLICATE_FAMILY"}
    for r in rows:
        if r["eligible"] == "true":
            assert r["exclusion_reason"] == ""
        else:
            assert r["exclusion_reason"] in allowed, r["exclusion_reason"]


def test_not_in_corpus_is_reported_never_used_as_an_eligibility_rule(rows):
    """A citation the corpus does not hold is a guaranteed miss, not an excused one. It belongs in
    the denominator so that failing to reach it counts against the system."""
    assert any(r["in_corpus"] == "false" and r["eligible"] == "true" for r in rows)
    assert not any(r["exclusion_reason"] == "NOT_IN_CORPUS" for r in rows)


def test_gold_source_states_whether_an_xy_filter_could_be_applied(rows):
    """Two subjects have no relevance codes at all, so 'X/Y only' cannot be applied uniformly.
    The label must say which regime each subject is under rather than implying one rule."""
    for r in rows:
        assert r["gold_source"] in {"corpus_citations_xy", "corpus_citations_uncoded",
                                    "transcribed_from_document"}
    by = {r["subject_id"]: r["gold_source"] for r in rows}
    coded = {s for s, g in by.items() if g == "corpus_citations_xy"}
    for r in rows:
        if r["subject_id"] in coded and r["eligible"] == "true":
            assert BG.is_xy(r["citation_code"]) is not False


def test_is_xy_distinguishes_absent_from_negative():
    assert BG.is_xy("X") is True
    assert BG.is_xy("XY") is True
    assert BG.is_xy("A") is False
    assert BG.is_xy("") is None            # no code available: the rule cannot be applied
    assert BG.is_xy("cat:APP") is None     # a category is not a relevance code
