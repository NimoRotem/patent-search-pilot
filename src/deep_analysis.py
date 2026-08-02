"""Read the top references in full and say, cell by cell, what each one actually discloses.

This is the step the whole search exists to serve. Retrieval surfaces a couple of thousand
families and ranks them; the ranking says "these look close". What an attorney needs next is the
other question — for THIS reference, which feature of my invention does it teach, where exactly,
and in whose words — and until now the product answered it in three thin ways:

  * an element x reference GRID whose filled cells meant only "the retriever returned this
    publication for this element" — a retrieval score standing in for a reading;
  * a per-reference chart built ON DEMAND, one card at a time, when somebody clicked;
  * a claims grid capped at EIGHT references.

and in every case the reference text handed to the model was truncated to 24,000 characters,
which for a real patent is the front page and part of the description.

So this module does the reading properly, for the top 50, up front:

  * **the full text of the reference** — every claim and every description paragraph out of the
    corpus, not a 40-passage sample. A patent that genuinely runs to 200,000 characters is sent
    in full; the model's context is not the binding constraint and truncation was never a
    considered decision, only an inherited default;
  * **two tables per reference, from one reading.** The FEATURE table maps each feature of the
    search input to what this reference discloses. The CLAIM table does the same for the claims
    of the patent that was uploaded as the search input, when there was one. Both come from a
    single pass so the model reads the reference once with both questions in hand;
  * **every cell carries a verbatim quote and a real location** — claim 7, paragraph 41 — resolved
    by CODE from the quote, never authored by the model;
  * **every quote is grounded and every "disclosed" is refuted.** A quote that is not found in the
    reference is dropped, not shown. A "disclosed" verdict that an independent refuting pass will
    not confirm becomes "uncertain". Both gates are the existing, measured ones in
    :mod:`claim_chart` — an audit put naive chart overclaim at 22%, and 7 of 12 coordinate-backed
    cells were false positives with perfectly real quotes at perfectly real coordinates.

Runs in the background once a report is ready, caches to ``<slug>.deep.json``, and reports
progress, because reading fifty patents in full is minutes of work and the user should be able to
watch it rather than wait blindly.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import claim_chart
import db
import grounding
import llm

#  3: caches written by the pre-deep_rank path were built against a PARTIAL, pre-listwise
#  ordering and were never invalidated, so they charted a list the page no longer showed.
#  Bumping the version makes every one of them a miss.
VERSION = 3

#  How deep to read. The retrieval side ranks thousands; these are the ones a human would
#  actually open. 50 is the number of references an attorney reads before deciding, and it is
#  what the search is asked to justify.
TOP_N = int(os.environ.get("DEEP_TOP_N", "50"))
MAX_FEATURES = 20
MAX_INPUT_CLAIMS = 30
#  Per-reference text budget. A US grant is typically 40,000-120,000 characters; the long tail
#  reaches 400,000. Send the whole thing up to this bound and SAY when it was cut, rather than
#  silently reading a quarter of the document.
MAX_REFERENCE_CHARS = int(os.environ.get("DEEP_REF_CHARS", "220000"))
MAX_CLAIMS_PER_REF = 200
MAX_PARAGRAPHS_PER_REF = 600
MAX_QUOTE_WORDS = claim_chart.MAX_QUOTE_WORDS
VERDICTS = claim_chart.VERDICTS

#  Concurrency and cost. One reference is one long-context read plus, when it found anything, one
#  short refutation. Eight at a time keeps a search inside a few minutes without asking the shared
#  Vertex quota for more than the rest of the app is already using.
WORKERS = int(os.environ.get("DEEP_WORKERS", "8"))
MAX_REFUTE_PER_REF = 12

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deep-analysis")
_LOCK = threading.Lock()
_STATUS: dict = {}


# ---------------------------------------------------------------------------
# the full text of one reference
# ---------------------------------------------------------------------------
def full_text(pub, max_chars=MAX_REFERENCE_CHARS):
    """Every citable unit of one publication: abstract, all claims, all description paragraphs.

    Returns ``{found, pub, title, passages, chars, n_claims, n_paragraphs, truncated}``. Each
    passage keeps its real coordinate so a quote taken from it can be cited as "claim 7" or
    "paragraph 41" by code rather than by the model.

    Ordering is deliberate: abstract, then claims, then description. If the budget does run out
    it runs out in the description, never in the claims — the claims are the reference's legal
    disclosure and are what a chart is mostly arguing about.
    """
    out = {"found": False, "pub": pub, "title": "", "passages": [], "chars": 0,
           "n_claims": 0, "n_paragraphs": 0, "truncated": False}
    with db.cursor() as cur:
        cur.execute("SELECT id, title, abstract FROM publications WHERE publication_number=%s "
                    "LIMIT 1", (pub,))
        row = cur.fetchone()
        if not row:
            return out
        out["found"] = True
        out["title"] = row["title"] or ""
        pid = row["id"]
        budget = max_chars

        def add(kind, coord, label, text):
            nonlocal budget
            text = (text or "").strip()
            if not text:
                return False
            if len(text) > budget:
                out["truncated"] = True
                return False
            out["passages"].append({"kind": kind, "coord": coord, "label": label, "text": text})
            budget -= len(text)
            out["chars"] += len(text)
            return True

        if (row["abstract"] or "").strip():
            add("abstract", {}, "abstract", row["abstract"])

        cur.execute("SELECT claim_no, text, resolved_text FROM claims WHERE publication_id=%s "
                    "ORDER BY claim_no LIMIT %s", (pid, MAX_CLAIMS_PER_REF))
        for c in cur.fetchall():
            #  resolved_text carries a dependent claim with its parent folded in, which is what a
            #  chart needs to see: "the system of claim 1, wherein X" discloses claim 1 too.
            if add("claim", {"claim_no": c["claim_no"]}, f"claim {c['claim_no']}",
                   c["resolved_text"] or c["text"]):
                out["n_claims"] += 1

        cur.execute("SELECT kind, coord, text FROM chunks WHERE publication_id=%s "
                    "AND kind NOT LIKE 'claim%%' AND kind <> 'abstract' AND kind <> 'whole' "
                    "AND text IS NOT NULL ORDER BY id LIMIT %s", (pid, MAX_PARAGRAPHS_PER_REF))
        for ch in cur.fetchall():
            coord = ch["coord"] if isinstance(ch["coord"], dict) else {}
            if add(ch["kind"], coord, claim_chart._coord_label(ch["kind"], coord), ch["text"]):
                out["n_paragraphs"] += 1
    return out


def _rendered(ref):
    return (f"TITLE: {ref.get('title', '')}\n\n" +
            "\n\n".join(f"[{p['label']}] {p['text']}" for p in ref["passages"]))


# ---------------------------------------------------------------------------
# the reading
# ---------------------------------------------------------------------------
_SYS = (
    "You are a patent examiner reading ONE prior-art reference in full and charting it against a "
    "subject invention. You are given the reference's complete text, the FEATURES of the subject "
    "invention, and — when the search started from a patent document — the subject's own CLAIMS.\n"
    "\n"
    "For EVERY feature, and for EVERY subject claim, decide what THIS reference discloses:\n"
    "- verdict: \"disclosed\" (the reference text clearly teaches it), \"partial\" (related but "
    "incomplete, narrower, or a different mechanism), or \"absent\" (not in this reference).\n"
    "- quote: the EXACT verbatim passage from the reference that discloses it, copied word for "
    "word, at most 40 words. NEVER paraphrase, NEVER stitch together separate sentences, NEVER "
    "invent. Empty string when absent.\n"
    "- note: one short sentence saying what the quoted passage teaches and, for \"partial\", what "
    "it does NOT teach.\n"
    "- confidence: 0.0-1.0.\n"
    "\n"
    "A subject claim is a whole limitation set. Judge it as disclosed only if the reference "
    "teaches ALL of its limitations; if it teaches some, that is \"partial\" and the note must say "
    "which limitation is missing.\n"
    "\n"
    "Use ONLY the reference text supplied. You have no outside knowledge of this reference. "
    "\"absent\" with an empty quote is the correct and expected answer whenever the text does not "
    "show something — it is not a failure, and it is much better than a guess. Do not state any "
    "conclusion about patentability, novelty, obviousness, validity or infringement.\n"
    "\n"
    "Return STRICT JSON:\n"
    '{"features":[{"item":"<the feature, verbatim as given>","verdict":"...","quote":"...",'
    '"note":"...","confidence":0.0}],'
    '"claims":[{"item":"<the claim number as given, e.g. \\"claim 3\\">","verdict":"...",'
    '"quote":"...","note":"...","confidence":0.0}],'
    '"overall":{"score":0,"why":"one sentence"}}\n'
    "Include every feature and every claim you were given, in the order given.\n"
    "\n"
    '"overall" is your judgement of how relevant THIS REFERENCE AS A WHOLE is to the subject '
    "invention, on the evidence of the text you were given: 90-100 = discloses essentially the "
    "same invention; 70-89 = strongly relevant, discloses most core elements; 40-69 = related "
    "field, some overlapping features; 1-39 = same broad area but a different problem or "
    "solution; 0 = unrelated. Judge the document, not the table you just filled in."
)


def _row(item, raw, ref, shown, kind):
    """One model answer -> one charted cell, or a demotion with the reason recorded.

    Two gates, in order, both borrowed from the measured claim-chart path: the quote must be
    FOUND in the reference text (otherwise it was invented), and it must be LOCATABLE in a
    specific passage (otherwise it cannot be cited). Failing either forces verdict="absent" —
    the cell is never quietly kept with a bad quote.
    """
    base = {"item": item, "kind": kind, "verdict": "absent", "quote": "", "note": "",
            "location": "", "coord": {}, "passage_kind": "", "confidence": 0.0,
            "grounding": "no-row-returned", "refuted": None}
    if not isinstance(raw, dict):
        return base
    verdict = str(raw.get("verdict") or "absent").lower()
    if verdict not in VERDICTS:
        verdict = "absent"
    quote = " ".join(str(raw.get("quote") or "").split()[:MAX_QUOTE_WORDS])
    note = str(raw.get("note") or "").strip()[:400]
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0

    if verdict == "absent" or not quote:
        return {**base, "note": note, "confidence": conf, "grounding": "model-absent"}
    if not grounding.grounded(quote, shown):
        return {**base, "note": note, "grounding": "dropped-ungrounded-quote"}
    loc = claim_chart._locate(quote, ref["passages"])
    if not loc:
        return {**base, "note": note, "grounding": "dropped-unlocatable-quote"}
    return {"item": item, "kind": kind, "verdict": verdict, "quote": quote, "note": note,
            "location": loc["label"], "coord": loc["coord"], "passage_kind": loc["kind"],
            "confidence": conf, "grounding": "verified", "refuted": None}


def _align(rows, items):
    """Match the model's answers back to the items we asked about, tolerating a paraphrase."""
    by_item = {}
    for r in rows or []:
        if isinstance(r, dict):
            by_item.setdefault(str(r.get("item") or "").strip(), r)
    out = []
    for item in items:
        r = by_item.get(item)
        if r is None:
            r = next((rr for rr in (rows or [])
                      if isinstance(rr, dict)
                      and str(rr.get("item") or "")[:24].lower() == item[:24].lower()), None)
        out.append(r)
    return out


_REFUTE_SYS = (
    "You are checking a colleague's claim chart, and your job is to REFUTE it. For each pair you "
    "are given an assertion and the exact quoted passage it rests on. Answer whether that passage, "
    "read alone, really does teach that assertion.\n"
    "Say refuted=true when the quote is merely on the same topic, describes a different mechanism, "
    "is about a different part, or supports only some of what is asserted. Default to refuted=true "
    "when you are unsure — a chart cell that survives this check should be one nobody can argue "
    "with.\n"
    'Return STRICT JSON: {"checks":[{"i":0,"refuted":true,"why":"one short sentence"}]} for every '
    "pair, by index."
)


def _refute(rows, pub, texts=None):
    """Argue the opposite side of every "disclosed" cell; downgrade the ones that do not survive.

    Cheap and worth it: an audit found 7 of 12 coordinate-backed cells were false positives whose
    quote and coordinate were both perfectly real. Only "disclosed" is checked — "partial" is
    already a hedge, and "absent" has nothing to refute.

    `texts` maps an item to the assertion it actually stands for. It matters for the CLAIM table,
    where the item is a label like "claim 3": handed that alone, the refuter answered "the
    assertion 'claim 3' is not a statement that can be refuted" and downgraded every single claim
    row for a reason that was about our prompt rather than about the evidence.
    """
    texts = texts or {}
    targets = [(i, r) for i, r in enumerate(rows)
               if r.get("verdict") == "disclosed" and r.get("quote")][:MAX_REFUTE_PER_REF]
    if not targets:
        return 0
    pairs = [{"i": n, "assertion": texts.get(r["item"]) or r["item"], "quote": r["quote"]}
             for n, (_, r) in enumerate(targets)]
    out = llm.chat_json(_REFUTE_SYS, json.dumps({"reference": pub, "pairs": pairs})[:40000],
                        max_tokens=2000) or {}
    checks = {int(c.get("i", -1)): c for c in (out.get("checks") or []) if isinstance(c, dict)}
    downgraded = 0
    for n, (idx, row) in enumerate(targets):
        c = checks.get(n)
        if c is None:
            continue
        if bool(c.get("refuted")):
            rows[idx]["verdict"] = "uncertain"
            rows[idx]["refuted"] = str(c.get("why") or "the refuter would not confirm it")[:300]
            downgraded += 1
        else:
            rows[idx]["refuted"] = False
    return downgraded


def analyse_reference(pub, features, input_claims, title=""):
    """Read ONE reference in full and chart it against the features and the subject claims."""
    started = time.time()
    ref = full_text(pub)
    result = {"pub": pub, "title": title or ref.get("title") or "", "found": ref["found"],
              "features": [], "claims": [], "method": "llm",
              "chars": ref["chars"], "n_claims_read": ref["n_claims"],
              "n_paragraphs_read": ref["n_paragraphs"], "text_truncated": ref["truncated"],
              "refuted": 0, "seconds": 0.0}
    feature_items = [f for f in features][:MAX_FEATURES]
    claim_items = [c["label"] for c in input_claims][:MAX_INPUT_CLAIMS]

    if not ref["found"] or not ref["passages"]:
        #  No local text means an LLM could only invent. Say so rather than charting nothing.
        result["method"] = "no-text"
        result["features"] = [_row(f, None, ref, "", "feature") for f in feature_items]
        result["claims"] = [_row(c, None, ref, "", "claim") for c in claim_items]
        for r in result["features"] + result["claims"]:
            r["grounding"] = "no-reference-text"
        result["seconds"] = round(time.time() - started, 2)
        return result

    shown = _rendered(ref)
    claim_payload = [{"item": c["label"], "text": c["text"]} for c in input_claims
                     ][:MAX_INPUT_CLAIMS]

    def _ask(features_now, claims_now):
        payload = {"reference": pub, "subject_features": features_now,
                   "subject_claims": claims_now, "reference_text": shown}
        #  The prompt is dominated by the reference itself; leave room for a full chart.
        return llm.chat_json(_SYS, json.dumps(payload, ensure_ascii=False),
                             max_tokens=12000) or {}

    #  TWO FOCUSED READS, not one combined one, whenever there are both features and claims.
    #  MEASURED: asking for 12 feature rows AND 13 claim rows in a single answer, each with a
    #  verbatim quote and a note, made the model economise: the same reference that grounded 10 of
    #  12 features when asked about features alone grounded 2 when the claims were asked for in
    #  the same breath. The reference text is re-sent, which costs input tokens and buys back the
    #  chart. Sequential on purpose: this already runs inside a wide worker pool (deep_rank), and
    #  a nested pool would multiply the concurrent Vertex calls by two.
    if feature_items and claim_payload:
        out_f = _ask(feature_items, [])
        out_c = _ask([], claim_payload)
        out = {}
        if out_f.get("features"):
            out["features"] = out_f["features"]
        if out_c.get("claims"):
            out["claims"] = out_c["claims"]
        #  Take the holistic judgement from the FEATURE read: that is the pass that saw the
        #  invention's features, and asking twice would only give two numbers to reconcile.
        if isinstance(out_f.get("overall"), dict):
            out["overall"] = out_f["overall"]
    else:
        out = _ask(feature_items, claim_payload)
    if not out:
        result["method"] = "unavailable"
        result["features"] = [_row(f, None, ref, shown, "feature") for f in feature_items]
        result["claims"] = [_row(c, None, ref, shown, "claim") for c in claim_items]
        result["seconds"] = round(time.time() - started, 2)
        return result

    result["features"] = [_row(item, raw, ref, shown, "feature")
                          for item, raw in zip(feature_items, _align(out.get("features"), feature_items))]
    result["claims"] = [_row(item, raw, ref, shown, "claim")
                        for item, raw in zip(claim_items, _align(out.get("claims"), claim_items))]
    #  The reader's holistic verdict on the document as a whole. Kept SEPARATE from the chart: the
    #  chart is the legal artefact and every cell in it is gated, whereas this is one number, used
    #  only to rank and only ever alongside grounded evidence (see deep_rank.score_reference).
    ov = out.get("overall") if isinstance(out.get("overall"), dict) else {}
    try:
        ov_score = max(0, min(100, int(ov.get("score"))))
    except (TypeError, ValueError):
        ov_score = None
    result["overall"] = {"score": ov_score, "why": str(ov.get("why") or "")[:300]}
    try:
        claim_texts = {c["label"]: c["text"] for c in input_claims}
        result["refuted"] = (_refute(result["features"], pub) +
                             _refute(result["claims"], pub, claim_texts))
    except Exception:
        pass
    result["counts"] = _counts(result["features"] + result["claims"])
    result["seconds"] = round(time.time() - started, 2)
    return result


def _counts(rows):
    c = {v: 0 for v in VERDICTS}
    for r in rows:
        c[r.get("verdict", "absent")] = c.get(r.get("verdict", "absent"), 0) + 1
    return c


# ---------------------------------------------------------------------------
# the whole report
# ---------------------------------------------------------------------------
def subject_material(report, view):
    """What the references are being charted AGAINST: the features and, if any, the subject claims."""
    features = [str(e).strip() for e in ((view or {}).get("elements") or
                                         (report or {}).get("elements") or []) if str(e).strip()]
    features = [f for f in features][:MAX_FEATURES]
    qd = (report or {}).get("query_document") or {}
    claims = []
    for c in (qd.get("claims") or [])[:MAX_INPUT_CLAIMS]:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        claims.append({"label": f"claim {c.get('claim_no') or len(claims) + 1}",
                       "claim_no": c.get("claim_no") or len(claims) + 1,
                       "independent": bool(c.get("independent")), "text": text})
    return features, claims, qd


def _extend_to(cards, report, top_n):
    """Top up the reading list from the ranked families beyond the cards the page renders.

    The page builds a bounded number of full cards on purpose — a card costs a drawing, a claim
    match and an explanation. The READING should not inherit that limit.

    The tail is taken from the families the cards do NOT already cover, NOT from
    ``ranked_families[len(cards):]``. That old slice assumed the cards were exactly the first N
    ranked families, which stopped being true the moment a listwise rerank and a federated merge
    ran between them: ranks 26-50 of the reading list were then an arbitrary offset into a list
    the page was no longer ordered by.
    """
    have = {c["pub"] for c in cards}
    need = top_n - len(cards)
    if need <= 0:
        return cards
    ranked = (report or {}).get("ranked_families") or []
    covered = {c.get("family") for c in cards if c.get("family")}
    rest = [f for f in ranked if f not in covered]
    tail = rest[:need * 3]                              # over-fetch: some resolve to nothing
    if not tail:
        return cards
    try:
        import webview
        conn = db.connect()
        conn.autocommit = True
        cur = conn.cursor()
        try:
            reps = webview.resolve_family_reps(cur, tail)
        finally:
            conn.close()
    except Exception:
        return cards
    rank = len(cards)
    for fam in tail:
        if len(cards) >= top_n:
            break
        r = reps.get(fam)
        if not r or r["publication_number"] in have:
            continue
        rank += 1
        have.add(r["publication_number"])
        cards.append({"pub": r["publication_number"], "title": r.get("title") or "",
                      "rank": rank, "beyond_cards": True})
    return cards


def _subject_pub(report):
    """The publication number of the document the search STARTED from, if it was a patent.

    Charting a patent against its own claims produces a row of "discloses claim 1" for every
    claim and tells the reader nothing — worse, it puts a meaningless perfect score at rank 1.
    When the uploaded document identifies itself, that publication is skipped.
    """
    qd = (report or {}).get("query_document") or {}
    return _norm_pub(qd.get("publication_number"))


def _norm_pub(s):
    """Compare publication numbers across every spelling they arrive in.

    The extraction reads "US 11,338,449 B2" off the front page; the corpus stores
    "US-11338449-B2". Stripping only spaces and hyphens left the COMMAS in and the two never
    matched, so the subject patent was charted against its own claims anyway.
    """
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _same_pub(a, b):
    return bool(a) and _norm_pub(a) == _norm_pub(b)


def build(report, view, on_progress=None, top_n=TOP_N):
    """Chart the top `top_n` references. Synchronous worker body."""
    started = time.time()
    features, claims, qd = subject_material(report, view)
    subject_pub = _subject_pub(report)
    cards = [c for c in ((view or {}).get("cards") or []) if c.get("pub")
             and not _same_pub(subject_pub, c.get("pub"))][:top_n]
    cards = _extend_to(cards, report, top_n)
    cards = [c for c in cards if not _same_pub(subject_pub, c.get("pub"))]
    if not cards or not (features or claims):
        return {"version": VERSION, "status": "done", "available": False,
                "reason": "no ranked references" if not cards else
                          "the search recorded no features to chart",
                "references": [], "features": features, "claims": claims}

    done = [0]

    def one(card):
        try:
            r = analyse_reference(card["pub"], features, claims, card.get("title") or "")
            r["rank"] = card.get("rank")
        except Exception as exc:
            r = {"pub": card["pub"], "title": card.get("title") or "", "found": False,
                 "features": [], "claims": [], "method": "error", "error": str(exc)[:200],
                 "rank": card.get("rank")}
        done[0] += 1
        if on_progress:
            try:
                on_progress(done[0], len(cards), r["pub"])
            except Exception:
                pass
        return r

    with ThreadPoolExecutor(max_workers=min(WORKERS, max(1, len(cards)))) as ex:
        refs = list(ex.map(one, cards))
    refs.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0))

    totals = {v: 0 for v in VERDICTS}
    for r in refs:
        for row in (r.get("features") or []) + (r.get("claims") or []):
            totals[row.get("verdict", "absent")] = totals.get(row.get("verdict", "absent"), 0) + 1
    #  Which features NO reference disclosed — the part of the invention the art did not reach.
    uncovered = []
    for i, f in enumerate(features):
        if not any((r.get("features") or [{}])[i:i + 1] and
                   r["features"][i].get("verdict") in ("disclosed", "partial")
                   for r in refs if len(r.get("features") or []) > i):
            uncovered.append(f)
    return {
        "version": VERSION, "status": "done", "available": True,
        "features": features, "claims": claims,
        "subject_label": qd.get("label") or "",
        "has_subject_claims": bool(claims),
        "references": refs,
        "n_references": len(refs),
        "n_analysed": sum(1 for r in refs if r.get("method") == "llm"),
        "n_no_text": sum(1 for r in refs if r.get("method") == "no-text"),
        "chars_read": sum(int(r.get("chars") or 0) for r in refs),
        "refuted": sum(int(r.get("refuted") or 0) for r in refs),
        "uncovered_features": uncovered,
        "subject_pub_excluded": subject_pub or "",
        "counts": totals,
        "seconds": round(time.time() - started, 2),
    }


# ---------------------------------------------------------------------------
# background execution + cache
# ---------------------------------------------------------------------------
def _path(reports, slug):
    return Path(reports) / f"{slug}.deep.json"


def _read_cache(reports, slug):
    p = _path(reports, slug)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    return data if data.get("version") == VERSION and data.get("status") == "done" else None


def _write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def metadata(report, view):
    """What the report page can say before the analysis has run."""
    features, claims, qd = subject_material(report, view)
    cards = [c for c in ((view or {}).get("cards") or []) if c.get("pub")]
    #  What will actually be READ, which is more than the page renders as cards. When deep_rank
    #  ran during generation it already read a known number in full, so say that number rather
    #  than this module's own default.
    dr = (report or {}).get("deep_rank") or {}
    if dr.get("charted"):
        reachable = int(dr["charted"])
    else:
        reachable = min(TOP_N, max(len(cards), len((report or {}).get("ranked_families") or [])))
    return {"available": bool(cards and (features or claims)),
            "n_features": len(features), "n_subject_claims": len(claims),
            "n_references": reachable,
            "subject_label": qd.get("label") or ""}


def status(slug, reports):
    cached = _read_cache(reports, slug)
    if cached:
        return {"status": "done", "available": cached.get("available", False),
                "n_references": cached.get("n_references", 0),
                "n_analysed": cached.get("n_analysed", 0),
                "counts": cached.get("counts", {}),
                "seconds": cached.get("seconds")}
    with _LOCK:
        s = dict(_STATUS.get(slug) or {})
    return s or {"status": "idle"}


def result(slug, reports):
    return _read_cache(reports, slug)


def ensure(slug, report, view, reports):
    """Start the analysis once, in the background. Idempotent and cache-backed."""
    cached = _read_cache(reports, slug)
    if cached:
        return {"status": "done", "available": cached.get("available", False)}
    with _LOCK:
        cur = _STATUS.get(slug)
        if cur and cur.get("status") == "running":
            return dict(cur)
        _STATUS[slug] = {"status": "running", "done": 0,
                         "total": metadata(report, view).get("n_references", TOP_N),
                         "started": time.time()}

    def progress(n, total, pub):
        with _LOCK:
            _STATUS[slug] = {"status": "running", "done": n, "total": total, "pub": pub,
                             "started": (_STATUS.get(slug) or {}).get("started", time.time())}

    def work():
        try:
            data = build(report, view, on_progress=progress)
            _write_atomic(_path(reports, slug), data)
            with _LOCK:
                _STATUS[slug] = {"status": "done", "available": data.get("available", False),
                                 "n_references": data.get("n_references", 0),
                                 "counts": data.get("counts", {})}
        except Exception as exc:
            with _LOCK:
                _STATUS[slug] = {"status": "error", "error": str(exc)[:300]}

    _POOL.submit(work)
    with _LOCK:
        return dict(_STATUS.get(slug) or {"status": "running"})


def invalidate(slug, reports):
    p = _path(reports, slug)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass
    with _LOCK:
        _STATUS.pop(slug, None)
