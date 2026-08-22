"""Bounded distribution measurements. Sampling and one GROUP BY that was already measured at 7.9s."""
import os, sys, json, time
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))
dsn = (f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} dbname={os.environ['PGDATABASE']} "
       f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']}")
conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
cur = conn.cursor()
cur.execute("SET statement_timeout = '180s'")


def t(sql, params=None, label=""):
    t0 = time.time()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    print(f"-- {label}  ({time.time()-t0:.1f}s)")
    return rows


print("== pg_stats family cardinality ==")
for r in t("SELECT attname, n_distinct, null_frac FROM pg_stats WHERE tablename='publications' "
           "AND attname IN ('simple_family_id','extended_family_id','tier','country')", label="pg_stats"):
    print(dict(r))

print("\n== chunk text length, TABLESAMPLE SYSTEM(0.05) ==")
for r in t("""SELECT count(*) n, avg(length(text))::int avg_chars, sum(length(text)) tot_chars,
                     percentile_disc(0.5) WITHIN GROUP (ORDER BY length(text)) med
              FROM chunks TABLESAMPLE SYSTEM (0.05)""", label="chunk text sample"):
    print(dict(r))

print("\n== chunk kind mix, TABLESAMPLE SYSTEM(0.05) ==")
for r in t("""SELECT kind, count(*) n, avg(length(text))::int avg_chars
              FROM chunks TABLESAMPLE SYSTEM (0.05) GROUP BY kind ORDER BY n DESC""",
           label="chunk kinds"):
    print(dict(r))

print("\n== classification rows per CPC subclass (top 40) ==")
rows = t("SELECT substr(symbol,1,4) d, count(*) n FROM classifications WHERE symbol IS NOT NULL "
         "GROUP BY 1 ORDER BY n DESC", label="domain histogram")
tot = sum(int(r["n"]) for r in rows)
print(f"distinct subclasses={len(rows)} total_rows={tot}")
for r in rows[:40]:
    print(f"  {r['d']:6s} {int(r['n']):>12,}  {100*int(r['n'])/tot:5.2f}%")
json.dump([{"d": r["d"], "n": int(r["n"])} for r in rows],
          open(os.path.join(ROOT, "data/logs/domain_hist.json"), "w"))
conn.close()
