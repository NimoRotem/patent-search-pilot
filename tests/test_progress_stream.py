"""What the page is told while a search reads. See webapp._reading_line / _read_log.

The line these replace was "Evidence sweep: batch 130 of 210…", which names neither the documents
being read nor the question being asked of them, and it is the line that sits on screen for hours.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import webapp                                                             # noqa: E402

CHART = {"done": 132, "total": 210, "pub": "US20210231163A1",
         "title": "Suction cup with a compliant multi-layer seal", "chars": 84213,
         "reused": False, "found": True, "n_features": 7}
SWEEP = {"done": 130, "total": 210, "requirement": "claim 1[c]",
         "pubs": ["JP2015077655A", "CN211104058U", "US5795001A", "DE9105214U1", "AU2017234379A1"]}


def _fresh(slug):
    with webapp._JOB_LOCK:
        webapp._JOBS.pop(slug, None)


def test_the_line_names_the_document_being_read():
    line = webapp._reading_line("Reading in full", CHART)
    assert "132 of 210" in line
    assert "US20210231163A1" in line
    assert "Suction cup with a compliant multi-layer seal" in line
    assert "84k chars" in line


def test_the_sweep_line_names_the_requirement_and_the_batch():
    line = webapp._reading_line("Evidence sweep", SWEEP)
    assert "130 of 210" in line
    assert "claim 1[c]" in line
    assert "JP2015077655A" in line and "DE9105214U1" in line
    assert "+1" in line, "a batch longer than four says how many more"


def test_a_reused_read_says_so():
    assert "checkpoint" in webapp._reading_line("Reading in full", dict(CHART, reused=True))


def test_a_line_with_nothing_to_name_still_counts():
    assert webapp._reading_line("Evidence sweep", {"done": 3, "total": 9}) == "Evidence sweep: 3 of 9…"


def test_the_log_takes_one_document_from_the_chart():
    _fresh("t1")
    webapp._read_log("t1", CHART)
    log = webapp._JOBS["t1"]["read_log"]
    assert [r["pub"] for r in log] == ["US20210231163A1"]
    assert log[0]["chars"] == 84213 and log[0]["n_features"] == 7


def test_the_log_takes_the_whole_batch_from_the_sweep():
    _fresh("t2")
    webapp._read_log("t2", SWEEP)
    log = webapp._JOBS["t2"]["read_log"]
    assert [r["pub"] for r in log] == SWEEP["pubs"]
    assert all(r["note"] == "claim 1[c]" for r in log)


def test_an_exact_repeat_is_not_logged_twice():
    _fresh("t3")
    webapp._read_log("t3", SWEEP)
    webapp._read_log("t3", SWEEP)
    assert len(webapp._JOBS["t3"]["read_log"]) == len(SWEEP["pubs"])


def test_the_same_document_read_again_for_another_requirement_is_logged_again():
    _fresh("t4")
    webapp._read_log("t4", SWEEP)
    webapp._read_log("t4", dict(SWEEP, done=135, requirement="claim 1[d]"))
    log = webapp._JOBS["t4"]["read_log"]
    assert len(log) == 2 * len(SWEEP["pubs"])
    assert {r["note"] for r in log} == {"claim 1[c]", "claim 1[d]"}


def test_the_log_is_capped():
    _fresh("t5")
    for i in range(40):
        webapp._read_log("t5", dict(CHART, done=i, pub="US%07dA1" % i))
    assert len(webapp._JOBS["t5"]["read_log"]) == webapp._READ_LOG_MAX


def test_nothing_to_log_is_not_an_empty_row():
    _fresh("t6")
    webapp._read_log("t6", {"done": 1, "total": 2})
    assert "read_log" not in (webapp._JOBS.get("t6") or {})


def test_the_log_reaches_the_wire():
    _fresh("t7")
    webapp._read_log("t7", CHART)
    ev = webapp._job_event("t7", webapp._JOBS["t7"])
    assert [r["pub"] for r in ev["read_log"]] == ["US20210231163A1"]
