"""Results page — Flask app (spec Milestone 2). Serves on 127.0.0.1:8631.

Reuses Retriever + CoverageAgent + the DB + enrich_display. Element×Reference claim chart,
ranked prior-art cards with drawings/PDF/highlighted sections, coverage ledger.

Report generation (CoverageAgent.run) is slow, so it runs in a background thread with a poll
endpoint; the agent report is cached to data/reports/<slug>.json and never blocks the request.
Per-card drawings/PDF/sections/rationale are enriched lazily via /api/ref.
"""
from __future__ import annotations
import json, re, threading, hashlib, time, traceback
from pathlib import Path
from flask import (Flask, render_template, request, jsonify, redirect, url_for,
                   send_from_directory, abort)
import db, embed, goldset, webview, enrich_display, llm
import export_data, export_pdf, export_docx
from retrieval import Retriever
from agent import CoverageAgent, AgentConfig
from config import DATA

app = Flask(__name__, template_folder="../templates", static_folder="../static")

REPORTS = DATA / "reports"
RATIONALE = DATA / "rationale"
EXPORTS = DATA / "reports" / "exports"
FLAGS = DATA / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)
RATIONALE.mkdir(parents=True, exist_ok=True)
EXPORTS.mkdir(parents=True, exist_ok=True)

_GOLD = {e["id"]: e for e in goldset.load()["entries"]}
_JOBS = {}          # slug -> {"status": "running|done|error", "msg": ...}
_JOB_LOCK = threading.Lock()
_R = None           # lazy singleton Retriever (loads family map once)
_R_LOCK = threading.Lock()


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
def _generate(slug, query, subject, mode):
    with _JOB_LOCK:
        _JOBS[slug] = {"status": "running", "msg": "Running coverage-ledger agent…", "t0": time.time()}
    try:
        A = CoverageAgent(retriever())
        rep = A.run(query, subject=subject, mode=mode,
                    cfg=AgentConfig(mode=mode, max_rounds=2, elements_per_round=3, ground=True))
        report_path(slug).write_text(json.dumps(rep, default=str, indent=1))
        with _JOB_LOCK:
            _JOBS[slug] = {"status": "done", "msg": "done"}
    except Exception as e:
        traceback.print_exc()
        with _JOB_LOCK:
            _JOBS[slug] = {"status": "error", "msg": str(e)[:300]}


def ensure_report(slug, query=None, subject=None, mode="novelty", regen=False):
    """Return ('ready'|'running', report_or_None). Kicks off background generation if needed."""
    p = report_path(slug)
    if p.exists() and not regen:
        try:
            return "ready", json.loads(p.read_text())
        except Exception:
            pass
    with _JOB_LOCK:
        job = _JOBS.get(slug)
    if job and job["status"] == "running":
        return "running", None
    if query is None:
        return "missing", None
    # subject resolution for gold subjects (pub number)
    subj_obj = _subject_obj(subject)
    if regen:
        p.unlink(missing_ok=True)
        (REPORTS / f"{slug}.view.json").unlink(missing_ok=True)
    threading.Thread(target=_generate, args=(slug, query, subj_obj, mode), daemon=True).start()
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
@app.route("/")
def index():
    gold = [{"id": e["id"], "category": e["category"], "mode": e["mode"],
             "subject": e.get("anchor_publication"), "notes": e.get("notes", ""),
             "cached": report_path(e["id"]).exists()}
            for e in _GOLD.values()]
    return render_template("index.html", gold=gold)


@app.route("/run", methods=["POST"])
def run():
    gold_id = request.form.get("gold_id", "").strip()
    if gold_id and gold_id in _GOLD:
        e = _GOLD[gold_id]
        ensure_report(gold_id, query=e["query_text"], subject=e.get("anchor_publication"),
                      mode=e["mode"])
        return redirect(url_for("report", slug=gold_id))
    query = request.form.get("query", "").strip()
    mode = request.form.get("mode", "novelty").strip()
    subject = request.form.get("subject", "").strip() or None
    if not query:
        return redirect(url_for("index"))
    slug = slugify(query + "|" + mode)
    ensure_report(slug, query=query, subject=subject, mode=mode)
    # remember adhoc meta for the report page title
    (REPORTS / f"{slug}.meta.json").write_text(json.dumps(
        {"query": query, "mode": mode, "subject": subject}))
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
    if status != "ready":
        return render_template("generating.html", slug=slug, title=title,
                               query=(query or "")[:400], mode=mode)
    view = _build_view_cached(slug, rep, regen)
    view["slug"] = slug
    view["title"] = title
    view["is_gold"] = slug in _GOLD
    flags = load_flags(slug)
    for c in view["cards"]:
        f = flags.get(c["pub"], {})
        c["triage"] = f.get("flag", "")      # relevant|maybe|not — distinct from c.flag (country emoji)
        c["note"] = f.get("note", "")
    return render_template("report.html", v=view)


def _build_view_cached(slug, rep, regen=False):
    """Cache the built view (query embed + DB resolution) to <slug>.view.json for instant reloads."""
    vp = REPORTS / f"{slug}.view.json"
    if vp.exists() and not regen:
        try:
            return json.loads(vp.read_text())
        except Exception:
            pass
    view = webview.build_view(rep, top_n=25)
    vp.write_text(json.dumps(view, default=str))
    return view


@app.route("/status/<slug>")
def status(slug):
    with _JOB_LOCK:
        job = _JOBS.get(slug, {})
    ready = report_path(slug).exists() and (not job or job.get("status") == "done")
    return jsonify({"ready": ready, "status": job.get("status", "unknown"),
                    "msg": job.get("msg", "")})


# ---- lazy per-card enrichment (drawings + PDF + sections + rationale) -----------------------
def _rationale(slug, pub, query, elements, biblio_txt, matched_txt):
    cache = RATIONALE / f"{slug}__{pub}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    sysmsg = ("You are a patent prior-art analyst. In 1-2 sentences, say why this reference is "
              "relevant to the invention and which of the listed elements it most reads on. "
              'Return JSON {"why": "...", "reads_on": ["element phrase", ...]}. Be concrete and terse.')
    usr = (f"Invention query: {query[:800]}\n\nInvention elements: {json.dumps(elements)}\n\n"
           f"Reference: {biblio_txt[:900]}\n\nBest-matching passage from the reference:\n{(matched_txt or '')[:900]}")
    out = llm.chat_json(sysmsg, usr, max_tokens=400) or {}
    res = {"why": out.get("why", ""), "reads_on": out.get("reads_on", [])}
    cache.write_text(json.dumps(res))
    return res


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


@app.route("/figures/<pub>/<path:fname>")
def figures(pub, fname):
    d = enrich_display.FIGDIR / pub
    if not (d / fname).exists():
        abort(404)
    return send_from_directory(d, fname)


@app.route("/pdf/<pub>")
def pdf(pub):
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
        cur.execute(
            "SELECT p.publication_number, p.title, p.country, "
            "min(c.embedding <=> %s) AS d FROM chunks c JOIN publications p ON p.id=c.publication_id "
            "WHERE c.embedding IS NOT NULL AND p.id <> %s "
            "GROUP BY p.publication_number, p.title, p.country ORDER BY d LIMIT 12",
            (er["embedding"], pid))
        res = [{"pub": r["publication_number"], "title": r["title"], "country": r["country"],
                "score": round(1 - float(r["d"]), 3)} for r in cur.fetchall()]
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


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "gold": len(_GOLD)})


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8631
    print(f"Results page on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
