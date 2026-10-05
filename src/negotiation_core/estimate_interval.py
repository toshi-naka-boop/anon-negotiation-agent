"""推定区間(design.md §8.3・FR-45): 金庫の 3 値の答えから、候補者の年収の境目が入る区間を計算する。

攻撃者が、他の軸を固定して年収だけを変えた提案(探索線の上の提案)を出し、候補者側の金庫が返した 3 値の評価
(受け手としての評価)を集めても、候補者の「この年収以上なら受ける」という境目(生の値。グリッド外もある)は、
グリッド上の隣り合う 2 点の間までしか絞れない。提案はグリッド上の値に限られ(FR-18)、金庫のポリシーは丸め済みで(§2.5)、
グリッド上の点の判定は丸める前と変わらないため。
- 「受けられる」(年収 s): 境目は s 以下
- 「受けられない」(年収 s): 境目は s より上
- 「本人確認が必要」: 情報なし(区間を変えない)
候補者側のエージェントが実際に受けたかどうか(LLM の手)は使わない。受けなかったことは「受けられない」を意味しないため。
呼び出し側(web のメーター)が、探索線の上の提案だけを選び、交渉をまたいで集めて渡す。この関数は、年収と評価の組だけを見る。

区間は (lower, upper]: 境目は lower より上、upper 以下。情報がない側は None(グリッドの外まで広がっている)。
区間は常に真の境目を含み、最後はグリッド 1 マス(例: 600〜650 万)で止まる。「受けられる」の最小の年収と「受けられない」の
最大の年収がグリッド上で隣り合っていれば 1 マスになる。その間に「本人確認が必要」の隙間があると、区間は広いまま止まる
(ケース 3 のフィクスチャは、探索線の上でこの隙間を持たない。§8.4)。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from negotiation_core.policy import Verdict
from negotiation_core.vocabulary import AXES

_GRID: tuple[int, ...] = AXES["salary"].grid
# 「受けられる」なら境目は提案値以下、というのは、候補者にとって年収が高いほど良いことが前提(語彙 §2.1)。
assert AXES["salary"].direction_for("candidate") == "higher_is_better"


@dataclass(frozen=True)
class Interval:
    """候補者の年収の境目が入る区間 (lower, upper](万円)。lower・upper はグリッド上の値。None は、その側の情報がない(グリッドの外まで広がる)。"""

    lower: int | None  # 境目はこの値より上(この値自身は含まない)
    upper: int | None  # 境目はこの値以下(この値自身を含む)

    @property
    def cells(self) -> int:
        """区間がまたぐグリッドのマスの数(1 以上)。1 なら、これ以上は狭まらない。情報がない側は、グリッドの外に 1 マス足して数える。"""
        low = -1 if self.lower is None else _GRID.index(self.lower)
        high = len(_GRID) if self.upper is None else _GRID.index(self.upper)
        return high - low

    def contains(self, boundary: float) -> bool:
        """boundary(生の境目の値)が区間に入るか。"""
        return (self.lower is None or self.lower < boundary) and (self.upper is None or boundary <= self.upper)


def estimate_interval(observations: Iterable[tuple[int, Verdict | str]]) -> Interval:
    """探索線の上の提案の年収と、候補者側の金庫の 3 値評価の組から、境目の区間を返す(§8.3)。

    observations: (提案の年収, 評価)の並び。評価は Verdict か、その値の文字列(イベントの own_evaluation)。順序は問わない。
    年収がグリッド外・評価が 3 値のどれでもない・答えどうしが食い違う(「受けられる」より高い年収が「受けられない」、など。
    候補者のポリシーは年収について単調なので、同じ探索線の上では起きない)ときは ValueError。
    """
    lower_index, upper_index = -1, len(_GRID)  # 情報がない状態
    for salary, verdict in observations:
        if salary not in _GRID:
            raise ValueError(f"salary {salary!r} is not on the grid")
        index = _GRID.index(salary)
        verdict = Verdict(verdict)
        if verdict is Verdict.ACCEPTABLE:
            upper_index = min(upper_index, index)
        elif verdict is Verdict.NOT_ACCEPTABLE:
            lower_index = max(lower_index, index)
    if lower_index >= upper_index:
        raise ValueError("the observations contradict each other: a salary is acceptable at or below one that is not")
    return Interval(
        lower=None if lower_index < 0 else _GRID[lower_index],
        upper=None if upper_index >= len(_GRID) else _GRID[upper_index],
    )
