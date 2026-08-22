"""Prove the loop: manager -> connection -> the retrieval SQL -> release. Throwaway smoke test."""
import sys, time
sys.path.insert(0, "src")
from retrieval import shard_backend, shard_manager

shard_backend.install()
print("shard_manager.available() =", shard_manager.available())
t0 = time.time()
states = shard_manager.ensure(["B65G"], timeout=20)
print("ensure ->", states, "in %.2fs" % (time.time() - t0))

conn = shard_manager.connection("B65G")
print("connection ->", conn)
if conn is None:
    sys.exit("no connection")

qvec = "[" + ",".join(["0.01"] * 768) + "]"
sql = ("SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score "
       "FROM chunks c JOIN publications p ON p.id=c.publication_id "
       "WHERE c.embedding IS NOT NULL "
       "ORDER BY c.embedding <=> %s::vector LIMIT %s")
with conn.cursor() as cur:
    t0 = time.time()
    cur.execute(sql, (qvec, qvec, 10))
    rows = cur.fetchall()
    print("dense channel SQL over the shard: %d rows in %.3fs" % (len(rows), time.time() - t0))
    cur.execute("SELECT count(*) n FROM chunks")
    print("chunks on the shard:", cur.fetchone()["n"])
    cur.execute("SELECT current_setting('server_version'), current_setting('default_transaction_read_only')")
    print("server:", cur.fetchone())
    cur.execute("SELECT count(*) n FROM information_schema.tables WHERE table_schema='public'")
    print("public tables:", cur.fetchone()["n"])
    try:
        cur.execute("CREATE TABLE should_not_work (x int)")
        print("WRITE SUCCEEDED, which is a bug")
    except Exception as e:
        print("write refused, as it must:", str(e).splitlines()[0])
shard_manager.release("B65G", conn)
again = shard_manager.connection("B65G")
print("pooled connection reused:", again is conn)
shard_manager.release("B65G", again)
