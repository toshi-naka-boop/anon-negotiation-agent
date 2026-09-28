"""Case-1 reachability simulation under design v7 counting rules.

v7 rules modelled (design.md §3.5):
- per side: moves 6 (non-check, invalid moves included), own evaluations 10
  (check, propose guard, ask_principal), questions 1, consecutive invalid 3
- receiver evaluation / accept re-check / judge / re-evaluation after answer are free
- propose: guard must be ACC (else invalid, eval consumed, move consumed)
- a side whose turn it is with 0 remaining moves -> stopped_budget
- agents see only 3-valued evaluations of their own side
All values are grid indices.
"""
import itertools
import random
from collections import Counter

# axes: S(25) R(6) N(5: 0,2,4,6,8) V(2: 6,12) T(2) J(2) ST(3)
SIZES = [25, 6, 5, 2, 2, 2, 3]
NUM = 4  # numeric axes 0..3
DIR = {"C": [1, 1, -1, -1], "E": [-1, -1, 1, 1]}  # +1: larger index is better
ACC, NEED, REJ = "ACC", "NEED", "REJ"
OTHER = {"C": "E", "E": "C"}


def s_val(i):
    return 300 + 50 * i


class Policy:
    def __init__(self, side, acc, rej, raw):
        self.side = side
        self.acc = list(acc)  # anchors: tuple(7) with None for '*' on categoricals
        self.rej = list(rej)
        self.raw = raw  # raw(P) -> bool (auto answer of the fictional principal)

    def _cat_ok(self, P, A):
        return all(A[i] is None or A[i] == P[i] for i in range(NUM, 7))

    def sat_acc(self, P, A):
        d = DIR[self.side]
        return all(d[i] * (P[i] - A[i]) >= 0 for i in range(NUM)) and self._cat_ok(P, A)

    def in_rej(self, P, R):
        d = DIR[self.side]
        return all(d[i] * (P[i] - R[i]) <= 0 for i in range(NUM)) and self._cat_ok(P, R)

    def eval(self, P):
        if any(self.sat_acc(P, A) for A in self.acc):
            return ACC
        if any(self.in_rej(P, R) for R in self.rej):
            return REJ
        return NEED

    def copy(self):
        return Policy(self.side, self.acc, self.rej, self.raw)

    def append_answer(self, P, yes):
        A = tuple(P)
        if yes:
            if any(self.in_rej(Q, R) for R in self.rej for Q in [A]):
                return
            self.acc.append(A)
        else:
            if any(self.sat_acc(A, X) for X in self.acc):
                return
            self.rej.append(A)


# ---------------------------------------------------------------- fixtures
INF = 999


def make_fixture(rng, sparse=0.0, cat_constraint=False):
    """Random case-1 fixture. cmin/emax are salary thresholds (S index)."""
    c0 = rng.randint(6, 12)            # 600..900 at R=0,N=0,V=6
    kc = rng.choice([0.5, 1.0, 1.5])   # candidate: salary discount per remote day (steps)
    capC = rng.randint(2, 5)           # max discount
    nC = rng.choice([0, 1, 2])         # candidate night tolerance (N idx)
    pnC = rng.choice([0, 1])           # extra salary per tolerated night step
    pvC = rng.choice([0, 1])           # extra salary if review 12
    gap = rng.randint(1, 3)            # e0 = c0 - gap  (no agreement at R=0, N=0)
    e0 = c0 - gap
    rEf = rng.randint(1, 3)            # employer flat remote tolerance
    ke = rng.choice([1, 2])            # employer cap drop per remote day beyond rEf
    rEmax = rng.randint(rEf, 5)        # beyond -> never
    bnE = rng.choice([0, 1])           # employer extra cap per night step
    pvE = rng.choice([0, 1])           # employer cap reduction if review 6

    def cmin(R, N, V):
        if N > nC:
            return INF
        return max(0, c0 - min(capC, int(kc * R)) + pnC * N + pvC * V)

    def emax(R, N, V):
        if R > rEmax:
            return -INF
        drop = 0 if R <= rEf else ke * (R - rEf)
        return min(24, e0 - drop + bnE * N - pvE * (1 - V))

    need_start = None
    if cat_constraint:
        need_start = rng.choice([0, 1])  # employer needs start == 1 month or 3 months

    # case-1 property: at R=0 nothing mutual; somewhere mutual
    cols = list(itertools.product(range(6), range(5), range(2)))
    mutual_cols = [(R, N, V) for (R, N, V) in cols if cmin(R, N, V) <= emax(R, N, V)]
    if any(R == 0 for (R, N, V) in mutual_cols):
        return None
    if not mutual_cols:
        return None

    # candidate policy: accept anchors per column (cmin, R, N, V), reject anchors (cmin-1)
    c_acc, c_rej, e_acc, e_rej = [], [], [], []
    for (R, N, V) in cols:
        cm = cmin(R, N, V)
        if cm != INF and cm <= 24 and rng.random() >= sparse:
            c_acc.append((cm, R, N, V, None, None, None))
        if cm != INF and cm >= 1 and rng.random() >= sparse:
            c_rej.append((cm - 1, R, N, V, None, None, None))
        em = emax(R, N, V)
        starts = [None] if need_start is None else ([0] if need_start == 0 else [0, 1])
        if em != -INF and em >= 0 and rng.random() >= sparse:
            for st in starts:
                e_acc.append((em, R, N, V, None, None, st))
        if em != -INF and em <= 23 and rng.random() >= sparse:
            e_rej.append((em + 1, R, N, V, None, None, None))
    # hard limits as unconditional reject anchors
    if nC < 4:
        c_rej.append((24, 5, nC + 1, 0, None, None, None))  # N >= nC+1 -> never
    if rEmax < 5:
        e_rej.append((0, rEmax + 1, 4, 1, None, None, None))  # R >= rEmax+1 -> never
    if need_start is not None:
        e_rej.append((0, 0, 4, 1, None, None, 2))  # start 6 months -> never
        if need_start == 0:
            e_rej.append((0, 0, 4, 1, None, None, 1))

    def raw_c(P):
        return cmin(P[1], P[2], P[3]) <= P[0]

    def raw_e(P):
        ok = emax(P[1], P[2], P[3]) >= P[0]
        if need_start is not None:
            ok = ok and (P[6] == 0 or (need_start == 1 and P[6] == 1))
        return ok

    C = Policy("C", c_acc, c_rej, raw_c)
    E = Policy("E", e_acc, e_rej, raw_e)
    # consistency (write-time check in the design)
    for P in itertools.product(*[range(n) for n in SIZES[:NUM]]):
        P = tuple(P) + (0, 0, 0 if need_start is None else need_start)
        for pol in (C, E):
            a = any(pol.sat_acc(P, A) for A in pol.acc)
            r = any(pol.in_rej(P, R) for R in pol.rej)
            assert not (a and r), "inconsistent fixture"
    # mutual region on the rounded policy (what the judge sees)
    mutual = 0
    for P in itertools.product(*[range(n) for n in SIZES[:NUM]]):
        for st in range(3):
            Q = tuple(P) + (0, 0, st)
            if C.eval(Q) == ACC and E.eval(Q) == ACC:
                mutual += 4
    if mutual == 0:
        return None
    return {"C": C, "E": E, "mutual": mutual,
            "params": dict(c0=s_val(c0), kc=kc, nC=nC, gap=gap, rEf=rEf, rEmax=rEmax,
                           need_start=need_start)}


# ---------------------------------------------------------------- vault (v7)
class Vault:
    def __init__(self, fx, lim_moves=6, lim_evals=10, lim_q=1, lim_inv=3):
        self.pol = {"C": fx["C"].copy(), "E": fx["E"].copy()}
        self.lim = dict(moves=lim_moves, evals=lim_evals, q=lim_q, inv=lim_inv)
        self.cnt = {s: dict(moves=0, evals=0, q=0, inv=0) for s in "CE"}
        self.to_move = "C"
        self.pending = None  # (by, P, recv_eval)
        self.result = None
        self.end_reason = None
        self.log = []

    def remaining(self, s):
        c = self.cnt[s]
        return dict(moves=self.lim["moves"] - c["moves"], evals=self.lim["evals"] - c["evals"],
                    q=self.lim["q"] - c["q"])

    def _invalid(self, s, reason):
        self.cnt[s]["moves"] += 1
        self.cnt[s]["inv"] += 1
        self.log.append((s, "invalid", reason))
        if self.cnt[s]["inv"] >= self.lim["inv"]:
            self._end("stopped_invalid")
        return ("invalid", reason)

    def _end(self, reason, result=None):
        self.end_reason = reason
        self.result = result

    def submit(self, s, move):
        assert self.result is None and self.end_reason is None
        assert s == self.to_move
        c = self.cnt[s]
        if c["moves"] >= self.lim["moves"]:
            self._end("stopped_budget")
            return ("ended", "stopped_budget")
        kind = move[0]
        o = OTHER[s]
        if kind == "check":
            if c["evals"] >= self.lim["evals"]:
                return self._invalid(s, "evaluation_budget_exhausted")
            c["evals"] += 1
            c["inv"] = 0
            e = self.pol[s].eval(move[1])
            self.log.append((s, "check", move[1], e))
            return ("ok", e)
        if kind == "propose":
            if c["evals"] >= self.lim["evals"]:
                return self._invalid(s, "evaluation_budget_exhausted")
            c["evals"] += 1
            g = self.pol[s].eval(move[1])
            if g != ACC:
                self.log.append((s, "guard", move[1], g))
                return self._invalid(s, "not_acceptable_to_own_principal")
            c["moves"] += 1
            c["inv"] = 0
            re = self.pol[o].eval(move[1])  # free
            self.pending = (s, move[1], re)
            self.prop_seq = getattr(self, "prop_seq", 0) + 1
            self.to_move = o
            self.log.append((s, "propose", move[1], re))
            return ("ok", ACC)
        if kind == "accept":
            if not self.pending or self.pending[0] != o:
                return self._invalid(s, "no_pending_offer")
            if self.pol[s].eval(self.pending[1]) != ACC:
                return self._invalid(s, "not_acceptable_to_own_principal")
            P = self.pending[1]
            self.log.append((s, "accept", P))
            self._end("agreed", P)
            return ("ended", "agreed")
        if kind == "reject":
            if not self.pending or self.pending[0] != o:
                return self._invalid(s, "no_pending_offer")
            c["moves"] += 1
            c["inv"] = 0
            self.pending = None
            self.to_move = o
            self.log.append((s, "reject"))
            return ("ok", None)
        if kind == "ask":
            if c["evals"] >= self.lim["evals"]:
                return self._invalid(s, "evaluation_budget_exhausted")
            c["evals"] += 1
            e = self.pol[s].eval(move[1])
            if e != NEED:
                return self._invalid(s, "question_not_applicable")
            if c["q"] >= self.lim["q"]:
                return self._invalid(s, "question_budget_exhausted")
            c["moves"] += 1
            c["q"] += 1
            c["inv"] = 0
            yes = self.pol[s].raw(move[1])
            self.pol[s].append_answer(move[1], yes)
            if self.pending and self.pending[0] == o:
                self.pending = (o, self.pending[1], self.pol[s].eval(self.pending[1]))
            self.log.append((s, "ask", move[1], yes))
            return ("ok", yes)
        if kind == "end":
            self._end("ended_by_agent")
            return ("ended", "ended_by_agent")
        raise ValueError(kind)


# ---------------------------------------------------------------- agents
class View:
    """What the agent can know (TurnInput + history of its own side)."""

    def __init__(self, side):
        self.side = side
        self.known = {}  # P -> eval (own side), from checks, guards, receiver evals, answers
        self.my_props = []
        self.other_props = []


def better(side, i, a, b):
    return DIR[side][i] * (a - b) > 0


def clamp(i, v):
    return max(0, min(SIZES[i] - 1, v))


def dominates_for(side, P, Q):
    """P is at least as good as Q for side on numeric axes and equal on categoricals."""
    d = DIR[side]
    return all(d[i] * (P[i] - Q[i]) >= 0 for i in range(NUM)) and P[NUM:] == Q[NUM:]


def infer(view, P, monotone):
    if P in view.known:
        return view.known[P]
    if monotone:
        for Q, e in view.known.items():
            if e == ACC and dominates_for(view.side, P, Q):
                return ACC
            if e == REJ and dominates_for(view.side, Q, P):
                return REJ
    return None


def aspiration(side, cfg):
    if side == "C":
        return (cfg["asp_c"], cfg["asp_c_r"], 0, 0, 1, 1, 1)
    return (cfg["asp_e"], 0, cfg["asp_e_n"], 1, 1, 1, 1)


def midpoint(side, A, B):
    """Per numeric axis midpoint, rounded toward side's preference; categoricals from B."""
    out = []
    for i in range(NUM):
        lo, hi = min(A[i], B[i]), max(A[i], B[i])
        m2 = A[i] + B[i]
        if m2 % 2 == 0:
            out.append(m2 // 2)
        else:
            out.append(hi if DIR[side][i] > 0 else lo)
    return tuple(out) + tuple(B[NUM:])


def toward(side, P, Q, frac):
    """point on segment own(P) -> other(Q) at fraction frac (0=P,1=Q), rounded toward side."""
    out = []
    for i in range(NUM):
        x = P[i] + (Q[i] - P[i]) * frac
        fl, ce = int(x // 1), -int(-x // 1)
        out.append(ce if DIR[side][i] > 0 else fl)
    return tuple(out) + tuple(Q[NUM:] if frac > 0 else P[NUM:])


def candidates_split(view, cfg):
    s = view.side
    own = view.my_props[-1] if view.my_props else aspiration(s, cfg)
    if not view.other_props:
        return [aspiration(s, cfg)]
    oth = view.other_props[-1]
    fr = [0.5, 0.25, 0.125, 0.75]
    return [toward(s, own, oth, f) for f in fr] + [own]


def candidates_concede(view, cfg):
    """salary-first concession; after `switch` own proposals, also move non-salary axes toward other."""
    s = view.side
    k = len(view.my_props)
    asp = aspiration(s, cfg)
    step = cfg["step"]
    d = DIR[s][0]
    S = clamp(0, asp[0] - d * step * k)
    P = list(asp)
    P[0] = S
    if view.other_props and k >= cfg["switch"]:
        oth = view.other_props[-1]
        m = k - cfg["switch"] + 1
        for i in range(1, NUM):
            if oth[i] != P[i]:
                sgn = 1 if oth[i] > P[i] else -1
                P[i] = P[i] + sgn * min(m, abs(oth[i] - P[i]))
        for i in range(NUM, 7):
            P[i] = oth[i]
    base = tuple(P)
    out = [base]
    # back-off variants: give back salary for own side one/two steps
    for b in (1, 2):
        Q = list(base)
        Q[0] = clamp(0, Q[0] + d * b)
        out.append(tuple(Q))
    return out


def candidates_mirror(view, cfg):
    s = view.side
    if not view.other_props:
        return [aspiration(s, cfg)]
    Q = view.other_props[-1]
    d = DIR[s]
    order = [1, 2, 0, 3] if s == "C" else [0, 1, 2, 3]
    out = []
    for steps in (1, 2):
        for i in order:
            P = list(Q)
            P[i] = clamp(i, P[i] + d[i] * steps)
            out.append(tuple(P))
    for i, j in itertools.combinations(order, 2):
        P = list(Q)
        P[i] = clamp(i, P[i] + d[i])
        P[j] = clamp(j, P[j] + d[j])
        out.append(tuple(P))
    own = view.my_props[-1] if view.my_props else aspiration(s, cfg)
    return out + [own]


STRATS = {"split": candidates_split, "concede": candidates_concede, "mirror": candidates_mirror}


def play_turn(vault, view, strat, cfg):
    """One turn: possibly several checks, then one move. Returns when turn passes or game ends."""
    s = view.side
    rem = vault.remaining(s)
    if rem["moves"] <= 0:
        return vault.submit(s, ("reject",))  # any submission -> stopped_budget
    p = vault.pending
    if p and p[0] != s:
        e = p[2]
        view.known[p[1]] = e
        if e == ACC:
            return vault.submit(s, ("accept",))
        if e == NEED and rem["q"] > 0 and rem["evals"] >= 1 and rem["moves"] >= 2:
            r = vault.submit(s, ("ask", p[1]))
            if vault.end_reason:
                return r
            view.known[p[1]] = vault.pending[2]
            if vault.pending[2] == ACC:
                return vault.submit(s, ("accept",))
            rem = vault.remaining(s)
            if rem["moves"] <= 0:
                return vault.submit(s, ("reject",))
    cands = STRATS[strat](view, cfg)
    # the other side's last proposal is known E-acceptable to them; own evaluations only
    chosen = None
    for P in cands:
        k = infer(view, P, cfg["monotone"])
        if k == ACC:
            chosen = P
            break
        if k is not None:
            continue
        rem = vault.remaining(s)
        if not cfg["check_first"]:
            chosen = P  # propose blindly; guard may fail
            break
        if rem["evals"] <= 1:
            break
        r = vault.submit(s, ("check", P))
        if vault.end_reason:
            return r
        if r[0] == "invalid":
            break
        view.known[P] = r[1]
        if r[1] == ACC:
            chosen = P
            break
    if chosen is None:
        # fall back: best-known own-acceptable package closest to other's last proposal
        known_acc = [P for P, e in view.known.items() if e == ACC]
        if not known_acc:
            chosen = aspiration(s, cfg)
        else:
            tgt = view.other_props[-1] if view.other_props else aspiration(s, cfg)
            chosen = min(known_acc, key=lambda P: sum(abs(P[i] - tgt[i]) for i in range(NUM)))
    r = vault.submit(s, ("propose", chosen))
    if r[0] == "ok":
        view.my_props.append(chosen)
        view.known[chosen] = ACC
    elif r[0] == "invalid" and r[1] == "not_acceptable_to_own_principal":
        view.known[chosen] = NEED  # agent only learns "not ACC"; treat as unknown-not-acc
    return r


def run(fx, strat_c, strat_e, cfg_c, cfg_e, lims):
    v = Vault(fx, *lims)
    views = {"C": View("C"), "E": View("E")}
    strats = {"C": strat_c, "E": strat_e}
    cfgs = {"C": cfg_c, "E": cfg_e}
    guard = 0
    while v.end_reason is None and guard < 200:
        guard += 1
        s = v.to_move
        if v.pending and v.pending[0] != s and getattr(views[s], "seen", 0) != v.prop_seq:
            # receiver sees the other side's proposal (once)
            views[s].other_props.append(v.pending[1])
            views[s].seen = v.prop_seq
        play_turn(v, views[s], strats[s], cfgs[s])
    used = {s: dict(v.cnt[s]) for s in "CE"}
    return v.end_reason, used, v.log


def default_cfg(rng, side):
    return dict(asp_c=rng.choice([10, 12, 14]),  # 800/900/1000
                asp_c_r=rng.choice([3, 5]),
                asp_e=rng.choice([2, 4]),        # 400/500
                asp_e_n=rng.choice([1, 2, 4]),   # night 2/4/8
                step=rng.choice([1, 2]),
                switch=rng.choice([1, 2, 3]),
                monotone=rng.choice([True, False]),
                check_first=True)


# ---------------------------------------------------------------- smarter strategies
def _concede(side, L, Q, axis, steps):
    """move L toward Q on axis by `steps` grid steps (not beyond Q)."""
    P = list(L)
    if P[axis] == Q[axis]:
        return None
    sgn = 1 if Q[axis] > P[axis] else -1
    P[axis] = P[axis] + sgn * min(steps, abs(Q[axis] - P[axis]))
    return tuple(P)


def candidates_trade(view, cfg):
    """logrolling concession: salary half-gap + one non-salary axis one step, with fallbacks."""
    s = view.side
    if not view.other_props:
        return [aspiration(s, cfg)]
    L = view.my_props[-1] if view.my_props else aspiration(s, cfg)
    Q = view.other_props[-1]
    L = tuple(L[:NUM]) + tuple(Q[NUM:])  # categoricals: follow the other (unknown preference)
    gs = abs(L[0] - Q[0])
    half = max(1, (gs + 1) // 2) if gs else 0
    # non-salary axes ordered by gap size (largest first)
    others = sorted([i for i in range(1, NUM) if L[i] != Q[i]], key=lambda i: -abs(L[i] - Q[i]))
    out = []
    for i in others[:2]:
        c = _concede(s, L, Q, i, 1)
        if half:
            c = _concede(s, c, Q, 0, half) or c
        out.append(c)
    if half:
        out.append(_concede(s, L, Q, 0, half))
    for i in others[:2]:
        out.append(_concede(s, L, Q, i, 1))
    if gs:
        out.append(_concede(s, L, Q, 0, 1))
    seen = set(view.my_props)
    res = []
    for c in out:
        if c and c not in res and c not in seen:
            res.append(c)
    return res or [L]


STRATS["trade"] = candidates_trade


def play_turn_budgeted(vault, view, strat, cfg):
    """Like play_turn but checks only while evals_left > moves_left (reserve 1 eval per future
    proposal), at most cfg['max_checks'] per turn; blind proposals if cfg['check_first'] is False."""
    s = view.side
    rem = vault.remaining(s)
    if rem["moves"] <= 0:
        return vault.submit(s, ("reject",))
    p = vault.pending
    if p and p[0] != s:
        e = p[2]
        view.known[p[1]] = e
        if e == ACC:
            return vault.submit(s, ("accept",))
        if e == NEED and rem["q"] > 0 and rem["evals"] >= 1 and rem["moves"] >= 2 and cfg.get("ask", True):
            r = vault.submit(s, ("ask", p[1]))
            if vault.end_reason:
                return r
            if r[0] == "ok":
                view.known[p[1]] = vault.pending[2]
                if vault.pending[2] == ACC:
                    return vault.submit(s, ("accept",))
            rem = vault.remaining(s)
            if rem["moves"] <= 0:
                return vault.submit(s, ("reject",))
    cands = STRATS[strat](view, cfg)
    chosen = None
    checks = 0
    for P in cands:
        k = infer(view, P, cfg["monotone"])
        if k == ACC:
            chosen = P
            break
        if k is not None:
            continue
        rem = vault.remaining(s)
        if not cfg["check_first"]:
            chosen = P
            break
        if rem["evals"] <= rem["moves"] or checks >= cfg.get("max_checks", 3):
            break
        r = vault.submit(s, ("check", P))
        checks += 1
        if vault.end_reason:
            return r
        if r[0] == "invalid":
            break
        view.known[P] = r[1]
        if r[1] == ACC:
            chosen = P
            break
    if chosen is None:
        known_acc = [P for P, e in view.known.items() if e == ACC and P not in view.my_props]
        if not known_acc:
            known_acc = [P for P, e in view.known.items() if e == ACC] or [aspiration(s, cfg)]
        tgt = view.other_props[-1] if view.other_props else aspiration(s, cfg)
        chosen = min(known_acc, key=lambda P: sum(abs(P[i] - tgt[i]) for i in range(NUM)))
    r = vault.submit(s, ("propose", chosen))
    if r[0] == "ok":
        view.my_props.append(chosen)
        view.known[chosen] = ACC
    elif r[0] == "invalid" and r[1] == "not_acceptable_to_own_principal":
        view.known[chosen] = NEED
    return r


def run2(fx, strat_c, strat_e, cfg_c, cfg_e, lims):
    v = Vault(fx, *lims)
    views = {"C": View("C"), "E": View("E")}
    strats = {"C": strat_c, "E": strat_e}
    cfgs = {"C": cfg_c, "E": cfg_e}
    guard = 0
    while v.end_reason is None and guard < 300:
        guard += 1
        s = v.to_move
        if v.pending and v.pending[0] != s and getattr(views[s], "seen", 0) != v.prop_seq:
            views[s].other_props.append(v.pending[1])
            views[s].seen = v.prop_seq
        play_turn_budgeted(v, views[s], strats[s], cfgs[s])
    used = {s: dict(v.cnt[s]) for s in "CE"}
    return v.end_reason, used, v.log


def _dist(A, B):
    return sum(abs(A[i] - B[i]) for i in range(NUM))


def candidates_hybrid(view, cfg):
    """concede (trade) while far apart; when close, minimally improve the other's last offer."""
    s = view.side
    if not view.other_props:
        return [aspiration(s, cfg)]
    L = view.my_props[-1] if view.my_props else aspiration(s, cfg)
    Q = view.other_props[-1]
    d = DIR[s]
    near = _dist(L, Q) <= cfg.get("near", 4)
    mirror = []
    for i in [0, 1, 2, 3]:
        P = list(Q)
        P[i] = clamp(i, P[i] + d[i])
        if tuple(P) != Q:
            mirror.append(tuple(P))
    for i, j in itertools.combinations([0, 1, 2, 3], 2):
        P = list(Q)
        P[i] = clamp(i, P[i] + d[i])
        P[j] = clamp(j, P[j] + d[j])
        mirror.append(tuple(P))
    trade = candidates_trade(view, cfg)
    seen = set(view.my_props)
    out = (mirror + trade) if near else (trade + mirror)
    res = []
    for c in out:
        if c not in res and c not in seen:
            res.append(c)
    return res or [L]


STRATS["hybrid"] = candidates_hybrid
