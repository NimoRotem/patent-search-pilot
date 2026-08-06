"""Run every DEV subject that has a usable frozen disclosure list, one at a time, resumably.

Sequential on purpose: the pipeline already fans out internally (18 chart workers, 6 screen
workers) and the box also serves the live site. Resumable because a 30-subject batch is hours
long and losing it to one bad subject would be the expensive kind of failure.
"""
import json, os, subprocess, sys, time
HERE="/home/nimrod_rotem/patent-search-pilot"
os.chdir(HERE)
sys.path.insert(0, HERE+"/src"); sys.path.insert(0, HERE+"/eval")
import disclosures
TAG=os.environ.get("TAG","v15"); SPLIT=os.environ.get("SPLIT","dev")
subs=json.load(open("eval/benchmark_subjects.json"))["subjects"]
todo=[]
for s in subs:
    if s.get("split","dev")!=SPLIT: continue
    if not disclosures.load_frozen(s["id"]):
        print(f"[skip] {s['id']}: no usable frozen disclosure list", flush=True); continue
    if os.path.exists(f"data/reports/bench-{s['id']}-{TAG}.json"):
        print(f"[done] {s['id']}: report already exists", flush=True); continue
    todo.append(s)
print(f"[batch] {len(todo)} subjects to run at tag {TAG}", flush=True)
for i,s in enumerate(todo,1):
    t0=time.time()
    print(f"[batch] {i}/{len(todo)} {s['id']}", flush=True)
    r=subprocess.run([HERE+"/.venv/bin/python", "eval/benchmark.py", "--run",
                      "--tag", TAG, "--only", s["id"]],
                     capture_output=True, text=True, timeout=3600)
    ok=os.path.exists(f"data/reports/bench-{s['id']}-{TAG}.json")
    print(f"[batch] {i}/{len(todo)} {s['id']} {'ok' if ok else 'FAILED'} "
          f"{time.time()-t0:.0f}s rc={r.returncode}", flush=True)
    if not ok:
        print((r.stderr or r.stdout or "")[-600:], flush=True)
print("[batch] complete", flush=True)
