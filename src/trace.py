"""One record per (subject, candidate family): where it came from and where it died.

WHY
---
"Was this reference retrieved and then dropped, or never retrieved at all?" could not be answered
from a finished report, and that is the first question every experiment asks. Without it, a miss
was attributed by guesswork, and guesswork attributed the EP 3 707 092 failure to reach when the
six-subject data later showed more was lost to ranking.

Every candidate that enters the pipeline gets a row. Every row ends at exactly one terminal stage
from a fixed enum, so misses can be counted by cause rather than described. A row whose stage is
UNKNOWN is a pipeline defect, not a category.

Cheap by construction: one dict per family, updated in place as it passes each stage, written once
at the end next to the report.
"""
from __future__ import annotations

import json
import os
import threading
import traceback

#  TERMINAL STAGES. Fixed set; adding one is a deliberate schema change, not an accident.
INELIGIBLE = "INELIGIBLE"                          # outside the date/jurisdiction window
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"          # the source that would hold it failed
NOT_RETRIEVED = "NOT_RETRIEVED"                    # no channel returned it
CHANNEL_TRUNCATED = "CHANNEL_TRUNCATED"            # a channel had it, past its cutoff
FUSION_TRUNCATED = "FUSION_TRUNCATED"              # fused, past the ranked-list cutoff
DEDUPED = "DEDUPED"                                # collapsed into another family
SCREEN_REJECTED = "SCREEN_REJECTED"                # screened and scored too low to read
NOT_SELECTED_FOR_READING = "NOT_SELECTED_FOR_READING"   # passed the screen, no read budget
READ_NO_EVIDENCE = "READ_NO_EVIDENCE"              # read in full, grounded nothing
CHARTING_FAILURE = "CHARTING_FAILURE"              # read, but the chart errored
PORTFOLIO_EXCLUDED = "PORTFOLIO_EXCLUDED"          # charted with evidence, not selected
TOP_50 = "TOP_50"                                  # in the delivered portfolio
UNKNOWN = "UNKNOWN"                                # a defect: must never be a resting state

STAGES = (INELIGIBLE, SOURCE_UNAVAILABLE, NOT_RETRIEVED, CHANNEL_TRUNCATED, FUSION_TRUNCATED,
          DEDUPED, SCREEN_REJECTED, NOT_SELECTED_FOR_READING, READ_NO_EVIDENCE,
          CHARTING_FAILURE, PORTFOLIO_EXCLUDED, TOP_50, UNKNOWN)

FIELDS = ("subject_id", "family_id", "publication_number", "source", "retrieval_channel",
          "query_id", "disclosure_id", "raw_rank", "raw_score", "channel_cutoff_passed",
          "fusion_score", "fused_rank", "dedupe_status", "dedupe_reason", "screen_score",
          "screen_decision", "read_selected", "read_depth", "chart_completed",
          "supported_disclosures", "grounding_status", "refuter_status", "portfolio_selected",
          "final_rank", "exclusion_stage", "exclusion_reason")

ENABLED = os.environ.get("CANDIDATE_TRACE_ENABLED", "1") != "0"
TRACE_DIR = os.environ.get("CANDIDATE_TRACE_DIR", "")


class Trace:
    """Per-run candidate trace. Thread-safe: the pipeline fans out across workers."""

    def __init__(self, subject_id="", slug="", enabled=None):
        self.subject_id = subject_id
        self.slug = slug
        self.enabled = ENABLED if enabled is None else bool(enabled)
        self._rows = {}
        self._lock = threading.Lock()

    # -- recording ----------------------------------------------------------------------------
    def seen(self, family_id, **fields):
        """Record or update a candidate. Channels are accumulated, not overwritten: a family
        found by three channels is evidence of agreement and overwriting loses it."""
        if not self.enabled or not family_id:
            return
        with self._lock:
            row = self._rows.get(family_id)
            if row is None:
                row = {k: None for k in FIELDS}
                row.update(subject_id=self.subject_id, family_id=family_id,
                           retrieval_channel=[], source=[], query_id=[], disclosure_id=[],
                           exclusion_stage=UNKNOWN)
                self._rows[family_id] = row
            for k, v in fields.items():
                if k not in FIELDS:
                    continue
                if isinstance(row.get(k), list):
                    for item in (v if isinstance(v, (list, tuple, set)) else [v]):
                        if item is not None and item not in row[k]:
                            row[k].append(item)
                elif k == "raw_rank" and row.get(k) is not None and v is not None:
                    row[k] = min(row[k], v)          # best rank any channel gave it
                else:
                    row[k] = v

    def stage(self, family_id, stage, reason=""):
        """Set the terminal stage. Later stages overwrite earlier ones, because a candidate that
        reaches the portfolio passed everything before it."""
        if not self.enabled or not family_id:
            return
        with self._lock:
            row = self._rows.get(family_id)
            if row is None:
                self.seen(family_id)
                row = self._rows[family_id]
            row["exclusion_stage"] = stage
            if reason:
                row["exclusion_reason"] = str(reason)[:300]

    def stage_many(self, family_ids, stage, reason=""):
        for f in family_ids or ():
            self.stage(f, stage, reason)

    # -- reporting ----------------------------------------------------------------------------
    def counts(self):
        with self._lock:
            out = {}
            for r in self._rows.values():
                s = r.get("exclusion_stage") or UNKNOWN
                out[s] = out.get(s, 0) + 1
            return out

    def rows(self):
        with self._lock:
            return [dict(r) for r in self._rows.values()]

    def unknown(self):
        """Candidates with no terminal stage. Any of these is a pipeline defect."""
        return [r["family_id"] for r in self.rows() if r.get("exclusion_stage") == UNKNOWN]

    def write(self, path=None):
        """One JSON lines file next to the report. Never raises: losing a trace must not lose a
        report, but a lost trace is logged rather than swallowed."""
        if not self.enabled:
            return ""
        try:
            path = path or os.path.join(TRACE_DIR or ".", f"{self.slug or 'run'}.trace.jsonl")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                for r in self.rows():
                    fh.write(json.dumps(r, default=str) + "\n")
            os.replace(tmp, path)
            return path
        except Exception:
            traceback.print_exc()
            return ""


def from_report(rep, subject_id="", slug="", view=None):
    """Reconstruct the full candidate funnel from a finished report.

    The pipeline already records what each stage saw (`ranked_families`, `channel_families`, and
    deep_rank's `candidates`, `screen_scores`, `order`, `unread`, `not_readable`); what it never
    did was join them into one row per family with a single terminal stage. Reconstructing here
    means every report ALREADY ON DISK becomes attributable, instead of attribution starting from
    the next run.

    Live per-query and per-disclosure provenance still has to be recorded during the run; this
    supplies the stage attribution, which is the part every experiment asks for first.
    """
    t = Trace(subject_id=subject_id, slug=slug, enabled=True)
    dr = (rep or {}).get("deep_rank") or {}
    ranked = list((rep or {}).get("ranked_families") or [])
    by_pub = dr.get("by_pub") or {}
    fam_of_pub = {p: (e or {}).get("family") for p, e in by_pub.items()}

    #  which channel(s) produced each family
    for chan, fams in ((rep or {}).get("channel_families") or {}).items():
        for f in fams or ():
            t.seen(f, retrieval_channel=chan)

    #  everything the fusion ranked
    for i, f in enumerate(ranked, 1):
        t.seen(f, fused_rank=i)
        t.stage(f, FUSION_TRUNCATED,
                f"ranked {i}, below the screen cutoff") if i > (dr.get("n_candidates") or 0) \
            else t.stage(f, NOT_SELECTED_FOR_READING, "screened, not read")

    #  screened: candidates carries the publications the screen actually saw
    screen = dr.get("screen_scores") or {}
    cand_fams = set(dr.get("candidate_families") or [])
    for f in cand_fams:
        t.seen(f)
    for pub, sc in screen.items():
        f = fam_of_pub.get(pub)
        if not f:
            continue
        t.seen(f, publication_number=pub, screen_score=sc, screen_decision="screened")

    #  read in full, with evidence or without
    for i, pub in enumerate(dr.get("order") or [], 1):
        e = by_pub.get(pub) or {}
        f = e.get("family")
        if not f:
            continue
        covered = [c for c in (e.get("covered") or [])
                   if c.get("verdict") in ("disclosed", "partial")]
        t.seen(f, publication_number=pub, read_selected=True,
               read_depth=e.get("chars_read"), chart_completed=True,
               supported_disclosures=len(covered), final_rank=i,
               screen_score=e.get("screen"))
        t.stage(f, PORTFOLIO_EXCLUDED if covered else READ_NO_EVIDENCE,
                "charted, outside the delivered portfolio" if covered
                else "read in full, grounded no disclosure")

    #  screened but never read
    for pub in (dr.get("unread") or {}):
        f = fam_of_pub.get(pub)
        if f:
            t.stage(f, SCREEN_REJECTED, f"screen {dr['unread'][pub]}, below the read threshold")

    #  the delivered portfolio
    for c in ((view or {}).get("cards") or []):
        pub = c.get("pub")
        f = fam_of_pub.get(pub)
        if not f:
            continue
        t.seen(f, portfolio_selected=True, final_rank=c.get("rank"))
        t.stage(f, TOP_50, "")
    return t


def attribute(trace, gold_family_ids):
    """Where each gold family died. -> {family_id: stage} plus a per-stage tally.

    This is the whole point of the trace: a miss becomes a cause instead of a description.
    """
    by = {r["family_id"]: r for r in trace.rows()}
    out, tally = {}, {}
    for fam in gold_family_ids or ():
        stage = (by.get(fam) or {}).get("exclusion_stage") or NOT_RETRIEVED
        out[fam] = stage
        tally[stage] = tally.get(stage, 0) + 1
    return out, tally
