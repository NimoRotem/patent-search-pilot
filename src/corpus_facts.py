"""Live corpus scope, currency, and measured-reliability facts for the user-facing disclosures.

Everything a user is told about what this tool covers and how well it works comes from HERE, so
the claims on the search page and the results page cannot drift apart from each other or from
the database.

Two different kinds of fact, deliberately sourced differently:

  * SCOPE and CURRENCY are read from Postgres on demand and cached briefly. The publication-date
    ceiling in particular MUST NOT be hardcoded: a weekly incremental ingest advances it, and a
    stale "current to <date>" notice on a novelty search is itself a correctness defect. If the
    DB cannot be read we return None and the templates say so rather than inventing a number.

  * MEASURED RELIABILITY is a set of constants, because these are results of specific evaluation
    runs against specific artefacts, not something derivable at request time. Each carries its
    provenance and its SAMPLE SIZE, and data/reports/RELIABILITY.json can override them so a
    re-audit updates the UI without a code change.
"""
from __future__ import annotations

import threading
import time

import db
from config import SEED_CPC, SEED_CPC_TITLES, JURISDICTIONS

_CACHE = {"t": 0.0, "v": None}
_LOCK = threading.Lock()
_TTL = 900          # 15 min; ingest advances the ceiling at most weekly


# --- measured reliability -------------------------------------------------------------------
# Do not adjust these to look better. They are the reason the disclosures exist.
RECALL_AT_100 = 0.19
RECALL_BASIS = ("11-query internal benchmark with human-marked relevant families; "
                "fraction of known-relevant families appearing in the top 100 results")
RECALL_QUERIES = 11

RATIONALE_ERROR_RATE = 0.075          # 3 of 40 audited rationales overclaimed
RATIONALE_N = 40
RATIONALE_BASIS = ("independent LLM examiner audit of 40 generated 'why relevant' rationales, "
                   "graded against the exact reference text the generator was shown")

# The claim chart needs THREE numbers, not one, and the third one is the honest one.
#
#   PRE   -- 7 of 12 coordinate-backed cells were false positives before any verification existed.
#   POST  -- the verification pass rejected 0 of the 5 cells it still rendered as coverage.
#   INDEP -- an INDEPENDENT reviewer then re-read all 18 covered cells of grabo_gripper_novelty and
#            judged those same 5 surviving "discloses" verdicts against the verbatim cited
#            passages. 2 of the 5 are clear overclaims.
#
# POST alone is the verifier grading its own homework, and "0 of 5 rejected" read as reassurance
# the evidence does not support. It is kept only as the pass's self-reported tally and must never
# be quoted on its own. INDEP is what a reader should weigh. Both share the caveat that the
# verifier and the auditing judge are gemini-2.5-flash and therefore share blind spots -- which
# makes the independent finding a LOWER bound on the error rate, not an upper one.
CHART_FP_PRE = 0.583                  # 7 of 12 coordinate-backed cells were false positives
CHART_FP_PRE_N = 12
CHART_FP_POST_BAD = 0                 # self-reported: rejected by the verification pass itself
CHART_FP_POST_N = 5
CHART_INDEP_CHECKED = 5               # surviving "discloses" cells re-read by a human-directed review
CHART_INDEP_OVERCLAIM = 2             # of those, judged to overclaim against the verbatim passage
CHART_INDEP_BASIS = (
    "independent review of all 18 covered cells of the grabo_gripper_novelty report; the 5 cells "
    "the verification pass had confirmed as 'discloses' were re-read against the verbatim cited "
    "passages. 2 were judged clear overclaims -- e.g. EP-0176125-A1, an adhesive wall-fixing "
    "patent, matched to 'driver pin for mechanical coupling' on the words 'pin' and 'clamped' "
    "alone -- and one further cell's verdict was defensible but cited the wrong coordinate")
CHART_BASIS = ("independent LLM examiner audit of coordinate-backed claim-chart cells: the "
               "fraction whose cited passage does not actually disclose the element")


def _load_measured():
    """Let a re-audit override the constants without a code change, so the UI can never quote a
    figure older than the last measurement. Missing file = keep the constants above."""
    global RATIONALE_ERROR_RATE, RATIONALE_N, CHART_FP_PRE, CHART_FP_PRE_N
    global CHART_FP_POST_BAD, CHART_FP_POST_N, CHART_INDEP_CHECKED, CHART_INDEP_OVERCLAIM
    try:
        import json
        from config import DATA
        p = DATA / "reports" / "RELIABILITY.json"
        if p.exists():
            d = json.loads(p.read_text())
            RATIONALE_ERROR_RATE = d.get("rationale_error_rate", RATIONALE_ERROR_RATE)
            RATIONALE_N = d.get("rationale_n", RATIONALE_N)
            CHART_FP_PRE = d.get("chart_fp_pre", CHART_FP_PRE)
            CHART_FP_PRE_N = d.get("chart_fp_pre_n", CHART_FP_PRE_N)
            CHART_FP_POST_BAD = d.get("chart_fp_post_bad", CHART_FP_POST_BAD)
            CHART_FP_POST_N = d.get("chart_fp_post_n", CHART_FP_POST_N)
            CHART_INDEP_CHECKED = d.get("chart_indep_checked", CHART_INDEP_CHECKED)
            CHART_INDEP_OVERCLAIM = d.get("chart_indep_overclaim", CHART_INDEP_OVERCLAIM)
            return d
    except Exception:
        pass
    return {}


_MEASURED = _load_measured()


def _query_db():
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n, max(publication_date) AS mx, "
                    "min(publication_date) AS mn FROM publications")
        r = cur.fetchone()
        out = {"publications": r["n"], "max_date": r["mx"], "min_date": r["mn"]}
        try:
            cur.execute("SELECT count(*) AS n FROM chunks")
            out["chunks"] = cur.fetchone()["n"]
        except Exception:
            out["chunks"] = None
    return out


def facts(force: bool = False) -> dict:
    """Scope + currency + reliability in one dict for the templates.

    Never raises: a disclosure surface that 500s because the DB hiccuped would remove the warning
    entirely, which is the worst possible failure mode here. On error the counts come back None
    and the templates degrade to "unavailable" instead of to silence.
    """
    now = time.time()
    with _LOCK:
        if not force and _CACHE["v"] is not None and now - _CACHE["t"] < _TTL:
            live = _CACHE["v"]
        else:
            try:
                live = _query_db()
            except Exception:
                live = {"publications": None, "max_date": None, "min_date": None, "chunks": None}
            _CACHE["v"], _CACHE["t"] = live, now

    mx = live.get("max_date")
    return {
        "publications": live.get("publications"),
        "chunks": live.get("chunks"),
        "max_date": mx,
        "max_date_str": mx.isoformat() if mx else None,
        "min_date": live.get("min_date"),
        "min_year": live["min_date"].year if live.get("min_date") else None,
        "jurisdictions": JURISDICTIONS,
        "cpc_count": len(SEED_CPC),
        "cpc": [{"code": c, "title": SEED_CPC_TITLES.get(c, "")} for c in SEED_CPC],
        "field_summary": "vacuum gripping, lifting and handling",
        "recall_at_100": RECALL_AT_100,
        "recall_pct": int(round(RECALL_AT_100 * 100)),
        "recall_queries": RECALL_QUERIES,
        "recall_basis": RECALL_BASIS,
        "rationale_error_rate": RATIONALE_ERROR_RATE,
        "rationale_error_pct": (int(round(RATIONALE_ERROR_RATE * 100))
                                if RATIONALE_ERROR_RATE is not None else None),
        "rationale_n": RATIONALE_N,
        "rationale_basis": RATIONALE_BASIS,
        "chart_fp_pre_pct": (int(round(CHART_FP_PRE * 100)) if CHART_FP_PRE is not None else None),
        "chart_fp_pre_n": CHART_FP_PRE_N,
        "chart_fp_post_bad": CHART_FP_POST_BAD,
        "chart_fp_post_n": CHART_FP_POST_N,
        "chart_indep_checked": CHART_INDEP_CHECKED,
        "chart_indep_overclaim": CHART_INDEP_OVERCLAIM,
        "chart_indep_pct": (int(round(100.0 * CHART_INDEP_OVERCLAIM / CHART_INDEP_CHECKED))
                            if CHART_INDEP_CHECKED else None),
        "chart_indep_basis": CHART_INDEP_BASIS,
        "chart_basis": CHART_BASIS,
        "measured": _MEASURED,
    }
