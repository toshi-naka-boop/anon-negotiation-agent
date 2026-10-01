"""台本の交渉エージェント(LLM を使わない。design.md §8.4・§12.2 の DV-14)。

交渉エージェント(Gemini)の代わりに、レフェリーの SendTurn の差し込み口に入れる。Gemini と同じく、TurnInput だけを見て
Move を返す: 手番ごとに状態を持たず、直前の呼び出しの記憶がない(履歴・相手の提案・確認の結果は、すべて TurnInput から
読み直す)。見えない条件(依頼者のポリシー)は、確認(check)と評価の 3 値からしか知れない。
軸ごとの向き(どちらが自分の依頼者に良いか)は、TurnInput にないので、台本の側に持つ(指示文の「軸の向き」に当たる)。

探し方は、7 巡目のシミュレーション(design/anon-negotiation-agent/reviews/round-7-sim/sim_case1.py の candidates_*・
play_turn_budgeted)を移植した。そのシミュレーションでは、確認の結果をエージェントが自分の記憶に持っていたが、ここでは
TurnInput の history・pending_offer・last_check・last_invalid から作り直す。そのため、確認せずに提案した手(無効手)の
記憶だけは、直前の 1 手ぶん(last_invalid)しか残らない。
- trade: 年収は差の半分、他の軸は 1 段ずつ一緒に譲る(7 巡目の表の 1 行目)
- hybrid: trade に加え、差が縮んだら相手の案を 1 段寄せて確かめる(2 行目)
- concede: 年収を 1 段ずつ譲り、switch 回目の提案から他の軸も寄せる(3 行目は switch=1、4 行目は switch=3)

手の選び方(シミュレーションの 1 手番と同じ)
1. 相手の提案の評価が「受けられる」なら accept する。
2. 候補の組み合わせ(探し方が決める)を順に見て、評価が「受けられる」と分かっているものを propose する。分からないものは、
   確認(check)に使える評価が残っていれば check する(残りの評価が残りの手数以下なら確認しない。1 手番に max_checks 回まで)。
3. 確認で「受けられる」組み合わせが見つからなければ、受けられると分かっている組み合わせのうち、相手の直前の提案に
   いちばん近いものを propose する。
check_first=False の探し方は、確認せずに候補の先頭を propose する(確認せずに譲歩案を出す型。7 巡目の表の 6 行目)。
途中確認(ask_principal)は使わない。ケース 1 のポリシーは、受けられる・受けられないがすべて決まっていて、
「本人確認が必要」になる組み合わせがないので、使う場面がない。
"""

import itertools
from dataclasses import dataclass
from typing import Literal

from negotiation_core import AXES, AXIS_KEYS, NUMERIC_AXIS_KEYS, Package, Side, TurnInput, Verdict
from web_helpers import move_dict

# 7 軸のグリッド上の位置(AXIS_KEYS の順)。探し方の計算は、値ではなく位置で行う(シミュレーションと同じ)。
Indices = tuple[int, ...]

_NUMERIC = len(NUMERIC_AXIS_KEYS)
assert NUMERIC_AXIS_KEYS == AXIS_KEYS[:_NUMERIC], "numeric axes must come first"
_SIZES = tuple(len(AXES[axis].grid) for axis in AXIS_KEYS)

# 軸ごとの向き: グリッドの位置が大きいほど良い(+1)か、小さいほど良い(-1)か。数値軸だけ(語彙 §2.1 から取る)。
_DIRECTIONS: dict[Side, tuple[int, ...]] = {
    side: tuple(1 if AXES[axis].direction_for(side) == "higher_is_better" else -1 for axis in NUMERIC_AXIS_KEYS)
    for side in ("candidate", "employer")
}


@dataclass(frozen=True)
class Strategy:
    """探し方(シミュレーションの cfg のうち、探し方に関わるもの)。"""

    kind: Literal["trade", "hybrid", "concede"]
    step: int = 1  # concede: 年収を 1 回の提案ごとに譲るグリッドの段数
    switch: int = 1  # concede: 何回目の提案から他の軸も寄せるか(それまでは年収だけを譲る)
    monotone: bool = True  # 確認した結果から、支配関係で評価を推し量る
    check_first: bool = True  # False なら、確認せずに候補の先頭を propose する
    max_checks: int = 3  # 1 手番に使う確認の最大回数
    near: int = 4  # hybrid: 自分の直前の提案と相手の直前の提案の距離がこれ以下なら「差が縮んだ」


@dataclass(frozen=True)
class Negotiator:
    """片側の台本のエージェント: 探し方と、最初の手(最初の提案にする組み合わせ)。"""

    strategy: Strategy
    opening: Package


def to_indices(package: Package) -> Indices:
    return tuple(AXES[axis].grid.index(getattr(package, axis)) for axis in AXIS_KEYS)


def to_package(indices: Indices) -> Package:
    return Package(**{axis: AXES[axis].grid[i] for axis, i in zip(AXIS_KEYS, indices, strict=True)})


# --- TurnInput から、探し方の入力を作る ---


def _proposals(turn_input: TurnInput) -> tuple[list[Indices], list[Indices]]:
    """(自分の提案, 相手の提案)を、出た順に。"""
    mine = [to_indices(e.package) for e in turn_input.history if e.by == "self" and e.move == "propose"]
    theirs = [to_indices(e.package) for e in turn_input.history if e.by == "counterparty" and e.move == "propose"]
    return mine, theirs


def _known_evaluations(turn_input: TurnInput) -> dict[Indices, Verdict]:
    """分かっている、自分側の評価(確認・提案・相手の提案を受けたときのもの)。出た順。"""
    known: dict[Indices, Verdict] = {}
    for entry in turn_input.history:
        if entry.move in ("check", "propose"):
            known[to_indices(entry.package)] = entry.result
    for evaluated in (turn_input.pending_offer, turn_input.last_check):  # 途中確認の回答で評価し直されたものを優先する
        if evaluated is not None:
            known[to_indices(evaluated.package)] = evaluated.own_evaluation
    invalid = turn_input.last_invalid
    if invalid is not None and invalid.package is not None:
        # 確認せずに出して断られた組み合わせ。シミュレーションと同じく、「受けられるとは限らない」とだけ覚える。
        known[to_indices(invalid.package)] = Verdict.NEEDS_CONFIRMATION
    return known


def _checks_this_turn(turn_input: TurnInput) -> int:
    """この手番で、すでに使った確認の回数(history の末尾の、続けて確認した数)。直前の手が無効なら、数え直す。"""
    if turn_input.last_invalid is not None:
        return 0
    count = 0
    for entry in reversed(turn_input.history):
        if entry.by != "self" or entry.move != "check":
            break
        count += 1
    return count


def _dominates_for(side: Side, better: Indices, worse: Indices) -> bool:
    """better が worse と比べて、数値軸すべてで自分側にとって同じかそれ以上に良く、区分軸は同じ。"""
    signs = _DIRECTIONS[side]
    return all(signs[i] * (better[i] - worse[i]) >= 0 for i in range(_NUMERIC)) and better[_NUMERIC:] == worse[_NUMERIC:]


def _infer(known: dict[Indices, Verdict], side: Side, package: Indices, monotone: bool) -> Verdict | None:
    if package in known:
        return known[package]
    if monotone:
        for other, verdict in known.items():
            if verdict is Verdict.ACCEPTABLE and _dominates_for(side, package, other):
                return Verdict.ACCEPTABLE
            if verdict is Verdict.NOT_ACCEPTABLE and _dominates_for(side, other, package):
                return Verdict.NOT_ACCEPTABLE
    return None


# --- 探し方(候補の組み合わせを、優先の高い順に返す) ---


def _clamp(axis: int, value: int) -> int:
    return max(0, min(_SIZES[axis] - 1, value))


def _distance(a: Indices, b: Indices) -> int:
    return sum(abs(a[i] - b[i]) for i in range(_NUMERIC))


def _move_toward(own: Indices, other: Indices, axis: int, steps: int) -> Indices | None:
    """own の axis を、other の方へ steps 段(other を越えない)動かす。すでに同じなら None。"""
    if own[axis] == other[axis]:
        return None
    sign = 1 if other[axis] > own[axis] else -1
    moved = list(own)
    moved[axis] += sign * min(steps, abs(other[axis] - own[axis]))
    return tuple(moved)


def _trade_candidates(
    side: Side, mine: list[Indices], theirs: list[Indices], opening: Indices, strategy: Strategy
) -> list[Indices]:
    """年収は差の半分、他の軸は 1 段ずつ一緒に譲る(区分軸は相手の直前の提案に合わせる)。"""
    if not theirs:
        return [opening]
    own = mine[-1] if mine else opening
    other = theirs[-1]
    own = own[:_NUMERIC] + other[_NUMERIC:]
    salary_gap = abs(own[0] - other[0])
    half = max(1, (salary_gap + 1) // 2) if salary_gap else 0
    gaps = sorted((i for i in range(1, _NUMERIC) if own[i] != other[i]), key=lambda i: -abs(own[i] - other[i]))
    ordered: list[Indices | None] = []
    for axis in gaps[:2]:
        moved = _move_toward(own, other, axis, 1)
        if half:
            moved = _move_toward(moved, other, 0, half) or moved
        ordered.append(moved)
    if half:
        ordered.append(_move_toward(own, other, 0, half))
    for axis in gaps[:2]:
        ordered.append(_move_toward(own, other, axis, 1))
    if salary_gap:
        ordered.append(_move_toward(own, other, 0, 1))
    seen = set(mine)
    candidates: list[Indices] = []
    for candidate in ordered:
        if candidate and candidate not in candidates and candidate not in seen:
            candidates.append(candidate)
    return candidates or [own]


def _hybrid_candidates(
    side: Side, mine: list[Indices], theirs: list[Indices], opening: Indices, strategy: Strategy
) -> list[Indices]:
    """trade に加え、差が縮んだら、相手の直前の提案を自分側に 1 段(または 2 軸 1 段ずつ)寄せたものを先に確かめる。"""
    if not theirs:
        return [opening]
    own = mine[-1] if mine else opening
    other = theirs[-1]
    signs = _DIRECTIONS[side]
    near = _distance(own, other) <= strategy.near
    mirrored: list[Indices] = []
    for axis in range(_NUMERIC):
        moved = list(other)
        moved[axis] = _clamp(axis, moved[axis] + signs[axis])
        if tuple(moved) != other:
            mirrored.append(tuple(moved))
    for first, second in itertools.combinations(range(_NUMERIC), 2):
        moved = list(other)
        moved[first] = _clamp(first, moved[first] + signs[first])
        moved[second] = _clamp(second, moved[second] + signs[second])
        mirrored.append(tuple(moved))
    traded = _trade_candidates(side, mine, theirs, opening, strategy)
    seen = set(mine)
    candidates: list[Indices] = []
    for candidate in (mirrored + traded) if near else (traded + mirrored):
        if candidate not in candidates and candidate not in seen:
            candidates.append(candidate)
    return candidates or [own]


def _concede_candidates(
    side: Side, mine: list[Indices], theirs: list[Indices], opening: Indices, strategy: Strategy
) -> list[Indices]:
    """年収を strategy.step 段ずつ譲り、switch 回目の提案から他の軸も(1 回ごとに寄せる幅を増やしながら)寄せる。"""
    count = len(mine)
    sign = _DIRECTIONS[side][0]
    package = list(opening)
    package[0] = _clamp(0, opening[0] - sign * strategy.step * count)
    if theirs and count >= strategy.switch:
        other = theirs[-1]
        width = count - strategy.switch + 1
        for axis in range(1, _NUMERIC):
            if other[axis] != package[axis]:
                direction = 1 if other[axis] > package[axis] else -1
                package[axis] += direction * min(width, abs(other[axis] - package[axis]))
        package[_NUMERIC:] = other[_NUMERIC:]
    base = tuple(package)
    candidates = [base]
    for giveback in (1, 2):  # 年収を、自分側に有利な方へ 1〜2 段戻したもの
        backed_off = list(base)
        backed_off[0] = _clamp(0, backed_off[0] + sign * giveback)
        candidates.append(tuple(backed_off))
    return candidates


_CANDIDATES = {"trade": _trade_candidates, "hybrid": _hybrid_candidates, "concede": _concede_candidates}


# --- 1 手を決める ---


def decide(turn_input: TurnInput, negotiator: Negotiator) -> dict:
    """TurnInput だけから、次の Move(dict)を決める。"""
    side = turn_input.side
    strategy = negotiator.strategy
    pending = turn_input.pending_offer
    if pending is not None and pending.own_evaluation is Verdict.ACCEPTABLE:
        return move_dict("accept")

    opening = to_indices(negotiator.opening)
    mine, theirs = _proposals(turn_input)
    known = _known_evaluations(turn_input)
    budget = turn_input.budget
    chosen: Indices | None = None
    for candidate in _CANDIDATES[strategy.kind](side, mine, theirs, opening, strategy):
        verdict = _infer(known, side, candidate, strategy.monotone)
        if verdict is Verdict.ACCEPTABLE:
            chosen = candidate
            break
        if verdict is not None:
            continue
        if not strategy.check_first:
            chosen = candidate  # 確認せずに出す
            break
        if budget.remaining_evaluations <= budget.remaining_moves or _checks_this_turn(turn_input) >= strategy.max_checks:
            break  # 残りの評価は、残りの手数ぶんの提案のガードに取っておく
        return move_dict("check", to_package(candidate))

    if chosen is None:
        # 確認で見つからなかった。受けられると分かっている組み合わせのうち、相手の直前の提案にいちばん近いものを出す。
        acceptable = [p for p, v in known.items() if v is Verdict.ACCEPTABLE and p not in mine]
        if not acceptable:
            acceptable = [p for p, v in known.items() if v is Verdict.ACCEPTABLE] or [opening]
        target = theirs[-1] if theirs else opening
        chosen = min(acceptable, key=lambda p: _distance(p, target))
    return move_dict("propose", to_package(chosen))


class ScriptedNegotiators:
    """SendTurn(web.referee)の形の台本のエージェント。role(candidate・employer)ごとに、探し方と最初の手を持つ。

    LLM を呼ばず、通信もしない。TurnInput だけを見て Move の dict を返す。
    """

    def __init__(self, candidate: Negotiator, employer: Negotiator) -> None:
        self._negotiators = {"candidate": candidate, "employer": employer}

    async def __call__(self, role, turn_input, *, nid, timeout_s) -> dict:
        return decide(turn_input, self._negotiators[role])
