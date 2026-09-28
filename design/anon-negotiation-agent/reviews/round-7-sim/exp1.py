import random
import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")
import sim_case1 as m

N_FX = int(sys.argv[1]) if len(sys.argv) > 1 else 150
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 6
SPARSE = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
CAT = (sys.argv[4] == "cat") if len(sys.argv) > 4 else False

rng = random.Random(20260928)
fxs = []
while len(fxs) < N_FX:
    fx = m.make_fixture(rng, sparse=SPARSE, cat_constraint=CAT)
    if fx:
        fxs.append(fx)

LIMS = {
    "v7 (6,10,1)": (6, 10, 1),
    "moves8 (8,10,1)": (8, 10, 1),
    "evals16 (6,16,1)": (6, 16, 1),
    "both (8,16,1)": (8, 16, 1),
}
PAIRS = [("split", "split"), ("concede", "concede"), ("mirror", "mirror"),
         ("split", "concede"), ("concede", "split")]

print(f"fixtures={N_FX} runs/fixture/pair={RUNS} sparse={SPARSE} cat={CAT}")
mut = sorted(fx["mutual"] for fx in fxs)
print("mutual breadth: min", mut[0], "median", mut[len(mut) // 2], "max", mut[-1])
for lname, lims in LIMS.items():
    print("==", lname)
    for pc, pe in PAIRS:
        res = Counter()
        used_ok = defaultdict(list)
        fx_success = 0
        for fx in fxs:
            any_ok = False
            for r in range(RUNS):
                rr = random.Random(hash((id(fx), r, pc, pe)) & 0xFFFFFFFF)
                cc = m.default_cfg(rr, "C")
                ce = m.default_cfg(rr, "E")
                reason, used, log = m.run(fx, pc, pe, cc, ce, lims)
                res[reason] += 1
                if reason == "agreed":
                    any_ok = True
                    for s in "CE":
                        used_ok[s + "_moves"].append(used[s]["moves"])
                        used_ok[s + "_evals"].append(used[s]["evals"])
            fx_success += any_ok
        tot = sum(res.values())
        ok = res["agreed"]
        def med(xs):
            xs = sorted(xs)
            return xs[len(xs) // 2] if xs else None
        def mx(xs):
            return max(xs) if xs else None
        print(f"  C={pc:8s} E={pe:8s} agreed {ok/tot:6.1%}  "
              f"reasons={dict(res)}  "
              f"ok-used med/max: Cmv {med(used_ok['C_moves'])}/{mx(used_ok['C_moves'])} "
              f"Cev {med(used_ok['C_evals'])}/{mx(used_ok['C_evals'])} "
              f"Emv {med(used_ok['E_moves'])}/{mx(used_ok['E_moves'])} "
              f"Eev {med(used_ok['E_evals'])}/{mx(used_ok['E_evals'])}  "
              f"fixtures with >=1 success {fx_success}/{len(fxs)}")
