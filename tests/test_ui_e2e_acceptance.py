import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import ui_e2e_acceptance as U


def test_report_template_publishes_durable_pipeline_markers():
    #  The card markup moved to _refcard.html so the SAME card can be rendered mid-search and
    #  streamed onto an open page (webapp.api_cards). These markers are a contract about what the
    #  rendered page publishes, not about which file holds it, so the guard reads both templates —
    #  otherwise it fails on a move and passes on a deletion from the card template.
    root = Path(__file__).resolve().parents[1] / "templates"
    template = (root / "report.html").read_text() + (root / "_refcard.html").read_text()
    for marker in (
        'data-partial=',
        'data-cross-encoder-reranked=',
        'data-listwise-reranked=',
        'data-agent-rounds=',
        'data-family=',
    ):
        assert marker in template


def test_ui_uses_live_corpus_count_and_background_enrichment_contracts():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "app.js").read_text()
    css = (root / "static" / "style.css").read_text()
    base = (root / "templates" / "base.html").read_text()
    report = (root / "templates" / "report.html").read_text()
    admin_users = (root / "templates" / "admin_users.html").read_text()
    admin_searches = (root / "templates" / "admin_searches.html").read_text()
    print_template = (root / "templates" / "print.html").read_text()
    drafts = (root / "templates" / "drafts.html").read_text()
    about = (root / "templates" / "about.html").read_text()
    assert "107,795" not in js and "107795" not in js
    assert "CORPUS_PUBLICATIONS" in js and "CORPUS_PUBLICATIONS" in base
    assert "~108,000" not in about
    assert "warmRationales" in js and "applyCardRationale" in js
    assert "&rationale=1" in js
    assert "recoverBrokenInitialThumbs" in js and "img.onerror = attempt" in js
    assert "warmMissingThumbs" in js and "section=figs" in js
    assert "warmQueryClaimGrid" in js and "/api/query-claim-grid/" in js
    assert "Claim × reference grid" in report
    assert ".chartwrap .vh{left:0;top:0}" in css
    assert '.admintable td[data-label]::before' in css
    assert 'data-label="Manage"' in admin_users
    assert 'data-label="Notification"' in admin_searches
    assert 'class="adminsearch-title"' in admin_searches and "-webkit-line-clamp:4" in css
    assert "filename='style.css', v=asset_version" in base
    assert "filename='app.js', v=asset_version" in base
    assert "filename='style.css', v=asset_version" in print_template
    assert "draftlibrary-head" in drafts and ".draftlibrary-head>.btn" in css


def test_account_and_report_chrome_use_the_compact_release_contract():
    root = Path(__file__).resolve().parents[1]
    base = (root / "templates" / "base.html").read_text()
    login = (root / "templates" / "login.html").read_text()
    admin = (root / "templates" / "admin_users.html").read_text()
    report = (root / "templates" / "report.html").read_text()
    css = (root / "static" / "style.css").read_text()
    auth_source = (root / "src" / "auth.py").read_text()

    assert "Shared administrator" not in base + login + admin
    assert "admin-password" not in login
    assert "APP_PASSWORD" not in auth_source and "legacy_admin" not in auth_source
    assert 'class="scopewarning"' in report
    assert 'class="runsummary"' in report and "Read more" in report
    assert 'class="archivecompact"' in report
    assert 'class="tail reportlimits"' in report
    assert report.count('<span class="th">Identified but not readable here</span>') == 0
    assert report.count('<span class="th">Coverage ledger</span>') == 0
    assert report.count('<span class="th">Search scope and measured reliability</span>') == 0
    assert ".scopewarning-pop" in css and ".runsummary" in css and ".archivecompact" in css


def test_parser_collects_a_gold_family_group():
    args = U.parser().parse_args(
        [
            "--query",
            "representative disclosure",
            "--expect-any-family",
            "34201690",
            "--expect-any-family",
            "63449883",
        ]
    )
    assert args.expect_any_family == ["34201690", "63449883"]


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
