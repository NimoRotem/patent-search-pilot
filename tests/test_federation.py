"""Federation bridge + out-of-domain detector + mode-availability tests.

Everything here is offline: no DB, no network, no Vertex calls. The detector's *calibration*
(which needs the real index) is exercised separately by domain_detect.calibrate(); its measured
result on the 28-query labelled set was accuracy 1.00, 0 false positives, 0 false negatives.
"""
from __future__ import annotations

import json
import types

import pytest

import federation as F
import search_modes as SM
from search_modes import Mode, ModeNotAvailable, Subject
from retrieval import Result, Retriever


# --- publication-number join keys -----------------------------------------------------------
def test_join_key_bridges_both_conventions():
    # the pilot stores hyphenated, the federated app stores bare — they must collapse together
    assert F.join_key("US-11999030-B2") == F.join_key("US11999030B2") == "US11999030B2"
    assert F.join_key("patent/EP1234567A1/en") == "EP1234567A1"
    assert F.join_key("") == ""
    assert F.join_key(None) == ""


def test_key_variants_cover_us_pregrant_zero_padding():
    # BigQuery/pilot: US-2023003794-A1 ; federated canonical: US20230003794A1
    v = F.key_variants("US-2023003794-A1")
    assert "US2023003794A1" in v
    assert "US20230003794A1" in v
    # a granted number has no padding ambiguity
    assert F.key_variants("US-11999030-B2") == ["US11999030B2"]


# --- normalisation of the federated payload -------------------------------------------------
DONE = {
    "shortlist": [
        {"pub_number": "US11999030B2", "title": "Vacuum gripper", "abstract": "a",
         "family_id": "66624664", "members": ["US11999030B2", "EP3838815A1"],
         "sources": ["serpapi_gpatents"], "final_score": 0.9, "cpc": ["B66C1/02"]},
        {"pub_number": "DE102017106252A1", "title": "Sauggreifsystem", "abstract": "b",
         "family_id": "63449883", "members": ["DE102017106252A1"],
         "sources": ["bigquery_gpatents"], "final_score": 0.7},
        {"pub_number": "", "title": "dropped — no publication number"},
    ],
    "elements": ["seal", "pump"],
}


def test_to_hits_normalises_and_drops_numberless():
    hits = F._to_hits(DONE)
    assert len(hits) == 2                     # the numberless entry is dropped
    assert hits[0].pub_number == "US11999030B2"
    assert hits[0].rank == 0 and hits[1].rank == 1
    assert "EP3838815A1" in hits[0].keys()    # members are join candidates too


# --- fusion into the pilot's shape ----------------------------------------------------------
class FakeRetriever:
    """Minimal stand-in exposing the surface federation.py uses. `local_pubs` maps a join key
    to (publication_id, family_key) as the real resolve_pub_numbers would."""

    def __init__(self, local_pubs=None):
        self.local_pubs = local_pubs or {}
        self._fam = {}
        self.registered = {}

    def resolve_pub_numbers(self, keys):
        return {k: self.local_pubs[k] for k in keys if k in self.local_pubs}

    def register_external(self, pid, fam):
        self._fam[pid] = fam
        self.registered[pid] = fam

    def family_key(self, pid):
        return self._fam.get(pid, str(pid))

    rrf = staticmethod(Retriever.rrf)      # the REAL fusion, so this exercises production code

    def dedup_family(self, ranked):
        seen, out = set(), []
        for pid, sc, prov in ranked:
            fk = self.family_key(pid)
            if fk in seen:
                continue
            seen.add(fk)
            out.append((fk, pid, sc, prov))
        return out


def _fed_ok():
    return F.FederatedResult(ok=True, hits=F._to_hits(DONE), elements=["seal"])


def test_as_channel_reuses_the_local_id_when_the_pub_is_already_in_the_corpus():
    # US11999030B2 exists locally as publication_id 42, family 66624664
    r = FakeRetriever({"US11999030B2": (42, "66624664")})
    chan, ext = F.as_channel(r, _fed_ok())
    pids = [p for p, _ in chan]
    assert 42 in pids                       # resolved to the REAL local id, not a virtual one
    assert 42 not in ext                    # so it is not an external
    # the unmatched German family becomes a virtual publication with a registered family key
    assert "fed:DE102017106252A1" in pids
    assert r.registered["fed:DE102017106252A1"] == "fedfam:63449883"


def test_as_channel_registers_family_keys_so_dedup_collapses_federated_siblings():
    r = FakeRetriever()
    chan, ext = F.as_channel(r, _fed_ok())
    assert len(chan) == 2 and len(ext) == 2
    # two federated hits in the SAME family must collapse to one row in dedup
    ranked = [(p, 1.0 / (i + 1), {}) for i, (p, _) in enumerate(chan)]
    r._fam["fed:DE102017106252A1"] = "fedfam:X"
    r._fam["fed:US11999030B2"] = "fedfam:X"
    assert len(r.dedup_family(ranked)) == 1


def test_as_channel_is_empty_when_federation_failed():
    r = FakeRetriever()
    assert F.as_channel(r, F.FederatedResult(ok=False, error="boom")) == ([], {})


def test_fuse_replace_drops_local_channels_entirely():
    r = FakeRetriever()
    local = Result(ranked_pubs=[(7, 1.0, {})], family_ranked=[("f7", 7, 1.0, {})],
                   channel_hits={"dense": [7, 8, 9]}, query="q")
    out = F.fuse(r, local, _fed_ok(), strategy="replace")
    assert set(out.channel_hits) == {"federated"}       # local channels gone
    assert 7 not in [p for p, _, _ in out.ranked_pubs]  # irrelevant local hit not carried over
    assert out.federation["ok"] is True


def test_fuse_augment_keeps_local_channels_and_adds_federated():
    r = FakeRetriever()
    local = Result(ranked_pubs=[(7, 1.0, {})], family_ranked=[("f7", 7, 1.0, {})],
                   channel_hits={"dense": [7, 8]}, query="q")
    out = F.fuse(r, local, _fed_ok(), strategy="augment")
    assert set(out.channel_hits) == {"dense", "federated"}
    pids = [p for p, _, _ in out.ranked_pubs]
    assert 7 in pids and "fed:US11999030B2" in pids


def test_fuse_returns_local_untouched_when_there_is_nothing_to_add():
    r = FakeRetriever()
    local = Result(ranked_pubs=[], family_ranked=[], channel_hits={}, query="q")
    assert F.fuse(r, local, F.FederatedResult(ok=False)) is local


# --- graceful degradation -------------------------------------------------------------------
def test_search_never_raises_when_the_federated_app_is_unreachable(monkeypatch):
    monkeypatch.setattr(F, "_stream_search",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("connection refused")))
    res = F.search("a vacuum gripper", use_cache=False)
    assert res.ok is False and "connection refused" in res.error


def test_search_refuses_empty_and_disabled(monkeypatch):
    assert F.search("   ").ok is False
    monkeypatch.setattr(F, "ENABLED", False)
    assert F.search("something").ok is False


def test_health_never_raises(monkeypatch):
    monkeypatch.setattr(F, "ENABLED", False)
    assert F.health()["ok"] is False


def test_stream_search_stops_at_done_and_surfaces_upstream_errors(monkeypatch):
    """The SSE parser must pick the done payload out of the event stream, and turn an
    upstream 'error' event into an exception rather than silently returning nothing."""
    class FakeResp:
        status_code = 200
        def __init__(self, lines): self._l = lines
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_lines(self, decode_unicode=True): return iter(self._l)

    ok_lines = ['data: {"kind":"start"}', 'data: {"kind":"round","n":1}',
                'data: ' + json.dumps({"kind": "done", **DONE}), 'data: {"kind":"end"}']
    fake_requests = types.SimpleNamespace(post=lambda *a, **k: FakeResp(ok_lines))
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)
    done = F._stream_search("q", "novelty")
    assert len(done["shortlist"]) == 3

    err = ['data: {"kind":"error","error":"upstream exploded"}']
    fake_requests.post = lambda *a, **k: FakeResp(err)
    with pytest.raises(RuntimeError, match="upstream exploded"):
        F._stream_search("q", "novelty")

    nodone = ['data: {"kind":"start"}', 'data: {"kind":"end"}']
    fake_requests.post = lambda *a, **k: FakeResp(nodone)
    with pytest.raises(RuntimeError, match="without a done"):
        F._stream_search("q", "novelty")


def test_search_two_tier_does_not_federate_unless_asked(monkeypatch):
    """The expensive path must never fire implicitly — this is the money guard."""
    calls = []
    monkeypatch.setattr(F, "search", lambda *a, **k: calls.append(1))

    local = Result(ranked_pubs=[], family_ranked=[], channel_hits={}, query="q")

    class R:
        def search(self, *a, **k): return local
    out = F.search_two_tier(R(), "some out of domain query", detect_domain=False, wide=False)
    assert calls == []                 # nothing spent
    assert out is local


# --- mode availability (the INVALIDITY / FTO correctness fix) --------------------------------
S = Subject(number="US-1-A", efd=__import__("datetime").date(2020, 1, 1))


def test_invalidity_refuses_instead_of_silently_reusing_the_novelty_window():
    """THE BUG: invalidity used to return the novelty date window, producing a confident
    answer that ignored every ground except novelty."""
    with pytest.raises(ModeNotAvailable) as e:
        SM.citable_where(Mode.INVALIDITY, S)
    assert "not implemented" in str(e.value).lower()
    # and it is emphatically NOT the novelty fragment any more
    nov, _ = SM.citable_where(Mode.NOVELTY, S)
    assert nov  # novelty itself still works


def test_fto_refuses():
    with pytest.raises(ModeNotAvailable):
        SM.citable_where(Mode.FTO, S)


def test_refusals_are_still_notimplementederror_for_existing_handlers():
    # ModeNotAvailable subclasses NotImplementedError so old `except NotImplementedError`
    # handlers keep catching it
    assert issubclass(ModeNotAvailable, NotImplementedError)
    for m in (Mode.INVALIDITY, Mode.FTO, Mode.LANDSCAPE):
        with pytest.raises(NotImplementedError):
            SM.citable_where(m, S)


def test_require_available_gates_the_api_boundary():
    assert SM.require_available("novelty") is Mode.NOVELTY
    assert SM.require_available(Mode.INVENTIVE_STEP) is Mode.INVENTIVE_STEP
    with pytest.raises(ModeNotAvailable) as e:
        SM.require_available("invalidity")
    assert e.value.missing                       # tells the caller what is needed
    with pytest.raises(ValueError):
        SM.require_available("not_a_mode")


def test_available_modes_reports_exactly_the_two_working_modes():
    avail = [m["mode"] for m in SM.available_modes() if m["available"]]
    assert avail == ["novelty", "inventive_step"]
    for m in SM.available_modes():
        if not m["available"]:
            assert m["reason"]                   # never refuse without an explanation


def test_the_good_date_logic_is_preserved():
    """Regression guard: the EPC Art.54(2)/54(3) semantics must be untouched by this change."""
    nov, p = SM.citable_where(Mode.NOVELTY, S)
    assert "publication_date <" in nov and "earliest_priority_date" in nov and len(p) == 3
    inv, p2 = SM.citable_where(Mode.INVENTIVE_STEP, S)
    assert inv == "p.publication_date < %s" and p2 == [S.efd]   # public art only
