import random, sys
from collections import Counter
sys.path.insert(0, ".")
import sim_case1 as m

def fixtures(n, sparse, cat, seed, min_sr=0):
    rng = random.Random(seed); out = []
    import itertools
    while len(out) < n:
        fx = m.make_fixture(rng, sparse=sparse, cat_constraint=cat)
        if not fx:
            continue
        if min_sr:
            sr = set()
            for P in itertools.product(range(25), range(6), range(5), range(2)):
                Q = tuple(P) + (0, 0, 0)
                if fx["C"].eval(Q) == "ACC" and fx["E"].eval(Q) == "ACC":
                    sr.add((P[0], P[1]))
            if len(sr) < min_sr:
                continue
        out.append(fx)
    return out

def cfg(rr, **kw):
    c = dict(asp_c=rr.choice([10, 12, 14]), asp_c_r=rr.choice([3, 5]),
             asp_e=rr.choice([2, 4]), asp_e_n=rr.choice([1, 2, 4]),
             step=rr.choice([1, 2]), switch=rr.choice([1, 2, 3]),
             monotone=rr.choice([True, False]), check_first=True, max_checks=rr.choice([1, 3]), near=4)
    c.update(kw); return c

# "greedy" = does not reserve evals for future proposals (patch play rule via huge max and no reserve)
def run_set(fxs, lims, greedy=False, pairs=None):
    pairs = pairs or [("hybrid", "hybrid", {}, {}), ("trade", "trade", {}, {}),
                      ("concede", "concede", dict(switch=1), dict(switch=1)), ("hybrid", "concede", {}, dict(switch=3))]
    res = Counter()
    orig = m.play_turn_budgeted
    if greedy:
        def patched(vault, view, strat, cfg):
            # emulate no reservation: pretend moves_left is 0 when deciding to check
            real = vault.remaining
            def fake(s):
                r = dict(real(s)); 
                return r
            s = view.side
            rem = vault.remaining(s)
            # temporarily reduce moves counter seen by the reserve rule
            saved = vault.lim["moves"]
            return orig(vault, view, strat, dict(cfg, max_checks=3, _greedy=True))
        pass
    for fi, fx in enumerate(fxs):
        for pi, (sc, se, kc, ke) in enumerate(pairs):
            for r in range(4):
                rr = random.Random(fi * 100 + pi * 10 + r)
                reason, u, log = m.run2(fx, sc, se, cfg(rr, **kc), cfg(rr, **ke), lims)
                res[reason] += 1
    tot = sum(res.values())
    return res["agreed"] / tot, dict(res)

import sys
for label, sparse, cat, min_sr in [("full, any shape", 0.0, False, 0),
                                   ("full, wide (>=6 S-R cells)", 0.0, False, 6),
                                   ("sparse 30% anchors missing (NEED gaps), wide", 0.3, False, 6),
                                   ("employer needs start<=3m (categorical), wide", 0.0, True, 6)]:
    fxs = fixtures(80, sparse, cat, 99 + int(sparse * 10) + (5 if cat else 0), min_sr)
    for lims in [(6, 10, 1), (6, 10, 2), (6, 16, 1), (8, 16, 2), (40, 100, 1)]:
        rate, res = run_set(fxs, lims)
        print(f"{label:48s} lims={lims}  agreed {rate:6.1%}  {res}")
