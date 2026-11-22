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

        key = submit_key()
        if key:
            push_ctx(key)
        f = cf.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            r = fn(*args, **kwargs)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:
            f.set_exception(e)
        finally:
            if key:
                pop_ctx()
        return f


# Items: pieces of a test that can be run on their own (fixture cases of
# check_file, TPC queries of the executor tests, dialects of generator_memory).
# Their lines go to contexts "<num>|<item key>".
import contextlib  # noqa: E402
import re  # noqa: E402

item = {"num": None, "meth": None, "fx_file": None, "fx_titles": {}, "k": 0}
ctx_stack = []
EXEC_SUBMIT = ("test_execute_tpch", "test_execute_tpcds")


def push_ctx(key):
    c = f"{item['num']}|{key}"
    ctx_stack.append(c)
    cov.switch_context(c)


def pop_ctx():
    ctx_stack.pop()
    cov.switch_context(ctx_stack[-1] if ctx_stack else str(item["num"]))


def submit_key():
    if item["num"] is None:
        return None
    if item["fx_file"] is not None:
        k = item["k"]
        item["k"] += 1
        return f"F|{item['fx_file']}|{k}"
    if item["meth"] in EXEC_SUBMIT:
        k = item["k"]
        item["k"] += 1
        return f"X|{k}"
    return None


def subtest_key(msg, params):
    if item["num"] is None:
        return None
    if isinstance(msg, str):
        t = msg[len("(execute) ") :] if msg.startswith("(execute) ") else msg
        if item["fx_titles"] and t in item["fx_titles"]:
            return f"F|{item['fx_file']}|{item['fx_titles'][t]}"
        m = re.match(r"tpc-(?:h|ds) (\d+)$", msg)
        if m:
            return f"X|{int(m.group(1)) - 1}"
        if item["meth"] == "test_optimized_tpch":
            m = re.match(r"(\d+), ", msg)
            if m:
                return f"X|{int(m.group(1)) - 1}"
    if item["meth"] == "test_generator_memory" and "dialect" in params:
        d = params["dialect"]
        return f"D|{getattr(d, 'value', d)}"
    return None


_orig_subtest = unittest.TestCase.subTest


@contextlib.contextmanager
def _subtest(self, msg=None, **params):
    key = subtest_key(msg, params)
    with _orig_subtest(self, msg, **params) if msg is not None else _orig_subtest(self, **params) as x:
        if key:
            push_ctx(key)
        try:
            yield x
        finally:
            if key:
                pop_ctx()


unittest.TestCase.subTest = _subtest

if "tests.test_optimizer" in mods:
    _orig_pairs = mods["tests.test_optimizer"].load_sql_fixture_pairs

    def _pairs(filename):
        pairs = list(_orig_pairs(filename))
        titles = {}
        for i, (meta, sql, _) in enumerate(pairs, start=1):
            titles.setdefault(meta.get("title") or f"{i}, {sql}", i - 1)
        item.update(fx_file=filename, fx_titles=titles, k=0)
        return pairs

    mods["tests.test_optimizer"].load_sql_fixture_pairs = _pairs


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
        item.update(num=num, meth=meth, fx_file=None, fx_titles={}, k=0)
        ctx_stack.clear()
        measured(str(num), lambda: cls(meth).run(result))
        item.update(num=None, meth=None, fx_file=None, fx_titles={}, k=0)
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
