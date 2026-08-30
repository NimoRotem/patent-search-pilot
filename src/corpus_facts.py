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

import datetime
import json
import os
import threading
import time

import db
from config import SEED_CPC, SEED_CPC_TITLES, JURISDICTIONS, DATA

_CACHE = {"t": 0.0, "v": None, "refreshing": False, "last_attempt": 0.0}
_LOCK = threading.Lock()
# Exact corpus scans cover millions of rows. They are deliberately refreshed in a daemon thread:
# a cold cache or an expired value must never put those scans on a page-rendering request. During
# bulk embedding an exact refresh has taken more than two minutes, and the old implementation held
# _LOCK for that entire time, serialising every template behind it.
_TTL = 3600         # scope/currency changes slowly; one exact refresh per hour is sufficient
_RETRY_TTL = 60     # do not start one failing background query per incoming request
# A country must hold at least this share of the corpus to be listed as an indexed jurisdiction.
# Below it, a few rows dragged in by a family/citation hop would otherwise read as "we cover that
# office", which is exactly the kind of overstatement the disclosure exists to prevent.
_JURIS_MIN_SHARE = 0.005            # 0.5%


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
        #  Jurisdictions are read from the DATA, not from a config constant.
        #
        #  They used to come from config.JURISDICTIONS = ["US","EP","WO","DE"], and disclosure.py
        #  turned that into the sentence "US, EP, WO, DE only — no JP, CN, KR, GB, FR or other
        #  national collections". The moment the corpus was widened past those four that sentence
        #  became false — in the one document on the page whose entire job is to state the tool's
        #  limits honestly. A disclosure that can drift out of step with the corpus is worse than
        #  no disclosure, so it now reports what is actually in the table.
        #
        #  Countries below the threshold are still counted but not listed: a handful of stray rows
        #  from a citation-expansion hop is not "coverage" of that office, and listing it would
        #  overstate scope in the other direction.
        try:
            cur.execute("SELECT country AS cc, count(*) AS n FROM publications "
                        "WHERE country IS NOT NULL AND country <> '' "
                        "GROUP BY country ORDER BY n DESC")
            rows = [(r["cc"], r["n"]) for r in cur.fetchall()]
            total = sum(n for _cc, n in rows) or 1
            out["jurisdictions"] = [cc for cc, n in rows if n / total >= _JURIS_MIN_SHARE]
            out["jurisdictions_all_n"] = len(rows)
            out["jurisdictions_trace"] = [cc for cc, n in rows
                                          if n / total < _JURIS_MIN_SHARE]
        except Exception:
            out["jurisdictions"] = None
            out["jurisdictions_all_n"] = None
            out["jurisdictions_trace"] = []
    return out


def _empty_live():
    return {"publications": None, "max_date": None, "min_date": None, "chunks": None}


#  The exact scans run over 4.8M publications and 26M chunks and, while a bulk embed is running,
#  have taken minutes. The refresh is already off the request path, but that left a COLD-START
#  HOLE: for the first minutes after a restart every page rendered the fallback, so the public
#  landing page said "millions of publications" and listed the four configured offices instead of
#  the ten the corpus actually holds. A wrong scope statement is precisely what this module exists
#  to prevent, so the last successful answer is persisted and re-read at start-up. It is labelled
#  with the time it was taken; it is not invented.
_SNAPSHOT = DATA / "corpus_facts.json"


def _save_snapshot(live):
    try:
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(live)
        for k in ("max_date", "min_date"):
            if payload.get(k) is not None:
                payload[k] = payload[k].isoformat()
        payload["_taken"] = time.time()
        tmp = _SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        os.replace(tmp, _SNAPSHOT)
    except Exception:
        pass


def _load_snapshot():
    try:
        d = json.loads(_SNAPSHOT.read_text())
    except Exception:
        return None
    for k in ("max_date", "min_date"):
        v = d.get(k)
        if isinstance(v, str) and v:
            try:
                d[k] = datetime.date.fromisoformat(v[:10])
            except ValueError:
                d[k] = None
    return d


def _refresh_cache():
    """Refresh exact facts without occupying a request or holding the cache lock."""
    try:
        live = _query_db()
    except Exception:
        live = None
    now = time.time()
    if live is not None:
        _save_snapshot(live)
    with _LOCK:
        if live is not None:
            _CACHE["v"], _CACHE["t"] = live, now
        _CACHE["refreshing"] = False


def _live_facts(force: bool):
    """Return a current-or-stale snapshot and refresh it off the request path.

    `force=True` preserves the original synchronous behaviour for explicit maintenance callers.
    Normal web requests return immediately: stale data is preferable to making the search page
    unavailable while an exact COUNT/GROUP BY competes with corpus ingestion.
    """
    if force:
        try:
            live = _query_db()
        except Exception:
            with _LOCK:
                return _CACHE["v"] or _empty_live()
        _save_snapshot(live)          # a forced refresh is the one most likely to be a warm-up
        with _LOCK:
            _CACHE["v"], _CACHE["t"] = live, time.time()
        return live

    now = time.time()
    start_refresh = False
    with _LOCK:
        if _CACHE["v"] is None:
            #  Serve the last successful answer immediately after a restart, and still refresh.
            snap = _load_snapshot()
            if snap:
                _CACHE["v"] = snap
                _CACHE["t"] = 0.0        # zero, not the snapshot time: force a refresh anyway
        live = _CACHE["v"]
        stale = live is None or now - _CACHE["t"] >= _TTL
        retry_ready = now - _CACHE["last_attempt"] >= _RETRY_TTL
        if stale and not _CACHE["refreshing"] and retry_ready:
            _CACHE["refreshing"] = True
            _CACHE["last_attempt"] = now
            start_refresh = True

    if start_refresh:
        threading.Thread(
            target=_refresh_cache,
            name="corpus-facts-refresh",
            daemon=True,
        ).start()
    return live or _empty_live()


def claims_publications():
    """How many publications actually have their claims parsed, or None if unknown.

    Read from `corpus_profile.json`, the same snapshot `/corpus` shows, so there is ONE number and
    a weekly cron maintains it. Cheap: a small JSON read, no scan.

    It exists because the search progress note told every user that the corpus held
    "N publications with full claims and description text". MEASURED 2026-08-25: N is 4,984,254 and
    the number with parsed claims is 814,523, which is 16%. Description text is rarer still. That
    sentence is the wrong scope statement this module exists to prevent, and an attorney reading it
    would believe the run had read five million patents' claims.
    """
    try:
        d = json.loads((DATA / "corpus_profile.json").read_text())
        rows = d.get("claims") or []
        n = rows[0].get("pubs") if rows else None
        return int(n) if n else None
    except Exception:
        return None


def facts(force: bool = False) -> dict:
    """Scope + currency + reliability in one dict for the templates.

    Never raises: a disclosure surface that 500s because the DB hiccuped would remove the warning
    entirely, which is the worst possible failure mode here. On error the counts come back None
    and the templates degrade to "unavailable" instead of to silence.
    """
    live = _live_facts(force)

    mx = live.get("max_date")
    return {
        "publications": live.get("publications"),
        "chunks": live.get("chunks"),
        "claims_publications": claims_publications(),
        "max_date": mx,
        "max_date_str": mx.isoformat() if mx else None,
        "min_date": live.get("min_date"),
        "min_year": live["min_date"].year if live.get("min_date") else None,
        #  Live from the corpus; falls back to the configured target only if the DB read failed,
        #  so a hiccup degrades to the old constant rather than to an empty scope statement.
        "jurisdictions": live.get("jurisdictions") or JURISDICTIONS,
        "jurisdictions_trace": live.get("jurisdictions_trace") or [],
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
