"""An immutable record of exactly what produced a run, written before it starts.

WHY
---
No benchmark comparison is valid unless control and treatment shared the same benchmark, corpus
snapshot, eligibility rules and budgets. Nothing recorded that, so every comparison in this
project so far rested on the assumption that nothing else moved between two runs taken hours
apart, during which the corpus was being written to, prompts were being edited and constants were
being tuned. At least one measured "regression" turned out to be two runs scored against
different disclosure lists.

The manifest is written at START with completion_status="running" and finalised at the end, so a
run that dies is distinguishable from one that never began, and a partial run can never be
reported as complete.

Everything here is cheap to collect. The expensive fields (corpus and index snapshots) are counts
and max-ids rather than checksums, which is enough to detect that the corpus moved between two
runs without scanning 273 GB.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_DIR = os.environ.get("RUN_MANIFEST_DIR", os.path.join(ROOT, "data", "manifests"))
SCHEMA_VERSION = 1


def _git(*args):
    try:
        return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def git_state():
    """Commit and whether the tree was dirty. A dirty tree means the commit does not describe the
    code that ran, which is worth knowing before trusting a comparison."""
    return {"git_commit": _git("rev-parse", "HEAD")[:12],
            "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "git_dirty": bool(_git("status", "--porcelain"))}


#  SNAPSHOTS MUST BE CHEAP. They run on the request path of every search, so an exact count is
#  not affordable: `count(*) FROM chunks WHERE embedding IS NOT NULL` is a scan of 26.6 million
#  rows and takes minutes. The first version of this module did exactly that and added it to every
#  run; two concurrency tests caught it by timing out. Row ESTIMATES from the planner statistics
#  are instant and are enough for what a manifest needs, which is to detect that the corpus moved
#  between two runs, not to audit it. Cached per process on top of that.
_SNAP_TTL = float(os.environ.get("MANIFEST_SNAPSHOT_TTL", "900"))
_snap_cache = {"at": 0.0, "corpus": None, "index": None}


def _estimate(cur, table):
    cur.execute("SELECT reltuples::bigint n FROM pg_class WHERE relname = %s", (table,))
    r = cur.fetchone()
    return int(r["n"]) if r and r["n"] is not None else None


def _snapshots():
    now = time.time()
    if _snap_cache["corpus"] is not None and now - _snap_cache["at"] < _SNAP_TTL:
        return _snap_cache["corpus"], _snap_cache["index"]
    corpus = {"error": "unavailable"}
    index = {"error": "unavailable"}
    try:
        import db
        with db.cursor() as cur:
            #  max(id) is an index scan, instant. reltuples is a planner estimate, free.
            cur.execute("SELECT (SELECT max(id) FROM publications) max_pub, "
                        "       (SELECT max(id) FROM chunks) max_chunk")
            r = cur.fetchone()
            corpus = {"publications_estimate": _estimate(cur, "publications"),
                      "max_publication_id": r["max_pub"],
                      "max_chunk_id": r["max_chunk"],
                      "counts_are": "planner estimates, not exact"}
            cur.execute("""SELECT indexname FROM pg_indexes
                           WHERE tablename='chunks' AND indexdef ILIKE '%hnsw%'""")
            index = {"chunks_estimate": _estimate(cur, "chunks"),
                     "hnsw_indexes": [x["indexname"] for x in cur.fetchall()]}
    except Exception:
        traceback.print_exc()
    _snap_cache.update(at=now, corpus=corpus, index=index)
    return corpus, index


def corpus_snapshot():
    """High-water marks and row estimates. Enough to detect the corpus moving between two runs."""
    return _snapshots()[0]


def index_snapshot():
    return _snapshots()[1]


def module_constants():
    """The tunables that decide a run, read from the modules rather than restated here, so a
    constant that moves cannot silently disagree with what the manifest claims."""
    out = {}
    try:
        import retrieval as R
        out["retrieval"] = {k: getattr(R, k) for k in (
            "RRF_K", "CHUNK_FETCH", "PUB_CAP", "SEED_CHUNK_FETCH", "SEED_PUB_CAP",
            "EF_SEARCH", "SEED_EF_SEARCH", "MAX_SCAN_TUPLES", "SEED_MAX_SCAN_TUPLES",
            "RERANK_TOP", "DENSE_FLOOR") if hasattr(R, k)}
        out["fusion_weights"] = dict(getattr(R, "CHANNEL_WEIGHTS", {}))
    except Exception:
        out["retrieval"] = {"error": "unavailable"}
    try:
        import deep_rank as D
        out["screening_budget"] = {k: getattr(D, k) for k in (
            "SCREEN_TOP", "SCREEN_CHARS", "SCREEN_BATCH", "SCREEN_WORKERS") if hasattr(D, k)}
        out["reading_budget"] = {k: getattr(D, k) for k in (
            "CHART_TOP", "CHART_MIN_SCREEN", "CHART_TOP_MAX", "CHART_WORKERS",
            "ALWAYS_CHART_RETRIEVAL_HEAD", "BLIND_RESCUE", "DISCLOSURE_CAP") if hasattr(D, k)}
        out["scoring"] = {k: getattr(D, k) for k in (
            "COVERAGE_WEIGHT", "OVERALL_SHARE", "DEPTH_FULL_CHARS", "DEPTH_CONFIDENCE_FLOOR",
            "UNREAD_SCORE_CAP", "COVERAGE_ORDER") if hasattr(D, k)}
    except Exception:
        out["screening_budget"] = {"error": "unavailable"}
    try:
        import coverage_rank as C
        out["portfolio"] = {"CORROBORATION": C.CORROBORATION,
                            "SCORE_WEIGHT": C.SCORE_WEIGHT,
                            "RERANK_DEPTH": C.RERANK_DEPTH}
    except Exception:
        pass
    try:
        import external as E
        out["external"] = {k: getattr(E, k) for k in (
            "ENABLED", "MAX_ASPECTS", "MAX_QUERIES", "SERPAPI_ASPECTS", "CPC_PER_ASPECT",
            "MERGE_FAMILIES", "CHANNEL_DEPTH", "RESCORE_TOP", "SEMANTIC_SHARE") if hasattr(E, k)}
        out["external"]["source_weights"] = dict(getattr(E, "SOURCE_WEIGHT", {}))
    except Exception:
        pass
    return out


def model_versions():
    out = {}
    try:
        import llm
        out["llm_model"] = getattr(llm, "MODEL", "")
    except Exception:
        pass
    try:
        import embed
        out["embedding_model"] = getattr(embed, "VERTEX_EMBED_MODEL", "")
        out["embedding_dim"] = getattr(embed, "EMBED_DIM", None)
        out["embedding_provider"] = getattr(embed, "EMBED_PROVIDER", "")
    except Exception:
        pass
    try:
        import rerank as rr
        out["reranker"] = getattr(rr, "MODEL_NAME", "") or getattr(rr, "MODEL", "")
    except Exception:
        pass
    return out


def prompt_versions():
    """Hash of every prompt that decides an outcome. A prompt edit is a change of experiment, and
    without this it is invisible in a diff of results."""
    out = {}
    for mod, attrs in (("deep_analysis", ("_SYS",)), ("deep_rank", ("_SCREEN_SYS", "_SYS")),
                       ("disclosures", ("_SYS",)), ("external", ("_PLAN_SYS",)),
                       ("tournament", ("_SYS",)), ("query_set", ("_ESSENCE_SYS",)),
                       ("llm", ("_CONDENSE_SYS",))):
        try:
            m = __import__(mod)
            for a in attrs:
                v = getattr(m, a, None)
                if isinstance(v, str) and v:
                    out[f"{mod}.{a}"] = hashlib.sha256(v.encode()).hexdigest()[:12]
        except Exception:
            continue
    return out


def feature_flags():
    """Every switchable behaviour, read from the environment with its effective default."""
    names = [
        "EXTERNAL_ENABLED", "DEEP_RANK_COVERAGE_ORDER", "FEDERATION_ENABLED",
        "BENCHMARK_FAIL_CLOSED", "ORACLE_INJECTION_ENABLED",
        "DISCLOSURE_SEARCH_ENABLED", "PER_DISCLOSURE_CANDIDATE_QUOTA_ENABLED",
        "DISCLOSURE_CONDITIONED_SCREENING_ENABLED", "CONSTRAINED_PORTFOLIO_ENABLED",
        "LEARNED_CPC_TRANSITION_ENABLED", "CITATION_GRAPH_EXPANSION_ENABLED",
        "LEARNED_FUSION_ENABLED", "SOURCE_SPECIFIC_QUERIES_ENABLED",
        "REFERENCE_TERMINOLOGY_QUERIES_ENABLED", "PER_DISCLOSURE_SCREEN_QUOTA_ENABLED",
        "DYNAMIC_READING_ENABLED",
    ]
    return {n: os.environ.get(n, "") for n in names}


def start(run_id, **fields):
    """Write the manifest before the run does anything. -> the manifest dict."""
    m = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "completion_status": "running",
        "failure_reason": "",
        "host": platform.node(),
        **git_state(),
        "src_tree_hash": src_tree_hash(),
        "corpus_snapshot": corpus_snapshot(),
        "index_snapshot": index_snapshot(),
        "model_versions": model_versions(),
        "prompt_versions": prompt_versions(),
        "parameters": module_constants(),
        "feature_flags": feature_flags(),
        "random_seeds": {"note": "no seeded randomness in the pipeline; LLM sampling is not "
                                 "controlled, which is why stochastic stages need repeated runs"},
    }
    m.update(fields)
    _write(m)
    return m


def finish(m, status="completed", failure_reason="", **fields):
    if not m:
        return
    m["completion_status"] = status
    m["failure_reason"] = str(failure_reason or "")[:2000]
    m["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    m.update(fields)
    _write(m)
    return m


def _write(m):
    try:
        os.makedirs(MANIFEST_DIR, exist_ok=True)
        safe = "".join(ch for ch in str(m.get("run_id") or "run") if ch.isalnum() or ch in "-_.")
        tmp = os.path.join(MANIFEST_DIR, f".{safe}.tmp")
        with open(tmp, "w") as fh:
            json.dump(m, fh, indent=1, default=str)
        os.replace(tmp, os.path.join(MANIFEST_DIR, f"{safe}.json"))
    except Exception:
        traceback.print_exc()


def load(run_id):
    p = os.path.join(MANIFEST_DIR, f"{run_id}.json")
    return json.load(open(p)) if os.path.exists(p) else None


def src_tree_hash():
    """A hash of every source file the pipeline actually imports.

    git_commit and git_dirty are not enough when the checkout is SHARED. Measured on 2026-08-06: a
    second agent was working in this same working tree on its own feature branch, switched the
    branch underneath a running experiment, and its edit silently reverted a line in webapp.py
    that the comparison depended on. git_dirty was true for reasons that had nothing to do with
    the experiment, so it could not be used to tell whether the CODE had moved between two arms.
    This can: it is the content of the files, not the state of the index.
    """
    import hashlib
    h = hashlib.sha256()
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    try:
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), "rb") as fh:
                h.update(name.encode())
                h.update(hashlib.sha256(fh.read()).digest())
    except Exception:
        return ""
    return h.hexdigest()


def comparable(a, b):
    """Fields that MUST match for a control/treatment comparison to mean anything.

    -> [] when the two runs are comparable, else the list of what differed. This is the check that
    was never run: two runs taken hours apart, with the corpus being written to and constants
    being edited between them, were compared as though only the treatment had changed.
    """
    if not a or not b:
        return ["one or both manifests are missing"]
    diffs = []
    for key in ("git_commit", "benchmark_version", "disclosure_list_version",
                "subject_id", "mode"):
        if a.get(key) != b.get(key):
            diffs.append(f"{key}: {a.get(key)!r} vs {b.get(key)!r}")
    for key in ("corpus_snapshot", "index_snapshot", "model_versions", "prompt_versions"):
        if a.get(key) != b.get(key):
            diffs.append(f"{key} differs")
    #  REPLAY STATE IS A COMPARABILITY VARIABLE. Two arms that saw different external results are
    #  not comparable however identical the code: measured, one subject delivered 3 of 11 gold
    #  references in one run and 0 of 11 in another hours earlier with no change between them, and
    #  the fan-out is the largest exogenous source of that. An arm recorded live and an arm served
    #  from cache are also not comparable, so the MODE has to match, not merely the versions.
    ra, rb = (a.get("replay") or {}), (b.get("replay") or {})
    for key in ("mode", "adapter_version", "normalization_version"):
        if ra.get(key) != rb.get(key):
            diffs.append(f"replay.{key}: {ra.get(key)!r} vs {rb.get(key)!r}")
    if ra.get("mode") in (None, "off") or rb.get("mode") in (None, "off"):
        diffs.append("replay was off for at least one arm, so the external world was not held "
                     "constant between them")
    #  The checkout is shared with another agent, so git_dirty is noisy for reasons unrelated to
    #  the experiment. The source CONTENT is what actually has to match.
    if a.get("src_tree_hash") and b.get("src_tree_hash"):
        if a["src_tree_hash"] != b["src_tree_hash"]:
            diffs.append(f"src_tree_hash differs: the pipeline source changed between the two "
                         f"arms ({a['src_tree_hash'][:12]} vs {b['src_tree_hash'][:12]})")
    elif a.get("git_dirty") or b.get("git_dirty"):
        diffs.append("one or both runs had uncommitted changes and no src_tree_hash to check")
    return diffs
