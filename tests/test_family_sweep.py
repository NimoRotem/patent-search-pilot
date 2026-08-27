"""An exclusion is a statement about ONE publication, and a family is many.

The measured case is Schmalz's own pole shoe, reported by counsel on 2026-08-26. The A1 of that
disclosure published 28 August 2025, after Schmalz's own 9 September 2024 priority date, so the
search read it and correctly kept it out of a US submission. The SAME disclosure was filed as a
German utility model on the same day and gazetted 18 April 2024, six months BEFORE the priority
date it would be cited against. Dead in the United States, where 102(b)(1)(A) reaches an
applicant's own disclosure inside the grace year. Full Art. 54(2) EPC and § 3(1) PatG prior art at
the EPO and the DPMA, which have no general grace period.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import family_sweep as fs                                                # noqa: E402
import search_modes as sm                                               # noqa: E402
import submission_compliance as sc                                      # noqa: E402

EFD = datetime.date(2024, 9, 9)                       # Schmalz's own priority date
A1 = "DE-102024105114-A1"                             # published 2025-08-28, after the EFD
GM = "DE-202024100869-U1"                             # gazetted 2024-04-18, before it

FAMILY = [
    {"pub": A1, "country": "DE", "kind": "A1", "pub_date": "2025-08-28", "prio_date": "2024-02-23"},
    {"pub": GM, "country": "DE", "kind": "U1", "pub_date": "2024-04-18", "prio_date": "2024-02-23"},
    {"pub": "EP-4706914-A1", "country": "EP", "kind": "A1", "pub_date": "2026-03-11",
     "prio_date": "2024-09-09"},
]


# --------------------------------------------------------------------------- utility models


def test_a_german_gebrauchsmuster_is_recognised_as_a_utility_model():
    assert fs.is_utility_model("DE", "U1") is True
    assert fs.is_utility_model("DE", "A1") is False
    assert fs.is_utility_model("CN", "U") is True
    assert fs.is_utility_model("US", "U1") is False, "there is no US utility model"


# --------------------------------------------------------------------------- the sweep


def _stub(monkeypatch, members):
    monkeypatch.setattr(fs, "_from_ops", lambda pub: list(members))
    monkeypatch.setattr(fs, "_from_corpus", lambda pub: [])


def test_the_sweep_finds_the_sibling_that_published_before_the_priority_date(monkeypatch):
    _stub(monkeypatch, FAMILY)
    got = fs.sweep(A1, EFD, own=True)
    assert got["checked"] is True
    assert got["best"]["pub"] == GM
    assert got["best"]["utility_model"] is True
    assert "before" in got["note"].lower()
    assert "utility model" in got["note"].lower()


def test_the_earliest_qualifying_member_wins_not_merely_a_qualifying_one(monkeypatch):
    _stub(monkeypatch, FAMILY + [
        {"pub": "DE-9999-U1", "country": "DE", "kind": "U1", "pub_date": "2024-07-01"}])
    assert fs.sweep(A1, EFD)["best"]["pub"] == GM


def test_the_document_being_swept_is_never_its_own_sibling(monkeypatch):
    _stub(monkeypatch, [FAMILY[0]])
    got = fs.sweep(A1, EFD)
    assert got["best"] is None
    assert "none published before" in got["note"]


def test_a_family_that_cannot_be_resolved_says_so_rather_than_saying_no(monkeypatch):
    """An empty answer from a network call is not the same finding as an empty family, and it is
    the one that would silently discard the document for ever."""
    _stub(monkeypatch, [])
    got = fs.sweep(A1, EFD)
    assert got["checked"] is False and got["best"] is None
    assert "by hand" in got["note"]


def test_sweep_excluded_only_spends_its_budget_on_documents_being_thrown_away(monkeypatch):
    _stub(monkeypatch, FAMILY)
    cands = [{"pub": A1, "basis": "not_art", "reads_on": 5},
             {"pub": "US-1-B2", "basis": "public", "reads_on": 30},
             {"pub": "DE-2-A1", "basis": "secret", "co_owned": True, "reads_on": 9}]
    fs.sweep_excluded(cands, EFD)
    assert cands[0].get("sibling_alert") is True
    assert "sibling" not in cands[1], "a document that is being filed needs no rescue"
    assert cands[2].get("sibling") is not None


def test_the_sweep_budget_is_spent_on_the_broadest_exclusions_first(monkeypatch):
    _stub(monkeypatch, FAMILY)
    cands = [{"pub": "DE-%d-A1" % i, "basis": "not_art", "reads_on": i} for i in range(1, 6)]
    fs.sweep_excluded(cands, EFD, limit=2)
    assert [bool(c.get("sibling")) for c in cands] == [False, False, False, True, True]


# --------------------------------------------------------------------------- per jurisdiction


def test_the_grace_period_exists_in_washington_and_nowhere_else():
    own_pub = datetime.date(2024, 4, 18)
    assert sm.own_disclosure_excepted("US", own_pub, EFD) is True
    assert sm.own_disclosure_excepted("EP", own_pub, EFD) is False
    assert sm.own_disclosure_excepted("DE", own_pub, EFD) is False


def test_a_disclosure_older_than_the_grace_year_is_prior_art_even_in_washington():
    assert sm.own_disclosure_excepted("US", datetime.date(2023, 1, 1), EFD) is False


def test_the_utility_model_is_dead_in_the_us_and_lethal_in_europe():
    matrix = sm.forum_matrix("DE", datetime.date(2024, 4, 18), datetime.date(2024, 2, 23),
                             EFD, own=True)
    by = {m["forum"]: m for m in matrix}
    assert by["US"]["available"] is False
    assert "102(b)(1)(A)" in by["US"]["why"]
    for f in ("EP", "DE"):
        assert by[f]["available"] is True, f
        assert by[f]["novelty_only"] is False, "no grace period means novelty AND inventive step"
    assert by["EP"]["statute"] == "EPC Art. 54(2)"
    assert by["DE"]["statute"] == "§ 3(1) PatG"


def test_the_same_document_owned_by_someone_else_is_prior_art_everywhere():
    matrix = sm.forum_matrix("DE", datetime.date(2024, 4, 18), datetime.date(2024, 2, 23),
                             EFD, own=False)
    assert all(m["available"] for m in matrix)


def test_a_later_published_german_application_reaches_the_dpma_but_not_the_uspto():
    matrix = sm.forum_matrix("DE", datetime.date(2025, 8, 28), datetime.date(2024, 2, 23),
                             EFD, own=False)
    by = {m["forum"]: m for m in matrix}
    assert by["US"]["available"] is False, "102(a)(2) does not reach a DE national publication"
    assert by["DE"]["available"] is True and by["DE"]["novelty_only"] is True
    assert by["DE"]["statute"] == "§ 3(2) PatG"


def test_where_it_still_works_names_the_offices_a_us_exclusion_does_not_reach():
    matrix = sm.forum_matrix("DE", datetime.date(2024, 4, 18), datetime.date(2024, 2, 23),
                             EFD, own=True)
    assert [m["forum"] for m in sm.where_it_still_works(matrix)] == ["EP", "DE"]


# --------------------------------------------------------------------------- the filing pass


def test_a_blocked_document_reports_its_earlier_sibling_instead_of_just_vanishing(monkeypatch):
    _stub(monkeypatch, FAMILY)
    doc = {"pub": A1, "rows": [{"quote": "", "strong": False}],
           "biblio": {"pub": A1, "country": "DE", "publication_date": "2025-08-28",
                      "priority_date": "2024-02-23", "assignee": "J. Schmalz GmbH"}}
    kept, blocked, _notes = sc.apply(
        [doc], {"efd": EFD}, source_text_for=lambda pub: "", target_assignees=["J. Schmalz GmbH"])
    assert kept == []
    assert len(blocked) == 1
    assert GM in blocked[0]["why"], "name the member that IS available"
    assert "BUT" in blocked[0]["why"]
