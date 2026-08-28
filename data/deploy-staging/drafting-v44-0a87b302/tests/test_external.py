"""External art fan-out: planning, junk rejection, weighted fusion, date citability.

Hermetic. Nothing here calls an API or the database; the pieces that would are exercised through
their pure inputs and outputs, which is where every defect this module was written to fix lived.
"""
import sys
import types

import pytest

import external


# --- plan ------------------------------------------------------------------------------------
def _fake_llm(monkeypatch, payload):
    monkeypatch.setattr(external.llm, "chat_json", lambda *a, **k: payload)


class _Spec:
    def __init__(self, name, text, kind):
        self.name, self.text, self.kind = name, text, kind


ONE_ASPECT = {"aspects": [{
    "name": "Noise in an air stream",
    "problem": "reducing the noise of a moving air stream",
    "devices": ["lawn mower", "vacuum cleaner"],
    "keywords": ["muffler", "silencer", "acoustic damping"],
    "cpc": ["G10K", "F01N", "A47L"],
    "blurb": "A device that attenuates the noise of a fast moving air stream.",
}]}


def test_plan_emits_one_bigquery_query_per_cpc_subclass(monkeypatch):
    """Four subclasses in ONE query share one row budget and the largest crowds out the rest.

    This is why a document sitting in exactly the subclass we asked about never came back.
    """
    _fake_llm(monkeypatch, ONE_ASPECT)
    p = external.plan([_Spec("brief", "a gripper that is quiet", "brief")],
                      brief="a gripper that is quiet")
    bq = [q for q in p["queries"] if q["source"] == "bigquery_gpatents" and q["element"] != "whole invention"]
    assert len(bq) == 3
    assert [q["cpc"] for q in bq] == [["G10K"], ["F01N"], ["A47L"]]
    assert all(len(q["cpc"]) == 1 for q in bq)


def test_plan_asks_every_source_and_stays_under_the_cap(monkeypatch):
    _fake_llm(monkeypatch, {"aspects": ONE_ASPECT["aspects"] * 9})
    p = external.plan([_Spec("essence", "quiet vacuum gripper", "essence")],
                      brief="quiet vacuum gripper")
    assert len(p["queries"]) <= external.MAX_QUERIES
    assert {q["source"] for q in p["queries"]} >= {"pqai", "bigquery_gpatents", "uspto"}


def test_plan_survives_an_llm_outage(monkeypatch):
    """No aspects must still leave the deterministic whole-invention queries."""
    _fake_llm(monkeypatch, {})
    p = external.plan([_Spec("essence", "quiet vacuum gripper", "essence")],
                      brief="quiet vacuum gripper")
    assert p["aspects"] == []
    assert any(q["source"] == "pqai" for q in p["queries"])


def test_title_words_splits_phrases_and_drops_filler():
    """BigQuery and USPTO match the TITLE column only, so a phrase matches nothing as a phrase."""
    got = external._title_words(["acoustic damping", "muffler", "sound absorbing device", "a"])
    assert "muffler" in got and "acoustic" in got and "damping" in got
    assert "device" not in got            # in _TITLE_STOP: it discriminates nothing
    assert "acoustic damping" not in got  # the unsplit phrase is useless


# --- junk rejection --------------------------------------------------------------------------
@pytest.mark.parametrize("pub", ["US35530491", "US35530032", "US99999999"])
def test_application_numbers_are_rejected(pub):
    """The USPTO adapter falls back to US<applicationNumber>; those are not publications.

    Without this they are INSERTED into the corpus as permanent rows with a title and no document.
    """
    assert not external.plausible(pub)


@pytest.mark.parametrize("pub", ["US11413727B2", "US2966138A", "US20140008929A1",
                                 "EP3707092B1", "DE3724659A1", "CN104925305B",
                                 "WO2009025557A1"])
def test_real_publications_are_kept(pub):
    assert external.plausible(pub)


def test_best_records_drops_junk_and_keeps_the_richest():
    cands = [
        {"pub_number": "US35530491", "title": "junk", "abstract": "x" * 500},
        {"pub_number": "US11413727B2", "title": "Vacuum gripper", "abstract": "short"},
        {"pub_number": "US-11413727-B2", "title": "Vacuum gripper", "abstract": "a much longer one"},
    ]
    got = external.best_records(cands)
    assert list(got) == ["US11413727B2"]
    assert got["US11413727B2"]["abstract"] == "a much longer one"


# --- dates -----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("2020-10-06", "2020-10-06"), ("20201006", "2020-10-06"),
    ("2020/10/06", "2020-10-06"), ("", None), ("2020", None),
    ("9999-99-99", None), (None, None),
])
def test_date_parsing(raw, want):
    assert external._date(raw) == want


# --- fusion ----------------------------------------------------------------------------------
def _cands(spec):
    """spec: [(query_i, source, [pub, ...])] -> candidate dicts with source_rank 1..n"""
    out = []
    for qi, src, pubs in spec:
        for i, p in enumerate(pubs, 1):
            out.append({"pub_number": p, "source": src, "source_rank": i, "query_i": qi})
    return out


def test_channel_depth_cap_lets_a_strong_single_hit_win():
    """RRF uncapped rewards breadth: rank 250 in ten channels beat rank 3 in one.

    That is backwards when the point is to find art in a remote field, which by construction only
    one aspect asks about. It is also unearned, because past the first hundred rows a
    title-keyword channel is ordered by RAND().
    """
    deep = [f"F{i}" for i in range(1, 300)]
    strong = ["S1", "S2", "REMOTE"]
    spec = [(0, "pqai", strong)] + [(i, "bigquery_gpatents", deep) for i in range(1, 11)]
    fam_of = {p: p for _, _, pubs in spec for p in pubs}
    chans = external.channels(_cands(spec), fam_of)
    assert all(len(c) <= external.CHANNEL_DEPTH for c in chans.values())
    ranked = [f for f, _ in external.fuse_families(chans)]
    # the tail of the deep channels contributes nothing at all
    assert "F250" not in ranked
    # and one source asked ten correlated ways is still ONE source's opinion
    assert ranked.index("REMOTE") < ranked.index("F99")


def test_source_weight_prefers_the_semantic_engine():
    """A semantic hit and a 'the word was in the title' hit are not equal evidence."""
    spec = [(0, "pqai", ["SEM"]), (1, "bigquery_gpatents", ["KW"])]
    fam_of = {"SEM": "SEM", "KW": "KW"}
    ranked = external.fuse_families(external.channels(_cands(spec), fam_of))
    assert ranked[0][0] == "SEM"
    assert ranked[0][1] > ranked[1][1]


def test_a_family_found_by_several_aspects_outranks_one_found_once():
    """Breadth still breaks ties -- it just cannot overturn a strong hit any more."""
    spec = [(0, "pqai", ["A", "B"]), (1, "pqai", ["B", "C"]), (2, "pqai", ["B"])]
    fam_of = {p: p for p in "ABC"}
    ranked = [f for f, _ in external.fuse_families(external.channels(_cands(spec), fam_of))]
    assert ranked[0] == "B"


def test_fusion_is_capped_to_the_merge_quota():
    spec = [(0, "pqai", [f"P{i}" for i in range(1, 60)])]
    fam_of = {p: p for _, _, pubs in spec for p in pubs}
    ranked = external.fuse_families(external.channels(_cands(spec), fam_of), limit=10)
    assert len(ranked) == 10


def test_in_corpus_hits_rank_under_their_local_family():
    """Cross-system agreement must COLLAPSE onto the local row, not compete with it."""
    records = {"US11413727B2": {"pub_number": "US11413727B2", "family_id": "999"}}
    fam_of = {"US11413727B2": "localfam-42"}
    chans = external.channels(_cands([(0, "pqai", ["US11413727B2"])]), fam_of)
    assert [f for f, _ in external.fuse_families(chans)] == ["localfam-42"]
    assert records  # the source's own family id is not used when the corpus knows better


# --- citability ------------------------------------------------------------------------------
class _Subj:
    number = None
    efd = None
    filing_date = None
    publication_date = None
    jurisdiction = None
    strict_secret_jurisdiction = False


def test_citable_passes_everything_through_without_a_subject():
    fams = [("a", 1.0, 1), ("b", 0.5, 2)]
    assert external.citable(fams, None, "novelty") == fams
    assert external.citable(fams, _Subj(), None) == fams


def test_citable_never_drops_everything_on_an_unsupported_mode(monkeypatch):
    """A mode search_modes refuses must leave the families alone, not silently empty the channel."""
    fams = [("a", 1.0, 1)]
    monkeypatch.setitem(sys.modules, "search_modes", types.SimpleNamespace(
        Mode=lambda m: (_ for _ in ()).throw(ValueError("unsupported")),
        citable_where=lambda *a: (_ for _ in ()).throw(ValueError("unsupported"))))
    assert external.citable(fams, _Subj(), "invalidity") == fams


# --- summary ---------------------------------------------------------------------------------
def test_summary_is_display_ready_and_never_raises():
    assert external.summary({}) == {}
    s = external.summary({"ok": True, "aspects": ONE_ASPECT["aspects"], "queries": [{}] * 5,
                          "n_candidates": 15035, "n_families": 400, "n_new": 341,
                          "stats": {"pqai": {"hits": 1584}}, "elapsed": 79.4})
    assert s["n_queries"] == 5 and s["per_source"] == {"pqai": 1584}
    assert s["aspects"][0]["cpc"] == ["G10K", "F01N", "A47L"]


# --- semantic rescoring ----------------------------------------------------------------------
def test_rescore_promotes_the_semantically_closer_family(monkeypatch):
    """Until this stage a candidate has never been compared to the invention at all.

    Sharing a keyword with one ASPECT of the problem is not the same as being about the same
    invention, and a title-keyword channel returns four hundred documents that share a word.
    """
    ranked = [("FAR", 0.9), ("NEAR", 0.1)]
    records = {"a": {"pub_number": "US1A", "title": "far", "abstract": "unrelated"},
               "b": {"pub_number": "US2A", "title": "near", "abstract": "the invention"}}
    fam_of = {"a": "FAR", "b": "NEAR"}
    fake = types.SimpleNamespace(
        embed_query=lambda t, *a, **k: [1.0, 0.0],
        embed_texts=lambda texts, dim, task_type=None: [
            [0.0, 1.0] if "unrelated" in t else [1.0, 0.0] for t in texts],
    )
    monkeypatch.setitem(sys.modules, "embed", fake)
    out = [f for f, _ in external.rescore(ranked, records, fam_of, "the invention")]
    assert out[0] == "NEAR"


def test_rescore_returns_the_input_order_when_embedding_fails(monkeypatch):
    """A fan-out ranked only by fusion is still far better than no fan-out."""
    ranked = [("A", 0.9), ("B", 0.1)]
    records = {"a": {"pub_number": "US1A", "title": "a", "abstract": "x"}}
    fam_of = {"a": "A"}
    boom = types.SimpleNamespace(
        embed_query=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vertex down")),
        embed_texts=lambda *a, **k: [])
    monkeypatch.setitem(sys.modules, "embed", boom)
    assert external.rescore(ranked, records, fam_of, "brief") == ranked


def test_rescore_is_a_no_op_without_a_brief():
    assert external.rescore([("A", 1.0)], {}, {}, "  ") == [("A", 1.0)]
