"""Catalog-only sizing measurements. No table scans."""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))

dsn = (f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} dbname={os.environ['PGDATABASE']} "
       f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']}")
conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
cur = conn.cursor()

def q(sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()

print("== relation sizes ==")
for r in q("""
    SELECT c.relname, c.reltuples::bigint AS est_rows,
           pg_relation_size(c.oid) AS heap_bytes,
           pg_total_relation_size(c.oid) AS total_bytes,
           pg_indexes_size(c.oid) AS idx_bytes,
           pg_relation_size(c.reltoastrelid) AS toast_bytes
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='public' AND c.relkind='r'
       AND c.relname IN ('chunks','publications','classifications','paragraphs','claims',
                         'citations','chunks_stage_v3')
     ORDER BY pg_total_relation_size(c.oid) DESC"""):
    print(json.dumps({k: (int(v) if v is not None else None) if k != 'relname' else v
                      for k, v in r.items()}))

print("\n== index sizes ==")
for r in q("""
    SELECT i.relname AS idx, t.relname AS tbl, pg_relation_size(i.oid) AS bytes
      FROM pg_class i JOIN pg_index x ON x.indexrelid=i.oid
      JOIN pg_class t ON t.oid=x.indrelid
      JOIN pg_namespace n ON n.oid=i.relnamespace
     WHERE n.nspname='public' AND t.relname IN ('chunks','chunks_stage_v3','publications','classifications','paragraphs')
     ORDER BY pg_relation_size(i.oid) DESC"""):
    print(json.dumps({"idx": r["idx"], "tbl": r["tbl"], "bytes": int(r["bytes"])}))

print("\n== reltuples ==")
for r in q("""SELECT relname, reltuples::bigint n FROM pg_class c JOIN pg_namespace ns ON ns.oid=c.relnamespace
              WHERE ns.nspname='public' AND relkind='r' AND relname IN
              ('chunks','publications','classifications','paragraphs','claims','chunks_stage_v3','corpus_ingest_queue','sources_docstore')"""):
    print(r["relname"], int(r["n"]))

print("\n== settings ==")
for r in q("SELECT name, setting, unit FROM pg_settings WHERE name IN "
           "('shared_buffers','maintenance_work_mem','work_mem','effective_cache_size','max_parallel_maintenance_workers','server_version')"):
    print(r["name"], r["setting"], r["unit"])
conn.close()
