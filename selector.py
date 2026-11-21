"""Blast Radius selector.

For each patch: work out which tests could see the change (coverage map built
at the pinned commit), run those against the patched code, and report only
the ones that fail. Tests too slow to be worth running are reported without
running when they could be affected.
"""

import ast
import gzip
import json
import os
import re
import sys
import time
import types

DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.json.gz"))
WORKER_COST = 2000
MAX_WORKERS = 8
# Tiers by recorded seconds. A: always run. B: run only when every A test
# passed, otherwise reported as is. C: never run; reported only when some A/B
# test failed or there was nothing smaller to run.
TIER_B = float(os.environ.get("BR_TIER_B", "0.3"))
TIER_C = float(os.environ.get("BR_TIER_C", "3.0"))


def tier(sec):
    return 0 if sec < TIER_B else (1 if sec < TIER_C else 2)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------- patches


def parse_patch(text):
    """Return [(old_path, new_path, hunks)], hunks = [(old_start, [lines])]."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = ln[4:].split("\t")[0].strip()
            new = lines[i + 1][4:].split("\t")[0].strip()
            old = None if old == "/dev/null" else re.sub(r"^a/", "", old)
            new = None if new == "/dev/null" else re.sub(r"^b/", "", new)
            cur = (old, new, [])
            files.append(cur)
            i += 2
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
        if m and cur is not None:
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                b = lines[i]
                if b.startswith("\\"):
                    body.append(b)
                elif b.startswith("+"):
                    body.append(b)
                    n += 1
                elif b.startswith("-"):
                    body.append(b)
                    o += 1
                else:
                    body.append(" " + b[1:] if b else " ")
                    o += 1
                    n += 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            cur[2].append((int(m.group(1)), body))
            continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    """Apply hunks; return (new_text, touched_old_lines, insert_points)."""
    src = old_text.split("\n") if old_text is not None else []
    out = []
    pos = 0  # index into src
    touched = set()
    inserts = set()
    no_eol_new = None
    for start, body in hunks:
        exp_old = [b[1:] for b in body if b[:1] in (" ", "-")]
        want = max(start - 1, 0)
        at = want
        if src[want : want + len(exp_old)] != exp_old:
            at = None
            for d in range(1, 200):
                for c in (want - d, want + d):
                    if 0 <= c and src[c : c + len(exp_old)] == exp_old:
                        at = c
                        break
                if at is not None:
                    break
            if at is None:
                raise ValueError("hunk does not apply")
        out.extend(src[pos:at])
        o = at
        prev = None
        for b in body:
            tag = b[:1]
            if tag == " ":
                out.append(b[1:])
                o += 1
            elif tag == "-":
                touched.add(o + 1)
                o += 1
            elif tag == "+":
                out.append(b[1:])
                inserts.add(o + 1)
            elif tag == "\\":
                no_eol_new = prev != "-"
            prev = tag
        pos = o
    out.extend(src[pos:])
    return "\n".join(out), touched, inserts


# ------------------------------------------------------------------------- map


class Map:
    def __init__(self, path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            m = json.load(f)
        self.sets = m["sets"]
        self.lines = m["lines"]
        self.clines = m["clines"]
        self.opens = m["opens"]
        self.users = m["users"]
        self.funcs = m["funcs"]
        self.name_funcs = m["name_funcs"]
        self.import_names = set(m["import_names"])
        self.import_lines = m["import_lines"]
        self.static_classes = m["static_classes"]
        self.static_modules = m["static_modules"]
        self.modfiles = m["modfiles"]

    def line_tests(self, f, lns):
        by = self.lines.get(f)
        out = set()
        if by:
            for ln in lns:
                s = by.get(str(ln))
                if s is not None:
                    out.update(self.sets[s])
        return out

    def line_classes(self, f, lns):
        by = self.clines.get(f)
        out = set()
        if by:
            for ln in lns:
                out.update(by.get(str(ln), ()))
        return out

    def users_of(self, qual):
        s = self.users.get(qual)
        return set(self.sets[s]) if s is not None else set()

    def name_tests(self, name, also=None):
        out = set()
        fids = self.name_funcs.get(name, ())
        if also is not None:
            fids = set(fids) & set(self.name_funcs.get(also, ()))
        for fid in fids:
            out.update(self.sets[self.funcs[fid][3]])
        return out

    def file_tests(self, f):
        out = set()
        for s in self.lines.get(f, {}).values():
            out.update(self.sets[s])
        return out


# -------------------------------------------------------------------- analysis


def modname_of(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def chain_at(tree, line):
    """Nodes from the module down whose span contains `line`."""
    chain = [tree]
    node = tree
    while True:
        nxt = None
        for c in ast.iter_child_nodes(node):
            a = getattr(c, "lineno", None)
            if a is None:
                if isinstance(c, (ast.arguments, ast.comprehension, ast.match_case, ast.withitem, ast.keyword)):
                    # these have no own span in some versions; descend if a child matches
                    for cc in ast.walk(c):
                        if getattr(cc, "lineno", 0) <= line <= getattr(cc, "end_lineno", -1):
                            nxt = c
                            break
                continue
            b = getattr(c, "end_lineno", a)
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and c.decorator_list:
                a = min(a, min(d.lineno for d in c.decorator_list))
            if a <= line <= b:
                nxt = c
                break
        if nxt is None:
            return chain
        chain.append(nxt)
        node = nxt


FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)


def header_end(stmt):
    if isinstance(stmt, COMPOUND + FUNC + (ast.ClassDef,)):
        body = getattr(stmt, "body", None) or getattr(stmt, "cases", None)
        if body:
            return body[0].lineno - 1
    return stmt.end_lineno


def in_body(fn, line):
    if isinstance(fn, ast.Lambda):
        return fn.body.lineno <= line <= fn.body.end_lineno
    return fn.body[0].lineno <= line <= fn.end_lineno


def assigned_names(stmt):
    out = set()
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            out.add((a.asname or a.name).split(".")[0])
    elif isinstance(stmt, FUNC + (ast.ClassDef,)):
        out.add(stmt.name)
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
    return out


class Analysis:
    def __init__(self, mp):
        self.mp = mp

    def class_users(self, qual, tree_cls=None, rel=None):
        out = self.mp.users_of(qual)
        if tree_cls is not None:
            out |= self.mp.line_tests(rel, range(tree_cls.lineno, tree_cls.end_lineno + 1))
        return out

    def module_users(self, rel, tree):
        mod = modname_of(rel)
        out = self.mp.file_tests(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                pass
        for q, s in self.mp.users.items():
            if q.startswith(mod + ".") and "." not in q[len(mod) + 1 :].split(".")[0:1][0]:
                out |= set(self.mp.sets[s])
        return out

    def name_users(self, names, mod, depth=0):
        """Tests that could read module-level names defined in `mod`."""
        out = set()
        for n in names:
            out |= self.mp.name_tests(n)
            for q in self.mp.static_classes.get(n, ()):
                out |= self.mp.users_of(q)
            if depth < 2:
                for m in self.mp.static_modules.get(n, ()):
                    if m != mod:
                        out |= self.module_users_by_name(m)
        return out

    def module_users_by_name(self, mod):
        rel = mod.replace(".", "/") + ".py"
        out = self.mp.file_tests(rel) | self.mp.file_tests(mod.replace(".", "/") + "/__init__.py")
        pre = mod + "."
        for q, s in self.mp.users.items():
            if q.startswith(pre):
                out |= set(self.mp.sets[s])
        return out

    def py_change(self, rel, old, new, touched, inserts):
        """Return (candidate tests, class contexts that must re-run setup, flags)."""
        mp = self.mp
        flags = set()
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            return None, set(), {"broken"}
        if old is None:
            return set(), set(), flags  # new module: nothing imports it at the pinned commit
        old_tree = ast.parse(old)
        if ast.dump(old_tree) == ast.dump(new_tree):
            return set(), set(), {"same"}
        mod = modname_of(rel)
        cand = set()
        lines = set(touched)
        for p in inserts:
            lines.add(p)
            if p > 1:
                lines.add(p - 1)
        nlines = old.count("\n") + 1
        lines = {min(max(1, ln), nlines) for ln in lines}
        rerun_classes = set()
        for ln in sorted(lines):
            chain = chain_at(old_tree, ln)
            fn_idx = None
            for i in range(len(chain) - 1, 0, -1):
                c = chain[i]
                if isinstance(c, FUNC + (ast.Lambda,)) and in_body(c, ln):
                    fn_idx = i
                    break
            stmts = [c for c in chain[1:] if isinstance(c, ast.stmt)]
            if fn_idx is not None:
                below = [c for c in chain[fn_idx + 1 :] if isinstance(c, ast.stmt)]
                fn = chain[fn_idx]
                if below:
                    s = below[-1]
                    span = set(range(s.lineno, header_end(s) + 1)) | {ln}
                elif isinstance(fn, ast.Lambda):
                    span = set(range(fn.body.lineno, fn.body.end_lineno + 1))
                else:
                    span = {ln}
                if isinstance(fn, ast.Lambda) or not below:
                    # lambdas: any line of the body; plus header of the enclosing stmt
                    pass
                t = mp.line_tests(rel, span)
                cand |= t
                rerun_classes |= mp.line_classes(rel, span)
                continue
            # Not inside a function body: import-time code.
            flags.add("static")
            s = stmts[-1] if stmts else None
            if s is None:
                continue
            classes = [c for c in chain[1:] if isinstance(c, ast.ClassDef)]
            if isinstance(s, FUNC):
                # decorator / signature / defaults of a def
                body = range(s.body[0].lineno, s.end_lineno + 1)
                cand |= mp.line_tests(rel, body)
                rerun_classes |= mp.line_classes(rel, body)
                cand |= self.name_users({s.name}, mod)
                if classes:
                    qual = mod + "." + ".".join(c.name for c in classes)
                    cand |= mp.users_of(qual) if s.decorator_list else set()
                continue
            if isinstance(s, ast.ClassDef) and not (s.body[0].lineno <= ln):
                qual = mod + "." + ".".join(c.name for c in classes)
                cand |= self.class_users(qual, s, rel) | self.name_users({s.name}, mod)
                continue
            if classes:
                cls = classes[-1]
                qual = mod + "." + ".".join(c.name for c in classes)
                names = assigned_names(s)
                if isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant):
                    continue  # docstring
                cand |= self.class_users(qual, cls, rel)
                refs_cls = set(mp.static_classes.get(cls.name, ()))
                for n in names:
                    cand |= mp.name_tests(n, also=cls.name)
                    for q in mp.static_classes.get(n, ()):
                        if q != qual and q in refs_cls:
                            cand |= mp.users_of(q)
                continue
            # module level
            names = assigned_names(s)
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                flags.add("import")
            cand |= self.name_users(names, mod)
            if not names:
                cand |= self.module_users_by_name(mod)
        return cand, rerun_classes, flags


# ------------------------------------------------------------------- packing


def pack(nums, ms):
    if not nums:
        return [], 0
    items = sorted(nums, key=lambda n: -ms[n])
    best = None
    for k in range(1, MAX_WORKERS + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            loads[i] += ms[n]
            ws[i].append(n)
        cost = max(loads) + WORKER_COST * k
        if best is None or cost < best[1]:
            best = (ws, cost)
        if max(loads) == ms[items[0]]:
            break
    return [w for w in best[0] if w], best[1]


# --------------------------------------------------------------------- runner


FUNCS = []


class Timeout(BaseException):
    pass


class InlineExecutor:
    """ProcessPoolExecutor stand-in: same pickling, no processes."""

    def __init__(self, *a, initializer=None, initargs=(), **k):
        import pickle

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
        import pickle

        f = cf.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except Timeout:
            raise
        except BaseException as e:
            f.set_exception(e)
        return f


def code_key(c):
    return (c.co_qualname if hasattr(c, "co_qualname") else c.co_name, c.co_firstlineno)


def const_sig(c):
    if isinstance(c, types.CodeType):
        return ("<code>", c.co_name)
    if isinstance(c, (tuple, frozenset)):
        return (type(c).__name__, tuple(sorted((const_sig(x) for x in c), key=repr)) if isinstance(c, frozenset) else tuple(const_sig(x) for x in c))
    return (type(c).__name__, repr(c))


def code_sig(c):
    return (
        c.co_code,
        c.co_names,
        c.co_varnames,
        c.co_freevars,
        c.co_cellvars,
        c.co_flags,
        c.co_argcount,
        c.co_kwonlyargcount,
        c.co_posonlyargcount,
        c.co_exceptiontable if hasattr(c, "co_exceptiontable") else b"",
        tuple(const_sig(x) for x in c.co_consts),
    )


def code_diff(old_code, new_code):
    """Pair code objects; return (module_level_same, {old_key: new_code})
    or None if structure differs."""
    swaps = {}

    def walk(a, b, top):
        ca = [x for x in a.co_consts if isinstance(x, types.CodeType)]
        cb = [x for x in b.co_consts if isinstance(x, types.CodeType)]
        if len(ca) != len(cb):
            return False
        same_here = code_sig(a) == code_sig(b)
        is_func = not top and not (a.co_flags & 0) and a.co_name != "<module>"
        ok = True
        for x, y in zip(ca, cb):
            if x.co_name != y.co_name:
                return False
            if not walk(x, y, False):
                ok = False
        if not same_here:
            if top or _is_class_body(a):
                return False
            if a.co_freevars != b.co_freevars:
                return False
            swaps[code_key(a)] = b
        return ok

    ok = walk(old_code, new_code, True)
    return ok, swaps


def _is_class_body(c):
    # class bodies load __name__ into __module__ first
    return "__module__" in c.co_names and "__qualname__" in c.co_names and c.co_argcount == 0 and not (c.co_flags & 0x02 and c.co_varnames)


def child_run(job, out_fd):
    """Runs in a fresh (forked or spawned) process. Writes 'num ok' lines."""
    import builtins
    import gc
    import importlib
    import io
    import signal
    import unittest

    w = os.fdopen(out_fd, "w", buffering=1)
    changed = job["changed"]  # rel -> new text or None
    fast = job["fast"]
    tmpdir = job["tmpdir"]
    redirect = {}
    for rel, txt in changed.items():
        absp = os.path.normcase(os.path.abspath(os.path.join(SRC, rel)))
        if txt is None:
            redirect[absp] = None
        else:
            p = os.path.join(tmpdir, rel.replace("/", "__"))
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(txt)
            redirect[absp] = p

    _open = builtins.open

    def ropen(file, *a, **k):
        if isinstance(file, (str, os.PathLike)):
            ap = os.path.normcase(os.path.abspath(os.fspath(file)))
            if ap in redirect:
                if redirect[ap] is None:
                    raise FileNotFoundError(file)
                file = redirect[ap]
        return _open(file, *a, **k)

    builtins.open = ropen
    io.open = ropen

    py_changed = {rel: txt for rel, txt in changed.items() if rel.endswith(".py")}
    if fast:
        # swap code objects of changed functions in place
        want = {}
        for rel, txt in py_changed.items():
            absp = os.path.join(SRC, rel)
            old = open(absp, encoding="utf-8").read()
            ok, swaps = code_diff(compile(old, absp, "exec"), compile(txt, absp, "exec"))
            for k, v in swaps.items():
                want[(os.path.normcase(absp), k)] = v
        n = 0
        for o in FUNCS or gc.get_objects():
            if isinstance(o, types.FunctionType):
                c = o.__code__
                key = (os.path.normcase(c.co_filename), code_key(c))
                nc = want.get(key)
                if nc is not None:
                    o.__code__ = nc
                    n += 1
        w.write(f"# swapped {n}\n")
    else:
        # fresh import of sqlglot + tests with the patched sources
        for name in list(sys.modules):
            if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
                del sys.modules[name]
        overlay = {}
        for rel, txt in py_changed.items():
            overlay[modname_of(rel)] = (rel, txt)

        import importlib.abc
        import importlib.util

        class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            def find_spec(self, name, path, target=None):
                if name in overlay:
                    rel, txt = overlay[name]
                    if txt is None:
                        raise ModuleNotFoundError(name)
                    ispkg = rel.endswith("__init__.py")
                    spec = importlib.util.spec_from_loader(name, self, origin=os.path.join(SRC, rel), is_package=ispkg)
                    if ispkg:
                        spec.submodule_search_locations = [os.path.join(SRC, os.path.dirname(rel))]
                    spec.has_location = True
                    return spec
                return None

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                rel, txt = overlay[module.__name__]
                module.__file__ = os.path.join(SRC, rel)
                exec(compile(txt, module.__file__, "exec"), module.__dict__)

        sys.meta_path.insert(0, Finder())
        # changed modules must import cleanly, or everything importing them fails
        for name in overlay:
            if name.startswith("sqlglot"):
                try:
                    importlib.import_module(name)
                except BaseException as e:
                    w.write(f"! broken {name} {type(e).__name__}\n")
                    w.close()
                    os._exit(0)

    tests = job["tests"]  # list of [num, module, class, method, seconds]
    reset = set(job.get("reset_classes", ()))
    mods = {}
    classes_done = set()
    use_alarm = hasattr(signal, "setitimer")

    def on_alarm(*a):
        raise Timeout()

    if use_alarm:
        signal.signal(signal.SIGALRM, on_alarm)

    current_cls = None
    a_failed = False
    for num, modn, clsn, meth, secs, tr in tests:
        if tr == 1 and a_failed:
            w.write(f"{num} S\n")
            continue
        try:
            if modn not in mods:
                m = importlib.import_module(modn)
                for attr in ("ProcessPoolExecutor",):
                    if hasattr(m, attr):
                        setattr(m, attr, InlineExecutor)
                mods[modn] = m
            m = mods[modn]
        except BaseException as e:
            w.write(f"{num} 0 import {type(e).__name__}\n")
            a_failed = a_failed or tr == 0
            continue
        cls = getattr(m, clsn, None)
        if cls is None:
            w.write(f"{num} 0 nocls\n")
            a_failed = a_failed or tr == 0
            continue
        key = f"{modn}::{clsn}"
        if key != current_cls:
            if current_cls is not None:
                pass
            current_cls = key
            if key not in classes_done:
                classes_done.add(key)
                need = (not fast) or key in reset or not getattr(cls, "_br_ready", False)
                if need:
                    try:
                        if use_alarm:
                            signal.setitimer(signal.ITIMER_REAL, 60)
                        cls.setUpClass()
                        cls._br_setup_failed = False
                    except BaseException as e:
                        cls._br_setup_failed = True
                    finally:
                        if use_alarm:
                            signal.setitimer(signal.ITIMER_REAL, 0)
        if getattr(cls, "_br_setup_failed", False):
            w.write(f"{num} 0 setup\n")
            a_failed = a_failed or tr == 0
            continue
        result = unittest.TestResult()
        result.failfast = True  # stop at the first failing subtest
        t0 = time.perf_counter()
        try:
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, max(2.0, secs * 4 + 1.5))
            cls(meth).run(result)
        except Timeout:
            result.errors.append((None, "timeout"))
        finally:
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
        ok = not (result.failures or result.errors or getattr(result, "unexpectedSuccesses", None))
        if not ok and tr == 0:
            a_failed = True
        w.write(f"{num} {1 if ok else 0} {time.perf_counter() - t0:.3f}\n")
    w.close()
    os._exit(0)


# ----------------------------------------------------------------------- main


def main():
    t_start = time.time()
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    secs = {t["number"]: t["seconds"] for t in tests}
    meta = {}
    for t in tests:
        path, cls, meth = t["id"].split("::")
        meta[t["number"]] = (modname_of(path), cls, meth)
    all_nums = set(ms)
    mp = Map(MAP)
    an = Analysis(mp)
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], set()).add(t["number"])

    commit_dir = os.path.join(DATA, "commits")
    cids = sorted(f[:-6] for f in os.listdir(commit_dir) if f.endswith(".patch"))
    plans = {}
    for cid in cids:
        try:
            plans[cid] = plan_commit(cid, open(os.path.join(commit_dir, cid + ".patch"), encoding="utf-8", errors="replace").read(), mp, an, by_file, all_nums, secs)
        except Exception as e:
            log("plan failed", cid, repr(e))
            plans[cid] = {"out": set(all_nums), "run": set(), "heavy": set(), "changed": {}}
    log(f"planned {len(cids)} in {time.time() - t_start:.2f}s")

    results = run_all(plans, meta, secs)

    for cid in cids:
        p = plans[cid]
        fails = set(p["out"])
        res = results.get(cid)
        if p["run"]:
            if res is None or res.get("broken") is not None:
                fails |= p["heavy"]
                fails |= set(p.get("broken_out", all_nums)) if res and res.get("broken") else set(p["run"])
                if res and res.get("broken"):
                    fails |= module_importers(res["broken"], mp, by_file, all_nums)
            else:
                any_fail = False
                for n in p["run"]:
                    r = res["ok"].get(n, False)
                    if r is not True:
                        fails.add(n)
                    if r is False:
                        any_fail = True
                if any_fail:
                    fails |= p["heavy"]
        workers, cost = pack(fails, ms)
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
        log(f"{cid}: out={len(p['out'])} run={len(p['run'])} fail={len(fails)} cost={cost / 1000:.3f} {p.get('why', '')}")
    log(f"total wall {time.time() - t_start:.2f}s")


def module_importers(mod, mp, by_file, all_nums):
    out = set()
    for tf in mp.modfiles.get(mod, ()):
        out |= by_file.get(tf, set())
    out |= Analysis(mp).module_users_by_name(mod)
    return out


def plan_commit(cid, text, mp, an, by_file, all_nums, secs):
    files = parse_patch(text)
    changed = {}
    cand = set()
    out = set()
    reset = set()
    fast = True
    why = []
    for old, new, hunks in files:
        rel = new or old
        src_path = os.path.join(SRC, old) if old else None
        old_text = None
        if old:
            try:
                with open(src_path, encoding="utf-8", newline="") as f:
                    old_text = f.read()
            except (FileNotFoundError, UnicodeDecodeError):
                old_text = None
        if new is None:
            new_text = None
            touched, inserts = set(range(1, (old_text or "").count("\n") + 2)), set()
        else:
            new_text, touched, inserts = apply_hunks(old_text if old else None, hunks)
        changed[rel] = new_text
        if old and new and old != new:
            changed[old] = None
        if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            if new_text is None:
                mod = modname_of(rel)
                out |= module_importers(mod, mp, by_file, all_nums)
                why.append("deleted")
                continue
            c, rc, flags = an.py_change(rel, old_text, new_text, touched, inserts)
            if "broken" in flags:
                mod = modname_of(rel)
                if rel.startswith("tests/"):
                    out |= by_file.get(rel, set())
                    for tf in mp.modfiles.get(mod, ()):
                        out |= by_file.get(tf, set())
                else:
                    out |= module_importers(mod, mp, by_file, all_nums)
                why.append("syntax")
                continue
            if "same" in flags:
                why.append("same-ast")
                continue
            if old_text is not None:
                ok, swaps = code_diff(compile(old_text, src_path, "exec"), compile(new_text, src_path, "exec"))
                if not ok:
                    fast = False
                elif not swaps:
                    why.append("same-code")
                    continue
            cand |= c
            reset |= rc
            why.append(("static " if "static" in flags else "") + f"py:{len(c)}")
        else:
            o = mp.opens.get(rel)
            if o is not None:
                cand |= set(mp.sets[o])
                why.append(f"data:{len(mp.sets[o])}")
            else:
                why.append("unused-file")
    cand -= out
    run = {n for n in cand if tier(secs[n]) < 2}
    heavy = {n for n in cand if tier(secs[n]) == 2}
    if heavy and not run:
        out |= heavy
        heavy = set()
    return {"out": out, "run": run, "heavy": heavy, "changed": changed, "fast": fast, "reset": reset, "why": " ".join(why)}


def run_all(plans, meta, secs):
    jobs = [(cid, p) for cid, p in plans.items() if p["run"]]
    if not jobs:
        return {}
    results = {}
    use_fork = hasattr(os, "fork") and os.environ.get("BR_NOFORK") != "1"
    if use_fork:
        preload(meta)
    import tempfile

    base_tmp = tempfile.mkdtemp(prefix="br")
    par = int(os.environ.get("BR_PAR", "2"))
    running = {}
    queue = sorted(jobs, key=lambda j: -sum(secs[n] for n in j[1]["run"]))
    import select
    import subprocess

    def start(cid, p):
        tdir = os.path.join(base_tmp, cid)
        os.makedirs(tdir, exist_ok=True)
        tl = sorted(p["run"])
        job = {
            "changed": p["changed"],
            "fast": p["fast"] and use_fork,
            "tmpdir": tdir,
            "tests": [[n, *meta[n], secs[n], tier(secs[n])] for n in sorted(tl, key=lambda n: (tier(secs[n]), n))],
            "reset_classes": sorted(p["reset"]),
        }
        budget = 30 + sum(secs[n] * 4 + 2 for n in tl)
        if use_fork:
            r, wfd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    child_run(job, wfd)
                finally:
                    os._exit(0)
            os.close(wfd)
            return {"pid": pid, "fd": r, "buf": b"", "deadline": time.time() + budget, "cid": cid}
        jp = os.path.join(tdir, "job.json")
        with open(jp, "w") as f:
            json.dump(job, f)
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--child", jp], stdout=subprocess.PIPE)
        return {"proc": proc, "deadline": time.time() + budget, "cid": cid}

    def finish(h):
        cid = h["cid"]
        if "proc" in h:
            try:
                data = h["proc"].communicate(timeout=max(1, h["deadline"] - time.time()))[0]
            except subprocess.TimeoutExpired:
                h["proc"].kill()
                data = h["proc"].communicate()[0]
        else:
            data = h["buf"]
        ok = {}
        broken = None
        for line in data.decode(errors="replace").splitlines():
            if line.startswith("! broken"):
                broken = line.split()[2]
            elif line and line[0].isdigit():
                parts = line.split()
                ok[int(parts[0])] = {"1": True, "0": False, "S": None}[parts[1]]
        results[cid] = {"ok": ok, "broken": broken}

    if not use_fork:
        # Windows/local: simple sequential pool of subprocesses
        handles = []
        for cid, p in queue:
            handles.append(start(cid, p))
            if len(handles) >= par:
                finish(handles.pop(0))
        for h in handles:
            finish(h)
        return results

    while queue or running:
        while queue and len(running) < par:
            cid, p = queue.pop(0)
            h = start(cid, p)
            running[h["fd"]] = h
        rl, _, _ = select.select(list(running), [], [], 0.5)
        now = time.time()
        for fd in rl:
            h = running[fd]
            chunk = os.read(fd, 65536)
            if chunk:
                h["buf"] += chunk
                continue
            os.close(fd)
            os.waitpid(h["pid"], 0)
            del running[fd]
            finish(h)
        for fd, h in list(running.items()):
            if now > h["deadline"]:
                try:
                    os.kill(h["pid"], 9)
                except OSError:
                    pass
                os.waitpid(h["pid"], 0)
                os.close(fd)
                del running[fd]
                finish(h)
    return results


def preload(meta):
    """Import sqlglot and the test modules once; children fork from here."""
    import gc
    import importlib
    import pkgutil

    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.dont_write_bytecode = True
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception:
            pass
    from sqlglot.dialects.dialect import Dialect, Dialects

    for d in Dialects:
        try:
            Dialect.get_or_raise(d.value)
        except Exception:
            pass
    for modn in sorted({v[0] for v in meta.values()}):
        try:
            m = importlib.import_module(modn)
            if hasattr(m, "ProcessPoolExecutor"):
                m.ProcessPoolExecutor = InlineExecutor
        except Exception as e:
            log("preload import failed", modn, repr(e))
    ex = sys.modules.get("tests.test_executor")
    if ex is not None and hasattr(ex, "TestExecutor"):
        try:
            ex.TestExecutor.setUpClass()
            ex.TestExecutor._br_ready = True
        except Exception as e:
            log("executor setup failed", repr(e))
    gc.collect()
    FUNCS.extend(o for o in gc.get_objects() if isinstance(o, types.FunctionType))
    if hasattr(gc, "freeze"):
        gc.freeze()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        sys.dont_write_bytecode = True
        sys.path.insert(0, SRC)
        job = json.load(open(sys.argv[2]))
        job["fast"] = False
        child_run(job, sys.stdout.fileno())
    else:
        main()
