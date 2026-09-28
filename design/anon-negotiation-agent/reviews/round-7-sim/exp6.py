import random, sys, itertools
from collections import Counter
sys.path.insert(0, ".")
import sim_case1 as m

INF = 999
def build(cmin, emax, need_night_c=1):
    cols = list(itertools.product(range(6), range(5), range(2)))
    c_acc, c_rej, e_acc, e_rej = [], [], [], []
    for (R, N, V) in cols:
        cm = cmin(R, N, V); em = emax(R, N, V)
        if cm != INF and cm <= 24: c_acc.append((cm, R, N, V, None, None, None))
        if cm != INF and cm >= 1: c_rej.append((cm - 1, R, N, V, None, None, None))
        if em != -INF and em >= 0: e_acc.append((em, R, N, V, None, None, None))
        if em != -INF and em <= 23: e_rej.append((em + 1, R, N, V, None, None, None))
    c_rej.append((24, 5, need_night_c + 1, 0, None, None, None))
    e_rej.append((0, 4, 4, 1, None, None, None))  # remote >= 4 never
    C = m.Policy("C", c_acc, c_rej, lambda P: cmin(P[1], P[2], P[3]) <= P[0])
    E = m.Policy("E", e_acc, e_rej, lambda P: emax(P[1], P[2], P[3]) >= P[0])
    mutual = sum(4 for P in itertools.product(range(25), range(6), range(5), range(2), [0], [0], range(3))
                 if C.eval(P) == "ACC" and E.eval(P) == "ACC")
    return {"C": C, "E": E, "mutual": mutual, "params": {}}

S = lambda yen: (yen - 300) // 50
# Candidate: 700 at R0, 650 at R1, 600 at R2+, night <= 2/month, review indifferent
def cmin(R, N, V):
    if N > 1: return INF
    return S(700) if R == 0 else (S(650) if R == 1 else S(600))
# Employer: cap 650 for R<=2, 600 at R3, never R>=4; +50 if night >= 2
def emax(R, N, V):
    if R >= 4: return -INF
    base = S(650) if R <= 2 else S(600)
    return base + (1 if N >= 1 else 0)

fx = build(cmin, emax)
print("hand-made case-1: mutual packages =", fx["mutual"])
for P in [(S(650), 0, 1, 0, 0, 0, 0), (S(700), 0, 0, 0, 0, 0, 0), (S(650), 1, 1, 0, 0, 0, 0), (S(600), 2, 0, 0, 0, 0, 0)]:
    print("  ", m.s_val(P[0]), "R", P[1], "N", [0,2,4,6,8][P[2]], "C:", fx["C"].eval(P), "E:", fx["E"].eval(P))

def cfg(asp_c, asp_c_r, asp_e, asp_e_n, **kw):
    c = dict(asp_c=asp_c, asp_c_r=asp_c_r, asp_e=asp_e, asp_e_n=asp_e_n, step=1, switch=1,
             monotone=True, check_first=True, max_checks=3, near=4)
    c.update(kw); return c

pairs = [("hybrid", "hybrid", {}, {}), ("trade", "trade", {}, {}),
         ("concede", "concede", dict(switch=1), dict(switch=1)),
         ("concede", "concede", dict(switch=2, step=2), dict(switch=2, step=2)),
         ("concede", "concede", dict(switch=3), dict(switch=3)),
         ("hybrid", "concede", {}, dict(switch=3)), ("trade", "trade", dict(check_first=False), dict(check_first=False))]
names = ["hybrid/hybrid", "trade/trade", "concede sw1", "concede sw2 step100", "concede sw3", "hybrid/concede sw3", "trade blind"]
for lims in [(6, 10, 1), (6, 16, 1), (8, 16, 1)]:
    print("== lims", lims)
    for (sc, se, kc, ke), nm in zip(pairs, names):
        res = Counter(); ex = None
        for asp_c in (S(800), S(900), S(1000)):
            for asp_c_r in (3, 5):
                for asp_e in (S(400), S(500)):
                    for asp_e_n in (1, 2, 4):
                        cc = cfg(asp_c, asp_c_r, asp_e, asp_e_n, **kc)
                        ce = cfg(asp_c, asp_c_r, asp_e, asp_e_n, **ke)
                        reason, u, log = m.run2(fx, sc, se, cc, ce, lims)
                        res[reason] += 1
        tot = sum(res.values())
        print(f"   {nm:22s} agreed {res['agreed']:2d}/{tot}  {dict(res)}")
