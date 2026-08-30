"""How hard the search works, as a setting rather than a deploy.

WHY THIS EXISTS. Every number that decides how long a search takes was a code default with an
environment override, which means changing one needed an edit and a `supervisorctl restart`, and
nobody could see what the run they were reading had actually been given. That is the same defect
`model_settings` was written for, one layer down: the model routing was invisible, and so is the
retrieval budget.

Precedence is deliberately identical to `model_settings`: the settings FILE, then the ENVIRONMENT
variable, then the code default. An operator who never opens the page gets exactly today's
behaviour, a deploy cannot quietly overwrite a deliberate choice, and `DEEP_RANK_*`-style env
pinning still wins over a code default for anyone driving this from a shell.

EVERY KNOB CARRIES ITS OWN EXPLANATION, and the explanation is what the page shows behind the (?).
A number with no stated effect on time or on quality is a number nobody can set responsibly, and
this file is the only place that pairing is written down. Where a figure was measured, the
measurement and its run are named; where it was reasoned, it says so.

NOTHING HERE IMPORTS ANYTHING FROM THIS PROJECT. It is read by `retrieval.lexical`,
`retrieval.dense`, `agent`, `query_set` and `search_profile`, which between them sit under most of
the package, so a project import here would be a cycle waiting for the first refactor.
"""
from __future__ import annotations

import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("SEARCH_SETTINGS_PATH",
                      os.path.join(ROOT, "data", "search_settings.json"))
TTL = float(os.environ.get("SEARCH_SETTINGS_TTL", "5"))

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}

#  GROUPS, in the order a search meets them. Purely presentational.
GROUPS = [
    ("retrieval", "Finding candidates",
     "What the search asks for and how widely it looks. These change what is REACHABLE; "
     "nothing later in the pipeline can rank a document that retrieval never returned."),
    ("effort", "When to stop looking",
     "Retrieval runs one pass per query in the portfolio. These decide when the passes stop "
     "paying for themselves."),
    ("reading", "How much is read in full",
     "Reading references end to end was 67% of the wall clock of the measured 871 s run "
     "(580 s of it, for 222 documents). It is the only stage where a large saving exists."),
]

#  kind: int | float | bool | choice
KNOBS = [
    # ---------------------------------------------------------------- retrieval
    {
        "key": "qs_limitations", "group": "retrieval", "kind": "bool", "default": True,
        "env": "QS_LIMITATIONS",
        "label": "Search each claim limitation as its own query",
        "effect": (
            "QUALITY, measurably. A claim is a conjunction of separate requirements, and a single "
            "paragraph-long brief averages them into one point that is close to nothing in "
            "particular. Measured on the v2 corpus with quantisation error removed entirely: one "
            "brief-shaped query put 1 of 6 known-relevant references in the top 25, while the "
            "union of the top 30 of three limitation-shaped queries put 3 of 6 there, and put the "
            "two most on-point references at ranks 3 and 4.\n\n"
            "TIME: each limitation is one more retrieval pass, so this adds passes. It is close to "
            "free in practice because the split is structural and needs no model call, and because "
            "'Elements per search' below is reduced automatically when this is on: when a document "
            "has claims, its limitations ARE its elements, and they are verbatim spans of the "
            "legal requirement rather than a model's paraphrase of it.\n\n"
            "Turn it OFF to get the pre-2026-08-27 behaviour exactly."),
    },
    {
        "key": "qs_max_limitations", "group": "retrieval", "kind": "int", "default": 12,
        "min": 0, "max": 40, "env": "QS_MAX_LIMITATIONS", "unit": "queries",
        "label": "Most limitation queries per search",
        "effect": (
            "TIME: one retrieval pass each, so this is the direct cost of the setting above. On "
            "the measured runs a pass costs between 0.1 s and 8 s of a shared 6-worker pool, and "
            "the slow ones are the long ones.\n\n"
            "QUALITY: limitations are taken independent claims first, because a dependent claim's "
            "limitation is mostly its parent's words plus one detail and retrieves a near "
            "duplicate neighbourhood. Below about 6 the distinctive requirements of a long claim "
            "set start being dropped, and those are exactly the ones a novelty attack turns on.\n\n"
            "0 disables limitation queries as surely as the switch above."),
    },
    {
        "key": "qs_elements_with_claims", "group": "retrieval", "kind": "int", "default": 6,
        "min": 0, "max": 14, "env": "QS_ELEMENTS_WITH_CLAIMS", "unit": "queries",
        "label": "Elements per search, when limitation queries are on",
        "effect": (
            "The element list is a model's description of the invention's parts; the limitation "
            "list is a verbatim cut of the claims. When both exist they cover much the same "
            "ground, so this trims the weaker one rather than paying twice.\n\n"
            "TIME: one pass per element, same cost as a limitation query. Raising it back to 14 "
            "restores the old pass count.\n\n"
            "QUALITY: elements still earn their place on a concept search and on a document whose "
            "claims split badly, which is why this is not zero by default. It has no effect at all "
            "on a search with no claims, where the full element list is always used."),
    },
    {
        "key": "third_party_sources", "group": "retrieval", "kind": "bool",
        "label": "Use third-party patent sources", "env": "THIRD_PARTY_SOURCES", "default": True,
        "effect": (
            "OFF means every search runs against our own corpus and nothing else: no PQAI, no "
            "BigQuery Google Patents, no SerpApi, no USPTO keyword search, and no fetching of "
            "full text from outside when a reference we want to read has none here. "
            "MEASURED on one run: the outside sources produced 319 of 3,470 candidate families "
            "and cost 58.5 of the 65 seconds retrieval took, while the local corpus produced the "
            "other 3,151 in about six. So turning this off is roughly a ten-fold speed-up of the "
            "finding stage. What it costs is real: 296 of those 319 families were found by "
            "nothing else, so a local-only search cannot see art outside what we hold. "
            "This is the default for every search; the search form can override it per run."),
    }, {
        "key": "external_timeout_quick", "group": "retrieval", "kind": "int",
        "label": "External API deadline on a normal search",
        "env": "EXTERNAL_TIMEOUT_QUICK", "default": 25, "min": 5, "max": 120, "unit": "seconds",
        "effect": (
            "How long a normal search waits for the seven outside patent APIs before going on "
            "with whatever they returned. MEASURED: the fan-out took 58.5 seconds inside a search "
            "whose whole wall clock was 73, so this is the single knob that decides how long a "
            "normal search takes. The local corpus is unaffected either way and the deep tier "
            "keeps the full 120 seconds, where a minute against a ten-minute read is noise. "
            "Lower it for speed and lose the slowest sources; raise it for reach outside the "
            "indexed classifications."),
    }, {
        "key": "hnsw_iterative_scan", "group": "retrieval", "kind": "choice",
        "default": "relaxed_order", "choices": ["off", "relaxed_order", "strict_order"],
        "env": "HNSW_ITERATIVE_SCAN",
        "label": "Iterative index scan",
        "effect": (
            "QUALITY, free. `hnsw.ef_search` caps at 1000 in pgvector, so widening the candidate "
            "pool past that buys nothing without this. With it on, the index is rescanned until "
            "the requested number of rows survives the filters instead of returning short.\n\n"
            "MEASURED on this corpus, chunk recall@100: `vacuum-cup` 0.850 -> 0.950, `bevel-angle` "
            "0.860 -> 0.910, `magnetic-poleshoe` unchanged at 1.000. Latency at pool 4000 with "
            "iterative scan was 82-87 ms, which is what pool 1000 costs today. The two queries it "
            "helps are the two carrying the novelty in the case it was measured on.\n\n"
            "`relaxed_order` allows a slightly out-of-order tail and is what the ops scripts "
            "already use; `strict_order` is stricter and slower. `off` restores the old ceiling.\n\n"
            "A retrieval probe with iterative scan left off once reported 780 chunks where there "
            "were 9,000, so this is also a correctness setting, not only a recall one."),
    },
    {
        "key": "ef_search_seed", "group": "retrieval", "kind": "int", "default": 1000,
        "min": 40, "max": 1000, "env": "SEED_EF_SEARCH", "unit": "candidates",
        "label": "Index search width on whole-invention passes (ef_search)",
        "effect": (
            "How much of the HNSW graph each ANN probe explores on a SEED pass, the passes that "
            "describe the whole invention and form the ranking backbone. pgvector refuses "
            "anything above 1000, so the default is the ceiling.\n\n"
            "MEASURED with iterative scan on, chunk recall@100: `vacuum-cup` 0.850 -> 0.950 and "
            "`bevel-angle` 0.860 -> 0.910 when the width went up, at 82-87 ms per probe, which is "
            "what the narrower setting already cost. This was 400 until 2026-08-27, i.e. it was "
            "leaving measured recall on the table for no saving.\n\n"
            "TIME: an ANN probe is under a tenth of a second against 20-176 s for the keyword "
            "channel beside it. Lowering this saves nothing you can feel and costs recall on "
            "exactly the dense-neighbourhood queries that a claim limitation produces."),
    },
    {
        "key": "ef_search_element", "group": "retrieval", "kind": "int", "default": 400,
        "min": 40, "max": 1000, "env": "HNSW_EF_SEARCH", "unit": "candidates",
        "label": "Index search width on element and limitation passes",
        "effect": (
            "The same width for the narrow passes: one element or one claim limitation at a time. "
            "There are many more of these than there are seed passes, so it is set lower.\n\n"
            "QUALITY: this is the width a limitation query runs at, and limitation queries are the "
            "ones measured to reach references a brief-shaped query ranks nowhere, so it is worth "
            "more than it looks. Raised from 200 to 400 on 2026-08-27 alongside the seed width.\n\n"
            "TIME: linear in the number of narrow passes, which is roughly 'elements + "
            "limitations'. Still small beside the keyword channel."),
    },
    {
        "key": "chunk_fetch_seed", "group": "retrieval", "kind": "int", "default": 9000,
        "min": 500, "max": 60000, "env": "SEED_CHUNK_FETCH", "unit": "rows",
        "label": "Candidate pool per whole-invention probe",
        "effect": (
            "How many chunk rows a seed pass pulls from the index before they are rolled up into "
            "publications and families. Only meaningful with iterative scan on; without it the "
            "pool is silently capped at ef_search and a probe returns short.\n\n"
            "MEASURED, publication recall@25 by pool: 250 -> 0.815, 500 -> 0.915, 1000 -> 0.965. "
            "Going from 4,000 to 60,000 chunks cost 123 s and did not move the target reference "
            "at all, which is why this sits at 9,000 and not higher.\n\n"
            "QUALITY: everything reachable in this corpus sat inside the top 1.6% of a dense "
            "ranking but most of it outside the top 100, so a wide pool followed by the fusion and "
            "the cross-encoder is what reaches it. A narrow pool cannot be recovered downstream.\n\n"
            "Widening a funnel only helps if the stage below it widens too: raising the seed "
            "publication cap to 6,000 was tried and REVERTED because top-50 recall FELL, a wider "
            "pool being more competition at every fixed-size stage after it."),
    },
    {
        "key": "lexical_timeout_ms", "group": "retrieval", "kind": "int", "default": 3000,
        "min": 0, "max": 180000, "env": "LEXICAL_TIMEOUT_MS", "unit": "ms",
        "label": "Keyword (BM25) channel deadline",
        "effect": (
            "TIME, and this is the big one in retrieval. The keyword channel is Postgres "
            "`to_tsvector('english')` over the whole corpus. It was measured at 20 s to 176 s for "
            "ONE pass when the corpus held 1.4M chunks; it now holds 11.2M. On a deep run making "
            "up to 39 passes it was 54% of every one of them, worth about 22 minutes of a "
            "112-minute search, which is more than the entire vector side of retrieval.\n\n"
            "QUALITY: it is the lowest-weighted channel in the fusion, on purpose. It ranks by raw "
            "lexeme count, and for the 39.9% of a worldwide corpus that is CJK the `english` "
            "configuration has no segmenter, so it is not degraded there but dead. What it "
            "genuinely contributes is rare vocabulary the vector side smooths away, and a document "
            "it would have found late is usually reachable through the citation or family channel "
            "anyway.\n\n"
            "A pass that hits the deadline returns the rows it already has and says so in the run "
            "record; it is never silently treated as 'this channel found nothing'. 0 removes the "
            "deadline and restores the old unbounded behaviour."),
    },
    # ---------------------------------------------------------------- effort
    {
        "key": "pass_yield_min_pct", "group": "effort", "kind": "float", "default": 2.0,
        "min": 0.0, "max": 50.0, "env": "PASS_YIELD_MIN_PCT", "unit": "%",
        "label": "Stop when a window of passes adds less than",
        "effect": (
            "TIME. MEASURED on run FT-D (871 s, 5,005 families): passes 25 to 30 took 23.5 s, "
            "23.6 s, 23.5 s, 65.0 s, 64.5 s and 64.4 s, and moved the family count from 4,912 to "
            "5,005. That is 60 to 90 s of wall clock, on a six-worker pool, for 93 new families "
            "out of 5,005, which is 1.9%. The tail of the pass loop is where the slow queries "
            "live, because the slow queries are the long ones.\n\n"
            "QUALITY: the families a plateaued pass adds are, by construction, ones several "
            "earlier passes already nearly reached, so they enter the fusion with weak ranks and "
            "rarely survive the screen. The risk is real but small: a late pass on an unusual "
            "vocabulary can be the only one that reaches a document.\n\n"
            "0 disables the gate and runs every pass in the portfolio, which is the old "
            "behaviour. Raise it above about 5 and a normal search stops while it is still "
            "finding things."),
    },
    {
        "key": "pass_yield_window", "group": "effort", "kind": "int", "default": 3,
        "min": 1, "max": 12, "env": "PASS_YIELD_WINDOW", "unit": "passes",
        "label": "Passes the plateau must hold for",
        "effect": (
            "How many consecutive passes have to come in under the threshold before the loop "
            "stops. One pass is noise: query 14 of a portfolio can legitimately return nothing "
            "because it is a near-duplicate of query 13, while query 15 opens a new field.\n\n"
            "TIME: a wider window is more passes before stopping, so it is strictly slower and "
            "strictly safer. 1 is aggressive, 3 is the default, above about 6 the gate rarely "
            "fires at all.\n\n"
            "The gate never stops before the whole seed set has run, whatever this is set to, so "
            "the queries that describe the invention as a whole are always issued."),
    },
    # ---------------------------------------------------------------- reading
    {
        "key": "submission_chart_top", "group": "reading", "kind": "int", "default": 45,
        "min": 10, "max": 200, "env": "SUBMISSION_CHART_TOP", "unit": "references",
        "label": "Approximate references read in full at Submission depth",
        "effect": (
            "THE REASON THE SUBMISSION DEPTH EXISTS. A full-depth run reads 211 to 224 references "
            "end to end and a 37 CFR 1.290 submission files TEN of them. Reading was 580 s of the "
            "measured 871 s run, and it is close to linear in the number of documents, so 45 "
            "references is about 120 s of reading instead of 580 s.\n\n"
            "THIS IS A DIAL, NOT A COUNT. The read set is the chart head plus the always-read "
            "retrieval head plus the per-claim reach round-robin plus the blind rescue plus the "
            "evidence sweep, and all five are derived from this number. The measured full-depth "
            "run read 222 with its chart head at 120, so the other four contributed about 100 "
            "between them; setting only the chart head would have left 145 documents being read "
            "while the page said 45.\n\n"
            "MEASURED END TO END, first submission-depth run (adhoc-804db2011f04, 2026-08-27), "
            "same subject and corpus as the full-depth baseline (adhoc-d8c2d44ef969):\n"
            "    full depth   222 read, 7,699,023 chars, 4,295 model calls, 64.0M prompt tokens\n"
            "    this, at 45   78 read, 2,279,081 chars,   757 model calls, 21.7M prompt tokens\n"
            "So 45 does not mean 45 documents: it meant 41 charted and 78 read in full once the "
            "evidence sweep is counted. What it buys is a 70% cut in text read and an 82% cut in "
            "model calls. Screening was untouched at 2,500 candidates in 50 s.\n\n"
            "QUALITY, measured on run FT-D against the nine references the offices themselves "
            "cited against this family: they landed at deep-block positions 1, 2, 5, 14, 41 and "
            "179. Reading the top 45 keeps five of those six. The one lost sits at 179, and no "
            "setting short of reading everything reaches it.\n\n"
            "Raise this toward 120 to converge on what full depth produces; lower it toward 20 "
            "and the ledger starts reporting requirements as uncovered that a deeper read would "
            "have covered, which is a false negative rather than a smaller report."),
    },
    {
        "key": "prescreen_rerank_top", "group": "reading", "kind": "int", "default": 0,
        "min": 0, "max": 1000, "env": "PRESCREEN_RERANK_TOP", "unit": "candidates",
        "label": "Candidates the cross-encoder reorders before the read set is chosen",
        "effect": (
            "OFF BY DEFAULT, AND THE ARITHMETIC IS WHY. The idea is sound: screening is cheap and "
            "reading is not, so a smaller read set is made safe by spending more on the choice. "
            "The bge cross-encoder currently runs over the top 50 only, purely to order documents "
            "that were going to be read anyway.\n\n"
            "But it runs on CPU on this box, measured at about 40 s per 25 passages, i.e. roughly "
            "1.6 s each. Reordering 300 candidates is therefore about 480 s, against the 460 s "
            "that cutting the read set from 222 to 45 saves. It does not pay: it spends the whole "
            "saving on the choosing and hands back a search of the same length.\n\n"
            "Set it above 0 only on a box with a GPU, or where the reranker has been moved off "
            "this machine. At that point 200-300 is the band worth trying, because everything "
            "reachable in this corpus sits inside the top 1.6% of a dense ranking and most of it "
            "outside the top 100.\n\n"
            "QUALITY, whatever this is set to: the screen scored a genuinely good reference 0 "
            "three times out of three off a wrong abstract, so the always-read head taken straight "
            "from retrieval stays in place and is never subject to this."),
    },
]
KNOB_BY_KEY = {k["key"]: k for k in KNOBS}


def _read() -> dict:
    try:
        with open(PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load(force=False) -> dict:
    """The settings file, cached briefly. Never raises: a broken file means default behaviour."""
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < TTL:
            return _cache["data"]
        d = _read()
        _cache["at"], _cache["data"] = now, d
        return d


def _coerce(knob, raw):
    """A stored or environment value as the knob's own type, clamped. None if unusable.

    Clamping rather than rejecting: an operator who types 50000 into a field capped at 20000 meant
    'as much as you will give me', and refusing the whole save over one field loses the other nine.
    """
    kind = knob["kind"]
    try:
        if kind == "bool":
            s = str(raw).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
            return None
        if kind == "choice":
            s = str(raw).strip()
            return s if s in knob["choices"] else None
        if kind == "int":
            v = int(float(str(raw).strip()))
        else:
            v = float(str(raw).strip())
    except Exception:
        return None
    lo, hi = knob.get("min"), knob.get("max")
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def get(key):
    """The resolved value: settings file, then environment, then the code default."""
    knob = KNOB_BY_KEY.get(key)
    if knob is None:
        raise KeyError(key)
    stored = (load().get("values") or {})
    if key in stored:
        v = _coerce(knob, stored[key])
        if v is not None:
            return v
    env = knob.get("env")
    if env and os.environ.get(env, "") != "":
        v = _coerce(knob, os.environ[env])
        if v is not None:
            return v
    return knob["default"]


def source_of(key) -> str:
    """Where the live value came from: `setting`, `environment` or `default`. For the page."""
    knob = KNOB_BY_KEY.get(key)
    if knob is None:
        return "default"
    stored = (load().get("values") or {})
    if key in stored and _coerce(knob, stored[key]) is not None:
        return "setting"
    env = knob.get("env")
    if env and os.environ.get(env, "") != "" and _coerce(knob, os.environ[env]) is not None:
        return "environment"
    return "default"


def save(values: dict) -> dict:
    """Write the settings and drop the cache. Unknown keys and unusable values are discarded.

    A value equal to the code default is REMOVED rather than stored, so the file records only
    deliberate departures and a later change to a default is not silently pinned by a page visit.
    """
    out = {}
    for key, raw in (values or {}).items():
        knob = KNOB_BY_KEY.get(key)
        if knob is None:
            continue
        v = _coerce(knob, raw)
        if v is None or v == knob["default"]:
            continue
        out[key] = v
    data = {"values": out, "saved_at": time.time()}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, PATH)                    # atomic: a half-written file must not be read
    with _lock:
        _cache["at"], _cache["data"] = 0.0, None
    return data


def snapshot() -> dict:
    """Every knob that is NOT at its code default, for the run record.

    A report is only reproducible if it says what it was given. This is deliberately the short
    list: recording all eleven on every run buries the one that was changed.
    """
    out = {}
    for k in KNOBS:
        v = get(k["key"])
        if v != k["default"]:
            out[k["key"]] = {"value": v, "from": source_of(k["key"]), "default": k["default"]}
    return out


def page_rows() -> list:
    """What the settings page renders: one row per knob, with its live value and provenance."""
    rows = []
    for k in KNOBS:
        row = dict(k)
        row["value"] = get(k["key"])
        row["source"] = source_of(k["key"])
        rows.append(row)
    return rows
