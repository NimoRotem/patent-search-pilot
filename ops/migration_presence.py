"""Read-only: what does the live database already hold for each pending migration?

Throwaway probe for the integration decision "adopt or apply". It opens the same connection
`src/migrate.py` uses, calls the runner's own `presence()` so the answer is the runner's answer and
not a second opinion, and writes nothing.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import migrate  # noqa: E402

WANT = {"002", "010", "011", "012", "013", "014", "015", "016"}

conn = migrate._connect()
for m in migrate.discover(os.path.join(ROOT, "sql")):
    if m.version not in WANT:
        continue
    state = migrate.presence(conn, m)
    replay = getattr(migrate, "is_replayable", None)
    replayable = replay(m.sql) if replay else "n/a"
    print("%-4s %-34s presence=%-9s replayable=%s" % (m.version, m.filename, state, replayable))
    for kind, name in migrate.sentinels(m.sql):
        print("        %-8s %s" % (kind, name))

#  The 002 question specifically: it is `partial`, and one of its indexes may be impossible rather
#  than merely absent. pgvector's hnsw access method refuses a column wider than 2000 dimensions.
print("\n---- 002: is the missing half of it even creatable? ----")
cur = conn.cursor()
cur.execute("SELECT c.relname AS tbl, a.attname AS col, "
            "       format_type(a.atttypid, a.atttypmod) AS typ "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname LIKE 'bench%' AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY c.relname, a.attnum")
for row in cur.fetchall():
    vals = list(row.values()) if isinstance(row, dict) else list(row)
    print("   %-12s %-12s %s" % tuple(vals[:3]))
cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
row = cur.fetchone()
print("   pgvector:", (list(row.values())[0] if isinstance(row, dict) else row[0]) if row else "?")
