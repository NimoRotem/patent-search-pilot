"""Catalog-only reads against the live corpus. No table scans except two small exact counts."""
import os
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

ROOT = "/home/nimrod_rotem/v3/O-release"
load_dotenv(os.path.join(ROOT, ".env"))
dsn = (f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
       f"dbname={os.environ['PGDATABASE']} user={os.environ['PGUSER']} "
       f"password={os.environ['PGPASSWORD']}")
c = psycopg.connect(dsn, row_factory=dict_row, autocommit=True,
                    options="-c default_transaction_read_only=on")
cur = c.cursor()
cur.execute("SET statement_timeout = '60s'")

print("== reltuples (catalog only) ==")
cur.execute("""SELECT relname, reltuples::bigint n, pg_total_relation_size(c.oid) tot,
                      pg_indexes_size(c.oid) idx
               FROM pg_class c JOIN pg_namespace ns ON ns.oid=c.relnamespace
               WHERE ns.nspname='public' AND relkind='r' AND relname = ANY(%s) ORDER BY 1""",
            (["chunks", "publications", "classifications", "paragraphs", "claims",
              "chunks_stage_v3", "corpus_ingest_queue", "sources_docstore"],))
for r in cur.fetchall():
    print(f"  {r['relname']:22s} {int(r['n']):>14,}  total {int(r['tot'])/2**30:8.1f} GiB  "
          f"idx {int(r['idx'])/2**30:7.1f} GiB")

print("== exact small counts ==")
for t in ("chunks_stage_v3", "corpus_ingest_queue"):
    cur.execute(f"SELECT count(*) n FROM {t}")
    print("  ", t, f"{int(cur.fetchone()['n']):,}")
cur.execute("SELECT state, count(*) n, COALESCE(sum(request_count),0) req "
            "FROM corpus_ingest_queue GROUP BY 1 ORDER BY 1")
print("  queue by state:", [dict(r) for r in cur.fetchall()])

print("== watermarks ==")
for t in ("publications", "chunks", "paragraphs", "chunks_stage_v3"):
    cur.execute(f"SELECT COALESCE(max(id),0) m FROM {t}")
    print("  ", t, "max id", f"{int(cur.fetchone()['m']):,}")

print("== release tables present on live? ==")
for t in ("corpus_release", "chunks_release", "corpus_release_active", "corpus_fetch_ledger",
          "corpus_niche_definition", "src_publications", "src_family_home"):
    cur.execute("SELECT to_regclass(%s) t", (t,))
    print("  ", t, cur.fetchone()["t"])

print("== stage table shape ==")
cur.execute("""SELECT column_name, data_type FROM information_schema.columns
               WHERE table_name='chunks_stage_v3' ORDER BY ordinal_position""")
print("  ", ", ".join(r["column_name"] for r in cur.fetchall()))
cur.execute("SELECT count(*) n FROM chunks_stage_v3 WHERE embedding IS NOT NULL")
print("   stage rows with an embedding:", f"{int(cur.fetchone()['n']):,}")
cur.execute("SELECT kind, count(*) n FROM chunks_stage_v3 GROUP BY 1 ORDER BY 2 DESC")
print("   stage kinds:", [(r["kind"], int(r["n"])) for r in cur.fetchall()])
c.close()
