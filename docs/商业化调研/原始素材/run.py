#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runner: run a list of anysearch queries with retry, save each to a txt file."""
import subprocess, sys, os, time, json

CLI = r"C:\Users\zjjem\AppData\Local\hermes\skills\openclaw-imports\anysearch\scripts\anysearch_cli.py"
OUT = r"C:\Users\zjjem\.search_tmp\inner_train"
os.makedirs(OUT, exist_ok=True)

queries = json.load(open(r"C:\Users\zjjem\.search_tmp\queries.json", encoding="utf-8"))

env = dict(os.environ)
env["HTTP_PROXY"] = "http://127.0.0.1:33210"
env["HTTPS_PROXY"] = "http://127.0.0.1:33210"

for i, q in enumerate(queries):
    fn = os.path.join(OUT, "%02d.txt" % i)
    if os.path.exists(fn) and os.path.getsize(fn) > 200:
        print("skip", i, q)
        continue
    for attempt in range(4):
        try:
            r = subprocess.run([sys.executable, CLI, "search", q, "--max_results", "10"],
                               capture_output=True, text=True, timeout=90, env=env, encoding="utf-8")
            out = r.stdout or ""
            if "Connection Error" in out or len(out) < 80:
                raise RuntimeError("conn err/short")
            open(fn, "w", encoding="utf-8").write(out)
            print("OK", i, q, len(out))
            break
        except Exception as e:
            print("retry", i, q, repr(e)[:120])
            time.sleep(3)
    else:
        print("FAIL", i, q)
