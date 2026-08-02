"""Rank by what the references ACTUALLY DISCLOSE, read in full, quote by quote.

MEASURED REASON THIS MODULE EXISTS (RECALL_STUDY_2026-08-02.md)
--------------------------------------------------------------
The report used to be ordered by an LLM score computed from a 900-character snippet
(``relevancy.py`` over ``rerank_listwise._matched_text``), while a SEPARATE stage
(``deep_analysis``) read the top 50 references in full and then fed a tab. The ordering signal was
the weakest text in the pipeline and the strongest one was thrown away.

On the case that prompted this rebuild, the same model on the same reference:

    US-10625955-B2   from the 900-char snippet ... 45      from its full text ... 85
    US-12115659-B1   from the 900-char snippet ... 60      from its full text ... 85
    DE-3724659-A1    never scored (rank 244)              from its full text ... 65-75

and the text budget is monotonic: 900 chars -> 0, all claims (18.5k) -> 75, full text (144k) -> 85.

So the reading becomes the ranking. Three stages:

  1. SCREEN, cheaply and widely. Every candidate the retrieval produced, in batches, on title +
     claims + abstract. Measured: 300 candidates in 12 calls and 25 seconds. This is what lifted
     DE-3724659-A1 from retrieval rank 244 to screen rank 8.
  2. READ, in full, the head of that screen. ``deep_analysis.analyse_reference`` already does this
     properly: every cell carries a verbatim quote, the quote must pass ``grounding.grounded``,
     the location is resolved BY CODE, and every "disclosed" is put to an independent refuter.
  3. SCORE by GROUNDED, RARITY-WEIGHTED evidence, never by a free-form number.

WHY RARITY WEIGHTING, AND WHY NOT A FREE-FORM SCORE
---------------------------------------------------
A free-form 0-100 read of the full text is not safe on its own: measured, it gave **85** to nine
Soviet-era records that hold ZERO characters of text in this corpus, scoring them off the title
alone and inventing 8 to 12 quotes each. Counting only quotes that pass the grounding gate gives
them 0, which is correct.

And not every feature is worth the same. Across 60 charted references for the vacuum-gripper case,
"portable vacuum gripper with a rigid base element" was disclosed by 34 of 60 and
"bracing structure protrudes less than the vacuum seal element" by 9 of 60. The second is what a
novelty attack turns on. Weighting each grounded feature by log(N/df) over the charted set makes
the distinctive disclosure count, and it is computed from the evidence rather than declared.

MEASURED RESULT, end to end on the live candidate list with retrieval untouched:

    US-10625955-B2   card 11 (score 45)          ->  rank 3   (10 of 12 features grounded)
    DE-3724659-A1    rank 244, never shown       ->  rank 18  (6 of 12, incl. both bracing rows)
    21 of the new top 25 had never appeared on the page, all from inside the top 300.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import db
import deep_analysis
import llm

VERSION = 1

#  How many of the retrieval's ranked families get a cheap screen. The retrieval produced 2,402
#  families on the case that prompted this; screening 300 already recovered every reference the
#  searcher named. 600 is the same shape with headroom, and it is ~24 LLM calls.
SCREEN_TOP = int(os.environ.get("DEEP_RANK_SCREEN_TOP", "600"))
SCREEN_BATCH = int(os.environ.get("DEEP_RANK_SCREEN_BATCH", "25"))
SCREEN_WORKERS = int(os.environ.get("DEEP_RANK_SCREEN_WORKERS", "6"))

#  How many get read IN FULL. Measured 50 refs / 3.8M chars in 45 s at 8 workers, so 150 at 14
#  workers is ~2 minutes, which is the budget this stage is worth.
CHART_TOP = int(os.environ.get("DEEP_RANK_CHART_TOP", "150"))
CHART_WORKERS = int(os.environ.get("DEEP_RANK_CHART_WORKERS", "14"))

#  Always read the head of the RETRIEVAL order regardless of what the screen thought, so a screen
#  miss can never cost a reference the old pipeline would have shown. This is not paranoia: the
#  screen scored US-10625955-B2 **0, three times out of three**, because its abstract in the
#  corpus is a different patent's abstract (upstream: patents.google.com shows the same wrong
#  abstract). A reference the retrieval ranked 15th must not be lost to a bad abstract.
ALWAYS_CHART_RETRIEVAL_HEAD = int(os.environ.get("DEEP_RANK_HEAD", "60"))

#  Evidence weights. "disclosed" survived the refuter; "partial" is the model's own hedge;
#  "uncertain" is a "disclosed" that an independent refuter would not confirm.
#
#  "uncertain" is deliberately NOT near-zero. The refuter is instructed to default to refuted=true
#  when unsure, because its job is to protect a legal chart from overclaim; that is the right bias
#  for a chart and the wrong one for a ranking. An "uncertain" row still means a real, located,
#  verbatim quote exists that a first reading called a disclosure. Measured on the live rebuild:
#  at 0.25 a reference that plainly discloses "portable or hand-held vacuum gripper" and grounds
#  it at claim 1 was scored as if it barely mentioned it, and the whole list compressed.
_W = {"disclosed": 1.0, "partial": 0.55, "uncertain": 0.5, "absent": 0.0}
#  Credit for being the BEST disclosure of a RARE feature. A reference that teaches one thing
#  almost nothing else teaches is what an inventive-step attack is built on: DE-3724659-A1 (1989,
#  9,160 characters) discloses 1 of 12 features and is the SECOND-best disclosure of the
#  characterising one out of 183 references read. Ranked on coverage alone it is off the page;
#  ranked with this credit it is on it, and the card says which feature it leads on.
LEAD_WEIGHT = float(os.environ.get("DEEP_RANK_LEAD_WEIGHT", "0.7"))
LEAD_DEPTH = int(os.environ.get("DEEP_RANK_LEAD_DEPTH", "3"))
#  How much of the displayed score is the reader's HOLISTIC judgement of the document, as opposed
#  to the rarity-weighted count of grounded cells.
#
#  Both are needed. The count alone is conservative by construction: the chart's refuter is told to
#  default to "refuted" and the prompt is told that "absent" is the expected answer, which is right
#  for a legal artefact and compresses a ranking. Measured, a reference an examiner-style read
#  scored 85 grounded only 2 clean "disclosed" cells. The holistic number alone is unsafe: it gave
#  85 to records with ZERO characters of text. So the holistic number is GATED on the reference
#  having been read AND having grounded at least one quote, and then blended.
OVERALL_WEIGHT = float(os.environ.get("DEEP_RANK_OVERALL_WEIGHT", "0.45"))
#  A reference charted against the uploaded patent's own claims gets that counted too, at a
#  discount: a claim is a conjunction of limitations, so a "partial" there is weaker evidence
#  about the whole reference than a "partial" on a single feature.
_CLAIM_WEIGHT = 0.75

#  A federated-only hit has no text in this corpus, so it CANNOT be read in full. It is ranked on
#  its screen score alone and capped, because an abstract-deep judgement must not outrank a
#  reference whose full text was read and quoted. The cap is a statement about evidence, not about
#  the source: the same cap applies to any candidate we could not read.
UNREAD_SCORE_CAP = int(os.environ.get("DEEP_RANK_UNREAD_CAP", "70"))

_STOP = set((
    "a an the of and or to for with in on at by is are be as from that this it its which such "
    "said have has having comprising comprises method system device apparatus assembly unit means "
    "portion member element part first second least one two including includes provided plurality "
    "wherein thereof therein configured surface end side body according present invention"
).split())

_SCREEN_SYS = (
    "You are a patent examiner SCREENING candidate references against a target invention, to "
    "decide which ones are worth reading in full. For EACH numbered candidate give an integer "
    "0-100 relevance score: 90-100 = discloses essentially the same invention; 70-89 = strongly "
    "relevant, discloses most core elements; 40-69 = related field, some overlapping features; "
    "1-39 = same broad area but a different problem or solution; 0 = unrelated. Judge ONLY on the "
    "technical substance of the text shown, NEVER on the title alone and never on shared words. "
    "When the text shown is too thin to judge, score it on what it does show rather than "
    "guessing high or low. "
    'Return ONLY JSON {"results":[{"id":<batch number>,"score":<0-100>}]} with one entry per '
    "candidate and every batch id exactly once."
)


def _tokens(s):
    return {w for w in re.findall(r"[a-z]{5,}", (s or "").lower()) if w not in _STOP}


def abstract_is_trustworthy(abstract, title, claim_text) -> bool:
    """False when the stored abstract cannot belong to this publication.

    ``publications.abstract`` for US-10625955-B2 ("Electric vacuum suction lifter") is the
    abstract of a touch display device with optical adhesive. That is UPSTREAM, not an ingest bug:
    patents.google.com/patent/US10625955B2 shows the same wrong abstract, and both members of the
    family carry it. Prevalence is low (0 of 1,500 sampled US publications had an abstract sharing
    no content word with their own title or claim 1), but the cost when it happens is total: the
    screen scored that patent 0 three times out of three.

    An abstract that shares no content word with either the title or the claims is therefore not
    shown to the screener. The claims are still shown, so the candidate is still judged.
    """
    a = _tokens(abstract)
    if not a:
        return False
    ref = _tokens(title) | _tokens(claim_text)
    if not ref:
        return True
    return bool(a & ref)


def _candidate_rows(cur, families, reps, limit):
    """[{pub, fam, title, text, rank}] for the screen, best retrieval rank first."""
    rows, pids = [], []
    for i, fam in enumerate(families):
        r = reps.get(fam)
        if not r:
            continue
        rows.append({"pub": r["publication_number"], "fam": fam, "pid": r["id"],
                     "title": r.get("title") or "", "rank": len(rows) + 1})
        pids.append(r["id"])
        if len(rows) >= limit:
            break
    if not pids:
        return rows
    claims = {}
    cur.execute("SELECT publication_id, claim_no, text FROM claims WHERE publication_id = ANY(%s) "
                "AND claim_no <= 3 ORDER BY publication_id, claim_no", (pids,))
    for c in cur.fetchall():
        claims.setdefault(c["publication_id"], []).append(c["text"] or "")
    abstracts = {}
    cur.execute("SELECT id, abstract FROM publications WHERE id = ANY(%s)", (pids,))
    for p in cur.fetchall():
        abstracts[p["id"]] = p["abstract"] or ""
    for row in rows:
        cl = " ".join(claims.get(row["pid"], []))[:1400]
        ab = abstracts.get(row["pid"], "")
        if not abstract_is_trustworthy(ab, row["title"], cl):
            ab = ""
        row["text"] = (ab + "  " + cl).strip()[:1600] or "(no text in the corpus)"
    return rows


def screen(rows, brief, on_progress=None):
    """{pub: 0-100} over every candidate row, in batches. Fail-soft: an unscored candidate simply
    has no screen score and falls back to its retrieval rank."""
    if not rows:
        return {}
    batches = [rows[i:i + SCREEN_BATCH] for i in range(0, len(rows), SCREEN_BATCH)]
    done = [0]
    lock = threading.Lock()

    def one(batch):
        body = "\n".join(f"[{j + 1}] {c['title']}\n  {c['text']}" for j, c in enumerate(batch))
        out = llm.chat_json(_SCREEN_SYS,
                            f"TARGET INVENTION:\n{brief[:6000]}\n\nCANDIDATES:\n{body}",
                            max_tokens=1600) or {}
        got = {}
        for x in (out.get("results") or []):
            if not isinstance(x, dict):
                continue
            try:
                j = int(x["id"]) - 1
                sc = int(x.get("score") if x.get("score") is not None else -1)
            except (TypeError, ValueError, KeyError):
                continue
            if 0 <= j < len(batch) and 0 <= sc <= 100:
                got[batch[j]["pub"]] = sc
        with lock:
            done[0] += 1
            if on_progress:
                try:
                    on_progress(done[0], len(batches))
                except Exception:
                    pass
        return got

    scores = {}
    with ThreadPoolExecutor(max_workers=min(SCREEN_WORKERS, len(batches))) as ex:
        for g in ex.map(one, batches):
            scores.update(g)
    return scores


def _grounded_rows(ref, kind):
    """The rows of one charted reference that carry real, located, verbatim evidence."""
    return [r for r in (ref.get(kind) or [])
            if r.get("grounding") == "verified" and r.get("verdict") in _W
            and _W[r["verdict"]] > 0]


def rarity(charts, features, claims):
    """log(N/df) per feature and per claim, computed over the references actually charted.

    Rarity is measured, not declared: it says how unusual this disclosure is AMONG THE ART THIS
    SEARCH FOUND, which is exactly the question a novelty argument turns on.
    """
    readable = [c for c in charts if c.get("method") == "llm"] or charts
    n = max(1, len(readable))
    fdf = {f: 0 for f in features}
    cdf = {c: 0 for c in claims}
    for ref in readable:
        for r in _grounded_rows(ref, "features"):
            if r["item"] in fdf:
                fdf[r["item"]] += 1
        for r in _grounded_rows(ref, "claims"):
            if r["item"] in cdf:
                cdf[r["item"]] += 1
    fidf = {f: math.log(n / max(1, d)) + 0.15 for f, d in fdf.items()}
    cidf = {c: math.log(n / max(1, d)) + 0.15 for c, d in cdf.items()}
    return {"n": n, "feature_df": fdf, "claim_df": cdf,
            "feature_idf": fidf, "claim_idf": cidf}


def leaders(charts, rar, depth=LEAD_DEPTH):
    """{pub: [(feature, idf)]} — the RARE features each reference is among the best disclosures of.

    "Rare" is measured, not declared: a feature whose idf is at or above the median across the
    features of this search. Only `depth` references per feature qualify, so this promotes the
    best disclosure of a distinctive teaching rather than everything that mentions it.
    """
    idfs = sorted(rar["feature_idf"].values())
    if not idfs:
        return {}
    median = idfs[len(idfs) // 2]
    out = {}
    for row in by_feature(charts, rar, top=depth):
        #  Compare the UNROUNDED idf: by_feature rounds for display, and the rarest feature in a
        #  small set can be the median, so `round(x, 3) < x` silently dropped it.
        if rar["feature_idf"].get(row["feature"], 0.0) < median:
            continue
        for r in row["references"][:depth]:
            if r["verdict"] == "uncertain":
                continue
            out.setdefault(r["pub"], []).append((row["feature"], row["idf"]))
    return out


def score_reference(ref, rar, lead=()):
    """(0-100 score, detail) for ONE charted reference.

    The score is the share of the invention's DISTINCTIVE MASS this reference covers: the sum of
    the rarity weights it grounded, over the sum of all rarity weights available. That makes it
    interpretable on its own ("covers 78% of what makes this invention distinctive") instead of
    being a rank in disguise, and it does not move when an unrelated reference is added.
    """
    fidf, cidf = rar["feature_idf"], rar["claim_idf"]
    total = sum(fidf.values()) + _CLAIM_WEIGHT * sum(cidf.values())
    got = 0.0
    lead = list(lead or [])
    n_disc = n_part = n_unc = 0
    covered = []
    for r in _grounded_rows(ref, "features"):
        w = _W[r["verdict"]]
        got += w * fidf.get(r["item"], 0.0)
        n_disc += r["verdict"] == "disclosed"
        n_part += r["verdict"] == "partial"
        n_unc += r["verdict"] == "uncertain"
        covered.append({"item": r["item"], "verdict": r["verdict"],
                        "idf": round(fidf.get(r["item"], 0.0), 3),
                        "location": r.get("location") or "", "quote": r.get("quote") or ""})
    for r in _grounded_rows(ref, "claims"):
        got += _CLAIM_WEIGHT * _W[r["verdict"]] * cidf.get(r["item"], 0.0)
    got += LEAD_WEIGHT * sum(idf for _f, idf in lead)
    covered.sort(key=lambda c: -c["idf"])
    pct = 0.0 if total <= 0 else max(0.0, min(1.0, got / total))
    coverage = 100.0 * pct
    #  Blend in the reader's holistic verdict, but ONLY for a reference that was actually read AND
    #  grounded at least one quote. An ungrounded or text-less document scores on coverage alone,
    #  which is 0 — that is the gate that keeps a title-only record out of the head.
    overall = (ref.get("overall") or {}).get("score")
    if overall is not None and ref.get("method") == "llm" and covered:
        score = (1.0 - OVERALL_WEIGHT) * coverage + OVERALL_WEIGHT * float(overall)
    else:
        score = coverage
        overall = None
    return int(round(max(0.0, min(100.0, score)))), {
        "coverage": int(round(coverage)), "overall": overall,
        "n_disclosed": n_disc, "n_partial": n_part, "n_uncertain": n_unc,
        "n_features": len(rar["feature_df"]), "covered": covered,
        "leads": [f for f, _idf in lead],
        "read_in_full": ref.get("method") == "llm",
        "chars_read": int(ref.get("chars") or 0),
    }


def _why(ref, detail):
    """A one-sentence opinion assembled from the EVIDENCE, not written by a model.

    The old per-card opinion was a model sentence about a 900-character snippet, and it is what
    told the searcher that a reference disclosing 10 of 12 features "does not disclose the
    specific loop-shaped seal, the continuous air extraction, or the unique bracing structure".
    This one can only say what a grounded, located, refuter-survived quote supports.
    """
    if not detail["read_in_full"]:
        return "Not read in full: this reference has no text in the corpus, so it is ranked on " \
               "its bibliographic record only."
    leads = detail.get("leads") or []
    lead_note = (" Best disclosure found for: " + "; ".join(leads[:2]) + ".") if leads else ""
    n = detail["n_disclosed"] + detail["n_partial"] + detail["n_uncertain"]
    if not n:
        return "Read in full: no feature of the invention could be grounded in a verbatim " \
               "passage of this reference."
    top = [c for c in detail["covered"] if c["verdict"] == "disclosed"][:2] or detail["covered"][:2]
    named = "; ".join(f"{c['item']} ({c['location']})".strip() for c in top if c.get("item"))
    ov = detail.get("overall")
    ov_note = f" Read as a whole it scores {ov}/100." if ov is not None else ""
    head = (f"Read in full ({detail['chars_read']:,} characters). Grounds "
            f"{detail['n_disclosed']} disclosed, {detail['n_partial']} partial and "
            f"{detail['n_uncertain']} unconfirmed of {detail['n_features']} features")
    return (f"{head}, the rarest being {named}.{ov_note}{lead_note}" if named
            else f"{head}.{ov_note}{lead_note}")


def by_feature(charts, rar, top=6):
    """The best reference for EACH feature, rarest feature first.

    A reference is not only a novelty candidate. DE-3724659-A1 (1989) discloses 2 to 6 of 12
    features and would never lead a novelty list, but it is the best single disclosure of "a
    spacer that limits the compression of the sealing lip", which is the characterising feature of
    the patent that was uploaded. That is an inventive-step reference, and the old report had no
    place to put it.
    """
    out = []
    for feat, idf in sorted(rar["feature_idf"].items(), key=lambda kv: -kv[1]):
        hits = []
        for ref in charts:
            for r in _grounded_rows(ref, "features"):
                if r["item"] != feat:
                    continue
                hits.append({"pub": ref["pub"], "title": ref.get("title") or "",
                             "verdict": r["verdict"], "location": r.get("location") or "",
                             "quote": r.get("quote") or "",
                             "confidence": float(r.get("confidence") or 0.0)})
        hits.sort(key=lambda h: ({"disclosed": 0, "partial": 1, "uncertain": 2}.get(h["verdict"], 3),
                                 -h["confidence"]))
        out.append({"feature": feat, "idf": round(idf, 3),
                    "df": rar["feature_df"].get(feat, 0), "references": hits[:top]})
    return out


def run(report, reports_dir=None, slug=None, on_progress=None):
    """Screen wide, read deep, and return the authoritative ranking. Mutates `report`.

    Writes the charts in ``deep_analysis``'s own schema so the "What it discloses" tab and
    ``/analysis/<slug>`` render from THIS reading instead of starting a second, separate one.
    """
    started = time.time()
    features = [str(e).strip() for e in (report.get("elements") or []) if str(e).strip()]
    features = features[:deep_analysis.MAX_FEATURES]
    qd = report.get("query_document") or {}
    claim_items = []
    for c in (qd.get("claims") or [])[:deep_analysis.MAX_INPUT_CLAIMS]:
        text = str(c.get("text") or "").strip()
        if text:
            claim_items.append({"label": f"claim {c.get('claim_no') or len(claim_items) + 1}",
                                "claim_no": c.get("claim_no") or len(claim_items) + 1,
                                "text": text})
    ranked = list(report.get("ranked_families") or [])
    if not ranked or not (features or claim_items):
        return None

    def emit(stage, **kw):
        if on_progress:
            try:
                on_progress(stage, kw)
            except Exception:
                pass

    import query_set
    import webview
    brief = query_set.retrieval_text(report.get("query") or "")
    subject_pub = deep_analysis._norm_pub(qd.get("publication_number"))

    conn = db.connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        reps = webview.resolve_family_reps(cur, ranked[:SCREEN_TOP])
        rows = _candidate_rows(cur, ranked[:SCREEN_TOP], reps, SCREEN_TOP)
    finally:
        conn.close()
    rows = [r for r in rows if not deep_analysis._same_pub(subject_pub, r["pub"])]
    if not rows:
        return None
    emit("screen_start", n=len(rows), batches=(len(rows) + SCREEN_BATCH - 1) // SCREEN_BATCH)
    t0 = time.time()
    scores = screen(rows, brief,
                    on_progress=lambda d, t: emit("screen_progress", done=d, total=t))
    screen_seconds = time.time() - t0

    #  Choose what to read: the head of the screen, plus the head of the RETRIEVAL order no matter
    #  what the screen said (see ALWAYS_CHART_RETRIEVAL_HEAD).
    by_screen = sorted(rows, key=lambda r: (-(scores.get(r["pub"], -1)), r["rank"]))
    chosen, seen = [], set()
    for r in by_screen[:CHART_TOP]:
        chosen.append(r)
        seen.add(r["pub"])
    for r in rows[:ALWAYS_CHART_RETRIEVAL_HEAD]:
        if r["pub"] not in seen:
            chosen.append(r)
            seen.add(r["pub"])
    emit("chart_start", n=len(chosen))

    done = [0]
    lock = threading.Lock()

    def one(row):
        try:
            ref = deep_analysis.analyse_reference(row["pub"], features, claim_items, row["title"])
        except Exception as exc:
            ref = {"pub": row["pub"], "title": row["title"], "found": False, "features": [],
                   "claims": [], "method": "error", "error": str(exc)[:200], "chars": 0}
        ref["screen"] = scores.get(row["pub"])
        ref["retrieval_rank"] = row["rank"]
        ref["family"] = row["fam"]
        with lock:
            done[0] += 1
            if done[0] % 5 == 0 or done[0] == len(chosen):
                emit("chart_progress", done=done[0], total=len(chosen), pub=row["pub"])
        return ref

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(CHART_WORKERS, max(1, len(chosen)))) as ex:
        charts = list(ex.map(one, chosen))
    chart_seconds = time.time() - t0

    rar = rarity(charts, features, [c["label"] for c in claim_items])
    lead_map = leaders(charts, rar)
    by_pub, order = {}, []
    for ref in charts:
        sc, detail = score_reference(ref, rar, lead=lead_map.get(ref["pub"], ()))
        by_pub[ref["pub"]] = {
            "score": sc, "screen": ref.get("screen"), "family": ref.get("family"),
            "retrieval_rank": ref.get("retrieval_rank"), "why": _why(ref, detail),
            **{k: v for k, v in detail.items() if k != "covered"},
            "covered": detail["covered"][:8],
        }
        order.append(ref["pub"])
    #  Ordering: evidence first, then the cheap screen, then the retrieval's own opinion. Every
    #  key is deterministic, so the same report always renders in the same order.
    order.sort(key=lambda p: (-by_pub[p]["score"],
                              -(by_pub[p]["screen"] if by_pub[p]["screen"] is not None else -1),
                              by_pub[p]["retrieval_rank"] or 10**6))

    #  Candidates that were screened but not read keep their screen score, capped, so they stay
    #  visible without ever outranking a reference whose full text was quoted.
    unread = []
    for r in rows:
        if r["pub"] in by_pub:
            continue
        s = scores.get(r["pub"])
        if s is None:
            continue
        unread.append((r, min(int(s), UNREAD_SCORE_CAP)))
    unread.sort(key=lambda t: (-t[1], t[0]["rank"]))

    #  Reorder the ranked families: charted first (by evidence), then screened-but-unread (by the
    #  capped screen score), then everything the screen never reached, in retrieval order.
    fam_order, seenfam = [], set()
    for p in order:
        f = by_pub[p].get("family")
        if f and f not in seenfam:
            seenfam.add(f)
            fam_order.append(f)
    for r, _s in unread:
        if r["fam"] not in seenfam:
            seenfam.add(r["fam"])
            fam_order.append(r["fam"])
    for f in ranked:
        if f not in seenfam:
            seenfam.add(f)
            fam_order.append(f)
    report["ranked_families"] = fam_order

    result = {
        "version": VERSION,
        "order": order,
        "by_pub": by_pub,
        "unread": {r["pub"]: s for r, s in unread},
        "screened": len(scores),
        "candidates": len(rows),
        "charted": len(charts),
        "read_in_full": sum(1 for c in charts if c.get("method") == "llm"),
        "no_text": sum(1 for c in charts if c.get("method") == "no-text"),
        "chars_read": sum(int(c.get("chars") or 0) for c in charts),
        "feature_df": rar["feature_df"],
        "feature_idf": {k: round(v, 3) for k, v in rar["feature_idf"].items()},
        "by_feature": by_feature(charts, rar),
        "screen_seconds": round(screen_seconds, 1),
        "chart_seconds": round(chart_seconds, 1),
        "seconds": round(time.time() - started, 1),
    }
    report["deep_rank"] = result

    #  Hand the reading straight to deep_analysis's cache: it is the same schema, the same
    #  grounding and the same refutation, so re-reading these references would be pure waste.
    if reports_dir and slug:
        try:
            _publish_deep_analysis(reports_dir, slug, report, charts, order, features, claim_items,
                                   qd, subject_pub)
        except Exception:
            pass
    return result


def _publish_deep_analysis(reports_dir, slug, report, charts, order, features, claim_items, qd,
                           subject_pub):
    rank_of = {p: i + 1 for i, p in enumerate(order)}
    refs = sorted(charts, key=lambda r: rank_of.get(r["pub"], 10 ** 6))
    for r in refs:
        r["rank"] = rank_of.get(r["pub"])
    totals = {v: 0 for v in deep_analysis.VERDICTS}
    for r in refs:
        for row in (r.get("features") or []) + (r.get("claims") or []):
            v = row.get("verdict", "absent")
            totals[v] = totals.get(v, 0) + 1
    uncovered = []
    for i, f in enumerate(features):
        hit = any(len(r.get("features") or []) > i
                  and r["features"][i].get("verdict") in ("disclosed", "partial") for r in refs)
        if not hit:
            uncovered.append(f)
    data = {
        "version": deep_analysis.VERSION, "status": "done", "available": True,
        "features": features, "claims": claim_items,
        "subject_label": qd.get("label") or "", "has_subject_claims": bool(claim_items),
        "references": refs, "n_references": len(refs),
        "n_analysed": sum(1 for r in refs if r.get("method") == "llm"),
        "n_no_text": sum(1 for r in refs if r.get("method") == "no-text"),
        "chars_read": sum(int(r.get("chars") or 0) for r in refs),
        "refuted": sum(int(r.get("refuted") or 0) for r in refs),
        "uncovered_features": uncovered, "subject_pub_excluded": subject_pub or "",
        "counts": totals, "seconds": (report.get("deep_rank") or {}).get("chart_seconds"),
        "source": "deep_rank",
    }
    deep_analysis._write_atomic(deep_analysis._path(reports_dir, slug), data)


def card_fields(report, pub):
    """What a rendered card should show for `pub`, or None when this search has no deep rank."""
    dr = (report or {}).get("deep_rank") or {}
    if not dr:
        return None
    hit = (dr.get("by_pub") or {}).get(pub)
    if hit:
        return {"deep_score": hit["score"], "deep_why": hit["why"],
                "deep_leads": hit.get("leads") or [],
                "deep_read": bool(hit.get("read_in_full")),
                "deep_disclosed": hit.get("n_disclosed", 0),
                "deep_partial": hit.get("n_partial", 0),
                "deep_features": hit.get("n_features", 0),
                "deep_covered": hit.get("covered") or [],
                "deep_chars": hit.get("chars_read", 0),
                "deep_screen": hit.get("screen")}
    s = (dr.get("unread") or {}).get(pub)
    if s is None:
        return None
    #  Capped HERE as well as where the list is built. The invariant that matters is at the point
    #  of use: whatever is in the report, a card the reading never reached must not be able to
    #  outrank one whose full text was quoted.
    return {"deep_score": min(int(s), UNREAD_SCORE_CAP), "deep_read": False,
            "deep_screen": int(s),
            "deep_disclosed": 0, "deep_partial": 0,
            "deep_features": len(dr.get("feature_df") or {}), "deep_covered": [],
            "deep_chars": 0,
            "deep_why": "Screened on its abstract and claims but not read in full, so it is "
                        "ranked below every reference whose full text was quoted."}
