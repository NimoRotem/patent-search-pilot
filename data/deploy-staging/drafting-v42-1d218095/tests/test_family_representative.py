"""Which member of a family gets read is which member gets cited, and that is a legal question.

Counsel, 2026-08-20: US 11,413,727 "doesn't appear in your 10-document package, in any anticipation
credit, or among the 60 fully-analysed references". It was not a recall failure — the family ranked
28th of 6,215 and WAS read. `resolve_family_reps` ordered by `publication_date DESC` and returned
US-11,999,030-B2 instead, which is the same disclosure with a weaker date.

These run the real SQL against the real corpus. Marked with the same live-DB marker the rest of the
suite uses so a machine without Postgres skips rather than fails.
"""
import datetime

import pytest

import webview

pytestmark = pytest.mark.usefixtures()

#  The reported family. US 2025/0033224 A1 has an effective filing date of 2021-04-20; this family
#  published in 2018 and is still publishing in 2026, so it holds members on both sides of it.
FAM = "66624664"
EFD = datetime.date(2021, 4, 20)


@pytest.fixture()
def cur():
    db = pytest.importorskip("db")
    try:
        conn = db.connect()
    except Exception:
        pytest.skip("no corpus available")
    conn.autocommit = True
    c = conn.cursor()
    try:
        c.execute("SELECT 1 FROM publications WHERE simple_family_id=%s LIMIT 1", (FAM,))
        if not c.fetchone():
            pytest.skip("corpus does not hold the reference family")
        yield c
    finally:
        conn.close()


def test_the_representative_prefers_unconditional_prior_art(cur):
    """The whole point. Published before the subject's EFD is 102(a)(1): unconditional, no
    exception reaches it. Published after is 102(a)(2): novelty only, and disqualified outright in
    the US by the common-ownership exception."""
    r = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD)[FAM]
    assert r["publication_date"] < EFD, (
        "picked %s published %s, which is only 102(a)(2) art, while the family holds a member "
        "published before the cutoff" % (r["publication_number"], r["publication_date"]))


def test_the_representative_can_still_be_read(cur):
    """Readability is a hard gate ahead of the date: a document with no claims and no text cannot
    be charted, quoted or filed, whatever its date."""
    r = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD)[FAM]
    assert (r["n_claims"] or 0) > 0 and (r["n_emb"] or 0) > 0


def test_without_a_subject_date_the_old_ordering_stands(cur):
    """Additive by construction: a caller that cannot supply a cutoff gets exactly what it always
    got, so nothing outside a dated search changes behaviour."""
    old = webview.resolve_family_reps(cur, [FAM])[FAM]
    new = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD)[FAM]
    assert old["publication_date"] > new["publication_date"]


def test_the_date_actually_changes_the_answer(cur):
    """Guards against the parameter being accepted and ignored — which is what a stub does.

    The bracket is US-2020/0338695-A1, published 2020-10-29. One day either side of it the answer
    has to move: before, the newest readable member that IS public art is the PCT; after, the US
    pre-grant publication becomes public art and wins on jurisdiction.
    """
    before = webview.resolve_family_reps(cur, [FAM], subject_efd=datetime.date(2020, 10, 1))[FAM]
    after = webview.resolve_family_reps(cur, [FAM], subject_efd=datetime.date(2020, 11, 1))[FAM]
    assert before["publication_number"] != after["publication_number"]
    assert before["publication_date"] < datetime.date(2020, 10, 1)
    assert after["publication_date"] < datetime.date(2020, 11, 1)


def test_an_early_cutoff_does_not_return_an_unreadable_member(cur):
    """At a cutoff before anything readable published, the date term has no candidate to prefer.
    It must fall back to a document that can be read, not to an unreadable one that happens to be
    old — a reference nobody can quote is no use to a submission."""
    r = webview.resolve_family_reps(cur, [FAM], subject_efd=datetime.date(2019, 1, 1))[FAM]
    assert (r["n_claims"] or 0) > 0 and (r["n_emb"] or 0) > 0


def test_subject_efd_of_reads_the_report_cutoff():
    assert webview.subject_efd_of({"date_cutoff": "2021-04-20"}) == EFD
    assert webview.subject_efd_of({"date_cutoff": ""}) is None
    assert webview.subject_efd_of({}) is None
    assert webview.subject_efd_of({"date_cutoff": "not a date"}) is None
    #  A malformed date must be None, not an exception on the search path.
    assert webview.subject_efd_of({"date_cutoff": "2021-02-30"}) is None


def test_family_alternates_name_the_other_members(cur):
    """A submission cites one member; the choice has consequences and has to be visible."""
    got = webview.family_alternates(cur, ["US-11999030-B2"], subject_efd=EFD)
    rows = got.get("US-11999030-B2") or []
    assert rows, "no alternates found for a family with 29 members"
    assert any(r.get("public_prior_art") for r in rows)
    #  unconditional prior art is listed first, because that is the one worth citing
    assert rows[0]["public_prior_art"] is True
    assert all(r["pub"] != "US-11999030-B2" for r in rows)
