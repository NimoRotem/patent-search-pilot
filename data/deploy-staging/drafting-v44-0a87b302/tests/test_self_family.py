"""The applicant's own family is never the art, at any stage.

Found by the second validation run of US 2025/0033224 A1. Rank 1 of the report, and the only claim
the ledger called ANTICIPATED, was US 2022/0331993 A1: same DOCDB family as the subject, same
priority date, same title. It is the applicant's own sibling publication, and of course it
discloses their claim 20.

`_drop_self_family` filters `ranked_families` once, before the reading. Everything after it can put
a family back — claim_reach and the orphan rescue search the corpus again, `_extend_to` tops the
reading list up, and deep_rank rewrites `ranked_families` from what it charted. A guard that runs
once at the top of a pipeline that keeps adding candidates is a guard for the first stage only.
"""
import re

import deep_rank


def test_the_family_is_recorded_even_when_nothing_was_dropped_yet():
    """The bug in one line: `self_family_excluded` is only written when the early filter removed
    something, so on every run where the family arrived LATER the later gates read nothing."""
    src = open(__import__("webapp").__file__.replace(".pyc", ".py")).read()
    m = re.search(r"def _drop_self_family\(rep\):.*?\n    return rep", src, re.S)
    assert m, "the self-family filter moved"
    body = m.group(0)
    assert 'rep["self_family"] = fam' in body, (
        "the family is only recorded on a drop, so a later stage cannot enforce it")
    #  and it is recorded BEFORE the conditional that depends on something being dropped
    assert body.index('rep["self_family"] = fam') < body.index("if dropped:")


def test_the_reading_never_charts_the_subjects_own_family():
    """The point of damage: a charted reference becomes evidence, a ledger cell and a rank."""
    src = open(deep_rank.__file__.replace(".pyc", ".py")).read()
    m = re.search(r'rows = \[r for r in rows if not deep_analysis\._same_pub.*?if not rows:',
                  src, re.S)
    assert m, "the candidate filter moved"
    assert 'self_family' in m.group(0), "candidates are filtered by publication number only"


def test_the_final_ranking_is_filtered_on_the_way_out():
    """`ranked_families` is what the page, the export and every later stage read as "the art"."""
    src = open(deep_rank.__file__.replace(".pyc", ".py")).read()
    m = re.search(r'report\["ranked_families"\] = fam_order', src)
    assert m, "the ranking write moved"
    before = src[:m.start()]
    tail = before[-400:]
    assert "self_fam" in tail, "nothing filters the subject's own family out of the final ranking"


def test_the_seed_gate_reads_the_family_that_is_always_recorded():
    src = open(deep_rank.__file__.replace(".pyc", ".py")).read()
    m = re.search(r'self_fam = str\(\(report or \{\}\)\.get\("self_family"\)', src)
    assert m, ("the seed gate still reads self_family_excluded, which is only set on a drop and "
               "was therefore inert on exactly the runs that needed it")


def _row(pub, fam, pid=1, pub_date=(2022, 10, 20), prio=(2021, 4, 20)):
    import datetime
    return {"id": pid, "publication_number": pub, "kind_code": "A1", "country": "US",
            "title": pub, "abstract": "", "publication_date": datetime.date(*pub_date),
            "filing_date": datetime.date(*prio),
            "earliest_priority_date": datetime.date(*prio),
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


def test_a_seed_in_the_subjects_own_family_is_refused_by_name():
    """The real one: US 12,115,659, the patent the subject is a continuation of, cited by the
    examiner for double patenting. It must be refused as the subject's own family, and the reason
    given must say so rather than leaning on the date check that happens to agree."""
    #  The control is real prior art on the dates, so the ONLY reason to refuse the other one is
    #  the family. Giving both the subject's own dates would have let the date check carry the test.
    rows = [_row("US-12115659-B1", "83602050", pid=3),
            _row("US-7690610-B2", "111", pid=4, pub_date=(2010, 4, 6), prio=(2004, 5, 3))]
    report = {"date_cutoff": "2021-04-20", "self_family": "83602050",
              "prosecution_seeds": ["US-12115659-B1", "US-7690610-B2"]}
    reps = {}
    fams, seed_fams = deep_rank._seed_families(_Cur(rows), report, ["111"], reps)
    assert "83602050" not in seed_fams and "83602050" not in reps
    assert "111" in seed_fams
