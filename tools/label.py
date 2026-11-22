"""Label synthetic patches by running sqlglot's tests on each (pytest, like the
organisers). Writes <dir>/answers.json and copies tests.json next to it.

    python label.py <sqlglot_dir> <venv_python> <tests.json> <mut_dir> <workers> [--skip-giants]
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from queue import Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from selector import apply_hunks, parse_patch  # noqa: E402

src, py, tests_json, mdir, nworkers = sys.argv[1:6]
skip_giants = "--skip-giants" in sys.argv
shard = next((a.split("=")[1] for a in sys.argv if a.startswith("--shard=")), "0/1")
shard_i, shard_n = map(int, shard.split("/"))
nworkers = int(nworkers)
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
GIANTS = [
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
]
if os.path.abspath(tests_json) != os.path.abspath(os.path.join(mdir, "tests.json")):
    shutil.copy(tests_json, os.path.join(mdir, "tests.json"))
ans_path = os.environ.get("LABEL_OUT") or os.path.join(mdir, "answers.json")
answers = json.load(open(ans_path))["commits"] if os.path.exists(ans_path) else {}
kinds = json.load(open(os.path.join(mdir, "kinds.json")))
todo = Queue()
for k, f in enumerate(sorted(os.listdir(os.path.join(mdir, "commits")))):
    cid = f[:-6]
    if k % shard_n != shard_i:
        continue
    if cid not in answers:
        todo.put(cid)
lock = threading.Lock()
work_root = os.path.join(os.path.dirname(src.rstrip("/\\")), "label-work")


def parse_fail(out):
    fails = set()
    for ln in out.splitlines():
        m = re.match(r"^(FAILED|ERROR|SUBFAILED\S*)\s+(tests/\S+)", ln)
        if not m:
            continue
        nid = m.group(2)
        nid = re.sub(r"\[.*$", "", nid)
        nid = nid.split(" - ")[0]
        if nid in num:
            fails.add(num[nid])
        elif nid.endswith(".py") or "::" not in nid:
            fails.update(by_file.get(nid.split("::")[0], []))
        else:
            # class-level error
            for tid, n in num.items():
                if tid.startswith(nid + "::"):
                    fails.add(n)
    return fails


def worker(i):
    wd = os.path.join(work_root, f"w{i}")
    if not os.path.exists(wd):
        shutil.copytree(src, wd, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    env = {**os.environ, "PYTHONPATH": wd, "SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    while not todo.empty():
        try:
            cid = todo.get_nowait()
        except Exception:
            return
        text = open(os.path.join(mdir, "commits", cid + ".patch"), encoding="utf-8").read()
        saved = {}
        try:
            for old, new, hunks in parse_patch(text):
                rel = new or old
                p = os.path.join(wd, rel)
                orig = open(p, encoding="utf-8", newline="").read()
                saved[p] = orig
                nt, _, _ = apply_hunks(orig, hunks)
                open(p, "w", encoding="utf-8", newline="").write(nt)
            args = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--timeout", "120", "-rfE", "tests"]
            if skip_giants:
                for g in GIANTS:
                    args += ["--deselect", g]
            r = subprocess.run(args, cwd=wd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            fails = parse_fail(r.stdout)
        finally:
            for p, t in saved.items():
                open(p, "w", encoding="utf-8", newline="").write(t)
        with lock:
            answers[cid] = {"failing": sorted(fails), "kind": kinds.get(cid, "?")}
            json.dump({"commits": answers}, open(ans_path, "w"))
            print(cid, kinds.get(cid), len(fails), flush=True)


ths = [threading.Thread(target=worker, args=(i,)) for i in range(nworkers)]
for t in ths:
    t.start()
for t in ths:
    t.join()
