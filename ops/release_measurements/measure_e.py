"""How big is the NICHE (the hot corpus), at seed-subgroup granularity?"""
import os, time
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

ROOT = "/home/nimrod_rotem/v3/F-release"
load_dotenv(os.path.join(ROOT, ".env"))
import sys
sys.path.insert(0, os.path.join(ROOT, "src"))
from config import SEED_CPC  # noqa: E402

dsn = (f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} dbname={os.environ['PGDATABASE']} "
       f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']}")
conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
cur = conn.cursor()
cur.execute("SET statement_timeout = '300s'")

cur.execute("SELECT datcollate, datctype FROM pg_database WHERE datname = current_database()")
print("collation:", dict(cur.fetchone()))

pats = [c.replace("/", "").replace(" ", "") + "%" for c in SEED_CPC]
pats2 = [c + "%" for c in SEED_CPC]
print("seed patterns:", pats2)

cur.execute("EXPLAIN SELECT publication_id FROM classifications WHERE symbol LIKE ANY(%s)", (pats2,))
print("EXPLAIN:", " | ".join(r["QUERY PLAN"] for r in cur.fetchall()))

t0 = time.time()
cur.execute("""SELECT count(*) rows, count(DISTINCT publication_id) pubs
               FROM classifications WHERE replace(symbol,' ','') LIKE ANY(%s)""", (pats2,))
r = cur.fetchone()
print(f"seed-subgroup classification rows={int(r['rows']):,} publications={int(r['pubs']):,} "
      f"({time.time()-t0:.1f}s)")

t0 = time.time()
cur.execute("""SELECT count(*) n FROM chunks c WHERE c.publication_id IN (
                 SELECT publication_id FROM classifications
                 WHERE replace(symbol,' ','') LIKE ANY(%s))""", (pats2,))
print(f"seed-subgroup chunks={int(cur.fetchone()['n']):,} ({time.time()-t0:.1f}s)")

t0 = time.time()
cur.execute("""SELECT count(DISTINCT CASE WHEN simple_family_id IN ('', '-1') THEN publication_number
                                          ELSE simple_family_id END) n
               FROM publications p WHERE EXISTS (
                 SELECT 1 FROM classifications cl WHERE cl.publication_id = p.id
                   AND replace(cl.symbol,' ','') LIKE ANY(%s))""", (pats2,))
print(f"seed-subgroup families={int(cur.fetchone()['n']):,} ({time.time()-t0:.1f}s)")
conn.close()
