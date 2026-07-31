#!/usr/bin/env python3
"""Acceptance probe for the live patent-search HTTP, agentic and federation pipeline.

The probe deliberately submits through POST /run and observes /status plus the rendered
partial/final reports. A successful exit means more than "the endpoint returned 200": useful cards
arrived inside the first-result budget, the full run met its latency budget, required pipeline
stages were observed, both rerankers produced real final-order signals, expected references were
ranked, grounding was present, and (for --wide) multiple external providers visibly contributed.

Run each representative disclosure separately so every result is a small, attributable JSON
artifact. By default cached reports fail the stage checks; this is intentional for a true E2E run.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

# `rank` and `round` are deliberately not polling requirements: the server can write either
# immediately before the next state, so a correct run can pass through between polls. The final
# HTML's explicit listwise marker and agent-round count are the durable proof. Likewise, a wide run
# need not pause in `federating` when its APIs finish before the local agent; contributing provider
# chips are the durable proof there.
DEFAULT_STAGES = ("elements", "partial", "seeded", "reranking", "done")


def elapsed(start: float) -> float:
    return round(time.monotonic() - start, 3)


def normalize_pub(pub: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(pub or "").upper())


def report_facts(body: str) -> dict:
    def data_bool(name: str):
        match = re.search(rf'\bdata-{re.escape(name)}="(true|false)"', body)
        return None if not match else match.group(1) == "true"

    def data_int(name: str):
        match = re.search(rf'\bdata-{re.escape(name)}="(\d+)"', body)
        return None if not match else int(match.group(1))

    card_tags = re.findall(r'<article class="refcard"[^>]*>', body)
    publications = []
    families = []
    for tag in card_tags:
        pub_match = re.search(r'\bdata-pub="([^"]+)"', tag)
        family_match = re.search(r'\bdata-family="([^"]*)"', tag)
        if pub_match:
            publications.append(html.unescape(pub_match.group(1)))
            families.append(html.unescape(family_match.group(1)) if family_match else "")
    source_tags = []
    for state, raw in re.findall(
        r'<span class="srctag s-([^" ]+)"[^>]*>(.*?)</span>\s*</span>',
        body,
        flags=re.DOTALL,
    ):
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        source_tags.append({"state": state, "text": " ".join(text.split())[:240]})
    verified = re.findall(r"(\d+)\s+verified", body)
    return {
        "status_code": None,
        "cards": len(publications),
        "publications": publications,
        "families": families,
        "first_publications": publications[:5],
        "source_tags": source_tags,
        "verified_references": max((int(n) for n in verified), default=0),
        "has_claim_grid": "Element × reference grid" in body,
        "has_refining_banner": 'id="refiningBanner"' in body,
        "partial": data_bool("partial"),
        "cross_encoder_reranked": data_bool("cross-encoder-reranked"),
        "listwise_reranked": data_bool("listwise-reranked"),
        "agent_rounds": data_int("agent-rounds"),
        "bytes": len(body.encode("utf-8")),
    }


def acceptance_failures(result: dict, args: argparse.Namespace) -> list[str]:
    failures = []
    first = result.get("first_ready") or {}
    final = result.get("final") or {}

    if not first:
        failures.append("no partial/first-ready report was observed")
    else:
        if first.get("status_code") != 200:
            failures.append(f"first-ready report returned HTTP {first.get('status_code')}")
        if first.get("cards", 0) < args.min_first_cards:
            failures.append(
                f"first-ready cards {first.get('cards', 0)} < {args.min_first_cards}"
            )
        if first.get("at_seconds", float("inf")) > args.max_first_seconds:
            failures.append(
                f"first-ready latency {first.get('at_seconds')}s > {args.max_first_seconds}s"
            )
        if first.get("partial") is not True:
            failures.append("first-ready report was not the progressive partial view")

    if not final:
        failures.append("no final report was observed")
        return failures
    if final.get("status_code") != 200:
        failures.append(f"final report returned HTTP {final.get('status_code')}")
    if final.get("cards", 0) < args.min_final_cards:
        failures.append(f"final cards {final.get('cards', 0)} < {args.min_final_cards}")
    if result.get("total_seconds", float("inf")) > args.max_total_seconds:
        failures.append(
            f"total latency {result.get('total_seconds')}s > {args.max_total_seconds}s"
        )
    if not final.get("has_claim_grid"):
        failures.append("final report has no element × reference grid")
    if final.get("has_refining_banner"):
        failures.append("final report still presents itself as partial")
    if final.get("partial") is not False:
        failures.append("final report is missing the final-view marker")
    if final.get("cross_encoder_reranked") is not True:
        failures.append("cross-encoder reranker did not produce real model scores")
    if final.get("listwise_reranked") is not True:
        failures.append("listwise agentic reranker did not produce the final order")
    if (final.get("agent_rounds") or 0) < args.min_agent_rounds:
        failures.append(
            f"agentic refinement rounds {final.get('agent_rounds') or 0} "
            f"< {args.min_agent_rounds}"
        )
    if final.get("verified_references", 0) < args.min_verified:
        failures.append(
            f"verified references {final.get('verified_references', 0)} < {args.min_verified}"
        )

    observed = {e.get("kind") for e in result.get("events") or []}
    required = set(args.require_stage or DEFAULT_STAGES)
    for stage in sorted(required - observed):
        failures.append(f"required pipeline stage not observed: {stage}")

    ranked = [normalize_pub(p) for p in final.get("publications", [])[: args.expected_top]]
    for pub in args.expect_pub or []:
        if normalize_pub(pub) not in ranked:
            failures.append(f"expected publication absent from top {args.expected_top}: {pub}")

    ranked_families = [
        normalize_pub(f) for f in final.get("families", [])[: args.expected_top]
    ]
    for family in args.expect_family or []:
        if normalize_pub(family) not in ranked_families:
            failures.append(
                f"expected family absent from top {args.expected_top}: {family}"
            )

    tags = final.get("source_tags") or []
    contributing = [
        tag
        for tag in tags
        if tag.get("state") in ("used", "degraded")
        and not tag.get("text", "").lower().startswith("local corpus")
    ]
    if args.wide and len(contributing) < args.min_external_sources:
        failures.append(
            f"contributing external sources {len(contributing)} < {args.min_external_sources}"
        )
    for source in args.require_source or []:
        match = next(
            (tag for tag in tags if source.lower() in tag.get("text", "").lower()),
            None,
        )
        if not match:
            failures.append(f"required source tag absent: {source}")
        elif match.get("state") not in ("used", "degraded"):
            failures.append(
                f"required source did not contribute: {source} ({match.get('state')})"
            )
    return failures


def run(args: argparse.Namespace) -> dict:
    base = args.base.rstrip("/") + "/"
    session = requests.Session()
    start = time.monotonic()
    result = {
        "label": args.label,
        "base": base,
        "query": args.query,
        "mode": args.mode,
        "wide": args.wide,
        "started_unix": time.time(),
        "thresholds": {
            "max_first_seconds": args.max_first_seconds,
            "max_total_seconds": args.max_total_seconds,
            "min_first_cards": args.min_first_cards,
            "min_final_cards": args.min_final_cards,
            "min_verified": args.min_verified,
            "min_agent_rounds": args.min_agent_rounds,
            "min_external_sources": args.min_external_sources,
        },
        "events": [],
    }

    payload = {
        "query": args.query,
        "mode": args.mode,
        "subject": "",
        "doc_token": "",
    }
    if args.wide:
        payload["wide"] = "1"
    response = session.post(
        urljoin(base, "run"),
        data=payload,
        allow_redirects=False,
        timeout=30,
    )
    result["run_http"] = response.status_code
    result["submit_seconds"] = elapsed(start)
    location = response.headers.get("Location", "")
    if response.status_code not in (302, 303) or "/report/" not in location:
        result["error"] = (
            f"POST /run did not redirect to a report: HTTP {response.status_code}"
        )
        return result

    report_url = urljoin(base, location)
    slug = urlparse(report_url).path.rsplit("/", 1)[-1]
    result.update({"slug": slug, "report_url": report_url})
    first_ready = None
    last_key = None

    while elapsed(start) <= args.timeout:
        status_response = session.get(urljoin(base, f"status/{slug}"), timeout=15)
        status_response.raise_for_status()
        status = status_response.json()
        key = (
            status.get("status"),
            status.get("kind"),
            status.get("msg"),
            json.dumps(status.get("detail"), sort_keys=True),
        )
        if key != last_key:
            result["events"].append(
                {
                    "at_seconds": elapsed(start),
                    "status": status.get("status"),
                    "kind": status.get("kind"),
                    "message": status.get("msg"),
                    "detail": status.get("detail"),
                }
            )
            last_key = key

        if status.get("ready") and first_ready is None:
            partial_response = session.get(report_url, timeout=90)
            facts = report_facts(partial_response.text)
            facts["status_code"] = partial_response.status_code
            first_ready = {"at_seconds": elapsed(start), **facts}
            result["first_ready"] = first_ready

        if status.get("done") or status.get("status") == "done":
            final_response = session.get(report_url, timeout=180)
            facts = report_facts(final_response.text)
            facts["status_code"] = final_response.status_code
            result["final"] = {"at_seconds": elapsed(start), **facts}
            result["total_seconds"] = elapsed(start)
            return result

        if status.get("status") == "error":
            result["error"] = status.get("msg") or "search job failed"
            result["total_seconds"] = elapsed(start)
            return result
        time.sleep(args.poll)

    result["error"] = f"timed out after {args.timeout:.0f}s"
    result["total_seconds"] = elapsed(start)
    return result


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8631/")
    ap.add_argument("--query", required=True)
    ap.add_argument("--label", default="representative-search")
    ap.add_argument("--mode", default="novelty", choices=("novelty", "inventive_step"))
    ap.add_argument("--wide", action="store_true", help="exercise configured external APIs")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--max-first-seconds", type=float, default=60.0)
    ap.add_argument("--max-total-seconds", type=float, default=300.0)
    ap.add_argument("--min-first-cards", type=int, default=1)
    ap.add_argument("--min-final-cards", type=int, default=10)
    ap.add_argument("--min-verified", type=int, default=1)
    ap.add_argument("--min-agent-rounds", type=int, default=1)
    ap.add_argument("--min-external-sources", type=int, default=4)
    ap.add_argument("--require-stage", action="append")
    ap.add_argument("--require-source", action="append")
    ap.add_argument("--expect-pub", action="append")
    ap.add_argument("--expect-family", action="append")
    ap.add_argument("--expected-top", type=int, default=25)
    ap.add_argument("--output", type=Path)
    return ap


def main() -> int:
    args = parser().parse_args()
    try:
        result = run(args)
    except requests.RequestException as exc:
        result = {
            "label": args.label,
            "base": args.base,
            "query": args.query,
            "mode": args.mode,
            "wide": args.wide,
            "error": f"{type(exc).__name__}: {exc}",
        }
    failures = [result["error"]] if result.get("error") else acceptance_failures(result, args)
    result["failures"] = failures
    result["passed"] = not failures
    body = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body + "\n")
    print(body)
    return 0 if result["passed"] else 5


if __name__ == "__main__":
    sys.exit(main())
