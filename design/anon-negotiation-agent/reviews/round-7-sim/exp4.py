import random, sys, itertools
from collections import defaultdict
sys.path.insert(0, ".")
import sim_case1 as m

rng = random.Random(4242)
fxs = []
while len(fxs) < 300:
    fx = m.make_fixture(rng)
    if fx:
        fxs.append(fx)

def cfg(rr, **kw):
    c = dict(asp_c=rr.choice([10, 12, 14]), asp_c_r=rr.choice([3, 5]),
             asp_e=rr.choice([2, 4]), asp_e_n=rr.choice([1, 2, 4]),
             step=rr.choice([1, 2]), switch=rr.choice([1, 2, 3]),
             monotone=rr.choice([True, False]), check_first=True, max_checks=rr.choice([1, 3]), near=4)
    c.update(kw); return c

PAIRS = [("hybrid", "hybrid", {}, {}), ("trade", "trade", {}, {}),
         ("concede", "concede", dict(switch=1), dict(switch=1)), ("hybrid", "concede", {}, dict(switch=3))]

def features(fx):
    C, E = fx["C"], fx["E"]
    cells = set()
    for P in itertools.product(range(25), range(6), range(5), range(2)):
        Q = tuple(P) + (0, 0, 0)
        if C.eval(Q) == "ACC" and E.eval(Q) == "ACC":
            cells.add(P)
    sr = {(p[0], p[1]) for p in cells}
    Rs = {p[1] for p in cells}; Ss = {p[0] for p in cells}; Ns = {p[2] for p in cells}
    return dict(cells=len(cells), sr=len(sr), nR=len(Rs), nS=len(Ss), nN=len(Ns),
                n0=(0 in Ns), minR=min(Rs), nC=fx["params"]["nC"], gap=fx["params"]["gap"])

rows = []
for fi, fx in enumerate(fxs):
    ok = tot = 0
    for pi, (sc, se, kc, ke) in enumerate(PAIRS):
        for r in range(5):
            rr = random.Random(fi * 100 + pi * 10 + r)
            reason, u, log = m.run2(fx, sc, se, cfg(rr, **kc), cfg(rr, **ke), (6, 10, 1))
            ok += reason == "agreed"; tot += 1
    f = features(fx); f["rate"] = ok / tot
    rows.append(f)

def show(title, keyf):
    g = defaultdict(list)
    for r in rows:
        g[keyf(r)].append(r["rate"])
    print(title)
    for k in sorted(g):
        v = g[k]
        print(f"   {str(k):28s} n={len(v):3d} mean success={sum(v)/len(v):5.1%}  fixtures>=80%: {sum(x>=0.8 for x in v)}")

print("overall mean success under v7 limits:", round(sum(r['rate'] for r in rows)/len(rows), 3))
show("by number of (salary, remote) cells in the mutual region", lambda r: min(r["sr"], 8))
show("by number of salary steps in mutual region", lambda r: min(r["nS"], 5))
show("by number of remote levels in mutual region", lambda r: r["nR"])
show("mutual contains night=0 (candidate-best night)", lambda r: r["n0"])
show("candidate night tolerance nC (0 => employer must drop night duty to 0)", lambda r: r["nC"])
show("salary gap at remote 0 (steps of 50)", lambda r: r["gap"])
show("shape: sr>=4 and nR>=2 and nS>=2", lambda r: (r["sr"] >= 4 and r["nR"] >= 2 and r["nS"] >= 2))
top = sorted(rows, key=lambda r: -r["rate"])
print("fixtures with success >= 90%:", sum(r["rate"] >= 0.9 for r in rows), "/", len(rows))
