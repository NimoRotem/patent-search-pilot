"""Which databases a search drew from, and how many families each one contributed.

An attorney relying on a report has to be able to answer "what did you search?" and "what did each
of them actually add?". Two facts make that answerable and neither was recorded:

* the external fan-out stored `per_source` as HITS RETURNED, which is the wrong unit. One measured
  run: bigquery_gpatents returned 9,979 rows and serpapi_gpatents returned 400, out of 12,480
  candidates that fused down to 393 families. The returned count says nothing about what reached
  the reader, and quoting it as a contribution flatters the noisiest adapter.
* the local retrieval channels were reported one per channel (dense, bm25, qbe, biblio), which
  answers a question nobody asked. They are all one database asked several ways.

So `run_stats.sources_of` reports one row per DATABASE with the families it put in front of the
reader, and `external.run` counts those per source, including how many no other source found.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import run_stats                                                        # noqa: E402


def _report(**kw):
    rep = {
        "channel_families": {"dense": ["F1", "F2", "F3"], "bm25": ["F2", "F3", "F4"],
                             "external": ["F9", "F8"]},
        "external": {},
    }
    rep.update(kw)
    return rep


def test_the_local_channels_are_one_database_not_four():
    rows = run_stats.sources_of(_report())
    local = [r for r in rows if r["kind"] == "local"]
    assert len(local) == 1, "retrieval channels are being reported as separate databases"
    #  F1..F4 deduplicated across dense and bm25; the external channel is NOT counted here.
    assert local[0]["families"] == 4
    assert "dense" not in local[0]["label"].lower()


def test_the_external_channel_is_never_counted_as_local():
    """`external`, `global` and `federation` all carry the fan-out's merged contribution. Counting
    them in the local total would credit our corpus with documents it does not hold."""
    rep = _report(channel_families={"dense": ["F1"], "external": ["F9"], "global": ["F7"],
                                    "federation": ["F6"]})
    local = [r for r in run_stats.sources_of(rep) if r["kind"] == "local"][0]
    assert local["families"] == 1


def test_families_per_source_is_reported_when_recorded():
    rep = _report(external={"families_by_source": {"pqai": 40, "uspto": 12},
                            "unique_families_by_source": {"pqai": 31, "uspto": 2},
                            "per_source": {"pqai": 1893, "uspto": 208}})
    by_key = {r["key"]: r for r in run_stats.sources_of(rep)}
    assert by_key["pqai"]["families"] == 40
    assert by_key["pqai"]["unique"] == 31
    assert by_key["pqai"]["returned"] == 1893
    assert by_key["uspto"]["label"] == "USPTO ODP"


def test_the_two_units_are_never_conflated():
    """A source with a returned count and NO family count must report families as unknown, not as
    its returned count and not as zero. Reports written before families were counted are the whole
    reason this can happen."""
    rep = _report(external={"per_source": {"pqai": 1893, "bigquery_gpatents": 9979}})
    rows = {r["key"]: r for r in run_stats.sources_of(rep)}
    assert rows["pqai"]["families"] is None
    assert rows["pqai"]["returned"] == 1893
    assert rows["bigquery_gpatents"]["families"] is None


def test_the_biggest_contributor_is_listed_first_after_the_corpus():
    rep = _report(external={"families_by_source": {"pqai": 5, "uspto": 40, "openalex": 0},
                            "per_source": {"pqai": 9000, "uspto": 10, "openalex": 3}})
    rows = run_stats.sources_of(rep)
    assert rows[0]["kind"] == "local"
    assert [r["key"] for r in rows[1:]] == ["uspto", "pqai", "openalex"], (
        "sources are ordered by what they returned rather than by what they contributed")


def test_a_report_with_no_channels_reports_no_sources():
    assert run_stats.sources_of({}) == []
    assert run_stats.sources_of(None) == []


def test_the_receipt_carries_the_sources():
    """`_from_report` is what a receipt is written from, so a search finishing today records its
    sources and the history page does not have to re-read a multi-megabyte report."""
    st = run_stats._from_report(_report(
        external={"families_by_source": {"pqai": 3}, "per_source": {"pqai": 30}}))
    assert st.get("sources"), "the receipt no longer records which databases were searched"
    assert any(s["key"] == "pqai" for s in st["sources"])


# --------------------------------------------------------------- counting families at the source

def test_external_counts_families_not_returned_rows():
    """The counting rule, over the real shape `external.run` works with: a candidate belongs to a
    source and resolves to a family, and only the families that SURVIVED fusion are credited.

    Mirrors the code rather than importing it, because `run()` is a network fan-out. The guard
    against drift is the next test, which asserts the real function computes both keys.
    """
    cands = [
        {"pub_number": "US-1-A", "source": "pqai"},
        {"pub_number": "US-2-A", "source": "pqai"},
        {"pub_number": "US-2-A", "source": "uspto"},      # same family, found twice
        {"pub_number": "US-3-A", "source": "uspto"},
        {"pub_number": "US-4-A", "source": "openalex"},   # did not survive fusion
    ]
    fam_of = {"US1A": "F1", "US2A": "F2", "US3A": "F3", "US4A": "F4"}
    pid_of = {"F1": 1, "F2": 2, "F3": 3}                  # F4 was cut

    def _norm(s):
        return "".join(ch for ch in str(s).upper() if ch.isalnum())

    by_source = {}
    for c in cands:
        fam = fam_of.get(_norm(c["pub_number"]))
        if fam in pid_of:
            by_source.setdefault(c["source"], set()).add(fam)
    finders = {}
    for src, fs in by_source.items():
        for f in fs:
            finders.setdefault(f, set()).add(src)

    assert {s: len(f) for s, f in by_source.items()} == {"pqai": 2, "uspto": 2}
    assert "openalex" not in by_source, "a source was credited with a family that was cut"
    unique = {s: sum(1 for f in fs if len(finders[f]) == 1) for s, fs in by_source.items()}
    assert unique == {"pqai": 1, "uspto": 1}, "F2 was found by both and is unique to neither"


def test_external_run_records_both_keys():
    """Anchored on the ASSIGNMENT, so deleting the counting deletes the green."""
    body = open(os.path.join(ROOT, "src", "external.py"), encoding="utf-8").read()
    assert "families_by_source = {" in body and "unique_by_source = {" in body
    assert '"families_by_source": families_by_source' in body
    #  and summary() has to pass them on, or nothing reaches the report
    assert '"families_by_source": ext.get("families_by_source")' in body


def test_the_history_page_renders_the_source_table():
    tpl = open(os.path.join(ROOT, "templates", "history.html"), encoding="utf-8").read()
    assert "st.sources" in tpl, "the history expander no longer shows where results came from"
    assert "srctbl" in tpl


@pytest.mark.parametrize("key,expected", [
    ("uspto", "USPTO ODP"), ("pqai", "PQAI"), ("epo_ops", "EPO OPS"),
    ("gpatents_direct", "Google Patents (direct)"),
])
def test_every_live_source_has_a_human_label(key, expected):
    """A key like `epo_ops` in front of an attorney is a defect, and the fallback prettifier turns
    it into 'Epo Ops', which is worse than the key."""
    import federation
    assert federation.source_label(key) == expected


def test_no_engine_copy_reaches_the_page_with_an_em_dash():
    """The adapters write their notes with em dashes and this page is customer-facing. House rule:
    never ship one. Normalised on the way in, not left to whoever notices."""
    import corpus_profile
    assert "—" not in corpus_profile._clean("a — b")
    assert corpus_profile._clean("trial licence — expired 2026-08-04") == \
        "trial licence, expired 2026-08-04"
    for live in (corpus_profile.external_sources() or []):
        assert "—" not in (live["note"] + live["reason"] + live["label"] + live["purpose"]), live


def test_the_corpus_page_explains_each_source():
    """A status table that says a source is searched, without saying what it is for, tells a
    reader nothing they can act on."""
    import corpus_profile
    import federation
    for key in ("uspto", "pqai", "epo_ops", "serpapi_gpatents", "bigquery_gpatents", "openalex",
                "lens", "kipris", "euipo", "ipaustralia", "himmpat", "gpatents_direct"):
        assert corpus_profile.SOURCE_PURPOSE.get(key), "no purpose written for %s" % key
        assert federation.source_label(key)
