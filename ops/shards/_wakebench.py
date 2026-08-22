"""Measure cold -> hot. Prints the milestone timeline the 20 s SHARD_WAKE_TIMEOUT has to live with."""
import sys, time, json
sys.path.insert(0, "src")
from retrieval import shard_backend

DOMAIN = sys.argv[1] if len(sys.argv) > 1 else "B65G"
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 1

b = shard_backend.ShardBackend()
shard = b.shard_for(DOMAIN)

def wait_cold():
    while True:
        info = b.gce.instance(shard.vm, shard.zone, max_age=0.0)
        if (info or {}).get("status", "").upper() == "TERMINATED":
            return
        time.sleep(3)

results = []
for run in range(1, RUNS + 1):
    print(f"--- run {run}: stopping {shard.vm}")
    b.stop(shard)
    wait_cold()
    print("    TERMINATED. state() =", b.state(DOMAIN))
    marks = {}
    t0 = time.time()
    b.gce.start(shard.vm, shard.zone)
    marks["start_api_returned"] = time.time() - t0
    seen_running = seen_agent = None
    while time.time() - t0 < 600:
        info = b.gce.instance(shard.vm, shard.zone, max_age=0.0)
        status = (info or {}).get("status", "").upper()
        if seen_running is None and status == "RUNNING":
            seen_running = time.time() - t0
            marks["instance_RUNNING"] = seen_running
        if status == "RUNNING" and (info or {}).get("ip"):
            h = b.probe(info["ip"], b.agent_port, timeout=1.5)
            if h:
                if seen_agent is None:
                    seen_agent = time.time() - t0
                    marks["agent_answering"] = seen_agent
                    marks["agent_first_state"] = h.get("state")
                if h.get("state") == "hot" and h.get("available"):
                    marks["HOT"] = time.time() - t0
                    marks["prewarm_blocking_ms"] = (h.get("prewarm") or {}).get("blocking_ms")
                    break
        time.sleep(0.5)
    print("   ", json.dumps(marks, default=str))
    results.append(marks)

print()
print("summary: cold -> hot seconds:", [round(m.get("HOT", -1), 1) for m in results])
