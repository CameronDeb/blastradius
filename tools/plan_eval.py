"""Plan-only check on a labelled set: recall of candidates and cost estimate."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import selector as S

data = sys.argv[1]
ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
secs = {t["number"]: t["seconds"] for t in tests}
ms = {n: round(s * 1000) for n, s in secs.items()}
names = {t["number"]: t["id"].split("::", 1)[1] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], set()).add(t["number"])
mp = S.Map(S.MAP); an = S.Analysis(mp)
tot_run = tot_est = 0
for cid in sorted(ans):
    p = S.plan_commit(cid, open(os.path.join(data, "commits", cid + ".patch")).read(), mp, an, by_file, set(secs), secs)
    fail = set(ans[cid]["failing"])
    miss = fail - p["out"] - p["run"] - p["heavy"]
    hv_miss = fail & p["heavy"] and not (fail & p["run"])
    A = {n for n in p["run"] if S.tier(secs[n]) == 0}; B = p["run"] - A
    afail = bool(fail & A)
    ran = A | (set() if afail else B)
    rs = sum(secs[n] for n in ran)
    outset = p["out"] | (fail & ran) | (B if afail else set()) | (p["heavy"] if fail & p["run"] else set())
    est = S.pack(outset, ms)[1] / 1000 + rs
    if hv_miss: miss |= fail & p["heavy"]
    tot_run += rs; tot_est += est
    print(f"{cid} {ans[cid]['kind']:8} fail={len(fail):3} out={len(p['out']):4} heavy={len(p['heavy']):2} run={len(p['run']):4} runsec={rs:6.2f} est={est:6.2f} {'MISS '+str(sorted(miss)) if miss else ''} {p['why']}")
    if p["out"] and len(p["out"]) < 8:
        print("    out:", [(names[n], secs[n], n in fail) for n in p["out"]])
print(f"total runsec={tot_run:.2f} est={tot_est:.2f}")
