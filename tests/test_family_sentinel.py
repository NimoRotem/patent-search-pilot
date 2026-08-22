"""DOCDB's '-1' means "no simple family", and treating it as a family id loses documents.

MEASURED on the live corpus 2026-08-22: 21,862 publications carry `simple_family_id = '-1'`.
Every family query used to say `COALESCE(NULLIF(simple_family_id,''), publication_number)`, which
catches the empty string and nothing else, so all 21,862 shared one key. Two live defects followed,
and each has a test here that goes red if the sentinel handling is removed:

* family collapse keeps one row per key, so at most one of the 21,862 could survive any search;
* `legal._date_clause` excludes the subject's own family, so a subject in that set excluded all
  21,862 unrelated documents from every channel.

The third test is the trap the fix itself introduces: fold the sentinel to NULL without saying
what to do about a subject that has no family, and `row <> NULL` is NULL for every row, which
discards the whole corpus instead of one family.
"""
import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from retrieval.family import (          # noqa: E402
    FAMILY_SENTINELS, family_id_sql, family_key_sql, real_family_id)


def _Subject(number):
    """The real `search_modes.Subject`, so this test cannot pass against a stub that has drifted."""
    from search_modes import Subject

    return Subject(number=number, efd=date(2020, 1, 1))


#  NAMED HERE AS LITERALS, ON PURPOSE. An earlier version of this file looped over
#  `FAMILY_SENTINELS` itself, so deleting '-1' from the constant deleted the assertion with it and
#  the suite stayed green with the defect restored. A test whose expectation is imported from the
#  code under test asserts nothing.
LIVE_SENTINEL = "-1"                       # 21,862 rows of the live corpus, measured 2026-08-22
SENTINELS_THAT_MUST_BE_HANDLED = ("", "-1", "0")


def test_every_docdb_sentinel_reads_as_no_family():
    for sentinel in SENTINELS_THAT_MUST_BE_HANDLED:
        assert real_family_id(sentinel) is None, sentinel
    assert real_family_id(None) is None
    assert real_family_id("  -1  ") is None, "a padded sentinel is still a sentinel"
    assert real_family_id("12345") == "12345"
    assert real_family_id("-12") == "-12", "a real family id that merely starts with a minus"


def test_the_family_key_expression_folds_every_sentinel_to_the_publication():
    sql = family_key_sql()
    for sentinel in SENTINELS_THAT_MUST_BE_HANDLED:
        assert f"'{sentinel}'" in sql, f"{sentinel!r} is not folded, so it is still a family key"
    assert sql.endswith("publication_number)"), "a sentinel must fall back to the publication"


def test_the_constant_still_covers_the_sentinel_the_live_corpus_actually_carries():
    assert LIVE_SENTINEL in FAMILY_SENTINELS, (
        "21,862 publications on the live corpus carry simple_family_id = '-1'. Removing it from "
        "FAMILY_SENTINELS puts them all back into one family.")


def test_the_family_id_expression_has_no_fallback():
    """`family_id_sql` answers "which family", not "which key", so it must not invent one.

    Two publications with no family are not family members of each other. A fallback to the
    publication number would be harmless in a key and wrong in a join, which is why these are two
    functions and not one with a flag.
    """
    sql = family_id_sql("p")
    assert "publication_number" not in sql
    assert sql.startswith("NULLIF(") and "p.simple_family_id" in sql


def test_the_sentinel_is_not_a_family_shared_by_21862_publications():
    """The collapse defect, in the form the retriever actually meets it."""
    from retrieval.family import FamilyMixin

    holder = FamilyMixin()
    #  What the OLD expression produced: every sentinel row keyed '-1'.
    holder._fam = {1: "-1", 2: "-1", 3: "REAL-FAM", 4: "REAL-FAM"}
    collapsed = holder.collapse_rows(
        [{"publication_id": p, "score": 1.0 / p} for p in (1, 2, 3, 4)], cap=10)
    assert len(collapsed) == 2, "the old keying collapses the two sentinel rows into one"

    #  What the FIXED expression produces: each sentinel row keyed by its own publication.
    holder._fam = {1: "US1A", 2: "US2A", 3: "REAL-FAM", 4: "REAL-FAM"}
    collapsed = holder.collapse_rows(
        [{"publication_id": p, "score": 1.0 / p} for p in (1, 2, 3, 4)], cap=10)
    assert [p for p, _s in collapsed] == [1, 2, 3], (
        "publications with no family are three separate disclosures, not one")


def test_a_subject_with_no_family_does_not_exclude_every_other_publication():
    """The `legal` defect, and the trap in fixing it.

    Two properties in one clause, because they pull in opposite directions: the subject's sentinel
    must not match other rows' sentinels, and a NULL subject family must not turn the comparison
    into NULL for every row.
    """
    from retrieval.legal import _date_clause
    from search_modes import Mode

    frag, params = _date_clause(_Subject("US9999999B2"), Mode("novelty"), alias="p")

    assert params[-2:] == ["US9999999B2", "US9999999B2"], (
        "the subject's family is consulted twice: once to ask whether it has one at all")
    assert "IS NULL OR" in frag, (
        "without the 'subject has no family' term, `row <> NULL` is NULL for every row and the "
        "filter discards the entire corpus rather than one family")
    for sentinel in SENTINELS_THAT_MUST_BE_HANDLED:
        assert f"'{sentinel}'" in frag, f"{sentinel!r} still compares as a real family id"


def test_against_the_live_corpus_the_sentinel_rows_are_not_one_family():
    """The proof, run as SQL against the real data rather than reasoned about.

    Read-only, and bounded: it touches only the rows carrying the sentinel, via the same index the
    anti-join uses, not the 5M-row table.
    """
    import db

    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n, count(DISTINCT {family_key_sql()}) AS keys "
                    "FROM publications WHERE simple_family_id = %s", (LIVE_SENTINEL,))
        row = cur.fetchone()

    n, keys = row["n"], row["keys"]
    if not n:
        pytest.skip(f"this corpus holds no publication with simple_family_id = {LIVE_SENTINEL!r}")
    assert keys == n, (
        f"{n} publications carry the sentinel and the family key gives them {keys} distinct "
        f"keys. They must be {n} families of one, not {keys}: family collapse keeps a single row "
        f"per key, so any smaller number is exactly that many documents made unreachable.")


@pytest.mark.parametrize("module,needle", [
    ("retrieval.base", "family_key_sql"),
    ("retrieval.cold", "family_key_sql"),
    ("retrieval.citations", "family_id_sql"),
    ("retrieval.legal", "family_id_sql"),
])
def test_no_channel_spells_the_family_expression_for_itself(module, needle):
    """One expression in one place. A second spelling that forgets a sentinel is the defect."""
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(module))
    assert needle in src, f"{module} should use {needle}"
    assert "NULLIF(simple_family_id,'')" not in src.replace(" ", ""), (
        f"{module} still carries the old expression, which folds '' and nothing else")
