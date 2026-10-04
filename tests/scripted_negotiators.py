"""台本の交渉エージェント(LLM を使わない。design.md §8.4・§12.2 の DV-14)。

交渉エージェント(Gemini)の代わりに、レフェリーの SendTurn の差し込み口に入れる。Gemini と同じく、TurnInput だけを見て
Plan・Move を返す: 手番ごとに状態を持たず、直前の呼び出しの記憶がない(履歴・相手の提案・確かめの結果は、すべて TurnInput から
読み直す)。見えない条件(依頼者のポリシー)は、確かめと評価の 3 値からしか知れない。
軸ごとの向き(どちらが自分の依頼者に良いか)は、TurnInput にないので、台本の側に持つ(指示文の「軸の向き」に当たる)。

v14(§4.1・§4.2)の計画・決定の形で動く。1 手番に最大 2 回呼ばれる。
- 計画(phase=plan): 確かめたい案を出したい順に並べて checks で返す(最大 max_checks 個)。確かめが要らないとき(相手の提案を
  受ける・確かめずに出す型・受けられると分かっている案がすぐ出せる・確かめに使える評価が残っていない)は、checks を空にして、
  手をそのまま返す(1 手番 1 回の呼び出し)。確かめは、レフェリーが金庫で実行する(エージェントは check という手を出せない)。
- 決定(phase=decide): レフェリーが確かめた結果(checked と、読み直された history・last_check)から、手を 1 つ選ぶ。

探し方は、7 巡目のシミュレーション(design/anon-negotiation-agent/reviews/round-7-sim/sim_case1.py の candidates_*・
play_turn_budgeted)を移植した。そのシミュレーションでは、確認の結果をエージェントが自分の記憶に持っていたが、ここでは
TurnInput の history・pending_offer・last_check・last_invalid から作り直す。そのため、確認せずに提案した手(無効手)の
記憶だけは、直前の 1 手ぶん(last_invalid)しか残らない。
- trade: 年収は差の半分、他の軸は 1 段ずつ一緒に譲る(7 巡目の表の 1 行目)
- hybrid: trade に加え、差が縮んだら相手の案を 1 段寄せて確かめる(2 行目)
- concede: 年収を 1 段ずつ譲り、switch 回目の提案から他の軸も寄せる(3 行目は switch=1、4 行目は switch=3)

手の選び方(シミュレーションの 1 手番と同じ)
1. 相手の提案の評価が「受けられる」なら accept する(計画で、手として返す)。
2. 候補の組み合わせ(探し方が決める)を順に見て、評価が「受けられる」と分かっているものを propose する。分からないものは、
   計画の checks に並べる(出したい順。「受けられる」と分かっている案が来たら、そこまで。先に確かめた案より悪い案は、結果が
   決まっているので並べない)。確かめに使える評価の数は、残りの評価回数 −(残りの手数 ＋ 残りの途中確認数)まで。1 手番に
   max_checks 個まで。レフェリーは、並びの順に確かめ、「受けられる」が出たら残りは確かめない。
3. 確かめで「受けられる」組み合わせが見つからなければ(決定)、受けられると分かっている組み合わせのうち、相手の直前の提案に
   いちばん近いものを propose する。
check_first=False の探し方は、確かめずに候補の先頭を propose する(確かめずに譲歩案を出す型。7 巡目の表の 6 行目)。
accepts=False の探し方は、受けられる提案が来ても accept せず、対案を出し続ける(交渉を終わらせない候補者。ケース 3 の攻撃で使う。
AC-12 の「候補者側が対案を返す台本」)。
途中確認(ask_principal)は使わない。ケース 1〜3 のポリシーは、受けられる・受けられないがすべて決まっていて、
「本人確認が必要」になる組み合わせがないので、使う場面がない。

攻撃者の台本(ScriptedAttacker。ケース 3。§8.2・§8.3): 攻撃モードの求人エージェント(role=attacker)の代わり。探索線(年収以外の軸を
固定した線)の上で、年収を二分探索する。交渉者の台本と違い、TurnInput だけでは候補者側の金庫の答えが分からないので、
候補者側のイベントを読む口(read_candidate_events)から答えを受け取る。金庫の答えをすべて見られる、最悪の場合の攻撃者を表す。
実体は src/web/attack/scripted.py(二分探索の実演の API が、同じ台本を LLM なしで動かす)。ここからは import で使う。
"""

import itertools
from dataclasses import dataclass
from typing import Literal

from negotiation_core import AXES, AXIS_KEYS, NUMERIC_AXIS_KEYS, Package, Side, TurnInput, Usage, Verdict
from web.attack.scripted import (  # noqa: F401  (ScriptedAttacker は、このファイルが使う。ほかは、ほかのテストが、ここから import する)
    SALARY_GRID,
    SEARCH_LINE,
    ScriptedAttacker,
    is_on_search_line,
    next_probe_salary,
    probe_package,
)
from web_helpers import make_usage, move_dict, plan_dict

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
    accepts: bool = True  # False なら、相手の提案が「受けられる」でも accept せず、対案を出す
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
    for checked in turn_input.checked:  # 決定の呼び出しで、レフェリーが確かめた結果(history・last_check にも出ているもの)
        if checked.evaluation is not None:
            known[to_indices(checked.package)] = checked.evaluation
    invalid = turn_input.last_invalid
    if invalid is not None and invalid.package is not None:
        # 確認せずに出して断られた組み合わせ。シミュレーションと同じく、「受けられるとは限らない」とだけ覚える。
        known[to_indices(invalid.package)] = Verdict.NEEDS_CONFIRMATION
    return known


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


# --- 1 手を決める(計画と決定) ---


@dataclass(frozen=True)
class _Scan:
    """候補の組み合わせを順に見た結果。"""

    chosen: Indices | None  # 「受けられる」と分かっている(または確かめずに出す)候補。なければ None
    to_check: list[Indices]  # chosen より前にある、評価の分からない候補(確かめたい順)


def _scan(turn_input: TurnInput, negotiator: Negotiator, *, check_limit: int) -> _Scan:
    """候補の組み合わせを順に見る。評価の分かっている候補は使い、分からない候補は、確かめたい案として check_limit 個まで集める。

    「受けられる」と分かっている候補が来たら、そこで止める(それが chosen)。確かめずに出す型(check_first=False)は、最初の
    分からない候補を、そのまま chosen にする。先に集めた案より悪い案(その案が「受けられない」なら、これも「受けられない」と
    決まる)は、確かめても結果が変わらないので集めない(monotone のとき)。check_limit に達したら、そこで止める。
    """
    side = turn_input.side
    strategy = negotiator.strategy
    opening = to_indices(negotiator.opening)
    mine, theirs = _proposals(turn_input)
    known = _known_evaluations(turn_input)
    to_check: list[Indices] = []
    for candidate in _CANDIDATES[strategy.kind](side, mine, theirs, opening, strategy):
        verdict = _infer(known, side, candidate, strategy.monotone)
        if verdict is Verdict.ACCEPTABLE:
            return _Scan(candidate, to_check)
        if verdict is not None:
            continue
        if not strategy.check_first:
            return _Scan(candidate, to_check)  # 確かめずに出す
        if len(to_check) >= check_limit:
            break  # 残りの評価は、残りの手数ぶんの提案のガードに取っておく
        if strategy.monotone and any(_dominates_for(side, earlier, candidate) for earlier in to_check):
            continue  # 先に確かめる案より悪い。その案が「受けられる」なら、そこで止まるので、確かめなくてよい
        to_check.append(candidate)
    return _Scan(None, to_check)


def _fallback(turn_input: TurnInput, negotiator: Negotiator) -> Indices:
    """確かめで見つからなかったとき: 受けられると分かっている組み合わせのうち、相手の直前の提案にいちばん近いもの。"""
    opening = to_indices(negotiator.opening)
    mine, theirs = _proposals(turn_input)
    known = _known_evaluations(turn_input)
    acceptable = [p for p, v in known.items() if v is Verdict.ACCEPTABLE and p not in mine]
    if not acceptable:
        acceptable = [p for p, v in known.items() if v is Verdict.ACCEPTABLE] or [opening]
    target = theirs[-1] if theirs else opening
    return min(acceptable, key=lambda p: _distance(p, target))


def _check_limit(turn_input: TurnInput, negotiator: Negotiator) -> int:
    """この手番で、確かめに使える評価の数。残りの評価回数のうち、残りの手数と途中確認の分(提案のガードなど)は取っておく。"""
    budget = turn_input.budget
    spare = budget.remaining_evaluations - (budget.remaining_moves + budget.remaining_principal_checks)
    return max(0, min(negotiator.strategy.max_checks, spare))


def plan(turn_input: TurnInput, negotiator: Negotiator) -> dict:
    """計画(phase=plan): TurnInput だけから、確かめたい案を出したい順に並べる(確かめが要らなければ、手をそのまま返す)。"""
    pending = turn_input.pending_offer
    if negotiator.strategy.accepts and pending is not None and pending.own_evaluation is Verdict.ACCEPTABLE:
        return plan_dict(move="accept")
    scan = _scan(turn_input, negotiator, check_limit=_check_limit(turn_input, negotiator))
    if scan.to_check:
        return plan_dict(checks=[to_package(candidate) for candidate in scan.to_check])
    chosen = scan.chosen if scan.chosen is not None else _fallback(turn_input, negotiator)
    return plan_dict(move="propose", package=to_package(chosen))


def choose(turn_input: TurnInput, negotiator: Negotiator) -> dict:
    """決定(phase=decide): レフェリーの確かめの結果(checked と、読み直された history・last_check)から、手を 1 つ選ぶ。

    確かめは計画でしか行えないので、ここでは、評価の分かっている候補だけを見る(評価の分からない候補は、確かめられなかった
    ものなので、飛ばして、受けられると分かっているものを探す)。
    """
    pending = turn_input.pending_offer
    if negotiator.strategy.accepts and pending is not None and pending.own_evaluation is Verdict.ACCEPTABLE:
        return move_dict("accept")
    scan = _scan(turn_input, negotiator, check_limit=0)
    chosen = scan.chosen if scan.chosen is not None else _fallback(turn_input, negotiator)
    return move_dict("propose", to_package(chosen))


class ScriptedNegotiators:
    """SendTurn(web.referee)の形の台本のエージェント。role(candidate・employer)ごとに、探し方と最初の手を持つ。

    LLM を呼ばず、通信もしない。TurnInput だけを見て、計画(phase=plan)なら Plan、決定(phase=decide)なら Move の dict を、
    使用量(usage。台本は LLM を呼ばないので固定の値)と一緒に返す。
    """

    def __init__(self, candidate: Negotiator, employer: Negotiator, *, usage: Usage | None = None) -> None:
        self._negotiators = {"candidate": candidate, "employer": employer}
        self._usage = usage if usage is not None else make_usage()

    async def __call__(self, role, turn_input, *, nid, timeout_s) -> tuple[dict, Usage]:
        negotiator = self._negotiators[role]
        payload = plan(turn_input, negotiator) if turn_input.phase == "plan" else choose(turn_input, negotiator)
        return payload, self._usage


# --- 攻撃者の台本(ケース 3。§8.2・§8.3) ---
# 攻撃者の台本(ScriptedAttacker・探索線)の実体は web.attack.scripted(二分探索の実演の API が使う。FR-45)。
# テストと run_demo.py は、これまでどおり、ここから import できる。


class ScriptedAttackNegotiators:
    """SendTurn(web.referee)の形の台本。攻撃モードの交渉の、候補者(role=candidate)と攻撃者(role=attacker)。

    候補者は、ScriptedNegotiators と同じ探し方(Negotiator)。使用量は固定の値(台本は LLM を呼ばない)。
    """

    def __init__(self, candidate: Negotiator, attacker: ScriptedAttacker, *, usage: Usage | None = None) -> None:
        self._candidate = candidate
        self._attacker = attacker
        self._usage = usage if usage is not None else make_usage()

    async def __call__(self, role, turn_input, *, nid, timeout_s) -> tuple[dict, Usage]:
        if role == "attacker":
            move, package = await self._attacker.next_move(nid)
            payload = plan_dict(move=move, package=package) if turn_input.phase == "plan" else move_dict(move, package)
        elif role == "candidate":
            payload = (
                plan(turn_input, self._candidate)
                if turn_input.phase == "plan"
                else choose(turn_input, self._candidate)
            )
        else:
            raise ValueError(f"an attack negotiation has no {role!r} agent")
        return payload, self._usage
