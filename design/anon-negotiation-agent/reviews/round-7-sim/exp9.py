import sys
from collections import Counter, defaultdict
sys.path.insert(0, ".")
exec(open("exp6.py").read().split("pairs = [")[0])
pairs = [("hybrid", "hybrid", {}, {}), ("trade", "trade", {}, {}),
         ("concede", "concede", dict(switch=1), dict(switch=1)),
         ("concede", "concede", dict(switch=3), dict(switch=3)),
         ("hybrid", "concede", {}, dict(switch=3))]
for (sc, se, kc, ke) in pairs:
    mv = []; ev = []; res = Counter()
    for asp_c in (S(800), S(900), S(1000)):
        for asp_c_r in (3, 5):
            for asp_e in (S(400), S(500)):
                for asp_e_n in (1, 2, 4):
                    cc = cfg(asp_c, asp_c_r, asp_e, asp_e_n, **kc); ce = cfg(asp_c, asp_c_r, asp_e, asp_e_n, **ke)
                    # unlimited moves/evals, but keep the "reserve" rule meaningless by large limits
                    reason, u, log = m.run2(fx, sc, se, cc, ce, (40, 100, 1))
                    res[reason] += 1
                    if reason == "agreed":
                        mv.append(max(u["C"]["moves"], u["E"]["moves"]))
                        ev.append(max(u["C"]["evals"], u["E"]["evals"]))
    mv.sort(); ev.sort()
    q = lambda xs, p: xs[min(len(xs)-1, int(len(xs)*p))]
    print(f"{sc}/{se} {kc or ''}{ke or ''}: agreed {res['agreed']}/36; per-side max moves p50/p90/max {q(mv,.5)}/{q(mv,.9)}/{mv[-1]}, evals p50/p90/max {q(ev,.5)}/{q(ev,.9)}/{ev[-1]}")
