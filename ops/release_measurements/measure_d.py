"""Is `simple_family_id` a real family key, or does it have a degenerate value?"""
import os, time
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

ROOT = "/home/nimrod_rotem/v3/F-release"
load_dotenv(os.path.join(ROOT, ".env"))
dsn = (f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} dbname={os.environ['PGDATABASE']} "
       f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']}")
conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
cur = conn.cursor()
cur.execute("SET statement_timeout = '300s'")


def t(sql, params=None, label=""):
    t0 = time.time()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    print(f"-- {label} ({time.time()-t0:.1f}s)")
    return rows


print("== largest simple_family_id groups (index-only scan on ix_pub_simple_family) ==")
for r in t("""SELECT simple_family_id, count(*) n FROM publications
              GROUP BY 1 ORDER BY n DESC LIMIT 15""", label="family sizes"):
    print(f"  {str(r['simple_family_id'])[:40]:42s} {int(r['n']):>10,}")

print("\n== distinct families, exactly ==")
for r in t("SELECT count(DISTINCT simple_family_id) n FROM publications", label="distinct fams"):
    print(" ", r)

print("\n== publications with no classification at all ==")
for r in t("""SELECT count(*) n FROM publications p
              WHERE NOT EXISTS (SELECT 1 FROM classifications c WHERE c.publication_id = p.id)""",
           label="unclassified pubs"):
    print(" ", r)

print("\n== tier mix ==")
for r in t("SELECT tier, count(*) n FROM publications GROUP BY 1", label="tier"):
    print(" ", dict(r))

print("\n== classification presence by tier ==")
for r in t("""SELECT p.tier, (EXISTS (SELECT 1 FROM classifications c WHERE c.publication_id=p.id))
                     AS classified, count(*) n
              FROM publications p GROUP BY 1,2 ORDER BY 1,2""", label="tier x classified"):
    print(" ", dict(r))
conn.close()
