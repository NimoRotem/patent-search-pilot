"""The research slider: what each stop buys, and what it must never claim to have bought.

The whole point of the control is that one draft can be searched four ways at four prices. That
makes ONE mistake possible and expensive: presenting the cheap answer in the words of the dear
one. A ranked list from the cheapest level and a charted reading from the dearest are different
claims about the same draft, and an application amended on the strength of the first believing it
was the second is the failure this file exists to prevent.

So most of what is pinned here is the language, not the plumbing: which level says it read, which
says it measured, and what the agent is told when neither is true.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import research_levels as rl


SECTIONS = {
    "title": "Branch limited multi connector electric vehicle charging control",
    "summary": "A charging system in which an edge controller allocates the current of one "
               "electrical branch among several connectors and sheds load on overcurrent.",
    "claims": "1. A system comprising a plurality of electric vehicle connector assemblies, a "
              "branch current sensor, and an edge controller configured to allocate the "
              "available charging current among the connector assemblies.",
    "detailed_description": "The branch current sensor is positioned upstream of the connector "
                            "assemblies and reports the total branch current to the edge "
                            "controller, which assigns a current limit to each connector.",
}


# =============================================================================================
# The ladder
# =============================================================================================
def test_there_are_four_stops_and_they_are_ordered_by_what_they_cost():
    assert [item["id"] for item in rl.LEVELS] == ["scan", "find", "ledger", "full"]


def test_every_stop_names_a_depth_the_search_pipeline_actually_accepts():
    """`webapp.run` allowlists these three. A fifth name here would 400 at the boundary and the
    panel would show a level nobody can run."""
    assert {item["depth"] for item in rl.LEVELS} <= {"quick", "ledger", "deep"}


def test_the_depths_match_the_ones_the_front_page_offers():
    """Guards against the two drifting apart, which would make the slider a private vocabulary."""
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    assert 'if depth not in ("quick", "ledger", "deep")' in source


def test_only_the_deepest_stop_claims_to_read_or_to_chart():
    reads = [item["id"] for item in rl.LEVELS if item["reads"]]
    charts = [item["id"] for item in rl.LEVELS if item["charts"]]
    assert reads == ["full"]
    assert charts == ["full"]


def test_the_cheap_stops_go_to_the_local_corpus_and_only_the_deepest_federates():
    """The external APIs are metered. A level advertised as under a minute must not spend on them."""
    assert [item["wide"] for item in rl.LEVELS] == [False, False, False, True]


def test_every_stop_says_how_long_it_takes():
    """A control that promises a minute and takes twenty is worse than one that says twenty."""
    for item in rl.LEVELS:
        assert item["eta"].strip()
        assert item["what"].strip()


def test_an_unknown_level_is_refused_rather_than_defaulted():
    """Defaulting a typo to the cheapest level would run a search nobody asked for; defaulting it
    to the dearest would spend an hour on one."""
    with pytest.raises(ValueError):
        rl.level("thorough")
    with pytest.raises(ValueError):
        rl.level("")


def test_the_public_shape_carries_what_the_slider_has_to_print():
    for item in rl.public():
        assert set(item) == {"id", "label", "what", "eta", "reads", "charts", "depth"}


# =============================================================================================
# What becomes the query
# =============================================================================================
def test_the_cheapest_level_searches_a_sentence_and_says_so(monkeypatch):
    """Measured in query_set: a 30-word essence beat the whole brief by 33 dense ranks, so the
    cheap level is not a truncation of the dear one, it is the strongest single vector."""
    import query_set
    monkeypatch.setattr(query_set, "build", lambda *a, **k: [
        query_set.QuerySpec(name="essence", kind="essence",
                            text="An electric vehicle charging system that allocates one "
                                 "branch's current among several connectors and sheds load on "
                                 "overcurrent.")])
    material = rl.material_for("scan", SECTIONS, SECTIONS["title"])
    assert material["query"].startswith("An electric vehicle charging system")
    assert len(material["query"]) < len(rl.draft_query(SECTIONS))
    assert "one-sentence" in material["note"]


def test_every_other_level_searches_the_whole_draft_and_says_so():
    material = rl.material_for("find", SECTIONS)
    assert SECTIONS["claims"][:40] in material["query"]
    assert "title, summary, claims and description" in material["note"]


def test_a_draft_with_nothing_in_it_is_refused_rather_than_searched():
    """Searching on a two-word title returns the field, not the invention, and a reader cannot
    tell that from a genuinely empty result."""
    with pytest.raises(ValueError) as caught:
        rl.material_for("find", {"title": "A clamp"})
    assert "not have enough technical detail" in str(caught.value)


def test_the_essence_falls_back_to_the_draft_when_the_model_is_out(monkeypatch):
    """A model outage costs the phrasing of the cheapest level, never the level."""
    import query_set
    monkeypatch.setattr(query_set, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    text = rl.essence_query(rl.draft_query(SECTIONS), SECTIONS["title"])
    assert len(text) > 20
    assert "charging" in text.lower()


def test_the_query_is_bounded_so_a_quarter_megabyte_disclosure_cannot_become_a_slug():
    sections = dict(SECTIONS, detailed_description="x " * 200_000)
    assert len(rl.draft_query(sections)) <= rl.MAX_QUERY_CHARS


# =============================================================================================
# What the drafting agent is told
# =============================================================================================
REFERENCES = [
    {"publication_number": "US-2014062401-A1", "title": "Power control apparatus",
     "publication_date": "2014-03-06", "relevance_summary": "shares current between vehicles"},
    {"publication_number": "EP-4538101-A1", "title": "Charging multiple vehicles",
     "publication_date": "2025-04-16", "relevance_summary": ""},
]
READING = {"ok": True, "n_elements": 14, "closest_coverage": 0.5,
           "closest_pub": "US-2014062401-A1", "closest_title": "Power control apparatus",
           "uncovered_elements": ["a branch current sensor upstream of the connectors"]}


def test_a_level_that_read_nothing_says_so_in_as_many_words():
    """This is the one that matters. A dense ranking is not a disclosure, and an agent told that
    the top of one 'discloses' a limitation will write the concession into the application."""
    text = rl.redraft_request(label="Find", level_id="find", slug="abc",
                              note="searched on the whole draft", references=REFERENCES)
    assert "did NOT read them in full" in text
    assert "did NOT chart them against your claims" in text
    assert "nothing above says that any of them discloses any limitation" in text
    assert "MEASURED AGAINST YOUR INDEPENDENT CLAIMS" not in text


def test_a_level_that_read_nothing_is_not_handed_a_measurement_even_if_one_is_passed():
    """The reading belongs to the tier that built the chart. Passing one in from anywhere else
    would put a number on a level that never measured."""
    text = rl.redraft_request(label="Find", level_id="find", slug="abc", note="n",
                              references=REFERENCES, reading=READING)
    assert "MEASURED AGAINST" not in text
    assert "50%" not in text


def test_the_deepest_level_states_the_measurement_and_the_open_ground():
    text = rl.redraft_request(label="Full reading", level_id="full", slug="abc", note="n",
                              references=REFERENCES, reading=READING)
    assert "MEASURED AGAINST YOUR INDEPENDENT CLAIMS" in text
    assert "7 of their 14 elements (50%)" in text
    assert "a branch current sensor upstream of the connectors" in text
    assert "strongest ground" in text


def test_the_deepest_level_says_when_everything_was_disclosed_rather_than_implying_room():
    text = rl.redraft_request(label="Full reading", level_id="full", slug="abc", note="n",
                              references=REFERENCES,
                              reading={**READING, "uncovered_elements": []})
    assert "Every element of the independent claims was disclosed" in text


def test_the_deepest_level_with_no_chart_does_not_pretend_to_have_one():
    text = rl.redraft_request(label="Full reading", level_id="full", slug="abc", note="n",
                              references=REFERENCES, reading={"ok": False})
    assert "MEASURED AGAINST" not in text
    assert "read the references in full but produced no claim chart" in text


def test_every_reference_is_named_with_its_date_and_why_it_surfaced():
    text = rl.redraft_request(label="Find", level_id="find", slug="abc", note="n",
                              references=REFERENCES)
    assert "US-2014062401-A1 (2014-03-06)" in text
    assert "shares current between vehicles" in text
    assert "EP-4538101-A1 (2025-04-16)" in text


def test_the_search_id_is_in_the_message_so_the_turn_can_be_traced_to_the_run():
    text = rl.redraft_request(label="Find", level_id="find", slug="adhoc-9f21", note="n",
                              references=REFERENCES)
    assert "adhoc-9f21" in text


def test_the_request_asks_for_the_citation_token_where_the_text_relies_on_it():
    text = rl.redraft_request(label="Find", level_id="find", slug="abc", note="n",
                              references=REFERENCES)
    assert "[REF:KEY]" in text
    assert "prior_art/INDEX.md" in text
    assert "not in a list at the end" in text


def test_the_request_carries_the_two_rules_that_have_cost_whole_rounds_before():
    text = rl.redraft_request(label="Full reading", level_id="full", slug="abc", note="n",
                              references=REFERENCES, reading=READING)
    assert "DO NOT buy distance from the art with scope." in text
    assert "no new numbered part" in text.lower()
    assert "draft/numerals.md" in text


def test_no_em_dash_reaches_the_agent_or_the_page():
    """House rule, and the agent copies this prose into an application."""
    text = rl.redraft_request(label="Full reading", level_id="full", slug="abc", note="n",
                              references=REFERENCES, reading=READING)
    assert "—" not in text
    for item in rl.LEVELS:
        assert "—" not in item["what"] and "—" not in item["eta"]


# =============================================================================================
# Wiring
# =============================================================================================
def test_the_level_column_is_created_on_boot():
    import draft_studio
    names = [path.name for path in draft_studio._MIGRATIONS]
    assert "026_draft_research_level.sql" in names


def test_the_studio_serves_the_slider_and_one_research_route():
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    assert 'def draft_studio_research(project_id)' in source
    assert 'def draft_studio_research_redraft(project_id, slug)' in source
    assert 'research_levels.public()' in source
    #  The three that were consolidated are gone, not hidden.
    assert "quickart" not in source
    assert len(re.findall(r'@app\.route\("/drafts/<int:project_id>/studio/research"', source)) == 1


def test_a_research_run_is_recorded_in_the_users_own_history_like_any_other_search():
    """Without this line a research run is findable only from inside the draft that started it,
    which is not what "saved as any other search" means."""
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    route = source[source.index("def draft_studio_research(project_id)"):
                   source.index("def _research_run_payload")]
    assert "accounts.record_search(" in route
    assert "ensure_report(" in route
    assert "search_slug(" in route


# =============================================================================================
# Two things that only showed up against the live server
# =============================================================================================
def test_a_running_search_is_read_through_the_durable_store_not_the_memory_job():
    """Measured on a live Scan: the worker held a fresh lease, had found 4,356 families and had
    written a 124 KB partial report, and the panel said "interrupted by a server restart and did
    not resume". `_job_event` sees a partial report with no in-process claim and reports it dead,
    and the queue row it checks for the other half of that branch is the LEGACY queue, which a
    durable run never appears in, so the wrong branch was the only branch reachable.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    helper = source[source.index("def _search_event(slug)"):source.index("def _research_run_payload")]
    assert "durable_runs_enabled()" in helper
    assert "_durable_lookup(slug)" in helper and "_durable_event(slug, run)" in helper
    #  and the "we cannot tell" case is answered as such rather than as a failure
    assert "_durable_unavailable_event(slug)" in helper
    #  the two payloads that report a search's state both go through it
    for payload in ("def _research_run_payload", "def _draft_search_payload"):
        block = source[source.index(payload):source.index(payload) + 1400]
        assert "_search_event(" in block, payload


def test_the_studio_fetches_its_cards_from_its_own_app_not_from_the_root_one():
    """The two apps keep SEPARATE report directories. /api/cards and /report belong to the search
    app at the root of this domain, which cannot see a report the drafting app generated: the
    studio asking there gets somebody else's 401, and a report link there is a 404 dressed as a
    feature. Confirmed live before this route existed.
    """
    root = Path(__file__).resolve().parents[1]
    source = (root / "src" / "webapp.py").read_text(encoding="utf-8")
    script = (root / "static" / "draft_studio.js").read_text(encoding="utf-8")
    assert 'def api_draft_research_cards(project_id, slug)' in source
    #  Under /api/drafts/, which is the prefix the proxy hands to this app.
    assert '@app.route("/api/drafts/<int:project_id>/research/<slug>/cards")' in source
    #  Both routes render through one function, so the studio's card cannot drift from the
    #  report's.
    assert source.count("def _cards_response(slug, refdraw_base") == 1
    assert "_cards_response(slug)" in source          # the report page's own call
    #  ...and the studio's call names where ITS drawings live, because /refdrawing/ is answered
    #  by the root app out of a figure directory that has never seen these files.
    assert 'refdraw_base=f"{request.script_root}/api/drafts/{int(project_id)}"' in source
    assert 'def api_draft_reference_drawing(project_id, pub, fname)' in source
    #  The panel never reaches for the root app's paths.
    assert "/api/drafts/${PID}/research/${encodeURIComponent(slug)}/cards" in script
    assert "`${BASE}/api/cards/" not in script
    assert "report_url" not in script


def test_the_cards_route_refuses_a_slug_this_draft_did_not_start():
    """Otherwise it is a way to read any report on the server by guessing a slug."""
    source = (Path(__file__).resolve().parents[1] / "src" / "webapp.py").read_text(
        encoding="utf-8")
    route = source[source.index("def api_draft_research_cards"):
                   source.index("def api_draft_research_cards") + 1600]
    assert "_draft_identity()" in route
    assert "studio._project(principal, project_id)" in route
    assert "repository.search(project_id, slug)" in route
    assert "valid_slug(slug)" in route


def test_the_document_viewer_is_wired_off_its_own_markup_not_off_the_report_page():
    """openDetail() is reachable from a card's inline onclick, so the slide-over OPENED in the
    studio and nothing had bound its close button, its back button, its backdrop or Escape. The
    handlers sat below `if (!document.getElementById('cards')) return;`, and that guard means
    "this is the report page", which stopped being the same thing as "this page shows a
    reference". A reader could open a document and had no way out of it.
    """
    from pathlib import Path
    #  THIS tree's copy. When the studio was at nimo.iptorch.com the browser loaded the root app's
    #  app.js and this pointed there; that deployment was archived on 2026-09-01 and the studio is
    #  at rotem.ai/patents, where /patents/static/app.js is served by this app. The test followed
    #  the file rather than the path, which is why it started skipping instead of failing.
    script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
        encoding="utf-8")
    wiring = script.index("document.getElementById('soClose').addEventListener")
    guard = script.index("if (!document.getElementById('cards')) return;")
    assert wiring < guard, "the viewer wiring is behind the report-page guard again"
    assert "if (document.getElementById('soOverlay')) {" in script
    #  and the drawing base is overridable, which is the other half of the same split
    assert "window.REFDRAW_BASE" in script


# =============================================================================================
# The statement that updates a search row
# =============================================================================================
def sets_for(**kw):
    import draft_studio
    base = dict(status="complete", imported_count=None, n_results=None, reading=None,
                redrafted_turn_id=None)
    base.update(kw)
    return draft_studio._search_update_sets(**base)


def test_a_field_that_is_not_changing_is_absent_from_the_statement():
    """Reported live on draft 21: "Use to redraft" answered "could not determine data type of
    parameter $5". The statement wrote `reading = CASE WHEN %s IS NULL THEN reading ELSE %s::jsonb
    END` and bound None to the first placeholder. There is nothing in `%s IS NULL` for Postgres to
    infer a type from, so it refused the WHOLE statement, and every call without a reading failed:
    which is every call except the deepest research level, including the one people press.
    """
    sets, args = sets_for(redrafted_turn_id=42)
    joined = " ".join(sets)
    assert "reading" not in joined
    assert "imported_count" not in joined
    assert "n_results" not in joined
    assert "redrafted_turn_id=%s" in joined
    #  and nothing is bound as an untyped NULL
    assert None not in args
    assert len(args) == joined.count("%s")


def test_every_optional_field_is_written_when_it_is_supplied():
    sets, args = sets_for(imported_count=5, n_results=60, reading={"ok": True},
                          redrafted_turn_id=9)
    joined = " ".join(sets)
    for column in ("imported_count=%s", "n_results=%s", "reading=%s::jsonb",
                   "redrafted_turn_id=%s"):
        assert column in joined, column
    assert None not in args
    assert len(args) == joined.count("%s")


def test_the_status_is_always_written_and_completion_is_stamped_once():
    sets, args = sets_for()
    joined = " ".join(sets)
    assert joined.startswith("status=%s")
    assert "coalesce(completed_at,now())" in joined, "a second completion must not move the stamp"
    assert args[:2] == ["complete", "complete"]


@pytest.mark.parametrize("kw", [
    {},
    {"redrafted_turn_id": 1},
    {"imported_count": 0},
    {"n_results": 0},
    {"reading": {}},
    {"imported_count": 3, "n_results": 60, "reading": {"ok": True}, "redrafted_turn_id": 9},
])
def test_no_call_shape_binds_a_parameter_postgres_cannot_type(kw):
    """The bug was one untyped NULL. This walks every shape the app makes, including the zeros
    and the empty dict, which are falsy and must still be written rather than skipped."""
    sets, args = sets_for(**kw)
    joined = " ".join(sets)
    assert None not in args
    assert len(args) == joined.count("%s")
    for key in kw:
        assert key.split("_")[0] in joined or key in joined, key


def test_the_two_quick_tier_stops_do_not_promise_different_speeds():
    """Measured end to end on this corpus 2026-09-01: Scan 288s, Find 277s. They are the same
    tier and the tier's time goes on retrieval and screening, not on the length of the query, so
    a label making one sound faster than the other is a promise the pipeline cannot keep."""
    quick = [item for item in rl.LEVELS if item["depth"] == "quick"]
    assert len(quick) == 2
    assert len({item["eta"] for item in quick}) == 1, [item["eta"] for item in quick]
    #  and none of them claims a minute, which is what search_profile's older numbers said
    assert not any("under a minute" in item["eta"] for item in rl.LEVELS)


def test_the_ladder_never_promises_a_dearer_stop_is_faster():
    order = [item["eta"] for item in rl.LEVELS]
    assert order == ["about 5 minutes", "about 5 minutes", "5 to 20 minutes",
                     "20 minutes to an hour"]
