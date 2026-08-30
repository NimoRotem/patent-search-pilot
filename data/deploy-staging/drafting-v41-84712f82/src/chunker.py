"""Hierarchical chunking — index EVERY claim (spec §4).

Chunk kinds: whole | abstract | claim_own | claim_resolved | paragraph | figure_caption.
Cross-lingual: for DE/EP/WO docs with an original-language abstract distinct from the English
MT, emit BOTH. Dependent claims get own AND parent-resolved text; independent claims emit only
own (resolved == own). Coordinates preserved in `coord` for grounded citations.
"""
from __future__ import annotations
import json
import bqclient, db
from ingest_bq import CORE_TBL, EXPANDED_TBL

MAX_CHARS = 8000
BATCH = 500


def _clip(s, n=MAX_CHARS):
    s = (s or "").strip()
    return s[:n] if len(s) > n else s


def _tok(s):
    return max(1, len(s) // 4)


def load_orig_abstracts():
    """{publication_number: (orig_text, orig_lang)} for non-English originals (cross-lingual)."""
    m = {}
    for tbl in (CORE_TBL, EXPANDED_TBL):
        sql = (f"SELECT publication_number, abstract_orig.text t, abstract_orig.lang l "
               f"FROM `{tbl}` WHERE abstract_orig.text IS NOT NULL AND abstract_orig.lang != 'en'")
        for r in bqclient.run(sql, max_gb_billed=10):
            if r["t"]:
                m[r["publication_number"]] = (r["t"], r["l"])
    return m


def run(rebuild=False):
    with db.cursor(autocommit=True) as cur:
        if rebuild:
            cur.execute("TRUNCATE chunks RESTART IDENTITY CASCADE")
        cur.execute("SELECT count(*) c FROM chunks")
        if cur.fetchone()["c"] and not rebuild:
            print("[chunk] chunks already present; use rebuild=True to redo"); return

    print("[chunk] loading original-language abstracts from staging...")
    orig = load_orig_abstracts()
    print(f"[chunk] {len(orig):,} non-EN original abstracts available")

    conn = db.connect(); cur = conn.cursor()
    cur.execute("SELECT max(id) m FROM publications")
    maxid = cur.fetchone()["m"] or 0
    total_chunks = 0

    for lo in range(0, maxid + 1, BATCH):
        hi = lo + BATCH
        cur.execute("SELECT id, publication_number, title, abstract, abstract_lang, tier "
                    "FROM publications WHERE id >= %s AND id < %s", (lo, hi))
        pubs = cur.fetchall()
        if not pubs:
            continue
        ids = [p["id"] for p in pubs]
        inlist = "(" + ",".join(["%s"] * len(ids)) + ")"
        cur.execute(f"SELECT id, publication_id, claim_no, is_independent, lang, text, resolved_text "
                    f"FROM claims WHERE publication_id IN {inlist} ORDER BY publication_id, claim_no", ids)
        claims_by = {}
        for c in cur.fetchall():
            claims_by.setdefault(c["publication_id"], []).append(c)
        cur.execute(f"SELECT id, publication_id, para_no, heading, page_no, lang, text "
                    f"FROM paragraphs WHERE publication_id IN {inlist}", ids)
        paras_by = {}
        for p in cur.fetchall():
            paras_by.setdefault(p["publication_id"], []).append(p)
        cur.execute(f"SELECT id, publication_id, figure_no, caption FROM figures WHERE publication_id IN {inlist}", ids)
        figs_by = {}
        for f in cur.fetchall():
            figs_by.setdefault(f["publication_id"], []).append(f)

        rows = []  # (publication_id, kind, ref_id, coord_json, lang, text, token_count)
        for p in pubs:
            pid, num = p["id"], p["publication_number"]
            title, abstract = p["title"], p["abstract"]
            lang = p["abstract_lang"] or "en"
            whole = _clip((title or "") + (". " + abstract if abstract else ""), 2000)
            if whole:
                rows.append((pid, "whole", None, None, "en", whole, _tok(whole)))
            if abstract:
                a = _clip(abstract)
                rows.append((pid, "abstract", None, None, "en", a, _tok(a)))
            # cross-lingual original abstract
            if num in orig:
                otext, olang = orig[num]
                if otext and otext.strip() != (abstract or "").strip():
                    oc = _clip(otext)
                    rows.append((pid, "abstract", None, json.dumps({"lang": olang}), olang, oc, _tok(oc)))
            for c in claims_by.get(pid, []):
                coord = json.dumps({"claim_no": c["claim_no"]})
                own = _clip(c["text"])
                if own:
                    rows.append((pid, "claim_own", c["id"], coord, c["lang"] or "en", own, _tok(own)))
                res = _clip(c["resolved_text"])
                if res and res != own:  # dependent claims only (else duplicate)
                    rows.append((pid, "claim_resolved", c["id"], coord, c["lang"] or "en", res, _tok(res)))
            for pa in paras_by.get(pid, []):
                coord = json.dumps({"para_no": pa["para_no"], "heading": pa["heading"], "page_no": pa["page_no"]})
                t = _clip(pa["text"])
                if t:
                    rows.append((pid, "paragraph", pa["id"], coord, pa["lang"] or "en", t, _tok(t)))
            for fg in figs_by.get(pid, []):
                if fg["caption"]:
                    coord = json.dumps({"figure_no": fg["figure_no"]})
                    t = _clip(fg["caption"])
                    rows.append((pid, "figure_caption", fg["id"], coord, "en", t, _tok(t)))

        if rows:
            with cur.copy("COPY chunks (publication_id, kind, ref_id, coord, lang, text, token_count) "
                          "FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
            conn.commit()
            total_chunks += len(rows)
        print(f"  pubs<={hi:,} chunks={total_chunks:,}", end="\r", flush=True)

    print(f"\n[chunk] done: {total_chunks:,} chunks")
    cur.execute("SELECT kind, count(*) c FROM chunks GROUP BY kind ORDER BY c DESC")
    for r in cur.fetchall():
        print(f"  {r['kind']:16s} {r['c']:,}")
    cur.close(); conn.close()


if __name__ == "__main__":
    import sys
    run(rebuild="--rebuild" in sys.argv)
