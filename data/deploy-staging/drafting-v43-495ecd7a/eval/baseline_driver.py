#!/usr/bin/env python3
"""Drive one link-input search through the deployed app, exactly as the UI does.

Usage (on instance-3): python3 baseline_driver.py <google-patents-url> [tag]
Prints the slug, then waits for the finished (non-partial) report and prints the funnel line.
Loopback only (AUTH_TRUST_LOOPBACK=1 covers auth). Not part of the app; a driver for baselines.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8631"


def post(path, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(BASE + path, data=data)
    with urllib.request.urlopen(req, timeout=120) as fh:
        body = fh.read().decode()
    try:
        return json.loads(body)
    except Exception:
        return {"_raw": body[:2000]}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as fh:
        body = fh.read().decode()
    try:
        return json.loads(body)
    except Exception:
        return {"_raw": body[:2000]}


def main():
    url = sys.argv[1]
    print(f"[driver] extracting {url}", flush=True)
    j = post("/extract", {"url": url, "async": "1"})
    job = j.get("job")
    if not job:
        print(f"[driver] extract refused: {j}", flush=True)
        sys.exit(1)
    st = {}
    for _ in range(240):
        time.sleep(5)
        st = get(f"/extract/status/{job}")
        if st.get("state") in ("done", "error"):
            break
    if st.get("state") != "done":
        print(f"[driver] extract did not finish: {st}", flush=True)
        sys.exit(1)
    d = st.get("result") or st
    token = d.get("doc_token") or ""
    brief = d.get("brief") or d.get("query") or ""
    claims = d.get("claims") or []
    print(f"[driver] extracted: doc_token={token[:12]}… brief={len(brief)} chars "
          f"claims={len(claims)}", flush=True)
    if not token or not brief:
        print(f"[driver] missing token/brief; keys were {sorted(d.keys())}", flush=True)
        sys.exit(1)

    #  /run answers with a redirect to /report/<slug>; the slug is the path's last segment.
    data = urllib.parse.urlencode({"query": brief, "doc_token": token, "mode": "novelty",
                                   "search_focus": "all_text"}).encode()
    req = urllib.request.Request(BASE + "/run", data=data)
    with urllib.request.urlopen(req, timeout=300) as fh:
        final = fh.geturl()
    slug = final.rstrip("/").rsplit("/", 1)[-1]
    if not slug or "report" in slug:
        print(f"[driver] could not read slug from {final}", flush=True)
        sys.exit(1)
    print(f"SLUG {slug}", flush=True)
    #  Wait for the finished report (partial=false), up to 4 hours.
    import os
    path = os.path.expanduser(f"~/patent-search-pilot/data/reports/{slug}.json")
    for _ in range(480):
        time.sleep(30)
        if os.path.exists(path):
            try:
                rep = json.load(open(path))
            except Exception:
                continue
            if not rep.get("partial"):
                dr = rep.get("deep_rank") or {}
                print(f"DONE {slug} deep_seconds={dr.get('seconds')} "
                      f"llm={json.dumps((dr.get('llm') or {}).get('calls'))}", flush=True)
                return
    print(f"TIMEOUT {slug}", flush=True)


if __name__ == "__main__":
    main()
