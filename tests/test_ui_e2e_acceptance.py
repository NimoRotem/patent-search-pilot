import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import ui_e2e_acceptance as U


def test_report_template_publishes_durable_pipeline_markers():
    template = (Path(__file__).resolve().parents[1] / "templates" / "report.html").read_text()
    for marker in (
        'data-partial=',
        'data-cross-encoder-reranked=',
        'data-listwise-reranked=',
        'data-agent-rounds=',
    ):
        assert marker in template


def _args(**overrides):
    values = {
        "max_first_seconds": 60.0,
        "max_total_seconds": 300.0,
        "min_first_cards": 1,
        "min_final_cards": 10,
        "min_verified": 1,
        "min_agent_rounds": 1,
        "wide": True,
        "min_external_sources": 4,
        "require_stage": None,
        "require_source": ["USPTO", "Lens"],
        "expect_pub": ["US-1234567-A1"],
        "expect_family": ["FAM-123"],
        "expect_any_family": ["FAM-X", "FAM-123"],
        "expected_top": 25,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _result():
    kinds = list(U.DEFAULT_STAGES)
    return {
        "events": [{"kind": kind} for kind in kinds],
        "first_ready": {
            "status_code": 200, "cards": 25, "at_seconds": 28.0, "partial": True,
        },
        "final": {
            "status_code": 200,
            "cards": 25,
            "publications": ["US1234567A1", "EP-7654321-B1"],
            "families": ["FAM-123", "FAM-456"],
            "source_tags": [
                {"state": "used", "text": "Local corpus 25"},
                {"state": "used", "text": "SerpApi 22"},
                {"state": "used", "text": "BigQuery 500"},
                {"state": "degraded", "text": "USPTO 75 — partial results"},
                {"state": "used", "text": "Lens 125"},
            ],
            "verified_references": 4,
            "has_claim_grid": True,
            "has_refining_banner": False,
            "partial": False,
            "cross_encoder_reranked": True,
            "listwise_reranked": True,
            "agent_rounds": 2,
        },
        "total_seconds": 220.0,
    }


def test_report_facts_extracts_cards_sources_and_grounding():
    body = """
      <div class="wrap" data-partial="false" data-cross-encoder-reranked="true"
           data-listwise-reranked="true" data-agent-rounds="2">
      <span class="srctag s-used"><span class="sdot"></span>SerpApi<b>22</b>
        <span class="vh">— used</span></span>
      <span class="srctag s-degraded"><span class="sdot"></span>USPTO<b>75</b>
        <span class="vh">— partial results</span></span>
      <article class="refcard" data-pub="US-123-A1" data-family="FAM-123"></article>
      <article class="refcard" data-pub="EP-456-B1" data-family="FAM-456"></article>
      Element × reference grid · 4 verified
      </div>
    """
    facts = U.report_facts(body)
    assert facts["publications"] == ["US-123-A1", "EP-456-B1"]
    assert facts["families"] == ["FAM-123", "FAM-456"]
    assert [t["state"] for t in facts["source_tags"]] == ["used", "degraded"]
    assert facts["verified_references"] == 4 and facts["has_claim_grid"] is True
    assert facts["partial"] is False
    assert facts["cross_encoder_reranked"] is True
    assert facts["listwise_reranked"] is True
    assert facts["agent_rounds"] == 2


def test_acceptance_passes_only_with_latency_stages_sources_and_expected_rank():
    assert U.acceptance_failures(_result(), _args()) == []


def test_durable_round_count_does_not_depend_on_transient_round_event():
    result = _result()
    assert all(event["kind"] != "round" for event in result["events"])
    assert result["final"]["agent_rounds"] == 2
    assert U.acceptance_failures(result, _args()) == []


def test_acceptance_reports_every_missing_contract():
    result = _result()
    result["first_ready"]["at_seconds"] = 61.0
    result["final"].update({
        "cards": 3,
        "verified_references": 0,
        "has_claim_grid": False,
        "has_refining_banner": True,
        "publications": ["US-999-A1"],
        "families": ["FAM-999"],
        "source_tags": [{"state": "used", "text": "Local corpus 3"}],
        "partial": True,
        "cross_encoder_reranked": False,
        "listwise_reranked": False,
        "agent_rounds": 0,
    })
    result["events"] = [{"kind": "partial"}, {"kind": "done"}]
    result["total_seconds"] = 301.0
    failures = U.acceptance_failures(result, _args())
    joined = "\n".join(failures)
    for expected in (
        "first-ready latency",
        "final cards",
        "total latency",
        "no element × reference grid",
        "still presents itself as partial",
        "missing the final-view marker",
        "cross-encoder reranker did not produce real model scores",
        "listwise agentic reranker did not produce the final order",
        "agentic refinement rounds",
        "verified references",
        "required pipeline stage not observed",
        "expected publication absent",
        "expected family absent",
        "none of the expected families",
        "contributing external sources",
        "required source tag absent",
    ):
        assert expected in joined
