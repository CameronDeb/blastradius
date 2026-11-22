"""Generate synthetic single-edit patches against the pinned sqlglot, in the
style of the sample set, for local evaluation of the selector.

    python mutate.py <sqlglot_dir> <out_dir> <count> [seed]
"""

import difflib
import glob
import os
import random
import re
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)

py_files = [p for p in glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True) if "__main__" not in p]
fixtures = glob.glob(os.path.join(src, "tests", "fixtures", "**", "*.sql"), recursive=True)
weights = [os.path.getsize(p) for p in py_files]

SWAPS = [
    (r" and ", " or "),
    (r" or ", " and "),
    (r" == ", " != "),
    (r" != ", " == "),
    (r" is not ", " is "),
    (r" is None", " == None"),
    (r" < ", " <= "),
    (r" > ", " >= "),
    (r" <= ", " < "),
    (r" >= ", " > "),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\bnot ", ""),
    (r"\bif not ", "if "),
    (r"copy=False", "copy=True"),
    (r"\+ 1\b", "- 1"),
    (r"\[0\]", "[1]"),
    (r"\[-1\]", "[0]"),
]
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Keep in sync with the generator", "# Intentionally not cached"]


def diff(rel, a, b):
    d = difflib.unified_diff(a.splitlines(True), b.splitlines(True), "a/" + rel, "b/" + rel, n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "".join(d)


def code_mutant():
    path = rng.choices(py_files, weights)[0]
    lines = open(path, encoding="utf-8").read().split("\n")
    for _ in range(200):
        i = rng.randrange(len(lines))
        ln = lines[i]
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith('"""') or s.startswith("import") or s.startswith("from "):
            continue
        opts = [(p, r) for p, r in SWAPS if re.search(p, ln)]
        nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", ln))
        if nums and rng.random() < 0.3:
            m = rng.choice(nums)
            new = ln[: m.start()] + str(int(m.group(1)) + 1) + ln[m.end() :]
        elif opts:
            p, r = rng.choice(opts)
            ms = list(re.finditer(p, ln))
            m = rng.choice(ms)
            new = ln[: m.start()] + m.expand(r) + ln[m.end() :]
        else:
            continue
        if new == ln:
            continue
        b = lines[:i] + [new] + lines[i + 1 :]
        return path, "code", "\n".join(lines), "\n".join(b)
    return None


def comment_mutant():
    path = rng.choices(py_files, weights)[0]
    lines = open(path, encoding="utf-8").read().split("\n")
    for _ in range(100):
        i = rng.randrange(1, len(lines))
        nxt = lines[i]
        if not nxt.strip():
            continue
        ind = re.match(r"\s*", nxt).group(0)
        b = lines[:i] + [ind + rng.choice(COMMENTS)] + lines[i:]
        return path, "comment", "\n".join(lines), "\n".join(b)
    return None


def fixture_mutant():
    path = rng.choice(fixtures)
    lines = open(path, encoding="utf-8").read().split("\n")
    for _ in range(100):
        i = rng.randrange(len(lines))
        ln = lines[i]
        if not ln.strip() or ln.startswith("#") or ln.startswith("--"):
            continue
        toks = ln.split(" ")
        j = rng.randrange(len(toks))
        r = rng.random()
        if r < 0.4:
            toks.insert(j, rng.choice(["x", "1", "42", "AND"]))
        elif r < 0.7 and len(toks) > 1:
            del toks[j]
        else:
            toks[j] = toks[j] + "1"
        b = lines[:i] + [" ".join(toks)] + lines[i + 1 :]
        return path, "fixture", "\n".join(lines), "\n".join(b)
    return None


made = 0
kinds = {}
while made < count:
    r = rng.random()
    if r < 0.12:
        parts = [x for x in (code_mutant(), comment_mutant(), fixture_mutant()) if x]
        seen = set()
        text = ""
        for path, _, a, b in parts:
            if path not in seen:
                seen.add(path)
                text += diff(os.path.relpath(path, src).replace(os.sep, "/"), a, b)
        kind = "multi"
    else:
        m = code_mutant() if r < 0.62 else (fixture_mutant() if r < 0.85 else comment_mutant())
        if not m:
            continue
        path, kind, a, b = m
        text = diff(os.path.relpath(path, src).replace(os.sep, "/"), a, b)
    cid = f"m{made:03d}"
    with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    kinds[cid] = kind
    made += 1
import json

json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"))
print("made", made)
