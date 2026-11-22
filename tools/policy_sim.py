"""Simulate run/report policies on labelled sets (plan only; CPU ~ recorded seconds).

    python policy_sim.py <data_dir> [<data_dir> ...]
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import selector as S

OVERHEAD = 0.15  # per commit with something to run


def load(data):
    ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
    tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
    return ans, tests


def main():
    rows = []
    for data in sys.argv[1:]:
        ans, tests = load(data)
        secs = {t["number"]: t["seconds"] for t in tests}
        ms = {n: round(s * 1000) for n, s in secs.items()}
        by_file = {}
        for t in tests:
            by_file.setdefault(t["file"], set()).add(t["number"])
        mp = S.Map(S.MAP)
        an = S.Analysis(mp, by_file)
        for cid in sorted(ans):
            try:
                p = S.plan_commit(cid, open(os.path.join(data, "commits", cid + ".patch"), encoding="utf-8").read(), mp, an, by_file, set(secs), secs)
            except Exception as e:
                print("plan error", cid, e)
                continue
            rows.append((data, cid, ans[cid]["kind"], set(ans[cid]["failing"]), p))
    print(len(rows), "commits")

    def pack(s):
        return S.pack(s, ms)[1] / 1000

    policies = {
        "runA_thenB_dropC": dict(b_when_a_pass="run", c_when_pass="drop"),
        "runA_dropB_dropC": dict(b_when_a_pass="drop", c_when_pass="drop"),
        "runA_thenB_keepC": dict(b_when_a_pass="run", c_when_pass="keep"),
        "runAB_all": dict(b_when_a_pass="run", c_when_pass="drop", b_always_run=True),
    }
    for name, pol in policies.items():
        total = cpu = misses = 0
        missed = []
        for data, cid, kind, F, p in rows:
            A = {n for n in p["run"] if S.tier(secs[n]) == 0}
            B = p["run"] - A
            C = set(p["heavy"])
            out = set(p["out"])
            c = 0.0
            if A or B:
                c += OVERHEAD
            c += sum(secs[n] for n in A)
            a_fail = bool(F & A)
            out |= F & A
            if pol.get("b_always_run"):
                c += sum(secs[n] for n in B)
                out |= F & B
                b_fail = bool(F & B)
            elif a_fail:
                out |= B
                b_fail = True
            elif pol["b_when_a_pass"] == "run":
                c += sum(secs[n] for n in B)
                out |= F & B
                b_fail = bool(F & B)
            else:
                b_fail = False
            if a_fail or b_fail:
                out |= C
            elif pol["c_when_pass"] == "keep":
                out |= C
            miss = bool(F - out)
            if miss:
                missed.append((cid, kind, sorted(F - out)[:5]))
            misses += miss
            cpu += c
            total += c + pack(out) + (300 if miss else 0)
        print(f"{name:20} total={total:8.1f} cpu={cpu:7.1f} misses={misses}  per-commit={total / len(rows):.2f}")
        for m in missed[:8]:
            print("     ", m)


main()
