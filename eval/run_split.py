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
def main():
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
        #  KEEP THE OUTPUT. Discarding it on success hid a real failure: the first run produced a
        #  report and a trace but NO VIEW, because _build_view_cached raised inside a caught except
        #  and its traceback went to a pipe nobody read. A run that "succeeded" is exactly when a
        #  swallowed error is most expensive, because nothing prompts anyone to look for it.
        logdir=os.path.join(HERE,"data","logs","runs"); os.makedirs(logdir, exist_ok=True)
        with open(os.path.join(logdir, f"{s['id']}-{TAG}.log"), "w") as fh:
            r=subprocess.run([HERE+"/.venv/bin/python", "eval/benchmark.py", "--run",
                              "--tag", TAG, "--only", s["id"]],
                             stdout=fh, stderr=subprocess.STDOUT, text=True, timeout=3600)
        ok=os.path.exists(f"data/reports/bench-{s['id']}-{TAG}.json")
        view_ok=os.path.exists(f"data/reports/bench-{s['id']}-{TAG}.view.json")
        print(f"[batch] {i}/{len(todo)} {s['id']} {'ok' if ok else 'FAILED'}"
              f"{'' if view_ok else ' NO-VIEW'} {time.time()-t0:.0f}s rc={r.returncode}", flush=True)
    print("[batch] complete", flush=True)


#  See eval/run_one_oracle.py: module-level work plus multiprocessing "spawn" equals recursion.
if __name__ == "__main__":
    main()
