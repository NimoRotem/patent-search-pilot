"""Tests for the `patentdata` reconciliation, one per claim the reconciliation actually makes.

`ops/niche_reconcile.py` produces the numbers in `docs/niche_pipeline_merge.md` and the manifest
that seeds workstream C's pool. Two things in it can be wrong in a way nobody would notice:

* the gap classification, because their own `terminology` signal is a fallback label and not a
  term match, so every reason has to be re-derived and the reasons have to PARTITION rather than
  overlap. A classification that quietly puts a `graph_only` publication in `cpc_outside_b` turns
  "their boundary is wider" into a fact when it is not.
* the seed priority, because C's two bands are 100 apart and the evidence offset is added inside
  one. An offset that crosses the band lifts a publication with no evidence above the ones with
  no texted family sibling, which is the population worth money.

Nothing here touches the database or the network.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))
import niche_reconcile as nr                                        # noqa: E402


def _row(**values):
    base = {"publication_number": "ZZ0000001A", "family_id": "", "authority": "ZZ",
            "priority": 4, "cpc": [], "ipc": [], "term_hit": False, "their_class_hit": False,
            "b_tier": "", "classified": False, "discovery_signals": ["family"]}
    base.update(values)
    return base


# --------------------------------------------------------------------------- classification
def test_every_gap_reason_is_reachable_and_they_partition():
    """One publication gets exactly one reason, and the five cover the space."""
    cases = {
        #  Strength order matters: a publication can satisfy several tests at once and the
        #  strongest evidence has to be the one reported.
        "b_boundary": _row(b_tier="core", their_class_hit=True, term_hit=True, classified=True),
        "cpc_outside_b": _row(their_class_hit=True, term_hit=True, classified=True),
        "terminology": _row(term_hit=True, classified=True),
        "unclassified": _row(classified=False),
        "graph_only": _row(classified=True),
    }
    for expected, row in cases.items():
        assert nr.classify_gap(row) == expected, row
    assert set(cases) == set(nr.GAP_REASONS)


def test_an_unclassified_publication_is_never_called_graph_only():
    """The 35.9% of examiner-cited art with no CPC is the population the citation closure exists
    for. Reporting it as 'no evidence' would argue for dropping exactly the art we cannot reach
    any other way."""
    assert nr.classify_gap(_row(classified=False, cpc=[], ipc=[])) == "unclassified"
    assert nr.classify_gap(_row(classified=True, cpc=["A01B1/00"])) == "graph_only"


def test_a_term_hit_outranks_no_classification():
    """Their `terminology` signal is an else branch and never reads the text, so a real term hit
    has to beat the unclassified bucket rather than disappear into it."""
    assert nr.classify_gap(_row(term_hit=True, classified=False)) == "terminology"


# --------------------------------------------------------------------------- seed priority
def _seed(tmp_path, rows):
    path = tmp_path / "new.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return nr.seed_rows(str(path))


def test_the_evidence_offset_never_crosses_workstream_cs_band(tmp_path):
    """C decides the band and this file may only order inside it. A publication with no texted
    family sibling is the one worth money and must stay in front of every publication that has
    one, whatever evidence either carries.

    Defect injection: raise `GAP_BAND_STEP` to 30 and the weakest no-sibling row (50 + 4*30 + 4 =
    174) overtakes the strongest has-sibling row (150 + 0 + 1 = 151).
    """
    from acquire.manifest import PRIORITY_HAS_SIBLING_TEXT, PRIORITY_NO_SIBLING_TEXT
    rows = []
    for index, reason in enumerate(nr.GAP_REASONS):
        for sibling in (False, True):
            rows.append(_row(publication_number=f"ZZ{index}{int(sibling)}00001A",
                             gap_reason=reason, priority=4 if sibling else 1,
                             text={"sibling_text": sibling}))
    seeded = _seed(tmp_path, rows)
    assert len(seeded) == len(rows)
    no_sibling = [r["priority"] for r in seeded
                  if r["publication_number"][3] == "0"]
    has_sibling = [r["priority"] for r in seeded
                   if r["publication_number"][3] == "1"]
    assert max(no_sibling) < min(has_sibling), (
        f"the evidence offset crossed the band: no-sibling reached {max(no_sibling)} and "
        f"has-sibling starts at {min(has_sibling)}")
    assert min(no_sibling) >= PRIORITY_NO_SIBLING_TEXT
    assert min(has_sibling) >= PRIORITY_HAS_SIBLING_TEXT


def test_stronger_evidence_is_fetched_first_inside_a_band(tmp_path):
    rows = [_row(publication_number=f"ZZ{index}000001A", gap_reason=reason, priority=4,
                 text={"sibling_text": False})
            for index, reason in enumerate(nr.GAP_REASONS)]
    seeded = _seed(tmp_path, rows)
    assert [r["gap_reason"] for r in seeded] == list(nr.GAP_REASONS)


def test_a_publication_whose_text_we_already_hold_is_not_seeded(tmp_path):
    """The pool is work to do. Re-fetching a document we hold is money for nothing."""
    held = _row(publication_number="ZZ0000009A", gap_reason="terminology",
                text={"claims": True, "sibling_text": False})
    fetched = _row(publication_number="ZZ0000010A", gap_reason="terminology",
                   text={"fetched_description": True})
    wanted = _row(publication_number="ZZ0000011A", gap_reason="terminology", text={})
    seeded = _seed(tmp_path, [held, fetched, wanted])
    assert [r["publication_number"] for r in seeded] == ["ZZ0000011A"]


# --------------------------------------------------------------------------- manifest scan
def test_scan_manifest_separates_a_missing_publication_from_a_missing_family(tmp_path):
    """A publication B misses whose FAMILY B has is an enumeration hole inside B's own boundary.
    A family B has no record for at all is a boundary difference. The reconciliation reports them
    apart because the fixes are different, and it measured the first at zero."""
    release = tmp_path / "rel"
    release.mkdir()
    parts = [{"name": "part-00000.jsonl"}]
    (release / "index.json").write_text(json.dumps({"release_id": "unit", "parts": parts}))
    #  Written exactly the way `corpus_niche.ManifestWriter` writes it, compact separators and
    #  all, because that shape is what the prefilter in scan_manifest depends on.
    compact = dict(separators=(",", ":"), sort_keys=True)
    (release / "part-00000.jsonl").write_text(
        json.dumps({"family_id": "111", "publications": ["ZZ-0000001-A", "ZZ-0000002-A"]},
                   **compact) + "\n"
        + json.dumps({"family_id": "222", "publications": ["ZZ-0000003-A"]}, **compact) + "\n")

    wanted = {"ZZ0000001A", "ZZ0000004A", "ZZ0000009A"}
    held, family_of, present, lines, index = nr.scan_manifest(
        str(release), wanted, {"111", "222", "333"})
    assert held == {"ZZ0000001A"}
    assert family_of["ZZ0000001A"] == "111"
    #  '333' is a family of theirs B has no record for; '111' and '222' are families B has.
    assert present == {"111", "222"}
    assert lines == 2 and index["release_id"] == "unit"


def test_a_manifest_in_the_wrong_shape_is_an_error_and_not_an_overlap_of_zero(tmp_path):
    """The prefilter is a substring match on the compact key. If a release were ever written
    with default `json.dumps` spacing, every lookup would miss and the reconciliation would
    report all 16,896 of their publications as genuinely new. That is a wrong answer wearing the
    shape of a right one, so it raises instead."""
    release = tmp_path / "rel"
    release.mkdir()
    (release / "index.json").write_text(
        json.dumps({"release_id": "unit", "parts": [{"name": "part-00000.jsonl"}]}))
    #  Default separators: `"publications": [` with a space.
    (release / "part-00000.jsonl").write_text(
        json.dumps({"family_id": "111", "publications": ["ZZ-0000001-A"]}) + "\n")
    with pytest.raises(ValueError, match="not in the shape"):
        nr.scan_manifest(str(release), {"ZZ0000001A"}, {"111"})
