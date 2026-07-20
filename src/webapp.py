"""Results page — Flask app (spec Milestone 2). Serves on 127.0.0.1:8631.

Reuses Retriever + CoverageAgent + the DB + enrich_display. Element×Reference claim chart,
ranked prior-art cards with drawings/PDF/highlighted sections, coverage ledger.

Report generation (CoverageAgent.run) is slow, so it runs in a background thread with a poll
endpoint; the agent report is cached to data/reports/<slug>.json and never blocks the request.
Per-card drawings/PDF/sections/rationale are enriched lazily via /api/ref.
"""
from __future__ import annotations
import json, os, re, queue, secrets, threading, hashlib, time, traceback
from pathlib import Path
from flask import (Flask, Response, render_template, request, jsonify, redirect, url_for,
                   send_from_directory, abort, stream_with_context)
import db, embed, goldset, webview, enrich_display, llm
import export_data, export_pdf, export_docx
import auth, rerank_pool
import claim_chart, translate, drawings          # ported per-card enrichment
import grounding                                  # length-stable quote grounding (shared w/ claim_chart)
import corpus_facts                               # live corpus scope/currency for the disclosures
import disclosure                                 # shared disclosure wording (web + print + PDF + DOCX)
import federation, domain_detect                 # two-tier search + out-of-domain guard
from search_modes import require_available, ModeNotAvailable, available_modes
from retrieval import Retriever
from agent import CoverageAgent, AgentConfig
from config import DATA, ROOT

app = Flask(__name__, template_folder="../templates", static_folder="../static")

# Signed-session key. Persisted next to .env (gitignored) so sessions survive restarts; generated
# on first boot if absent. NEVER hard-coded and never committed.
def _secret_key():
    k = os.environ.get("SECRET_KEY", "").strip()
    if k:
        return k
    p = ROOT / ".secret_key"
    if p.exists():
        return p.read_text().strip()
    k = secrets.token_urlsafe(48)
    p.write_text(k)
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return k


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
# The session cookie carries the whole auth decision, so it must never travel in cleartext. Public
# access is HTTPS-only (nginx terminates TLS on rotem.ai and proxies here over the VPC), so `Secure`
# costs nothing there. Note Flask sets this flag from app.config alone — it does NOT sniff the
# request scheme — so the http:// hop between nginx and gunicorn does not suppress the flag and
# login keeps working through the proxy. Direct plain-HTTP access to 127.0.0.1:8631 is the one case
# a Secure cookie would not stick, so leave an escape hatch for local/dev use.
app.config["SESSION_COOKIE_SECURE"] = (os.environ.get("SESSION_COOKIE_SECURE", "1").strip().lower()
                                       not in ("0", "false", "no"))

# ---- slug hygiene ---------------------------------------------------------------------------
# Slugs are interpolated straight into filenames (reports/<slug>.json, exports/<slug>__<key>.pdf,
# rationale/<slug>__<pub>.json). Flask's default path converter refuses "/" so `/report/<slug>` was
# always safe, but several routes read the slug from a FORM FIELD or QUERY STRING instead, where no
# converter runs: POST /export, /api/ref?slug=, /compare?slug=, /api/chart?slug=. Two of those
# (/export, /api/chart via _rationale) end up WRITING to the derived path. Nothing is exploitable
# today — assemble() happens to raise on an unknown slug before any write — but that is luck, not a
# control, and the raised exception surfaced as an unhandled HTML 500 rather than a 400.
_SLUG_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def valid_slug(slug):
    """True for slugs that can only ever name a file INSIDE our data directories."""
    return bool(slug) and _SLUG_RE.match(slug) is not None and slug not in (".", "..")

# Route the cross-encoder through a dedicated child process. This is what makes _GEN_LOCK
# unnecessary — see rerank_pool.py for the full rationale and the RSS measurements.
rerank_pool.install()


class _PrefixMiddleware:
    """Serve the app under an optional URL prefix supplied by a reverse proxy via the
    `X-Forwarded-Prefix` header (e.g. `/patents-data` when fronted at rotem.ai/patents-data).
    Sets SCRIPT_NAME so Flask's `url_for` / `request.script_root` become prefix-aware, and strips
    the prefix from PATH_INFO if the proxy passes it through. With no header the app serves at the
    root exactly as before (127.0.0.1:8631), so nothing changes for direct/local access."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "").rstrip("/")
        if prefix:
            environ["SCRIPT_NAME"] = prefix
            path = environ.get("PATH_INFO", "")
            if path.startswith(prefix):
                environ["PATH_INFO"] = path[len(prefix):] or "/"
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _PrefixMiddleware(app.wsgi_app)


@app.context_processor
def _inject_corpus_facts():
    """Make `corpus` available to EVERY template.

    base.html renders the corpus scope/currency disclosure in its footer, so every page that
    extends it needs `corpus` — but it was only passed explicitly by index, out_of_domain and
    report. notfound.html, compare.html and print.html therefore raised
    'corpus' is undefined and returned a 500, which is how a 404 for a bad slug became a crash.
    Supplying it here rather than at each render_template keeps the disclosure impossible to omit
    by forgetting an argument. Explicit corpus= arguments still win; facts() is cached.
    """
    try:
        return {"corpus": corpus_facts.facts(), "disc": disclosure}
    except Exception:
        # The disclosure must never be the reason a page fails to render.
        return {"corpus": {}}

REPORTS = DATA / "reports"
RATIONALE = DATA / "rationale"
EXPORTS = DATA / "reports" / "exports"
FLAGS = DATA / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)
RATIONALE.mkdir(parents=True, exist_ok=True)
EXPORTS.mkdir(parents=True, exist_ok=True)

_GOLD = {e["id"]: e for e in goldset.load()["entries"]}
_JOBS = {}          # slug -> {"status": "running|partial|done|error", "msg": ...}
_JOB_LOCK = threading.Lock()
# NOTE: the old module-level _GEN_LOCK is GONE. It serialized every report generation (~3 min) to
# protect the ~3 s cross-encoder step, capping the app at one concurrent search. The reranker now
# runs in a dedicated child process (rerank_pool) and the genai clients are already thread-local
# (llm.py / embed.py), so nothing in the request path is shared-mutable any more. Concurrency is
# bounded by an explicit resource budget instead — auth.run_gate (MAX_CONCURRENT_RUNS).
_R = None           # lazy singleton Retriever (loads family map once)
_R_LOCK = threading.Lock()

# ---- SSE fan-out ---------------------------------------------------------------------------
# Each /events/<slug> listener registers a Queue here; _set_job publishes every progress update to
# all listeners for that slug. Bounded queues + drop-oldest so a stalled browser can never make a
# generation thread block.
_SUBS = {}          # slug -> set[queue.Queue]
_SUBS_LOCK = threading.Lock()
_SSE_PING = 15.0    # seconds between keep-alive comments


def _subscribe(slug):
    q = queue.Queue(maxsize=64)
    with _SUBS_LOCK:
        _SUBS.setdefault(slug, set()).add(q)
    return q


def _unsubscribe(slug, q):
    with _SUBS_LOCK:
        subs = _SUBS.get(slug)
        if subs:
            subs.discard(q)
            if not subs:
                _SUBS.pop(slug, None)


def _publish(slug, event):
    with _SUBS_LOCK:
        subs = list(_SUBS.get(slug, ()))
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            try:                      # drop the oldest, keep the newest — progress is a heartbeat
                q.get_nowait()
                q.put_nowait(event)
            except Exception:
                pass


def retriever():
    global _R
    with _R_LOCK:
        if _R is None:
            _R = Retriever()
    return _R


def _wants_json(req=None):
    """True when the caller is an API client rather than a browser doing a form navigation.

    Several error paths returned a bare jsonify(), so a normal form POST that hit "server busy"
    or a bad mode rendered raw JSON in the address bar instead of a page. Content negotiation:
    an explicit Accept: application/json, an XHR/fetch marker, or an /api/ path means JSON;
    a browser navigation (which sends Accept: text/html) gets HTML.
    """
    r = req or request
    if r.path.startswith("/api/"):
        return True
    if r.headers.get("X-Requested-With", "").lower() == "xmlhttprequest":
        return True
    # Default to JSON and return HTML only when the client EXPLICITLY ranks it higher. A browser
    # navigation sends "text/html,...;q=0.9,*/*;q=0.8", so html outranks json and it gets a page.
    # API clients, curl and the test client send "*/*", which ranks both equally and therefore
    # keeps the JSON contract. Defaulting the other way would have silently turned every
    # programmatic error response into an HTML page.
    acc = r.accept_mimetypes
    if acc and acc.provided and acc["text/html"] > acc["application/json"]:
        return False
    return True


def _error_response(payload, status, title=None):
    """One error path for both audiences: JSON for API clients, the notfound page for browsers."""
    if _wants_json():
        return jsonify(payload), status
    msg = title or payload.get("detail") or payload.get("error") or "Request failed"
    return render_template("notfound.html", slug=msg), status


def slugify(text):
    return "adhoc-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def report_path(slug):
    return REPORTS / f"{slug}.json"


# ---- background report generation ----------------------------------------------------------
def _set_job(slug, **kw):
    with _JOB_LOCK:
        j = _JOBS.get(slug, {})
        j.update(kw)
        _JOBS[slug] = j
        snapshot = dict(j)
    _publish(slug, _job_event(slug, snapshot))


# ---------------------------------------------------------------------------------------------
# Stage heartbeat.
#
# The cross-encoder head is one blocking call inside a child process; there is no per-item hook to
# tap, and slicing it to manufacture one was measured to double the stage (RERANK_CHUNK in
# retrieval.py). So while it runs, tick the elapsed time from a daemon thread. It costs nothing,
# it cannot affect the result, and it turns a 56 s frozen message into a live one.
_HEARTBEATS = {}
_HB_LOCK = threading.Lock()
_HB_TICK = float(os.environ.get("PROGRESS_HEARTBEAT_SEC", "3"))


def _start_stage_heartbeat(slug, n_refs, tick=None):
    """Tick elapsed time on `slug`'s current stage until _stop_stage_heartbeat is called."""
    _stop_stage_heartbeat(slug)
    tick = _HB_TICK if tick is None else tick
    stop = threading.Event()

    def _run():
        t0 = time.time()
        while not stop.wait(tick):
            with _JOB_LOCK:
                job = _JOBS.get(slug)
                # Only keep ticking while this job is still running; never resurrect a finished
                # or errored job, and never overwrite a newer stage's message.
                if not job or job.get("status") != "running" or job.get("kind") != "reranking":
                    return
            secs = int(time.time() - t0)
            _set_job(slug, kind="reranking",
                     detail={"elapsed_sec": secs, "refs": n_refs, "stage": "rerank"},
                     msg=f"Scoring the closest {n_refs} references against your claim elements "
                         f"— {secs}s elapsed, usually about a minute…")

    t = threading.Thread(target=_run, name=f"hb-{slug}", daemon=True)
    with _HB_LOCK:
        _HEARTBEATS[slug] = stop
    t.start()
    return stop


def _stop_stage_heartbeat(slug):
    with _HB_LOCK:
        stop = _HEARTBEATS.pop(slug, None)
    if stop:
        stop.set()


def _job_event(slug, job):
    """The single wire shape shared by /status (poll) and /events (SSE), so the fallback path and
    the streaming path can never disagree."""
    st = job.get("status", "unknown")
    exists = report_path(slug).exists()
    return {"kind": job.get("kind", "progress"), "slug": slug, "status": st,
            "msg": job.get("msg", ""),
            # Structured counterpart to `msg`. The progress UI needs the NUMBERS (elements found,
            # families seen, which round) to render a narrative rather than re-parsing prose out of
            # the message string, and to keep showing the last known state during the long silent
            # stretch between 'partial' and 'reranking'.
            "detail": job.get("detail") or {},
            "ready": exists and (st in ("done", "partial") or not job),
            "done": st == "done" or (exists and not job)}


def _write_report(slug, rep):
    report_path(slug).write_text(json.dumps(rep, default=str, indent=1))
    (REPORTS / f"{slug}.view.json").unlink(missing_ok=True)   # force the view to rebuild from this


def _run_job(slug, query, subject, mode, gated, wide=False):
    """Thread entrypoint: run the generation, then always release the reserved budget slot.
    Kept separate from _generate so _generate's signature stays purely about doing the work."""
    try:
        _generate(slug, query, subject, mode, wide=wide)
    finally:
        if gated and auth.run_gate:
            auth.run_gate.end()


def _generate(slug, query, subject, mode, wide=False):
    """Run one report. Runs fully concurrently with other generations — the only serialized step is
    the cross-encoder, which lives in its own child process (rerank_pool)."""
    _set_job(slug, status="running", msg="Queued…", t0=time.time())
    try:
        _set_job(slug, msg="Decomposing the invention into technical elements…")
        A = CoverageAgent(retriever())
        # Cheap, no-spend relevance guard: is this query even in the indexed field? Never fatal —
        # a detector failure must not cost the user their search.
        # Also computed up-front in /run (the OOD interstitial) — recomputed here because a job
        # can be started from other entrypoints (gold set, warm_reports, direct ensure_report).
        # It is cheap and non-fatal; a detector failure must not cost the user their search.
        verdict = None
        try:
            verdict = domain_detect.detect(query, retriever=retriever())
        except Exception:
            traceback.print_exc()

        def on_event(stage, data):
            # Stream progress + a first render. 'partial' writes an un-reranked snapshot (cards
            # only) the moment the seed search returns, so the user sees results in seconds.
            if stage == "elements":
                _set_job(slug, kind=stage, detail={"elements": data["n"]},
                         msg=f"Decomposed into {data['n']} elements — searching all 8 channels…")
            elif stage == "partial":
                rep = data["report"]; rep["partial"] = True
                _write_report(slug, rep)
                _set_job(slug, kind=stage, status="partial",
                         detail={"families": len(rep.get("ranked_families") or [])},
                         msg="Showing the first matches — refining (more channels, rounds, claim chart)…")
            elif stage == "seeded":
                _set_job(slug, kind=stage, detail={"families": data["families"]},
                         msg=f"{data['families']} candidate families — expanding via citations, families, cross-lingual…")
            elif stage == "round":
                _set_job(slug, kind=stage, detail={"round": data["round"], "families": data["families"]},
                         msg=f"Refinement round {data['round']}: {data['families']} families — reranking…")
            elif stage == "reranking":
                _set_job(slug, kind=stage, detail={"families": data["families"]},
                         msg=f"Reranking {data['families']} families + grounding the claim chart…")
                # This stage is the cross-encoder scoring the top-25 head, and it is the last
                # ~40-60 s of the run. It used to sit on the single message above for 56.4 s --
                # about half the total elapsed time -- with nothing changing, which reads as a
                # hang. Slicing the scoring to report countable progress was measured to DOUBLE
                # the stage (see RERANK_CHUNK in retrieval.py), so tick elapsed time instead:
                # it costs nothing and the user can always see the run is alive and roughly how
                # far along it is.
                _start_stage_heartbeat(slug, RERANK_TOP)
            elif stage == "rerank_progress":
                # Only fires when RERANK_CHUNK is enabled; real per-item counts beat a heartbeat.
                done, total = data["done"], data["total"]
                if done < total:
                    _set_job(slug, kind=stage,
                             detail={"done": done, "total": total, "families": data["families"]},
                             msg=f"Scoring the closest {total} references against your claim "
                                 f"elements — {done} of {total}…")

        rep = A.run(query, subject=subject, mode=mode,
                    cfg=AgentConfig(mode=mode, max_rounds=2, elements_per_round=3, ground=True),
                    on_event=on_event)
        _stop_stage_heartbeat(slug)      # reranking finished; stop ticking before the next stage
        rep["partial"] = False
        rep["domain"] = verdict.to_dict() if verdict is not None else None
        # Federation costs real money per call, so it is opt-in per request and NEVER implicit.
        if wide:
            _set_job(slug, kind="federating",
                     msg="Searching external patent APIs (wider search)…")
            rep["federation"] = _federate_block(query, mode)
        else:
            rep["federation_offered"] = bool(verdict is not None and verdict.should_federate)
        _write_report(slug, rep)
        _set_job(slug, kind="done", status="done", msg="done")
    except Exception as e:
        traceback.print_exc()
        _set_job(slug, kind="error", status="error", msg=str(e)[:300])
    finally:
        # A crash mid-rerank must not leave a thread ticking progress onto a dead job.
        _stop_stage_heartbeat(slug)


def _federate_block(query, mode):
    """Run ONE federated search and return a display-ready block.

    Federated-only hits are deliberately kept in their own block rather than merged into
    `ranked_families`. That list holds LOCAL family keys which webview.build_view resolves
    against Postgres, so a synthetic `fedfam:` key would render as a blank card. Cross-system
    RRF fusion does exist (federation.fuse / search_two_tier) and is the right tool when the
    caller wants one ranked list; the report page wants provenance kept visible instead.
    """
    try:
        fed = federation.search(query, mode=mode)
    except Exception as e:
        # federation.search is documented never to raise; this is the belt-and-braces path.
        # Even here, still NAME the sources so the results page can show which external APIs
        # went unanswered rather than one opaque "federation failed".
        traceback.print_exc()
        reason = str(e)[:300]
        return {"ok": False, "error": reason, "error_kind": "unknown", "hits": [],
                "source_status": federation.fallback_status(reason)}
    d = fed.to_dict()
    d["hits"] = [{"pub": h.pub_number, "title": h.title, "abstract": (h.abstract or "")[:600],
                  "assignee": h.assignee, "date": h.date, "country": h.country,
                  "cpc": h.cpc[:6], "url": h.url, "family_id": h.family_id,
                  # Office-source link next to the Google one; family-scoped when the federated
                  # hit carried a family id. Never fatal -- a hit without one still renders.
                  "espacenet": _espacenet_safe(h.pub_number, h.family_id),
                  "sources": h.sources, "rank": h.rank}
                 for h in (fed.hits or [])[:40]]
    return d


def _espacenet_safe(pub, family_id=None):
    try:
        return enrich_display.espacenet_url(pub, family_id)
    except Exception:
        return None


def ensure_report(slug, query=None, subject=None, mode="novelty", regen=False, wide=False):
    """Return ('ready'|'running'|'missing'|'busy', report_or_None). Kicks off background
    generation if needed. 'busy' means the concurrency or daily spend cap is exhausted."""
    p = report_path(slug)
    if p.exists() and not regen:
        try:
            return "ready", json.loads(p.read_text())
        except Exception:
            pass
    # Atomically claim the slug: check-and-set under the lock so two concurrent requests for the
    # same new query can't both start a generation (the second sees "running" and just polls).
    with _JOB_LOCK:
        job = _JOBS.get(slug)
        if job and job["status"] in ("running", "partial"):
            return "running", None
        if query is None:
            return "missing", None
        _JOBS[slug] = {"status": "running", "msg": "Queued…", "t0": time.time()}
    # Reserve a generation slot AFTER claiming the slug (so the claim can be released cleanly).
    gated = False
    if auth.run_gate:
        ok, why = auth.run_gate.try_begin()
        if not ok:
            with _JOB_LOCK:
                _JOBS.pop(slug, None)              # release the claim; nothing was started
            return "busy", why
        gated = True
    try:
        subj_obj = _subject_obj(subject)
        if regen:
            p.unlink(missing_ok=True)
            (REPORTS / f"{slug}.view.json").unlink(missing_ok=True)
        threading.Thread(target=_run_job, args=(slug, query, subj_obj, mode, gated, wide),
                         daemon=True).start()
    except Exception:
        # Never leak the reserved slot or leave a phantom "running" claim if we fail to launch.
        traceback.print_exc()
        with _JOB_LOCK:
            _JOBS.pop(slug, None)
        if gated and auth.run_gate:
            auth.run_gate.end()
        raise
    return "running", None


def _subject_obj(subject):
    if not subject:
        return None
    from search_modes import Subject
    with db.cursor() as cur:
        cur.execute("SELECT publication_number, earliest_priority_date, filing_date, "
                    "publication_date, country FROM publications WHERE publication_number=%s LIMIT 1",
                    (subject,))
        r = cur.fetchone()
    if not r:
        return None
    return Subject(number=r["publication_number"],
                   efd=r["earliest_priority_date"] or r["filing_date"] or r["publication_date"],
                   filing_date=r["filing_date"], publication_date=r["publication_date"],
                   jurisdiction=r["country"])


# ---- routes ----------------------------------------------------------------------------------
#  EXAMPLE_QUERIES removed with the "Try an example:" chip row it fed. The chips filled the
#  textarea with one of three canned inventions; the frozen gold-set reports in /history cover
#  the same "show me what this does" need without occupying the search page.
def _gold_cards():
    return [{"id": e["id"], "category": e["category"], "mode": e["mode"],
             "subject": e.get("anchor_publication"), "notes": e.get("notes", ""),
             "query": e.get("query_text", ""),
             "cached": report_path(e["id"]).exists()}
            for e in _GOLD.values()]


@app.route("/")
def index():
    """The search page is now ONLY the search.

    It previously opened with ~50 lines of prose above the input: a headline paragraph, the full
    scope-and-reliability panel, and the indexed-CPC list. That content is not wrong -- it is the
    disclosure that stops a thin result set being read as a clear field -- but it belongs where it
    is read, not between a user and the search box. It moved intact to /about, and the search page
    keeps one factual line linking there. The equivalent disclosure on the RESULTS page and in
    every exported document is untouched: that is the point of decision, and it stays.
    """
    return render_template("index.html", corpus=corpus_facts.facts())


@app.route("/about")
def about():
    """Everything that used to sit above the search box, in full."""
    return render_template("about.html", corpus=corpus_facts.facts())


def _history_entries(limit=200):
    """Past searches, most recent first, straight off the cached reports on disk.

    Every ad-hoc run already writes <slug>.json plus a <slug>.meta.json holding the query text and
    mode, so the history needs no new bookkeeping and no database -- and because each entry is a
    cached report, opening one is instant and spends nothing. Reports whose meta file is missing
    (or which are gold entries) are skipped here; the gold set is listed separately and labelled.
    """
    out = []
    gold_ids = set(_GOLD.keys())
    try:
        paths = sorted(REPORTS.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return out
    for mp in paths[:limit]:
        slug = mp.name[:-len(".meta.json")]
        if slug in gold_ids:
            continue
        if not report_path(slug).exists():
            continue                      # a run that never finished: nothing to open instantly
        try:
            m = json.loads(mp.read_text())
        except Exception:
            continue
        try:
            ts = report_path(slug).stat().st_mtime
        except Exception:
            ts = mp.stat().st_mtime
        out.append({"slug": slug, "query": (m.get("query") or "")[:400],
                    "mode": m.get("mode", "novelty"), "subject": m.get("subject"),
                    "ood": bool(m.get("ood")),
                    "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), "ts": ts})
    return out


@app.route("/history")
def history():
    """Search history + the frozen gold-set examples, clearly separated.

    The gold entries used to sit on the search page under "or open a frozen gold-set example
    (instant)". They are demo fixtures, not the user's work, so they are labelled as examples here
    rather than mixed into the history list.
    """
    return render_template("history.html", entries=_history_entries(), gold=_gold_cards(),
                           corpus=corpus_facts.facts())


@app.route("/run", methods=["POST"])
def run():
    gold_id = request.form.get("gold_id", "").strip()
    if gold_id and gold_id in _GOLD:
        e = _GOLD[gold_id]
        st, why = ensure_report(gold_id, query=e["query_text"],
                                subject=e.get("anchor_publication"), mode=e["mode"])
        if st == "busy":
            return _error_response({"error": "server busy", "detail": why}, 429,
                                   f"The server is at capacity — {why}. Please retry shortly.")
        return redirect(url_for("report", slug=gold_id))
    query = request.form.get("query", "").strip()
    # Validate at the API boundary, not the dropdown: the form had no allowlist, so a crafted
    # POST could reach the pipeline with mode=invalidity (returning novelty dates mislabelled as
    # an invalidity opinion) or mode=fto (unhandled 500).
    mode = request.form.get("mode", "novelty").strip()
    try:
        mode = require_available(mode).value
    except ModeNotAvailable as e:
        # e.mode is a str-Enum: str() on it yields "Mode.INVALIDITY" on py3.9, which is an
        # implementation detail leaking into the API. Emit the wire value.
        return _error_response({"error": "mode_not_available",
                                "mode": getattr(e.mode, "value", str(e.mode)),
                                "detail": e.message, "missing": e.missing}, 400,
                               f"That search mode is not available: {e.message}")
    except ValueError as e:
        return _error_response({"error": "unknown_mode", "detail": str(e)}, 400,
                               f"Unknown search mode: {e}")
    subject = request.form.get("subject", "").strip() or None
    #  FEDERATION IS NOW UNCONDITIONAL.
    #
    #  The "Also search wider — external patent APIs" checkbox is gone and every search federates.
    #  The "|wide" marker STAYS in the slug, and deliberately so: it is what keeps these reports in
    #  a different cache namespace from the narrow reports generated before this change. Dropping
    #  it would have made a new wide run overwrite the cached narrow report for the same query --
    #  silently replacing a result the user may have cited. Every pre-existing narrow report keeps
    #  its own slug, stays readable at its own URL, and is listed in /history.
    wide = True
    if not query:
        return redirect(url_for("index"))
    #  OUT-OF-DOMAIN: STILL DETECTED, NO LONGER A GATE.
    #
    #  The detector still runs HERE rather than inside _generate(): it is cheap (embedding + CPC
    #  signals, llm=False on the tested queries) and running it before the pipeline is what keeps
    #  the verdict available to record on the report.
    #
    #  The interstitial existed to offer the wider search as a paid choice before spending on a
    #  local-only run that could not answer the query. Federation is now automatic, so that choice
    #  no longer exists and re-asking it would be a pointless second click. The INFORMATION is
    #  still valuable, though -- "your query is outside the indexed field" is exactly what stops a
    #  thin local result set being misread -- so the verdict is recorded on the report and shown
    #  as a banner at the top of the results, where it is read against the actual results.
    ood = None
    try:
        v = domain_detect.detect(query, retriever=retriever())
    except Exception:
        v = None                          # detector failure must never block a search
    if v is not None and v.should_federate:
        ood = v.to_dict()
    # `wide` MUST be part of the slug: a wide result and a narrow one are different reports and
    # would otherwise overwrite each other's cache.
    slug = slugify(query + "|" + mode + ("|wide" if wide else ""))
    st, why = ensure_report(slug, query=query, subject=subject, mode=mode, wide=wide)
    if st == "busy":
        return _error_response({"error": "server busy", "detail": why}, 429,
                               f"The server is at capacity — {why}. Please retry shortly.")
    # remember adhoc meta for the report page title
    (REPORTS / f"{slug}.meta.json").write_text(json.dumps(
        {"query": query, "mode": mode, "subject": subject, "wide": wide, "ood": ood}))
    return redirect(url_for("report", slug=slug))


@app.route("/report/<slug>")
def report(slug):
    regen = request.args.get("rerun") == "1"
    query = subject = None
    mode = "novelty"
    wide = False        # the progress view lists the federation stage only for a wide run
    ood = None          # out-of-domain verdict recorded at search time, shown as a results banner
    if slug in _GOLD:
        e = _GOLD[slug]
        query, subject, mode = e["query_text"], e.get("anchor_publication"), e["mode"]
        title = f"{slug}  ·  {e['category']}"
    else:
        meta = REPORTS / f"{slug}.meta.json"
        if meta.exists():
            m = json.loads(meta.read_text())
            query, subject, mode = m["query"], m.get("subject"), m.get("mode", "novelty")
            wide = bool(m.get("wide"))
            ood = m.get("ood")
        title = "Ad-hoc search"
    status, rep = ensure_report(slug, query=query, subject=subject, mode=mode, regen=regen)
    if status == "missing":
        return render_template("notfound.html", slug=slug), 404
    if status == "busy":
        return render_template("notfound.html", slug=f"{slug} — {rep}"), 429
    if status != "ready":
        return render_template("generating.html", slug=slug, title=title,
                               query=(query or "")[:400], mode=mode, wide=wide)
    view = _build_view_cached(slug, rep, regen)
    view["slug"] = slug
    view["title"] = title
    view["is_gold"] = slug in _GOLD
    return render_template("report.html", v=view, ood=ood, corpus=corpus_facts.facts())


def _build_view_cached(slug, rep, regen=False):
    """Cache the built view (query embed + DB resolution) to <slug>.view.json for instant reloads.
    A partial/streaming snapshot is NEVER cached (it would shadow the final report) and is flagged
    so the report page keeps polling and upgrades itself when the full run finishes."""
    partial = bool(rep.get("partial"))
    vp = REPORTS / f"{slug}.view.json"
    if vp.exists() and not regen and not partial:
        try:
            view = json.loads(vp.read_text())
            # source_tags is STATUS, not content. The cache exists to skip the query embed,
            # the DB resolution and the claim-matrix verification -- all immutable for a
            # finished report. Which APIs are wired up is not: it changes when a key is
            # added upstream, and the first view built after a restart sees an empty source
            # catalogue (the health probe is backgrounded so a render never blocks on it).
            # Freezing that would leave a report permanently claiming its sources failed.
            view["source_tags"] = webview._source_tags(rep, len(view.get("cards") or []))
            return view
        except Exception:
            pass
    view = webview.build_view(rep, top_n=25)
    view["partial"] = partial
    if not partial:
        # Verify the element x reference matrix BEFORE it is cached and rendered. Until now a
        # filled cell there meant only "the retriever returned this publication for this element";
        # nothing checked that the cited passage discloses anything, and an audit measured 7 of 12
        # coordinate-backed cells as false positives. One batched LLM pass per report, cached in
        # the view, so it costs nothing on reload. Never fatal: on failure cells stay "unchecked"
        # and the template renders them as retrieval-only rather than as coverage.
        try:
            claim_chart.verify_matrix(view.get("claim_chart") or {}, rep)
        except Exception:
            traceback.print_exc()
        vp.write_text(json.dumps(view, default=str))
    return view


@app.route("/status/<slug>")
def status(slug):
    """Polling fallback. Kept as the compatibility path for clients without EventSource (and for
    regression.sh); /events/<slug> is the primary, push-based channel."""
    with _JOB_LOCK:
        job = dict(_JOBS.get(slug, {}))
    ev = _job_event(slug, job)
    # 'partial' is renderable (first cards streamed); 'done' is the final report. A cached report on
    # disk with no live job is treated as done.
    # `detail` too: the poll fallback drives the same progress narrative as the SSE path, and it
    # would otherwise silently lose the counters that SSE clients get.
    return jsonify({"ready": ev["ready"], "status": ev["status"], "done": ev["done"],
                    "msg": ev["msg"], "kind": ev["kind"], "detail": ev["detail"]})


@app.route("/events/<slug>")
def events(slug):
    """Server-Sent Events stream of generation progress — replaces 1.5 s polling.

    nginx is already streaming-ready for this location (proxy_buffering off, proxy_read_timeout
    1800s); we additionally send X-Accel-Buffering: no so no other proxy re-buffers us, and a
    comment heartbeat every 15 s so idle connections are not reaped.
    """
    q = _subscribe(slug)

    def gen():
        try:
            # 1. Immediately emit current state, so a client that connects late (or after the run
            #    already finished) is never left waiting for an event that will never come.
            with _JOB_LOCK:
                job = dict(_JOBS.get(slug, {}))
            first = _job_event(slug, job)
            yield f"data: {json.dumps(first)}\n\n"
            if first["done"] or first["status"] == "error":
                return
            # 2. Then stream updates until terminal, with keep-alive pings.
            last = time.time()
            while True:
                try:
                    ev = q.get(timeout=1.0)
                except queue.Empty:
                    if time.time() - last >= _SSE_PING:
                        last = time.time()
                        yield ": ping\n\n"
                    continue
                last = time.time()
                yield f"data: {json.dumps(ev)}\n\n"
                if ev["status"] in ("done", "error") or ev["done"]:
                    return
        except GeneratorExit:       # client disconnected
            raise
        except Exception as e:      # never let a stream error escape as a 500 mid-body
            yield f"data: {json.dumps({'kind': 'error', 'status': 'error', 'msg': str(e)[:200], 'ready': False, 'done': False})}\n\n"
        finally:
            _unsubscribe(slug, q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache, no-transform",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# ---- lazy per-card enrichment (drawings + PDF + sections + rationale) -----------------------
# How much reference text the rationale generator may see. The old prompt was title+abstract
# (900 chars) plus ONE nearest chunk (900 chars). On a reference whose nearest chunk was its
# own abstract that is title-level reasoning, and it shows: EP-0176125-A1, an adhesive
# wall-fixing patent, was described as disclosing a "driver pin for mechanical coupling of
# clamping means" on nothing but a lexical match on "pin" and "clamped". Claims and the
# description body are where disclosure actually lives.
_RAT_BIBLIO_CHARS = 900
_RAT_PASSAGE_CHARS = 700          # per passage
_RAT_EVIDENCE_CHARS = 4200        # total across all passages
_RAT_MAX_PASSAGES = 8
_RAT_SOURCE_CHARS = 8000          # what we persist as _source_text / show the verifier
# 1100, not the old 500: the evidence block is ~4x larger now, so the model writes more (and
# longer-quoted) reads_on entries. At 500 the JSON came back truncated mid-object and
# llm.chat_json returns {} on a parse error — which surfaced as a card with a BLANK
# "why relevant", strictly worse than a title-level one. Measured at 4 of 40 before this.
_RAT_MAX_TOKENS = 1100

_CLAIM_KINDS = ("claim_own", "claim_resolved")
_BODY_KINDS = ("paragraph", "whole", "figure_caption")


def _english_half(text):
    """Many DE/EP publications store a claim as the German text immediately followed by its
    English machine translation, concatenated with NO separator. Feeding both doubles the
    tokens and lets the model 'find' its quote in whichever half suits it."""
    t = (text or "").strip()
    if not t:
        return t
    try:
        pair = translate.split_bilingual(t)
        if pair and pair[1].strip():
            return pair[1].strip()
    except Exception:
        pass
    return t


def _passage_label(p):
    """Human location tag for a passage, used both to label it in the prompt and to cite it."""
    c = p.get("coord") or {}
    if isinstance(c, dict):
        if c.get("claim_no") is not None:
            return f"claim {c['claim_no']}"
        if c.get("para_no") is not None:
            return f"paragraph {c['para_no']}"
        if c.get("figure_no") is not None:
            return f"figure {c['figure_no']}"
    return {"abstract": "abstract", "whole": "description",
            "paragraph": "description", "claim_own": "claim",
            "claim_resolved": "claim",
            "figure_caption": "figure caption"}.get(p.get("kind") or "", "text")


def ref_passages(cur, pid, qvec, secs=None, limit=_RAT_MAX_PASSAGES):
    """Evidence set for the rationale: the independent claims, then the query's nearest chunks
    across every text kind (claims, description paragraphs, abstract, figure captions).

    The independent claims are SEEDED rather than left to the embedding to find. In a novelty
    read the claims are the disclosure, but a description paragraph that merely shares
    vocabulary with the query routinely outranks the claim it belongs to, so a purely
    dense-ranked evidence set was often claim-free.
    """
    out, seen = [], set()

    def add(kind, coord, text, score=0.0):
        t = (text or "").strip()
        if not t:
            return
        key = (kind, json.dumps(coord, sort_keys=True, default=str), t[:80])
        if key in seen:
            return
        seen.add(key)
        out.append({"kind": kind, "coord": coord, "text": t, "score": score})

    claims = list((secs or {}).get("claims") or [])
    if not claims and cur is not None and pid:
        try:
            cur.execute("SELECT claim_no, is_independent, text, resolved_text FROM claims "
                        "WHERE publication_id=%s ORDER BY claim_no LIMIT 40", (pid,))
            claims = [{"claim_no": r["claim_no"], "independent": r["is_independent"],
                       "text": r["text"], "resolved_text": r["resolved_text"]}
                      for r in cur.fetchall()]
        except Exception:
            claims = []
    indep = [c for c in claims if c.get("independent")] or claims[:1]
    for c in indep[:2]:
        add("claim_own", {"claim_no": c.get("claim_no")},
            c.get("resolved_text") or c.get("text"))

    # The query's nearest chunks of ANY kind. webview.match_in_pub is LIMIT 1 and exists for
    # the highlight coordinate; the rationale needs a SET, because a reference whose single
    # nearest chunk happened to be its own abstract gave the model nothing but bibliographic
    # text to reason from. Kept here rather than in webview to stay out of that module.
    if cur is not None and pid and qvec is not None:
        try:
            cur.execute(
                "SELECT kind, coord, 1-(embedding <=> %s::vector) AS score, text "
                "FROM chunks WHERE publication_id=%s AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (webview._vec(qvec), pid, webview._vec(qvec), int(limit)),
            )
            for r in cur.fetchall():
                coord = r["coord"] if isinstance(r["coord"], dict) else (
                    json.loads(r["coord"]) if r["coord"] else None)
                add(r["kind"], coord, r["text"], float(r["score"] or 0.0))
        except Exception:
            pass
    return out[:limit + 2]


def _evidence_block(passages):
    """-> (prompt_text, passages_actually_shown). Each passage is tagged with its location so
    the model can cite it and so a quote can be traced back to a claim/paragraph number."""
    blocks, shown, used = [], [], 0
    for p in passages or []:
        t = _english_half(p.get("text"))[:_RAT_PASSAGE_CHARS]
        if not t:
            continue
        label = p.get("label") or _passage_label(p)
        piece = f"[{label}] {t}"
        if used + len(piece) > _RAT_EVIDENCE_CHARS:
            break
        blocks.append(piece)
        used += len(piece) + 2
        shown.append({"kind": p.get("kind"), "coord": p.get("coord"),
                      "text": t, "label": label})
    return "\n\n".join(blocks), shown


def _text_basis(shown):
    """What the opinion is ACTUALLY based on — recorded, and stated in the prose when thin."""
    kinds = {p.get("kind") for p in shown}
    has_claims = bool(kinds & set(_CLAIM_KINDS))
    has_body = bool(kinds & set(_BODY_KINDS))
    if has_claims and has_body:
        return "claims+description"
    if has_claims:
        return "claims"
    if has_body:
        return "description"
    if "abstract" in kinds:
        return "abstract-only"
    return "title-only"


def _rationale(slug, pub, query, elements, biblio_txt, matched_txt=None, passages=None):
    cache = RATIONALE / f"{slug}__{pub}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    # Deterministic guard: with no reference text (title-less junk / un-enriched thin doc) an LLM
    # can only hallucinate. Return an explicit "not verifiable" instead of inventing disclosure.
    # Back-compat: callers (and tests) may still pass a single passage STRING as matched_txt.
    if passages is None:
        if isinstance(matched_txt, (list, tuple)):
            passages = list(matched_txt)
        elif matched_txt:
            passages = [{"kind": None, "coord": None, "text": matched_txt}]
        else:
            passages = []
    evidence, shown = _evidence_block(passages)
    basis = _text_basis(shown)

    title_abs = re.sub(r"^\S+\s*", "", (biblio_txt or "").strip()).strip(" .")
    if len(title_abs) < 8 and not evidence.strip():
        res = {"why": "Reference text was not available to verify relevance; treat as unconfirmed.",
               "reads_on": []}
        cache.write_text(json.dumps(res))
        return res
    # M9 rationale-accuracy tightening: ground STRICTLY in the provided text and make anti-overclaim
    # DETERMINISTIC — the model must quote the supporting words per element, and code drops any
    # element whose evidence quote is not actually present in the shown reference text. (Audit:
    # overclaim+hallucinate was 22%; re-measured after this change.)
    sysmsg = (
        "You are a careful patent prior-art analyst. Use ONLY the reference text provided below "
        "(its bibliographic data and the tagged claim / description passages). Do NOT use outside "
        "knowledge and do NOT "
        "assume features not shown in that text. Return JSON with two keys: "
        '"why" = 1-2 sentences on why the reference is relevant, citing the SPECIFIC overlapping '
        "wording that actually appears in the reference text (quote or closely paraphrase it), "
        "HEDGED ('appears to', 'the abstract mentions') on partial matches. Every specific feature "
        'you name in "why" must be one you can also ground in reads_on — never name a structural '
        'feature the text does not show. "reads_on" = a list of objects {"element":"<one invention '
        'element, verbatim from the list>","evidence":"<a short quote copied from the reference text '
        'that discloses that element>"}. Include an element ONLY if you can quote reference text that '
        "explicitly discloses it; if the text is just a title or does not clearly show an element, "
        "OMIT it — an empty reads_on is the correct answer when nothing is clearly disclosed. Prefer "
        "omitting over guessing. Each passage is tagged with its location, e.g. [claim 1] or "
        "[paragraph 0012]; prefer quoting a claim or description passage over the abstract, since "
        "the abstract summarises rather than defines what is disclosed.")
    if basis in ("abstract-only", "title-only"):
        # Do not let a thin record be presented with the same confidence as a full one.
        sysmsg += (" IMPORTANT: this reference's claims and description are NOT available — you "
                   "have only its " + ("abstract" if basis == "abstract-only" else "title") +
                   ". Say so explicitly in \"why\" (e.g. 'based on the abstract alone') and do not "
                   "assert structural detail that only a full text could establish.")
    bib = (biblio_txt or "")[:_RAT_BIBLIO_CHARS]
    usr = (f"Invention query: {query[:800]}\n\nInvention elements (candidates — include only the "
           f"ones the reference text actually discloses, with a quote): {json.dumps(elements)}\n\n"
           f"Reference bibliographic data: {bib}\n\n"
           f"Reference text — claims and description passages, each tagged with its location. "
           f"This is the ONLY evidence you may use:\n"
           f"{evidence or '(no claims or description text is available for this reference)'}")
    out = llm.chat_json(sysmsg, usr, max_tokens=_RAT_MAX_TOKENS) or {}
    if not (out.get("why") or "").strip() and len(shown) > 4:
        # Still empty: retry once against a trimmed evidence set. Cheaper and more honest than
        # caching a blank opinion, and it keeps the long tail of very verbose references.
        short, shown_short = _evidence_block(passages[:4])
        if short and short != evidence:
            retry = llm.chat_json(sysmsg, usr.replace(evidence, short),
                                  max_tokens=_RAT_MAX_TOKENS) or {}
            if (retry.get("why") or "").strip():
                out, evidence, shown = retry, short, shown_short
    # Ground against EXACTLY the string the model was shown. Building `source` from the
    # untruncated inputs (as before) let a quote verify against text the generator never saw.
    source = f"{bib}\n\n{evidence}".strip()
    diag = []
    reads_on = _ground_reads_on(out.get("reads_on") or [], source, diag=diag)
    why, why_state = _verify_why(out.get("why", ""), source)
    if not (why or "").strip():
        # Never cache a silently blank rationale — say what happened instead.
        why = ("A grounded rationale could not be generated for this reference; "
               "treat its relevance as unconfirmed.")
        why_state = "generation-failed"
    # Trace each surviving element back to the claim / paragraph its quote came from. Same
    # grounding thresholds as the keep/drop test -- this only ATTRIBUTES, it never rescues.
    kept_set = {e.lower() for e in reads_on}
    citations = []
    for item in (out.get("reads_on") or []):
        if not isinstance(item, dict):
            continue
        el = (item.get("element") or "").strip()
        ev = (item.get("evidence") or "").strip()
        if not el or not ev or el.lower() not in kept_set:
            continue
        loc = grounding.best_passage(ev, shown) if shown else {}
        if loc:
            citations.append({"element": el, "label": loc.get("label"),
                              "kind": loc.get("kind"), "coord": loc.get("coord"),
                              "span": round(float(loc.get("span") or 0.0), 3)})
    res = {"why": why, "reads_on": reads_on, "why_grounding": why_state,
           "citations": citations, "text_basis": basis,
           "n_passages": len(shown),
           # The EXACT text the generator was shown. The audit judge previously rebuilt its own
           # reference text (title+abstract+claim 1) and so graded the rationale against text the
           # generator never saw -- that desync alone inflated the measured rate. Persisting the
           # real input lets audit.judge_rationale grade like-for-like.
           "_source_text": source[:_RAT_SOURCE_CHARS],
           "grounding_diag": diag}
    cache.write_text(json.dumps(res))
    return res


_WORD_RE = re.compile(r"[a-z0-9]+")

def _ground_reads_on(raw, ref_text, min_overlap=None, diag=None):
    """Keep an element only if the model's evidence quote is genuinely grounded in the reference
    text we showed it. Deterministic anti-overclaim: a fabricated or absent-from-text element is
    dropped even if the model listed it. Tolerates the old string-only shape.

    The test is now `grounding.grounded` (local sliding-window span + word-order bigrams) rather
    than a global bag-of-words overlap. The old rule scored the quote against a haystack SET that
    grew with the passage, so it got monotonically easier as reference text got longer -- an OPS
    full-text backfill lengthened passages and the measured overclaim rate went 10% -> 26.3%
    WITHOUT the rule ever failing loudly. See src/grounding.py for the measurement.

    `min_overlap` is accepted only for backwards compatibility with callers/tests that passed the
    old threshold positionally; it maps onto the span component.
    """
    kept = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                kept.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        el = (item.get("element") or "").strip()
        ev = (item.get("evidence") or "").strip()
        if not el:
            continue
        span_min = grounding.MIN_SPAN if min_overlap is None else float(min_overlap)
        ok = grounding.grounded(ev, ref_text, min_span=span_min)
        if diag is not None:
            # Persist the decision AND its scores. Previously only the surviving element names
            # were stored, so the filter's own effect could never be measured after the fact --
            # which is precisely how it regressed from 10% to 26.3% unnoticed. With this, a future
            # audit can recompute the drop rate without re-running the generator.
            d = grounding.explain(ev, ref_text)
            d.update({"element": el, "evidence": ev[:200], "kept": ok})
            diag.append(d)
        if ok:
            kept.append(el)          # evidence is quoted from the shown text -> keep
        # no evidence, or evidence not actually quoted from the text -> drop (anti-overclaim)
    # de-dup preserving order
    seen = set(); out = []
    for e in kept:
        if e.lower() not in seen:
            seen.add(e.lower()); out.append(e)
    return out


_WHY_VERIFY_SYS = (
    "You verify one AI-written sentence about a patent reference against that reference's ACTUAL "
    "text. You are looking for assertions that the reference DISCLOSES something it does not "
    "actually disclose. Ignore statements about the invention/query itself and ignore hedged "
    "statements that are true of the text. Return JSON "
    '{"supported": true|false, "unsupported": ["<the specific unsupported assertion>"], '
    '"corrected": "<1-2 sentences making ONLY assertions the reference text supports, hedged '
    'where partial; empty string if the text supports nothing specific>"}. '
    "Judge ONLY against the supplied text; you have no outside knowledge of this patent.")


def _verify_why(why, source):
    """Second, independent pass over the PROSE.

    The deterministic filter only ever governed `reads_on`; `why` -- the sentence a reader
    actually reads -- was never checked against anything. Since the audit judge weighs the prose
    heavily, an unfiltered `why` was a large share of the measured overclaim rate. This runs a
    cheap verifier that must REFUTE rather than confirm, and on refutation we substitute the
    verifier's strictly-grounded rewrite. Failures are non-fatal: any error keeps the original
    text rather than blanking the card.
    """
    why = (why or "").strip()
    if not why or not (source or "").strip():
        return why, "no-source"
    try:
        out = llm.chat_json(_WHY_VERIFY_SYS,
                            f"REFERENCE ACTUAL TEXT:\n{source[:_RAT_SOURCE_CHARS]}\n\n"
                            f"ASSERTION:\n{why}",
                            max_tokens=400) or {}
    except Exception:
        return why, "verifier-error"
    if out.get("supported") is True:
        return why, "verified"
    corrected = (out.get("corrected") or "").strip()
    if corrected:
        return corrected, "corrected"
    return ("The reference is topically related, but its available text does not clearly "
            "disclose specific elements of the query; treat as unconfirmed."), "stripped"


@app.route("/api/ref/<pub>")
def api_ref(pub):
    slug = request.args.get("slug", "")
    # Optional, but when present it reaches _rationale() which WRITES rationale/<slug>__<pub>.json.
    # Query strings bypass the route converter, so vet it here.
    if slug and not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    disp = enrich_display.enrich_for_display(pub)
    # DB sections + matched coordinate (for highlighting)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
        row = cur.fetchone()
        secs, matched, rat_passages = None, None, []
        if row:
            pid = row["id"]
            secs = webview.sections(cur, pid)
            q = _query_for_slug(slug)
            if q:
                qv = _query_vec(slug, q)
                matched = webview.match_in_pub(cur, pid, qv)
                rat_passages = ref_passages(cur, pid, qv, secs)
    # fall back to SerpApi claims when DB has none
    if secs is not None and not secs["claims"] and disp.get("claims"):
        secs["claims"] = [{"claim_no": i + 1, "independent": None, "text": c, "resolved_text": None}
                          for i, c in enumerate(disp["claims"])]
    rationale = None
    # `light=1` returns everything EXCEPT the grounded opinion. The results list needs sections
    # (claims / description) to fill a card's expandable panes, and _rationale() runs a Vertex call
    # for any pub that has no cached opinion yet — so without this, merely opening the Claims tab
    # (or lazily hydrating a card) would spend LLM budget the user never asked for. The opinion is
    # still fetched eagerly by the "Why relevant" pane and the full detail view.
    if slug and request.args.get("light") != "1":
        q = _query_for_slug(slug)
        rep = _load_report(slug)
        if q and rep:
            biblio_txt = f"{pub} {disp.get('title') or ''}. {disp.get('abstract') or ''}"
            # SerpApi-sourced claims when the DB has none, so a BigQuery-thin record still gets
            # claim text rather than silently degrading to title-level reasoning.
            if not rat_passages and secs and secs.get("claims"):
                rat_passages = [{"kind": "claim_own", "coord": {"claim_no": c.get("claim_no")},
                                 "text": c.get("resolved_text") or c.get("text")}
                                for c in secs["claims"][:2]]
            rationale = _rationale(slug, pub, q, rep.get("elements", []), biblio_txt,
                                   passages=rat_passages)
    # Pure-heuristic language flag: costs nothing, so it is safe on every card. The actual
    # translation stays behind its own lazy endpoint.
    disp["lang_flags"] = {"abstract": translate.looks_nonenglish(disp.get("abstract") or "")}
    return jsonify({
        "pub": pub, "display": disp, "sections": secs,
        "matched": {"coord": webview._coord_str((matched or {}).get("coord")),
                    "kind": (matched or {}).get("kind"),
                    "score": round((matched or {}).get("score", 0) or 0, 3),
                    "coord_raw": (matched or {}).get("coord")} if matched else None,
        "rationale": rationale,
    })


_QCACHE = {}
def _query_for_slug(slug):
    if slug in _GOLD:
        return _GOLD[slug]["query_text"]
    rep = _load_report(slug)
    return rep.get("query") if rep else None


def _load_report(slug):
    p = report_path(slug)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _query_vec(slug, q):
    if slug not in _QCACHE:
        _QCACHE[slug] = embed.embed_query(q[:8000], 768)
    return _QCACHE[slug]


# A publication number is like "US-11207792-B2"; a figure filename like "003.png". Validate both
# BEFORE any path use — defense-in-depth against traversal on top of Flask's safe_join.
_PUB_RE = re.compile(r"^[A-Za-z]{2}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_FNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _safe_pub(pub):
    return bool(pub) and len(pub) <= 40 and bool(_PUB_RE.match(pub))


@app.route("/figures/<pub>/<path:fname>")
def figures(pub, fname):
    if not _safe_pub(pub) or not _FNAME_RE.match(fname):   # reject traversal / odd names early
        abort(404)
    d = enrich_display.FIGDIR / pub
    if not (d / fname).exists():
        abort(404)
    return send_from_directory(d, fname)                    # Flask safe_join is the second guard


@app.route("/api/figs")
def api_figs():
    """Batch figure manifest for the results list — disk only, no network, no LLM.

    The card list needs one thumbnail per reference. The only endpoint that could answer that was
    /api/ref, which ALSO runs display enrichment (outbound HTTP) and the grounded rationale (a
    Vertex call), so the old page fanned 25 of those out just to decide whether to show a sketch.
    This reads the already-downloaded figure directory and nothing else, in one request, which is
    what makes 'never a stuck spinner' cheap enough to guarantee.
    """
    pubs = [p for p in (request.args.get("pubs") or "").split(",") if p][:80]
    out = {}
    for pub in pubs:
        if not _safe_pub(pub):
            continue
        out[pub] = webview._cached_images(pub)
    return jsonify(out)


def _pdf_available(pub: str) -> bool:
    """Would /pdf/<pub> actually serve something? Same two sources the route itself uses."""
    if not _safe_pub(pub):
        return False
    if (enrich_display.PDFDIR / f"{pub}.pdf").exists():
        return True
    disp = enrich_display.load_cached(pub)
    return bool((disp or {}).get("_display", {}).get("pdf_url")) if disp else False


@app.route("/api/pdfs")
def api_pdfs():
    """Batch PDF-availability manifest for the results list — disk only, no network, no LLM.

    The report used to emit a "PDF" link for EVERY card unconditionally while /pdf/<pub> aborts
    404 whenever neither a cached file nor a cached pdf_url exists: 23 of 34 links on the gold
    report were dead, and which ones tracked on-disk presence exactly. Offering a lawyer a link
    that 404s four times out of five is worse than not offering it, so the page now asks first
    and only promotes the ones that resolve. Mirrors /api/figs deliberately.
    """
    pubs = [p for p in (request.args.get("pubs") or "").split(",") if p][:80]
    return jsonify({pub: _pdf_available(pub) for pub in pubs if _safe_pub(pub)})


@app.route("/pdf/<pub>")
def pdf(pub):
    if not _safe_pub(pub):
        abort(404)
    f = enrich_display.PDFDIR / f"{pub}.pdf"
    if f.exists():
        return send_from_directory(enrich_display.PDFDIR, f"{pub}.pdf",
                                   mimetype="application/pdf")
    # fall back to remote pdf if we have it cached in enriched json
    disp = enrich_display.load_cached(pub)
    url = (disp or {}).get("_display", {}).get("pdf_url") if disp else None
    if url:
        return redirect(url)
    abort(404)


@app.route("/print/<slug>")
def print_view(slug):
    rep = _load_report(slug)
    if not rep:
        abort(404)
    #  _build_view_cached, not build_view: the per-cell disclosure verdicts are applied there.
    #  Calling build_view directly is why the print view rendered cells that nothing had checked.
    view = _build_view_cached(slug, rep)
    view["slug"] = slug
    view["title"] = slug
    return render_template("print.html", v=view)


# ---- triage flags + notes (persist per report) ---------------------------------------------
def _flags_path(slug):
    return FLAGS / f"{slug}.flags.json"


def load_flags(slug):
    p = _flags_path(slug)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


@app.route("/api/flags/<slug>", methods=["GET", "POST"])
def api_flags(slug):
    # The path converter already refuses "/", but this route WRITES flags/<slug>.flags.json, so
    # hold it to the same character set as every other filesystem-bound slug.
    if not valid_slug(slug):
        return jsonify({"ok": False, "error": "invalid slug"}), 400
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        pub = data.get("pub")
        if not pub:
            return jsonify({"ok": False}), 400
        flags = load_flags(slug)
        entry = flags.get(pub, {})
        if "flag" in data:
            entry["flag"] = data["flag"]          # relevant | maybe | not | ""
        if "note" in data:
            entry["note"] = data["note"]
        flags[pub] = entry
        _flags_path(slug).write_text(json.dumps(flags, indent=1))
        return jsonify({"ok": True, "flags": flags})
    return jsonify(load_flags(slug))


# ---- export selected references -> PDF / DOCX (the headline) --------------------------------
@app.route("/export", methods=["POST"])
def export():
    slug = request.form.get("slug", "").strip()
    fmt = request.form.get("format", "pdf").strip().lower()
    pubs = [p for p in request.form.get("pubs", "").split(",") if p.strip()]
    if not slug or not pubs or fmt not in ("pdf", "docx"):
        return jsonify({"error": "need slug, pubs, format(pdf|docx)"}), 400
    # `slug` arrives in a form field, so no route converter has vetted it, and it is about to become
    # part of a path we WRITE to. Validate before touching the filesystem.
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    key = hashlib.sha1((slug + "|" + fmt + "|" + ",".join(sorted(pubs))).encode()).hexdigest()[:12]
    out = EXPORTS / f"{slug}__{key}.{fmt}"
    if not out.exists():
        # An unknown slug used to raise inside assemble() and surface as an unhandled HTML 500.
        if not report_path(slug).exists() and slug not in _GOLD:
            return jsonify({"error": "unknown report", "slug": slug}), 404
        try:
            model = export_data.assemble(slug, pubs)
        except Exception as e:
            return jsonify({"error": "could not assemble export", "detail": str(e)[:200]}), 400
        if fmt == "pdf":
            export_pdf.render(model, out)
        else:
            export_docx.render(model, out)
    dl = f"prior-art-{slug}.{fmt}"
    mime = "application/pdf" if fmt == "pdf" else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return send_from_directory(EXPORTS, out.name, as_attachment=True,
                               download_name=dl, mimetype=mime)


# ---- citation graph + more-like-this -------------------------------------------------------
@app.route("/api/graph/<pub>")
def api_graph(pub):
    """cited_by (forward) + patent_citations (backward) + similar_documents from the SerpApi cache."""
    disp = enrich_display.load_cached(pub)
    raw = (disp or {}).get("raw") if disp else None
    if not raw:
        enrich_display.enrich_for_display(pub)
        disp = enrich_display.load_cached(pub)
        raw = (disp or {}).get("raw") if disp else None
    raw = raw or {}
    def _pubs(node, key):
        out = []
        v = node.get(key)
        items = v.get("original", []) if isinstance(v, dict) else (v or [])
        for c in items[:40]:
            if isinstance(c, dict) and c.get("publication_number"):
                out.append({"pub": c["publication_number"], "title": c.get("title"),
                            "date": c.get("priority_date") or c.get("publication_date"),
                            "examiner": bool(c.get("examiner_cited"))})
        return out
    incorpus = set()
    cand = [c["pub"] for grp in ("backward", "forward", "similar") for c in []]  # filled below
    backward = _pubs(raw, "patent_citations")
    forward = _pubs(raw, "cited_by")
    similar = []
    for s in (raw.get("similar_documents") or [])[:20]:
        if isinstance(s, dict) and s.get("publication_number"):
            similar.append({"pub": s["publication_number"], "title": s.get("title"),
                            "date": s.get("publication_date")})
    # which of these are in our corpus (so the UI can open them as references)
    allpubs = list({x["pub"] for x in backward + forward + similar})
    if allpubs:
        norm = {p.replace("-", ""): p for p in allpubs}
        with db.cursor() as cur:
            cur.execute("SELECT publication_number FROM publications WHERE "
                        "replace(publication_number,'-','') = ANY(%s)", (list(norm.keys()),))
            for r in cur.fetchall():
                incorpus.add(r["publication_number"].replace("-", ""))
    def mark(lst):
        for x in lst:
            x["in_corpus"] = x["pub"].replace("-", "") in incorpus
        return lst
    return jsonify({"pub": pub, "backward": mark(backward), "forward": mark(forward),
                    "similar": mark(similar)})


@app.route("/api/morelike/<pub>")
def api_morelike(pub):
    """Query-by-example: nearest families to this reference's own embedding (in-corpus)."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
        row = cur.fetchone()
        if not row:
            return jsonify({"pub": pub, "results": []})
        pid = row["id"]
        cur.execute("SELECT embedding FROM chunks WHERE publication_id=%s AND embedding IS NOT NULL "
                    "AND kind IN ('whole','abstract','claim_own') ORDER BY "
                    "CASE kind WHEN 'whole' THEN 0 WHEN 'abstract' THEN 1 ELSE 2 END LIMIT 1", (pid,))
        er = cur.fetchone()
        if not er:
            return jsonify({"pub": pub, "results": []})
        # Index-backed nearest-neighbour: ORDER BY <=> LIMIT uses the HNSW index (a GROUP BY min()
        # can't and full-scanned all 1.82M vectors → 90s+). Pull the nearest chunks, then dedup to
        # families in Python. (fixes the "more like this" hang.)
        qv = er["embedding"]
        cur.execute(
            "SELECT p.publication_number, p.title, p.country, (c.embedding <=> %s) AS d "
            "FROM chunks c JOIN publications p ON p.id=c.publication_id "
            "WHERE c.embedding IS NOT NULL AND p.id <> %s "
            "ORDER BY c.embedding <=> %s LIMIT 200",
            (qv, pid, qv))
        best = {}
        for r in cur.fetchall():
            k = r["publication_number"]
            d = float(r["d"])
            if k not in best or d < best[k]["d"]:
                best[k] = {"pub": k, "title": r["title"], "country": r["country"], "d": d}
        # `<=>` is pgvector cosine DISTANCE, so 1-d is a genuine cosine similarity in [0,1] — the
        # numbers here are already normalised, not saturated. A score of 1.000 is real and means
        # what it says: an embedding-identical document. In practice that is a family member with
        # the same text (typically the A1 pre-grant publication of the very B2 being queried), so
        # flag those rather than presenting a bare, alarming-looking 1.0 as an ordinary ranking.
        res = [{"pub": v["pub"], "title": v["title"], "country": v["country"],
                "score": round(1 - v["d"], 3), "near_identical": v["d"] < 0.02}
               for v in sorted(best.values(), key=lambda x: x["d"])[:12]]
    return jsonify({"pub": pub, "results": res})


# ---- side-by-side compare ------------------------------------------------------------------
@app.route("/compare")
def compare():
    slug = request.args.get("slug", "")
    pubs = [p for p in request.args.get("pubs", "").split(",") if p.strip()][:3]
    if not valid_slug(slug):
        abort(400)
    rep = _load_report(slug)
    if not rep or not pubs:
        abort(400)
    q = rep.get("query", "")
    qv = embed.embed_query(q[:8000], 768) if q else None
    cols = []
    with db.cursor() as cur:
        for pub in pubs:
            cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
            row = cur.fetchone()
            pid = row["id"] if row else None
            b = webview.biblio(cur, pid) if pid else {"pub": pub}
            disp = enrich_display.enrich_for_display(pub)
            matched = webview.match_in_pub(cur, pid, qv) if (pid and qv is not None) else None
            # which elements this family covers (from report evidence)
            fam = b.get("family_id")
            covers = []
            for el, hits in rep.get("element_evidence", {}).items():
                if any(h.get("family") == fam for h in hits):
                    covers.append(el)
            img = None
            imgs = (disp or {}).get("images") or []
            if imgs:
                img = f"/figures/{pub}/{imgs[0]['file']}"
            cols.append({"pub": pub, "biblio": b, "img": img,
                         "matched": {"kind": (matched or {}).get("kind"),
                                     "coord": webview._coord_str((matched or {}).get("coord")),
                                     "text": (matched or {}).get("text", "")[:1000]} if matched else None,
                         "covers": covers, "n_images": len(imgs),
                         "google_patents": (disp or {}).get("google_patents")})
    return render_template("compare.html", slug=slug, query=q, mode=rep.get("mode"),
                           elements=rep.get("elements", []), cols=cols)


@app.route("/api/chart/<pub>")
def api_chart(pub):
    """Element-by-element claim chart for one reference. Synchronous Vertex call, so this is a
    lazy per-card endpoint (like /api/ref), never part of the main search path."""
    if not _safe_pub(pub):
        abort(404)
    slug = request.args.get("slug", "")
    if not valid_slug(slug):        # reaches `RATIONALE / f"chart__{slug}__{pub}.json"` below
        return jsonify({"error": "invalid slug"}), 400
    rep = _load_report(slug) or {}
    elements = rep.get("elements", [])
    if not elements:
        return jsonify({"error": "no elements for this report", "slug": slug}), 400
    cp = RATIONALE / f"chart__{slug}__{pub}.json"
    if cp.exists():
        try:
            return jsonify(json.loads(cp.read_text()))
        except Exception:
            pass
    out = claim_chart.build_chart(elements, pub)
    try:
        cp.write_text(json.dumps(out, default=str))
    except Exception:
        pass
    return jsonify(out)


@app.route("/api/translate/<pub>")
def api_translate(pub):
    """On-demand English translation of a non-English reference. Chunked + SHA1-cached in
    translate.py; English text short-circuits without an LLM call."""
    if not _safe_pub(pub):
        abort(404)
    return jsonify(translate.translate_publication(pub))


@app.route("/api/modes")
def api_modes():
    """Capabilities, so the UI can build its mode picker from truth instead of a hard-coded list.
    INVALIDITY and FTO report as unavailable with the reason, rather than silently degrading."""
    return jsonify({"modes": available_modes()})


@app.route("/api/federation/health")
def api_federation_health():
    return jsonify(federation.health())


@app.route("/healthz")
def healthz():
    """Unauthenticated on purpose so external monitoring keeps working."""
    h = {"ok": True, "gold": len(_GOLD)}
    if auth.run_gate:
        h["runs"] = auth.run_gate.stats()
    return jsonify(h)


# ---- auth + rate limiting (registered LAST, after every route exists) ------------------------
auth.init_app(app, state_path=DATA / "run_budget.json")


if __name__ == "__main__":
    # DEVELOPMENT ONLY. Production is gunicorn (see gunicorn_conf.py + the supervisor unit):
    #     gunicorn -c gunicorn_conf.py webapp:app
    # The Werkzeug server that used to run here is single-process, has no request timeouts and is
    # explicitly not for production use.
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8631
    host = os.environ.get("WEBAPP_HOST", "127.0.0.1")
    print(f"[dev server — production uses gunicorn] http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, debug=False)
