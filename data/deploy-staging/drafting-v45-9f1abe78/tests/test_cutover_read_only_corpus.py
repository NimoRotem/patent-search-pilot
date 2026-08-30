"""What the cutover does to a search path that used to write the live corpus.

THE DEFECT THIS FILE IS ABOUT. Moving execution into `runner.worker` arms `corpus_guard`, and two
things the search path does today are writes to protected tables:

  * `deep_rank._enrich_missing_text` -> `enrich.enrich_publication`, which inserts `claims`,
    `paragraphs` and `chunks`. Refused, so every text-less reference would be LISTED rather than
    read, and the reading is the most valuable thing a deep search does.
  * `external.materialise`, which inserts `publications` and `classifications` for the external
    candidates this corpus does not hold. Measured on twelve recent live reports: between 93 and
    283 NEW publications per deep search. Refused ROW BY ROW inside a SAVEPOINT handler that
    swallows the exception, so the external channel came back short with nothing saying why.

Neither of those is a durability problem, which is why neither was found by the durability work.
Both are cutover problems: they change what a search RETURNS, and a cutover that changes the
answer is not invisible.

The rule stays. The corpus is read only in the search worker (docs/corpus_write_policy.md and the
rebuild brief), so nothing here relaxes the guard. What it does is send the text to the two places
the policy names, and make the one loss that remains loud instead of silent.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import corpus_guard                                                  # noqa: E402
import deep_analysis                                                 # noqa: E402
import deep_rank                                                     # noqa: E402
import external                                                      # noqa: E402
import failclosed                                                    # noqa: E402


# ------------------------------------------------------------------ a corpus, faked at the cursor
class _Cur:
    """The three reads `deep_analysis.full_text` makes, answered from a dict."""

    def __init__(self, pub_row, claims, chunks):
        self._pub, self._claims, self._chunks = pub_row, claims, chunks
        self._last = None

    def execute(self, sql, args=None):
        self._last = " ".join(str(sql).split()).lower()

    def fetchone(self):
        return self._pub

    def fetchall(self):
        if "from claims" in self._last:
            return list(self._claims)
        if "from chunks" in self._last:
            return list(self._chunks)
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def corpus(monkeypatch):
    """-> a setter for what the corpus holds for one publication."""
    state = {"pub": {"id": 1, "title": "A vacuum gripper", "abstract": ""},
             "claims": [], "chunks": []}

    import contextlib

    @contextlib.contextmanager
    def cursor(*a, **k):
        yield _Cur(state["pub"], state["claims"], state["chunks"])

    monkeypatch.setattr(deep_analysis.db, "cursor", cursor)
    return state


@pytest.fixture()
def scratch(monkeypatch):
    """-> the scratch store, as a dict of publication -> record."""
    store = {}
    import sources.docstore as docstore
    monkeypatch.setattr(docstore, "_get_sync", lambda pn, want_text=True: store.get(str(pn)))
    return store


# ============================================================ the reading, from the scratch store
def test_a_reference_with_no_corpus_text_is_read_from_the_scratch_store(corpus, scratch):
    scratch["US1234567B2"] = {"pub_number": "US1234567B2", "title": "Stashed title",
                              "abstract": "an abstract",
                              "claims": "1. A gripper comprising a sealing lip.\n\n"
                                        "2. The gripper of claim 1, wherein the lip is elastic.",
                              "description": "The invention relates to vacuum handling of "
                                             "porous workpieces in a factory setting.\n\n"
                                             "In one embodiment the sealing lip is a moulded "
                                             "elastomer skirt that follows the surface.",
                              "fulltext_source": "epo:ops"}
    ref = deep_analysis.full_text("US1234567B2")
    assert ref["found"] is True
    assert ref["n_claims"] == 2, "both stored claims must be citable units"
    assert ref["n_paragraphs"] == 2
    assert ref["scratch"] == "epo:ops", "the reader must say where the text came from"
    labels = [p["label"] for p in ref["passages"]]
    assert "claim 1" in labels and "paragraph 1" in labels, \
        "a quote has to be citable by coordinate, not just present"


def test_without_the_scratch_overlay_the_same_reference_is_unreadable(corpus, scratch, monkeypatch):
    """DEFECT INJECTION. Remove the overlay and the reference has no text at all, which is exactly
    what the armed worker would have produced: a reference listed rather than read."""
    scratch["US1234567B2"] = {"claims": "1. A gripper comprising a sealing lip.",
                              "description": "The invention relates to vacuum handling."}
    monkeypatch.setattr(deep_analysis, "_add_scratch_text", lambda *a, **k: 0)
    ref = deep_analysis.full_text("US1234567B2")
    assert ref["n_claims"] == 0 and ref["n_paragraphs"] == 0
    assert not ref["passages"]


def test_the_scratch_store_never_displaces_text_the_corpus_already_holds(corpus, scratch):
    """Scoped to the empty case ON PURPOSE. A publication the corpus can answer for is answered
    from the corpus, so this can only ever add an unreadable document and never re-order or
    replace a readable one."""
    corpus["claims"] = [{"claim_no": 1, "text": "1. The corpus copy of the claim.",
                         "resolved_text": None}]
    scratch["US1234567B2"] = {"claims": "1. The scratch copy, which must not be used.",
                              "description": "nor this description"}
    ref = deep_analysis.full_text("US1234567B2")
    assert ref["n_claims"] == 1
    assert "corpus copy" in ref["passages"][0]["text"]
    assert "scratch" not in ref


def test_an_unreachable_scratch_store_leaves_the_reference_exactly_as_it_was(corpus, monkeypatch):
    import sources.docstore as docstore

    def boom(*a, **k):
        raise RuntimeError("scratch store down")

    monkeypatch.setattr(docstore, "_get_sync", boom)
    ref = deep_analysis.full_text("US1234567B2")
    assert ref["found"] is True and ref["n_claims"] == 0


def test_a_stored_blob_with_no_blank_lines_is_still_split_into_units():
    units = deep_analysis._split_units(
        "1. A gripper comprising a sealing lip arranged around a suction chamber.\n"
        "2. The gripper of claim 1, wherein the sealing lip is a moulded elastomer skirt.")
    assert len(units) == 2


def test_a_short_independent_claim_is_never_dropped_as_noise():
    """The regression that produced this test: a 38-character independent claim was discarded by
    a minimum-length filter, which is the one sentence a novelty chart is mostly about."""
    units = deep_analysis._split_units("1. A gripper comprising a sealing lip.\n\n"
                                       "2. The gripper of claim 1, wherein the lip is elastic.")
    assert units[0] == "1. A gripper comprising a sealing lip."


def test_a_stored_claim_is_cited_by_the_number_it_states_for_itself(corpus, scratch):
    """A blob that does not start at claim 1 must not be renumbered from 1: a quote cited as
    "claim 1" when the reference calls it claim 7 is a wrong citation in a legal document."""
    scratch["US1234567B2"] = {"claims": "7. A gripper comprising a sealing lip and a chamber.\n\n"
                                        "8. The gripper of claim 7, wherein the lip is elastic.",
                              "description": "A description paragraph about vacuum handling."}
    ref = deep_analysis.full_text("US1234567B2")
    labels = [p["label"] for p in ref["passages"]]
    assert "claim 7" in labels and "claim 8" in labels


# ================================================== the fetch, routed away from the live corpus
def test_a_read_only_process_stashes_the_text_instead_of_writing_the_corpus(monkeypatch):
    """The whole point: the SAME text is fetched, and it lands where the policy says."""
    calls = {"stash": [], "persist": []}
    import enrich
    monkeypatch.setattr(enrich, "recovery_available", lambda: True)
    monkeypatch.setattr(enrich, "stash_full_text",
                        lambda pub, **k: (calls["stash"].append(pub)
                                          or {"pub": pub, "ok": True, "claims_chars": 900,
                                              "desc_chars": 4000}))
    monkeypatch.setattr(enrich, "enrich_publication",
                        lambda pub, **k: (calls["persist"].append(pub)
                                          or {"pub": pub, "ok": True, "added_claims": 1}))
    _fake_thin(monkeypatch, ["US1111111B2"])
    monkeypatch.setattr(corpus_guard, "armed", lambda: True)
    monkeypatch.setattr(corpus_guard, "writes_allowed", lambda: False)

    got = deep_rank._enrich_missing_text([{"pub": "US1111111B2", "rank": 1}])
    assert got == 1
    assert calls["stash"] == ["US1111111B2"]
    assert calls["persist"] == [], "the live corpus must not be written from a search process"


def test_an_unarmed_process_keeps_writing_the_corpus_exactly_as_before(monkeypatch):
    """The web app is not armed and its behaviour must be unchanged by any of this."""
    calls = {"stash": [], "persist": []}
    import enrich
    monkeypatch.setattr(enrich, "recovery_available", lambda: True)
    monkeypatch.setattr(enrich, "stash_full_text",
                        lambda pub, **k: calls["stash"].append(pub) or {"ok": True})
    monkeypatch.setattr(enrich, "enrich_publication",
                        lambda pub, **k: (calls["persist"].append(pub)
                                          or {"pub": pub, "ok": True, "added_claims": 1}))
    _fake_thin(monkeypatch, ["US1111111B2"])
    monkeypatch.setattr(corpus_guard, "armed", lambda: False)

    assert deep_rank._enrich_missing_text([{"pub": "US1111111B2", "rank": 1}]) == 1
    assert calls["persist"] == ["US1111111B2"] and calls["stash"] == []


def _fake_thin(monkeypatch, pubs):
    """Make `_enrich_missing_text` believe every `pubs` entry has no text in the corpus."""
    class _C:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return [{"pub": p, "cl": 0, "pa": 0} for p in pubs]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        autocommit = True

        def cursor(self):
            return _C()

        def close(self):
            pass

    monkeypatch.setattr(deep_rank.db, "connect", lambda *a, **k: _Conn())


# ================================== the external channel, degraded loudly instead of silently
@pytest.fixture()
def ext(monkeypatch):
    """`materialise` with its corpus read faked and its demand queue captured."""
    import contextlib
    queued = []

    @contextlib.contextmanager
    def cursor(*a, **k):
        yield object()

    monkeypatch.setattr(external.db, "cursor", cursor)
    monkeypatch.setattr(external, "_resolve_existing",
                        lambda cur, pubs: {"us9999999": (77, "F77")})
    import runstore
    monkeypatch.setattr(runstore, "queue_for_ingest",
                        lambda pub, **k: queued.append(pub) or {"id": 1, "request_count": 1,
                                                                "state": "pending"})
    failclosed.reset()
    yield queued
    failclosed.reset()


_RECORDS = {"us9999999": {"pub_number": "US9999999B2", "title": "already held"},
            "us8888888": {"pub_number": "US8888888B2", "title": "new one", "cpc": ["B25J15/06"]},
            "us7777777": {"pub_number": "US7777777B2", "title": "another new one"}}


def test_external_candidates_a_read_only_process_cannot_insert_are_recorded_as_demand(
        ext, monkeypatch):
    monkeypatch.setattr(corpus_guard, "armed", lambda: True)
    monkeypatch.setattr(corpus_guard, "writes_allowed", lambda: False)

    out = external.materialise(dict(_RECORDS))

    assert out == {"us9999999": (77, "F77")}, \
        "the candidates the corpus already holds are still usable; only the new ones are lost"
    assert sorted(ext) == ["US-7777777-B2", "US-8888888-B2"], \
        "every candidate that could not be inserted must become demand for the next release"
    sites = failclosed.summary()["sites"].get("corpus_read_only") or []
    assert "external:materialise" in sites, \
        "the report must be able to say this run's external channel was short and why"


def test_the_same_refusal_is_silent_without_the_check(ext, monkeypatch):
    """DEFECT INJECTION. With the up-front check removed, the guard refuses each insert inside the
    per-row SAVEPOINT handler, which swallows it: the candidates vanish, nothing is queued, and
    nothing in the report says the channel came back short."""
    monkeypatch.setattr(corpus_guard, "armed", lambda: True)
    monkeypatch.setattr(corpus_guard, "writes_allowed", lambda: False)

    real = external._queue_external_demand
    monkeypatch.setattr(external, "_queue_external_demand", lambda *a, **k: 0)
    try:
        out = external.materialise(dict(_RECORDS))
    finally:
        monkeypatch.setattr(external, "_queue_external_demand", real)
    assert out == {"us9999999": (77, "F77")}
    assert ext == [], "this is the failure mode: the demand signal is simply not recorded"


def test_an_unarmed_process_still_inserts_external_candidates(ext, monkeypatch):
    """The web app path is untouched: it reaches the insert loop exactly as before."""
    monkeypatch.setattr(corpus_guard, "armed", lambda: False)
    reached = []

    def _canon(pub):
        reached.append(pub)
        return None                      # stop before any real insert, having proved we got there

    monkeypatch.setattr(external, "_canonical", _canon)
    external.materialise(dict(_RECORDS))
    assert sorted(reached) == ["US7777777B2", "US8888888B2"]
    assert ext == [], "an unarmed process writes the corpus; it does not queue demand"
