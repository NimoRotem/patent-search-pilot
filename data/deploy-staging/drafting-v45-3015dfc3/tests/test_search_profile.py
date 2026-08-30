"""A description with no claims is a different search from a patent with claims.

Anchored on the measured runs of 2026-08-18..21 (app_saved_searches + data/reports):

  document WITH claims   3,097-7,243 s, charting 1,375-5,067 s, claim rescue 495-4,217 s
  typed description      the one measured no-claims deep run charted 292 references in 72 s and
                         ran no rescue at all -- and still spent an hour on two retrieval rounds,
                         a 2,500-candidate screen and a 400-document paid full-text fetch that
                         were sized for the claim attack.

The tests below guard the two halves of the fix: that the KIND is decided from the input, and
that the kind actually reaches the stages that cost the hour. A test that only checked the label
would pass while the pipeline kept running the expensive plan behind it.
"""
import deep_rank
import search_profile


# --- which search is this ---------------------------------------------------------------------
def test_no_claims_is_a_concept_search():
    assert search_profile.kind_for(None) == search_profile.CONCEPT
    assert search_profile.kind_for([]) == search_profile.CONCEPT


def test_claims_make_it_a_claim_attack():
    assert search_profile.kind_for([{"text": "A gripper comprising a seal."}]) \
        == search_profile.CLAIMS


def test_the_kind_matches_limitations_own_type_rule():
    """The engine already had this distinction in `limitations.search_type`; it just read it off a
    report that does not exist until the run is under way. The two must never disagree, or the
    page would name one search and the reader would chart the other."""
    import limitations
    claims = [{"text": "A gripper comprising a seal."}]
    assert limitations.search_type({"query_document": {"claims": claims}}) == limitations.TYPE_B
    assert search_profile.kind_for(claims) == search_profile.CLAIMS
    assert limitations.search_type({"query_document": {}}) == limitations.TYPE_A
    assert search_profile.kind_for([]) == search_profile.CONCEPT


# --- the claim attack is not touched ----------------------------------------------------------
def test_a_claim_attack_keeps_every_measured_constant():
    """Each constant in deep_rank carries a measurement in its comment. The concept split must not
    move any of them for the search those measurements were taken on."""
    p = search_profile.for_input([{"text": "A gripper comprising a seal."}])
    assert p.kind == search_profile.CLAIMS
    assert search_profile.budget_for(p) == {}
    b = deep_rank._budget(search_profile.budget_for(p))
    assert b["SCREEN_TOP"] == deep_rank.SCREEN_TOP
    assert b["CHART_TOP"] == deep_rank.CHART_TOP
    assert b["PRESCREEN_ENRICH_TOP"] == deep_rank.PRESCREEN_ENRICH_TOP
    assert p.rounds == 2


# --- the concept search is actually cheaper ---------------------------------------------------
def test_a_concept_search_cuts_the_stages_that_cost_the_hour():
    p = search_profile.for_input([])
    b = deep_rank._budget(search_profile.budget_for(p))
    assert p.rounds == 1, "the second retrieval round was half the local channel"
    assert b["SCREEN_TOP"] < deep_rank.SCREEN_TOP
    assert b["CHART_TOP"] < deep_rank.CHART_TOP, "reading is 97% of the bill"
    assert b["PRESCREEN_ENRICH_TOP"] < deep_rank.PRESCREEN_ENRICH_TOP, "this is the paid stage"
    assert b["ALWAYS_CHART_RETRIEVAL_HEAD"] < deep_rank.ALWAYS_CHART_RETRIEVAL_HEAD


def test_the_concept_screen_still_reaches_past_the_old_default():
    """900 is a cut from 2,500 and it is still four times the 600 the screen used for most of this
    repo's life. A cut that went below the depth this engine ever shipped would be a new risk, not
    a saving."""
    b = deep_rank._budget(search_profile.budget_for(search_profile.for_input([])))
    assert b["SCREEN_TOP"] >= 600


def test_the_blind_rescue_never_reaches_past_the_screen_it_was_cut_to():
    """BLIND_RESCUE defaults to the screen depth. Cutting the screen without carrying it down
    would leave the rescue reading candidates the screen never looked at."""
    b = deep_rank._budget(search_profile.budget_for(search_profile.for_input([])))
    assert b["BLIND_RESCUE"] <= b["SCREEN_TOP"]


def test_quick_is_left_exactly_as_it_was():
    """Quick already reads nothing in full and fetches no paid text; its recall was measured at
    its current screen width. The kind split must not narrow it as a side effect."""
    for claims in ([], [{"text": "A gripper comprising a seal."}]):
        p = search_profile.for_input(claims)
        assert search_profile.budget_for(p, depth="quick") == {}


def test_the_switch_restores_the_old_single_budget(monkeypatch):
    monkeypatch.setattr(search_profile, "ENABLED", False)
    p = search_profile.for_input([])
    assert search_profile.budget_for(p) == {}
    b = deep_rank._budget(search_profile.budget_for(p))
    assert b["SCREEN_TOP"] == deep_rank.SCREEN_TOP
    assert b["CHART_TOP"] == deep_rank.CHART_TOP


def test_an_explicit_environment_override_beats_the_profile(monkeypatch):
    """`DEEP_RANK_CHART_TOP=360 CLAIM_REACH=0` is how this repo restores a previous pipeline for
    an A/B. A profile budget that silently overrode it would make that A/B a lie."""
    monkeypatch.setenv("DEEP_RANK_CHART_TOP", "360")
    monkeypatch.setattr(deep_rank, "CHART_TOP", 360)
    cut = search_profile.budget_for(search_profile.for_input([]))
    assert cut["CHART_TOP"] < 360, "the profile does want to cut this one"
    b = deep_rank._budget(cut)
    assert b["CHART_TOP"] == 360, "the pinned value must survive the profile"
    assert b["SCREEN_TOP"] == cut["SCREEN_TOP"], "unpinned knobs are still cut"


# --- what the user is told --------------------------------------------------------------------
def test_each_kind_quotes_a_time_and_they_are_not_the_same():
    concept = search_profile.describe(search_profile.for_input([]))
    claims = search_profile.describe(search_profile.for_input([{"text": "A gripper."}]))
    assert concept["eta_text"] and claims["eta_text"]
    assert concept["eta_text"] != claims["eta_text"]
    #  The whole point: the cheap one must be quoted as cheaper.
    assert concept["eta_high"] < claims["eta_low"]


def test_quick_never_promises_reading_it_does_not_do():
    d = search_profile.describe(search_profile.for_input([]), depth="quick")
    assert d["depth"] == "quick"
    assert "read" not in d["summary"].split("escalate")[0].lower() or "Nothing is read" in d["summary"]
    assert d["eta_high"] <= search_profile.for_input([]).eta_high


def test_the_catalogue_covers_both_kinds():
    kinds = {k["kind"] for k in search_profile.catalogue()}
    assert kinds == {search_profile.CONCEPT, search_profile.CLAIMS}
    for k in search_profile.catalogue():
        assert k["label"] and k["summary"] and k["eta_text"]


def test_no_em_dash_in_anything_the_user_reads():
    """House rule, and it is easy to reintroduce in a summary string."""
    for k in search_profile.catalogue():
        for field in ("label", "summary", "eta_text", "unit"):
            assert "—" not in k[field], field
