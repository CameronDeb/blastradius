"""Measure the CPU each test costs under the selector's runner (inline pools,
preloaded modules) at the pinned commit. Prints JSON {number: cpu_seconds}
and {"setup": {class: cpu}} to stdout.

Run inside the image:  python /app/measure_cpu.py
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, "/app")
import selector as S  # noqa: E402

tests = json.load(open(os.path.join(S.DATA, "tests.json")))["tests"]
meta = {t["number"]: (S.modname_of(t["id"].split("::")[0]), *t["id"].split("::")[1:]) for t in tests}
S.preload({m[0] for m in meta.values()})
import importlib  # noqa: E402

cpu = {}
setup = {}
done = set()
for t in tests:
    n = t["number"]
    modn, clsn, meth = meta[n]
    m = importlib.import_module(modn)
    cls = getattr(m, clsn)
    key = f"{modn}::{clsn}"
    if key not in done:
        done.add(key)
        c0 = time.process_time()
        if not getattr(cls, "_br_ready", False):
            try:
                cls.setUpClass()
            except Exception as e:
                print("setup failed", key, e, file=sys.stderr)
        setup[key] = round(time.process_time() - c0, 4)
    r = unittest.TestResult()
    c0 = time.process_time()
    cls(meth).run(r)
    cpu[n] = round(time.process_time() - c0, 4)
    if r.failures or r.errors:
        print("FAIL", t["id"], file=sys.stderr)
print(json.dumps({"cpu": cpu, "setup": setup}))
