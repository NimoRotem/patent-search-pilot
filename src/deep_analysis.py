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
#  HOW MANY FEATURES ARE CHARTED. This was 20 and it was a SILENT TRUNCATION, not a cap.
#  ``deep_rank`` builds its checklist from ``disclosures`` (40+ items) and hands the whole list
#  here; ``analyse_reference`` then charted the first twenty and returned nothing for the rest,
#  while ``deep_rank.rarity`` still counted all of them in the denominator. Measured on
#  adhoc-220e6c97a41e: 40 features declared, 20 charted, and EXACTLY the last 20 had df=0 — the
#  bracing structure, the channel geometry and the handle, which is the whole characterising half
#  of that invention, were reported as "nothing read in full discloses this" having never been
#  asked about, and every reference's score was divided by a denominator half of which was
#  unreachable.
MAX_FEATURES = int(os.environ.get("DEEP_MAX_FEATURES", "60"))
#  Features per model call. NOT a truncation: the list is charted in consecutive batches and the
#  answers are concatenated. It exists because the model ECONOMISES when a single answer has to
#  carry too many quoted rows — the same effect already measured for features-plus-claims in one
#  breath (10 of 12 grounded when asked alone, 2 of 12 when asked together). Two batches of 24
#  costs one extra pass over the reference text and buys back the other half of the chart.
FEATURE_BATCH = int(os.environ.get("DEEP_FEATURE_BATCH", "24"))
#  Raised for Type B: the reader charts LIMITATIONS, and eleven claims become sixty-odd of them.
MAX_INPUT_CLAIMS = int(os.environ.get("DEEP_MAX_INPUT_CLAIMS", "80"))
#  Claims (or limitations) per model call. See the batching comment in analyse_reference.
CLAIM_BATCH = int(os.environ.get("DEEP_CLAIM_BATCH", "12"))
#  How many of a single reference's batches may be in flight at once. A 68-limitation
#  subject costs 9 batches plus the refuter; issuing them one at a time was measured to hold
#  the whole pipeline at 2.70 calls/s against the 5.97 one provider alone sustains. The real
#  ceiling is the per-provider semaphore in model_pool; this only stops one enormous document
#  from occupying all of it.
BATCH_WORKERS = int(os.environ.get("DEEP_BATCH_WORKERS", "6"))
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
#  Raised with MAX_FEATURES: a checklist of 48 disclosures produces more than twelve "disclosed"
#  cells on a strong reference, and the twelve that happened to be first were the only ones any
#  refuter ever saw. An unrefuted "disclosed" is the cell an audit found to be a false positive
#  7 times in 12, so the gate has to cover the whole chart, not its head.
MAX_REFUTE_PER_REF = int(os.environ.get("DEEP_MAX_REFUTE", "24"))

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
    "MATCH ON MEANING, NOT ON WORDING. This is the single most common way this task is done "
    "badly. Each feature is phrased in the SUBJECT's vocabulary and reference frame. The reference "
    "in front of you was written by different people, often decades earlier, frequently translated "
    "from another language, and usually for a different application, so it will almost never use "
    "the same nouns. Decide whether it teaches the same TECHNICAL IDEA:\n"
    "  - the same part under another name — seal / gasket / sealing lip / sealing ring / "
    "diaphragm / Dichtung; base / body / plate / housing / frame / carrier; air extraction means / "
    "vacuum pump / blower / fan / impeller / ejector / venturi / aspirator;\n"
    "  - the same structure described from another reference frame: \"protrudes from the second "
    "side of the base element\" and \"stands proud of the underside of the plate\" and \"a rib on "
    "the lower face\" are the same teaching;\n"
    "  - the same function achieved by an equivalent structure;\n"
    "  - a SPECIES of a genus the feature states: a closed-cell foam seal discloses \"a seal "
    "elastically deformable at its contact surface\"; a hand grip discloses \"a handle\";\n"
    "  - a teaching carried by a drawing description or a dimension rather than by a sentence "
    "that restates the feature.\n"
    "A reference that plainly teaches the idea in its own words is \"disclosed\". Reserve "
    "\"absent\" for a reference that does not teach the idea AT ALL — never for one that simply "
    "words it differently, and never because you could not find a sentence that repeats the "
    "feature's phrasing. The quote you return is still the reference's own words, verbatim.\n"
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
    #  STRONG TIER. This is the gate that decides what the report may assert: a cell it downgrades
    #  never renders as coverage. It is also cheap in volume — one call per reference against the
    #  eleven the reading costs — so it is exactly where a better model is worth paying for.
    out = llm.chat_json(_REFUTE_SYS, json.dumps({"reference": pub, "pairs": pairs})[:40000],
                        tier="strong",
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


def analyse_reference(pub, features, input_claims, title="", hints=None):
    """Read ONE reference in full and chart it against the features and the subject claims.

    `hints` is the optional ``{feature: "other words for the same idea"}`` map from
    :func:`concept_expansions`. It is sent alongside the checklist so the reader knows what the
    subject's vocabulary looks like in OTHER people's patents before it decides "absent".
    """
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
    hints = hints or {}

    def _ask(features_now, claims_now):
        payload = {"reference": pub, "subject_features": features_now,
                   "subject_claims": claims_now, "reference_text": shown}
        #  What the subject's own vocabulary looks like in other people's patents. Sent only for
        #  the features actually being asked about, so the prompt does not carry the whole map.
        vocab = {f: hints[f] for f in features_now if hints.get(f)}
        if vocab:
            payload["other_words_for_each_feature"] = vocab
        #  The prompt is dominated by the reference itself; leave room for a full chart.
        return llm.chat_json(_SYS, json.dumps(payload, ensure_ascii=False),
                             max_tokens=12000) or {}

    #  FOCUSED READS, not one combined one. MEASURED: asking for 12 feature rows AND 13 claim rows
    #  in a single answer, each with a verbatim quote and a note, made the model economise: the
    #  same reference that grounded 10 of 12 features when asked about features alone grounded 2
    #  when the claims were asked for in the same breath. So the claims get their own pass, and
    #  the features are asked for in batches of FEATURE_BATCH for the same reason — a 48-row
    #  answer is subject to exactly the same economising as a 25-row one.
    #
    #  The reference text is re-sent per pass, which costs input tokens and buys back the chart.
    #
    #  THE BATCHES RUN CONCURRENTLY. They used to be sequential, on the reasoning that this already
    #  runs inside a wide worker pool and a nested pool would multiply the concurrent calls. That
    #  reasoning was never measured and it was the ceiling the whole pipeline ran under: profiled
    #  on real documents, a reference costs ELEVEN calls and they were issued one after another, so
    #  a single reference took 97 seconds of which almost all was waiting. At CHART_WORKERS=24 the
    #  pipeline achieved 2.70 calls/s while ONE provider sustains 5.97 and a second sustains 7.96
    #  at the same concurrency and the same 60k-character prompts, with no throttling from either.
    #
    #  Concurrency is now bounded where it should be — by the per-provider semaphores in
    #  model_pool — rather than by refusing to ask two questions at once. `BATCH_WORKERS` caps the
    #  fan-out per reference so a single 68-limitation document cannot occupy the whole pool.
    out = {"features": [], "claims": [], "overall": None}
    feature_batches = [feature_items[i:i + FEATURE_BATCH]
                       for i in range(0, len(feature_items), FEATURE_BATCH)]
    #  BATCHED, for a measured reason. A Type B search charts LIMITATIONS rather than whole claims
    #  — sixty rows where there used to be eleven — and a single answer carrying sixty quoted,
    #  located rows is where a reader starts economising. Measured on the feature side: the same
    #  reference grounded 10 of 12 asked alone and 2 of 12 asked together. Widening the batch to
    #  cut calls would buy speed with the evidence, which is the wrong trade.
    claim_batches = [claim_payload[i:i + CLAIM_BATCH]
                     for i in range(0, len(claim_payload), CLAIM_BATCH)]
    jobs = ([("features", b, []) for b in feature_batches]
            + [("claims", [], b) for b in claim_batches])

    def _run(job):
        kind, fb, cb = job
        return kind, _ask(fb, cb)

    if len(jobs) > 1 and BATCH_WORKERS > 1:
        with ThreadPoolExecutor(max_workers=min(BATCH_WORKERS, len(jobs))) as ex:
            results = list(ex.map(_run, jobs))
    else:
        results = [_run(j) for j in jobs]
    #  Reassembled in the ORIGINAL batch order, not completion order: `_align` zips these back
    #  against the item list positionally, so a chart whose rows arrived out of order would put
    #  every verdict against the wrong requirement.
    for (kind, got), _job in zip(results, jobs):
        out[kind].extend(got.get(kind) or [])
        #  The holistic judgement comes from the FIRST feature pass — that pass saw the head of the
        #  checklist — and averaging several would only give numbers to reconcile.
        if out["overall"] is None and isinstance(got.get("overall"), dict):
            out["overall"] = got["overall"]
    if out["overall"] is None:
        out.pop("overall")
    if not (out.get("features") or out.get("claims")):
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
# the concept layer: what each feature looks like in OTHER people's words
# ---------------------------------------------------------------------------
_CONCEPT_SYS = (
    "You are a patent examiner preparing to read prior art. You are given the checklist of "
    "features of ONE subject invention, written in that applicant's own vocabulary and reference "
    "frame.\n"
    "\n"
    "For each feature, write the SAME technical idea the way OTHER patents would express it. This "
    "is what lets a reader recognise the feature in a 1974 German utility model or a translated "
    "Japanese application that shares not one noun with the subject.\n"
    "\n"
    "For each feature return:\n"
    '  "concept": the feature restated as a short, general technical idea, stripped of claim '
    "scaffolding (\"at least one\", \"said\", \"first/second side\", reference numerals) and of "
    "this applicant's private naming. 5-15 words.\n"
    '  "other_words": 4-8 alternative terms and phrasings the same thing is called elsewhere — '
    "older terms, the generic term, the industrial term, the robotics term, the translated term. "
    "Include component synonyms AND functional restatements.\n"
    '  "counts_as": one short sentence saying what ELSE in a reference would satisfy this '
    "feature — a species of the genus, an equivalent structure, a different reference frame.\n"
    "\n"
    "Do not broaden a feature into something it is not. \"A bracing structure on the base, inside "
    "the seal, shorter than the seal\" is the right generality for \"at least one bracing "
    "structure protrudes from the second side of the base element to a lesser extent than the "
    "vacuum seal element\"; \"a support\" is too broad and is useless.\n"
    'Return ONLY JSON: {"features":[{"item":"<the feature verbatim as given>","concept":"...",'
    '"other_words":["..."],"counts_as":"..."}]} with one entry per feature, in the order given.'
)


def concept_expansions(features, title="", brief=""):
    """{feature: "concept · other words · what else counts"} for the whole checklist, in one call.

    WHY THIS EXISTS. The checklist is written in the subject's vocabulary — it is extracted from
    the subject's own claims — and the references are not. Measured on adhoc-939fb711122b, "seal
    element elastically deformable at contact surface" is disclosed by hundreds of references that
    say sealing lip, gasket, resilient ring or Dichtlippe, and the retrieval-side grid scored the
    best of them 0.07 because it was comparing strings. This map is the bridge: computed ONCE per
    search (not per reference), sent with the checklist, and reused by the second-pass re-read.

    Fail-soft: an empty map simply means the reader works from the feature text alone, which is
    what it did before.
    """
    feats = [f for f in (features or []) if str(f or "").strip()][:MAX_FEATURES]
    if not feats:
        return {}
    out = {}
    #  Batched for the same reason the chart is: a single answer covering fifty features with
    #  eight synonyms each is where a model starts returning three words per row.
    for i in range(0, len(feats), FEATURE_BATCH):
        batch = feats[i:i + FEATURE_BATCH]
        try:
            got = llm.chat_json(_CONCEPT_SYS, json.dumps(
                {"invention_title": title[:200], "invention": (brief or "")[:2500],
                 "features": batch}, ensure_ascii=False), max_tokens=6000) or {}
        except Exception:
            continue
        rows = got.get("features") or []
        for item, raw in zip(batch, _align(rows, batch)):
            if not isinstance(raw, dict):
                continue
            bits = []
            concept = " ".join(str(raw.get("concept") or "").split())[:200]
            words = [" ".join(str(w).split()) for w in (raw.get("other_words") or [])
                     if str(w or "").strip()][:8]
            counts = " ".join(str(raw.get("counts_as") or "").split())[:300]
            if concept:
                bits.append(concept)
            if words:
                bits.append("also called: " + "; ".join(words))
            if counts:
                bits.append(counts)
            if bits:
                out[item] = " — ".join(bits)[:600]
    return out


_REREAD_SYS = (
    "You are a patent examiner taking a SECOND look at one reference. A first reading of this "
    "document found nothing for the features below. First readings miss disclosures for one "
    "reason above all others: the feature is phrased in the subject's vocabulary and the reference "
    "uses its own.\n"
    "\n"
    "For each feature you are given the idea behind it and the other words the same thing goes by. "
    "Search the reference text for the IDEA. Look in the description and the drawing description, "
    "not only in the claims — an older patent often teaches in its figures what a modern one "
    "claims. A species discloses its genus. An equivalent structure performing the same function "
    "discloses the function.\n"
    "\n"
    "Return a row for each feature: verdict \"disclosed\", \"partial\" or \"absent\"; quote, the "
    "EXACT verbatim passage from the reference, at most 40 words, copied word for word, empty when "
    "absent; note, one short sentence; confidence 0.0-1.0.\n"
    "\n"
    "Most of these really will be absent, and saying so is correct — you are not being asked to "
    "find something. Change the answer only where the reference genuinely teaches the idea in its "
    "own words. NEVER paraphrase a quote, NEVER stitch sentences together, NEVER invent one.\n"
    'Return STRICT JSON: {"features":[{"item":"<the feature verbatim as given>","verdict":"...",'
    '"quote":"...","note":"...","confidence":0.0}]}'
)


_REREAD_CLAIM_SYS = (
    "You are a patent examiner taking a SECOND look at one reference, this time for a few SPECIFIC "
    "CLAIMS of the subject patent. A first reading of this document found nothing for them, and a "
    "search of the whole corpus has found nothing for them either: as things stand these claims "
    "have NO prior art at all.\n"
    "\n"
    "For each claim you are given the claim text and the idea behind it in other people's words. "
    "Search the reference for the IDEA, not for the claim's phrasing. Look in the description and "
    "the drawing description, not only in the claims — an older patent often teaches in its figures "
    "what a modern one claims. A species discloses its genus. An equivalent structure performing "
    "the same function discloses the function.\n"
    "\n"
    "A claim is a whole set of limitations. Answer \"disclosed\" only if this reference teaches ALL "
    "of them. If it teaches some, answer \"partial\" and say in the note which limitation is "
    "missing.\n"
    "\n"
    "A PARTIAL IS A REAL ANSWER HERE AND IT IS WANTED. These claims currently have nothing against "
    "them, so a reference that teaches most of a claim, or teaches the specific thing a dependent "
    "claim ADDS to the claim it depends from, is exactly what is being looked for — even if it "
    "comes from a different field. Say so rather than rounding it down to \"absent\".\n"
    "\n"
    "What you must NOT do is manufacture one. Most of these will still be absent and saying so is "
    "correct. NEVER paraphrase a quote, NEVER stitch sentences together, NEVER invent one — the "
    "quote is the reference's own words, copied exactly, or there is no answer.\n"
    'Return STRICT JSON: {"claims":[{"item":"<the claim label verbatim, e.g. \\"claim 7\\">",'
    '"verdict":"...","quote":"...","note":"...","confidence":0.0}]}'
)


def reread_absent(pub, rows, hints=None, ref=None, max_features=FEATURE_BATCH, kind="feature",
                  texts=None):
    """Second pass over the rows a first reading returned nothing for. -> rows changed.

    `kind="claim"` re-asks about the SUBJECT'S CLAIMS instead of the feature checklist, with the
    claim text in `texts` and a prompt that knows a claim is a conjunction. Used by claim_rescue
    for the claims the whole search found nothing for.

    THIS IS THE AGENTIC PART OF THE READING. The first pass reads a whole patent against a whole
    checklist in one breath and is, measurably, conservative: it answers "absent" whenever it
    cannot find a sentence that restates the feature. This pass takes only what came back empty,
    hands the reader the CONCEPT and the other words for it, and asks the narrower question
    against the same text. A row is only ever upgraded on a quote that passes the same grounding
    and location gates as the first pass, so a second look cannot lower the evidential bar.
    """
    targets = [r for r in rows
               if r.get("verdict") == "absent" and r.get("grounding") in
               ("model-absent", "dropped-ungrounded-quote", "dropped-unlocatable-quote",
                "no-row-returned")][:max_features]
    if not targets:
        return 0
    ref = ref or full_text(pub)
    if not ref.get("found") or not ref.get("passages"):
        return 0
    shown = _rendered(ref)
    hints = hints or {}
    texts = texts or {}
    items = [r["item"] for r in targets]
    #  A CLAIM CARRIES ITS OWN TEXT INTO THE PROMPT; a feature is its own text. Handed the bare
    #  label "claim 7" a reader has nothing to look for, which is the same defect the refuter had
    #  (see _refute): it answered about our prompt instead of about the evidence.
    key = "claims" if kind == "claim" else "features"
    system = _REREAD_CLAIM_SYS if kind == "claim" else _REREAD_SYS
    asked = [{"item": it, "idea": hints.get(it) or texts.get(it) or it} for it in items]
    if kind == "claim":
        for a in asked:
            a["claim_text"] = texts.get(a["item"]) or ""
    payload = {"reference": pub, "reference_text": shown, key: asked}
    try:
        #  STRONG TIER. The second look exists because the first read economised; asking the same
        #  model the same way again is the one thing that cannot help. Low volume: a few hundred
        #  calls against tens of thousands.
        out = llm.chat_json(system, json.dumps(payload, ensure_ascii=False), tier="strong",
                            max_tokens=8000) or {}
    except Exception:
        return 0
    changed = 0
    by_item = {r["item"]: r for r in targets}
    #  Accept the other key too: the model occasionally answers under "features" whatever it was
    #  asked, and dropping the whole answer over a key name reads as "nothing was found".
    rows_back = out.get(key)
    if not rows_back:
        rows_back = out.get("features") if key == "claims" else out.get("claims")
    for item, raw in zip(items, _align(rows_back, items)):
        if not isinstance(raw, dict):
            continue
        fresh = _row(item, raw, ref, shown, kind)
        if fresh["verdict"] == "absent" or fresh["grounding"] != "verified":
            continue
        row = by_item.get(item)
        if row is None:
            continue
        row.update(fresh)
        #  Recorded so the report can say which cells the second look found, and so a later audit
        #  can measure this pass instead of taking it on trust.
        row["second_pass"] = True
        changed += 1
    return changed


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
