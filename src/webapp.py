"""Results page — Flask app (spec Milestone 2). Serves on 127.0.0.1:8631.

Reuses Retriever + CoverageAgent + the DB + enrich_display. Element×Reference claim chart,
ranked prior-art cards with drawings/PDF/highlighted sections, coverage ledger.

Report generation (CoverageAgent.run) is slow, so it runs in a background thread with a poll
endpoint; the agent report is cached to data/reports/<slug>.json and never blocks the request.
Per-card drawings/PDF/sections/rationale are enriched lazily via /api/ref.
"""
from __future__ import annotations
import difflib, json, os, re, queue, secrets, threading, hashlib, time, traceback
from pathlib import Path
from flask import (Flask, Response, render_template, request, jsonify, redirect, url_for,
                   send_from_directory, send_file, abort, stream_with_context, make_response, session)
import db, embed, goldset, webview, enrich_display, llm
import pubnorm  # single link-builder: zero-padded Google/Espacenet URLs (dropped-zero fix)
import ops_family, prefetch                        # worldwide family timeline + top-N proactive enrich
import query_claim_grid                            # uploaded-claim x ranked-reference background grid
import deep_analysis                               # full-text agentic reading of the top references
import disclosures                                # the checklist a search is argued against
import failclosed                                  # degrade loudly, or not at all
import manifest                                    # immutable record of what produced a run
import trace                                       # one row per candidate, one terminal stage
import deep_rank                                   # screen wide, read deep, rank on the evidence
import query_set                                   # many short queries instead of one long brief
import report_archive                              # automatic top-50 full-text Markdown ZIP
import export_data, export_pdf, export_docx, export_xlsx, export_md, export_ids
import public_report                             # public link, password gate, visitor log
import submission_compliance                     # what must be true before a 1.290 paper ships
import concise_md                                # the editable form of a 1.290 paper
import deliverables                                # letterhead / matter / narrative + share links
import library                                     # saved publications, across searches
#  NOT `import figures`: this module already defines a route function called `figures`
#  (the reference-drawing file server at /figures/<pub>/<name>), and the import was
#  shadowed by it at definition time — the app booted straight into
#  "'function' object has no attribute 'ensure_schema'".
import draft_figures                               # model-generated patent drawings for a draft
import auth, accounts, notifications, rerank_pool
import run_queue                                   # gate-full searches wait in Postgres, not bounce
import drafting, draft_export, draft_worker
#  Phase two: the drafting CONVERSATION. A Claude Code agent edits a workspace of files, a second
#  agent reviews every iteration, and draft_uspto answers "can this be filed".
import draft_studio, draft_studio_service, draft_uspto, draft_workspace
import figure_compiler, figure_compiler_service       # deterministic filing-drawing compiler
import claim_chart, translate, drawings          # ported per-card enrichment
import ingest_input                                # front-door document / patent-link -> search brief
import grounding                                  # length-stable quote grounding (shared w/ claim_chart)
import corpus_facts                               # live corpus scope/currency for the disclosures
import disclosure                                 # shared disclosure wording (web + print + PDF + DOCX)
import federation, domain_detect                 # two-tier search + out-of-domain guard
import external                                    # parallel keyword+semantic fan-out, raw hits
import retrieval                                  # search_doc_chunks (parallel doc-chunk channel)
import img_search                                 # patent-drawing image-similarity channel
import rerank_listwise                            # listwise agentic reranker (in-context, several at a time)
from search_modes import require_available, ModeNotAvailable, available_modes
from retrieval import Retriever
from agent import CoverageAgent, AgentConfig
from config import DATA, ROOT
import base64, uuid
import concurrent.futures as _cf

app = Flask(__name__, template_folder="../templates", static_folder="../static")


def _asset_version():
    """Content-derived cache key for browser assets.

    The public proxy deliberately caches static files.  An unversioned ``style.css`` left an
    already-open browser on the pre-deploy mobile layout even though the new CSS was live on the
    server.  Hashing both shared assets once at process start makes every deploy select the exact
    matching CSS/JS without disabling useful caching.
    """
    override = os.environ.get("PATENT_STATIC_VERSION", "").strip()
    if override:
        return override
    digest = hashlib.sha256()
    for name in ("style.css", "app.js", "draft_studio.js"):
        path = Path(app.static_folder) / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(name.encode("utf-8"))
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()

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
# TWO INSTANCES, ONE DOMAIN, ONE COOKIE NAME = MUTUAL LOGOUT. The fable bench and production
# both live under rotem.ai; each checkout has its own .secret_key, and Flask's default cookie
# ("session", path "/") meant every request to one instance overwrote a cookie the other could
# not verify — with both tabs open the user was signed out of both mid-run. Each instance must
# therefore own a distinctly NAMED cookie, path-scoped to its prefix. Defaults unchanged, so
# production behavior is byte-identical unless the env says otherwise.
_cookie_name = os.environ.get("SESSION_COOKIE_NAME", "").strip()
if _cookie_name:
    app.config["SESSION_COOKIE_NAME"] = _cookie_name
_cookie_path = os.environ.get("SESSION_COOKIE_PATH", "").strip()
if _cookie_path:
    app.config["SESSION_COOKIE_PATH"] = _cookie_path
# Hard ceiling on any request body. The document-upload route caps at 30 MB itself; this is a
# belt-and-braces WSGI-level guard so an oversized body is refused (413) before it is buffered.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", str(32 * 1024 * 1024)))
# The session cookie carries the whole auth decision, so it must never travel in cleartext. Public
# access is HTTPS-only (nginx terminates TLS on rotem.ai and proxies here over the VPC), so `Secure`
# costs nothing there. Note Flask sets this flag from app.config alone — it does NOT sniff the
# request scheme — so the http:// hop between nginx and gunicorn does not suppress the flag and
# login keeps working through the proxy. Direct plain-HTTP access to 127.0.0.1:8631 is the one case
# a Secure cookie would not stick, so leave an escape hatch for local/dev use.
app.config["SESSION_COOKIE_SECURE"] = (os.environ.get("SESSION_COOKIE_SECURE", "1").strip().lower()
                                       not in ("0", "false", "no"))

_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "frame-src 'self' https:",
    "frame-ancestors 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
))


@app.after_request
def _browser_security_headers(response):
    """Apply the browser boundary in the app so errors and direct service traffic get it too."""
    response.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response

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
    root exactly as before (127.0.0.1:8631), so nothing changes for direct/local access.

    TRAP, and it has bitten once: our nginx uses `proxy_pass http://host:8631/` with a TRAILING
    SLASH, which already strips `/patents/` before the request arrives. The defensive strip below
    then fires a SECOND time on any route whose own path begins with the prefix — a route at
    `/patents` arrived as PATH_INFO `/patents`, was stripped to `""`, and silently served the
    index instead. Do not name a route after the mount prefix; the saved-publication library is
    at `/library` for exactly this reason."""

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


@app.errorhandler(404)
def page_not_found(_error):
    """Keep browser dead ends inside the product while preserving JSON API contracts."""
    wants_json = (request.path.startswith(("/api/", "/status/", "/events/")) or
                  "application/json" in request.headers.get("Accept", ""))
    if wants_json:
        return jsonify({"error": "not found"}), 404
    return render_template("error404.html", missing_path=request.path), 404


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
        f = corpus_facts.facts()
        # Built once here so the web scope block, /print, /about and the exported PDF/DOCX/XLSX/MD
        # all state the SAME jurisdiction coverage, derived from the corpus rather than written
        # into each surface by hand.
        return {"corpus": f, "disc": disclosure, "asset_version": ASSET_VERSION,
                "juris_sentence": disclosure._juris_sentence(f)}
    except Exception:
        # The disclosure must never be the reason a page fails to render.
        return {"corpus": {}, "asset_version": ASSET_VERSION, "juris_sentence": ""}

REPORTS = DATA / "reports"
RATIONALE = DATA / "rationale"
EXPORTS = DATA / "reports" / "exports"
FLAGS = DATA / "reports"
#  Uploaded report logos, materialised from the database so reportlab and python-docx have a path
#  to embed. The database row is authoritative; these are a cache and are safe to delete.
LOGOS = DATA / "reports" / "logos"
#  Model-generated patent figures for a draft, one PNG per version.
DRAWINGS = DATA / "draft_drawings"
REPORTS.mkdir(parents=True, exist_ok=True)
RATIONALE.mkdir(parents=True, exist_ok=True)
EXPORTS.mkdir(parents=True, exist_ok=True)
LOGOS.mkdir(parents=True, exist_ok=True)
DRAWINGS.mkdir(parents=True, exist_ok=True)

#  The four export shapes, in one table so /export, the export bar and the tests cannot disagree
#  about which formats exist. They differ along exactly two axes:
#    drawings — resolve one local figure file per reference (PDF/DOCX/XLSX embed it; Markdown is
#               text-only, and skipping the resolve also skips any CDN fetch, so .md stays fast)
#    text     — attach every reference's FULL claims + description. Only Markdown wants this: in a
#               paginated document it is hundreds of pages, and the other three already quote the
#               single best-matching passage.
EXPORT_FORMATS = {
    "pdf":  {"render": lambda m, o: export_pdf.render(m, o),  "drawings": True,  "text": False,
             "mime": "application/pdf", "label": "PDF"},
    "docx": {"render": lambda m, o: export_docx.render(m, o), "drawings": True,  "text": False,
             "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "label": "Word"},
    "xlsx": {"render": lambda m, o: export_xlsx.render(m, o), "drawings": True,  "text": False,
             "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "label": "Excel"},
    #  Bare "text/markdown": Flask appends the charset itself for text/* types, so spelling it out
    #  here produced a doubled "charset=utf-8; charset=utf-8" header.
    "md":   {"render": lambda m, o: export_md.render(m, o),   "drawings": False, "text": True,
             "mime": "text/markdown", "label": "Markdown"},
    #  The citation listing for a USPTO Information Disclosure Statement. Needs no drawings and no
    #  full text — only the bibliographic fields, which every reference already carries.
    "ids":  {"render": lambda m, o: export_ids.render(m, o),  "drawings": False, "text": False,
             "mime": "application/pdf", "label": "IDS (SB/08a)"},
}

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


def search_slug(query, mode, *, wide, search_focus, subject=None, doc_token=None, depth="deep"):
    """Stable cache identity for every input that can change retrieval/report content.

    `depth` joined ONLY when it is not "deep", so every pre-existing deep report keeps its slug —
    the same backward-compatibility rule the "|wide" marker follows."""
    parts = [query, mode, "wide" if wide else "narrow", search_focus,
             f"subject:{subject or '-'}", f"document:{doc_token or '-'}"]
    if depth != "deep":
        parts.append(f"depth:{depth}")
    return slugify("|".join(parts))


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
                if (not job or job.get("status") not in ("running", "partial")
                        or job.get("kind") != "reranking"):
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


#  Cheap, cached answers to "is the file on disk partial" and "what does the queue know about
#  this slug" — both consulted on every /status poll and every SSE event, so neither may cost a
#  full JSON parse or a DB round-trip each time. The partial check keys on mtime; the queue row
#  keys on a short TTL.
_PARTIAL_CACHE: dict = {}
_QROW_CACHE: dict = {}


def _report_partial(slug):
    p = report_path(slug)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None                                    # no file at all
    hit = _PARTIAL_CACHE.get(slug)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        partial = bool(json.loads(p.read_text()).get("partial"))
    except Exception:
        partial = True                                 # unreadable = not a finished report
    _PARTIAL_CACHE[slug] = (mt, partial)
    return partial


def _queue_row_cached(slug, ttl=5.0):
    hit = _QROW_CACHE.get(slug)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    row = run_queue.get_row(slug)
    _QROW_CACHE[slug] = (now, row)
    return row


def _job_event(slug, job):
    """The single wire shape shared by /status (poll) and /events (SSE), so the fallback path and
    the streaming path can never disagree.

    THE RELOAD LOOP THIS MUST NEVER RECREATE: a run killed by a restart leaves a PARTIAL report
    and no live job. Calling that state "done" made the report page reload onto the same partial
    page forever. A partial file with no job is "interrupted" (done=False), and when the queue
    holds the run it says so — restarting automatically, attempt N."""
    st = job.get("status", "unknown")
    exists = report_path(slug).exists()
    qrow = _queue_row_cached(slug)
    msg = job.get("msg", "")
    done = st == "done"
    if not job and exists:
        partial = _report_partial(slug)
        if not partial:
            done = True
        else:
            st = "interrupted"
            if qrow and qrow.get("state") in ("queued", "running"):
                msg = ("This search was interrupted by a server restart and is restarting "
                       f"automatically (attempt {int(qrow.get('attempts') or 0) + 1}). Reading "
                       "already banked in the evidence store is reused, so the restart is far "
                       "cheaper than the first pass. You can close this tab.")
            else:
                msg = ("This search was interrupted by a server restart and did not resume. "
                       "Use Re-run to start it again; reading already banked in the evidence "
                       "store is reused.")
    attempt = int((qrow or {}).get("attempts") or 0) or (1 if job else 0)
    t0_overall = (qrow or {}).get("t0_overall")
    elapsed_total = int(max(0.0, time.time() - float(t0_overall))) if t0_overall else None
    return {"kind": job.get("kind", "progress"), "slug": slug, "status": st,
            "msg": msg,
            "attempt": attempt,
            "elapsed_total_sec": elapsed_total,
            # Structured counterpart to `msg`. The progress UI needs the NUMBERS (elements found,
            # families seen, which round) to render a narrative rather than re-parsing prose out of
            # the message string, and to keep showing the last known state during the long silent
            # stretch between 'partial' and 'reranking'.
            "detail": job.get("detail") or {},
            #  LIVE COST AND CLOCK. A search runs for a long time and the page had no way to say
            #  how long or how much. `t0` is set where the job is created; the token figure is the
            #  process-wide counter differenced against its value at that moment, so it is an
            #  ESTIMATE and is labelled as one: concurrent searches share the same process counter
            #  and would each attribute the other's spend to themselves. It is right to within one
            #  other running search, which is the accuracy the number is used at.
            "elapsed_sec": int(max(0.0, time.time() - float(job.get("t0") or 0))) if job.get("t0") else 0,
            "tokens": _job_tokens(job),
            "ready": exists and (st in ("done", "partial", "interrupted") or not job),
            "done": done}


def _job_tokens(job):
    """Prompt + completion tokens spent since this job started, or 0. Never raises."""
    base = job.get("tok0")
    if base is None:
        return 0
    try:
        import llm
        u = llm.process_usage()
        return max(0, (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)) - base)
    except Exception:
        return 0


def _tok_now():
    """The process-wide token counter, for use as a per-job baseline."""
    try:
        import llm
        u = llm.process_usage()
        return u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
    except Exception:
        return 0


def _write_report(slug, rep):
    report_path(slug).write_text(json.dumps(rep, default=str, indent=1))
    (REPORTS / f"{slug}.view.json").unlink(missing_ok=True)   # force the view to rebuild from this
    (REPORTS / f"{slug}.detail-preview.json").unlink(missing_ok=True)
    # A rerun can reuse the same slug with a different uploaded document.  Never let its old
    # Claim x Reference analysis survive the source report it was built from.
    try:
        query_claim_grid.invalidate(slug, REPORTS)
    except Exception:
        traceback.print_exc()


def _run_job(slug, query, subject, mode, gated, wide=False, doc_token=None,
             search_focus="all_text", depth="deep"):
    """Thread entrypoint: run the generation, then always release the reserved budget slot.
    Kept separate from _generate so _generate's signature stays purely about doing the work."""
    try:
        # Only pass doc_token/depth when they deviate from the defaults, so callers/tests that
        # stub _generate with the pre-existing (slug, query, subject, mode, wide) signature keep
        # working for typed deep queries.
        extra = {} if depth == "deep" else {"depth": depth}
        if doc_token is None and search_focus == "all_text":
            # Preserve the historical call shape for adapters/tests that wrap _generate.
            _generate(slug, query, subject, mode, wide=wide, **extra)
        elif doc_token is None:
            _generate(slug, query, subject, mode, wide=wide, search_focus=search_focus, **extra)
        else:
            _generate(slug, query, subject, mode, wide=wide, doc_token=doc_token,
                      search_focus=search_focus, **extra)
    finally:
        if gated and auth.run_gate:
            auth.run_gate.end(depth=depth)
        run_queue.mark_finished(slug, ok=report_path(slug).exists())


# ---- document materials (multi-chunk + image channels) -------------------------------------
# A dropped file / patent link is turned by ingest_input into (a) a summary brief — still the
# text-channel query — PLUS (b) full-text chunks embedded at 768d and (c) the extracted drawings.
# /extract stashes (b)+(c) server-side keyed by a token and returns only the token; /run carries
# the token so the search can fan out over the document's own chunks and figures IN PARALLEL with
# the local text channels and the federated APIs. Stashing (rather than round-tripping vectors and
# base64 images through the browser) keeps the page weight unchanged.
DOCSTASH = REPORTS
# per-chunk retrieval weight by kind: independent claims / abstract / whole carry the invention;
# dependent claims and paragraphs are supporting; figure captions weakest.
_CHUNK_KIND_W = {"claim_own": 0.70, "claim_resolved": 0.70, "abstract": 0.90, "whole": 0.85,
                 "paragraph": 0.50, "figure_caption": 0.40}


def _chunk_weight(c):
    w = _CHUNK_KIND_W.get(c.get("kind"), 0.6)
    if c.get("independent"):
        w = max(w, 1.0)                       # an independent claim is the strongest single signal
    return w


def _stash_doc(res):
    """Persist the extract result's search materials (chunk vectors + drawing blobs) under a fresh
    token; return the token (or None when there is nothing extra to search). Small JSON on the
    214 GB-free disk — vectors + base64 drawings + the uploaded claim rows, not the whole extract
    payload.  Claims are retained even when embedding failed, because the asynchronous
    Claim x Reference grid can still compare their text with the ranked references."""
    try:
        chunks = res.get("chunks") or []
        vecs, weights = [], []
        for c in chunks:
            v = c.get("vector")
            if v:
                vecs.append(v)
                weights.append(_chunk_weight(c))
        figs = [im.get("b64") for im in (res.get("figure_images") or []) if im.get("b64")]
        full_text = ""
        if res.get("source") == "upload":
            full_text = str(res.get("full_text") or "")[:drafting.MAX_DISCLOSURE_CHARS].strip()
        claims = []
        # Claims are stashed whichever way the document arrived. They are a few kilobytes, and
        # the review panel lets a user hand-correct the claim set of a LINKED publication too —
        # that correction has to survive the re-stash. Which searches get the report-side claim
        # grid is decided separately, by _attach_query_document.
        if True:
            for i, c in enumerate(chunks):
                if c.get("kind") != "claim_own" or not (c.get("text") or "").strip():
                    continue
                coord = c.get("coord") if isinstance(c.get("coord"), dict) else {}
                claims.append({
                    "claim_no": coord.get("claim_no") or i + 1,
                    "text": str(c.get("text"))[:8000],
                    "independent": bool(c.get("independent")),
                })
        if not vecs and not figs and not claims and not full_text:
            return None
        token = uuid.uuid4().hex
        (DOCSTASH / f"doc-{token}.json").write_text(json.dumps(
            {"chunk_vecs": vecs, "chunk_weights": weights, "figure_b64": figs,
             "claims": claims, "source": res.get("source"), "label": res.get("label"),
             "title": res.get("title"), "full_text": full_text,
             #  The uploaded document's own publication number, when it identified itself. The
             #  analysis needs it to avoid charting that patent against its own claims.
             "publication_number": res.get("publication_number") or res.get("pub") or "",
             "n_chunks": len(vecs), "n_figs": len(figs), "n_claims": len(claims),
             "t": time.time()}))
        return token
    except Exception:
        traceback.print_exc()
        return None


def _load_doc_materials(token):
    """Load stashed retrieval materials plus uploaded-claim metadata, or return None."""
    if not token:
        return None
    p = DOCSTASH / f"doc-{re.sub(r'[^0-9a-f]', '', str(token))[:64]}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    blobs = []
    for b in d.get("figure_b64") or []:
        try:
            blobs.append(base64.b64decode(b))
        except Exception:
            continue
    return {"chunk_vecs": d.get("chunk_vecs") or [], "chunk_weights": d.get("chunk_weights") or [],
            "figure_blobs": blobs, "claims": d.get("claims") or [],
            "source": d.get("source"), "label": d.get("label"), "title": d.get("title"),
            "publication_number": d.get("publication_number") or "",
            "full_text": str(d.get("full_text") or "")[:drafting.MAX_DISCLOSURE_CHARS]}


def _attach_query_document(report, doc):
    """Record the claims of the document the search STARTED FROM, however it arrived.

    A LINK COUNTS. This used to require source == "upload", and that single condition is why a
    search started from a patent LINK produced no claim mapping at all. `query_document` is the
    only place the reading stage looks for the subject's claims (deep_rank.run reads
    report["query_document"]["claims"] and passes them to every reader), so a linked patent's
    claims were extracted, stashed, embedded and used for RETRIEVAL — and then never put to a
    single reference. The claim table came back empty and the report read as though no prior art
    disclosed any of them, which is the opposite of "nobody looked".

    The claims are the same claims whichever way the document arrived; `_stash_doc` already says
    so and keeps them for both. Only `disclosure_text` differs, because the full text is stashed
    for an upload alone, and everything downstream already treats it as optional.

    The narrower Claim x Reference grid (query_claim_grid) keeps its own upload-only gate: it is
    capped at eight references and the full-text reading grid supersedes it.
    """
    if not doc or not (doc.get("claims") or (doc.get("full_text") or "").strip()):
        return report
    report["query_document"] = {
        "source": doc.get("source") or "upload",
        "label": doc.get("label") or "uploaded patent",
        "publication_number": doc.get("publication_number") or "",
        "title": doc.get("title"),
        "disclosure_text": str(doc.get("full_text") or "")[:drafting.MAX_DISCLOSURE_CHARS],
        "claims": (doc.get("claims") or [])[:60],
        "n_claims": len(doc.get("claims") or []),
    }
    return report


def _image_channel(figure_blobs, k=15):
    """Image-similarity channel: embed the query drawings and match the corpus figure index.
    Returns {"families": [(family_key, pid, score)], "hits": [...], "state": "used|none|failed",
    "note": str}. Never raises — a loud img_search error becomes a failed-source tag, not a crash
    and not a silent []."""
    out = {"families": [], "hits": [], "state": "off", "note": ""}
    blobs = [b for b in (figure_blobs or []) if b]
    if not blobs:
        return out
    try:
        hits = img_search.search_by_images(blobs, k=k)
        fam = getattr(retriever(), "_fam", {}) or {}
        seen, fams = set(), []
        for h in hits:
            pid = h.get("publication_id")
            fk = fam.get(pid, str(h.get("publication_number") or pid))
            if fk in seen:
                continue
            seen.add(fk)
            fams.append((fk, pid, float(h.get("score") or 0)))
        out["families"] = fams
        out["hits"] = hits
        out["state"] = "used" if fams else "none"
    except img_search.ImageIndexEmpty as e:
        out["state"] = "none"
        out["note"] = "image index not built yet"
    except Exception as e:
        traceback.print_exc()
        out["state"] = "failed"
        out["note"] = str(e)[:160]
    return out


def _dedup_preserve(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


#  How many UNIQUE finds a parallel channel may splice into the ranking. This was 8, which threw
#  away 192 of the 200 families the document-chunk channel found and 6 of the 14 the image channel
#  found; on the case that prompted the rebuild the image channel was the ONLY channel that
#  surfaced a reference the searcher named. There is no reason to cap tightly now that every
#  spliced candidate is screened and, if it survives, read in full (deep_rank).
MERGE_TAKE = int(os.environ.get("MERGE_TAKE", "60"))


def order_cards_by_evidence(cards):
    """The authoritative report order once the references have been read (deep_rank).

    Primary key is whether the reference was READ IN FULL, not the number. A candidate judged from
    an abstract cannot be compared like-for-like with one whose full text was quoted, and capping
    its score is not enough: measured on a live run, three federated-only hits sitting at the cap
    took the top three slots ahead of a reference that grounded 9 of 12 features with verbatim,
    located quotes. Evidence outranks a guess.

    Ties fall back to the incoming order, so this is a deterministic permutation of the input and
    nothing is dropped. Ranks are renumbered 1..N.
    """
    incoming = {id(c): i for i, c in enumerate(cards)}
    out = sorted(cards, key=lambda c: (0 if c.get("deep_read") else 1,
                                       -(c.get("deep_score") or 0), incoming[id(c)]))
    for i, c in enumerate(out, 1):
        c["rank"] = i
    return out


def _merge_channel(rep, name, scored, head_keep=12, take=None):
    """Record a NEW parallel channel's families on the report and splice its best UNIQUE finds into
    the ranked list just below the established local head, so a document-chunk-only or image-only
    match enters the display window and is judged by the listwise reranker alongside everything
    else. Does NOT reorder the local ranking; the listwise pass does the final ordering."""
    if not scored:
        return
    take = MERGE_TAKE if take is None else take
    fams = [fk for fk, _, _ in scored]
    rep.setdefault("channel_families", {})[name] = sorted(set(fams))
    ranked = list(rep.get("ranked_families") or [])
    have = set(ranked)
    fresh = [fk for fk in fams if fk not in have][:take]
    if fresh:
        merged = ranked[:head_keep] + fresh + ranked[head_keep:]
        rep["ranked_families"] = _dedup_preserve(merged)


def _attach_fed_family_sources(rep):
    """Cross-reference federated hits against the LOCAL corpus so a result found by both a local
    channel and an external API records BOTH. For each federated hit whose publication number
    resolves to a local family, record which APIs (PQAI / USPTO / SerpApi / …) returned it, keyed
    by family, in rep['family_sources']. Federated hits with no local row stay in the federation
    block (webview cannot render an external family as a card)."""
    fed = rep.get("federation") or {}
    hits = fed.get("hits") or []
    if not hits:
        return
    keys = [federation.join_key(h.get("pub")) for h in hits if h.get("pub")]
    try:
        resolved = retriever().resolve_pub_numbers(keys)   # {join_key: (pid, family_key)}
    except Exception:
        traceback.print_exc()
        return
    fam_sources = rep.setdefault("family_sources", {})
    for h in hits:
        jk = federation.join_key(h.get("pub") or "")
        r = resolved.get(jk)
        if not r:
            continue
        fk = r[1]
        srcs = fam_sources.setdefault(fk, [])
        for s in (h.get("sources") or []):
            if s not in srcs:
                srcs.append(s)


def _attach_prosecution(rep, slug=None):
    """Read the subject's US file wrapper onto the report. -> rep. Never raises.

    Stores `prosecution` = {"dossier", "mined"} and `prosecution_seeds`, the corpus publications an
    examiner applied or considered against this family. deep_rank reads the seeds and gives them a
    reading slot whatever the screen thought of them: a reference a USPTO examiner APPLIED in a
    rejection of substantially these claims does not have to win a similarity contest to be worth
    reading.
    """
    qd = (rep or {}).get("query_document") or {}
    pub = qd.get("publication_number") or rep.get("subject") or qd.get("label") or ""
    if not pub:
        return rep
    try:
        import prosecution
        got = prosecution.for_subject(pub)
    except Exception:
        traceback.print_exc()
        return rep
    rep["prosecution"] = got
    mined = got.get("mined") or {}
    rep["prosecution_seeds"] = list(mined.get("seeds") or [])
    note = prosecution.summarise(mined)
    if note:
        print(f"[prosecution {slug or ''}] {note}", flush=True)
    if rep["prosecution_seeds"] and slug:
        _set_job(slug, kind="screening",
                 msg=f"The USPTO file wrapper for this family names "
                     f"{len(rep['prosecution_seeds'])} references the Office already "
                     f"cited. Reading those too…")
    return rep


def _drop_self_family(rep):
    """Remove the searcher's OWN patent family from the results.

    Charting a patent against itself is meaningless, and the existing guard only excluded the
    exact publication number. A DOCDB simple family routinely runs to thirty members across a
    dozen offices, so the same invention came back as its own closest prior art under a different
    number: on a real search the #1 result was the US member of the uploaded EP patent's family,
    and it had been the #1 result on every previous run of that search too.

    The date filter already does this when the searcher types a subject publication number. This
    covers the case that matters more in practice: a document was uploaded and identified itself,
    so we know the family without being told.
    """
    qd = (rep or {}).get("query_document") or {}
    self_pub = qd.get("publication_number") or rep.get("subject")
    if not self_pub:
        return rep
    key = deep_analysis._norm_pub(self_pub)
    if not key:
        return rep
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(NULLIF(simple_family_id,''), publication_number) fam
                   FROM publications
                   WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) = ANY(%s)
                   LIMIT 1""",
                (sorted({key} | {re.sub(r"[^A-Z0-9]", "", v.upper())
                                 for v in pubnorm.variants(self_pub)}),))
            row = cur.fetchone()
    except Exception:
        traceback.print_exc()
        return rep
    if not row or not row["fam"]:
        return rep
    fam = row["fam"]
    #  RECORDED WHETHER OR NOT ANYTHING WAS DROPPED. Filtering the list here only cleans what
    #  retrieval has produced SO FAR; claim_reach, the orphan rescue and the reading top-up all
    #  search again afterwards, and deep_rank rewrites `ranked_families` from what it charted.
    #  The fact that must survive all of that is the family id, so the later stages can enforce it.
    rep["self_family"] = fam
    before = rep.get("ranked_families") or []
    rep["ranked_families"] = [f for f in before if f != fam]
    dropped = len(before) - len(rep["ranked_families"])
    if dropped:
        rep["self_family_excluded"] = {"publication": self_pub, "family": fam}
        print(f"[self-family] excluded {self_pub} family {fam} from the results", flush=True)
    return rep


def _generate(slug, query, subject, mode, wide=False, doc_token=None,
              search_focus="all_text", depth="deep"):
    """Run one report. Runs fully concurrently with other generations — the only serialized step is
    the cross-encoder, which lives in its own child process (rerank_pool).

    depth="quick" is the interactive tier: one retrieval round, no external fan-out (the caller
    already forces wide=False), and deep_rank stops after screen + per-limitation batch tail —
    no full reads, no refuter tail, no rescue. Everything else is byte-identical to deep."""
    _set_job(slug, status="running", msg="Queued…", t0=time.time(), tok0=_tok_now())
    #  IMMUTABLE RUN MANIFEST, written BEFORE anything happens. No comparison between two runs is
    #  valid unless they shared a corpus snapshot, a commit, the same prompts and the same budgets,
    #  and nothing recorded that: runs taken hours apart, with the corpus being written to and
    #  constants being edited between them, were compared as though only the treatment had moved.
    #  Written first so a run that dies stays "running" and can never be reported as complete.
    run_id = f"{slug}-{int(time.time())}"
    failclosed.reset()
    run_manifest = None
    try:
        import replay as _replay
        run_manifest = manifest.start(
            run_id, slug=slug, subject_id=benchmark_subject_id(slug) or "",
            mode=mode, wide=bool(wide), search_focus=search_focus,
            doc_token=bool(doc_token),
            #  Which external world this run saw. manifest.comparable() refuses to compare two
            #  arms whose replay state differs, or either of which ran with it off. Re-applied
            #  after a concurrent session's edit reverted it; a control/treatment comparison is
            #  not interpretable without it.
            replay=_replay.stats("bulk_search"))
    except Exception:
        traceback.print_exc()
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
            elif stage in ("search_progress", "seed_progress", "round_progress"):
                done, maximum = data["search_done"], data["search_max"]
                phase = {
                    "search_progress": "Initial whole-invention search",
                    "seed_progress": "Element expansion",
                    "round_progress": f"Refinement round {data.get('round', '')}".strip(),
                }[stage]
                _set_job(slug, kind=stage, detail=data,
                         msg=f"{phase}: {done} of up to {maximum} retrieval passes complete "
                             f"({data['families']} families; last pass {data['search_seconds']:.1f}s).")
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
                _start_stage_heartbeat(slug, retrieval.RERANK_TOP)
            elif stage == "rerank_progress":
                # Only fires when RERANK_CHUNK is enabled; real per-item counts beat a heartbeat.
                done, total = data["done"], data["total"]
                if done < total:
                    _set_job(slug, kind=stage,
                             detail={"done": done, "total": total, "families": data["families"]},
                             msg=f"Scoring the closest {total} references against your claim "
                                 f"elements — {done} of {total}…")

        # ---- PARALLEL MULTI-CHANNEL FAN-OUT (spec item 3) ----------------------------------
        # These channels are independent and each opens its own DB connection / hits its own
        # service, so they run CONCURRENTLY, not one after another:
        #   (a) local: CoverageAgent.run — the 8 local text channels + the agentic loop, driven
        #       by the summary/brief (this is also the longest stage; it streams the partial and
        #       owns the cross-encoder head).
        #   (b) federated: the external patent APIs (SerpApi/PQAI/USPTO/EPO OPS/Lens/…).
        #   (c) docchunks: multi-chunk semantic search — each strong chunk of the query document
        #       retrieves its own corpus neighbours, pooled (retrieval.search_doc_chunks).
        #   (d) image: the query document's drawings matched against the corpus figure index.
        # A typed (no-document) query simply has no (c)/(d); (a) and (b) still run in parallel.
        doc = _load_doc_materials(doc_token)
        #  A LINK OR UPLOAD SEARCH NAMES NO SUBJECT, so until now it ran with no date cutoff and
        #  no self-exclusion at all. Both are wrong, and measurably so: on the EP 3 707 092
        #  benchmark the subject's OWN family came back at rank 1 of its own results, and because
        #  retrieval.channel_citation_family expands the backward citations of whatever it
        #  retrieves, that family's citation list -- the examiner's answer key -- was expanded
        #  straight into the candidate pool. The document also has a priority date, so without it
        #  art published AFTER the invention was eligible to be returned as prior art against it.
        #
        #  Recovering the subject from the ingested document fixes all three at once, because
        #  retrieval._date_clause consumes both the date and the number.
        if subject is None and (doc or {}).get("publication_number"):
            try:
                subject = external.subject_from_doc(doc["publication_number"])
                if subject is not None:
                    print(f"[subject] recovered from the ingested document: "
                          f"{subject.number or doc['publication_number']} efd={subject.efd}",
                          flush=True)
            except Exception:
                traceback.print_exc()
        parallel = ["local"] + (["federated", "external"] if wide else [])
        if doc and doc.get("chunk_vecs"):
            parallel.append("docchunks")
        if doc and doc.get("figure_blobs"):
            parallel.append("image")
        _set_job(slug, kind="fanout", detail={"channels": parallel},
                 msg="Searching in parallel: local corpus (8 channels)" +
                     (", external patent APIs" if wide else "") +
                     (", document chunks" if "docchunks" in parallel else "") +
                     (", figure images" if "image" in parallel else "") + "…")

        timing = {}   # channel -> {"start", "end"} wall-clock — proves overlap, logged below

        def _timed(name, fn, *a, **kw):
            timing[name] = {"start": time.time()}
            try:
                return fn(*a, **kw)
            finally:
                timing[name]["end"] = time.time()

        # read-only across threads (not mutated during a search); only needed by the doc channels
        fam_map = {}
        if "docchunks" in parallel or "image" in parallel:
            fam_map = getattr(retriever(), "_fam", {}) or {}
        with _cf.ThreadPoolExecutor(max_workers=len(parallel)) as ex:
            futs = {}
            futs["local"] = ex.submit(
                _timed, "local", A.run, query, subject=subject, mode=mode,
                #  Quick tier: ONE retrieval round. The second round's marginal families arrive
                #  minutes later and feed a reading depth the quick tier does not have; the deep
                #  escalation re-runs with the full two rounds.
                cfg=AgentConfig(mode=mode, max_rounds=(1 if depth == "quick" else 2),
                                elements_per_round=3, ground=True,
                                search_config=("claim_agentic" if search_focus == "claims"
                                               else "agentic"),
                                input_claims=list((doc or {}).get("claims") or [])),
                on_event=on_event)
            if wide:
                #  PHASE 2a OF THE REBUILD: the App A /api/search channel is OFF by default.
                #  Measured (2026-08-18 study): it ran App A's whole planner+cascade per call —
                #  including 100 gemini-2.5-pro full-document reads, $2.34/call, 408-545s of every
                #  fan-out — and its judged shortlist landed in a display-only side block while
                #  its candidates re-entered anyway through the raw /api/bulk_search channel
                #  below, which B screens and reads itself. Deleting the duplicate reader is the
                #  single biggest per-search saving in the rebuild. FEDERATION_CHANNEL=1 restores
                #  it exactly, for an A/B or if bulk reach ever measures short.
                if os.environ.get("FEDERATION_CHANNEL", "0") != "0":
                    futs["federated"] = ex.submit(_timed, "federated", _federate_block,
                                                  query, mode)
                futs["external"] = ex.submit(_timed, "external", _external_block, query, doc)
            if "docchunks" in parallel:
                futs["docchunks"] = ex.submit(
                    _timed, "docchunks", retrieval.search_doc_chunks,
                    doc["chunk_vecs"], doc["chunk_weights"], fam_map, subject, mode)
            if "image" in parallel:
                futs["image"] = ex.submit(_timed, "image", _image_channel, doc["figure_blobs"])

            rep = futs["local"].result()      # the report backbone (raises if the agent failed)
            # The cross-encoder is complete once the local future resolves. Stop its heartbeat
            # immediately; otherwise a slower external API fan-out leaves the page claiming it is
            # still reranking. Name that wait explicitly so the operator can see the real hold-up.
            _stop_stage_heartbeat(slug)
            if "federated" in futs and not futs["federated"].done():
                _set_job(slug, kind="federating", detail={"local_done": True},
                         msg="Local ranking is ready — waiting for the wider patent APIs…")
            fed = futs["federated"].result() if "federated" in futs else None
            ext = futs["external"].result() if "external" in futs else None
            doc_fams = futs["docchunks"].result() if "docchunks" in futs else []
            img_res = futs["image"].result() if "image" in futs else None

        _stop_stage_heartbeat(slug)      # idempotent: also covers a no-local-results edge case
        # Log the wall-clock windows so the parallelism is verifiable in the service log.
        t0 = min((v["start"] for v in timing.values()), default=time.time())
        for nm, v in sorted(timing.items(), key=lambda kv: kv[1]["start"]):
            print(f"[fanout {slug}] {nm}: {v['start']-t0:6.2f}s .. {v.get('end', v['start'])-t0:6.2f}s "
                  f"({v.get('end', v['start'])-v['start']:.2f}s)", flush=True)

        rep["partial"] = False
        rep["search_focus"] = search_focus
        rep["domain"] = verdict.to_dict() if verdict is not None else None
        if wide:
            rep["federation"] = fed
            _attach_fed_family_sources(rep)   # per-result API provenance for local overlaps
        else:
            rep["federation_offered"] = bool(verdict is not None and verdict.should_federate)
        # Merge the new parallel channels: record their families and splice their best UNIQUE
        # finds into the display+rerank window (dedup by family reuses the shared family map).
        #  EXTERNAL ART, spliced into the ranked list rather than parked beside it. This is the
        #  difference between "an API mentioned it" and "the pipeline read it": ranked_families is
        #  what the screen, the reader and the claim charter consume, so a reference that is not in
        #  it cannot be judged, only listed. Quota'd, because every stage below is a fixed size.
        if ext and ext.get("families"):
            #  A link/upload search names no subject, so nothing has bounded these by date. The
            #  local channels are unaffected either way (they filter in their own SQL); this only
            #  stops the new fan-out, which skews recent, from injecting art that POSTDATES the
            #  invention it is supposed to be prior art for.
            if subject is not None:
                rep["date_cutoff"] = str(getattr(subject, "efd", "") or "")
            keep = external.citable(ext["families"], subject, mode)
            if len(keep) != len(ext["families"]):
                print(f"[external] {len(ext['families']) - len(keep)} families dropped as not "
                      f"citable prior art under {mode}", flush=True)
            ext["families"], ext["n_families"] = keep, len(keep)
            #  Splice BELOW the retrieval head deep_rank always reads in full. Inserting at the
            #  usual position 12 would have handed 48 of those 60 guaranteed slots to candidates
            #  judged on a title and an abstract, displacing local references the corpus holds the
            #  full text for. External art has to earn its place through the screen like anything
            #  else; it does not get to walk into the read set.
            _merge_channel(rep, "external", keep, take=external.MERGE_FAMILIES,
                           head_keep=deep_rank.ALWAYS_CHART_RETRIEVAL_HEAD)
        if ext:
            rep["external"] = external.summary(ext)
        if doc_fams:
            _merge_channel(rep, "docchunks", doc_fams)
        if img_res and img_res.get("families"):
            _merge_channel(rep, "image", img_res["families"])
        if img_res:
            rep["image_channel"] = {"state": img_res.get("state"), "note": img_res.get("note"),
                                    "n": len(img_res.get("families") or [])}
        _attach_query_document(rep, doc)
        _attach_disclosures(rep, doc, subject, slug=slug)
        if _ORACLE_PLAN:
            rep["_oracle"] = dict(_ORACLE_PLAN)
        _drop_self_family(rep)

        # ---- WHAT THE OFFICE ALREADY DECIDED (prosecution) --------------------------------
        # Runs before the reading, because what it finds changes what gets read. A US application
        # under examination has a file wrapper, and the wrapper holds the examiner's own
        # rejections and the reference lists a professional who read the application chose. On
        # US 2025/0033224 A1 that is US 11,413,727 applied under 102(a)(2) to thirteen claims,
        # plus every one of the five references counsel independently filed. Fail-soft and off
        # without a key: an unreachable USPTO costs its own findings and nothing else.
        _attach_prosecution(rep, slug=slug)

        # ---- SCREEN WIDE, READ DEEP, RANK ON THE EVIDENCE (deep_rank) ----------------------
        # This is the stage that decides the order of the report. Retrieval hands over a couple of
        # thousand ranked families; this screens the head of that list cheaply, reads the survivors
        # IN FULL, and ranks by what each one was measured to disclose with a grounded, located,
        # refuter-survived quote. It replaces an LLM score computed from a 900-character snippet,
        # which is what put a reference disclosing 10 of 12 features at rank 11 with a 45.
        # Never fatal: a failure here leaves the fusion order and the old listwise path in place.
        def _deep_event(stage, data):
            if stage == "screen_start":
                _set_job(slug, kind="screening", detail=data,
                         msg=f"Screening {data.get('n', 0)} candidate references against your "
                             f"invention…")
            elif stage == "screen_progress":
                _set_job(slug, kind="screening", detail=data,
                         msg=f"Screening candidates: batch {data.get('done')} of "
                             f"{data.get('total')}…")
            elif stage == "enrich_start":
                _set_job(slug, kind="enriching", detail=data,
                         msg=f"Fetching the full text of {data.get('n', 0)} references this "
                             f"corpus holds only an abstract for…")
            elif stage == "enrich_progress":
                _set_job(slug, kind="enriching", detail=data,
                         msg=f"Fetching missing full text: {data.get('done')} of "
                             f"{data.get('total')}…")
            elif stage == "chart_start":
                _set_job(slug, kind="reading", detail=data,
                         msg=f"Reading the {data.get('n', 0)} strongest references IN FULL and "
                             f"charting what each one discloses…")
            elif stage == "chart_progress":
                _set_job(slug, kind="reading", detail=data,
                         msg=f"Read {data.get('done')} of {data.get('total')} references in "
                             f"full…")
            #  The orphan-claim rescue (claim_rescue): a second, claim-driven search for the claims
            #  the whole run found nothing against. It runs after the reading, so without these the
            #  page would sit on "Read 420 of 420" for several more minutes.
            elif stage == "rescue_start":
                _set_job(slug, kind="rescuing", detail=data,
                         msg=f"{data.get('n', 0)} claims have no prior art against them yet — "
                             f"going back for them: " +
                             ", ".join(data.get("claims") or [])[:120] + "…")
            elif stage == "rescue_reread_start":
                _set_job(slug, kind="rescuing", detail=data,
                         msg=f"Re-reading {data.get('n', 0)} references already read, asking only "
                             f"about those claims…")
            elif stage == "rescue_search_start":
                _set_job(slug, kind="rescuing", detail=data,
                         msg=f"Searching for those claims specifically: {data.get('n', 0)} "
                             f"queries, with no classification filter…")
            elif stage == "rescue_search_progress":
                _set_job(slug, kind="rescuing", detail=data,
                         msg=f"Claim-focused search: {data.get('done')} of {data.get('total')}…")
            elif stage == "batch_tail_start":
                _set_job(slug, kind="reading", detail=data,
                         msg=f"Evidence sweep: reading {data.get('n', 0)} more references "
                             f"against every requirement, in batches…")
            elif stage == "batch_read_progress":
                _set_job(slug, kind="reading", detail=data,
                         msg=f"Evidence sweep: batch {data.get('done')} of "
                             f"{data.get('total')}…")
            elif stage == "rescue_read_start":
                _set_job(slug, kind="rescuing", detail=data,
                         msg=f"Reading {data.get('n', 0)} references found for the uncovered "
                             f"claims…")
            elif stage == "rescue_read_progress":
                _set_job(slug, kind="rescuing", detail=data,
                         msg=f"Reading rescued references: {data.get('done')} of "
                             f"{data.get('total')}…")
        try:
            rep["depth"] = depth
            dr = deep_rank.run(rep, reports_dir=REPORTS, slug=slug, on_progress=_deep_event,
                               depth=depth)
            if dr:
                print(f"[deep_rank {slug}] screened {dr['screened']}/{dr['n_candidates']} in "
                      f"{dr['screen_seconds']}s, read {dr['read_in_full']} in full "
                      f"({dr['chars_read']:,} chars) in {dr['chart_seconds']}s", flush=True)
        except Exception:
            traceback.print_exc()

        rep["run_id"] = run_id
        rep["manifest"] = {"run_id": run_id,
                           "git_commit": (run_manifest or {}).get("git_commit"),
                           "disclosure_list_version": rep.get("disclosure_list_version"),
                           "disclosure_list_hash": rep.get("disclosure_list_hash")}
        _write_report(slug, rep)
        # Warm the view cache HERE (in the background job, where the user is already on the
        # progress page) so the listwise agentic rerank + claim-matrix verification run once and
        # the /report GET is instant instead of blocking ~40 s on first view.
        view = None
        try:
            _set_job(slug, kind="ranking",
                     msg="Ranking references against each other in context (listwise)…")
            view = _build_view_cached(slug, rep)
            # Persist tab-ready text and start bounded figure/family/rationale preparation HERE,
            # inside the durable search job. These must finish even when the user chose email and
            # closed the browser; client-side warming is only a latency optimization after this.
            _write_detail_preview(slug, view)
            final_pubs = [c.get("pub") for c in (view.get("cards") or []) if c.get("pub")]
            prefetch.prefetch_top(slug, final_pubs, n=len(final_pubs))
            _schedule_background_report_analysis(slug, final_pubs[:8])
            # Uploaded patent claims are analysed against the final ranked references on a
            # separate one-wide worker.  Scheduling is instant, so "Report ready" is never held
            # behind 8 grounded/refuted claim-chart passes.
            query_claim_grid.ensure(slug, rep, view, REPORTS)
            # Push the FINAL listwise order to any client still watching the progress stream, so
            # cards that arrived early in fusion order re-sort to the authoritative ranking before
            # (and independently of) the reload. Just the pub ids, in order — the client reorders
            # its already-rendered cards in place; a client that ignores the event keeps the
            # server-rendered order (deterministic fallback).
            _set_job(slug, kind="rank",
                     detail={"rank_order": [c.get("pub") for c in (view.get("cards") or []) if c.get("pub")]},
                     msg="Final ranking ready.")
        except Exception:
            traceback.print_exc()
        # Build the requested top-50 full-text Markdown archive proactively.  The single-wide
        # worker resolves text/sketch links while the user reads the report; a download click never
        # starts this expensive work.
        if "PYTEST_CURRENT_TEST" not in os.environ:
            try:
                report_archive.ensure(slug, rep, view or {}, REPORTS)
            except Exception:
                traceback.print_exc()
        #  CANDIDATE-STAGE TRACE. One row per candidate family with exactly one terminal stage, so
        #  "was this retrieved and dropped, or never retrieved?" is answerable from a finished run
        #  instead of by guesswork. Guesswork attributed the EP 3 707 092 failure to reach when the
        #  six-subject data later showed more was being lost to ranking.
        try:
            tracer = trace.from_report(rep, subject_id=benchmark_subject_id(slug) or "",
                                       slug=slug, view=view or {})
            path = tracer.write(str(REPORTS / f"{slug}.trace.jsonl"))
            unknown = tracer.unknown()
            print(f"[trace {slug}] {len(tracer.rows())} candidates -> {tracer.counts()}"
                  + (f"  UNKNOWN={len(unknown)} (pipeline defect)" if unknown else "")
                  + (f"  {path}" if path else ""), flush=True)
        except Exception:
            traceback.print_exc()
        _set_job(slug, kind="done", status="done", msg="done")
        #  Every degraded path taken during this run, on the manifest AND on the report, so a
        #  report produced with a source down or the reranker unavailable can be recognised as
        #  degraded afterwards instead of being read as a clean result.
        degraded = failclosed.summary()
        rep["degraded"] = degraded
        _write_report(slug, rep)
        manifest.finish(run_manifest, status="completed",
                        n_ranked_families=len(rep.get("ranked_families") or []),
                        n_displayed=len((view or {}).get("cards") or []),
                        degraded=degraded)
        if "PYTEST_CURRENT_TEST" not in os.environ:
            try:
                notifications.queue_search_completion(slug)
            except Exception:
                traceback.print_exc()
    except Exception as e:
        traceback.print_exc()
        manifest.finish(run_manifest, status="failed", failure_reason=f"{type(e).__name__}: {e}",
                        degraded=failclosed.summary())
        try:
            accounts.mark_search_failed(slug)
            #  Silence is the worst answer here: to the person waiting it is identical to a search
            #  still running, and identical to mail being broken.
            notifications.queue_search_failure(slug, reason=f"{type(e).__name__}: {e}")
        except Exception:
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


def _subject_description(subject, doc):
    """Description text for the subject: the uploaded document, else the corpus paragraphs.

    The claims say what was CLAIMED; the description says what was DISCLOSED, and the gap between
    them is where the potential claims live -- the ones a drafter could have written and did not,
    which is exactly the art a search goes blind to the moment the claims are amended."""
    full = str((doc or {}).get("full_text") or "").strip()
    if len(full) > 400:
        return full
    num = getattr(subject, "number", None) if subject is not None else None
    if not num:
        return ""
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT ch.text FROM chunks ch JOIN publications p
                             ON p.id = ch.publication_id
                           WHERE p.publication_number = %s AND ch.kind = 'paragraph'
                             AND ch.text IS NOT NULL ORDER BY ch.id LIMIT 150""", (num,))
            return "\n".join(r["text"] for r in cur.fetchall())
    except Exception:
        traceback.print_exc()
        return ""


#  Set by eval/run_one_oracle.py only. None in every other path, including production, so an
#  injected run cannot happen by accident: oracle.Oracle also requires its own flag and a
#  non-empty gold list before it will arm.
_ORACLE_PLAN = None

_BENCH_SLUG = re.compile(r"^bench-(.+?)-[^-]+(?:-(?:before_\w+|control))?$")


def benchmark_subject_id(slug):
    """The benchmark subject a slug belongs to, or None for an ordinary search.

    Benchmark reports are named `bench-<subject_id>-<tag>`. Deriving the id from the slug keeps
    the benchmark path from needing its own plumbing through _run_job and _generate, and means a
    benchmark run cannot forget to declare itself.
    """
    m = _BENCH_SLUG.match(str(slug or ""))
    return m.group(1) if m else None


def _attach_disclosures(rep, doc, subject, slug=None):
    """Build the checklist the search is argued against, and record it on the report.

    BENCHMARK RUNS LOAD A FROZEN LIST AND FAIL IF THERE IS NONE. The metric's denominator has to
    be fixed before the run: generating it during the run scores two runs of the same subject
    against two different checklists, and a retrieval change then moves the denominator underneath
    the numerator. Falling back to generation here would restore exactly that, silently, and the
    run would still produce a number.

    An ordinary search generates its list and is never fatal: with none, deep_rank falls back to
    the element summary, which is what it always used.
    """
    bench = benchmark_subject_id(slug)
    if bench:
        frozen = disclosures.load_frozen(bench)
        if not frozen:
            raise RuntimeError(
                f"benchmark run {slug}: no usable frozen disclosure list for subject "
                f"'{bench}'. Run eval/freeze_disclosures.py. Refusing to generate one, because "
                f"a denominator built during the run cannot be compared against any other run.")
        rep["disclosures"] = frozen["disclosures"]
        rep["disclosures_summary"] = frozen["summary"]
        rep["disclosure_list_version"] = frozen.get("disclosure_list_version")
        rep["disclosure_list_hash"] = frozen.get("content_hash")
        rep["disclosure_list_source"] = "frozen"
        print(f"[disclosures] FROZEN list for {bench}: {frozen['summary']} "
              f"v{frozen.get('disclosure_list_version')} {frozen.get('content_hash')}",
              flush=True)
        return
    try:
        claims = list((doc or {}).get("claims") or [])
        desc = _subject_description(subject, doc)
        if not claims and len(desc) < 400:
            return
        ds = disclosures.extract(claims=claims, description=desc,
                                 title=(doc or {}).get("title") or "")
        if not ds:
            return
        rep["disclosures"] = ds
        rep["disclosures_summary"] = disclosures.summary(ds)
        rep["disclosure_list_source"] = "generated"
        print(f"[disclosures] {disclosures.summary(ds)} from {len(claims)} claims and "
              f"{len(desc):,} chars of description (elements list was "
              f"{len(rep.get('elements') or [])})", flush=True)
    except Exception:
        traceback.print_exc()


def _external_block(query, doc):
    """Run the parallel keyword + semantic fan-out against the external APIs.

    Separate from `_federate_block` on purpose, and both run. They answer different questions:
    the federation returns App A's own LLM-ranked shortlist of ~45 families, which is a good
    second opinion and carries per-source provenance; this returns the RAW candidates of several
    dozen problem-shaped queries, which is where art from OUTSIDE the indexed CPC branches comes
    from. Never raises."""
    try:
        brief = query_set.retrieval_text(query or "")
        claims = list((doc or {}).get("claims") or [])
        specs = query_set.build(query, claims=claims)
        ext = external.run(specs, brief=brief, claims=claims)
        print(f"[external] {len(ext.get('aspects') or [])} aspects, "
              f"{len(ext.get('queries') or [])} queries -> {ext.get('n_candidates', 0)} candidates, "
              f"{ext.get('n_families', 0)} families in {ext.get('elapsed', 0)}s "
              f"{ext.get('stats')}", flush=True)
        return ext
    except Exception:
        traceback.print_exc()
        return None


def _espacenet_safe(pub, family_id=None):
    try:
        return enrich_display.espacenet_url(pub, family_id)
    except Exception:
        return None


def ensure_report(slug, query=None, subject=None, mode="novelty", regen=False, wide=False,
                  doc_token=None, search_focus="all_text", from_queue=False, depth="deep",
                  restart_partial=False):
    """Return ('ready'|'running'|'missing'|'busy', report_or_None). Kicks off background
    generation if needed. A search that arrives while the gate is full is QUEUED (run_queue) and
    reported as running; 'busy' is only returned to the dispatcher itself (`from_queue=True`),
    which leaves the row queued and retries.

    `restart_partial`: a PARTIAL file with no live job is an interrupted run, and serving it as
    "ready" made the dispatcher mark re-queued runs done without ever running them. The dispatcher
    passes True, and so does a POST to /run, because both are requests to RUN the search: the
    stale partial artifacts are dropped and the run starts over. VIEWER calls keep the default, so
    a partial page still renders with the interrupted banner from _job_event saying what it is.
    Reported 2026-08-20 as a report stuck on its first phase for 76 minutes, whose own status
    endpoint said "Use Re-run to start it again" while no Re-run could."""
    p = report_path(slug)
    if p.exists() and not regen:
        try:
            rep = json.loads(p.read_text())
        except Exception:
            rep = None
        if rep is not None:
            with _JOB_LOCK:
                job = _JOBS.get(slug) or {}
                job_live = job.get("status") in ("running", "partial") and not job.get("queued")
            if not (restart_partial and rep.get("partial") and not job_live):
                return "ready", rep
            _drop_partial_report(slug)
    # Atomically claim the slug: check-and-set under the lock so two concurrent requests for the
    # same new query can't both start a generation (the second sees "running" and just polls).
    # A "queued" placeholder is claimable ONLY by the dispatcher — a user re-request must not
    # jump the line, and the dispatcher must not be blocked by the placeholder it is serving.
    with _JOB_LOCK:
        job = _JOBS.get(slug)
        if job and job["status"] in ("running", "partial"):
            if not (from_queue and job.get("queued")):
                return "running", None
        if query is None:
            return "missing", None
        _JOBS[slug] = {"status": "running", "msg": "Queued…", "t0": time.time(),
                       "tok0": _tok_now()}
    # Reserve a generation slot AFTER claiming the slug (so the claim can be released cleanly).
    gated = False
    if auth.run_gate:
        ok, why = auth.run_gate.try_begin(depth=depth)
        if not ok:
            if from_queue:
                with _JOB_LOCK:                    # restore the placeholder; row stays queued
                    _JOBS[slug] = {"status": "running", "queued": True, "msg": "Queued…",
                                   "t0": time.time(), "tok0": _tok_now()}
                return "busy", why
            try:
                pos = run_queue.enqueue(slug, {
                    "query": query, "subject": subject, "mode": mode, "wide": wide,
                    "doc_token": doc_token, "search_focus": search_focus, "depth": depth})
                with _JOB_LOCK:
                    _JOBS[slug] = {"status": "running", "queued": True,
                                   "msg": (f"Queued behind {max(pos - 1, 0)} search(es) — "
                                           "starts automatically, you can close this tab."),
                                   "t0": time.time(), "tok0": _tok_now()}
                return "running", None
            except Exception:
                #  The queue store being down must not turn into a lost search request:
                #  fall back to the old refusal so the caller knows to retry.
                traceback.print_exc()
                with _JOB_LOCK:
                    _JOBS.pop(slug, None)          # release the claim; nothing was started
                return "busy", why
        gated = True
    #  EVERY start gets a queue row (see run_queue.record_started): a direct start that never
    #  waited in line must still be requeue-able after a restart, and the row carries the run's
    #  overall clock and attempt counter for the progress UI.
    run_queue.record_started(slug, {
        "query": query, "subject": subject, "mode": mode, "wide": wide,
        "doc_token": doc_token, "search_focus": search_focus, "depth": depth})
    _QROW_CACHE.pop(slug, None)
    try:
        subj_obj = _subject_obj(subject)
        if regen:
            p.unlink(missing_ok=True)
            (REPORTS / f"{slug}.view.json").unlink(missing_ok=True)
            (REPORTS / f"{slug}.detail-preview.json").unlink(missing_ok=True)
        threading.Thread(target=_run_job,
                         args=(slug, query, subj_obj, mode, gated, wide, doc_token, search_focus,
                               depth),
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


def _detail_preview_item(card):
    """The text-only subset tabs need, shaped like ``/api/ref`` but with no live lookups.

    ``build_view`` already paid for these claims/paragraphs while assembling the report. Writing
    the subset once avoids 25 repeat database round trips and means the first tab click can be a
    memory hit even while the search is still refining.
    """
    pub = card.get("pub")
    cpc = card.get("cpc") or []
    return {
        "pub": pub,
        "display": {
            "title": card.get("title"), "abstract": card.get("abstract"),
            "classifications": cpc, "images": card.get("images") or [],
            "n_images": card.get("n_images") or 0,
            "google_patents": card.get("google_patents"), "espacenet": card.get("espacenet"),
            "lang_flags": {"abstract": translate.looks_nonenglish(card.get("abstract") or "")},
        },
        "sections": {
            "claims": card.get("claims") or [],
            "paragraphs": card.get("description") or [],
            "figures": card.get("figure_caps") or [],
            "citations": [],
        },
        "matched": {
            "coord": card.get("match_coord"), "kind": card.get("match_kind"),
            "score": card.get("match_score") or 0,
            "coord_raw": card.get("matched_coord_raw"),
        } if card.get("match_kind") or card.get("match_coord") else None,
        "rationale": None,
        "_preview": True,
    }


def _write_detail_preview(slug, view):
    if not valid_slug(slug):
        return
    items = {}
    for card in (view or {}).get("cards") or []:
        pub = card.get("pub")
        if pub and pub not in items:
            items[pub] = _detail_preview_item(card)
        if len(items) >= _DISPLAY_TOP:
            break
    if not items:
        return
    path = REPORTS / f"{slug}.detail-preview.json"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps({"version": 1, "partial": bool((view or {}).get("partial")),
                                   "items": items}, default=str))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _detail_preview_section(item, section):
    """Return only the cached fields needed by one result-card tab."""
    display, sections = item.get("display") or {}, item.get("sections") or {}
    base_display = {
        "title": display.get("title"), "google_patents": display.get("google_patents"),
        "espacenet": display.get("espacenet"),
    }
    out = {"pub": item.get("pub"), "display": base_display, "sections": {},
           "matched": item.get("matched"), "rationale": None, "_preview": True}
    if section == "abstract":
        out["display"].update(abstract=display.get("abstract"),
                              lang_flags=display.get("lang_flags") or {})
    elif section == "claims":
        out["sections"]["claims"] = sections.get("claims") or []
    elif section == "desc":
        out["sections"]["paragraphs"] = sections.get("paragraphs") or []
    elif section == "class":
        out["display"]["classifications"] = display.get("classifications") or []
    elif section == "figs":
        out["display"].update(images=display.get("images") or [],
                              n_images=display.get("n_images") or 0)
        out["sections"]["figures"] = sections.get("figures") or []
    elif section == "why":
        out["display"].update(images=display.get("images") or [],
                              n_images=display.get("n_images") or 0)
        out["rationale"] = item.get("rationale")
    else:
        return item
    return out


def _can_access_report(slug):
    """Named users see their own searches; administrators retain operational access to all."""
    # On-box regression/warmers are already explicitly trusted by the auth gate. Preserve that
    # contract here too; otherwise named-account isolation authenticates their request and then
    # paradoxically hides every ad-hoc report because a loopback script has no browser session.
    if auth.TRUST_LOOPBACK and auth.is_loopback():
        return True
    if slug in _GOLD or not auth.accounts_enabled(app) or auth.is_admin():
        return True
    user = auth.current_user()
    if not user:
        return False
    try:
        return accounts.can_access_search(user["id"], slug)
    except Exception:
        return False


@app.context_processor
def _corpus_context():
    """`corpus` on every template, so the footer and the public pages do not depend on each route
    remembering to pass it. Cached in corpus_facts and never raises."""
    try:
        return {"corpus": corpus_facts.facts()}
    except Exception:
        return {"corpus": {}}


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
    facts = corpus_facts.facts()
    #  A SIGNED-OUT visitor gets the landing page instead of the search box. Sending them straight
    #  to a login form with no explanation was the biggest gap against a finished product:
    #  somebody deciding whether to upload an unpublished invention to a service has to be able to
    #  read what that service does, and what it indexes, first.
    try:
        signed_out = auth.accounts_enabled(app) and not auth.current_user() and not auth.is_admin()
    except Exception:
        signed_out = False
    if signed_out:
        return render_template("landing.html", corpus=facts)
    return render_template("index.html", corpus=facts)


@app.route("/about")
def about():
    """What the system is, plus the same scope disclosure the report and the exports carry."""
    return render_template("about.html", corpus=corpus_facts.facts())


@app.route("/how-it-works")
def how_it_works():
    """The pipeline in plain language. Public, for the same reason /about is."""
    return render_template("how_it_works.html", corpus=corpus_facts.facts())


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


def _account_history_entries(user_id, *, saved_only=False, limit=300):
    out = []
    for row in accounts.list_searches(user_id, saved_only=saved_only, limit=limit):
        when = row.get("updated_at")
        if hasattr(when, "strftime"):
            when = when.strftime("%Y-%m-%d %H:%M")
        out.append({"slug": row["slug"], "query": (row.get("query") or "")[:400],
                    "title": row.get("title"), "mode": row.get("mode") or "novelty",
                    "search_focus": row.get("search_focus") or "all_text",
                    "subject": row.get("subject"), "ood": False, "when": when or "",
                    "status": row.get("status") or "running", "saved": bool(row.get("saved")),
                    "notify_email": bool(row.get("notify_email")),
                    "notification_status": row.get("notification_status")})
    return out


@app.route("/history")
def history():
    """Search history + the frozen gold-set examples, clearly separated.

    The gold entries used to sit on the search page under "or open a frozen gold-set example
    (instant)". They are demo fixtures, not the user's work, so they are labelled as examples here
    rather than mixed into the history list.
    """
    user = auth.current_user()
    if user:
        try:
            entries = _account_history_entries(
                user["id"], saved_only=request.args.get("saved") == "1")
        except Exception:
            entries = []
    else:
        entries = _history_entries()
    #  Which searches already have a third-party submission built. The history page is where you
    #  come back to a search weeks later, so it has to say which ones already produced papers.
    for e in entries:
        try:
            e["concise_built"] = _concise_count(e.get("slug") or "")
        except Exception:
            pass
    return render_template("history.html", entries=entries, gold=_gold_cards(),
                           named_account=bool(user), saved_only=request.args.get("saved") == "1",
                           corpus=corpus_facts.facts())


@app.route("/run", methods=["POST"])
def run():
    user = auth.current_user()
    if user:
        auth.require_csrf()
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
    search_focus = request.form.get("search_focus", "all_text").strip()
    if search_focus not in ("all_text", "claims"):
        return _error_response({"error": "unknown_search_focus",
                                "detail": "Search focus must be all_text or claims."}, 400,
                               "Unknown search focus.")
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
    #  Document/patent-link searches carry a doc_token minted by /extract: it points at the stashed
    #  full-text chunk vectors + drawing images so the search fans out over the document itself
    #  (multi-chunk semantic + image channels) alongside the text channels. Empty for typed queries.
    doc_token = (request.form.get("doc_token") or "").strip() or None
    #  FEDERATION IS NOW UNCONDITIONAL.
    #
    #  The "Also search wider — external patent APIs" checkbox is gone and every search federates.
    #  The "|wide" marker STAYS in the slug, and deliberately so: it is what keeps these reports in
    #  a different cache namespace from the narrow reports generated before this change. Dropping
    #  it would have made a new wide run overwrite the cached narrow report for the same query --
    #  silently replacing a result the user may have cited. Every pre-existing narrow report keeps
    #  its own slug, stays readable at its own URL, and is listed in /history.
    wide = True
    #  THE TWO-TIER SPLIT (public-tool build-out). depth="quick" is the interactive product: one
    #  retrieval round, local corpus only (no external fan-out, no paid enrichment), screen +
    #  per-limitation batch tail, first report in minutes for well under a dollar. depth="deep"
    #  is the full claim-by-claim attack (unchanged pipeline) and is what the quick report's
    #  escalate button re-runs with. Defaults preserve today's behavior: deep unless asked, and
    #  the gates below only bite when their env flags are set.
    depth = request.form.get("depth", "").strip() or "deep"
    if depth not in ("quick", "deep"):
        return _error_response({"error": "unknown_depth", "depth": depth}, 400,
                               f"Unknown search depth: {depth}")
    if depth == "deep" and not user and os.environ.get("DEEP_REQUIRES_LOGIN", "0") != "0":
        #  Public visitors get the quick tier; the multi-hour attack is for accounts. Forcing
        #  quick (rather than a login wall) keeps the public flow alive and makes the escalate
        #  button the login prompt.
        depth = "quick"
    if depth == "quick":
        wide = False                       # local corpus only: no external APIs on the quick tier
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
    # Every input that changes retrieval or eligibility belongs in the cache identity. Omitting an
    # anchor publication or uploaded-document token can return another search's report/claim grid
    # for the same visible query.
    slug = search_slug(query, mode, wide=wide, search_focus=search_focus,
                       subject=subject, doc_token=doc_token, depth=depth)
    #  restart_partial=True: this is a POST to /run, an explicit request to RUN this search. If
    #  all that is on disk is a partial left by an interrupted run, the honest answer is to start
    #  it again, not to hand back the page that stopped. Viewing the report is a GET and keeps the
    #  default, so a partial still renders with its interrupted banner.
    st, why = ensure_report(slug, query=query, subject=subject, mode=mode, wide=wide,
                            doc_token=doc_token, search_focus=search_focus, depth=depth,
                            restart_partial=True)
    if st == "busy":
        return _error_response({"error": "server busy", "detail": why}, 429,
                               f"The server is at capacity — {why}. Please retry shortly.")
    # remember adhoc meta for the report page title (doc_token persisted so a live Re-run keeps the
    # document-chunk + image channels instead of degrading to text-only).
    (REPORTS / f"{slug}.meta.json").write_text(json.dumps(
        {"query": query, "mode": mode, "subject": subject, "wide": wide, "ood": ood,
         "doc_token": doc_token, "search_focus": search_focus, "depth": depth}))
    if user:
        notify = request.form.get("notify_email") == "1"
        try:
            accounts.record_search(user["id"], slug, query, mode, search_focus, subject,
                                   notify_email=notify,
                                   status=("complete" if st == "ready" else "running"), saved=False)
            if st == "ready" and notify:
                notifications.queue_search_completion(slug)
        except Exception:
            traceback.print_exc()
    return redirect(url_for("report", slug=slug))


@app.route("/extract", methods=["POST"])
def extract():
    """Front door for the document / patent-link input modes.

    Accepts EITHER a multipart file upload (drag-drop or browse) OR a `url` form field
    (Google Patents / Espacenet URL, or a bare publication number). Extracts text AND drawings,
    runs a Gemini vision pass over the figures, and fuses everything into ONE search brief which
    the client then submits to the existing POST /run pipeline — no second search path.

    Rate-limited (endpoint 'extract' in auth._LIMITERS) and behind the auth gate, like every
    other route that spends on Vertex. Returns JSON; on failure {ok:false,error} with a status.
    """
    if auth.current_user():
        auth.require_csrf()
    f = request.files.get("file")
    url = (request.form.get("url") or "").strip()
    data = None
    if f is not None and (f.filename or "").strip():
        data = f.read(ingest_input.MAX_BYTES + 1)
        if len(data) > ingest_input.MAX_BYTES:
            return jsonify({"ok": False,
                            "error": f"file too large (max {ingest_input.MAX_BYTES // (1024*1024)} MB)"}), 413
    elif not url:
        return jsonify({"ok": False, "error": "provide a file or a patent URL"}), 400

    # Reading a 64-page grant measures at ~50 s — two model passes over the whole document plus
    # drawing extraction. Held synchronously that is a blank spinner on the most important
    # interaction in the app, so the client asks for a job and polls /extract/status for the
    # phase actually running. The synchronous path is kept for programmatic callers and tests.
    if request.form.get("async") == "1":
        job = _start_extract_job(data, (f.filename if f is not None else ""), url)
        if job is None:
            return jsonify({"ok": False, "error": "the server is reading other documents right "
                                                  "now — please try again in a minute"}), 429
        return jsonify({"ok": True, "job": job, "state": "running"}), 202

    try:
        res = (ingest_input.extract_upload(data, f.filename) if data is not None
               else ingest_input.extract_link(url))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"extraction failed: {str(e)[:200]}"}), 500
    status = res.pop("status", 200 if res.get("ok") else 400)
    # Stash the full-text chunk vectors + drawing images server-side and hand back only a token, so
    # /run can fan out over the document itself (multi-chunk semantic + image channels) without
    # round-tripping heavy vectors/base64 through the browser or bloating the page.
    if res.get("ok"):
        res["doc_token"] = _stash_doc(res)
        # The browser needs the brief, preview thumbnails, counts and token — never the 768-float
        # vectors, full claim chunks, or full-resolution base64 drawings.  The old response kept
        # those heavy fields despite the server-side stash and could turn a 30 MB upload into a
        # much larger JSON response.
        res.pop("chunks", None)
        res.pop("figure_images", None)
        res.pop("full_text", None)
    return jsonify(res), status


# ---------------------------------------------------------------------------
# extraction as a job, so the upload can show real progress instead of a spinner
# ---------------------------------------------------------------------------
# Each phase carries the share of the bar it has reached when it BEGINS. The percentages are
# proportional to measured phase durations on a 67-page scan (read 1 s, drawings 24 s,
# structure 40 s, brief 10 s, vision 6 s, embed 4 s), so the bar tracks work done rather than a
# timer. They must be listed in EXECUTION order — extract_upload runs read and figures,
# patent_doc.analyze runs structure then brief, and _build runs vision then embed — otherwise
# the phase NAME goes backwards even though the bar does not.
EXTRACT_STAGES = [
    ("read", "Reading the document", 5),
    ("figures", "Extracting the drawings", 12),
    ("structure", "Separating the claims and the abstract", 40),
    ("brief", "Writing the search brief from the whole document", 78),
    ("vision", "Reading the drawings", 88),
    ("embed", "Preparing the query vectors", 94),
]
_EXTRACT_PCT = {k: p for k, _, p in EXTRACT_STAGES}
_EXTRACT_LABEL = {k: label for k, label, _ in EXTRACT_STAGES}
EXTRACT_JOB_TTL = 30 * 60
EXTRACT_JOBS_MAX = 200
# Running the extraction on a background thread takes it out from under gunicorn's worker pool,
# which is what used to bound it. One extraction holds its uploaded bytes (up to 30 MB), runs
# poppler and the drawing extractor, and makes ~11 Vertex calls; unbounded, a handful of
# simultaneous uploads would take the box down. Saturation is reported as "busy", the same way
# an over-subscribed search is.
EXTRACT_MAX_CONCURRENT = int(os.environ.get("EXTRACT_MAX_CONCURRENT", "4"))
_EXTRACT_SLOTS = threading.BoundedSemaphore(EXTRACT_MAX_CONCURRENT)
_EXTRACT_JOBS = {}
_EXTRACT_JOBS_LOCK = threading.Lock()


def _extract_job_set(job, **kw):
    with _EXTRACT_JOBS_LOCK:
        rec = _EXTRACT_JOBS.get(job)
        if rec is not None:
            rec.update(kw)
            rec["t"] = time.time()


def _extract_jobs_sweep():
    """Drop finished/abandoned jobs. Called on each new job, so nothing accumulates unbounded."""
    now = time.time()
    with _EXTRACT_JOBS_LOCK:
        for k in [k for k, v in _EXTRACT_JOBS.items() if now - v.get("t", 0) > EXTRACT_JOB_TTL]:
            _EXTRACT_JOBS.pop(k, None)
        while len(_EXTRACT_JOBS) > EXTRACT_JOBS_MAX:
            _EXTRACT_JOBS.pop(min(_EXTRACT_JOBS, key=lambda k: _EXTRACT_JOBS[k].get("t", 0)), None)


def _start_extract_job(data, filename, url):
    """Start a background extraction. Returns the job id, or None when the box is at capacity."""
    _extract_jobs_sweep()
    if not _EXTRACT_SLOTS.acquire(blocking=False):
        return None
    job = uuid.uuid4().hex
    with _EXTRACT_JOBS_LOCK:
        _EXTRACT_JOBS[job] = {"state": "running", "stage": "read", "pct": 3,
                              "msg": "Reading the document", "t": time.time(), "result": None}

    def on_stage(key, msg):
        _extract_job_set(job, stage=key, pct=_EXTRACT_PCT.get(key, 50),
                         msg=_EXTRACT_LABEL.get(key, msg))

    def work():
        try:
            res = (ingest_input.extract_upload(data, filename, on_stage=on_stage)
                   if data is not None else ingest_input.extract_link(url, on_stage=on_stage))
            res.pop("status", None)
            if res.get("ok"):
                res["doc_token"] = _stash_doc(res)
                res.pop("chunks", None)
                res.pop("figure_images", None)
                res.pop("full_text", None)
                _extract_job_set(job, state="done", pct=100, stage="done",
                                 msg="Ready", result=res)
            else:
                _extract_job_set(job, state="error", pct=100,
                                 msg=res.get("error") or "extraction failed")
        except Exception as e:
            traceback.print_exc()
            _extract_job_set(job, state="error", pct=100,
                             msg=f"extraction failed: {str(e)[:200]}")
        finally:
            _EXTRACT_SLOTS.release()

    threading.Thread(target=work, name=f"extract-{job[:8]}", daemon=True).start()
    return job


@app.route("/extract/status/<job>")
def extract_status(job):
    job = re.sub(r"[^0-9a-f]", "", str(job))[:64]
    with _EXTRACT_JOBS_LOCK:
        rec = _EXTRACT_JOBS.get(job)
        snap = dict(rec) if rec else None
    if snap is None:
        return jsonify({"ok": False, "state": "unknown",
                        "error": "that upload is no longer in progress — please try again"}), 404
    out = {"ok": snap["state"] != "error", "state": snap["state"], "stage": snap.get("stage"),
           "pct": snap.get("pct"), "msg": snap.get("msg")}
    if snap["state"] == "done":
        out["result"] = snap.get("result")
        with _EXTRACT_JOBS_LOCK:                   # one delivery; the client has the payload now
            _EXTRACT_JOBS.pop(job, None)
    elif snap["state"] == "error":
        out["error"] = snap.get("msg")
    return jsonify(out)


MAX_REVISED_CLAIMS = 200
MAX_REVISED_CLAIM_CHARS = 12000
MAX_REVISED_BRIEF_CHARS = 20000


@app.route("/extract/revise", methods=["POST"])
def extract_revise():
    """Apply the user's corrections to the extracted search material.

    The review panel is only meaningful if a correction actually reaches retrieval. Each claim
    is its own query vector, so an edited claim has to be re-chunked and re-embedded; otherwise
    the textarea would show the corrected claim while the search still ran on the text the user
    had just rejected. Returns a NEW doc_token — the old one is left alone so a stale tab cannot
    be affected, and the token is part of the report slug, so a corrected search is a different
    report rather than an overwrite of the uncorrected one.
    """
    if auth.current_user():
        auth.require_csrf()
    body = request.get_json(silent=True) or {}
    token = (body.get("doc_token") or "").strip()
    prior = _load_doc_materials(token) if token else None
    if token and prior is None:
        return jsonify({"ok": False, "error": "that upload has expired — please upload it again"}), 410

    claims = []
    for c in (body.get("claims") or [])[:MAX_REVISED_CLAIMS]:
        if isinstance(c, str):
            c = {"text": c}
        if not isinstance(c, dict):
            continue
        t = str(c.get("text") or "").strip()[:MAX_REVISED_CLAIM_CHARS]
        if t:
            claims.append({"claim_no": len(claims) + 1, "text": t,
                           "independent": c.get("independent")})
    abstract = str(body.get("abstract") or "").strip()[:ingest_input.MAX_QUERY_CHUNKS * 1000]
    brief = str(body.get("brief") or "").strip()[:MAX_REVISED_BRIEF_CHARS]
    title = str(body.get("title") or "").strip()[:300]
    if not (claims or abstract or brief):
        return jsonify({"ok": False, "error": "nothing to search — provide a brief, an abstract "
                                              "or at least one claim"}), 400
    try:
        rebuilt = ingest_input.rebuild_from_edits(abstract=abstract, claims=claims, brief=brief,
                                                  title=title)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"could not apply the edits: {str(e)[:200]}"}), 500

    # Carry the figures and the verbatim upload across from the original extraction: the user
    # edited the TEXT, not the drawings, and the drafting workspace still needs the raw document.
    rebuilt["source"] = (prior or {}).get("source") or "upload"
    rebuilt["label"] = (prior or {}).get("label") or ""
    rebuilt["full_text"] = (prior or {}).get("full_text") or ""
    rebuilt["figure_images"] = [{"mime": "image/png", "b64": base64.b64encode(b).decode("ascii")}
                                for b in (prior or {}).get("figure_blobs") or []]
    new_token = _stash_doc(rebuilt)
    embedded = sum(1 for c in rebuilt["chunks"] if c.get("vector"))
    return jsonify({"ok": True, "doc_token": new_token, "n_claims": rebuilt["n_claims"],
                    "n_chunks": rebuilt["n_chunks"], "n_embedded": embedded,
                    "n_independent": rebuilt["n_independent"]})


@app.route("/report/<slug>/ranked")
def ranked_tail(slug):
    """The FULL ranked list, paginated — every family the search ordered, not only the 60 cards.

    The card page is a fixed-size view over `ranked_families`; this page is the list itself, so
    a reference the pipeline found, read and ranked is never unreachable (the measured failure:
    attorney references read cell-perfectly and ranked 103/247 were invisible). Cheap by
    construction: one reps query, no LLM, no figures."""
    if not _can_access_report(slug):
        abort(404)
    p = report_path(slug)
    if not p.exists():
        return render_template("notfound.html", slug=slug), 404
    try:
        rep = json.loads(p.read_text())
    except Exception:
        return render_template("notfound.html", slug=slug), 404
    fams = list(rep.get("ranked_families") or [])
    try:
        start = max(0, int(request.args.get("start", "0")))
        n = min(300, max(20, int(request.args.get("n", "120"))))
    except ValueError:
        start, n = 0, 120
    window = fams[start:start + n]
    deep = {}
    try:
        d = deep_analysis.result(slug, REPORTS) or {}
        deep = d.get("by_pub") or {}
    except Exception:
        pass
    rows = []
    try:
        with db.cursor() as cur:
            reps = webview.resolve_family_reps(cur, window)
    except Exception:
        traceback.print_exc()
        reps = {}
    for i, fam in enumerate(window):
        r = reps.get(fam) or {}
        pub = r.get("publication_number") or fam
        info = deep.get(pub) or {}
        cells = info.get("covered")
        rows.append({
            "rank": start + i + 1, "pub": pub, "title": (r.get("title") or "")[:160],
            "date": str(r.get("publication_date") or "")[:10],
            "screen": info.get("screen"), "read": bool(info.get("read_in_full")),
            "batched": bool(info.get("batched")),
            "cells": (len(cells) if isinstance(cells, list) else cells) or ""})
    return render_template("ranked.html", slug=slug, rows=rows, start=start, n=n,
                           total=len(fams), page_size_note=_DISPLAY_TOP)


@app.route("/report/<slug>")
def report(slug):
    if not _can_access_report(slug):
        abort(404)
    regen = request.args.get("rerun") == "1"
    query = subject = None
    mode = "novelty"
    wide = False        # the progress view lists the federation stage only for a wide run
    ood = None          # out-of-domain verdict recorded at search time, shown as a results banner
    doc_token = None     # document-search materials, so a live Re-run keeps the doc channels
    search_focus = "all_text"
    depth = "deep"
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
            doc_token = m.get("doc_token")
            search_focus = m.get("search_focus") or "all_text"
            depth = m.get("depth") or "deep"
        title = "Ad-hoc search"
    status, rep = ensure_report(slug, query=query, subject=subject, mode=mode, regen=regen,
                                wide=wide, doc_token=doc_token, search_focus=search_focus,
                                depth=depth)
    if status == "missing":
        return render_template("notfound.html", slug=slug), 404
    if status == "busy":
        return render_template("notfound.html", slug=f"{slug} — {rep}"), 429
    if status != "ready":
        active_search = None
        user = auth.current_user()
        if user:
            try:
                active_search = accounts.get_search(user["id"], slug)
            except Exception:
                pass
        return render_template("generating.html", slug=slug, title=title,
                               query=(query or "")[:400], mode=mode, wide=wide,
                               search_focus=search_focus, active_search=active_search)
    view = _build_view_cached(slug, rep, regen)
    view["slug"] = slug
    view["title"] = title
    view["is_gold"] = slug in _GOLD
    view["search_focus"] = rep.get("search_focus") or search_focus
    #  Carried so "Refine and search again" keeps the uploaded document's chunk + image channels
    #  instead of silently degrading the new search to text-only.
    view["doc_token"] = doc_token or ""
    #  `depth` is part of the slug hash, so the Restart control on an interrupted run has to post
    #  it back or the restart mints a NEW slug and the user's url still never finishes.
    view["depth"] = depth
    try:
        _write_detail_preview(slug, view)
    except Exception:
        traceback.print_exc()
    user = auth.current_user()
    view["account_search"] = None
    view["public"] = None
    if user:
        try:
            view["account_search"] = accounts.get_search(user["id"], slug)
            accounts.mark_search_viewed(user["id"], slug)
        except Exception:
            pass
        #  Whether this report already has a public link, so the Export control can show its state
        #  rather than asking. Never fatal: a missing table must not take down the report page.
        try:
            view["public"] = public_report.status_for_owner(user["id"], slug)
        except Exception:
            traceback.print_exc()
    # Also schedule on report-open.  This covers a process restart between generation and the
    # worker starting, while ensure() keeps the operation idempotent and cache-backed.
    try:
        query_claim_grid.ensure(slug, rep, view, REPORTS)
    except Exception:
        traceback.print_exc()
    if "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            report_archive.ensure(slug, rep, view, REPORTS)
        except Exception:
            traceback.print_exc()
        #  Read the top references in full and chart each against the search input. Normally
        #  already done: deep_rank reads them DURING generation and publishes the charts here, so
        #  this is a cache hit. It still runs for a report generated before that stage existed.
        #
        #  NEVER start it from a PARTIAL report. `ensure_report` returns "ready" as soon as the
        #  partial snapshot is on disk, so opening the page mid-run used to start the reading
        #  against an ordering that the final report then replaced, and the result was cached for
        #  ever (`deep_analysis.invalidate` was never called). That is why a card at rank 11 could
        #  say "not among the ones read in full".
        try:
            if not rep.get("partial"):
                deep_analysis.ensure(slug, rep, view, REPORTS)
        except Exception:
            traceback.print_exc()
    view["deep_analysis"] = deep_analysis.metadata(rep, view)
    view["archive"] = report_archive.metadata(slug, REPORTS)
    view["slug"] = slug                       # the full-ranked-list link needs it
    #  The tier this report was made at, and everything the escalate form needs to re-run the
    #  same inputs at full depth.
    view["depth"] = rep.get("depth") or depth
    if view["depth"] == "quick":
        view["escalate"] = {"query": query or rep.get("query") or "", "mode": mode,
                            "doc_token": doc_token or "", "search_focus": search_focus}
    #  The filing artefacts belong on the report itself, not only on a share of it: the owner is
    #  the one who builds them and the most likely person to come back for them.
    view["concise_docs"] = _concise_built(slug)
    view["concise_built"] = len(view["concise_docs"])
    return render_template("report.html", v=view, ood=ood, corpus=corpus_facts.facts())


#  {slug: (report mtime, view)} for the live card stream below. Bounded by the number of searches
#  running at once, and every entry is dropped the moment its report stops being partial.
_PARTIAL_VIEWS: dict = {}


@app.route("/api/cards/<slug>")
def api_cards(slug):
    """The references ranked SO FAR, as rendered cards, for a page that is already open.

    A search that runs for fifteen minutes used to show one snapshot of results and then, at the
    very end, replace the whole page. Everything the agent found in between — the rounds, the
    citation and family expansion, the external fan-out — was invisible until it was all over.
    This lets the open page take delivery of each new reference as the agent admits it, using the
    SAME card markup as first paint (templates/_refcard.html), so nothing about a streamed card
    differs from one that was there at the start.

    `offset` is how many cards the page already holds. The response is the ones after it.
    """
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    p = report_path(slug)
    if not p.exists():
        return jsonify({"cards": "", "n": 0, "partial": True, "ready": False})
    try:
        rep = json.loads(p.read_text())
    except Exception:
        return jsonify({"cards": "", "n": 0, "partial": True, "ready": False})
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    partial = bool(rep.get("partial"))
    #  A partial view is deliberately never written to disk (it would shadow the final report), so
    #  every poll would otherwise re-embed the query and re-resolve the whole candidate list from
    #  Postgres. Keyed on the report's mtime: the answer can only change when the agent writes a
    #  new snapshot, so between snapshots this is free, and a new snapshot invalidates it exactly.
    stamp = p.stat().st_mtime
    hit = _PARTIAL_VIEWS.get(slug)
    if hit and hit[0] == stamp:
        view = hit[1]
    else:
        view = _build_view_cached(slug, rep)
        if partial:
            _PARTIAL_VIEWS[slug] = (stamp, view)
        else:
            _PARTIAL_VIEWS.pop(slug, None)
    cards = view.get("cards") or []
    #  Only ever APPEND. Re-sending a card the page already holds would discard whatever state it
    #  has accumulated — an opened tab, a triage flag, a loaded drawing. The authoritative order
    #  arrives separately (reorderCards), which moves existing nodes instead of replacing them.
    fresh = cards[offset:]
    html = ""
    if fresh:
        html = render_template("_cards.html", v=view, cards=fresh, partial=partial)
    return jsonify({"cards": html, "n": len(cards), "added": len(fresh),
                    "partial": partial, "ready": not partial,
                    "families": rep.get("n_families") or 0})


@app.route("/api/searches/<slug>", methods=["GET", "POST"])
def api_saved_search(slug):
    """Bookmark/unbookmark a report in the signed-in user's account."""
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    user = auth.current_user()
    if not user:
        return jsonify({"error": "a named account is required"}), 403
    if request.method == "GET":
        row = accounts.get_search(user["id"], slug)
        return jsonify({"exists": bool(row), "saved": bool(row and row.get("saved")),
                        "title": row.get("title") if row else None})
    auth.require_csrf()
    data = request.get_json(silent=True) or request.form
    saved = data.get("saved")
    saved = saved is True or str(saved).lower() in ("1", "true", "yes", "on")
    row = accounts.get_search(user["id"], slug)
    if not row:
        rep = _load_report(slug)
        if not rep:
            return jsonify({"error": "report not found"}), 404
        meta = REPORTS / f"{slug}.meta.json"
        m = {}
        if meta.exists():
            try:
                m = json.loads(meta.read_text())
            except Exception:
                pass
        row = accounts.record_search(
            user["id"], slug, m.get("query") or rep.get("query") or "",
            m.get("mode") or rep.get("mode") or "novelty",
            m.get("search_focus") or rep.get("search_focus") or "all_text",
            m.get("subject") or rep.get("subject"), notify_email=False,
            status="complete", saved=saved)
    row = accounts.set_search_saved(user["id"], slug, saved, title=data.get("title"))
    return jsonify({"ok": True, "saved": bool(row.get("saved")), "title": row.get("title")})


@app.route("/api/searches/<slug>/notification", methods=["GET", "POST"])
def api_search_notification(slug):
    """Let a signed-in user switch from waiting in the tab to a durable email alert."""
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    user = auth.current_user()
    if not user:
        return jsonify({"error": "a named account is required"}), 403
    row = accounts.get_search(user["id"], slug)
    if not row:
        return jsonify({"error": "search is not in this account"}), 404
    if request.method == "GET":
        return jsonify({"enabled": bool(row.get("notify_email")),
                        "status": row.get("notification_status") or "not_requested"})
    auth.require_csrf()
    data = request.get_json(silent=True) or request.form
    raw = data.get("enabled", True)
    enabled = raw is True or str(raw).lower() in ("1", "true", "yes", "on")
    row = accounts.set_search_notification(user["id"], slug, enabled)
    # If completion raced this click, queue now instead of waiting for an event that already ran.
    # The durable outbox key makes the concurrent worker path idempotent.
    cached_report = _load_report(slug)
    report_complete = bool(cached_report and not cached_report.get("partial"))
    if enabled and (row.get("status") == "complete" or report_complete):
        try:
            notifications.queue_search_completion(slug)
            row = accounts.get_search(user["id"], slug) or row
        except Exception:
            traceback.print_exc()
    return jsonify({"enabled": bool(row.get("notify_email")),
                    "status": row.get("notification_status") or "pending",
                    "email": user["email"]})


@app.route("/api/archive/<slug>", methods=["GET", "POST"])
def api_archive(slug):
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    if not _can_access_report(slug):
        abort(404)
    if request.method == "GET":
        return jsonify(report_archive.status(slug, REPORTS))
    if auth.current_user():
        auth.require_csrf()
    rep = _load_report(slug)
    if not rep:
        return jsonify({"error": "report not found"}), 404
    vp = REPORTS / f"{slug}.view.json"
    view = {}
    if vp.exists():
        try:
            view = json.loads(vp.read_text())
        except Exception:
            pass
    return jsonify(report_archive.ensure(slug, rep, view, REPORTS))


@app.route("/archive/<slug>/download")
def download_archive(slug):
    if not valid_slug(slug) or not _can_access_report(slug):
        abort(404)
    path = report_archive.archive_path(slug, REPORTS)
    if not path:
        abort(404)
    st = report_archive.status(slug, REPORTS)
    return send_from_directory(path.parent, path.name, as_attachment=True,
                               download_name=st.get("download_name") or path.name,
                               mimetype="application/zip")


# Cap on the unified (local + federated) ranked list that is rendered/exported. Kept at the
# previous local-only top-N so folding in external hits does not regress the page-weight budget:
# federated-only cards compete for these slots by relevance instead of extending the list.
#  50, not 25: the reading stage (deep_rank) reads far more than that in full, and a reference
#  it grounded should be on the page rather than behind a 'more references' pager that shows
#  bibliography only.
#  60, raised with deep_rank.CHART_TOP_MAX. The measured lesson there is that widening the READ
#  set while the page stays a fixed size lowers visible recall — references that had been on the
#  page at chart rank 30-47 fell off it. So the page grows whenever the reading does.
_DISPLAY_TOP = int(os.environ.get('DISPLAY_TOP', '60'))


def _build_view_cached(slug, rep, regen=False):
    """Cache the built view (query embed + DB resolution) to <slug>.view.json for instant reloads.
    A partial/streaming snapshot is NEVER cached (it would shadow the final report) and is flagged
    so the report page keeps polling and upgrades itself when the full run finishes."""
    partial = bool(rep.get("partial"))
    vp = REPORTS / f"{slug}.view.json"
    if vp.exists() and not regen and not partial:
        try:
            view = json.loads(vp.read_text())
            # Only trust a cache that was actually listwise-ranked. A cache written when the
            # listwise pass failed at generation time (transient LLM error) is frozen in
            # FUSION order -- serving it would show the wrong order forever, which is exactly
            # the "strong result stuck low" bug. Treat an un-reranked cache as a miss and
            # rebuild (which re-runs the rerank and re-folds the federated hits). A successful
            # cache carries listwise_reranked=True and is returned instantly.
            if view.get("listwise_reranked"):
                # Query-document metadata is small and can be backfilled onto a cache written by
                # an older release without rebuilding or reranking the report.
                qmeta = query_claim_grid.metadata(rep)
                if view.get("query_claim_grid") != qmeta:
                    view["query_claim_grid"] = qmeta
                    vp.write_text(json.dumps(view, default=str))
                # source_tags is STATUS, not content. The cache exists to skip the query embed,
                # the DB resolution and the claim-matrix verification -- all immutable for a
                # finished report. Which APIs are wired up is not: it changes when a key is
                # added upstream, and the first view built after a restart sees an empty source
                # catalogue (the health probe is backgrounded so a render never blocks on it).
                # Freezing that would leave a report permanently claiming its sources failed.
                view["source_tags"] = webview._source_tags(
                    rep, view.get("n_local", len(view.get("cards") or [])))
                # THE CLAIM LEDGER IS RE-DERIVED ON EVERY CACHE HIT, not backfilled once.
                # It is computed from the stored limitation rows alone — no DB, no LLM — and it is
                # the one part of the view that carries a legal assertion, so it must always be
                # the answer the CURRENT rule gives rather than the one frozen at run time. Every
                # report written before 2026-08-20 marked dependent claims ANTICIPATED under a
                # parent that nothing anticipated, which 112(d) makes impossible; re-deriving here
                # is what withdraws it from the 660 reports already on disk.
                try:
                    fresh = webview.build_ledger_view(rep)
                    if fresh != view.get("ledger"):
                        view["ledger"] = fresh
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # The prosecution block likewise: it is read off the report, and a cache written
                # before the file wrapper was mined simply does not have it.
                try:
                    pros = webview.build_prosecution_view(rep)
                    if pros != view.get("prosecution"):
                        view["prosecution"] = pros
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # THE CLAIM LEDGER IS RE-DERIVED ON EVERY CACHE HIT, not backfilled once.
                # It is computed from the stored limitation rows alone — no DB, no LLM — and it is
                # the one part of the view that carries a legal assertion, so it must always be
                # the answer the CURRENT rule gives rather than the one frozen at run time. Every
                # report written before 2026-08-20 marked dependent claims ANTICIPATED under a
                # parent that nothing anticipated, which 112(d) makes impossible; re-deriving here
                # is what withdraws it from the 660 reports already on disk.
                try:
                    fresh = webview.build_ledger_view(rep)
                    if fresh != view.get("ledger"):
                        view["ledger"] = fresh
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # The prosecution block likewise: it is read off the report, and a cache written
                # before the file wrapper was mined simply does not have it.
                try:
                    pros = webview.build_prosecution_view(rep)
                    if pros != view.get("prosecution"):
                        view["prosecution"] = pros
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # THE CLAIM LEDGER IS RE-DERIVED ON EVERY CACHE HIT, not backfilled once.
                # It is computed from the stored limitation rows alone — no DB, no LLM — and it is
                # the one part of the view that carries a legal assertion, so it must always be
                # the answer the CURRENT rule gives rather than the one frozen at run time. Every
                # report written before 2026-08-20 marked dependent claims ANTICIPATED under a
                # parent that nothing anticipated, which 112(d) makes impossible; re-deriving here
                # is what withdraws it from the 660 reports already on disk.
                try:
                    fresh = webview.build_ledger_view(rep)
                    if fresh != view.get("ledger"):
                        view["ledger"] = fresh
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # The prosecution block likewise: it is read off the report, and a cache written
                # before the file wrapper was mined simply does not have it.
                try:
                    pros = webview.build_prosecution_view(rep)
                    if pros != view.get("prosecution"):
                        view["prosecution"] = pros
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # Backfill the Feature-1 family timeline onto caches written before it existed
                # (cheap one-query DB pass, no rerank/LLM) so old reports render the strip too.
                try:
                    if webview.ensure_family_timelines(view.get("cards")):
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # Backfill the eager lemad-Mongo figures + full text onto caches written before this
                # existed (cheap: get_detail is on-disk cached, no download/OPS/LLM), gated by a flag
                # so it runs once per cache. This is what makes an ALREADY-cached report (e.g. the
                # gold drone report) show its sketches on reload without a costly re-rank.
                try:
                    if not view.get("mongo_enriched"):
                        webview.mongo_enrich_cards(view.get("cards") or [])
                        view["mongo_enriched"] = True
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # Backfill the ELEMENT GRID onto caches written before it was drawn from the
                # reading. Every finished report already on disk holds a grid whose cells are
                # retrieval cosines, next to a <slug>.deep.json holding the verdict, the verbatim
                # quote and the passage number for the same feature and the same reference. This
                # is a pure re-shape of data both files already contain — no LLM, no DB, no
                # rerank — so an existing report upgrades on its next view rather than needing a
                # re-run. Gated on the chart's own `source`, which is also what the template
                # branches on, so there is one flag rather than a second one to keep in step.
                try:
                    dirty = False
                    if (view.get("claim_chart") or {}).get("source") != "reading":
                        deep = deep_analysis.result(slug, REPORTS)
                        fresh = webview.build_reading_chart(rep, deep) if deep else None
                        if fresh:
                            view["claim_chart"] = fresh
                            dirty = True
                    # Same shape of backfill for the settings panel ("Show full search"): it is a
                    # projection of the report, so a cache written before it existed can gain it
                    # without a rebuild.
                    if not view.get("search_params"):
                        view["search_params"] = webview.search_params(rep)
                        dirty = True
                    if dirty:
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    traceback.print_exc()
                # Backfill CORRECT outbound office links onto caches written before the dropped-zero
                # fix (US pre-grant Google/Espacenet URLs that 404). Cheap pure-string pass via
                # pubnorm, gated by a flag so it runs once per cache.
                try:
                    if not view.get("office_links_fixed"):
                        webview.fix_view_office_links(view)
                        view["office_links_fixed"] = True
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                # A view cache can outlive a recovered figure file. Drop stale local entries so
                # the page renders a settled placeholder (and can backfill from /api/figs) instead
                # of issuing a guaranteed-broken /figures/... request on every reload.
                try:
                    if webview.prune_missing_image_files(view.get("cards") or []):
                        vp.write_text(json.dumps(view, default=str))
                except Exception:
                    pass
                return view
        except Exception:
            pass
    #  The full-text reading, when this search already has one. It is what the element grid is
    #  drawn from (webview.build_reading_chart): the retrieval grid it replaces was showing a
    #  cosine score per cell while this file held a verdict, a verbatim quote and a claim or
    #  paragraph number for the same feature and the same reference.
    deep = None
    try:
        deep = deep_analysis.result(slug, REPORTS)
    except Exception:
        traceback.print_exc()
    view = webview.build_view(rep, top_n=_DISPLAY_TOP, deep=deep)
    view["partial"] = partial
    view["query_claim_grid"] = query_claim_grid.metadata(rep)
    if partial:
        # A partial snapshot is never cached and never reranked; keep it light (fusion order,
        # capped) so the first render stays inside the page-weight budget.
        view["cards"] = (view.get("cards") or [])[:_DISPLAY_TOP]
        return view
    if not partial:
        # LISTWISE AGENTIC RERANK (spec item 6). The cards are already fusion- + cross-encoder-
        # ordered (pointwise); now re-judge the merged, family-deduped display set (local corpus
        # + federated-only, folded in by build_view) SEVERAL AT A TIME, each in the CONTEXT of the
        # others, so a near-duplicate sinks and an independently strong reference (incl. a
        # document-chunk-only, image-only, or external-API find) rises. This is the AUTHORITATIVE
        # order the page, the exports and /print all render. Runs once, here, before the view is
        # cached, so /report reloads are instant.
        #
        # listwise_reranked is the cache's trust flag: only set it True when the pass actually
        # produced an order (or there was nothing to reorder). On failure it is False, so the next
        # view build treats the cache as stale and retries rather than freezing a fusion order.
        try:
            cards = view.get("cards") or []
            if rep.get("deep_rank"):
                #  The order is already decided by what the references disclose, read in full
                #  (deep_rank). Running the listwise/snippet pass on top of it would re-judge a
                #  full-text result from 900 characters, which is the exact defect this rebuild
                #  removed. Sort is by the evidence score, ties broken by the incoming order, so
                #  it stays a permutation and stays deterministic.
                cards = order_cards_by_evidence(cards)
                view["ranked_by"] = "deep_rank"
            elif len(cards) > 1:
                q = {"brief": query_set.retrieval_text(rep.get("query") or ""),
                     "elements": rep.get("elements") or [],
                     "domain": rep.get("domain")}  # task C: OOD de-dilution reads the verdict here
                cards = rerank_listwise.rerank_report_cards(q, cards)
                view["ranked_by"] = "listwise"
            # Cap the unified list AFTER ranking, so federated-only cards compete for the visible
            # slots by relevance rather than being appended below the corpus, and the page stays
            # inside its node budget. rerank_report_cards renumbers rank 1..N over the full pool;
            # the trimmed head is already contiguous 1..DISPLAY_TOP.
            view["cards"] = cards[:_DISPLAY_TOP]
            view["listwise_reranked"] = True
        except Exception:
            traceback.print_exc()
            view["cards"] = (view.get("cards") or [])[:_DISPLAY_TOP]
            view["listwise_reranked"] = False
            view["ranked_by"] = "fusion"
        # EAGER MONGO DETAIL + FIGURES (iptorch-style, item 1/2). For every displayed card, pull
        # the pre-built lemad corpus doc (figures as Google-CDN URLs + full claims/description/CPC)
        # in ONE cheap call each — no download, no OPS, no PDF raster — and fill any gap the local
        # corpus left. This is what makes drawings + full content appear on the card immediately,
        # including on the federated-only PQAI hits that carry only a title/abstract, and it fixes
        # the dropped-leading-zero pub bug that hid US-2019168875-A1's four sketches. Bounded to the
        # displayed set (<=25), runs once here before the view is cached, so reloads stay instant.
        try:
            webview.mongo_enrich_cards(view.get("cards") or [])
            view["mongo_enriched"] = True
        except Exception:
            traceback.print_exc()
        # Verify the element x reference matrix BEFORE it is cached and rendered. Until now a
        # filled cell there meant only "the retriever returned this publication for this element";
        # nothing checked that the cited passage discloses anything, and an audit measured 7 of 12
        # coordinate-backed cells as false positives. One batched LLM pass per report, cached in
        # the view, so it costs nothing on reload. Never fatal: on failure cells stay "unchecked"
        # and the template renders them as retrieval-only rather than as coverage.
        #  NOT for a chart built from the reading. verify_matrix exists to put a verdict on a cell
        #  that has none — a retrieval hit whose only evidence is a cosine — and it decides by
        #  looking the cell's coordinate up in report["element_evidence"]. A reading cell is not in
        #  there and its coord is a label, not a dict, so every cell fell to "no-coord" and the
        #  grid rendered a full-text disclosure with a verbatim quote as "whole-document match, no
        #  passage to verify". The reading's verdict IS the verification: the quote had to be found
        #  in the reference, located to a real passage, and survive an independent refuter — three
        #  gates this pass does not apply.
        try:
            if (view.get("claim_chart") or {}).get("source") != "reading":
                claim_chart.verify_matrix(view.get("claim_chart") or {}, rep)
        except Exception:
            traceback.print_exc()
        webview.prune_missing_image_files(view.get("cards") or [])
        vp.write_text(json.dumps(view, default=str))
    return view


@app.route("/status/<slug>")
def status(slug):
    """Polling fallback. Kept as the compatibility path for clients without EventSource (and for
    regression.sh); /events/<slug> is the primary, push-based channel."""
    if not _can_access_report(slug):
        abort(404)
    with _JOB_LOCK:
        job = dict(_JOBS.get(slug, {}))
    ev = _job_event(slug, job)
    # 'partial' is renderable (first cards streamed); 'done' is the final report. A cached report on
    # disk with no live job is treated as done.
    # `detail` too: the poll fallback drives the same progress narrative as the SSE path, and it
    # would otherwise silently lose the counters that SSE clients get.
    return jsonify({"ready": ev["ready"], "status": ev["status"], "done": ev["done"],
                    "msg": ev["msg"], "kind": ev["kind"], "detail": ev["detail"],
                    "elapsed_sec": ev["elapsed_sec"], "tokens": ev["tokens"]})


@app.route("/events/<slug>")
def events(slug):
    """Server-Sent Events stream of generation progress — replaces 1.5 s polling.

    nginx is already streaming-ready for this location (proxy_buffering off, proxy_read_timeout
    1800s); we additionally send X-Accel-Buffering: no so no other proxy re-buffers us, and a
    comment heartbeat every 15 s so idle connections are not reaped.
    """
    if not _can_access_report(slug):
        abort(404)
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
_RAT_VERSION = 2                  # invalidates thin, pre-full-text rationale cache entries
_RAT_BIBLIO_CHARS = 1400
_RAT_PASSAGE_CHARS = 1100         # enough context to preserve a complete claim relationship
_RAT_EVIDENCE_CHARS = 9000        # claims + description diversity, not one nearest snippet
_RAT_MAX_PASSAGES = 12
_RAT_SOURCE_CHARS = 16000         # exact verifier/audit evidence (still bounded)
# The richer answer includes the concrete overlap, the material gap, grounded reads_on entries,
# and citations.  1100 tokens frequently truncated that JSON once 8-12 passages were supplied.
_RAT_MAX_TOKENS = 1800

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
    for c in indep[:3]:
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
                (webview._vec(qvec), pid, webview._vec(qvec), int(limit * 2)),
            )
            for r in cur.fetchall():
                coord = r["coord"] if isinstance(r["coord"], dict) else (
                    json.loads(r["coord"]) if r["coord"] else None)
                add(r["kind"], coord, r["text"], float(r["score"] or 0.0))
        except Exception:
            pass

    # Guarantee a description view when one exists.  Dense similarity can fill its entire head
    # with near-duplicate claims; that is excellent retrieval but poor explanation context.
    if not any(p.get("kind") in _BODY_KINDS for p in out):
        for p in list((secs or {}).get("paragraphs") or [])[:2]:
            add("paragraph", {"para_no": p.get("para_no")}, p.get("text"))

    # Preserve evidence diversity under the hard cap: independent claims first, then body text,
    # then an abstract if present, and finally the remaining nearest chunks in their score order.
    picked = []

    def take(kinds, n):
        for p in out:
            if len([x for x in picked if x.get("kind") in kinds]) >= n:
                break
            if p.get("kind") in kinds and p not in picked:
                picked.append(p)

    take(set(_CLAIM_KINDS), 4)
    take(set(_BODY_KINDS), 4)
    take({"abstract"}, 1)
    for p in out:
        if p not in picked:
            picked.append(p)
        if len(picked) >= limit:
            break
    return picked[:limit]


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


def _write_rationale_cache(cache, result):
    """Atomic write: background warming and a user click may finish the same rationale together."""
    tmp = cache.with_name(
        f".{cache.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(result))
        os.replace(tmp, cache)
    finally:
        tmp.unlink(missing_ok=True)


def _rationale(slug, pub, query, elements, biblio_txt, matched_txt=None, passages=None):
    cache = RATIONALE / f"{slug}__{pub}.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            if cached.get("_version") == _RAT_VERSION:
                return cached
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
        res = {"_version": _RAT_VERSION,
               "why": "Reference text was not available to verify relevance; treat as unconfirmed.",
               "reads_on": [], "citations": [], "text_basis": "title-only", "n_passages": 0,
               "why_grounding": "no-source", "_source_text": "", "grounding_diag": []}
        _write_rationale_cache(cache, res)
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
        '"why" = 2-3 concise sentences that (1) identify the SPECIFIC mechanism, component '
        "relationship, or control behaviour that overlaps the user's disclosure, (2) cite where "
        "the strongest support appears (claim or paragraph when tagged), and (3) state the most "
        "important query limitation that the supplied reference text does NOT establish. Use the "
        "actual technical nouns and relationships, not generic phrases such as 'same field' or "
        "'similar system'. Cite or closely paraphrase wording that actually appears in the text, "
        "HEDGED ('appears to', 'the abstract mentions') on partial matches. Every AFFIRMATIVE "
        'overlap you name in "why" must be one you can also ground in reads_on. You may name an '
        'unshown query limitation only when explicitly saying the supplied reference text does NOT '
        'establish it. "reads_on" = a list of objects {"element":"<one invention '
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
    usr = (f"Invention disclosure: {query[:2000]}\n\nInvention elements (candidates — include only the "
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
    res = {"_version": _RAT_VERSION,
           "why": why, "reads_on": reads_on, "why_grounding": why_state,
           "citations": citations, "text_basis": basis,
           "n_passages": len(shown),
           # The EXACT text the generator was shown. The audit judge previously rebuilt its own
           # reference text (title+abstract+claim 1) and so graded the rationale against text the
           # generator never saw -- that desync alone inflated the measured rate. Persisting the
           # real input lets audit.judge_rationale grade like-for-like.
           "_source_text": source[:_RAT_SOURCE_CHARS],
           "grounding_diag": diag}
    _write_rationale_cache(cache, res)
    return res


_REPORT_ANALYSIS_POOL = None
_REPORT_ANALYSIS_LOCK = threading.Lock()
_REPORT_ANALYSIS_RUNNING = set()


def _report_analysis_pool():
    global _REPORT_ANALYSIS_POOL
    with _REPORT_ANALYSIS_LOCK:
        if _REPORT_ANALYSIS_POOL is None:
            _REPORT_ANALYSIS_POOL = _cf.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="report-analysis")
        return _REPORT_ANALYSIS_POOL


def _warm_one_rationale(slug, pub, query, elements):
    """Build the same full-text-grounded cache as /api/ref without a browser request."""
    cache = RATIONALE / f"{slug}__{pub}.json"
    if cache.exists():
        try:
            if json.loads(cache.read_text()).get("_version") == _RAT_VERSION:
                return
        except Exception:
            pass
    disp = enrich_display.enrich_for_display(pub)
    secs, passages = None, []
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
        row = cur.fetchone()
        if row:
            secs = webview.sections(cur, row["id"])
            qv = _query_vec(slug, query)
            passages = ref_passages(cur, row["id"], qv, secs)
    if secs is None and (disp.get("claims") or disp.get("description")):
        secs = {
            "claims": [{"claim_no": i + 1, "text": c} for i, c in
                       enumerate(disp.get("claims") or []) if c],
            "paragraphs": [{"para_no": None, "text": p} for p in
                           (disp.get("description") or []) if p],
        }
    if not passages and secs:
        passages = [
            {"kind": "claim_own", "coord": {"claim_no": c.get("claim_no")},
             "text": c.get("resolved_text") or c.get("text")}
            for c in (secs.get("claims") or [])[:4]
        ]
        passages.extend({"kind": "paragraph", "coord": {"para_no": p.get("para_no")},
                         "text": p.get("text")}
                        for p in (secs.get("paragraphs") or [])[:4])
    biblio = f"{pub} {disp.get('title') or ''}. {disp.get('abstract') or ''}"
    _rationale(slug, pub, query, elements, biblio, passages=passages)


def _background_report_analysis(slug, pubs):
    try:
        rep = _load_report(slug) or {}
        query, elements = rep.get("query") or "", rep.get("elements") or []
        if not query:
            return
        for pub in pubs:
            try:
                _warm_one_rationale(slug, pub, query, elements)
            except Exception:
                traceback.print_exc()
    finally:
        with _REPORT_ANALYSIS_LOCK:
            _REPORT_ANALYSIS_RUNNING.discard(slug)


def _schedule_background_report_analysis(slug, pubs):
    """One bounded server-side task per report; survives a closed browser tab."""
    pubs = [pub for pub in pubs if _safe_pub(pub)][:8]
    if not pubs:
        return False
    pool = _report_analysis_pool()
    with _REPORT_ANALYSIS_LOCK:
        if slug in _REPORT_ANALYSIS_RUNNING:
            return False
        _REPORT_ANALYSIS_RUNNING.add(slug)
    pool.submit(_background_report_analysis, slug, pubs)
    return True


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


@app.route("/api/ref-batch/<slug>")
def api_ref_batch(slug):
    """Return already-built tab text for the visible result cards in one cheap request."""
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    if not _may_read_report(slug):
        abort(404)
    path = REPORTS / f"{slug}.detail-preview.json"
    if not path.exists():
        return jsonify({"items": {}, "ready": False}), 202
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return jsonify({"items": {}, "ready": False}), 202
    wanted = [p for p in request.args.get("pubs", "").split(",") if _safe_pub(p)][:_DISPLAY_TOP]
    items = payload.get("items") or {}
    if wanted:
        items = {pub: items[pub] for pub in wanted if pub in items}
    section = request.args.get("section", "").strip()
    if section in {"abstract", "claims", "desc", "class", "figs", "why"}:
        items = {pub: _detail_preview_section(item, section) for pub, item in items.items()}
    return jsonify({"items": items, "ready": True, "partial": bool(payload.get("partial"))})


@app.route("/api/ref/<pub>")
def api_ref(pub):
    slug = request.args.get("slug", "")
    # Optional, but when present it reaches _rationale() which WRITES rationale/<slug>__<pub>.json.
    # Query strings bypass the route converter, so vet it here.
    if slug and not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    if slug and not _may_read_report(slug):
        abort(404)
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
    # fall back to SerpApi/Mongo claims when DB has none
    if secs is not None and not secs["claims"] and disp.get("claims"):
        secs["claims"] = [{"claim_no": i + 1, "independent": None, "text": c, "resolved_text": None}
                          for i, c in enumerate(disp["claims"])]
    # Federated / out-of-corpus pubs have NO Postgres row, so `secs` is None even though the lemad
    # corpus carries the full document. Synthesize a sections dict from the Mongo-backed display so
    # the Claims / Description panes and the full-document view render uniformly instead of showing
    # "no full text" for a pub the corpus clearly has. Mongo claims/description are plain strings.
    if secs is None and (disp.get("claims") or disp.get("description")):
        secs = {
            "claims": [{"claim_no": i + 1, "independent": None, "text": c, "resolved_text": None}
                       for i, c in enumerate(disp.get("claims") or []) if c],
            "paragraphs": [{"para_no": None, "heading": None, "text": t}
                           for t in (disp.get("description") or []) if t],
            "figures": [],
        }
    rationale = None
    # `light=1` normally returns everything EXCEPT the grounded opinion. The results list needs sections
    # (claims / description) to fill a card's expandable panes, and _rationale() runs a Vertex call
    # for any pub that has no cached opinion yet — so without this, merely opening the Claims tab
    # (or lazily hydrating a card) would spend LLM budget the user never asked for. The opinion is
    # still fetched eagerly by the "Why relevant" pane and the full detail view. The automatic
    # background warmer sends `light=1&rationale=1`: it asks for the opinion while keeping the
    # unrelated worldwide-family lookup cache-only.
    if slug and (request.args.get("light") != "1" or request.args.get("rationale") == "1"):
        q = _query_for_slug(slug)
        rep = _load_report(slug)
        if q and rep:
            biblio_txt = f"{pub} {disp.get('title') or ''}. {disp.get('abstract') or ''}"
            # Federated/Mongo-only references have no vector-ranked local chunks.  Still give the
            # analyst a balanced full-text slice instead of silently degrading to claims 1-2.
            if not rat_passages and secs:
                rat_passages = [
                    {"kind": "claim_own", "coord": {"claim_no": c.get("claim_no")},
                     "text": c.get("resolved_text") or c.get("text")}
                    for c in (secs.get("claims") or [])[:4]
                ]
                rat_passages.extend({
                    "kind": "paragraph", "coord": {"para_no": p.get("para_no")},
                    "text": p.get("text"),
                } for p in (secs.get("paragraphs") or [])[:4])
            rationale = _rationale(slug, pub, q, rep.get("elements", []), biblio_txt,
                                   passages=rat_passages)
    # Pure-heuristic language flag: costs nothing, so it is safe on every card. The actual
    # translation stays behind its own lazy endpoint.
    disp["lang_flags"] = {"abstract": translate.looks_nonenglish(disp.get("abstract") or "")}
    # Worldwide family timeline (Feature 1). Lazy on card-open, exactly like drawings: authoritative
    # EPO OPS INPADOC family, cached forever. A Lens family already on the display supplements it
    # without a second network call. Never fatal — a failure just leaves the corpus-only baseline.
    try:
        # `light=1` is what the eager per-card warm and the text-only panes use; a live OPS family
        # fetch there would fan an INPADOC request per visible card against the shared weekly OPS
        # budget. In light mode read the disk cache only (populated by the bounded prefetch worker /
        # a full card-open); a full (non-light) open still fetches the authoritative worldwide family.
        if request.args.get("light") == "1":
            disp["family"] = ops_family.load_cached(pub)
        else:
            disp["family"] = ops_family.fetch_family(pub, lens_family=disp.get("lens_family"))
    except Exception:
        disp["family"] = None
    payload = {
        "pub": pub, "display": disp, "sections": secs,
        "matched": {"coord": webview._coord_str((matched or {}).get("coord")),
                    "kind": (matched or {}).get("kind"),
                    "score": round((matched or {}).get("score", 0) or 0, 3),
                    "coord_raw": (matched or {}).get("coord")} if matched else None,
        "rationale": rationale,
    }
    section = request.args.get("section", "").strip()
    if section in {"abstract", "claims", "desc", "class", "figs", "why"}:
        payload = _detail_preview_section(payload, section)
    return jsonify(payload)


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


# A publication number can be canonical ("US-11207792-B2") or a compact identifier returned by
# a federated provider ("US20220256273A1"); a figure filename is like "003.png". Validate both
# BEFORE any path use — defense-in-depth against traversal on top of Flask's safe_join.
_PUB_RE = re.compile(
    r"^[A-Za-z]{2}(?:[A-Za-z0-9]{3,38}|-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)$"
)
_FNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _safe_pub(pub):
    return bool(pub) and len(pub) <= 40 and bool(_PUB_RE.match(pub))


@app.route("/figures/<pub>/<path:fname>")
def figures(pub, fname):
    if not _safe_pub(pub) or not _FNAME_RE.match(fname):   # reject traversal / odd names early
        abort(404)
    # Recovery persists assets under the corpus' hyphenated key even when a federated result is
    # compact (US20220256273A1). Keep the public URL stable but serve from that shared directory.
    d = enrich_display.FIGDIR / enrich_display._canonical_pubkey(pub)
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
        # Disk figures first; if none are downloaded, union the lemad Mongo remote-CDN thumbnails
        # so a Mongo-served pub shows a sketch on the list without any download or recovery round-
        # trip (enrich_display.remote_thumbs reads only the cached display, no network).
        imgs = webview._cached_images(pub)
        if not imgs:
            try:
                imgs = enrich_display.remote_thumbs(pub)
            except Exception:
                imgs = []
        out[pub] = imgs
    return jsonify(out)


@app.route("/api/family")
def api_family():
    """Batched worldwide-family manifest for the results list — DISK CACHE ONLY, no network.

    Mirrors /api/figs: the page calls this for the top-N prefetched cards to upgrade their
    corpus-only baseline strip to the authoritative EPO OPS INPADOC timeline once the prefetch
    worker has cached it. Returns null for a pub not yet resolved (the card keeps its baseline)
    and never itself spends an OPS request — the live fetch happens in prefetch / /api/ref only.
    """
    pubs = [p for p in (request.args.get("pubs") or "").split(",") if p][:80]
    out = {}
    for pub in pubs:
        if not _safe_pub(pub):
            continue
        fam = ops_family.load_cached(pub)
        # Only advertise an authoritative (non-partial, has-codes) family; a cached "none"/empty
        # answer must not clobber the server-rendered corpus baseline on the card.
        if fam and fam.get("timeline") and fam.get("source") in ("ops", "lens"):
            out[pub] = fam
    return jsonify(out)


@app.route("/api/prefetch/<slug>", methods=["GET", "POST"])
def api_prefetch(slug):
    """Kick off (POST) or poll (GET) proactive top-N enrichment for a report.

    POST bounds itself to the report's own top-N ranked cards and returns the scheduled pub list;
    work runs on a shared bounded pool and never blocks this request or the render. GET returns
    progress so the page can stop polling /api/figs + /api/family once prefetch has finished.
    """
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    if not _can_access_report(slug):
        abort(404)
    if request.method == "GET":
        return jsonify(prefetch.status(slug))
    # POST: derive the top-N from the (already reranked + cached) view, so N tracks the listwise
    # order and can never be spoofed by the client.
    vp = REPORTS / f"{slug}.view.json"
    pubs = []
    if vp.exists():
        try:
            view = json.loads(vp.read_text())
            pubs = [c.get("pub") for c in (view.get("cards") or []) if c.get("pub")]
        except Exception:
            pubs = []
    # Prefetch EVERY displayed card (iptorch-style), not just the top handful: Mongo figures cost
    # no download and no OPS, so eager enrichment of the whole shown set is cheap. The ~half that
    # Mongo misses (older EP/DE/WO/CN) still go through the bounded, OPS-budget-guarded recovery
    # worker, so this cannot hammer the shared quota.
    return jsonify(prefetch.prefetch_top(slug, pubs, n=len(pubs)))


@app.route("/analysis/<slug>")
def analysis_page(slug):
    """Every reference's two tables on one page — the reading, end to end.

    The per-card tab answers "what does THIS one disclose". This answers the other question an
    attorney actually has: across everything that was read, which feature is disclosed where, and
    what did nothing reach. It is also the printable form of the analysis.
    """
    if not _can_access_report(slug):
        abort(404)
    rep = _load_report(slug)
    if not rep:
        abort(404)
    data = deep_analysis.result(slug, REPORTS)
    view = _build_view_cached(slug, rep)
    user = auth.current_user()
    account_search = accounts.get_search(user["id"], slug) if user else None
    if not data:
        try:
            deep_analysis.ensure(slug, rep, view, REPORTS)
        except Exception:
            traceback.print_exc()
    return render_template("analysis.html", slug=slug, data=data,
                           status=deep_analysis.status(slug, REPORTS),
                           title=(account_search or {}).get("title") or slug,
                           query=rep.get("query", ""))


@app.route("/api/deep/<slug>", methods=["GET", "POST"])
def api_deep_analysis(slug):
    """The full-text reading of the top references: status while it runs, the charts when done.

    POST starts it (or restarts it after a re-run); GET polls. The payload is large — fifty
    references with a quote per cell — so a `?pub=` filter returns just one reference for a card
    that has been opened, and the summary is returned without the reference bodies unless asked.
    """
    if not _can_access_report(slug):
        abort(404)
    rep = _load_report(slug)
    if not rep:
        return jsonify({"status": "missing"}), 404
    if request.method == "POST":
        if auth.current_user():
            auth.require_csrf()
        view = _build_view_cached(slug, rep)
        return jsonify(deep_analysis.ensure(slug, rep, view, REPORTS))

    data = deep_analysis.result(slug, REPORTS)
    if not data:
        return jsonify(deep_analysis.status(slug, REPORTS))
    pub = (request.args.get("pub") or "").strip()
    if pub:
        one = next((r for r in data.get("references") or [] if r.get("pub") == pub), None)
        if not one:
            return jsonify({"status": "done", "found": False, "pub": pub})
        return jsonify({"status": "done", "found": True, "reference": one,
                        "features": data.get("features") or [],
                        "claims": data.get("claims") or []})
    if request.args.get("full") == "1":
        return jsonify(data)
    #  Summary only: the per-reference bodies are the bulk and the page fetches them per card.
    light = {k: v for k, v in data.items() if k != "references"}
    light["references"] = [{"pub": r.get("pub"), "rank": r.get("rank"),
                            "title": r.get("title"), "method": r.get("method"),
                            "counts": r.get("counts") or {},
                            "chars": r.get("chars"), "refuted": r.get("refuted"),
                            "text_truncated": r.get("text_truncated")}
                           for r in data.get("references") or []]
    return jsonify(light)


@app.route("/api/query-claim-grid/<slug>", methods=["GET", "POST"])
def api_query_claim_grid(slug):
    """Start or poll the uploaded Claim x Reference grid without blocking the report page."""
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    if not _can_access_report(slug):
        abort(404)
    if request.method == "GET":
        return jsonify(query_claim_grid.status(slug, REPORTS))

    rep = _load_report(slug)
    if not rep:
        return jsonify({"error": "report not found"}), 404
    vp = REPORTS / f"{slug}.view.json"
    if not vp.exists():
        return jsonify({"status": "waiting", "available": True,
                        "reason": "final ranking is not ready"}), 202
    try:
        view = json.loads(vp.read_text())
    except Exception:
        return jsonify({"status": "waiting", "available": True,
                        "reason": "final ranking is not ready"}), 202
    return jsonify(query_claim_grid.ensure(slug, rep, view, REPORTS))


def _pdf_available(pub: str) -> bool:
    """Would /pdf/<pub> actually serve something? Same two sources the route itself uses."""
    if not _safe_pub(pub):
        return False
    canon = enrich_display._canonical_pubkey(pub)
    if (enrich_display.PDFDIR / f"{canon}.pdf").exists():
        return True
    disp = enrich_display.load_cached(canon)
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
    canon = enrich_display._canonical_pubkey(pub)
    f = enrich_display.PDFDIR / f"{canon}.pdf"
    if f.exists():
        return send_from_directory(enrich_display.PDFDIR, f"{canon}.pdf",
                                   mimetype="application/pdf")
    # fall back to remote pdf if we have it cached in enriched json
    disp = enrich_display.load_cached(canon)
    url = (disp or {}).get("_display", {}).get("pdf_url") if disp else None
    if url:
        return redirect(url)
    abort(404)


@app.route("/print/<slug>")
def print_view(slug):
    if not _can_access_report(slug):
        abort(404)
    rep = _load_report(slug)
    if not rep:
        abort(404)
    #  _build_view_cached, not build_view: the per-cell disclosure verdicts are applied there.
    #  Calling build_view directly is why the print view rendered cells that nothing had checked.
    view = _build_view_cached(slug, rep)
    view["slug"] = slug
    user = auth.current_user()
    account_search = accounts.get_search(user["id"], slug) if user else None
    view["title"] = (account_search or {}).get("title") or slug
    view["account_search"] = account_search
    view["query_claim_grid_data"] = query_claim_grid.status(slug, REPORTS)
    view["printed_at"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
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
    if not _can_access_report(slug):
        abort(404)
    user = auth.current_user()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        pub = data.get("pub")
        if not pub:
            return jsonify({"ok": False}), 400
        if user:
            auth.require_csrf()
            try:
                entry = accounts.save_report_flag(
                    user["id"], slug, pub,
                    flag=data.get("flag") if "flag" in data else None,
                    note=data.get("note") if "note" in data else None)
                return jsonify({"ok": True, "flags": accounts.load_report_flags(user["id"], slug),
                                "entry": entry})
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        flags = load_flags(slug)
        entry = flags.get(pub, {})
        if "flag" in data:
            entry["flag"] = data["flag"]          # relevant | maybe | not | ""
        if "note" in data:
            entry["note"] = data["note"]
        flags[pub] = entry
        _flags_path(slug).write_text(json.dumps(flags, indent=1))
        return jsonify({"ok": True, "flags": flags})
    if user:
        return jsonify(accounts.load_report_flags(user["id"], slug))
    return jsonify(load_flags(slug))


# ---- patent figures for a draft: generate, edit, keep every version -------------------------
def _figure_project(principal, project_id):
    """The project, checked against this principal — figures inherit the draft's permissions."""
    return _drafting_service().get_project(principal, project_id)


def _figure_in_project(user_id, project_id, figure_id):
    """Resolve a figure only inside the project named by the URL."""
    figure = draft_figures.get_figure(figure_id, user_id)
    if not figure or int(figure.get("project_id") or 0) != int(project_id):
        abort(404)
    return figure


@app.route("/drafts/<int:project_id>/figures", methods=["POST"])
def draft_figure_generate(project_id):
    """Generate a new figure, or apply a change to an existing one.

    Synchronous on purpose: one image is ~5 s, which is inside a request, and a job queue for it
    would add a status-polling surface for something the user is watching happen.
    """
    try:
        user, principal = _draft_identity()
    except drafting.DraftingError as exc:
        return _error_response({"error": str(exc)}, _draft_error_status(exc), str(exc))
    auth.require_csrf()
    try:
        project = _figure_project(principal, project_id)
    except drafting.DraftingError as exc:
        return _error_response({"error": str(exc)}, _draft_error_status(exc), str(exc))

    body = request.get_json(silent=True) or request.form.to_dict() or {}
    label = str(body.get("label") or "").strip()[:80]
    caption = str(body.get("caption") or "").strip()[:400]
    instruction = str(body.get("instruction") or "").strip()[:1000]
    figure_id = body.get("figure_id")
    figure_id = int(figure_id) if str(figure_id or "").isdigit() else None
    if not figure_id and not (label or caption):
        return jsonify({"ok": False, "error": "describe the figure first"}), 400
    if not figure_id and not label:
        label = "FIG. %d" % (len(_figures_for(project)) + 1)

    version = None
    try:
        version = _drafting_service().get_project(principal, project_id, include_versions=True)
        latest = int(version.get("latest_version_no") or 0)
        sections = next((v.get("sections") or {} for v in version.get("versions", [])
                         if int(v.get("version_no") or 0) == latest), {})
    except Exception:
        sections = {}
    try:
        out = draft_figures.render_figure(
            project["id"], project["user_id"], label=label, caption=caption, sections=sections,
            instruction=instruction, figure_id=figure_id,
            #  Before the specification is generated the inventor's disclosure is the ONLY place
            #  the reference numerals exist, and it is usually where they originated.
            disclosure=str(project.get("disclosure_text") or "")[:40000])
    except draft_figures.FigureError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"could not draw that: {str(exc)[:180]}"}), 500
    return jsonify({"ok": True, **out})


@app.route("/drafts/<int:project_id>/figures/<int:figure_id>.png")
def draft_figure_png(project_id, figure_id):
    user, principal = _draft_identity()
    _figure_project(principal, project_id)
    _figure_in_project(user["id"], project_id, figure_id)
    version = request.args.get("version", type=int)
    mime, data = draft_figures.png_bytes(figure_id, user["id"], version)
    if not data:
        abort(404)
    return Response(data, mimetype=mime or "image/png",
                    headers={"Cache-Control": "private, max-age=300"})


@app.route("/drafts/<int:project_id>/figures/<int:figure_id>/activate", methods=["POST"])
def draft_figure_activate(project_id, figure_id):
    user, principal = _draft_identity()
    auth.require_csrf()
    _figure_project(principal, project_id)
    _figure_in_project(user["id"], project_id, figure_id)
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        n = int(body.get("version_no"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "which version?"}), 400
    if not draft_figures.set_active(figure_id, user["id"], n):
        return jsonify({"ok": False, "error": "no such version"}), 404
    return jsonify({"ok": True, "version_no": n})


@app.route("/drafts/<int:project_id>/figures/<int:figure_id>/delete", methods=["POST"])
def draft_figure_delete(project_id, figure_id):
    user, principal = _draft_identity()
    auth.require_csrf()
    _figure_project(principal, project_id)
    _figure_in_project(user["id"], project_id, figure_id)
    draft_figures.delete_figure(figure_id, user["id"])
    return redirect(url_for("draft_detail", project_id=project_id, message="Figure deleted."))


MORE_REFERENCES_PAGE = 25
MORE_REFERENCES_MAX = 300


@app.route("/api/more-references/<slug>")
def api_more_references(slug):
    """The ranked tail beyond the cards the page shows.

    The report ranks thousands of families but builds full cards for the top 25 only, because a
    card costs a database resolution, a drawing, a claim match and a grounded explanation. That
    is the right default — semantic ranking puts the art at the top — but "there are 2,186
    families and you may see 25 of them" is not something a searcher should have to accept on
    trust when they are looking for one specific document.

    So this resolves the tail CHEAPLY: one batched query for each family's representative
    publication, returning bibliographic rows and links, with no rerank, no drawing fetch and no
    explanation. The response says plainly that these are ranked but not analysed.
    """
    if not _can_access_report(slug):
        abort(404)
    rep = _load_report(slug)
    if not rep:
        return jsonify({"ok": False, "error": "no report"}), 404
    try:
        offset = max(0, min(int(request.args.get("offset", MORE_REFERENCES_PAGE)),
                            MORE_REFERENCES_MAX))
        limit = max(1, min(int(request.args.get("limit", MORE_REFERENCES_PAGE)),
                           MORE_REFERENCES_PAGE))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad offset or limit"}), 400

    ranked = rep.get("ranked_families") or []
    page = ranked[offset:offset + limit]
    if not page:
        return jsonify({"ok": True, "rows": [], "offset": offset, "total": len(ranked),
                        "exhausted": True})
    conn = db.connect()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        reps = webview.resolve_family_reps(cur, page)
    finally:
        conn.close()
    rows = []
    for i, fam in enumerate(page):
        r = reps.get(fam)
        if not r:
            continue                      # a federated-only family has no local publication row
        pub = r["publication_number"]
        rows.append({
            "rank": offset + i + 1, "pub": pub, "title": r.get("title") or "",
            "country": r.get("country") or "",
            "publication_date": str(r.get("publication_date") or "")[:10],
            "priority_date": str(r.get("earliest_priority_date") or "")[:10],
            "google_patents": pubnorm.google_url(pub),
            "espacenet": pubnorm.espacenet_url(pub, r.get("simple_family_id")),
        })
    return jsonify({"ok": True, "rows": rows, "offset": offset, "next": offset + limit,
                    "total": len(ranked),
                    "exhausted": offset + limit >= min(len(ranked), MORE_REFERENCES_MAX)})


@app.route("/api/improve-query", methods=["POST"])
def api_improve_query():
    """Rewrite a typed query into the vocabulary the corpus is written in.

    Rate-limited with the other model-spending endpoints. Returns the improvement AND what was
    added, because an expansion the user cannot inspect is one they cannot reject.
    """
    if auth.current_user():
        auth.require_csrf()
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    text = str(body.get("query") or "").strip()
    if len(text) < 8:
        return jsonify({"ok": False, "error": "write a little more first"}), 400
    try:
        out = llm.improve_query(text[:16000])
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"could not improve that: {str(exc)[:160]}"}), 500
    if not out.get("changed"):
        return jsonify({"ok": True, "changed": False, "improved": out["improved"],
                        "added": [], "questions": out.get("questions") or []})
    return jsonify({"ok": True, "changed": True, **out})


# ---- the saved-patent library: references that outlive the search that found them ------------
@app.route("/library")
def saved_patents():
    user = _require_user()
    q = (request.args.get("q") or "").strip()
    rows = library.listing(user["id"], query=q)
    #  Resolve each publication's display record lazily and fail soft: a library entry must still
    #  list when the corpus has never seen that number (a federated-only hit, say).
    for r in rows:
        try:
            disp = enrich_display.load_cached(r["publication_number"]) or {}
        except Exception:
            disp = {}
        r["display_title"] = r.get("title") or disp.get("title") or ""
        r["google_patents"] = pubnorm.google_url(r["publication_number"])
    return render_template("patents.html", rows=rows, q=q, n=len(rows))


@app.route("/api/library", methods=["POST"])
def api_library():
    """Save, annotate or remove one publication. Used by the report cards and the library page."""
    user = _require_user()
    auth.require_csrf()
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    action = (body.get("action") or "save").strip()
    pub = body.get("pub") or body.get("publication_number") or ""
    try:
        if action == "remove":
            removed = library.remove(user["id"], pub)
            return jsonify({"ok": True, "saved": False, "removed": removed,
                            "count": library.count(user["id"])})
        if action == "note":
            row = library.update_note(user["id"], pub, body.get("note") or "")
            if not row:
                return jsonify({"ok": False, "error": "not in your library"}), 404
            return jsonify({"ok": True, "saved": True})
        row = library.save(user["id"], pub, title=body.get("title") or "",
                           note=body.get("note") or "", tag=body.get("tag") or "",
                           source_slug=body.get("slug") or "")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    return jsonify({"ok": True, "saved": True, "pub": row["publication_number"],
                    "count": library.count(user["id"])})


@app.route("/api/library/state")
def api_library_state():
    """Which of the publications on this page are already saved — one call per report render."""
    user = auth.current_user()
    if not user:
        return jsonify({"saved": [], "count": 0})
    pubs = [p for p in (request.args.get("pubs") or "").split(",") if p.strip()]
    try:
        return jsonify({"saved": sorted(library.saved_set(user["id"], pubs)),
                        "count": library.count(user["id"])})
    except Exception:
        traceback.print_exc()
        return jsonify({"saved": [], "count": 0})


# ---- the client-facing document: letterhead, matter, narrative, share link -------------------
def _require_user():
    """The signed-in account, or refuse.

    The before_request gate already turns anonymous traffic away, with one deliberate exception:
    loopback on this host is trusted so the box can drive its own API. These routes all write
    rows keyed by user id, so they need a real account rather than that exemption.
    """
    user = auth.current_user()
    if user:
        return user
    if _wants_json():
        abort(Response(json.dumps({"ok": False, "error": "a named account is required"}),
                       status=401, mimetype="application/json"))
    abort(redirect(url_for("auth.login", next=request.full_path.rstrip("?") or "/")))


def _report_doc(slug):
    """This user's report document for `slug`, or None. Never raises — a missing accounts store
    must degrade to a plain retrieval export, not a 500 on the download button."""
    user = auth.current_user()
    if not user:
        return None
    try:
        return deliverables.get(user["id"], slug)
    except Exception:
        traceback.print_exc()
        return None


def _report_logo_path(slug):
    """Materialise the stored logo as a file the PDF/DOCX writers can embed, or None.

    reportlab and python-docx both want a path or a stream; keeping one cached file per report
    avoids re-writing it for every one of the four export formats."""
    user = auth.current_user()
    if not user:
        return None
    try:
        mime, data = deliverables.logo(user["id"], slug)
    except Exception:
        return None
    if not data:
        return None
    ext = deliverables.LOGO_MIMES.get(mime, ".png")
    p = LOGOS / f"{slug}-{user['id']}{ext}"
    try:
        if not p.exists() or p.stat().st_size != len(data):
            p.write_bytes(data)
        return str(p)
    except Exception:
        return None


@app.route("/report/<slug>/details", methods=["GET", "POST"])
def report_details(slug):
    """Edit the letterhead, matter details and narrative that head the exported document."""
    user = _require_user()
    if not _can_access_report(slug):
        abort(404)
    error = ""
    if request.method == "POST":
        auth.require_csrf()
        try:
            deliverables.save(user["id"], slug, request.form.to_dict())
            if _wants_json():
                return jsonify({"ok": True})
            return redirect(url_for("report_details", slug=slug, saved=1))
        except Exception as exc:
            traceback.print_exc()
            error = str(exc)[:200]
    doc = deliverables.get_or_create(user["id"], slug)
    rep = _load_report(slug) or {}
    account_search = accounts.get_search(user["id"], slug) if user else None
    return render_template("report_details.html", slug=slug, doc=doc, error=error,
                           saved=request.args.get("saved") == "1",
                           query=rep.get("query", ""),
                           title=(account_search or {}).get("title") or slug,
                           letterhead_fields=deliverables.LETTERHEAD_FIELDS,
                           matter_fields=deliverables.MATTER_FIELDS)


@app.route("/report/<slug>/logo", methods=["POST"])
def report_logo_upload(slug):
    user = _require_user()
    auth.require_csrf()
    if not _can_access_report(slug):
        abort(404)
    if request.form.get("action") == "clear":
        deliverables.clear_logo(user["id"], slug)
        return redirect(url_for("report_details", slug=slug, saved=1))
    f = request.files.get("logo")
    if f is None or not (f.filename or "").strip():
        return _error_response({"error": "no file"}, 400, "Choose a logo image first.")
    data = f.read(deliverables.MAX_LOGO_BYTES + 1)
    try:
        deliverables.set_logo(user["id"], slug, data, f.mimetype or "")
    except ValueError as exc:
        return _error_response({"error": str(exc)}, 400, str(exc))
    return redirect(url_for("report_details", slug=slug, saved=1))


@app.route("/report/<slug>/logo.img")
def report_logo(slug):
    user = _require_user()
    if not _can_access_report(slug):
        abort(404)
    mime, data = deliverables.logo(user["id"], slug)
    if not data:
        abort(404)
    return Response(data, mimetype=mime or "image/png",
                    headers={"Cache-Control": "private, max-age=60"})


@app.route("/api/report/<slug>/suggest/<kind>", methods=["POST"])
def api_report_suggest(slug, kind):
    """Draft the purpose / key findings / analysis from the report itself."""
    _require_user()
    auth.require_csrf()
    if not _can_access_report(slug):
        abort(404)
    if kind not in deliverables.NARRATIVE_FIELDS:
        return jsonify({"ok": False, "error": "unknown section"}), 400
    rep = _load_report(slug)
    if not rep:
        return jsonify({"ok": False, "error": "no report yet"}), 404
    try:
        view = _build_view_cached(slug, rep)
        text = deliverables.suggest(kind, view, rep)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"could not draft that section: {str(exc)[:160]}"}), 500
    if not text:
        return jsonify({"ok": False, "error": "the model returned nothing for this section"}), 502
    return jsonify({"ok": True, "text": text})


@app.route("/report/<slug>/share", methods=["POST"])
def report_share(slug):
    """Mint or revoke the read-only link. The token is shown ONCE, at mint time."""
    user = _require_user()
    auth.require_csrf()
    if not _can_access_report(slug):
        abort(404)
    if (request.get_json(silent=True) or {}).get("revoke") or request.form.get("revoke"):
        deliverables.revoke_share(user["id"], slug)
        return jsonify({"ok": True, "shared": False})
    token = deliverables.create_share(user["id"], slug)
    return jsonify({"ok": True, "shared": True,
                    "url": f"{notifications.PUBLIC_BASE_URL}/shared/{token}"})


@app.route("/shared/<token>")
def shared_report(token):
    """A read-only report for somebody with the link and no account.

    The token is a capability for ONE report. It carries no session, cannot be swapped for
    another slug, and every mutating route stays behind the normal login — this view renders the
    same report template with editing, exporting and re-running switched off.
    """
    doc = deliverables.by_share_token(token)
    if not doc:
        abort(404)
    slug = doc["slug"]
    rep = _load_report(slug)
    if not rep:
        abort(404)
    view = _build_view_cached(slug, rep)
    #  Fill the SAME keys the owner's report route fills. Hand-rolling a subset is how the first
    #  version of this shipped, and it 500'd on `v.archive` — a key the template reads and this
    #  route had never heard of. Anything the template can read must be present here too.
    view["slug"] = slug
    view["title"] = doc.get("matter_title") or slug
    view["is_gold"] = slug in _GOLD
    view["search_focus"] = rep.get("search_focus") or "all_text"
    view["doc_token"] = ""
    view["account_search"] = None
    view["archive"] = report_archive.metadata(slug, REPORTS)
    view["query_claim_grid_data"] = query_claim_grid.status(slug, REPORTS)
    view["report_doc"] = doc
    view["share_token"] = token
    view["read_only"] = True
    view["concise_docs"] = _concise_built(slug)
    return render_template("report.html", v=view, read_only=True, share_token=token,
                           ood=None, corpus=corpus_facts.facts())


# ---- publish one report at a public URL, and record who reads it ---------------------------
@app.route("/report/<slug>/publish", methods=["POST"])
def report_publish(slug):
    """Mint, re-password or revoke the public link. Owner only.

    Separate from `/report/<slug>/share`, which mints an unguessable token at `/shared/<token>`.
    This one is the STABLE, readable URL an owner can put in an email and later take back, and it
    is the only thing that makes a slug resolve publicly at all.
    """
    user = _require_user()
    auth.require_csrf()
    if not _can_access_report(slug):
        abort(404)
    body = request.get_json(silent=True) or request.form or {}
    if body.get("revoke"):
        public_report.unpublish(user["id"], slug)
        return jsonify({"ok": True, "published": False})
    password = (body.get("password") or "").strip()
    clear = bool(body.get("clear_password"))
    rep = _load_report(slug) or {}
    row = public_report.publish(user["id"], slug, password=password or None,
                                title=(rep.get("query") or "")[:200], clear_password=clear)
    if row.get("error") == "already_published_by_another_user":
        return jsonify({"ok": False, "error": "Someone else has already published this report. "
                                              "Ask them to revoke their link first."}), 409
    if not row:
        abort(404)
    return jsonify({"ok": True, "published": True,
                    "has_password": bool(row.get("password_hash")),
                    "url": f"{notifications.PUBLIC_BASE_URL}/public-report/{slug}"})


def _public_unlocked(slug) -> bool:
    """Has this browser already answered this link's password?

    Kept in the signed session cookie under a per-slug key, so unlocking one report does not unlock
    another and a stolen cookie is worth exactly the link the visitor already had.
    """
    return slug in (session.get("public_unlocked") or [])


@app.route("/public-report/<slug>")
def public_report_page(slug):
    """The report as its owner sees it, with the application taken away.

    Renders the SAME template as the owner's page through a stripped layout, so the evidence
    cannot drift between the two. Everything that mutates, exports, re-runs or navigates into the
    application is off: `read_only` gates those, and `base_public.html` has no navigation at all.
    """
    pub = public_report.get(slug)
    if not pub:
        #  A revoked link, a never-published one and a slug that does not exist must all look the
        #  same from out here. Anything else tells a stranger which reports are real.
        abort(404)
    if pub.get("password_hash") and not _public_unlocked(slug):
        return render_template("public_gate.html", slug=slug, error=None,
                               corpus=corpus_facts.facts()), 401
    rep = _load_report(slug)
    if not rep:
        abort(404)
    view = _build_view_cached(slug, rep)
    #  The same keys the owner's route fills. A hand-rolled subset is how the token share first
    #  shipped and it 500'd on a key the template reads and the route had never heard of.
    view["slug"] = slug
    view["title"] = pub.get("title") or slug
    view["is_gold"] = slug in _GOLD
    view["search_focus"] = rep.get("search_focus") or "all_text"
    view["doc_token"] = ""
    view["account_search"] = None
    view["archive"] = report_archive.metadata(slug, REPORTS)
    view["query_claim_grid_data"] = query_claim_grid.status(slug, REPORTS)
    #  The letterhead belongs to whoever published the link, and it is part of the document a
    #  recipient is meant to see. Read with the owner's id from the publish row, never from the
    #  visitor's session — there is not one.
    try:
        view["report_doc"] = deliverables.get(pub["user_id"], slug)
    except Exception:
        view["report_doc"] = None
    view["read_only"] = True
    view["public"] = None
    #  The filing artefacts are part of what was shared: a recipient opening the public link is
    #  usually the person who needs the papers, and hiding them behind an account defeats the
    #  point of publishing the report at all.
    view["concise_docs"] = _concise_built(slug)
    visit_key = public_report.record_visit(slug, request, unlocked=_public_unlocked(slug))
    resp = make_response(render_template(
        "report.html", v=view, read_only=True, layout="base_public.html", share_token=None,
        ood=None, corpus=corpus_facts.facts(), public_visit_key=visit_key))
    #  A client document, not something to be indexed or cached by an intermediary.
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/public-report/<slug>/unlock", methods=["POST"])
def public_report_unlock(slug):
    """One password, no username. Wrong answers are counted the same as any other login attempt."""
    pub = public_report.get(slug)
    if not pub:
        abort(404)
    password = (request.form.get("password") or "").strip()
    if not public_report.check_password(slug, password):
        #  No CSRF token on this form on purpose: the visitor has no session yet and a token would
        #  be one more thing to get wrong for somebody who was simply sent a link. There is nothing
        #  to forge — the only effect of this endpoint is to let the caller read a page they
        #  already hold the URL for.
        return render_template("public_gate.html", slug=slug,
                               error="That password did not match.",
                               corpus=corpus_facts.facts()), 401
    unlocked = list(session.get("public_unlocked") or [])
    if slug not in unlocked:
        unlocked.append(slug)
    session["public_unlocked"] = unlocked[-20:]
    return redirect(f"{request.script_root}/public-report/{slug}")


@app.route("/public-report/<slug>/beacon", methods=["POST"])
def public_report_beacon(slug):
    """What only the page can know: screen, timezone, capabilities, and TIME ON PAGE.

    Called repeatedly — a heartbeat while the page is visible and a final `sendBeacon` on pagehide.
    Idempotent, and it only ever raises the recorded maximum, because the final delivery is exactly
    the one most likely to be lost.
    """
    if not public_report.get(slug):
        abort(404)
    payload = request.get_json(silent=True)
    if payload is None:
        try:
            payload = json.loads((request.get_data() or b"{}").decode("utf-8", "replace"))
        except Exception:
            payload = {}
    key = str((payload or {}).get("visit_key") or "")
    public_report.record_beacon(key, payload or {})
    #  204 and never a body: this is fired from `sendBeacon` during unload, where nothing can read
    #  a response and an error would be invisible anyway.
    return ("", 204)


@app.route("/report/<slug>/visitors")
def report_visitors(slug):
    """Who has opened the public link. Owner only."""
    user = _require_user()
    if not _can_access_report(slug):
        abort(404)
    rows = public_report.visits(user["id"], slug)
    return render_template("visitors.html", slug=slug, rows=rows,
                           summary=public_report.summary(rows),
                           status=public_report.status_for_owner(user["id"], slug),
                           public_url=f"{notifications.PUBLIC_BASE_URL}/public-report/{slug}",
                           corpus=corpus_facts.facts())


@app.route("/shared/<token>/logo.img")
def shared_report_logo(token):
    mime, data = deliverables.logo_by_share_token(token)
    if not data:
        abort(404)
    return Response(data, mimetype=mime or "image/png")


# ---- export selected references -> PDF / DOCX (the headline) --------------------------------
#  ---------------------------------------------------------------- 37 CFR 1.290 submissions
#
#  A finished search already holds what a preissuance submission is made of: per-limitation
#  verdicts with the passage that supports each one and the coordinate it sits at. These routes
#  pivot that onto the filing's axis (one document, its claims in order) and hand back the
#  two-column paper an attorney files. See concise_description.py for the three rules that govern
#  what may appear on it; the important one is that no citation is ever written by a model.

CONCISE_DIR = REPORTS / "concise"


def _concise_deep(slug):
    """The deep-read block for a slug, or None. That block IS the evidence; without it there is
    nothing to describe and the caller should say so rather than render an empty table."""
    p = REPORTS / ("%s.deep.json" % slug)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


#  Building a submission is minutes of work: a model call and an enrichment fetch per document,
#  then the compliance pass, then two renderings each. It used to run inside the POST, so the
#  browser sat on a dead page with nothing to show until every document was finished — which is
#  indistinguishable from a click that never registered. The work runs in a thread now and the page
#  polls this. In-process state is the right home for it: this app is one gunicorn worker with
#  threads precisely so shared state like _JOBS and the run gate stays visible to every request.
_CONCISE_JOBS: dict = {}
_CONCISE_JOBS_LOCK = threading.Lock()


def _concise_job(slug):
    with _CONCISE_JOBS_LOCK:
        j = _CONCISE_JOBS.get(slug)
        return dict(j) if j else None


def _concise_set(slug, **kw):
    with _CONCISE_JOBS_LOCK:
        j = _CONCISE_JOBS.get(slug)
        if j is None:
            return
        j.update(kw)


@app.route("/report/<slug>/concise/progress")
def concise_progress(slug):
    """Where the build has got to. Polled once a second, so it must stay a dict lookup."""
    if not valid_slug(slug) or not _can_access_report(slug):
        abort(404)
    j = _concise_job(slug)
    if not j:
        return jsonify({"state": "idle"})
    total, done = int(j.get("total") or 0), int(j.get("done") or 0)
    return jsonify({"state": j.get("state"), "done": done, "total": total,
                    "pct": round(100.0 * done / total, 1) if total else 0.0,
                    "msg": j.get("msg") or "", "error": j.get("error"),
                    "elapsed": int(time.time() - float(j.get("t0") or time.time()))})


def _may_read_report(slug):
    """May this request read `slug`'s CONTENT — its references, figures and filing artefacts?

    Wider than `_can_access_report`, narrower than public: whoever has been given the report and
    answered its password may read what the report is made of. Anything that writes, or that
    changes what would be filed, stays owner-only.

    It began as a gate for the 1.290 downloads, but the principle is the report's. On 2026-08-19
    figures were missing from a shared link while the owner saw them all: /api/figs is disk-only
    and ungated, so a figure appears only once something has DOWNLOADED it — and the endpoints
    that do the downloading were owner-only. The owner's own browsing filled the cache, so the
    page looked complete to the one person who could not see the bug.
    """
    if _can_access_report(slug):
        return True
    try:
        pub = public_report.get(slug) or {}
        if not pub.get("published"):
            return False
        #  A link with no password is readable by anyone holding it, so its papers are too. A link
        #  WITH a password must have been answered in this session, exactly like the report page.
        return (not public_report.needs_password(slug)) or _public_unlocked(slug)
    except Exception:
        return False


def _concise_source_text(pub):
    """The reference's own stored full text, for re-verifying quotations before filing."""
    try:
        return deep_analysis.full_text(pub) or ""
    except Exception:
        return ""


def _concise_count(slug):
    """How many 1.290 documents exist for this slug. Directory listing only — this is called once
    per row on the history page, so it must not parse anything."""
    try:
        d = CONCISE_DIR / slug
        if not d.is_dir():
            return 0
        return len([p for p in d.iterdir()
                    if p.name.startswith("ConciseDescription_") and p.suffix == ".pdf"])
    except Exception:
        return 0


def _concise_built(slug):
    """Documents already built for this slug, newest numbering first.

    Without this the page only ever showed what the CURRENT request produced, so coming back to
    collect the files meant rebuilding them — a model call per document to regenerate a paper that
    was already on disk. The .model.json beside each pair is the provenance record and is never
    listed; it is not a filing artefact.
    """
    d = CONCISE_DIR / slug
    if not d.is_dir():
        return []
    by_stem = {}
    for p in sorted(d.iterdir()):
        if not p.name.startswith("ConciseDescription_") or p.suffix not in (".pdf", ".docx"):
            continue
        stem = p.name[:-len(p.suffix)]
        row = by_stem.setdefault(stem, {"n": 0, "pub": "", "label": stem, "rows": None})
        row[p.suffix.lstrip(".")] = p.name
        bits = stem.split("_", 2)
        if len(bits) == 3 and bits[1].startswith("Doc"):
            try:
                row["n"] = int(bits[1][3:])
            except ValueError:
                pass
            row["pub"] = bits[2]
    out = [r for r in by_stem.values() if r.get("pdf") or r.get("docx")]
    out.sort(key=lambda r: r["n"])
    return out


def _concise_subject(slug, form=None):
    """Identify the application under examination.

    The submission names the application it is filed in, which is NOT always the document that was
    searched: the search may have been run from a family member or an uploaded draft. So the fields
    are editable and default to what the report knows.
    """
    form = form or {}
    meta = {}
    try:
        meta = json.loads((REPORTS / ("%s.meta.json" % slug)).read_text())
    except Exception:
        pass
    deep = _concise_deep(slug) or {}
    label = (deep.get("subject_label") or meta.get("subject") or "").strip()
    pub_no = label
    try:
        import concise_description
        pretty, _kind = concise_description._us_style(label)
        if pretty and "Publication No." in pretty:
            pub_no = pretty.split("Publication No.", 1)[1].strip()
    except Exception:
        pass
    return {
        "app_no": (form.get("app_no") or "").strip(),
        "pub_no": (form.get("pub_no") or pub_no or "").strip(),
        "title": (form.get("title") or "").strip(),
        "inventor": (form.get("inventor") or "").strip(),
    }


@app.route("/report/<slug>/concise", methods=["GET", "POST"])
def concise_descriptions(slug):
    if not valid_slug(slug):
        abort(404)
    if not _can_access_report(slug):
        abort(404)
    #  Building costs a model call per document, so it is gated like every other route that spends.
    if request.method == "POST" and auth.current_user():
        auth.require_csrf()
    deep = _concise_deep(slug)
    if not deep or not (deep.get("references") or []):
        return render_template("concise.html", slug=slug, cands=[], docs=[],
                               subject=_concise_subject(slug), error=(
                                   "This report has no full-text reading stage, so there is no "
                                   "per-claim evidence to describe. Re-run the search at depth "
                                   "'deep' first."))
    import concise_description
    import concise_render
    #  The REPORT, not {}: the picker ranks on what the ledger says each reference kills and on
    #  whether the Office itself applied it, and neither is in the deep block.
    rep_for_pick = _load_report(slug) or {}
    #  The family's own office actions come FIRST in the picker. They are not prior art and no
    #  search can produce them; they are an examiner's findings on substantially these claims, and
    #  1.290(b) forbids the submitter from making the argument they already make.
    cands = (concise_description.office_action_candidates(rep_for_pick)
             + concise_description.candidates(rep_for_pick, deep, limit=40))
    subject = _concise_subject(slug, request.form if request.method == "POST" else None)
    if request.method == "GET":
        return render_template("concise.html", slug=slug, cands=cands,
                               docs=_concise_built(slug), subject=subject, error=None)

    pubs = [p.strip() for p in request.form.getlist("pubs") if p.strip()]
    if not pubs:
        return render_template("concise.html", slug=slug, cands=cands, docs=_concise_built(slug), subject=subject,
                               error="Select at least one document."), 400
    #  Only a publication this report actually read can be described: `pubs` is user input that
    #  becomes a document lookup and a filename. Filtering it silently, though, hands back a
    #  success page with nothing on it and no reason, so an unknown one is named and refused.
    #
    #  The gate is "this reference carries verified evidence in this report", NOT "it is in the
    #  picker's top 40". The two differ and the difference matters: the reference an attorney most
    #  wants described is often not the one with the most rows. Measured on adhoc-0a80ecb18aa6,
    #  where US 6,419,291 and US 7,240,935 are both references a practitioner actually filed
    #  against this application and both sit outside the top 40 by row count.
    known = {c["pub"] for c in concise_description.candidates(
        rep_for_pick, deep, limit=10000, collapse_families=False)}
    known |= {c["pub"] for c in concise_description.office_action_candidates(rep_for_pick)}
    unknown = [p for p in pubs if p not in known]
    pubs = [p for p in pubs if p in known]
    if not pubs:
        return render_template(
            "concise.html", slug=slug, cands=cands, docs=_concise_built(slug), subject=subject,
            error=("None of the selected documents carry per-claim evidence in this report: %s"
                   % ", ".join(unknown[:5]))), 400
    #  EVERYTHING THE THREAD NEEDS IS CAPTURED HERE. The worker outlives this request, so it may
    #  not touch `request` at all.
    start_at = int(request.form.get("start_at") or 1)
    skip_compliance = request.form.get("skip_compliance") == "1"
    mode = "novelty"
    meta_p = REPORTS / ("%s.meta.json" % slug)
    if meta_p.exists():
        try:
            mode = json.loads(meta_p.read_text()).get("mode") or "novelty"
        except Exception:
            pass

    if (_concise_job(slug) or {}).get("state") == "running":
        #  A second click must not start a second build over the same output directory.
        return render_template("concise.html", slug=slug, cands=cands,
                               docs=_concise_built(slug), subject=subject, error=None,
                               blocked=[], family_notes=[], building=True)

    with _CONCISE_JOBS_LOCK:
        #  total counts one step per document for the build, one for the compliance pass, and one
        #  per document for rendering — so the bar tracks work, not documents.
        _CONCISE_JOBS[slug] = {"state": "running", "done": 0, "total": 2 * len(pubs) + 1,
                               "msg": "Starting", "error": None, "t0": time.time()}

    def _work():
        try:
            docs = concise_description.build(
                deep, pubs, subject, start_at=start_at, report=rep_for_pick,
                on_progress=lambda n, msg: _concise_set(slug, done=n, msg=msg))
            blocked, family_notes = [], []
            if not skip_compliance:
                _concise_set(slug, done=len(pubs),
                             msg="Checking prior-art status, families and quotations")
                facts = concise_description.subject_facts(
                    (deep.get("subject_label") or "").strip())
                docs, blocked, family_notes = submission_compliance.apply(
                    docs, {"efd": facts.get("efd")}, source_text_for=_concise_source_text,
                    mode=mode, target_assignees=facts.get("assignees") or [])
            out = CONCISE_DIR / slug
            out.mkdir(parents=True, exist_ok=True)
            for k, d in enumerate(docs, 1):
                _concise_set(slug, done=len(pubs) + k,
                             msg="Writing document %d of %d: %s" % (k, len(docs), d["pub"]))
                for fmt, fn in (("pdf", concise_render.to_pdf), ("docx", concise_render.to_docx)):
                    try:
                        (out / concise_render.filename(d, fmt)).write_bytes(fn(d))
                    except Exception:
                        traceback.print_exc()
                try:
                    (out / concise_render.filename(d, "md")).write_text(
                        concise_md.to_markdown(d), encoding="utf-8")
                except Exception:
                    traceback.print_exc()
                (out / ("%s.model.json" % concise_render.filename(d, "x")[:-2])).write_text(
                    json.dumps(d, ensure_ascii=False, indent=1))
            n = len(docs)
            _concise_set(slug, state="done", done=2 * len(pubs) + 1,
                         msg="%d document%s ready" % (n, "" if n == 1 else "s"))
        except Exception as exc:
            traceback.print_exc()
            _concise_set(slug, state="failed",
                         error="Could not build the documents: %s" % str(exc)[:200])

    threading.Thread(target=_work, name="concise-build", daemon=True).start()
    return render_template("concise.html", slug=slug, cands=cands, docs=_concise_built(slug),
                           subject=subject, error=None, blocked=[], family_notes=[],
                           building=True)


def _concise_doc_paths(slug, n):
    """The four files that make up one document: model, markdown, and the two renderings."""
    d = CONCISE_DIR / slug
    if not d.is_dir():
        return None
    for p in sorted(d.glob("ConciseDescription_Doc%d_*.md" % n)):
        stem = p.name[:-3]
        return {"dir": d, "stem": stem, "md": p,
                "model": d / ("%s.model.json" % stem),
                "pdf": d / ("%s.pdf" % stem), "docx": d / ("%s.docx" % stem)}
    return None


@app.route("/report/<slug>/concise/doc/<int:n>", methods=["GET", "POST"])
def concise_document(slug, n):
    """Preview one document, edit its markdown, and re-render the PDF and DOCX from the edit.

    The markdown is the editable form and the JSON model is the record; an edit rebuilds the model
    from the markdown so what is filed is what was reviewed. A markdown file that no longer matches
    the grammar is REFUSED rather than parsed loosely, because a loose parse silently drops rows.
    """
    if not valid_slug(slug) or not _can_access_report(slug):
        abort(404)
    paths = _concise_doc_paths(slug, n)
    if not paths:
        abort(404)
    if request.method == "POST" and auth.current_user():
        auth.require_csrf()
    err = saved = None
    if request.method == "POST":
        md = request.form.get("markdown") or ""
        try:
            base = json.loads(paths["model"].read_text())
            doc = concise_md.from_markdown(md, base)
            paths["md"].write_text(md, encoding="utf-8")
            paths["pdf"].write_bytes(concise_render.to_pdf(doc))
            paths["docx"].write_bytes(concise_render.to_docx(doc))
            paths["model"].write_text(json.dumps(doc, ensure_ascii=False, indent=1))
            saved = True
        except concise_md.MarkdownShapeError as e:
            err = str(e)
        except Exception:
            traceback.print_exc()
            err = "Could not re-render from that markdown; the error is in the log."
    return render_template("concise_doc.html", slug=slug, n=n, stem=paths["stem"],
                           markdown=paths["md"].read_text(encoding="utf-8"),
                           error=err, saved=saved)


@app.route("/report/<slug>/concise.zip")
def concise_zip(slug):
    """Every filing artefact for this search in one archive.

    The model and markdown are working files, not filing artefacts, so the archive carries the
    PDFs and DOCXs only — what actually goes to the Office.
    """
    if not valid_slug(slug) or not _may_read_report(slug):
        abort(404)
    d = CONCISE_DIR / slug
    if not d.is_dir():
        abort(404)
    import io as _io
    import zipfile
    buf = _io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(d.iterdir()):
            if p.name.startswith("ConciseDescription_") and p.suffix in (".pdf", ".docx"):
                z.write(p, arcname=p.name)
                n += 1
    if not n:
        abort(404)
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="application/zip", headers={
        "Content-Disposition": 'attachment; filename="third-party-submission-%s.zip"' % slug})


@app.route("/report/<slug>/concise/<path:name>")
def concise_download(slug, name):
    if not valid_slug(slug) or not _may_read_report(slug):
        abort(404)
    #  `name` is user-supplied and about to become a path. Only ever serve a file this feature
    #  wrote, matched by exact basename, so no traversal is possible.
    d = CONCISE_DIR / slug
    if not d.is_dir():
        abort(404)
    base = os.path.basename(name)
    target = d / base
    if not target.is_file() or base.endswith(".model.json"):
        abort(404)
    return send_from_directory(str(d), base, as_attachment=True)


@app.route("/export", methods=["POST"])
def export():
    slug = request.form.get("slug", "").strip()
    fmt = request.form.get("format", "pdf").strip().lower()
    pubs = [p for p in request.form.get("pubs", "").split(",") if p.strip()]
    if not slug or not pubs or fmt not in EXPORT_FORMATS:
        return jsonify({"error": "need slug, pubs, format(%s)" % "|".join(EXPORT_FORMATS)}), 400
    # `slug` arrives in a form field, so no route converter has vetted it, and it is about to become
    # part of a path we WRITE to. Validate before touching the filesystem.
    if not valid_slug(slug):
        return jsonify({"error": "invalid slug"}), 400
    if not _can_access_report(slug):
        abort(404)
    #  The letterhead, matter details and narrative are PART of the exported document, so they
    #  belong in the cache identity. Without the revision stamp, editing the client name and
    #  re-exporting hands back the file built before the edit.
    doc = _report_doc(slug)
    rev = str((doc or {}).get("updated_at") or "")
    #  THE VIEW IS PART OF THE EXPORT'S IDENTITY. export_data.assemble builds from the cached view
    #  — the ranked order, the cards and the element grid all come from it — so a change to the
    #  view produces a different document from the same slug, pubs and letterhead revision. It was
    #  not in the key, so when the grid changed from retrieval cells to full-text reading cells,
    #  every report that had ever been exported kept serving the old file: the page showed the
    #  reading and the exported PDF showed the retrieval map, indefinitely and silently.
    try:
        rev += "|" + str((REPORTS / f"{slug}.view.json").stat().st_mtime)
    except OSError:
        pass                                   # no cached view yet; assemble() will build one
    key = hashlib.sha1((slug + "|" + fmt + "|" + ",".join(sorted(pubs)) + "|" + rev)
                       .encode()).hexdigest()[:12]
    out = EXPORTS / f"{slug}__{key}.{fmt}"
    if not out.exists():
        # An unknown slug used to raise inside assemble() and surface as an unhandled HTML 500.
        if not report_path(slug).exists() and slug not in _GOLD:
            return jsonify({"error": "unknown report", "slug": slug}), 404
        spec = EXPORT_FORMATS[fmt]
        try:
            model = export_data.assemble(slug, pubs, include_text=spec["text"],
                                         include_drawings=spec["drawings"])
        except Exception as e:
            return jsonify({"error": "could not assemble export", "detail": str(e)[:200]}), 400
        model["report_doc"] = doc
        model["report_logo"] = _report_logo_path(slug) if doc and doc.get("has_logo") else None
        spec["render"](model, out)
    dl = f"prior-art-{slug}.{fmt}"
    return send_from_directory(EXPORTS, out.name, as_attachment=True,
                               download_name=dl, mimetype=EXPORT_FORMATS[fmt]["mime"])


# ---- citation graph + more-like-this -------------------------------------------------------
@app.route("/api/graph/<pub>")
def api_graph(pub):
    """cited_by (forward) + patent_citations (backward) + similar_documents from the SerpApi cache."""
    disp = enrich_display.load_cached(pub)
    raw = (disp or {}).get("raw") if disp else None
    if not raw:
        # A Mongo-served pub has a cached display but no SerpApi `raw` (citations / cited_by /
        # similar are not in the corpus). ensure_raw lazily fetches + caches just that payload,
        # instead of re-running the whole display enrichment.
        raw = enrich_display.ensure_raw(pub)
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
def _compare_biblio(cur, pid, pub, display):
    """Return a complete comparison header for local *and* federated-only references.

    The final list can legitimately contain an API-federated publication whose compact number is
    not an exact ``publications.publication_number`` match.  Its rich metadata has already been
    cached by the same enrichment path used by the detail drawer, so an ``(untitled)`` comparison
    header is both slower to understand and inconsistent with the card the user just selected.
    """
    if pid:
        return webview.biblio(cur, pid)
    d = display or {}
    country = (d.get("country") or str(pub)[:2]).upper()
    return {
        "pid": None,
        "pub": d.get("pub") or pub,
        "kind": d.get("type"),
        "title": d.get("title"),
        "abstract": d.get("abstract"),
        "country": country,
        "flag": webview.FLAG.get(country, "🏳️"),
        "publication_date": d.get("publication_date"),
        "filing_date": d.get("filing_date"),
        "priority_date": d.get("priority_date"),
        "family_id": d.get("family_id"),
        "assignees": d.get("assignees") or [],
        "inventors": d.get("inventors") or [],
        "cpc": d.get("classifications") or [],
        "legal_events": d.get("legal_events") or [],
    }


@app.route("/compare")
def compare():
    slug = request.args.get("slug", "")
    if slug and not _can_access_report(slug):
        abort(404)
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
            disp = enrich_display.enrich_for_display(pub)
            b = _compare_biblio(cur, pid, pub, disp)
            matched = webview.match_in_pub(cur, pid, qv) if (pid and qv is not None) else None
            # which elements this family covers (from report evidence)
            fam = b.get("family_id")
            covers = []
            for el, hits in rep.get("element_evidence", {}).items():
                if any(h.get("family") == fam for h in hits):
                    covers.append(el)
            img = img_full = None
            imgs = (disp or {}).get("images") or []
            if imgs:
                im0 = imgs[0]
                # A lemad-Mongo figure has file=None and a remote Google-CDN thumbnail/full URL;
                # a locally-recovered figure has a filename served from /figures/<pub>/<file>. A
                # root-relative "/figures/..." path is prefixed with script_root in the template;
                # an absolute remote URL is used verbatim (the template only prefixes local paths).
                if im0.get("file"):
                    img = img_full = f"/figures/{pub}/{im0['file']}"
                else:
                    img = im0.get("thumbnail") or im0.get("full")
                    img_full = im0.get("full") or im0.get("thumbnail")
            cols.append({"pub": pub, "biblio": b, "img": img, "img_full": img_full,
                         "matched": {"kind": (matched or {}).get("kind"),
                                     "coord": webview._coord_str((matched or {}).get("coord")),
                                     "text": (matched or {}).get("text", "")[:1000]} if matched else None,
                         "covers": covers, "n_images": len(imgs),
                         # zero-padded Google Patents link (pubnorm) so US pre-grant pubs
                         # do not 404; the cached disp value drops the leading zero.
                         "google_patents": pubnorm.google_url(pub) or (disp or {}).get("google_patents")})
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
    if not _can_access_report(slug):
        abort(404)
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


# ---- account-owned US application drafting ------------------------------------------------
_DRAFTING_SERVICE = None


def _draft_report_loader(principal, slug, owner_user_id):
    """Load only an authoritative, final server-side report for the project's owner."""
    principal.require_active()
    owner_user_id = int(owner_user_id)
    if principal.user_id != owner_user_id and not principal.is_admin:
        raise drafting.DraftingNotFound("Search report was not found.")
    if slug not in _GOLD:
        try:
            owned = accounts.can_access_search(owner_user_id, slug)
        except Exception:
            owned = False
        # An administrator may inspect operational reports, but an ordinary drafting project can
        # only depend on a report row that belongs to its account.
        if not owned and not principal.is_admin:
            raise drafting.DraftingNotFound("Search report was not found in this account.")
    rep = _load_report(slug)
    if not rep:
        raise drafting.DraftingNotFound("Search report was not found.")
    if rep.get("partial"):
        raise drafting.DraftingConflict("Wait for the final ranking before starting a draft.")
    view = _build_view_cached(slug, rep)
    view["slug"] = slug
    # The interactive view is deliberately based on the condensed search brief.  Drafting is a
    # different trust boundary: only the inventor's verbatim uploaded disclosure may supply new
    # matter, so pass that immutable server-side snapshot separately when the search came from an
    # upload.  It is never synthesized from prior-art cards.
    view["query_document"] = dict(rep.get("query_document") or {})
    return view


def _drafting_service():
    global _DRAFTING_SERVICE
    if _DRAFTING_SERVICE is None:
        _DRAFTING_SERVICE = drafting.DraftingService(
            drafting.DraftingRepository(), _draft_report_loader)
    return _DRAFTING_SERVICE


def _draft_identity():
    user = auth.current_user()
    if not user:
        raise drafting.DraftingPermissionDenied("A named account is required for drafting.")
    return user, drafting.Principal.from_user(user)


def _draft_error_status(exc):
    if isinstance(exc, drafting.DraftingNotFound):
        return 404
    if isinstance(exc, drafting.DraftingPermissionDenied):
        return 403
    if isinstance(exc, drafting.DraftingConflict):
        return 409
    return 400


def _draft_error_redirect(project_id, exc):
    return redirect(url_for("draft_detail", project_id=project_id, error=str(exc)[:300]))


def _draft_report_choices(user, limit=300):
    choices = []
    try:
        rows = accounts.list_searches(user["id"], limit=limit)
    except Exception:
        rows = []
    for row in rows:
        if row.get("status") != "complete" or not report_path(row["slug"]).exists():
            continue
        choices.append({
            "slug": row["slug"], "title": row.get("title"),
            "query": row.get("query") or "", "mode": row.get("mode") or "novelty",
            "search_focus": row.get("search_focus") or "all_text",
            "updated_at": row.get("updated_at"),
        })
    return choices


def _draft_new_context(user, principal, slug, selected=None, values=None, error=""):
    choices = _draft_report_choices(user)
    report_view = None
    if slug:
        try:
            report_view = _draft_report_loader(principal, slug, user["id"])
        except drafting.DraftingError as exc:
            error = error or str(exc)
    values = dict(values or {})
    if not values.get("applicant"):
        values["applicant"] = user.get("default_applicant") or user.get("organization") or ""
    if not values.get("inventors"):
        values["inventors"] = user.get("default_inventors") or user.get("full_name") or ""
    if report_view:
        account_search = accounts.get_search(user["id"], slug) or {}
        source_document = report_view.get("query_document") or {}
        if not values.get("title"):
            values["title"] = (account_search.get("title") or source_document.get("title") or
                               (report_view.get("query") or "US patent application")[:180])
        if not values.get("disclosure_text"):
            values["disclosure_text"] = (source_document.get("disclosure_text") or
                                         report_view.get("query") or "")
    cards = list((report_view or {}).get("cards") or [])
    selected = list(selected or [])
    if cards and not selected:
        selected = [card.get("pub") for card in cards[:5] if card.get("pub")]
    source_document = (report_view or {}).get("query_document") or {}
    return {"choices": choices, "report_view": report_view, "search_slug": slug,
            "source_document": source_document,
            "selected": set(selected), "values": values, "error": error}


def _structured_drafting_notes(values) -> str:
    """Turn explicit intake choices into a stable brief instead of accepting a catch-all box."""
    priority = str(values.get("priority_status") or "unknown")
    priority_text = {
        "none": "No domestic or foreign priority claim is expected.",
        "claim": "A priority claim may be required: " +
                 (str(values.get("priority_details") or "details not supplied; ask for them")[:1000]),
        "unknown": "Priority status is not confirmed; leave a drafting note requesting it.",
    }.get(priority, "Priority status is not confirmed; leave a drafting note requesting it.")
    support = str(values.get("government_support") or "unknown")
    support_text = {
        "none": "No federally sponsored research or government contract was identified.",
        "yes": "Government support may apply: " +
               (str(values.get("government_support_details") or
                    "award and contract details not supplied; ask for them")[:1000]),
        "unknown": "Government support status is not confirmed; leave a drafting note requesting it.",
    }.get(support, "Government support status is not confirmed; leave a drafting note requesting it.")
    strategy = {
        "balanced": "Use a broad, disclosure-supported independent claim with a graduated fallback ladder.",
        "broad": "Prioritize the broadest disclosure-supported independent claim and retain narrower fallbacks.",
        "conservative": "Prioritize explicit written support and use narrower independent claims where needed.",
    }.get(str(values.get("claim_strategy") or "balanced"),
          "Use a broad, disclosure-supported independent claim with a graduated fallback ladder.")
    claim_types = values.getlist("claim_types") if hasattr(values, "getlist") else \
        values.get("claim_types") or []
    if isinstance(claim_types, str):
        claim_types = [claim_types]
    allowed_types = [item for item in claim_types if item in ("apparatus", "method", "system")]
    classes = ("Requested claim classes where supported: " + ", ".join(allowed_types) + ".") \
        if allowed_types else "Use every statutory claim class the disclosure genuinely supports."
    means = ("Means-plus-function language is permitted where useful."
             if str(values.get("means_plus_function") or "avoid") == "allow"
             else "Avoid means-plus-function language unless the user later requests it.")
    terminology = str(values.get("protected_terms") or "").replace("\x00", "").strip()[:2000]
    deadline = str(values.get("filing_deadline") or "").strip()[:40]
    parts = ["Filing and drafting instructions:", priority_text, support_text, strategy, classes, means]
    if terminology:
        parts.append("Terminology to preserve: " + terminology)
    if deadline:
        parts.append("Target filing date supplied by the user: " + deadline)
    # Legacy clients may still post this field. Preserve it as clearly labelled user material,
    # while the shipped UI no longer invites an unstructured catch-all answer.
    legacy = str(values.get("inventor_notes") or "").replace("\x00", "").strip()[:20_000]
    if legacy:
        parts.append("Additional user-supplied instructions: " + legacy)
    return "\n".join(parts)


@app.route("/drafts")
def drafts_list():
    try:
        user, principal = _draft_identity()
        include_all = bool(principal.is_admin and request.args.get("all") == "1")
        projects = _drafting_service().list_projects(principal, include_all=include_all)
        return render_template("drafts.html", projects=projects, include_all=include_all,
                               user=user)
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)


@app.route("/drafts/new", methods=["GET", "POST"])
def draft_new():
    try:
        user, principal = _draft_identity()
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)
    slug = (request.values.get("search_slug") or request.values.get("slug") or "").strip()
    selected = request.values.getlist("pubs")
    # Selection-bar GET links encode the publications as one comma-separated value.
    if len(selected) == 1 and "," in selected[0]:
        selected = [value for value in selected[0].split(",") if value]
    if request.method == "POST":
        auth.require_csrf()
        values = {"title": request.form.get("title", ""),
                  "disclosure_text": request.form.get("disclosure_text", ""),
                  "inventor_notes": request.form.get("inventor_notes", "")}
        try:
            service = _drafting_service()
            project = service.create_project_with_references(
                principal, search_slug=slug, title=values["title"],
                disclosure_text=values["disclosure_text"], inventor_notes=values["inventor_notes"],
                publication_numbers=selected)
            return redirect(url_for("draft_detail", project_id=project["id"], created="1"))
        except drafting.DraftingError as exc:
            ctx = _draft_new_context(user, principal, slug, selected, values, str(exc))
            return render_template("draft_new.html", **ctx), _draft_error_status(exc)
    ctx = _draft_new_context(user, principal, slug, selected)
    return render_template("draft_new.html", **ctx)


def _draft_detail_context(principal, project_id):
    service = _drafting_service()
    project = service.get_project(principal, project_id, include_versions=True)
    chosen_no = request.args.get("version", type=int) or int(project.get("latest_version_no") or 0)
    version = next((v for v in project.get("versions", [])
                    if int(v.get("version_no") or 0) == chosen_no), None)
    version_diff = ""
    if version:
        previous = next((v for v in project.get("versions", [])
                         if int(v.get("version_no") or 0) == int(version["version_no"]) - 1), None)
        if previous:
            chunks = []
            for key, heading in drafting.SECTION_ORDER:
                before = str((previous.get("sections") or {}).get(key) or "").splitlines()
                after = str((version.get("sections") or {}).get(key) or "").splitlines()
                if before != after:
                    chunks.extend(difflib.unified_diff(
                        before, after, fromfile=f"v{previous['version_no']} {heading}",
                        tofile=f"v{version['version_no']} {heading}", lineterm=""))
            version_diff = "\n".join(chunks)[:60_000]
    try:
        report_view = _draft_report_loader(principal, project["search_slug"], project["user_id"])
    except drafting.DraftingError:
        report_view = {"cards": []}
    selected_pubs = {r["publication_number"] for r in project.get("references", [])}
    jobs = project.get("jobs") or []
    return {"project": project, "version": version,
            "latest_job": jobs[0] if jobs else None,
            "report_cards": report_view.get("cards") or [], "selected_pubs": selected_pubs,
            "section_order": drafting.SECTION_ORDER, "version_diff": version_diff,
            "generation_key": secrets.token_urlsafe(24),
            "error": request.args.get("error", ""), "message": request.args.get("message", ""),
            "created": request.args.get("created") == "1",
            #  Figures live beside the draft, not inside a version: a drawing survives a
            #  regeneration of the text, which is what makes iterating on it worth doing.
            "figures": _figures_for(project),
            "figure_suggestions": draft_figures.figures_from_draft((version or {}).get("sections") or {})}


def _figures_for(project):
    """This project's figures, or [] if the store is unavailable — never break the draft page."""
    try:
        return draft_figures.listing(project["id"], project["user_id"])
    except Exception:
        traceback.print_exc()
        return []


@app.route("/drafts/<int:project_id>")
def draft_detail(project_id):
    try:
        _user, principal = _draft_identity()
        return render_template("draft.html", **_draft_detail_context(principal, project_id))
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)


@app.route("/drafts/<int:project_id>/project", methods=["POST"])
def draft_update_project(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        _drafting_service().update_project(
            principal, project_id, title=request.form.get("title", ""),
            disclosure_text=request.form.get("disclosure_text", ""),
            inventor_notes=request.form.get("inventor_notes", ""),
            expected_revision=request.form.get("expected_revision", type=int))
        return redirect(url_for("draft_detail", project_id=project_id,
                                message="Project inputs saved. Generate a new version when ready."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/references", methods=["POST"])
def draft_update_references(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        _drafting_service().select_references(
            principal, project_id, request.form.getlist("pubs"),
            expected_revision=request.form.get("expected_revision", type=int))
        return redirect(url_for("draft_detail", project_id=project_id,
                                message="Prior-art source selection saved."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/generate", methods=["POST"])
def draft_generate(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        idem = request.form.get("idempotency_key") or secrets.token_urlsafe(18)
        job = _drafting_service().queue_generation(
            principal, project_id, instructions=request.form.get("instructions", ""),
            idempotency_key=idem)
        draft_worker.kick()
        state = job.get("status") or "queued"
        message = ("Draft generation queued. You may leave this page safely."
                   if state in {"queued", "running"}
                   else f"That generation request is already {state}. Reload to start another.")
        return redirect(url_for("draft_detail", project_id=project_id,
                                message=message))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/retry/<int:job_id>", methods=["POST"])
def draft_retry(project_id, job_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        job = _drafting_service().get_generation(principal, job_id)
        if int(job["project_id"]) != project_id:
            raise drafting.DraftingNotFound("Draft generation was not found.")
        _drafting_service().retry_generation(
            principal, job_id, idempotency_key=secrets.token_urlsafe(18))
        draft_worker.kick()
        return redirect(url_for("draft_detail", project_id=project_id,
                                message="Draft generation queued again."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/cancel/<int:job_id>", methods=["POST"])
def draft_cancel(project_id, job_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        job = _drafting_service().get_generation(principal, job_id)
        if int(job["project_id"]) != project_id:
            raise drafting.DraftingNotFound("Draft generation was not found.")
        _drafting_service().cancel_generation(principal, job_id)
        return redirect(url_for("draft_detail", project_id=project_id,
                                message="Draft generation cancelled; your project was preserved."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/versions", methods=["POST"])
def draft_save_version(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        sections = {key: request.form.get(key, "") for key, _heading in drafting.SECTION_ORDER}
        version = _drafting_service().save_edited_version(
            principal, project_id, sections,
            base_version_no=request.form.get("base_version_no", type=int))
        return redirect(url_for("draft_detail", project_id=project_id,
                                version=version["version_no"], message="Edits saved as a new version."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/versions/<int:version_no>/status", methods=["POST"])
def draft_version_status(project_id, version_no):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        _drafting_service().set_version_status(
            principal, project_id, version_no, request.form.get("status", "draft"))
        return redirect(url_for("draft_detail", project_id=project_id, version=version_no,
                                message="Version review status updated."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/versions/<int:version_no>/restore", methods=["POST"])
def draft_restore_version(project_id, version_no):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        service = _drafting_service()
        prior = service.get_version(principal, project_id, version_no)
        restored = service.save_edited_version(
            principal, project_id, prior["sections"], base_version_no=version_no)
        return redirect(url_for(
            "draft_detail", project_id=project_id, version=restored["version_no"],
            message=f"Version {version_no} restored as new version {restored['version_no']}."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/drafts/<int:project_id>/archive", methods=["POST"])
def draft_archive(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        archived = request.form.get("archived", "1") == "1"
        _drafting_service().archive_project(principal, project_id, archived=archived)
        if archived:
            return redirect(url_for("drafts_list"))
        return redirect(url_for("draft_detail", project_id=project_id,
                                message="Draft project restored."))
    except drafting.DraftingError as exc:
        return _draft_error_redirect(project_id, exc)


@app.route("/api/drafts/<int:project_id>/status")
def api_draft_status(project_id):
    try:
        _user, principal = _draft_identity()
        project = _drafting_service().get_project(principal, project_id, include_versions=True)
        jobs = project.get("jobs") or []
        job = jobs[0] if jobs else None
        return jsonify({
            "id": project["id"], "status": project["status"],
            "revision": project["revision"], "latest_version_no": project["latest_version_no"],
            "reference_count": len(project.get("references") or []),
            "job": ({key: job.get(key) for key in
                     ("id", "status", "attempts", "max_attempts", "last_error", "created_at",
                      "started_at", "completed_at")} if job else None),
            "ready_url": (url_for("draft_detail", project_id=project_id,
                                  version=project["latest_version_no"])
                          if project.get("latest_version_no") and
                          not (job and job.get("status") in {"queued", "running"}) else None),
        })
    except drafting.DraftingError as exc:
        return jsonify({"error": str(exc)}), _draft_error_status(exc)


@app.route("/drafts/<int:project_id>/download/<fmt>")
def draft_download(project_id, fmt):
    if fmt not in {"md", "docx", "pdf"}:
        abort(404)
    try:
        _user, principal = _draft_identity()
        service = _drafting_service()
        project = service.get_project(principal, project_id, include_versions=False)
        version_no = request.args.get("version", type=int) or int(project.get("latest_version_no") or 0)
        if not version_no:
            raise drafting.DraftingNotFound("No draft version is ready to download.")
        version = service.get_version(principal, project_id, version_no)
        refs = project.get("references") or []
        name = draft_export.download_name(project, version_no, fmt)
        if fmt == "md":
            return Response(
                draft_export.render_markdown(project, version, refs),
                mimetype="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{name}"'})
        if fmt == "docx":
            return send_file(
                draft_export.render_docx(project, version, refs), as_attachment=True,
                download_name=name,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return send_file(draft_export.render_pdf(project, version, refs), as_attachment=True,
                         download_name=name, mimetype="application/pdf")
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)


@app.route("/drafts/<int:project_id>/print")
def draft_print(project_id):
    try:
        _user, principal = _draft_identity()
        service = _drafting_service()
        project = service.get_project(principal, project_id, include_versions=False)
        version_no = request.args.get("version", type=int) or int(project.get("latest_version_no") or 0)
        if not version_no:
            raise drafting.DraftingNotFound("No draft version is ready to print.")
        version = service.get_version(principal, project_id, version_no)
        return render_template("draft_print.html", project=project, version=version,
                               section_order=drafting.SECTION_ORDER,
                               notice=draft_export.WORKING_DRAFT_NOTICE)
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)


# ---- the drafting conversation (phase two) --------------------------------------------------
#  The classic page above edits a draft section by section. This is the product: the user talks to
#  a drafting agent, the agent edits the application, and a reviewer checks every iteration.
_STUDIO_SERVICE = None
_FIGURE_COMPILER_SERVICE = None


def _studio():
    global _STUDIO_SERVICE
    if _STUDIO_SERVICE is None:
        _STUDIO_SERVICE = draft_studio_service.StudioService(_drafting_service())
    return _STUDIO_SERVICE


def _figure_compiler():
    global _FIGURE_COMPILER_SERVICE
    if _FIGURE_COMPILER_SERVICE is None:
        _FIGURE_COMPILER_SERVICE = figure_compiler_service.FigureCompilerService(
            _drafting_service())
    return _FIGURE_COMPILER_SERVICE


def _turn_runner():
    return draft_studio.TurnRunner(draft_studio.StudioRepository(),
                                   _drafting_service().repository)


def _studio_error(exc):
    return jsonify({"ok": False, "error": str(exc)}), _draft_error_status(exc)


def _figure_compiler_error(exc):
    status = 409 if isinstance(
        exc, (figure_compiler.ApprovalRequired, figure_compiler.CompilationBlocked)) else \
        _draft_error_status(exc)
    return jsonify({"ok": False, "error": str(exc)}), status


def _uploads_from_request(default_kind="prior_art"):
    """Read multipart uploads without letting one oversized file consume the worker."""
    out = []
    kind = request.form.get("kind") or default_kind
    for storage in request.files.getlist("files") or []:
        if not storage or not storage.filename:
            continue
        data = storage.read(draft_studio_service.MAX_UPLOAD_BYTES + 1)
        if len(data) > draft_studio_service.MAX_UPLOAD_BYTES:
            raise drafting.DraftingValidationError(
                f"{storage.filename} is larger than "
                f"{draft_studio_service.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        out.append({"data": data, "filename": storage.filename,
                    "content_type": storage.mimetype or "", "kind": kind,
                    "note": request.form.get("note", "")})
        if len(out) >= 12:
            break
    return out


@app.route("/drafts/start", methods=["GET", "POST"])
def draft_start():
    """Open a drafting project from a description, or from a draft the user already has."""
    try:
        user, principal = _draft_identity()
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)

    slug = (request.values.get("search_slug") or request.values.get("slug") or "").strip()
    selected = request.values.getlist("pubs")
    if len(selected) == 1 and "," in selected[0]:
        selected = [value for value in selected[0].split(",") if value]

    if request.method == "POST":
        auth.require_csrf()
        values = {key: request.form.get(key, "") for key in
                  ("title", "disclosure_text", "inventor_notes", "applicant", "inventors",
                   "input_kind", "priority_status", "priority_details", "government_support",
                   "government_support_details", "claim_strategy", "means_plus_function",
                   "protected_terms", "filing_deadline")}
        values["claim_types"] = request.form.getlist("claim_types")
        try:
            # A finished owned search is already a complete intake. The action on the report is a
            # POST so one click can create durable state without turning a GET into a mutation.
            # All content and publications are loaded again from the server-owned report; hidden
            # form values cannot substitute another disclosure or smuggle in a reference.
            if request.form.get("direct") == "1":
                if not slug:
                    raise drafting.DraftingValidationError("Choose a completed search first.")
                ctx = _draft_new_context(user, principal, slug)
                if not ctx.get("report_view"):
                    raise drafting.DraftingNotFound("That completed search is not available.")
                direct_values = ctx["values"]
                direct_cards = list(ctx["report_view"].get("cards") or [])
                selected = [card.get("pub") for card in direct_cards[:5] if card.get("pub")]
                project = _studio().create(
                    principal, title=direct_values.get("title") or "",
                    disclosure_text=direct_values.get("disclosure_text") or "",
                    input_kind="description", search_slug=slug,
                    publication_numbers=selected,
                    inventor_notes=_structured_drafting_notes(request.form),
                    applicant=direct_values.get("applicant") or "",
                    inventors=direct_values.get("inventors") or "", uploads=[])
                return redirect(url_for("draft_studio_page", project_id=project["id"], created="1"))

            uploads = _uploads_from_request()
            #  A source document supersedes the pasted text: the user who uploads their own draft
            #  and also leaves the textarea half-filled means the file.
            for storage in request.files.getlist("source_document") or []:
                if storage and storage.filename:
                    data = storage.read(draft_studio_service.MAX_UPLOAD_BYTES + 1)
                    extracted = draft_studio_service.extract_text(data, storage.filename)
                    if not extracted.get("ok"):
                        raise drafting.DraftingValidationError(extracted["error"])
                    values["disclosure_text"] = extracted["text"]
                    values["input_kind"] = "existing_draft"
                    values["title"] = values["title"] or extracted.get("title") or ""
            project = _studio().create(
                principal, title=values["title"], disclosure_text=values["disclosure_text"],
                input_kind=values["input_kind"] or "description", search_slug=slug,
                publication_numbers=selected, inventor_notes=_structured_drafting_notes(request.form),
                applicant=values["applicant"], inventors=values["inventors"], uploads=uploads)
            return redirect(url_for("draft_studio_page", project_id=project["id"], created="1"))
        except drafting.DraftingError as exc:
            ctx = _draft_new_context(user, principal, slug, selected, values, str(exc))
            ctx["agent"] = draft_agent_availability()
            return render_template("draft_start.html", **ctx), _draft_error_status(exc)

    ctx = _draft_new_context(user, principal, slug, selected)
    ctx["agent"] = draft_agent_availability()
    return render_template("draft_start.html", **ctx)


def draft_agent_availability():
    try:
        import draft_agent
        return draft_agent.availability()
    except Exception:                                   # noqa: BLE001 - the page must still render
        traceback.print_exc()
        return {"ok": False, "reason": "The drafting agent could not be inspected."}


@app.route("/drafts/<int:project_id>/studio")
def draft_studio_page(project_id):
    try:
        _user, principal = _draft_identity()
        state = _studio().state(principal, project_id)
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)
    #  The page renders itself from exactly the same JSON the poller fetches, so there is one
    #  rendering path rather than a server-rendered first paint that drifts from the live one.
    return render_template("draft_studio.html", state=state, project=state["project"],
                           payload=_studio_payload(state),
                           created=request.args.get("created") == "1")


@app.route("/api/drafts/<int:project_id>/studio")
def api_draft_studio(project_id):
    try:
        _user, principal = _draft_identity()
        return jsonify(_studio_payload(_studio().state(principal, project_id)))
    except drafting.DraftingError as exc:
        return _studio_error(exc)


def _studio_payload(state):
    """The whole studio as JSON. One shape, rendered by one client function."""
    project = state["project"]
    version = state.get("version") or {}
    qa = state.get("qa")
    return {
        "ok": True,
        #  The disclosure can be a quarter of a megabyte and the page never renders it, so the
        #  payload carries only enough to show what the project was started from. This object is
        #  re-fetched on every change during a turn.
        "project": dict({key: project.get(key) for key in
                         ("id", "title", "status", "revision", "latest_version_no", "search_slug",
                          "input_kind", "applicant", "inventors")},
                        disclosure_excerpt=str(project.get("disclosure_text") or "")[:4000],
                        disclosure_chars=len(str(project.get("disclosure_text") or ""))),
        "messages": [{"id": m["id"], "role": m["role"], "body": m["body"],
                      "payload": m["payload"], "created_at": str(m["created_at"])}
                     for m in state["messages"]],
        "turns": [{key: t.get(key) for key in
                   ("id", "turn_no", "kind", "status", "stage", "summary", "version_no",
                    "cost_usd", "duration_ms", "model_name", "last_error")}
                  for t in state["turns"][:40]],
        "active_turn": state.get("active_turn"),
        "version": {"version_no": version.get("version_no"), "sections": version.get("sections"),
                    "citations": version.get("citations"),
                    "change_note": version.get("change_note"),
                    "created_at": str(version.get("created_at") or "")} if version else None,
        "versions": [{"version_no": v["version_no"], "status": v.get("status"),
                      "created_at": str(v.get("created_at") or ""),
                      "change_note": v.get("change_note") or "",
                      "verdict": (state["qa_by_version"].get(v["version_no"]) or {}).get("verdict")}
                     for v in project.get("versions", [])],
        "qa": qa,
        "references": [{"publication_number": r["publication_number"], "title": r.get("title"),
                        "origin": r.get("origin") or "report", "url": r.get("source_url"),
                        "rank": r.get("report_rank")}
                       for r in project.get("references", [])],
        "documents": [{"id": d["id"], "filename": d["filename"], "kind": d["kind"],
                       "title": d.get("title"), "note": d.get("note"),
                       "publication_number": d.get("publication_number"),
                       "chars": d.get("char_count")} for d in state["documents"]],
        "sections": [{"key": key, "heading": heading} for key, _n, heading in
                     draft_workspace.SECTION_FILES],
        "figures": state.get("figures") or [],
        "searches": _draft_search_payload(state.get("searches") or []),
        "agent": state["agent"],
    }


def _draft_search_payload(rows):
    out = []
    for row in rows:
        slug = str(row.get("slug") or "")
        with _JOB_LOCK:
            job = dict(_JOBS.get(slug, {}))
        event = _job_event(slug, job) if valid_slug(slug) else {
            "status": "error", "msg": "Invalid search id.", "ready": False, "done": False,
            "detail": {}}
        status = "complete" if event.get("done") and event.get("ready") else \
            ("error" if event.get("status") == "error" else "running")
        out.append({"slug": slug, "status": status, "ready": bool(event.get("ready")),
                    "done": bool(event.get("done")), "msg": event.get("msg") or "",
                    "detail": event.get("detail") or {},
                    "imported_count": int(row.get("imported_count") or 0),
                    "created_at": str(row.get("created_at") or ""),
                    "report_url": url_for("report", slug=slug) if valid_slug(slug) else ""})
    return out


@app.route("/api/drafts/<int:project_id>/studio/poll")
def api_draft_studio_poll(project_id):
    try:
        _user, principal = _draft_identity()
        return jsonify(_studio().poll(principal, project_id))
    except drafting.DraftingError as exc:
        return _studio_error(exc)


# ---- deterministic filing drawings ----------------------------------------------------------
@app.route("/api/drafts/<int:project_id>/figure-compiler")
def api_draft_figure_compiler(project_id):
    try:
        _user, principal = _draft_identity()
        return jsonify({"ok": True, "compiler": _figure_compiler().state(principal, project_id)})
    except (drafting.DraftingError, figure_compiler.FigureCompilerError) as exc:
        return _figure_compiler_error(exc)


def _compiler_action(project_id, action):
    """One authenticated response shape for every explicit compiler gate."""
    try:
        _user, principal = _draft_identity()
        return jsonify({"ok": True, "compiler": action(principal)})
    except (drafting.DraftingError, figure_compiler.FigureCompilerError) as exc:
        return _figure_compiler_error(exc)


@app.route("/drafts/<int:project_id>/figure-compiler/start", methods=["POST"])
def draft_figure_compiler_start(project_id):
    auth.require_csrf()
    body = request.get_json(silent=True) or request.form
    return _compiler_action(project_id, lambda principal: _figure_compiler().start(
        principal, project_id, version_no=body.get("version_no", type=int)
        if hasattr(body, "get") and not isinstance(body, dict) else body.get("version_no"),
        ruleset=str(body.get("ruleset") or "uspto-letter-2026.1")))


@app.route("/drafts/<int:project_id>/figure-compiler/model/approve", methods=["POST"])
def draft_figure_compiler_model_approve(project_id):
    auth.require_csrf()
    return _compiler_action(project_id, lambda principal:
                            _figure_compiler().approve_model(principal, project_id))


@app.route("/drafts/<int:project_id>/figure-compiler/model/resolve", methods=["POST"])
def draft_figure_compiler_model_resolve(project_id):
    auth.require_csrf()
    body = request.get_json(silent=True) or request.form
    return _compiler_action(project_id, lambda principal:
                            _figure_compiler().resolve_model_conflict(
                                principal, project_id,
                                conflict_id=str(body.get("conflict_id") or ""),
                                choice=str(body.get("choice") or "")))


@app.route("/drafts/<int:project_id>/figure-compiler/manifest/approve", methods=["POST"])
def draft_figure_compiler_manifest_approve(project_id):
    auth.require_csrf()
    return _compiler_action(project_id, lambda principal:
                            _figure_compiler().approve_manifest(principal, project_id))


@app.route("/drafts/<int:project_id>/figure-compiler/compile", methods=["POST"])
def draft_figure_compiler_compile(project_id):
    auth.require_csrf()
    return _compiler_action(project_id, lambda principal:
                            _figure_compiler().compile(principal, project_id))


@app.route("/drafts/<int:project_id>/figure-compiler/patch", methods=["POST"])
def draft_figure_compiler_patch(project_id):
    auth.require_csrf()
    body = request.get_json(silent=True) or request.form
    patch = dict(body) if hasattr(body, "items") else {}
    return _compiler_action(project_id, lambda principal:
                            _figure_compiler().patch(principal, project_id, patch))


@app.route("/drafts/<int:project_id>/figure-compiler/approve", methods=["POST"])
def draft_figure_compiler_approve(project_id):
    auth.require_csrf()
    return _compiler_action(project_id, lambda principal:
                            _figure_compiler().approve_final(principal, project_id))


@app.route("/drafts/<int:project_id>/figure-compiler/export.<format_name>")
def draft_figure_compiler_export(project_id, format_name):
    if format_name not in {"svg", "pdf"}:
        abort(404)
    try:
        _user, principal = _draft_identity()
        sheet = max(1, request.args.get("sheet", 1, type=int))
        output = _figure_compiler().export(
            principal, project_id, format_name, sheet=sheet)
        suffix = f"-sheet-{sheet}" if format_name == "svg" else ""
        return Response(
            output, mimetype="image/svg+xml" if format_name == "svg" else "application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="draft-{project_id}-figures{suffix}.{format_name}"'})
    except (drafting.DraftingError, figure_compiler.FigureCompilerError) as exc:
        return _figure_compiler_error(exc)


@app.route("/drafts/<int:project_id>/studio/message", methods=["POST"])
def draft_studio_message(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        body = request.get_json(silent=True) or request.form
        turn = _studio().start_turn(
            principal, project_id, message=str(body.get("message") or ""),
            kind=str(body.get("kind") or "revise"),
            idempotency_key=str(body.get("idempotency_key") or "") or None)
        return jsonify({"ok": True, "turn": {"id": turn["id"], "turn_no": turn["turn_no"],
                                             "status": turn["status"]}})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/studio/upload", methods=["POST"])
def draft_studio_upload(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        stored = _studio().add_uploads(principal, project_id, _uploads_from_request())
        return jsonify({"ok": True, "documents": [
            {"id": d["id"], "filename": d["filename"], "kind": d["kind"],
             "chars": d["char_count"]} for d in stored]})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/studio/reference", methods=["POST"])
def draft_studio_reference(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        body = request.get_json(silent=True) or request.form
        if body.get("remove"):
            _studio().remove_reference(principal, project_id, str(body["remove"]))
            return jsonify({"ok": True})
        record = _studio().add_reference(principal, project_id,
                                         str(body.get("publication") or ""))
        return jsonify({"ok": True, "reference": {
            "publication_number": record.get("publication_number"),
            "title": record.get("title"), "source": record.get("source")}})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/studio/search", methods=["POST"])
def draft_studio_search(project_id):
    """Start the established prior-art pipeline from the current draft and stay in studio."""
    auth.require_csrf()
    try:
        user, principal = _draft_identity()
        material = _studio().search_material(principal, project_id)
        query = material["query"]
        mode, focus, wide = "novelty", "all_text", True
        slug = search_slug(query, mode, wide=wide, search_focus=focus)
        state, detail = ensure_report(
            slug, query=query, mode=mode, wide=wide, search_focus=focus)
        if state == "busy":
            return jsonify({"ok": False, "error": f"The search server is busy: {detail}"}), 429
        (REPORTS / f"{slug}.meta.json").write_text(json.dumps(
            {"query": query, "mode": mode, "subject": None, "wide": wide,
             "ood": None, "doc_token": None, "search_focus": focus,
             "draft_project_id": int(project_id)}))
        accounts.record_search(
            user["id"], slug, query, mode, focus, None, notify_email=False,
            status="complete" if state == "ready" else "running", saved=False)
        tracked = _studio().record_search(
            principal, project_id, slug=slug, query=query,
            status="complete" if state == "ready" else "running")
        return jsonify({"ok": True, "slug": slug, "status": state,
                        "search": _draft_search_payload([tracked])[0]})
    except drafting.DraftingError as exc:
        return _studio_error(exc)
    except Exception as exc:                                  # search launch boundary
        traceback.print_exc()
        return jsonify({"ok": False,
                        "error": f"Could not start the search: {str(exc)[:200]}"}), 502


@app.route("/drafts/<int:project_id>/studio/search/<slug>/import", methods=["POST"])
def draft_studio_search_import(project_id, slug):
    """Attach ranked results from a draft-originated search without leaving the studio."""
    auth.require_csrf()
    if not valid_slug(slug):
        return jsonify({"ok": False, "error": "Invalid search id."}), 400
    try:
        _user, principal = _draft_identity()
        report_view = _draft_report_loader(principal, slug, principal.user_id)
        body = request.get_json(silent=True) or request.form
        pubs = body.get("publications") if isinstance(body, dict) else body.getlist("publications")
        if not pubs:
            pubs = [card.get("pub") for card in (report_view.get("cards") or [])[:5]
                    if card.get("pub")]
        if isinstance(pubs, str):
            pubs = [pubs]
        count = _studio().import_search(principal, project_id, slug, list(pubs)[:10])
        return jsonify({"ok": True, "imported": count})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/studio/document/<int:document_id>/delete", methods=["POST"])
def draft_studio_document_delete(project_id, document_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        _studio().remove_document(principal, project_id, document_id)
        return jsonify({"ok": True})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/studio/figure", methods=["POST"])
def draft_studio_figure(project_id):
    """Draw one of the draft's own figures. Synchronous: it takes about five seconds."""
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        body = request.get_json(silent=True) or request.form
        region = body.get("region")
        if region is not None and (not isinstance(region, (list, tuple)) or len(region) != 4):
            raise drafting.DraftingValidationError("Select one rectangular area to edit.")
        drawn = _studio().draw_figure(
            principal, project_id, label=body.get("label") or "",
            caption=body.get("caption") or "", instruction=body.get("instruction") or "",
            figure_id=int(body["figure_id"]) if body.get("figure_id") else None,
            region=region)
        return jsonify({"ok": True, "figure": drawn})
    except drafting.DraftingError as exc:
        return _studio_error(exc)
    except Exception as exc:                                # noqa: BLE001 - the image model
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Could not draw that: {str(exc)[:200]}"}), 502


@app.route("/drafts/<int:project_id>/studio/figure/<int:figure_id>/manual", methods=["POST"])
def draft_studio_figure_manual(project_id, figure_id):
    """Store a flattened browser-canvas edit as a new figure version."""
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        storage = request.files.get("image")
        if not storage:
            raise drafting.DraftingValidationError("The edited drawing was not received.")
        png = storage.read(draft_figures.MAX_PNG_BYTES + 1)
        if len(png) > draft_figures.MAX_PNG_BYTES:
            raise drafting.DraftingValidationError("The edited drawing is too large.")
        saved = _studio().save_figure(
            principal, project_id, figure_id, png,
            instruction=request.form.get("instruction") or "Manual drawing edit")
        return jsonify({"ok": True, "figure": saved})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/studio/figure/<int:figure_id>/delete", methods=["POST"])
def draft_studio_figure_delete(project_id, figure_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        _studio().delete_figure(principal, project_id, figure_id)
        return jsonify({"ok": True})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/studio/photo-to-sketch", methods=["POST"])
def draft_studio_photo_to_sketch(project_id):
    """Convert an uploaded product/part photo into a patent drawing."""
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        storage = request.files.get("image")
        if not storage or not storage.filename:
            raise drafting.DraftingValidationError("Choose a product or part image first.")
        data = storage.read(draft_figures.MAX_SOURCE_BYTES + 1)
        if len(data) > draft_figures.MAX_SOURCE_BYTES:
            raise drafting.DraftingValidationError(
                f"Choose an image smaller than "
                f"{draft_figures.MAX_SOURCE_BYTES // (1024 * 1024)} MB.")
        drawn = _studio().photo_to_sketch(
            principal, project_id, image=data, content_type=storage.mimetype or "",
            label=request.form.get("label") or "", caption=request.form.get("caption") or "",
            instruction=request.form.get("instruction") or "")
        return jsonify({"ok": True, "figure": drawn})
    except drafting.DraftingError as exc:
        return _studio_error(exc)
    except Exception as exc:                                  # image provider boundary
        traceback.print_exc()
        return jsonify({"ok": False,
                        "error": f"Could not convert that image: {str(exc)[:200]}"}), 502


@app.route("/drafts/<int:project_id>/studio/cancel", methods=["POST"])
def draft_studio_cancel(project_id):
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        body = request.get_json(silent=True) or request.form
        _studio().cancel(principal, project_id, int(body.get("turn_id") or 0))
        return jsonify({"ok": True})
    except (drafting.DraftingError, ValueError) as exc:
        return _studio_error(exc if isinstance(exc, drafting.DraftingError)
                             else drafting.DraftingValidationError("Unknown drafting turn."))


@app.route("/drafts/<int:project_id>/studio/review", methods=["POST"])
def draft_studio_review(project_id):
    """Re-run the consistency review over the current version without drafting anything."""
    auth.require_csrf()
    try:
        _user, principal = _draft_identity()
        return jsonify({"ok": True, **_studio().rerun_review(principal, project_id)})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


def _readiness_for(principal, project_id):
    service = _drafting_service()
    project = service.get_project(principal, project_id, include_versions=False)
    version_no = request.args.get("version", type=int) or int(project.get("latest_version_no") or 0)
    if not version_no:
        raise drafting.DraftingNotFound("There is no draft version yet.")
    version = service.get_version(principal, project_id, version_no)
    qa = draft_studio.StudioRepository().latest_qa(project_id)
    references = project.get("references") or []
    figures = _figures_for(project)
    report = draft_uspto.readiness(project=project, version=version, qa=qa,
                                   references=references, figures=figures)
    return project, version, report, references


@app.route("/api/drafts/<int:project_id>/filing")
def api_draft_filing(project_id):
    try:
        _user, principal = _draft_identity()
        _project, version, report, _refs = _readiness_for(principal, project_id)
        report["version_no"] = version["version_no"]
        return jsonify({"ok": True, "readiness": report})
    except drafting.DraftingError as exc:
        return _studio_error(exc)


@app.route("/drafts/<int:project_id>/download/filing.<fmt>")
def draft_filing_download(project_id, fmt):
    if fmt not in {"docx", "txt"}:
        abort(404)
    try:
        _user, principal = _draft_identity()
        project, version, report, references = _readiness_for(principal, project_id)
        stem = draft_export._clean_filename(str(project.get("title") or ""))
        name = f"{stem}-filing-v{version['version_no']}.{fmt}"
        if fmt == "txt":
            return Response(draft_uspto.filing_text(project, version), mimetype="text/plain",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})
        return send_file(
            draft_uspto.render_filing_docx(project, version, readiness_report=report,
                                           references=references),
            as_attachment=True, download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    except drafting.DraftingError as exc:
        return render_template("notfound.html", slug=str(exc)), _draft_error_status(exc)


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
    h["mail"] = notifications.transport_status()
    h["draft_worker"] = draft_worker.status()
    h["draft_turn_worker"] = draft_studio_service.status()
    return jsonify(h)


RECOVERY_GRACE_SECONDS = 120


def recover_interrupted_searches():
    """Reconcile searches that were running when the previous process died.

    Completion is recorded in-process: ``_generate`` finishes, marks the saved search complete
    and queues the email. A deploy, a restart or an OOM in the middle of a multi-minute search
    therefore leaves the row saying ``running`` for ever, and the user who was told "you can
    close this tab, we will email you" is never emailed and never sees the search finish.

    At startup nothing is running in THIS process, so every stale ``running`` row belongs to a
    process that is gone. Each is settled against what is actually on disk:

      * a finished report (``partial`` false) -> the work DID complete and only the bookkeeping
        was lost: mark it complete and queue the email that was promised;
      * a partial report or none at all       -> mark it failed, so Search history says so and
        offers a re-run instead of showing a search that will never end.

    Fail-soft and best-effort: the accounts store may be unavailable, and a search page must
    still come up if it is.
    """
    settled = {"completed": 0, "failed": 0}
    try:
        if not auth.accounts_enabled(app):
            return settled
        with db.cursor() as cur:
            cur.execute("SELECT DISTINCT slug FROM app_saved_searches "
                        "WHERE status='running' AND updated_at < now() - interval '%s seconds'"
                        % int(RECOVERY_GRACE_SECONDS))
            slugs = [r["slug"] for r in cur.fetchall()]
    except Exception:
        traceback.print_exc()
        return settled

    for slug in slugs:
        try:
            rep = None
            p = report_path(slug)
            if p.exists():
                try:
                    rep = json.loads(p.read_text())
                except Exception:
                    rep = None
            if rep is not None and not rep.get("partial"):
                notifications.queue_search_completion(slug)
                settled["completed"] += 1
            else:
                accounts.mark_search_failed(slug)
                #  Interrupted by a restart and not recoverable. The user asked to be told when it
                #  was done; being told it is NOT done is the same promise.
                try:
                    notifications.queue_search_failure(
                        slug, reason="the search was interrupted and could not be resumed")
                except Exception:
                    traceback.print_exc()
                settled["failed"] += 1
        except Exception:
            traceback.print_exc()
    if settled["completed"] or settled["failed"]:
        print(f"[recovery] settled interrupted searches: {settled['completed']} completed, "
              f"{settled['failed']} marked failed", flush=True)
    return settled


# ---- auth + rate limiting (registered LAST, after every route exists) ------------------------
auth.init_app(app, state_path=DATA / "run_budget.json")
notifications.init_app(app)
for _schema in (deliverables.ensure_schema, library.ensure_schema, draft_figures.ensure_schema,
                draft_studio.ensure_schema):
    try:
        _schema()
    except Exception:                   # a missing accounts store must not stop the app booting
        traceback.print_exc()
draft_worker.init_app(app, _drafting_service)
draft_studio_service.init_app(app, _turn_runner)
def _report_is_finished(slug):
    p = report_path(slug)
    if not p.exists():
        return False
    try:
        return not json.loads(p.read_text()).get("partial")
    except Exception:
        return False


def _drop_partial_report(slug):
    for suffix in (".json", ".view.json", ".detail-preview.json"):
        (REPORTS / f"{slug}{suffix}").unlink(missing_ok=True)


def _queue_launch(slug, payload):
    """Dispatcher hook: start one queued run. -> 'started' | 'done' | 'busy' | 'gone'."""
    try:
        st, _ = ensure_report(
            slug, query=payload.get("query"), subject=payload.get("subject"),
            mode=payload.get("mode") or "novelty", wide=bool(payload.get("wide")),
            doc_token=payload.get("doc_token"),
            search_focus=payload.get("search_focus") or "all_text", from_queue=True,
            depth=payload.get("depth") or "deep", restart_partial=True)
    except Exception:
        traceback.print_exc()
        return "gone"
    return {"ready": "done", "running": "started", "busy": "busy"}.get(st, "gone")


if "PYTEST_CURRENT_TEST" not in os.environ:
    recover_interrupted_searches()
    draft_studio_service.recover_interrupted_turns()
    try:
        run_queue.ensure_schema()
        run_queue.requeue_orphans(report_finished=_report_is_finished,
                                  drop_partial=_drop_partial_report)
        run_queue.start_dispatcher(_queue_launch)
    except Exception:                   # the queue store being down must not stop the app booting
        traceback.print_exc()


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
