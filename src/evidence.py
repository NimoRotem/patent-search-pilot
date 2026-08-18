"""The permanent evidence store: what has already been proven, so no run re-buys it.

Two tables, two different reuse shapes:

`evidence_charts` — one row per (reference, checklist-fingerprint): the FULL per-reference chart
`deep_analysis.analyse_reference` produced. A re-run of the same subject (every benchmark arm,
every re-run after a deploy, every gold-set scoring pass) asks the same checklist of the same
documents; before this table each such run re-read ~550-700 documents at ~$50-80 a pass. The
fingerprint covers the exact features + claim texts asked, so ANY change to the checklist misses
the cache and reads fresh — reuse can never serve an answer to a question that was not asked.

`evidence_cells` — one row per (reference, limitation-fingerprint, bar, model): the granular
claim-evidence graph. This is the asset that accumulates across subjects and powers per-limitation
lookups (Which documents already disclose a suction plate with a peripheral sealing lip?) without
any reading at all. Bars: "discloses" (verbatim quote, survived grounding) vs "teaches" (the
reference conveys the idea; no verbatim support) — the two standards an examiner actually uses.

Reuse is gated three ways: the env flag (DEEP_EVIDENCE_REUSE=0 turns it off, e.g. for a clean A/B),
age (EVIDENCE_TTL_DAYS), and the read pool the chart was made with (a chart read by a different
model tier is evidence about that tier, not this one). Storage is best-effort and never fails a
run: the store being down costs money, not correctness.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import traceback

import db

REUSE = os.environ.get("DEEP_EVIDENCE_REUSE", "1") != "0"
TTL_DAYS = int(os.environ.get("EVIDENCE_TTL_DAYS", "90"))

_ready = threading.Event()
_ready_lock = threading.Lock()


def ensure_schema():
    with db.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS evidence_charts (
                publication_number text NOT NULL,
                subject_fp  text NOT NULL,
                read_pool   text NOT NULL DEFAULT '',
                chart       jsonb NOT NULL,
                run_slug    text DEFAULT '',
                created_at  timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (publication_number, subject_fp)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS evidence_cells (
                id bigserial PRIMARY KEY,
                publication_number text NOT NULL,
                limitation_fp   text NOT NULL,
                limitation_text text NOT NULL DEFAULT '',
                bar        text NOT NULL DEFAULT '',
                verdict    text NOT NULL,
                quote      text DEFAULT '',
                location   text DEFAULT '',
                confidence real DEFAULT 0,
                grounding  text DEFAULT '',
                model      text DEFAULT '',
                run_slug   text DEFAULT '',
                subject    text DEFAULT '',
                created_at timestamptz NOT NULL DEFAULT now()
            )""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_cells ON evidence_cells "
                    "(publication_number, limitation_fp, bar, model)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_evidence_cells_lim ON evidence_cells "
                    "(limitation_fp)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_evidence_cells_pub ON evidence_cells "
                    "(publication_number)")
    _ready.set()


def _ensure():
    if _ready.is_set():
        return True
    with _ready_lock:
        if _ready.is_set():
            return True
        try:
            ensure_schema()
            return True
        except Exception:
            traceback.print_exc()
            return False


def _norm(text):
    return " ".join(str(text or "").lower().split())


def fingerprint(text) -> str:
    """Stable id for one limitation's text, across runs and subjects."""
    return hashlib.sha1(_norm(text).encode()).hexdigest()


def subject_fp(features, input_claims) -> str:
    """Fingerprint of the EXACT checklist a reference is asked about.

    Order-sensitive on purpose: the reader is batched in list order and batch composition shapes
    the answers (measured: batch size is the speed/evidence dial), so a reordered checklist is a
    different question.
    """
    h = hashlib.sha1()
    for f in features or []:
        h.update(b"F")
        h.update(_norm(f).encode())
    for c in input_claims or []:
        h.update(b"C")
        h.update(_norm((c or {}).get("label")).encode())
        h.update(b"=")
        h.update(_norm((c or {}).get("text")).encode())
    return h.hexdigest()


def read_pool_id() -> str:
    try:
        import model_pool
        return ",".join(model_pool.READ)
    except Exception:
        return ""


# ---------------------------------------------------------------------------- charts


def load_chart(pub, fp):
    """A previous run's full chart of `pub` against this exact checklist, or None."""
    if not (REUSE and _ensure()):
        return None
    try:
        with db.cursor() as cur:
            cur.execute("SELECT chart FROM evidence_charts WHERE publication_number=%s "
                        "AND subject_fp=%s AND read_pool=%s "
                        "AND created_at > now() - make_interval(days => %s)",
                        (pub, fp, read_pool_id(), TTL_DAYS))
            r = cur.fetchone()
    except Exception:
        traceback.print_exc()
        return None
    if not r:
        return None
    chart = r["chart"] if isinstance(r, dict) else r[0]
    if isinstance(chart, str):
        try:
            chart = json.loads(chart)
        except Exception:
            return None
    return chart


def save_chart(pub, fp, chart, run_slug=""):
    if not _ensure():
        return
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO evidence_charts (publication_number, subject_fp, read_pool, "
                        "chart, run_slug) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (publication_number, subject_fp) DO UPDATE SET "
                        "chart=EXCLUDED.chart, read_pool=EXCLUDED.read_pool, "
                        "run_slug=EXCLUDED.run_slug, created_at=now()",
                        (pub, fp, read_pool_id(), json.dumps(chart, default=str), run_slug))
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------------------- cells


def save_cells_from_chart(chart, texts, run_slug="", subject=""):
    """Record every judged claim-axis cell of one reference chart. Best-effort.

    `texts` maps limitation label -> limitation text (the fingerprint source). Feature-axis rows
    are subject-specific phrasing and are not written to the graph.
    """
    if not _ensure():
        return 0
    pub = (chart or {}).get("pub") or ""
    rows = [r for r in (chart or {}).get("claims") or [] if isinstance(r, dict)]
    if not pub or not rows:
        return 0
    n = 0
    try:
        with db.cursor() as cur:
            for r in rows:
                text = texts.get(r.get("item")) or ""
                if not text:
                    continue
                bar = r.get("bar") or ("discloses" if r.get("grounding") == "verified" else "")
                cur.execute(
                    "INSERT INTO evidence_cells (publication_number, limitation_fp, "
                    "limitation_text, bar, verdict, quote, location, confidence, grounding, "
                    "model, run_slug, subject) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (publication_number, limitation_fp, bar, model) DO UPDATE SET "
                    "verdict=EXCLUDED.verdict, quote=EXCLUDED.quote, location=EXCLUDED.location, "
                    "confidence=EXCLUDED.confidence, grounding=EXCLUDED.grounding, "
                    "run_slug=EXCLUDED.run_slug, subject=EXCLUDED.subject, created_at=now()",
                    (pub, fingerprint(text), text[:2000], bar,
                     r.get("verdict") or "absent", (r.get("quote") or "")[:2000],
                     (r.get("location") or "")[:200], float(r.get("confidence") or 0.0),
                     (r.get("grounding") or "")[:60], read_pool_id(), run_slug, subject))
                n += 1
    except Exception:
        traceback.print_exc()
    return n


def known_disclosers(limitation_text, limit=50):
    """Documents the graph already holds non-absent evidence for, best bar first.

    This is the flywheel read path: a rescue for an orphaned limitation consults the graph before
    it searches, because a document proven in an earlier run is a better lead than a fresh ANN hit.
    """
    if not _ensure():
        return []
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT publication_number, bar, verdict, quote, location, confidence "
                "FROM evidence_cells WHERE limitation_fp=%s AND verdict IN "
                "('disclosed','partial') ORDER BY (bar='discloses') DESC, "
                "(verdict='disclosed') DESC, confidence DESC LIMIT %s",
                (fingerprint(limitation_text), limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        traceback.print_exc()
        return []


def stats():
    if not _ensure():
        return {}
    try:
        with db.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM evidence_charts) AS charts, "
                        "(SELECT count(*) FROM evidence_cells) AS cells")
            r = cur.fetchone()
            return dict(r) if r else {}
    except Exception:
        return {}
