"""Bounded DE/EP/WO enrichment (Milestone 5 §1 fix for the German full-text hole).

For a budget-bounded, field-representative set of claimless DE/EP/WO publications, fetch their
CLAIMS from SerpApi (returned in the native language — German for DE), parse + resolve them,
insert claim rows + claim chunks, then embed the new chunks (Vertex). New vectors are inserted
into the existing HNSW index incrementally (no full rebuild). Idempotent + resumable.
"""
from __future__ import annotations
import json, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
import db, patent_text as pt, enrich, embed
from config import DATA

PUBS_FILE = DATA / "de_enrich_pubs.json"
WORKERS = 8
_lock = threading.Lock()


def _claims_blob(d):
    cl = d.get("claims")
    if isinstance(cl, list):
        # SerpApi returns each claim as an element; ensure a leading number so the parser splits
        import re
        parts = []
        for i, c in enumerate(cl):
            c = str(c).strip()
            parts.append(c if re.match(r"^\s*\d", c) else f"{i+1}. {c}")
        return "\n".join(parts)
    return cl or ""


def fetch_one(pub):
    """SerpApi -> (pub, claims list of dicts, lang) or (pub, None, reason)."""
    d = enrich.fetch_details(pub)
    if not d:
        return pub, None, "no_details"
    blob = _claims_blob(d)
    if not blob:
        return pub, None, "no_claims"
    claims = pt.resolve_claims(pt.split_claims(blob))
    # detect language of claim 1
    import re
    lang = "de" if claims and re.search(r"\b(und|der|die|das|eine|einer|mit|zum|dass)\b",
                                        (claims[0]["text"] or "").lower()) else "en"
    return pub, claims, lang


def run():
    pubs = json.loads(PUBS_FILE.read_text())
    print(f"[de-enrich] {len(pubs)} pubs to enrich")
    src = db.get_source_id("serpapi:google_patents", "2026-07-de")
    # resume: skip pubs that already have claims
    with db.cursor() as cur:
        cur.execute("SELECT publication_number FROM publications p WHERE p.publication_number = ANY(%s) "
                    "AND EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=p.id)", (pubs,))
        done = {r["publication_number"] for r in cur.fetchall()}
    todo = [p for p in pubs if p not in done]
    print(f"[de-enrich] {len(done)} already have claims; {len(todo)} to fetch")

    conn = db.connect(); cur = conn.cursor()
    ok = fail = added_claims = added_chunks = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for pub, claims, lang in ex.map(fetch_one, todo):
            if not claims:
                fail += 1
                continue
            cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
            row = cur.fetchone()
            if not row:
                fail += 1; continue
            pid = row["id"]
            # re-check inside the loop (idempotent)
            cur.execute("SELECT count(*) c FROM claims WHERE publication_id=%s", (pid,))
            if cur.fetchone()["c"] > 0:
                continue
            import json as _j
            for c in claims:
                cur.execute("INSERT INTO claims(publication_id,claim_no,is_independent,lang,text,resolved_text) "
                            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                            (pid, c["claim_no"], c["is_independent"], lang, c["text"], c["resolved_text"]))
                cid = cur.fetchone()["id"]
                coord = _j.dumps({"claim_no": c["claim_no"]})
                own = (c["text"] or "")[:8000]
                cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                            "VALUES (%s,'claim_own',%s,%s,%s,%s,%s)",
                            (pid, cid, coord, lang, own, max(1, len(own)//4)))
                added_chunks += 1
                res = (c["resolved_text"] or "")[:8000]
                if res and res != own:
                    cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                                "VALUES (%s,'claim_resolved',%s,%s,%s,%s,%s)",
                                (pid, cid, coord, lang, res, max(1, len(res)//4)))
                    added_chunks += 1
                added_claims += 1
            cur.execute("INSERT INTO field_provenance(entity,entity_id,field,source_id,ocr_status) "
                        "VALUES ('publication',%s,'enriched_claims_de',%s,'scrape')", (pid, src))
            ok += 1
            if ok % 100 == 0:
                conn.commit()
                print(f"  enriched {ok} pubs, {added_claims} claims, {added_chunks} chunks, "
                      f"{fail} fail, {time.time()-t0:.0f}s", flush=True)
    conn.commit(); cur.close(); conn.close()
    print(f"[de-enrich] done: {ok} enriched, {fail} failed, {added_claims} claims, {added_chunks} chunks")
    # embed the new NULL chunks (HNSW maintains incrementally on UPDATE)
    pending = db.scalar("SELECT count(*) FROM chunks WHERE embedding IS NULL")
    print(f"[de-enrich] embedding {pending} new chunks...")
    embed.run(order_priority=False)
    print("[de-enrich] embed done")


if __name__ == "__main__":
    run()
