"""Merge build_map shards into the compact map the selector ships with.

Run from the sqlglot checkout with PYTHONPATH=. :
    python finalize_map.py <tests.json> <shard_dir> <out.json.gz>

Map contents (all test references are indices into "sets", a table of
deduplicated sorted test-number lists):
  lines[file][line]      tests that executed that line at run time
  opens[file]            tests that opened that file
  users[class qualname]  tests that instantiated the class or a subclass
  funcs                  [file, body_start, body_end, set] per function/lambda
  name_funcs[name]       functions whose code mentions the name
  import_names           names read by code that runs at import time
  import_lines[file]     lines executed while importing everything
  modfiles[module]       test files that import the module at import time
"""

import ast
import glob
import gzip
import importlib
import json
import os
import pkgutil
import sys
from collections import defaultdict

tests_json, shard_dir, out = sys.argv[1:4]
ROOT = os.path.abspath(".")
tests = json.load(open(tests_json))["tests"]
by_class = defaultdict(list)
for t in tests:
    path, cls, _ = t["id"].split("::")
    by_class[f"C:{path[:-3].replace('/', '.')}::{cls}"].append(t["number"])


def ctx_tests(ctx):
    if ctx.startswith("C:"):
        return by_class[ctx[:-5] if ctx.endswith(":down") else ctx]
    return [int(ctx)]


lines = defaultdict(lambda: defaultdict(set))
clines = defaultdict(lambda: defaultdict(set))
opens = defaultdict(set)
cls_tests = defaultdict(set)
failed = []
for f in sorted(glob.glob(os.path.join(shard_dir, "shard*.json"))):
    d = json.load(open(f))
    for file, by in d["lines"].items():
        for ln, ctxs in by.items():
            s = lines[file][int(ln)]
            for c in ctxs:
                s.update(ctx_tests(c))
                if c.startswith("C:"):
                    clines[file][int(ln)].add(c[2:-5] if c.endswith(":down") else c[2:])
    for ctx, fs in d["opens"].items():
        for p in fs:
            opens[p].update(ctx_tests(ctx))
    for ctx, cs in d["classes"].items():
        for c in cs:
            cls_tests[c].update(ctx_tests(ctx))
    failed += [k for k, ok in d["outcomes"].items() if not ok]
print("failing at base:", failed, file=sys.stderr)

# Which test files import which modules (transitively, at import time):
# do it per test file in a fresh interpreter to be exact.
import subprocess  # noqa: E402

test_files = sorted({t["file"] for t in tests})
code = (
    "import sys,importlib,json;b=set(sys.modules);importlib.import_module(sys.argv[1]);"
    "print(json.dumps(sorted(m for m in set(sys.modules)-b if m.startswith(('sqlglot','tests')))))"
)
modfiles = defaultdict(list)
file_imports = {}
for tf in test_files:
    mod = tf[:-3].replace("/", ".")
    r = subprocess.run([sys.executable, "-c", code, mod], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ROOT})
    try:
        ms = json.loads(r.stdout.strip().splitlines()[-1])
        file_imports[mod] = set(ms)
        for m in ms:
            modfiles[m].append(tf)
    except Exception:
        file_imports[mod] = set()
        print("import graph failed", tf, r.stderr[-300:], file=sys.stderr)

# Import-time coverage. Context "S": importing all of sqlglot. Context
# "I:<module>": importing one test module (dependencies imported first, so
# each module's own import-time code lands in its own context).
import coverage  # noqa: E402

cov = coverage.Coverage(data_file=None, source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")])
cov.start()
cov.switch_context("S")
import sqlglot  # noqa: E402

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(m.name)
    except Exception:
        pass
from sqlglot.dialects.dialect import Dialect, Dialects  # noqa: E402

for dname in Dialects:
    try:
        Dialect.get_or_raise(dname.value)
    except Exception:
        pass
test_mods = set()
for mm in file_imports.values():
    test_mods |= {x for x in mm if x.startswith("tests")}
test_mods |= set(file_imports)
done = set()


def imp(mod):
    if mod in done:
        return
    done.add(mod)
    for dep in sorted(file_imports.get(mod, ())):
        if dep.startswith("tests") and dep != mod and dep.count(".") <= mod.count(".") + 1:
            pass
    # import shallower test dependencies first
    for dep in sorted(x for x in file_imports.get(mod, ()) if x.startswith("tests.") and x != mod):
        if dep not in done and dep in test_mods and mod not in file_imports.get(dep, ()):
            imp(dep)
    cov.switch_context("I:" + mod)
    try:
        importlib.import_module(mod)
    except Exception as e:
        print("cannot import", mod, e, file=sys.stderr)


for mod in sorted(test_mods):
    imp(mod)
cov.stop()
data = cov.get_data()
import_lines = {}
tmod_lines = {}
for f in data.measured_files():
    rel = os.path.relpath(f, ROOT).replace(os.sep, "/")
    by = data.contexts_by_lineno(f)
    s_lines = sorted(ln for ln, cs in by.items() if "S" in cs)
    if s_lines:
        import_lines[rel] = s_lines
    t = {}
    for ln, cs in by.items():
        ims = sorted(c[2:] for c in cs if c.startswith("I:"))
        if ims:
            t[str(ln)] = ims
    if t:
        tmod_lines[rel] = t

# Users: tests that instantiated a class whose MRO includes the qualname.
classes_by_name = {}
for m in list(sys.modules.values()):
    name = getattr(m, "__name__", "")
    if not name.startswith(("sqlglot", "tests")):
        continue
    for v in list(vars(m).values()):
        if isinstance(v, type):
            stack = [v]
            while stack:
                c = stack.pop()
                q = f"{c.__module__}.{c.__qualname__}"
                if q in classes_by_name:
                    continue
                classes_by_name[q] = c
                stack += [x for x in vars(c).values() if isinstance(x, type)]
users = defaultdict(set)
for q, ts in cls_tests.items():
    c = classes_by_name.get(q)
    mro = c.__mro__ if c else ()
    for b in mro:
        bq = f"{b.__module__}.{b.__qualname__}"
        if bq.startswith(("sqlglot", "tests")):
            users[bq] |= ts
    users[q] |= ts

# Functions and the names they mention.
funcs = []
name_funcs = defaultdict(set)
import_names = set()


def names_in(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)
    return out


def body_span(fn):
    if isinstance(fn, ast.Lambda):
        return fn.body.lineno, fn.body.end_lineno
    return fn.body[0].lineno, fn.end_lineno


sets_index = {}
sets = []


def sid(s):
    k = tuple(sorted(s))
    if k not in sets_index:
        sets_index[k] = len(sets)
        sets.append(list(k))
    return sets_index[k]


static_classes = defaultdict(set)
static_modules = defaultdict(set)


def static_walk(node, mod, qual, out_mod):
    """Names read by class bodies / module level code (outside functions)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in child.decorator_list + child.args.defaults + child.args.kw_defaults:
                if d is not None:
                    for n in names_in(d):
                        (static_classes[n].add(qual) if qual else out_mod[n].add(mod))
            continue
        if isinstance(child, ast.Lambda):
            continue
        if isinstance(child, ast.ClassDef):
            q = f"{qual}.{child.name}" if qual else f"{mod}.{child.name}"
            for b in child.bases + child.keywords + child.decorator_list:
                for n in names_in(b):
                    (static_classes[n].add(qual) if qual else out_mod[n].add(mod))
            static_walk(child, mod, q, out_mod)
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            (static_classes[child.id].add(qual) if qual else out_mod[child.id].add(mod))
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            (static_classes[child.attr].add(qual) if qual else out_mod[child.attr].add(mod))
        static_walk(child, mod, qual, out_mod)


for path in sorted(glob.glob("sqlglot/**/*.py", recursive=True) + glob.glob("tests/**/*.py", recursive=True)):
    rel = path.replace(os.sep, "/")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    modname = rel[:-3].replace("/", ".")
    if modname.endswith(".__init__"):
        modname = modname[: -len(".__init__")]
    static_walk(tree, modname, None, static_modules)
    imp = set(import_lines.get(rel, ()))
    by = lines.get(rel, {})
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        a, b = body_span(fn)
        ts = set()
        for ln in range(a, b + 1):
            ts |= by.get(ln, set())
        ns = names_in(fn.body if isinstance(fn, ast.Lambda) else ast.Module(body=fn.body, type_ignores=[]))
        if any(ln in imp for ln in range(a, b + 1)):
            import_names.update(ns)
        if not ts:
            continue
        fid = len(funcs)
        funcs.append([rel, a, b, sid(ts)])
        for n in ns:
            name_funcs[n].add(fid)


# Static import graph (module level, TYPE_CHECKING blocks skipped): who imports whom.
def import_deps(rel, m, known):
    tree = ast.parse(open(rel, encoding="utf-8").read())
    out = set()
    pkg = m if rel.endswith("__init__.py") else m.rsplit(".", 1)[0]

    def visit(nodes):
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(n, ast.If) and "TYPE_CHECKING" in ast.dump(n.test):
                visit(n.orelse)
                continue
            if isinstance(n, ast.Import):
                for a in n.names:
                    out.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    parts = pkg.split(".")
                    parts = parts[: len(parts) - n.level + 1]
                    base = ".".join(parts + ([n.module] if n.module else []))
                out.add(base)
                for a in n.names:
                    out.add(base + "." + a.name)
            visit(list(ast.iter_child_nodes(n)))

    visit(tree.body)
    return {d for d in out if d in known and d != m}


known_mods = {}
for path in glob.glob("sqlglot/**/*.py", recursive=True) + glob.glob("tests/**/*.py", recursive=True):
    rel = path.replace(os.sep, "/")
    mm = rel[:-3].replace("/", ".")
    known_mods[mm[:-9] if mm.endswith(".__init__") else mm] = rel
rdeps = defaultdict(set)
for mm, rel in known_mods.items():
    try:
        for d in import_deps(rel, mm, known_mods):
            rdeps[d].add(mm)
    except SyntaxError:
        pass

out_map = {
    "lines": {f: {str(ln): sid(ts) for ln, ts in by.items() if ts} for f, by in lines.items()},
    "clines": {f: {str(ln): sorted(cs) for ln, cs in by.items()} for f, by in clines.items()},
    "static_classes": {n: sorted(v) for n, v in static_classes.items()},
    "static_modules": {n: sorted(v) for n, v in static_modules.items()},
    "opens": {f: sid(ts) for f, ts in opens.items()},
    "users": {q: sid(ts) for q, ts in users.items()},
    "funcs": funcs,
    "name_funcs": {n: sorted(v) for n, v in name_funcs.items()},
    "import_names": sorted(import_names),
    "import_lines": import_lines,
    "tmod_lines": tmod_lines,
    "modfiles": modfiles,
    "rdeps": {k: sorted(v) for k, v in rdeps.items()},
    "sets": sets,
}
with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
    json.dump(out_map, f, separators=(",", ":"))
print("sets", len(sets), "funcs", len(funcs), "size", os.path.getsize(out), file=sys.stderr)
