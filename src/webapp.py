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


def _job_event(slug, job):
    """The single wire shape shared by /status (poll) and /events (SSE), so the fallback path and
    the streaming path can never disagree."""
    st = job.get("status", "unknown")
    exists = report_path(slug).exists()
    return {"kind": job.get("kind", "progress"), "slug": slug, "status": st,
            "msg": job.get("msg", ""),
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
        verdict = None
        try:
            verdict = domain_detect.detect(query, retriever=retriever())
        except Exception:
            traceback.print_exc()

        def on_event(stage, data):
            # Stream progress + a first render. 'partial' writes an un-reranked snapshot (cards
            # only) the moment the seed search returns, so the user sees results in seconds.
            if stage == "elements":
                _set_job(slug, kind=stage, msg=f"Decomposed into {data['n']} elements — searching all 8 channels…")
            elif stage == "partial":
                rep = data["report"]; rep["partial"] = True
                _write_report(slug, rep)
                _set_job(slug, kind=stage, status="partial",
                         msg="Showing the first matches — refining (more channels, rounds, claim chart)…")
            elif stage == "seeded":
                _set_job(slug, kind=stage, msg=f"{data['families']} candidate families — expanding via citations, families, cross-lingual…")
            elif stage == "round":
                _set_job(slug, kind=stage, msg=f"Refinement round {data['round']}: {data['families']} families — reranking…")
            elif stage == "reranking":
                _set_job(slug, kind=stage, msg=f"Reranking {data['families']} families + grounding the claim chart…")

        rep = A.run(query, subject=subject, mode=mode,
                    cfg=AgentConfig(mode=mode, max_rounds=2, elements_per_round=3, ground=True),
                    on_event=on_event)
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
        traceback.print_exc()
        return {"ok": False, "error": str(e)[:300], "hits": []}
    d = fed.to_dict()
    d["hits"] = [{"pub": h.pub_number, "title": h.title, "abstract": (h.abstract or "")[:600],
                  "assignee": h.assignee, "date": h.date, "country": h.country,
                  "cpc": h.cpc[:6], "url": h.url, "family_id": h.family_id,
                  "sources": h.sources, "rank": h.rank}
                 for h in (fed.hits or [])[:40]]
    return d


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


# ---- routes --------------------------------------------------------------------------------
EXAMPLE_QUERIES = [
    {"label": "Handheld vacuum lifter",
     "text": ("A cordless handheld vacuum lifter for glass and stone panels, with a flexible "
              "sealing lip, an electric vacuum pump that keeps running to hold grip on rough or "
              "porous surfaces, and a pressure sensor that alarms the operator when grip vacuum is lost.")},
    {"label": "Robotic EOAT gripper",
     "text": ("A robotic end-of-arm vacuum gripper for handling sheets and panels, with an array of "
              "independently-controlled suction zones, a compliant foam seal, and a venturi vacuum "
              "generator, able to release a workpiece by a controlled air pulse.")},
    {"label": "Suction cup with check valve",
     "text": ("A suction cup for lifting non-porous objects, comprising an elastomer cup body, a "
              "self-sealing check valve that closes when the cup contacts the surface, and a manual "
              "pump lever to evacuate the chamber.")},
]


@app.route("/")
def index():
    gold = [{"id": e["id"], "category": e["category"], "mode": e["mode"],
             "subject": e.get("anchor_publication"), "notes": e.get("notes", ""),
             "cached": report_path(e["id"]).exists()}
            for e in _GOLD.values()]
    return render_template("index.html", gold=gold, examples=EXAMPLE_QUERIES)


@app.route("/run", methods=["POST"])
def run():
    gold_id = request.form.get("gold_id", "").strip()
    if gold_id and gold_id in _GOLD:
        e = _GOLD[gold_id]
        st, why = ensure_report(gold_id, query=e["query_text"],
                                subject=e.get("anchor_publication"), mode=e["mode"])
        if st == "busy":
            return jsonify({"error": "server busy", "detail": why}), 429
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
        return jsonify({"error": "mode_not_available",
                        "mode": getattr(e.mode, "value", str(e.mode)),
                        "detail": e.message, "missing": e.missing}), 400
    except ValueError as e:
        return jsonify({"error": "unknown_mode", "detail": str(e)}), 400
    subject = request.form.get("subject", "").strip() or None
    wide = request.form.get("wide") == "1"
    if not query:
        return redirect(url_for("index"))
    # `wide` MUST be part of the slug: a wide result and a narrow one are different reports and
    # would otherwise overwrite each other's cache.
    slug = slugify(query + "|" + mode + ("|wide" if wide else ""))
    st, why = ensure_report(slug, query=query, subject=subject, mode=mode, wide=wide)
    if st == "busy":
        return jsonify({"error": "server busy", "detail": why}), 429
    # remember adhoc meta for the report page title
    (REPORTS / f"{slug}.meta.json").write_text(json.dumps(
        {"query": query, "mode": mode, "subject": subject, "wide": wide}))
    return redirect(url_for("report", slug=slug))


@app.route("/report/<slug>")
def report(slug):
    regen = request.args.get("rerun") == "1"
    query = subject = None
    mode = "novelty"
    if slug in _GOLD:
        e = _GOLD[slug]
        query, subject, mode = e["query_text"], e.get("anchor_publication"), e["mode"]
        title = f"{slug}  ·  {e['category']}"
    else:
        meta = REPORTS / f"{slug}.meta.json"
        if meta.exists():
            m = json.loads(meta.read_text())
            query, subject, mode = m["query"], m.get("subject"), m.get("mode", "novelty")
        title = "Ad-hoc search"
    status, rep = ensure_report(slug, query=query, subject=subject, mode=mode, regen=regen)
    if status == "missing":
        return render_template("notfound.html", slug=slug), 404
    if status == "busy":
        return render_template("notfound.html", slug=f"{slug} — {rep}"), 429
    if status != "ready":
        return render_template("generating.html", slug=slug, title=title,
                               query=(query or "")[:400], mode=mode)
    view = _build_view_cached(slug, rep, regen)
    view["slug"] = slug
    view["title"] = title
    view["is_gold"] = slug in _GOLD
    return render_template("report.html", v=view)


def _build_view_cached(slug, rep, regen=False):
    """Cache the built view (query embed + DB resolution) to <slug>.view.json for instant reloads.
    A partial/streaming snapshot is NEVER cached (it would shadow the final report) and is flagged
    so the report page keeps polling and upgrades itself when the full run finishes."""
    partial = bool(rep.get("partial"))
    vp = REPORTS / f"{slug}.view.json"
    if vp.exists() and not regen and not partial:
        try:
            return json.loads(vp.read_text())
        except Exception:
            pass
    view = webview.build_view(rep, top_n=25)
    view["partial"] = partial
    if not partial:
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
    return jsonify({"ready": ev["ready"], "status": ev["status"], "done": ev["done"],
                    "msg": ev["msg"]})


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
def _rationale(slug, pub, query, elements, biblio_txt, matched_txt):
    cache = RATIONALE / f"{slug}__{pub}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    # Deterministic guard: with no reference text (title-less junk / un-enriched thin doc) an LLM
    # can only hallucinate. Return an explicit "not verifiable" instead of inventing disclosure.
    title_abs = re.sub(r"^\S+\s*", "", (biblio_txt or "").strip()).strip(" .")
    if len(title_abs) < 8 and not (matched_txt or "").strip():
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
        "(its title, abstract, and best-matching passage). Do NOT use outside knowledge and do NOT "
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
        "omitting over guessing.")
    usr = (f"Invention query: {query[:800]}\n\nInvention elements (candidates — include only the "
           f"ones the reference text actually discloses, with a quote): {json.dumps(elements)}\n\n"
           f"Reference (the ONLY evidence you may use): {biblio_txt[:900]}\n\n"
           f"Best-matching passage from the reference:\n{(matched_txt or '(none)')[:900]}")
    out = llm.chat_json(sysmsg, usr, max_tokens=500) or {}
    reads_on = _ground_reads_on(out.get("reads_on") or [], f"{biblio_txt} {matched_txt or ''}")
    res = {"why": out.get("why", ""), "reads_on": reads_on}
    cache.write_text(json.dumps(res))
    return res


_WORD_RE = re.compile(r"[a-z0-9]+")

def _ground_reads_on(raw, ref_text, min_overlap=0.6):
    """Keep an element only if the model's evidence quote is actually grounded in the reference
    text we showed it (>=60% of the quote's content words present). Deterministic anti-overclaim:
    a fabricated or absent-from-text element is dropped even if the model listed it. Tolerates the
    old string-only shape (kept as-is, since there is no evidence to verify)."""
    hay = set(_WORD_RE.findall((ref_text or "").lower()))
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
        words = [w for w in _WORD_RE.findall(ev.lower()) if len(w) > 3]
        if words and sum(w in hay for w in words) >= max(1, min_overlap * len(words)):
            kept.append(el)          # evidence is grounded in the shown text -> keep
        # no evidence, or evidence not found in the text -> drop as ungrounded (anti-overclaim)
    # de-dup preserving order
    seen = set(); out = []
    for e in kept:
        if e.lower() not in seen:
            seen.add(e.lower()); out.append(e)
    return out


@app.route("/api/ref/<pub>")
def api_ref(pub):
    slug = request.args.get("slug", "")
    disp = enrich_display.enrich_for_display(pub)
    # DB sections + matched coordinate (for highlighting)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
        row = cur.fetchone()
        secs, matched = None, None
        if row:
            pid = row["id"]
            secs = webview.sections(cur, pid)
            q = _query_for_slug(slug)
            if q:
                qv = _query_vec(slug, q)
                matched = webview.match_in_pub(cur, pid, qv)
    # fall back to SerpApi claims when DB has none
    if secs is not None and not secs["claims"] and disp.get("claims"):
        secs["claims"] = [{"claim_no": i + 1, "independent": None, "text": c, "resolved_text": None}
                          for i, c in enumerate(disp["claims"])]
    rationale = None
    if slug:
        q = _query_for_slug(slug)
        rep = _load_report(slug)
        if q and rep:
            biblio_txt = f"{pub} {disp.get('title') or ''}. {disp.get('abstract') or ''}"
            rationale = _rationale(slug, pub, q, rep.get("elements", []), biblio_txt,
                                   (matched or {}).get("text"))
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
    view = webview.build_view(rep, top_n=25)
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
    key = hashlib.sha1((slug + "|" + fmt + "|" + ",".join(sorted(pubs))).encode()).hexdigest()[:12]
    out = EXPORTS / f"{slug}__{key}.{fmt}"
    if not out.exists():
        model = export_data.assemble(slug, pubs)
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
        res = [{"pub": v["pub"], "title": v["title"], "country": v["country"],
                "score": round(1 - v["d"], 3)}
               for v in sorted(best.values(), key=lambda x: x["d"])[:12]]
    return jsonify({"pub": pub, "results": res})


# ---- side-by-side compare ------------------------------------------------------------------
@app.route("/compare")
def compare():
    slug = request.args.get("slug", "")
    pubs = [p for p in request.args.get("pubs", "").split(",") if p.strip()][:3]
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
