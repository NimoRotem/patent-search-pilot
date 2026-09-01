"""The drafting agent's corpus tools: the search, the chart, and the proposals.

None of this needs Postgres or a model. The chart is exercised through a stub of
``claim_chart.build_chart`` that returns whatever verdicts a test hands it, because what has to
be right here is the REDUCTION: which reference is nearest, which elements nothing disclosed,
and that a refuted cell is not a disclosure. The search is exercised through a fake connection,
because what has to be right there is the grouping: one row per family, best passage first,
attached references marked rather than hidden.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import draft_agent_tools  # noqa: E402


# =============================================================================================
# Claims into elements
# =============================================================================================
CLAIMS = """1. A clamp for aligning a first pipe end and a second pipe end, the clamp comprising:
a first jaw and a second jaw hinged on a common pin;
a first magnet array carried by the first jaw; and
a registration shoulder projecting from the first jaw and presenting an abutment face.

2. The clamp of claim 1, wherein the shoulder is a fixed step.

3. A method of fitting up a pipe joint, the method comprising:
laying a clamp across the joint; and
bearing a shoulder of the clamp against a pipe end to set a root gap.

4. A method of using the clamp of claim 1, comprising releasing the magnet array with one lever.
"""


def test_elements_are_the_semicolon_clauses_after_the_transition():
    parsed = draft_agent_tools.claim_elements(
        "1. A clamp for aligning pipes, the clamp comprising: a first jaw; a second jaw; and "
        "a shoulder on the first jaw.")
    assert parsed["preamble"] == "A clamp for aligning pipes, the clamp"
    assert parsed["elements"] == ["a first jaw", "a second jaw", "a shoulder on the first jaw"]


def test_a_run_on_claim_is_split_at_its_wherein_clauses():
    parsed = draft_agent_tools.claim_elements(
        "A valve comprising a body having a bore, wherein the bore is threaded, and wherein "
        "the body is cast")
    assert parsed["elements"] == ["a body having a bore", "wherein the bore is threaded",
                                  "wherein the body is cast"]


def test_the_preamble_is_never_an_element():
    parsed = draft_agent_tools.claim_elements("A widget comprising: a thing; another thing.")
    assert all("widget" not in element for element in parsed["elements"])


def test_independent_claims_follow_the_studio_reading_including_new_subject_claims():
    """Claim 4 names its own subject and is independent for the Office; it is charted too."""
    items = draft_agent_tools.independent_claims(CLAIMS)
    assert [item["number"] for item in items] == [1, 3, 4]
    assert items[0]["elements"][0].startswith("a first jaw and a second jaw")
    assert len(items[0]["elements"]) == 3


# =============================================================================================
# The reduction
# =============================================================================================
def _stub_chart(verdicts_by_pub):
    """A build_chart that answers with the verdicts the test chose, per publication."""
    def build_chart(elements, pub, ref=None):
        rows = []
        for element in elements:
            verdict, entailment = verdicts_by_pub.get(pub, {}).get(element, ("absent", ""))
            rows.append({"element": element, "verdict": verdict, "entailment": entailment,
                         "quote": "a quote" if verdict != "absent" else "",
                         "location": "claim 1" if verdict != "absent" else ""})
        return {"pub": pub, "found": True, "method": "llm", "rows": rows, "stats": {}}
    return build_chart


@pytest.fixture()
def charted(monkeypatch):
    module = types.ModuleType("claim_chart")
    module._load_reference = lambda pub: {"found": True, "pub": pub, "title": pub,
                                          "passages": [{"kind": "abstract", "coord": {},
                                                        "label": "abstract", "text": "text"}]}
    monkeypatch.setitem(sys.modules, "claim_chart", module)

    def install(verdicts):
        module.build_chart = _stub_chart(verdicts)
    return install


REFS = [{"publication_number": "US-1000000-A", "title": "Old clamp"},
        {"publication_number": "US-2000000-A", "title": "Older clamp"}]


def test_the_headline_is_the_nearest_single_reference_and_uncovered_elements_are_named(charted):
    e1 = "a first jaw and a second jaw hinged on a common pin"
    e2 = "a first magnet array carried by the first jaw"
    e3 = "a registration shoulder projecting from the first jaw and presenting an abutment face"
    charted({"US-1000000-A": {e1: ("disclosed", "confirmed"), e2: ("disclosed", "confirmed")},
             "US-2000000-A": {e1: ("disclosed", "confirmed")}})
    reading = draft_agent_tools.novelty(claims_text=CLAIMS, references=REFS, workers=1)
    claim1 = reading["claims"][0]
    assert claim1["number"] == 1 and claim1["n_elements"] == 3
    assert claim1["closest"]["pub"] == "US-1000000-A"
    assert claim1["closest"]["disclosed"] == 2
    assert claim1["uncovered"] == [e3]
    assert claim1["combination"] == pytest.approx(2 / 3, abs=1e-3)
    assert reading["closest_coverage"] == pytest.approx(2 / 3, abs=1e-3)


def test_a_refuted_cell_is_not_a_disclosure(charted):
    e1 = "a first jaw and a second jaw hinged on a common pin"
    charted({"US-1000000-A": {e1: ("disclosed", "refuted")}})
    reading = draft_agent_tools.novelty(claims_text=CLAIMS, references=REFS[:1], workers=1)
    claim1 = reading["claims"][0]
    assert claim1["closest"]["disclosed"] == 0
    assert e1 in claim1["uncovered"]


def test_a_partial_cell_is_reported_as_weak_not_as_disclosed_and_not_as_uncovered(charted):
    e2 = "a first magnet array carried by the first jaw"
    charted({"US-1000000-A": {e2: ("partial", "")}})
    reading = draft_agent_tools.novelty(claims_text=CLAIMS, references=REFS[:1], workers=1)
    claim1 = reading["claims"][0]
    assert claim1["closest"]["disclosed"] == 0
    assert e2 in claim1["weak"] and e2 not in claim1["uncovered"]


def test_only_the_asked_for_references_are_charted(charted):
    charted({})
    reading = draft_agent_tools.novelty(claims_text=CLAIMS, references=REFS,
                                        publications=["US-2000000-A"], workers=1)
    assert [item["pub"] for item in reading["references"]] == ["US-2000000-A"]


def test_an_uploaded_document_is_charted_under_its_index_key(charted):
    charted({})
    documents = [{"kind": "prior_art", "publication_number": None, "title": "A brochure",
                  "filename": "brochure.pdf", "body": "Paragraph one of the brochure.\n\n"
                  "Paragraph two of the brochure about jaws."}]
    reading = draft_agent_tools.novelty(claims_text=CLAIMS, references=[], documents=documents,
                                        workers=1)
    assert reading["references"][0]["pub"] == "UPLOAD-01"
    assert reading["references"][0]["has_text"]


def test_nothing_attached_is_said_plainly(charted):
    charted({})
    with pytest.raises(draft_agent_tools.ToolError, match="No prior art is attached"):
        draft_agent_tools.novelty(claims_text=CLAIMS, references=[], workers=1)


def test_the_rendering_carries_the_headline_and_the_caveat(charted):
    e3 = "a registration shoulder projecting from the first jaw and presenting an abutment face"
    charted({"US-1000000-A": {e3: ("disclosed", "confirmed")}})
    reading = draft_agent_tools.novelty(claims_text=CLAIMS, references=REFS[:1], workers=1)
    text = draft_agent_tools.render_novelty(reading)
    assert "CLAIM 1 (3 elements" in text
    assert "US-1000000-A discloses 1 of 3" in text
    assert "not progress" in text                      # the narrowing warning
    compacted = draft_agent_tools.compact(reading)
    assert compacted["claims"][0]["references"][0]["rows"][0]["element"]


# =============================================================================================
# The search
# =============================================================================================
class _Cursor:
    def __init__(self, rows_by_sql):
        self._rows_by_sql = rows_by_sql
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._rows = []
        for key, rows in self._rows_by_sql.items():
            if key in sql:
                self._rows = rows
                return

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, rows_by_sql):
        self.rows_by_sql = rows_by_sql

    def cursor(self):
        return _Cursor(self.rows_by_sql)

    def close(self):
        pass


def test_search_groups_chunks_by_publication_and_family_and_marks_what_is_attached(monkeypatch):
    chunk_rows = [
        {"publication_id": 1, "kind": "paragraph", "coord": {"para": 4}, "text": "a hinged jaw",
         "score": 0.81},
        {"publication_id": 1, "kind": "claim_own", "coord": {"claim_no": 1}, "text": "claim one",
         "score": 0.70},
        {"publication_id": 2, "kind": "abstract", "coord": {}, "text": "another clamp",
         "score": 0.79},
        {"publication_id": 3, "kind": "paragraph", "coord": {"para": 1}, "text": "same family",
         "score": 0.78},
    ]
    pub_rows = [
        {"id": 1, "publication_number": "US-1000000-A", "kind_code": "A", "country": "US",
         "title": "Clamp one", "abstract": "", "publication_date": "1970-01-01",
         "earliest_priority_date": "1969-01-01", "simple_family_id": "F1"},
        {"id": 2, "publication_number": "US-2000000-A", "kind_code": "A", "country": "US",
         "title": "Clamp two", "abstract": "", "publication_date": "1980-01-01",
         "earliest_priority_date": None, "simple_family_id": "F2"},
        {"id": 3, "publication_number": "GB-3000000-A", "kind_code": "A", "country": "GB",
         "title": "Clamp one again", "abstract": "", "publication_date": "1971-01-01",
         "earliest_priority_date": None, "simple_family_id": "F1"},
    ]
    fake_db = types.ModuleType("db")
    fake_db.connect = lambda **kw: _Conn({"FROM chunks c": chunk_rows,
                                         "FROM publications WHERE": pub_rows})
    fake_embed = types.ModuleType("embed")
    fake_embed.embed_query = lambda text: [0.1, 0.2]
    fake_embed._vec = lambda e: "[0.1,0.2]"
    monkeypatch.setitem(sys.modules, "db", fake_db)
    monkeypatch.setitem(sys.modules, "embed", fake_embed)

    result = draft_agent_tools.search_corpus("a hinged clamp with a shoulder", top=5,
                                             attached=["US-2000000-A"])
    pubs = [hit["pub"] for hit in result["hits"]]
    assert pubs == ["US-1000000-A", "US-2000000-A"]      # GB-3000000-A is the same family
    first = result["hits"][0]
    assert first["matched"] == "paragraph 4" and first["passage"] == "a hinged jaw"
    assert result["hits"][1]["attached"] is True
    text = draft_agent_tools.render_search(result)
    assert "already attached" in text and "--attach" in text


def test_a_search_needs_something_to_search_for():
    with pytest.raises(draft_agent_tools.ToolError):
        draft_agent_tools.search_corpus("jaw")


def test_relevancy_is_calibrated_like_the_report_cards():
    assert draft_agent_tools.relevancy(0.90) == 99
    assert draft_agent_tools.relevancy(0.35) == 1
    assert 60 <= draft_agent_tools.relevancy(0.70) <= 70


# =============================================================================================
# Proposals
# =============================================================================================
PROPOSALS_MD = """# Proposals for the inventor

## 1. Index the shoulder in discrete steps
Feature: a detent ring with four positions.
Why: clears US-1000000-A, which sets the gap continuously.
Confirm: that the shoulder can be made in stepped form.

## Keeper plate spring return
Feature: a spring that returns the keeper.
"""


def test_proposals_are_read_one_per_heading_with_optional_numbers():
    items = draft_agent_tools.parse_proposals(PROPOSALS_MD)
    assert [item["title"] for item in items] == ["Index the shoulder in discrete steps",
                                                 "Keeper plate spring return"]
    assert items[0]["no"] == 1 and items[1]["no"] is None
    assert items[0]["body"].startswith("Feature: a detent ring")


def test_merging_keeps_statuses_numbers_new_ones_and_never_drops_an_adopted_one():
    existing = [{"no": 1, "title": "Index the shoulder in discrete steps", "body": "old",
                 "status": "adopted", "version_no": 2},
                {"no": 2, "title": "Something the agent removed", "body": "gone",
                 "status": "open", "version_no": 2}]
    merged = draft_agent_tools.merge_proposals(
        existing, draft_agent_tools.parse_proposals(PROPOSALS_MD), version_no=3)
    by_no = {item["no"]: item for item in merged}
    assert by_no[1]["status"] == "adopted" and by_no[1]["body"] == "old"   # adopted text is frozen
    assert by_no[2]["title"] == "Something the agent removed"              # still in the record
    assert by_no[3]["title"] == "Keeper plate spring return"
    assert by_no[3]["status"] == "open" and by_no[3]["version_no"] == 3


def test_the_written_back_file_carries_each_status():
    text = draft_agent_tools.render_proposals(
        [{"no": 1, "title": "A", "body": "b", "status": "dismissed"}])
    assert "## 1. A" in text and "Status: dismissed" in text


def test_the_daily_ceiling_on_novelty_checks_holds(monkeypatch):
    monkeypatch.setattr(draft_agent_tools, "_RUNS", {})
    monkeypatch.setattr(draft_agent_tools, "NOVELTY_RUNS_PER_DAY", 2)
    monkeypatch.setitem(sys.modules, "llm_spend_guard", None)
    draft_agent_tools.check_budget(99)
    draft_agent_tools.check_budget(99)
    with pytest.raises(draft_agent_tools.ToolError, match="ceiling"):
        draft_agent_tools.check_budget(99)


def test_a_job_reports_progress_and_then_its_result():
    import time

    def work(progress):
        progress("1/1 charted")
        return {"text": "done"}

    seen = []
    job_id = draft_agent_tools.start_job(7, work, on_done=lambda result: seen.append(result))
    for _ in range(50):
        state = draft_agent_tools.job(job_id, 7)
        if state["status"] == "done":
            break
        time.sleep(0.02)
    assert state["status"] == "done" and state["result"] == {"text": "done"}
    assert seen == [{"text": "done"}]
    assert draft_agent_tools.job(job_id, 8) is None           # another project cannot read it
