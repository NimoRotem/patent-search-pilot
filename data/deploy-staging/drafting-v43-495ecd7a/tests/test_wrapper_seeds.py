"""What the file wrapper is allowed to force into the reading, and what it is not.

Both of these are defects the first validation run surfaced in the seeding itself, on the real
subject US 2025/0033224 A1:

  * US 12,115,659 is the patent this application is a CONTINUATION of. The examiner cites it for
    obviousness-type double patenting, which is not a prior-art ground, and its priority date is
    the subject's own. `_drop_self_family` had removed that family; the seeding walked it back in
    and it ranked #1 in a prior-art report.
  * US 11,413,727, applied under 102(a)(2) to thirteen claims, lost its own family slot to
    US 2020/0338695 A1, a merely-considered sibling out of the same 1449 list, because every seed
    overwrote the representative rather than the first one claiming it.
"""
import datetime

import deep_rank


def _row(pub, fam, pub_date, prio, pid=1):
    return {"id": pid, "publication_number": pub, "kind_code": "B2", "country": "US",
            "title": pub, "abstract": "", "publication_date": datetime.date.fromisoformat(pub_date),
            "filing_date": datetime.date.fromisoformat(prio),
            "earliest_priority_date": datetime.date.fromisoformat(prio),
            "simple_family_id": fam, "tier": 1, "facsimile_path": None, "fam": fam,
            "n_claims": 20, "n_emb": 90}


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        want = set((params or [[]])[0])
        self._out = [r for r in self._rows if r["publication_number"] in want]

    def fetchall(self):
        return self._out


#  The real shape: the subject's own family is 83602050 and its EFD is 2021-04-20.
SUBJECT = {"date_cutoff": "2021-04-20",
           "self_family_excluded": {"publication": "US-2025033224-A1", "family": "83602050"}}
ROWS = [
    _row("US-11413727-B2", "66624664", "2022-08-16", "2018-05-08", pid=1),   # APPLIED, 102(a)(2)
    _row("US-2020338695-A1", "66624664", "2020-10-29", "2018-05-08", pid=2),  # sibling, considered
    _row("US-12115659-B1", "83602050", "2024-10-15", "2021-04-20", pid=3),    # the subject's own
    _row("US-7690610-B2", "111", "2010-04-06", "2004-05-03", pid=4),          # ordinary prior art
]


def _run(seeds, ranked=("111",)):
    report = dict(SUBJECT, prosecution_seeds=list(seeds))
    reps = {}
    fams, seed_fams = deep_rank._seed_families(_Cur(ROWS), report, list(ranked), reps)
    return fams, seed_fams, reps


def test_the_subjects_own_family_is_not_walked_back_in():
    fams, seed_fams, reps = _run(["US-12115659-B1", "US-7690610-B2"])
    assert "83602050" not in seed_fams, "the subject's own parent patent was seeded as prior art"
    assert "83602050" not in reps
    assert "83602050" not in fams
    assert "111" in seed_fams


def test_a_seed_that_is_not_prior_art_on_the_dates_is_not_read():
    """Same gate every other candidate passes. The wrapper says what to look at; it does not
    exempt a document from being prior art."""
    rows = ROWS + [_row("US-9999999-B2", "222", "2026-01-01", "2025-01-01", pid=9)]
    report = dict(SUBJECT, prosecution_seeds=["US-9999999-B2"])
    reps = {}
    fams, seed_fams, = deep_rank._seed_families(_Cur(rows), report, [], reps)[:2]
    assert seed_fams == [] and reps == {}


def test_the_applied_reference_keeps_its_own_family_slot():
    """Seeds arrive applied-first. The first one to claim a family keeps it."""
    _f, seed_fams, reps = _run(["US-11413727-B2", "US-2020338695-A1"])
    assert reps["66624664"]["publication_number"] == "US-11413727-B2", (
        "a merely-considered sibling displaced the reference the examiner applied")
    assert seed_fams.count("66624664") == 1


def test_a_seed_whose_family_was_never_ranked_is_added_to_the_candidates():
    fams, seed_fams, _r = _run(["US-11413727-B2"], ranked=["111"])
    assert fams[0] == "66624664", fams
    assert "111" in fams


def test_a_seed_already_in_the_ranking_does_not_duplicate_it():
    fams, _s, _r = _run(["US-7690610-B2"], ranked=["111", "999"])
    assert fams == ["111", "999"]


def test_no_seeds_changes_nothing():
    report = {"date_cutoff": "2021-04-20"}
    reps = {"111": "x"}
    fams, seed_fams = deep_rank._seed_families(_Cur(ROWS), report, ["111"], reps)
    assert fams == ["111"] and seed_fams == [] and reps == {"111": "x"}


def test_a_broken_query_does_not_break_the_search():
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

    report = dict(SUBJECT, prosecution_seeds=["US-11413727-B2"])
    fams, seed_fams = deep_rank._seed_families(_Boom(), report, ["111"], {})
    assert fams == ["111"] and seed_fams == []
