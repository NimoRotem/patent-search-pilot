"""The demand loop: turning what searches asked for into what the next release holds.

WHERE THE REQUESTS COME FROM. A search that reaches a document this corpus holds no text for is
forbidden to write the corpus (docs/corpus_write_policy.md). What it may do is put the fetched text
in the scratch store (`sources_docstore`) and ASK for the publication in the next permanent release
(`corpus_ingest_queue`). `request_count` is the demand signal: a publication four searches wanted
is worth four times one that one search wanted, and `runstore.pending_ingest` already orders by it.

WHAT THIS MODULE ADDS. The other half of that loop. `runstore.claim_ingest` takes a batch with
FOR UPDATE SKIP LOCKED; this chunks and embeds the scratch text those rows point at, carries it
into the release under construction, records what happened in `corpus_fetch_ledger`, and calls
`runstore.mark_ingested` with the release id once the release is sealed.

WHY THE LEDGER EXISTS SEPARATELY FROM THE QUEUE. The queue records a REQUEST. Without a ledger, a
publication that was requested four times and produced no usable text looks exactly like one that
was never asked for: both are simply absent from the release. The ledger is how "we tried and the
source had nothing" stays distinguishable from "nobody wanted it".
"""
from __future__ import annotations

import json
import re
import time

MAX_CHARS = 8000        # same clip as src/chunker.py, so demand rows match corpus rows
MIN_CHARS = 20
CHUNKER_VERSION = "corpus-demand/1"
EMBED_SUB = 200

#  A claim starts with its number. Two common shapes, and a fallback that keeps the whole block as
#  one chunk rather than dropping text that did not match a pattern.
_CLAIM_SPLIT = re.compile(r"(?m)^\s*(?:\[?\d{1,3}\]?[.)]|\d{1,3}\s*\.)\s+")
_PARA_SPLIT = re.compile(r"\n\s*\n+")


def split_claims(text):
    parts = [p.strip() for p in _CLAIM_SPLIT.split(text or "") if p and p.strip()]
    return [p[:MAX_CHARS] for p in parts if len(p.strip()) >= MIN_CHARS]


def split_description(text):
    parts = [p.strip() for p in _PARA_SPLIT.split(text or "") if p and p.strip()]
    return [p[:MAX_CHARS] for p in parts if len(p.strip()) >= MIN_CHARS]


def pending(limit=500, runstore=None):
    rs = runstore or _runstore()
    return rs.pending_ingest(limit=limit)


def claim(limit=100, runstore=None):
    """Take a batch of requests. SKIP LOCKED, so two release builders never take the same one."""
    rs = runstore or _runstore()
    return rs.claim_ingest(limit=limit)


def mark_ingested(publication_numbers, release_id, note="", runstore=None):
    rs = runstore or _runstore()
    return rs.mark_ingested(list(publication_numbers), corpus_release=release_id, note=note)


def _runstore():
    import runstore
    return runstore


def _docstore():
    from sources import docstore
    return docstore


def record(dst, rows, *, release_id="", state="fetched"):
    """Write claimed queue rows into `corpus_fetch_ledger` on the builder database."""
    if not rows:
        return 0
    with dst.cursor() as cur:
        for r in rows:
            cur.execute("""INSERT INTO corpus_fetch_ledger (publication_number, release_id,
                               queue_id, request_count, priority, source, scratch_ref, state,
                               claims_chars, desc_chars, reason)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (publication_number, release_id) DO UPDATE SET
                               request_count = EXCLUDED.request_count,
                               priority = EXCLUDED.priority,
                               state = EXCLUDED.state,
                               claims_chars = EXCLUDED.claims_chars,
                               desc_chars = EXCLUDED.desc_chars""",
                        (r["publication_number"], release_id, r.get("id"),
                         int(r.get("request_count") or 0), int(r.get("priority") or 100),
                         r.get("source") or "", r.get("scratch_ref") or "", state,
                         int((r.get("payload") or {}).get("claims_chars") or 0),
                         int((r.get("payload") or {}).get("desc_chars") or 0),
                         r.get("reason") or ""))
    dst.commit()
    return len(rows)


def set_state(dst, publication_numbers, *, release_id, state, n_chunks=None, note=""):
    if not publication_numbers:
        return 0
    with dst.cursor() as cur:
        cur.execute("""UPDATE corpus_fetch_ledger
                          SET state=%s, release_id=%s, note=%s,
                              n_chunks=COALESCE(%s, n_chunks),
                              included_at = CASE WHEN %s='included' THEN now() ELSE included_at END
                        WHERE publication_number = ANY(%s)
                          AND release_id IN ('', %s) RETURNING id""",
                    (state, release_id, note, n_chunks, state, list(publication_numbers),
                     release_id))
        n = len(cur.fetchall() or [])
    dst.commit()
    return n


def materialise(rows, *, embed_module=None, docstore=None, embed_dim=768, model=""):
    """Chunk and embed the scratch text behind a batch of claimed requests.

    -> (chunks, report). `chunks` are release-shaped dicts with no chunk_id yet; the builder
    numbers them inside the release. A request whose scratch store holds nothing produces no
    chunks and is reported as `missing`, which is the distinction the ledger exists to keep.

    Embedding happens here, in the OFFLINE builder, and never in a search. That is the whole point
    of the write policy: the search path may ask, it may not write, and it certainly may not add
    vectors to an index that is being queried.
    """
    ds = docstore or _docstore()
    if embed_module is None:
        import embed as embed_module            # noqa: PLC0415
    out, report = [], {"requested": len(rows), "with_text": 0, "missing": [], "chunks": 0,
                       "embed_calls": 0}
    pending_texts, pending_meta = [], []
    for r in rows:
        pn = r["publication_number"]
        rec = None
        try:
            rec = ds._get_sync(pn)
        except Exception as exc:                                   # noqa: BLE001
            report.setdefault("errors", []).append(f"{pn}: {str(exc)[:120]}")
        if not rec:
            report["missing"].append(pn)
            continue
        pieces = []
        if rec.get("abstract"):
            pieces.append(("abstract", rec["abstract"][:MAX_CHARS], None))
        for i, c in enumerate(split_claims(rec.get("claims") or "")):
            pieces.append(("claim_own", c, i + 1))
        for i, p in enumerate(split_description(rec.get("description") or "")):
            pieces.append(("paragraph", p, i + 1))
        if not pieces:
            report["missing"].append(pn)
            continue
        report["with_text"] += 1
        for kind, text, no in pieces:
            pending_texts.append(text)
            pending_meta.append({"publication_number": pn, "kind": kind, "text": text,
                                 "coord": {"claim_no": no} if kind == "claim_own"
                                          else ({"para_no": no} if no else {}),
                                 "lang": rec.get("claims_lang") or rec.get("desc_lang") or "",
                                 "source": "demand"})
    for i in range(0, len(pending_texts), EMBED_SUB):
        window = pending_texts[i:i + EMBED_SUB]
        vecs = embed_module.embed_texts(window, embed_dim)
        report["embed_calls"] += 1
        for meta, vec in zip(pending_meta[i:i + EMBED_SUB], vecs):
            out.append({**meta, "embedding": vec, "embed_model": model,
                        "embed_dim": embed_dim, "task_type": "RETRIEVAL_DOCUMENT",
                        "chunker_version": CHUNKER_VERSION,
                        "token_count": max(1, len(meta["text"]) // 4)})
    report["chunks"] = len(out)
    return out, report


def ledger_summary(dst, release_id=""):
    with dst.cursor() as cur:
        cur.execute("""SELECT state, count(*) n, COALESCE(sum(request_count),0) req,
                              COALESCE(sum(n_chunks),0) chunks
                       FROM corpus_fetch_ledger WHERE release_id = %s GROUP BY 1 ORDER BY 1""",
                    (release_id,))
        return [dict(r) for r in cur.fetchall()]


def report_json(report):
    return json.dumps({**report, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                      sort_keys=True, default=str)
