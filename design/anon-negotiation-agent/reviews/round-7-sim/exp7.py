import sys
sys.path.insert(0, ".")
exec(open("exp6.py").read().split("pairs = [")[0])
def show(P):
    return f"({m.s_val(P[0])}万, リモ{P[1]}, 当直{[0,2,4,6,8][P[2]]}, 見直し{[6,12][P[3]]})"
printed = 0
for (sc, se, kc, ke, nm) in [("hybrid", "hybrid", {}, {}, "hybrid"), ("concede", "concede", dict(switch=3), dict(switch=3), "concede sw3"), ("trade","trade",dict(check_first=False),dict(check_first=False),"blind")]:
    shown = 0
    for asp_c in (S(800), S(900), S(1000)):
        for asp_c_r in (3, 5):
            for asp_e in (S(400), S(500)):
                for asp_e_n in (1, 2, 4):
                    if shown: break
                    cc = cfg(asp_c, asp_c_r, asp_e, asp_e_n, **kc); ce = cfg(asp_c, asp_c_r, asp_e, asp_e_n, **ke)
                    reason, u, log = m.run2(fx, sc, se, cc, ce, (6, 10, 1))
                    if reason != "agreed":
                        shown = 1
                        print("==", nm, "open C", m.s_val(asp_c), "R", asp_c_r, "/ E", m.s_val(asp_e), "night", [0,2,4,6,8][asp_e_n], "->", reason, u)
                        for L in log:
                            print("    ", L[0], L[1], *(show(x) if isinstance(x, tuple) else x for x in L[2:]))
                        # does it succeed with evals 16?
                        r2, u2, _ = m.run2(fx, sc, se, cc, ce, (6, 16, 1))
                        print("    same run with evals=16 ->", r2, u2)
