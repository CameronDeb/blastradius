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
# CPU seconds a test costs us relative to its recorded seconds
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.1"))


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
        self.rdeps = m.get("rdeps", {})
        self.cpu = {int(k): v for k, v in m.get("cpu", {}).items()}
        self.items = m.get("items", [])
        self.ilines = m.get("ilines", {})
        self.nlines = m.get("nlines", {})
        self.items_of = {}
        for iid, (num, key) in enumerate(self.items):
            self.items_of.setdefault(num, []).append(iid)
        self.tmod_lines = m.get("tmod_lines", {})
        self.import_set = {f: set(v) for f, v in self.import_lines.items()}

    def item_hits(self, f, lns):
        by = self.ilines.get(f)
        out = set()
        if by:
            for ln in lns:
                s = by.get(str(ln))
                if s is not None:
                    out.update(self.sets[s])
        return out

    def nonitem_tests(self, f, lns):
        by = self.nlines.get(f)
        out = set()
        if by:
            for ln in lns:
                s = by.get(str(ln))
                if s is not None:
                    out.update(self.sets[s])
        return out

    def tmods_at(self, f, lns):
        by = self.tmod_lines.get(f)
        out = set()
        if by:
            for ln in lns:
                out.update(by.get(str(ln), ()))
        return out

    def at_import(self, f, lns):
        s = self.import_set.get(f)
        return bool(s) and any(ln in s for ln in lns)

    def dependents(self, mods):
        seen = set(mods)
        stack = list(mods)
        while stack:
            for d in self.rdeps.get(stack.pop(), ()):
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        return seen

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


BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")
_OLD_TREES = {}
_STR_SPANS = {}


def in_multiline_string(rel, tree, lines, inserts=()):
    spans = _STR_SPANS.get(rel)
    if spans is None:
        spans = _STR_SPANS[rel] = [
            (n.lineno, n.end_lineno)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Constant, ast.JoinedStr)) and getattr(n, "end_lineno", n.lineno) > n.lineno
        ]
    # an inserted line lands before old line p: inside a string only if the
    # string started earlier and runs through p
    return any(a <= ln <= b for a, b in spans for ln in lines) or any(a < p <= b for a, b in spans for p in inserts)


def _span(n):
    a = n.lineno
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.decorator_list:
        a = min(a, min(d.lineno for d in n.decorator_list))
    return a, getattr(n, "end_lineno", a)


def same_region(xs, ys, lines):
    """AST equality of two statement lists, comparing only statements that
    overlap the changed (old) lines; text elsewhere is identical."""
    if len(xs) != len(ys):
        return False
    for x, y in zip(xs, ys):
        if type(x) is not type(y):
            return False
        a, b = _span(x)
        if not any(a <= ln <= b for ln in lines):
            continue
        blocks = [f for f in BLOCK_FIELDS if isinstance(getattr(x, f, None), list)]
        if blocks:
            for f in blocks:
                if len(getattr(x, f)) != len(getattr(y, f)):
                    return False
            hx = {k: v for k, v in ast.iter_fields(x) if k not in BLOCK_FIELDS}
            hy = {k: v for k, v in ast.iter_fields(y) if k not in BLOCK_FIELDS}
            if hx.keys() != hy.keys() or any(_dump(hx[k]) != _dump(hy[k]) for k in hx):
                return False
            for f in blocks:
                if f == "handlers" or f == "cases":
                    for hx_, hy_ in zip(getattr(x, f), getattr(y, f)):
                        fx = {k: v for k, v in ast.iter_fields(hx_) if k != "body"}
                        fy = {k: v for k, v in ast.iter_fields(hy_) if k != "body"}
                        if any(_dump(fx[k]) != _dump(fy.get(k)) for k in fx):
                            return False
                        if not same_region(hx_.body, hy_.body, lines):
                            return False
                elif not same_region(getattr(x, f), getattr(y, f), lines):
                    return False
        elif ast.dump(x) != ast.dump(y):
            return False
    return True


def _dump(v):
    if isinstance(v, ast.AST):
        return ast.dump(v)
    if isinstance(v, list):
        return [_dump(i) for i in v]
    return repr(v)


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
    def __init__(self, mp, by_file=None):
        self.mp = mp
        self.by_file = by_file or {}
        self.reimport = set()
        self.spans = {}

    def tests_of_module(self, tm):
        out = set(self.by_file.get(tm.replace(".", "/") + ".py", ()))
        for tf in self.mp.modfiles.get(tm, ()):
            out |= self.by_file.get(tf, set())
        return out

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
        cache = self.__dict__.setdefault("_mu_cache", {})
        if mod in cache:
            return set(cache[mod])
        if not hasattr(self, "_by_prefix"):
            bp = {}
            for q, s in self.mp.users.items():
                parts = q.split(".")
                for i in range(1, len(parts)):
                    bp.setdefault(".".join(parts[:i]), set()).update(self.mp.sets[s])
            self._by_prefix = bp
        rel = mod.replace(".", "/") + ".py"
        out = self.mp.file_tests(rel) | self.mp.file_tests(mod.replace(".", "/") + "/__init__.py")
        out |= self._by_prefix.get(mod, set())
        cache[mod] = frozenset(out)
        return out

    def py_change(self, rel, old, new, touched, inserts, removed_txt=(), added_txt=()):
        """Return (candidate tests, class contexts that must re-run setup, flags)."""
        mp = self.mp
        flags = set()
        if old is None:
            try:
                ast.parse(new)
            except SyntaxError:
                return None, set(), {"broken"}
            return set(), set(), flags  # new module: nothing imports it at the pinned commit
        old_tree = _OLD_TREES.get(rel)
        if old_tree is None:
            old_tree = _OLD_TREES[rel] = ast.parse(old)
        region = set(touched) | set(inserts) | {p - 1 for p in inserts}
        texts = list(removed_txt) + list(added_txt)
        quick = texts and all(not t.strip() or t.lstrip().startswith("#") for t in texts) and not any(
            re.search(r"coding[:=]", t) for t in texts
        )
        if not (quick and not in_multiline_string(rel, old_tree, set(touched), inserts)):
            # The child compiles the new source anyway; only parse here when
            # the edit could be a no-op we can prove cheaply.
            quick = False
        if quick:
            try:
                new_tree = ast.parse(new)
            except SyntaxError:
                return None, set(), {"broken"}
            if same_region(old_tree.body, new_tree.body, region):
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
        spans = set()
        imp_spans = set()
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
                spans |= span
                # a lambda's first line is also where the lambda object is created
                imp_spans |= span - {fn.lineno} if isinstance(fn, ast.Lambda) else span
                rerun_classes |= mp.line_classes(rel, span)
                continue
            # Not inside a function body: import-time code.
            flags.add("static")
            s = stmts[-1] if stmts else None
            if s is None:
                continue
            spans |= set(range(s.lineno, s.end_lineno + 1))
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
        # code that runs while a test module is imported: a failure there
        # breaks every test file importing that module
        tmods = mp.tmods_at(rel, spans)
        if tmods:
            flags.add("tmod")
            self.reimport |= tmods
            for tm in tmods:
                cand |= self.tests_of_module(tm)
        # function bodies executed while sqlglot itself is imported
        if "static" not in flags and mp.at_import(rel, imp_spans):
            flags.add("importtime")
            cand |= self.module_users_by_name(mod)
            for d in mp.dependents({mod}):
                cand |= self.module_users_by_name(d)
        self.spans[rel] = (spans, "static" in flags or "importtime" in flags or "tmod" in flags)
        if not cand and not quick:
            # nothing would run the new source, so make sure it still compiles
            try:
                ast.parse(new)
            except SyntaxError:
                return None, set(), {"broken"}
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
        """Returns (ok, changed). A function whose nested code (lambda,
        genexpr, inner def) changed is swapped too: nested code objects are
        only reachable through the enclosing function's constants."""
        ca = [x for x in a.co_consts if isinstance(x, types.CodeType)]
        cb = [x for x in b.co_consts if isinstance(x, types.CodeType)]
        if len(ca) != len(cb):
            return False, True
        same_here = code_sig(a) == code_sig(b)
        child_changed = False
        for x, y in zip(ca, cb):
            if x.co_name != y.co_name:
                return False, True
            ok, ch = walk(x, y, False)
            if not ok:
                return False, True
            child_changed = child_changed or ch
        container = top or _is_class_body(a)
        if not same_here:
            if container or a.co_freevars != b.co_freevars:
                return False, True
            swaps[code_key(a)] = b
        elif child_changed and not container:
            if a.co_freevars != b.co_freevars:
                return False, True
            swaps[code_key(a)] = b
        return True, (not same_here) or child_changed

    ok, _ = walk(old_code, new_code, True)
    return ok, swaps


def _is_class_body(c):
    # class bodies load __name__ into __module__ first
    return "__module__" in c.co_names and "__qualname__" in c.co_names and c.co_argcount == 0 and not (c.co_flags & 0x02 and c.co_varnames)


ITEM_SEL = {"F": None}


def prepare_test_module(m):
    if hasattr(m, "ProcessPoolExecutor"):
        m.ProcessPoolExecutor = InlineExecutor
    if m.__name__ == "tests.test_optimizer" and not getattr(m, "_br_pairs", None):
        orig = m.load_sql_fixture_pairs

        def pairs(filename, _orig=orig):
            sel = ITEM_SEL["F"]
            got = list(_orig(filename))
            if sel is None:
                return got
            keep = sel.get(filename)
            if keep is None:
                return got
            return [x for i, x in enumerate(got) if i in keep]

        m.load_sql_fixture_pairs = pairs
        m._br_pairs = True
    if m.__name__ == "tests.test_executor" and not getattr(m, "_br_duck", None):
        m.duckdb = LazyDuck(m.duckdb)
        m._br_duck = True


def apply_items(m, cls, meth, keys):
    """Restrict a test to some of its items; returns an undo function."""
    if not keys:
        return lambda: None
    fx, xs, ds = {}, set(), set()
    for k in keys:
        kind, _, rest = k.partition("|")
        if kind == "F":
            f, _, i = rest.rpartition("|")
            fx.setdefault(f, set()).add(int(i))
        elif kind == "X":
            xs.add(int(rest))
        elif kind == "D":
            ds.add(rest)
    undo = []
    if fx:
        ITEM_SEL["F"] = fx
        undo.append(lambda: ITEM_SEL.__setitem__("F", None))
    if xs:
        attr = "tpcds_sqls" if meth == "test_execute_tpcds" else "tpch_sqls"
        orig = getattr(cls, attr, None)
        if orig is not None:
            setattr(cls, attr, [x for i, x in enumerate(orig) if i in xs])
            undo.append(lambda: setattr(cls, attr, orig))
    if ds and hasattr(m, "Dialects"):
        orig_d = m.Dialects
        m.Dialects = [d for d in orig_d if getattr(d, "value", d) in ds]
        undo.append(lambda: setattr(m, "Dialects", orig_d))

    def restore():
        for u in undo:
            u()

    return restore


class LazyDuck:
    """duckdb module stand-in whose connections open on first real use, so
    class setup can run before fork without live connections crossing it."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def connect(self, *a, **k):
        return LazyConn(self._real, a, k)


class LazyConn:
    def __init__(self, real, a, k):
        self._real, self._a, self._k = real, a, k
        self._conn = None
        self._pending = []

    def _get(self):
        if self._conn is None:
            self._conn = self._real.connect(*self._a, **self._k)
            for sql in self._pending:
                self._conn.execute(sql)
            self._pending = []
        return self._conn

    def execute(self, sql, *a, **k):
        if self._conn is None and not a and not k and isinstance(sql, str) and sql.lstrip().upper().startswith("CREATE VIEW"):
            self._pending.append(sql)
            return self
        return self._get().execute(sql, *a, **k)

    def close(self):
        if self._conn is not None:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._get(), name)


def holders(purge):
    """Grow `purge` with every loaded sqlglot/tests module that holds an object
    (class, function, module or instance) defined in a purged module."""
    mods = [(n, m) for n, m in list(sys.modules.items()) if m is not None and n.split(".")[0] in ("sqlglot", "tests")]
    changed = True
    while changed:
        changed = False
        for name, mod in mods:
            if name in purge:
                continue
            for v in list(vars(mod).values()):
                if isinstance(v, types.ModuleType):
                    owner = v.__name__
                elif isinstance(v, (type, types.FunctionType)):
                    owner = getattr(v, "__module__", None)
                else:
                    owner = type(v).__module__
                if owner in purge:
                    purge.add(name)
                    changed = True
                    break
    return sorted(purge)


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
        want = {}
        for rel, txt in py_changed.items():
            absp = os.path.join(SRC, rel)
            with _open(absp, encoding="utf-8") as f:
                old = f.read()
            try:
                new_code = compile(txt, absp, "exec")
            except SyntaxError:
                w.write(f"! broken {modname_of(rel)} SyntaxError\n")
                w.close()
                os._exit(0)
            ok, swaps = code_diff(compile(old, absp, "exec"), new_code)
            if not ok:
                fast = False
                job["purge"] = sorted({modname_of(r) for r in py_changed} | set(job.get("reimport") or ()))
                w.write("# fallback to re-import\n")
                break
            for k, v in swaps.items():
                want[(os.path.normcase(absp), k)] = v
    if fast:
        # swap code objects of changed functions in place
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
        if job.get("reimport"):
            for name in holders(set(job["reimport"])):
                sys.modules.pop(name, None)
    else:
        # re-import the changed modules and everything that imports them
        purge = job.get("purge")
        if purge is None:
            purge = [n for n in sys.modules if n.split(".")[0] in ("sqlglot", "tests")]
        else:
            purge = holders(set(purge))
        for name in purge:
            sys.modules.pop(name, None)
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
        broken_any = False
        for name in sorted(purge):
            if name.startswith("sqlglot") and not name.endswith("__main__"):
                try:
                    importlib.import_module(name)
                except BaseException as e:
                    w.write(f"! broken {name} {type(e).__name__}\n")
                    broken_any = True
        if broken_any:
            w.close()
            os._exit(0)
        # a re-imported package loses attributes for submodules that stayed loaded
        for name, m in list(sys.modules.items()):
            if m is not None and "." in name and name.split(".")[0] in ("sqlglot", "tests"):
                parent, _, short = name.rpartition(".")
                pm = sys.modules.get(parent)
                if pm is not None and short not in pm.__dict__:
                    try:
                        setattr(pm, short, m)
                    except Exception:
                        pass

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
    fixed = {int(n): int(v) for n, v in job.get("fixed", [])}
    heavy = {int(n): int(v) for n, v in job.get("heavy", [])}
    tms = dict(fixed)
    tms.update(heavy)
    for t in tests:
        tms[t[0]] = round(t[4] * 1000)
    fails = set()

    def should_stop(i):
        # after a failure: report what is left instead of running it, when cheaper
        rem = [t[0] for t in tests[i + 1 :]]
        if not rem:
            return False
        base = fails | set(fixed) | set(heavy)
        stop = pack(base | set(rem), tms)[1]
        cont = CPU_FACTOR * 1000 * sum(t[6] for t in tests[i + 1 :]) + pack(base, tms)[1]
        return stop <= cont

    i = -1
    while i + 1 < len(tests):
        i += 1
        num, modn, clsn, meth, secs, tr, _c = tests[i]
        ok = False
        try:
            if modn not in mods:
                m = importlib.import_module(modn)
                prepare_test_module(m)
                mods[modn] = m
            m = mods[modn]
        except BaseException as e:
            w.write(f"{num} 0 import {type(e).__name__}\n")
            m = None
        cls = getattr(m, clsn, None) if m is not None else None
        if m is not None and cls is None:
            w.write(f"{num} 0 nocls\n")
        if cls is not None:
            key = f"{modn}::{clsn}"
            if key not in classes_done:
                classes_done.add(key)
                need = (not fast) or key in reset or not getattr(cls, "_br_ready", False)
                if need:
                    try:
                        if use_alarm:
                            signal.setitimer(signal.ITIMER_REAL, 60)
                        cls.setUpClass()
                        cls._br_setup_failed = False
                    except BaseException:
                        cls._br_setup_failed = True
                    finally:
                        if use_alarm:
                            signal.setitimer(signal.ITIMER_REAL, 0)
            if getattr(cls, "_br_setup_failed", False):
                w.write(f"{num} 0 setup\n")
            else:
                restore = apply_items(m, cls, meth, job.get("items", {}).get(str(num)))
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
                restore()
                ok = not (result.failures or result.errors or getattr(result, "unexpectedSuccesses", None))
                w.write(f"{num} {1 if ok else 0} {time.perf_counter() - t0:.3f}\n")
                if not ok and os.environ.get("BR_DEBUG"):
                    err = str((result.failures or result.errors or [(0, "?")])[0][1])[-700:]
                    w.write("#   " + err.replace("\n", "\n#   ") + "\n")
        if not ok:
            fails.add(num)
            if should_stop(i):
                for t in tests[i + 1 :]:
                    w.write(f"{t[0]} S\n")
                break
    w.close()
    os._exit(0)


# ----------------------------------------------------------------------- main


def main():
    t_start = time.time()
    # Only answer lines may reach stdout: keep the real stdout aside and send
    # everything else (imports, tests, children) to stderr.
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    secs = {t["number"]: t["seconds"] for t in tests}
    meta = {}
    for t in tests:
        path, cls, meth = t["id"].split("::")
        meta[t["number"]] = (modname_of(path), cls, meth)
        TEST_CLASS[t["number"]] = f"{modname_of(path)}::{cls}"
    all_nums = set(ms)
    mp = Map(MAP)
    cost = {n: mp.cpu.get(n, secs[n]) for n in secs}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], set()).add(t["number"])
    an = Analysis(mp, by_file)

    commit_dir = os.path.join(DATA, "commits")
    cids = sorted(f[:-6] for f in os.listdir(commit_dir) if f.endswith(".patch"))
    plans = {}
    for cid in cids:
        try:
            plans[cid] = plan_commit(cid, open(os.path.join(commit_dir, cid + ".patch"), encoding="utf-8", errors="replace").read(), mp, an, by_file, all_nums, cost)
        except Exception as e:
            log("plan failed", cid, repr(e))
            plans[cid] = {"out": set(all_nums), "run": set(), "heavy": set(), "changed": {}}
    log(f"planned {len(cids)} in {time.time() - t_start:.2f}s")

    try:
        results = run_all(plans, meta, secs, cost)
    except BaseException as e:  # never lose the batch: unrun tests get reported
        log("runner failed", repr(e))
        results = {}

    for cid in cids:
        p = plans[cid]
        fails = set(p["out"])
        res = results.get(cid)
        if p["run"]:
            if res is None or res.get("broken") is not None:
                fails |= p["heavy"]
                fails |= set(p["run"])
                for bm in (res or {}).get("broken") or ():
                    fails |= module_importers(bm, mp, by_file, all_nums)
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
        out.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
        out.flush()
        ccpu = (res or {}).get("cpu", 0.0)
        log(f"{cid}: out={len(p['out'])} run={len(p['run'])} fail={len(fails)} cost={cost / 1000:.3f} cpu={ccpu:.2f} {'fast' if p.get('fast') else 'slow'} {p.get('why', '')}")
    log(f"total wall {time.time() - t_start:.2f}s, parent cpu {time.process_time():.2f}s, children cpu {sum(r.get('cpu', 0) for r in results.values()):.2f}s")


def module_importers(mod, mp, by_file, all_nums):
    out = set()
    for tf in mp.modfiles.get(mod, ()):
        out |= by_file.get(tf, set())
    an = _AN.get(id(mp))
    if an is None:
        an = _AN[id(mp)] = Analysis(mp)
    out |= an.module_users_by_name(mod)
    return out


_AN = {}


TEST_CLASS = {}


def meta_cls(n):
    return TEST_CLASS.get(n)


def select_items(n, iids, spans, changed, mp, cls_key):
    """Items of test n that execute a changed line, or None to run it whole."""
    if any(not rel.endswith(".py") for rel in changed):
        return None
    sel = set()
    for rel, (sp, static) in spans.items():
        if not sp:
            continue
        if static or n in mp.nonitem_tests(rel, sp):
            return None
        if cls_key and cls_key in mp.line_classes(rel, sp):
            return None
        sel |= mp.item_hits(rel, sp)
    for rel in changed:
        if rel.endswith(".py") and rel not in spans:
            return None
    sel &= set(iids)
    return sel or None


def plan_commit(cid, text, mp, an, by_file, all_nums, secs):
    files = parse_patch(text)
    an.reimport = set()
    an.spans = {}
    data_heavy = False
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
            removed_txt = [b[1:] for _, body in hunks for b in body if b[:1] == "-"]
            added_txt = [b[1:] for _, body in hunks for b in body if b[:1] == "+"]
            c, rc, flags = an.py_change(rel, old_text, new_text, touched, inserts, removed_txt, added_txt)
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
            if "importtime" in flags or "static" in flags or old_text is None:
                fast = False
            cand |= c
            reset |= rc
            why.append(("static " if "static" in flags else "") + f"py:{len(c)}")
        else:
            o = mp.opens.get(rel)
            if o is not None:
                cand |= set(mp.sets[o])
                data_heavy = True
                why.append(f"data:{len(mp.sets[o])}")
            else:
                why.append("unused-file")
    cand -= out
    purge = None
    if not fast:
        purge = sorted(mp.dependents({modname_of(r) for r in changed if r.endswith(".py")}))
    items = {}
    est = {}
    for n in cand:
        iids = mp.items_of.get(n)
        if not iids:
            continue
        sel = select_items(n, iids, an.spans, changed, mp, meta_cls(n))
        if sel is None:
            continue
        items[n] = sorted(mp.items[i][1] for i in sel)
        est[n] = secs[n] * (len(sel) + 1) / (len(iids) + 1)
    c_of = lambda n: est.get(n, secs[n])  # noqa: E731
    run = {n for n in cand if tier(c_of(n)) < 2}  # secs here: our CPU cost per test
    heavy = {n for n in cand if tier(c_of(n)) == 2}
    if heavy and (not run or data_heavy):
        out |= heavy
        heavy = set()
    if purge is not None:
        purge = sorted(set(purge) | an.reimport)
    return {"out": out, "run": run, "heavy": heavy, "purge": purge, "reimport": sorted(an.reimport), "items": items, "est": est, "changed": changed, "fast": fast, "reset": reset, "why": " ".join(why)}


def run_all(plans, meta, secs, cost):
    jobs = [(cid, p) for cid, p in plans.items() if p["run"]]
    if not jobs:
        return {}
    results = {}
    use_fork = hasattr(os, "fork") and os.environ.get("BR_NOFORK") != "1"
    if use_fork:
        need = set()
        for _, p in jobs:
            need |= {meta[n][0] for n in p["run"]}
        c0 = time.process_time()
        preload(need)
        log(f"preload {time.process_time() - c0:.2f}s cpu, {len(need)} test modules")
    import tempfile

    base_tmp = tempfile.mkdtemp(prefix="br")
    par = int(os.environ.get("BR_PAR", "2"))
    running = {}
    queue = sorted(jobs, key=lambda j: -sum(cost[n] for n in j[1]["run"]))
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
            "tests": [[n, *meta[n], secs[n], tier(p["est"].get(n, cost[n])), p["est"].get(n, cost[n])] for n in sorted(tl, key=lambda n: (tier(p["est"].get(n, cost[n])), p["est"].get(n, cost[n]), n))],
            "items": {str(n): v for n, v in p.get("items", {}).items() if n in p["run"]},
            "fixed": [[n, round(secs[n] * 1000)] for n in sorted(p["out"])],
            "heavy": [[n, round(secs[n] * 1000)] for n in sorted(p["heavy"])],
            "reset_classes": sorted(p["reset"]),
            "purge": p.get("purge") if use_fork else None,
            "reimport": p.get("reimport", []),
        }
        budget = 30 + sum(max(secs[n], cost[n]) * 4 + 2 for n in tl)
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
        broken = []
        for line in data.decode(errors="replace").splitlines():
            if line.startswith("! broken"):
                broken.append(line.split()[2])
            elif line and line[0].isdigit():
                parts = line.split()
                ok[int(parts[0])] = {"1": True, "0": False, "S": None}[parts[1]]
        results[cid] = {"ok": ok, "broken": broken or None, "cpu": h.get("cpu", 0.0)}

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
            _, _, ru = os.wait4(h["pid"], 0)
            h["cpu"] = ru.ru_utime + ru.ru_stime
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


def preload(test_mods):
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
    for modn in sorted(test_mods):
        try:
            m = importlib.import_module(modn)
            prepare_test_module(m)
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
