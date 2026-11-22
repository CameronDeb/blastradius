"""Fold measured per-test CPU (from measure_cpu.py) into map.json.gz."""
import gzip, json, sys
mp_path, cpu_path = sys.argv[1], sys.argv[2]
with gzip.open(mp_path, "rt", encoding="utf-8") as f:
    m = json.load(f)
d = json.load(open(cpu_path))
m["cpu"] = {k: round(v, 3) for k, v in d["cpu"].items()}
m["setup_cpu"] = d["setup"]
with gzip.open(mp_path, "wt", encoding="utf-8", compresslevel=9) as f:
    json.dump(m, f, separators=(",", ":"))
print("cpu entries", len(m["cpu"]))
