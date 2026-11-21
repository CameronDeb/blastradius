"""Record, for every sqlglot test at the pinned commit, what it touches.

Run from a sqlglot checkout (cwd) with PYTHONPATH=. :
    python build_map.py <tests.json> <out.json> <shard> <nshards>

Per test it records:
  * lines executed in sqlglot/ and tests/ (coverage dynamic contexts),
    with import-time code excluded by importing everything first;
  * class-fixture (setUpClass) lines, attributed to every test of the class;
  * files opened under tests/ (fixtures);
  * sqlglot classes instantiated (Expr, Dialect, Tokenizer, Parser, Generator).
"""

import builtins
import importlib
import io
import json
import os
import pickle
import pkgutil
import sys
import unittest
from collections import defaultdict

os.environ.setdefault("SKIP_INTEGRATION", "0")
if __name__ != "__main__":
    raise SystemExit

tests_json, out_path, shard, nshards = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
ROOT = os.path.abspath(".")
tests = json.load(open(tests_json))["tests"]

# Group tests by (module, class); shards own whole classes.
groups = defaultdict(list)
for t in tests:
    path, cls, meth = t["id"].split("::")
    mod = path[:-3].replace("/", ".")
    groups[(mod, cls)].append((t["number"], meth))
# Balance shards on recorded seconds; a class may be split across shards
# (each shard runs its own setUpClass).
loads = [0.0] * nshards
assign = {}
for t in sorted(tests, key=lambda t: -t["seconds"]):
    i = loads.index(min(loads))
    loads[i] += t["seconds"] + 0.01
    assign[t["number"]] = i
for k in groups:
    groups[k] = [(n, m) for n, m in groups[k] if assign[n] == shard]
mine = [k for k in sorted(groups) if groups[k]]

# Import every sqlglot module, every dialect and the test modules before measuring.
import sqlglot  # noqa: E402

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(m.name)
    except Exception as e:  # pragma: no cover
        print("import fail", m.name, e, file=sys.stderr)
from sqlglot.dialects.dialect import Dialect, Dialects  # noqa: E402

for d in Dialects:
    try:
        Dialect.get_or_raise(d.value)
    except Exception:
        pass
mods = {}
for mod, _ in mine:
    if mod not in mods:
        mods[mod] = importlib.import_module(mod)


class InlineExecutor:
    """Stand-in for ProcessPoolExecutor so coverage sees the worker code.
    Arguments and results are pickled to keep the serialisation path."""

    def __init__(self, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def shutdown(self, *a, **k):
        pass

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf

        f = cf.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            r = fn(*args, **kwargs)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:
            f.set_exception(e)
        return f


for _m in ("tests.test_executor", "tests.test_optimizer"):
    if _m in mods:
        mods[_m].ProcessPoolExecutor = InlineExecutor

# Instrumentation: opened files and instantiated classes.
current = {"opens": None, "classes": None}
_open = builtins.open


def rec_open(file, *a, **k):
    s = current["opens"]
    if s is not None and isinstance(file, (str, os.PathLike)):
        p = os.path.abspath(os.fspath(file))
        if p.startswith(ROOT):
            s.add(os.path.relpath(p, ROOT).replace(os.sep, "/"))
    return _open(file, *a, **k)


builtins.open = rec_open
io.open = rec_open

from sqlglot.expressions.core import Expr  # noqa: E402
from sqlglot.generator import Generator  # noqa: E402
from sqlglot.parser import Parser  # noqa: E402
from sqlglot.tokens import Tokenizer  # noqa: E402


def wrap_init(cls):
    orig = cls.__init__

    def __init__(self, *a, **k):
        s = current["classes"]
        if s is not None:
            s.add(type(self))
        return orig(self, *a, **k)

    cls.__init__ = __init__


for c in (Expr, Dialect, Tokenizer, Parser, Generator):
    wrap_init(c)


def qual(c):
    return f"{c.__module__}.{c.__qualname__}"


import coverage  # noqa: E402

cov = coverage.Coverage(
    data_file=out_path + ".cov",
    source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")],
    branch=False,
)
cov.start()

ctx_opens = {}
ctx_classes = {}
outcomes = {}


def measured(ctx, fn):
    cov.switch_context(ctx)
    current["opens"], current["classes"] = set(), set()
    try:
        return fn()
    finally:
        ctx_opens[ctx] = sorted(current["opens"])
        ctx_classes[ctx] = sorted({qual(c) for c in current["classes"]})
        current["opens"] = current["classes"] = None


for mod, clsname in mine:
    cls = getattr(mods[mod], clsname)
    cctx = f"C:{mod}::{clsname}"
    try:
        measured(cctx, cls.setUpClass)
    except Exception as e:
        print("setUpClass failed", cctx, e, file=sys.stderr)
    for num, meth in groups[(mod, clsname)]:
        result = unittest.TestResult()
        measured(str(num), lambda: cls(meth).run(result))
        ok = not (result.failures or result.errors)
        outcomes[num] = ok
        if not ok:
            print("FAILED", num, meth, (result.failures + result.errors)[0][1][-300:], file=sys.stderr)
    try:
        measured(cctx + ":down", cls.tearDownClass)
    except Exception:
        pass

cov.stop()
cov.save()
data = cov.get_data()
lines = {}
for f in data.measured_files():
    rel = os.path.relpath(f, ROOT).replace(os.sep, "/")
    by = data.contexts_by_lineno(f)
    d = {}
    for ln, ctxs in by.items():
        ctxs = [c for c in ctxs if c]
        if ctxs:
            d[ln] = ctxs
    if d:
        lines[rel] = d
json.dump(
    {"lines": lines, "opens": ctx_opens, "classes": ctx_classes, "outcomes": outcomes},
    open(out_path, "w"),
)
print("done shard", shard, len(mine), "classes", file=sys.stderr)
