import random
import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")
import sim_case1 as m

N_FX = int(sys.argv[1]) if len(sys.argv) > 1 else 150
SPARSE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
CAT = (sys.argv[3] == "cat") if len(sys.argv) > 3 else False

rng = random.Random(20260928 + int(SPARSE * 100) + (7 if CAT else 0))
fxs = []
while len(fxs) < N_FX:
    fx = m.make_fixture(rng, sparse=SPARSE, cat_constraint=CAT)
    if fx:
        fxs.append(fx)

LIMS = {
    "v7 (6,10,1)": (6, 10, 1),
    "(6,14,1)": (6, 14, 1),
    "(8,14,1)": (8, 14, 1),
    "(6,10,2)": (6, 10, 2),
    "unlimited (40,100,1)": (40, 100, 1),
}


def cfg(rr, **kw):
    c = dict(asp_c=rr.choice([10, 12, 14]), asp_c_r=rr.choice([3, 5]),
             asp_e=rr.choice([2, 4]), asp_e_n=rr.choice([1, 2, 4]),
             step=rr.choice([1, 2]), switch=rr.choice([1, 2, 3]),
             monotone=rr.choice([True, False]), check_first=True, max_checks=3)
    c.update(kw)
    return c


SCEN = [
    ("trade/trade check-first", "trade", "trade", {}, {}),
    ("trade/trade blind", "trade", "trade", dict(check_first=False), dict(check_first=False)),
    ("concede(switch=1)/concede(switch=1)", "concede", "concede", dict(switch=1), dict(switch=1)),
    ("concede(switch=3)/concede(switch=3)", "concede", "concede", dict(switch=3), dict(switch=3)),
    ("trade/concede(switch=3)", "trade", "concede", {}, dict(switch=3)),
]

print(f"fixtures={N_FX} sparse={SPARSE} cat={CAT}")
mut = sorted(fx["mutual"] for fx in fxs)
print("mutual breadth: min", mut[0], "p25", mut[len(mut)//4], "median", mut[len(mut)//2], "max", mut[-1])
RUNS = 4
for lname, lims in LIMS.items():
    print("==", lname)
    for name, sc, se, kc, ke in SCEN:
        res = Counter()
        used = defaultdict(list)
        by_breadth = defaultdict(lambda: [0, 0])
        for fi, fx in enumerate(fxs):
            for r in range(RUNS):
                rr = random.Random(fi * 1000 + r)
                cc, ce = cfg(rr, **kc), cfg(rr, **ke)
                reason, u, log = m.run2(fx, sc, se, cc, ce, lims)
                res[reason] += 1
                b = "narrow(<=48)" if fx["mutual"] <= 48 else ("mid(<=144)" if fx["mutual"] <= 144 else "wide")
                by_breadth[b][1] += 1
                if reason == "agreed":
                    by_breadth[b][0] += 1
                    for s in "CE":
                        used[s + "mv"].append(u[s]["moves"])
                        used[s + "ev"].append(u[s]["evals"])
        tot = sum(res.values())

        def q(xs, p):
            xs = sorted(xs)
            return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None
        bb = " ".join(f"{k}:{v[0]/v[1]:.0%}" for k, v in sorted(by_breadth.items()))
        print(f"  {name:38s} agreed {res['agreed']/tot:6.1%} | {bb} | "
              f"fail={ {k: v for k, v in res.items() if k != 'agreed'} } | "
              f"ok: C mv p50/p90 {q(used['Cmv'],.5)}/{q(used['Cmv'],.9)} ev {q(used['Cev'],.5)}/{q(used['Cev'],.9)}; "
              f"E mv {q(used['Emv'],.5)}/{q(used['Emv'],.9)} ev {q(used['Eev'],.5)}/{q(used['Eev'],.9)}")
