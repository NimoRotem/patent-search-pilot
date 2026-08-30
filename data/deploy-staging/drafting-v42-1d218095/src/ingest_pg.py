"""Load BigQuery staging tables -> normalized Postgres schema (spec §2, §3).
Streams via the BQ Storage API; parses claims/paragraphs/figures; resolves dependent claims;
records provenance. Idempotent at the publication level (skips already-loaded pubs)."""
from __future__ import annotations
import sys
import psycopg
from psycopg import sql
import bqclient, db, patent_text as pt
from ingest_bq import CORE_TBL, EXPANDED_TBL

PAGE = 1500


def norm_name(s):
    return (s or "").strip().upper() or None


def insert_returning(cur, table, cols, rows, returning="id", conflict=None):
    """Multi-row INSERT ... RETURNING, preserving VALUES order. Chunks to stay under
    Postgres's 65535-parameter limit. rows: list of tuples."""
    if not rows:
        return []
    ncol = len(cols)
    per_chunk = max(1, 60000 // ncol)
    out = []
    for i in range(0, len(rows), per_chunk):
        sub = rows[i:i + per_chunk]
        ph = ",".join(["(" + ",".join(["%s"] * ncol) + ")"] * len(sub))
        q = f"INSERT INTO {table} ({','.join(cols)}) VALUES {ph}"
        if conflict:
            q += f" {conflict}"
        q += f" RETURNING {returning}"
        cur.execute(q, [v for r in sub for v in r])
        out.extend(cur.fetchall())
    return out


def executemany(cur, table, cols, rows, conflict=""):
    if not rows:
        return
    ph = "(" + ",".join(["%s"] * len(cols)) + ")"
    q = f"INSERT INTO {table} ({','.join(cols)}) VALUES {ph} {conflict}"
    cur.executemany(q, rows)


def load_table(staging_tbl, tier, source_id, limit=None):
    total = bqclient.client().get_table(staging_tbl).num_rows
    print(f"[load] {staging_tbl} -> tier={tier} ({total:,} rows)")
    it = bqclient.client().list_rows(staging_tbl, page_size=PAGE)
    done = 0
    conn = db.connect()
    cur = conn.cursor()
    # already-loaded set (idempotent resume)
    cur.execute("SELECT publication_number||'|'||COALESCE(kind_code,'') FROM publications")
    seen = {r["?column?"] if "?column?" in r else list(r.values())[0] for r in cur.fetchall()}
    seen = set(x for x in seen)

    batch = []
    def flush(batch):
        nonlocal done
        # filter already-loaded
        rows = [r for r in batch if f"{r['publication_number']}|{r['kind_code'] or ''}" not in seen]
        if not rows:
            done += len(batch); return
        # 1) applications (upsert, get id map)
        apps = {}
        for r in rows:
            an = r["application_number"]
            if an and an not in apps:
                apps[an] = (an, r["country_code"], r["filing_date"], r["priority_date"])
        app_id = {}
        if apps:
            recs = insert_returning(
                cur, "applications",
                ["application_number", "country", "filing_date", "earliest_priority_date"],
                list(apps.values()),
                returning="id, application_number",
                conflict="ON CONFLICT (application_number) DO UPDATE SET country=EXCLUDED.country",
            )
            for rec in recs:
                app_id[rec["application_number"]] = rec["id"]
        # 2) publications
        pub_rows = []
        for r in rows:
            ab_orig = r.get("abstract_orig")
            pub_rows.append((
                app_id.get(r["application_number"]),
                r["publication_number"], r["kind_code"], r["country_code"],
                r["publication_date"], r["filing_date"], r["priority_date"],
                r["family_id"], None,
                r["title_en"], r["abstract_en"],
                (ab_orig["lang"] if ab_orig and ab_orig.get("text") else None),
                tier,
            ))
        precs = insert_returning(
            cur, "publications",
            ["application_id", "publication_number", "kind_code", "country", "publication_date",
             "filing_date", "earliest_priority_date", "simple_family_id", "extended_family_id",
             "title", "abstract", "abstract_lang", "tier"],
            pub_rows, returning="id",
            conflict="ON CONFLICT (publication_number, kind_code) DO NOTHING",
        )
        # ON CONFLICT DO NOTHING may drop rows -> re-fetch ids by natural key for safety
        pub_id = {}
        keys = [(r["publication_number"], r["kind_code"] or "") for r in rows]
        cur.execute(
            "SELECT id, publication_number, COALESCE(kind_code,'') kc FROM publications "
            "WHERE (publication_number, COALESCE(kind_code,'')) IN (" +
            ",".join(["(%s,%s)"] * len(keys)) + ")",
            [v for k in keys for v in k],
        )
        for rec in cur.fetchall():
            pub_id[(rec["publication_number"], rec["kc"])] = rec["id"]

        claim_rows, para_rows, fig_rows, class_rows, cite_rows, party_rows, prov_rows = \
            [], [], [], [], [], [], []
        claim_index = []  # (pid, claim_no, parents) aligned with claim_rows order
        for r in rows:
            pid = pub_id.get((r["publication_number"], r["kind_code"] or ""))
            if pid is None:
                continue
            # claims (prefer en; fall back to original-language blob)
            blob = r["claims_en"]
            lang = "en"
            if not blob:
                co = r.get("claims_orig")
                if co and co.get("text"):
                    blob, lang = co["text"], co["lang"]
            claims = pt.resolve_claims(pt.split_claims(blob)) if blob else []
            for c in claims:
                claim_rows.append((pid, c["claim_no"], c["is_independent"], lang,
                                   c["text"], c["resolved_text"]))
                claim_index.append((pid, c["claim_no"], c["parents"]))
            # description paragraphs + figures (core only; description_en absent on expanded)
            desc = r.get("description_en") if tier == "core" else None
            if desc:
                for p in pt.split_paragraphs(desc):
                    para_rows.append((pid, p["para_no"], p["heading"], None, "en", p["text"]))
                for f in pt.figure_captions(desc):
                    fig_rows.append((pid, f["figure_no"], f["caption"], f["reference_numbers"]))
            # classifications (ipc is a core-only staging column)
            for c in (r.get("cpc") or []):
                class_rows.append((pid, "CPC", c["code"], None, bool(c.get("first"))))
            for i in (r.get("ipc") or []):
                class_rows.append((pid, "IPC", i["code"], None, False))
            # citations
            for c in (r.get("cites") or []):
                cite_rows.append((r["publication_number"], c["pub"], c.get("category"),
                                  c.get("type") or "unknown", 1))
            # parties (inventors is a core-only staging column)
            for a in (r.get("assignees") or []):
                party_rows.append((pid, "assignee", a["name"], norm_name(a["name"])))
            for iv in (r.get("inventors") or []):
                party_rows.append((pid, "inventor", iv["name"], norm_name(iv["name"])))
            # provenance (lean: one row per pub for full text; BQ text is digital, not OCR)
            prov_rows.append(("publication", pid, "fulltext", source_id,
                              None, None, "digital_text", None))

        # claims need ids for dependencies -> insert with RETURNING preserving order
        if claim_rows:
            crecs = insert_returning(
                cur, "claims",
                ["publication_id", "claim_no", "is_independent", "lang", "text", "resolved_text"],
                claim_rows, returning="id")
            claim_id_map = {}
            dep_rows = []
            for (pid, cno, parents), rec in zip(claim_index, crecs):
                claim_id_map[(pid, cno)] = rec["id"]
            for (pid, cno, parents), rec in zip(claim_index, crecs):
                for p in parents:
                    parent_id = claim_id_map.get((pid, p))
                    if parent_id:
                        dep_rows.append((rec["id"], parent_id))
            executemany(cur, "claim_dependencies", ["claim_id", "depends_on_claim_id"], dep_rows)

        executemany(cur, "paragraphs",
                    ["publication_id", "para_no", "heading", "page_no", "lang", "text"], para_rows)
        executemany(cur, "figures",
                    ["publication_id", "figure_no", "caption", "reference_numbers"], fig_rows)
        executemany(cur, "classifications",
                    ["publication_id", "scheme", "symbol", "version_date", "is_first"], class_rows)
        executemany(cur, "citations",
                    ["src_pub", "dst_pub", "category", "origin", "occurrences"], cite_rows,
                    conflict="ON CONFLICT (src_pub, dst_pub, origin) DO UPDATE SET occurrences=citations.occurrences+1")
        executemany(cur, "parties",
                    ["publication_id", "role", "raw_name", "normalized_name"], party_rows)
        executemany(cur, "field_provenance",
                    ["entity", "entity_id", "field", "source_id", "original_value",
                     "normalized_value", "ocr_status", "ocr_confidence"], prov_rows)
        conn.commit()
        for r in rows:
            seen.add(f"{r['publication_number']}|{r['kind_code'] or ''}")
        done += len(batch)
        print(f"  loaded {done:,}/{total:,}", end="\r", flush=True)

    for row in it:
        batch.append(row)
        if len(batch) >= PAGE:
            flush(batch); batch = []
        if limit and done >= limit:
            break
    if batch:
        flush(batch)
    print(f"\n[load] done tier={tier}: {done:,}")
    cur.close(); conn.close()


def main():
    src_core = db.get_source_id("bigquery:patents-public-data", "core-2026-07")
    src_exp = db.get_source_id("bigquery:patents-public-data", "expanded-2026-07")
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "core"):
        load_table(CORE_TBL, "core", src_core)
    if which in ("all", "expanded"):
        load_table(EXPANDED_TBL, "expanded", src_exp)


if __name__ == "__main__":
    main()
