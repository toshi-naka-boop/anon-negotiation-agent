import sys
from collections import Counter
sys.path.insert(0, ".")
exec(open("exp6.py").read().split("pairs = [")[0])
orig = m.play_turn_budgeted
def picky(vault, view, strat, cfg):
    s = view.side
    p = vault.pending
    if p and p[0] != s and p[2] == "ACC" and not getattr(view, "declined", False) and vault.remaining(s)["moves"] >= 2:
        view.declined = True
        # push once for a better deal: re-send own last proposal (known acceptable to own side)
        own = view.my_props[-1] if view.my_props else m.aspiration(s, cfg)
        r = vault.submit(s, ("propose", own))
        if r[0] == "ok":
            view.my_props.append(own)
        return r
    return orig(vault, view, strat, cfg)
for mode in ("accept-at-once", "push-back-once"):
    m.play_turn_budgeted = orig if mode == "accept-at-once" else picky
    for lims in [(6, 10, 1), (6, 16, 1)]:
        res = Counter()
        for (sc, se) in [("hybrid", "hybrid"), ("trade", "trade")]:
            for asp_c in (S(800), S(900), S(1000)):
                for asp_c_r in (3, 5):
                    for asp_e in (S(400), S(500)):
                        for asp_e_n in (1, 2, 4):
                            cc = cfg(asp_c, asp_c_r, asp_e, asp_e_n); ce = cfg(asp_c, asp_c_r, asp_e, asp_e_n)
                            reason, u, log = m.run2(fx, sc, se, cc, ce, lims)
                            res[reason] += 1
        tot = sum(res.values())
        print(f"{mode:16s} lims={lims} agreed {res['agreed']}/{tot} {dict(res)}")
