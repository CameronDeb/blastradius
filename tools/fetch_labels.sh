#!/bin/sh
# Merge lab/<set>-* branches into mut/<set>/answers.json
set -e
SET=$1
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
git fetch -q origin "+refs/heads/lab/$SET-*:refs/remotes/origin/lab/$SET-*"
python -I - "$SET" <<'PY'
import json, subprocess, sys
s = sys.argv[1]
refs = subprocess.run(["git", "for-each-ref", "--format=%(refname)", f"refs/remotes/origin/lab/{s}-*"], capture_output=True, text=True).stdout.split()
out = {}
for r in refs:
    name = r.rsplit("/", 1)[1]
    try:
        d = json.loads(subprocess.run(["git", "show", f"{r}:{name}.json"], capture_output=True, text=True).stdout)
        out.update(d["commits"])
    except Exception as e:
        print("bad", r, e)
json.dump({"commits": out}, open(f"mut/{s}/answers.json", "w"))
print(len(refs), "shards", len(out), "labels")
PY
