"""Read ONE requirement against MANY documents in a single call, instead of one document against
many requirements.

MEASURED BEFORE IT WAS BUILT (2026-08-17, offline A/B on adhoc-5972e6042dfa: the same 25 documents,
the same 40 verbatim limitations, the same read tier, only the shape of the ask changed):

    1,000 (document, requirement) pairs, 200 calls, 2.5 min
    recall against per-document reading            83%   (365 of 441 grounded cells)
    cells batching found that per-document missed   298
    limitations that LOST their evidence entirely     0   (40/40 grounded both ways)
    limitations covered at the ledger's bar (>=2)   40/40 batched vs 37/40 per-document
    quotes attributed to a document not in the batch  1 in 200 calls, caught by the grounding gate

The 17% of cells batching loses land on limitations that already had 11 to 20 grounded documents, so
they cost nothing that the ledger measures. The economising effect this repo measured twice (asking
for 12 requirements per call found 58 cells, 4 found 66, 2 found 88) does not bite here because that
effect is about REQUIREMENTS PER CALL, and this shape asks for exactly one.

WHAT THIS IS NOT
----------------
It is NOT cheaper or faster per unit of work, and believing otherwise would size the pipeline wrong.
Measured on the same run:

    throughput                batched 6.7 pairs/s   per-document 26.8 pairs/s
    effective tokens per pair batched ~8,600        per-document ~1,266

Roughly 4x slower and 7x more tokens FOR THE SAME PAIRS, because a call carries ~150k tokens instead
of ~15.6k. The win comes from asking far fewer pairs: 20 claims against their own 25 retrieved
documents is 500 pairs, where reading 549 documents against 40 limitations is 21,960. The batch
shape is the vehicle; per-claim retrieval is the engine. A poor top-25 is not rescued by batching.

PROMPT CACHING IS WHY THE PER-PAIR PENALTY IS SURVIVABLE. Batches are ordered DOCUMENTS-FIRST, so
every requirement asked of one batch shares one long prefix. Measured: 18,814,313 of 22,779,905
prompt tokens served from cache, 83%. Reversing the payload order would forfeit all of it, which is
the same defect `deep_analysis._ask` had.
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import deep_analysis
import grounding
import llm

#  Characters of rendered document text per call. ~600k chars is ~150k tokens, which sits inside a
#  200k window and well inside 1M. Measured document sizes: mean 15,583 tokens, median 12,191,
#  p90 31,107, max 55,000 — so BUDGET BY CHARACTERS, never by document count. A fixed "ten
#  documents" is 156k tokens at the mean and 550k at the maximum.
BATCH_CHARS = int(os.environ.get("BATCH_READ_CHARS", "600000"))
#  Per-document cap inside a batch, so one 220k-character monster cannot crowd out nine others.
DOC_CAP = int(os.environ.get("BATCH_READ_DOC_CAP", "120000"))
WORKERS = int(os.environ.get("BATCH_READ_WORKERS", "8"))
MAX_TOKENS = int(os.environ.get("BATCH_READ_MAX_TOKENS", "6000"))
#  Documents whose text we load at once. Bounded because each is up to DOC_CAP in memory.
LOAD_WORKERS = int(os.environ.get("BATCH_READ_LOAD_WORKERS", "8"))

_RANK = {"disclosed": 3, "partial": 2, "uncertain": 1, "absent": 0}

#  ENGLISH TEXT ONLY. The deep read wave enriches a reference (fetches its English full text)
#  before reading; this tail deliberately reads what the corpus already holds — and for DE/FR
#  documents that is the ORIGINAL-language claims. The first production tail dutifully quoted
#  them verbatim, grounded them (they ARE in the text) and put 47 German cells on an English
#  report. A non-English reference is skipped here and named in the log; the deep tier's
#  enrichment path is where it gets read properly.
def _mostly_english(text):
    return deep_analysis.mostly_english(text)

_SYS = (
    "You are a patent examiner. You are given ONE REQUIREMENT taken verbatim from a subject "
    "patent's claims, and SEVERAL prior-art references, each labelled with its publication "
    "number.\n"
    "\n"
    "For EVERY reference supplied, decide what THAT reference discloses about THAT ONE "
    "requirement:\n"
    "- verdict: \"disclosed\" (the reference clearly teaches it), \"partial\" (related but "
    "incomplete, narrower, or a different mechanism), or \"absent\" (not in this reference).\n"
    "- quote: the EXACT verbatim passage FROM THAT REFERENCE, copied word for word, at most 40 "
    "words. NEVER paraphrase, NEVER stitch separate sentences together, NEVER invent, and NEVER "
    "take the quote from a different reference than the one you are answering about. Empty string "
    "when absent.\n"
    "- note: one short sentence saying what the quoted passage teaches and, for \"partial\", what "
    "it does NOT teach.\n"
    "- confidence: 0.0-1.0.\n"
    "\n"
    "MATCH ON MEANING, NOT ON WORDING. This is the single most common way this task is done badly. "
    "The requirement is phrased in the SUBJECT's vocabulary and reference frame. The references "
    "were written by different people, often decades earlier, frequently translated from another "
    "language, and usually for a different application, so they will almost never use the same "
    "nouns. Decide whether the reference teaches the same TECHNICAL IDEA:\n"
    "  - the same part under another name — seal / gasket / sealing lip / sealing ring / "
    "diaphragm; base / body / plate / housing / frame; air extraction means / vacuum pump / "
    "blower / fan / ejector / venturi;\n"
    "  - the same structure described from another reference frame;\n"
    "  - the same function achieved by an equivalent structure;\n"
    "  - a SPECIES of a genus the requirement states.\n"
    "Reserve \"absent\" for a reference that does not teach the idea AT ALL — never for one that "
    "simply words it differently.\n"
    "\n"
    "Answer for EVERY reference given, in the order given, using ONLY the text supplied.\n"
    "\n"
    "THE TWO BARS. A verbatim quote is the strong bar. When a reference GENUINELY TEACHES the "
    "requirement but no single verbatim passage of at most 40 words supports it (the teaching "
    "sits in a figure description, spans sentences, or is implicit in the mechanism), keep your "
    "honest verdict, set \"teaches\": true, leave \"quote\" EMPTY, and say in \"note\" where the "
    "teaching sits. NEVER stitch or invent a quote; a truthful quote-free teaching beats a "
    "fabricated quote, which is discarded anyway.\n"
    'Return ONLY JSON: {"references":[{"pub":"<publication number>","verdict":"...",'
    '"quote":"...","note":"...","confidence":0.0,"teaches":false}]}'
)


def _load(pubs, log=print):
    """{pub: (ref, shown)} — the same full text and rendering the per-document reader uses.
    Non-English documents are skipped (see _mostly_english) and counted in the log."""
    out, lock = {}, threading.Lock()
    skipped_lang = [0]

    def one(pub):
        try:
            ref = deep_analysis.full_text(pub, max_chars=DOC_CAP)
            if not ref.get("found") or not ref.get("passages"):
                return
            shown = deep_analysis._rendered(ref)[:DOC_CAP]
            if not _mostly_english(shown):
                with lock:
                    skipped_lang[0] += 1
                return
            with lock:
                out[pub] = (ref, shown)
        except Exception:
            traceback.print_exc()

    with ThreadPoolExecutor(max_workers=max(1, min(LOAD_WORKERS, len(pubs) or 1))) as ex:
        list(ex.map(one, list(pubs)))
    if skipped_lang[0]:
        log(f"[batch_read] skipped {skipped_lang[0]} non-English references — the tail must not "
            f"quote what the page cannot read; the deep tier's enrichment path covers them")
    return out


def _batches(pubs, loaded, budget=BATCH_CHARS):
    """Group documents by character budget. See BATCH_CHARS: never by count."""
    out, cur, size = [], [], 0
    for p in pubs:
        got = loaded.get(p)
        if not got:
            continue
        n = len(got[1])
        if cur and size + n > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += n
    if cur:
        out.append(cur)
    return out


def read(pubs, items, kind="claim", emit=None, log=print, workers=WORKERS):
    """Chart every `pub` against every `item`. -> {pub: {item_label: row}}

    Rows come back through `deep_analysis._row`, so the grounding gate, the location resolver and
    the verdict vocabulary are byte-for-byte the ones the per-document path uses. A batched cell is
    held to exactly the same bar; only the question's shape differs.
    """
    labels = [i["label"] if isinstance(i, dict) else str(i) for i in items]
    texts = {(i["label"] if isinstance(i, dict) else str(i)):
             (i.get("text") if isinstance(i, dict) else "") for i in items}
    loaded = _load(pubs, log=log)
    if not loaded or not labels:
        return {}
    batches = _batches([p for p in pubs if p in loaded], loaded)
    jobs = [(bi, lab) for bi in range(len(batches)) for lab in labels]
    log(f"[batch_read] {len(loaded)} documents x {len(labels)} requirements -> "
        f"{len(batches)} batches ({[len(b) for b in batches]} docs) = {len(jobs)} calls")

    out = {p: {} for p in loaded}
    lock, done = threading.Lock(), [0]
    misattributed = [0]

    def one(job):
        bi, label = job
        pubs_in = batches[bi]
        #  DOCUMENTS FIRST. This is the cache prefix: every requirement asked of this batch reuses
        #  it. Putting the requirement first would forfeit the 83% cache hit rate this shape was
        #  measured at, exactly as `deep_analysis._ask` used to.
        doc_part = json.dumps({"references": [{"pub": p, "text": loaded[p][1]}
                                              for p in pubs_in]}, ensure_ascii=False)[:-1]
        tail = ", " + json.dumps({"requirement": texts.get(label) or label},
                                 ensure_ascii=False)[1:]
        try:
            got = llm.chat_json(_SYS, [{"text": doc_part, "cache": True}, {"text": tail}],
                                tier="read", max_tokens=MAX_TOKENS) or {}
        except Exception:
            traceback.print_exc()
            got = {}
        rows = got.get("references") or []
        in_batch = set(pubs_in)
        with lock:
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                pub = str(raw.get("pub") or "").strip()
                if pub not in in_batch:
                    #  A quote credited to a document that was not in front of the model. Rare
                    #  (1 in 200 calls measured) but it is the failure mode unique to this shape,
                    #  so it is counted rather than silently dropped.
                    misattributed[0] += 1
                    continue
                ref, shown = loaded[pub]
                row = deep_analysis._row(label, raw, ref, shown, kind)
                prev = out[pub].get(label)
                if prev is None or _RANK.get(row["verdict"], 0) > _RANK.get(prev["verdict"], 0):
                    out[pub][label] = row
            done[0] += 1
            if emit and (done[0] % 10 == 0 or done[0] == len(jobs)):
                emit("batch_read_progress", done=done[0], total=len(jobs))

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
        list(ex.map(one, jobs))

    #  Never leave a hole: a pair the model simply did not answer about is "absent with no row",
    #  which is what the per-document path records too, rather than a missing key downstream.
    for pub in out:
        ref, shown = loaded[pub]
        for label in labels:
            out[pub].setdefault(label, deep_analysis._row(label, None, ref, shown, kind))
    grounded_cells = sum(1 for p in out for l in out[p]
                         if out[p][l]["grounding"] == "verified"
                         and out[p][l]["verdict"] in ("disclosed", "partial"))
    log(f"[batch_read] {grounded_cells} grounded cells across {len(out)} documents"
        + (f"; {misattributed[0]} quotes credited to a document not in their batch"
           if misattributed[0] else ""))
    return out


def charts(pubs, items, kind="claim", titles=None, emit=None, log=print):
    """`read`, assembled into the per-document chart dicts `deep_rank` already consumes.

    Same keys as `deep_analysis.analyse_reference` so the ledger, the claim page and
    `coverage_rank` need no knowledge of which reader produced a cell.
    """
    titles = titles or {}
    cells = read(pubs, items, kind=kind, emit=emit, log=log)
    labels = [i["label"] if isinstance(i, dict) else str(i) for i in items]
    out = []
    for pub, by_label in cells.items():
        rows = [by_label[l] for l in labels if l in by_label]
        #  method stays "llm" — the ledger, the rescue and the grid all gate on it — and
        #  `batched` records how the cells were obtained.
        ref = {"pub": pub, "title": titles.get(pub, ""), "found": True,
               "features": [], "claims": rows if kind == "claim" else [],
               "method": "llm", "batched": True, "chars": 0, "refuted": 0}
        if kind != "claim":
            ref["features"] = rows
        try:
            #  The refuter is NOT optional here. Grounding proves the quote exists in that
            #  document; it does not prove the document teaches the requirement, and the measured
            #  298 cells this shape finds that per-document reading missed are exactly the
            #  population that needs an independent second opinion before it is asserted.
            ref["refuted"] = deep_analysis._refute(
                rows, pub, {l: (texts_of(items).get(l) or "") for l in labels})
            ref["counts"] = deep_analysis._counts(rows)
        except Exception:
            traceback.print_exc()
        out.append(ref)
    return out


def texts_of(items):
    return {(i["label"] if isinstance(i, dict) else str(i)):
            (i.get("text") if isinstance(i, dict) else "") for i in items}
