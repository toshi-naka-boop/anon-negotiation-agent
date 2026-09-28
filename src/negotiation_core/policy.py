"""Package/Anchor/Policy and the 3-valued dominance evaluation (design.md §2.2, §2.7).

ポリシーは「受けるアンカー」と「受けないアンカー」の集合として持ち、支配関係で 3 値判定する。
Package（全 7 軸の値を持つ 1 点。§2.7）は、ここでのアンカー評価の対象そのものなのでここに置く。
"""

import itertools
from collections.abc import Iterator
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from negotiation_core.vocabulary import (
    AXES,
    AXIS_KEYS,
    CATEGORICAL_AXIS_KEYS,
    NUMERIC_AXIS_KEYS,
    NightDutyValue,
    RemoteDaysValue,
    ReviewMonthsValue,
    SalaryValue,
    Side,
    SideJobValue,
    StartValue,
    TrainingValue,
    goodness_rank,
)

Wildcard = Literal["*"]


class StrictModel(BaseModel):
    """このパッケージ内のスキーマ共通の設定(pydantic、extra="forbid"、strict。§2.7)。"""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class Package(StrictModel):
    """全 7 軸の値を持つ、グリッド上の 1 点(§2.1・§2.7)。数値はグリッド上、区分は列挙値。"""

    salary: SalaryValue
    remote_days: RemoteDaysValue
    night_duty: NightDutyValue
    review_months: ReviewMonthsValue
    training: TrainingValue
    side_job: SideJobValue
    start: StartValue


class Anchor(StrictModel):
    """全軸の値を持つ 1 つの組み合わせ(§2.2)。区分軸には「どれでもよい(*)」も書ける。"""

    salary: SalaryValue
    remote_days: RemoteDaysValue
    night_duty: NightDutyValue
    review_months: ReviewMonthsValue
    training: TrainingValue | Wildcard
    side_job: SideJobValue | Wildcard
    start: StartValue | Wildcard


class Verdict(str, Enum):
    """3 値判定の結果(§2.2・FR-10)。"""

    ACCEPTABLE = "acceptable"
    NOT_ACCEPTABLE = "not_acceptable"
    NEEDS_CONFIRMATION = "needs_confirmation"


class Policy(StrictModel):
    """依頼者 1 人ぶんのポリシー(§2.2)。書き込み時点で矛盾(同じ組み合わせが両方に
    当てはまる状態)を拒否する。"""

    side: Side
    accept_anchors: list[Anchor] = Field(default_factory=list)
    reject_anchors: list[Anchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_contradictions(self) -> "Policy":
        for accept_anchor in self.accept_anchors:
            for reject_anchor in self.reject_anchors:
                if is_contradictory(accept_anchor, reject_anchor, self.side):
                    raise ValueError(
                        "accept anchor and reject anchor contradict each other: "
                        f"{accept_anchor!r} vs {reject_anchor!r}"
                    )
        return self


def _numeric_at_least_as_good(higher: Anchor | Package, lower: Anchor | Package, side: Side) -> bool:
    """higher が、lower と比べて全数値軸で「以上に良い」(>=)かどうか(§2.2)。"""
    return all(
        goodness_rank(axis, getattr(higher, axis), side) >= goodness_rank(axis, getattr(lower, axis), side)
        for axis in NUMERIC_AXIS_KEYS
    )


def _categorical_compatible(a: Anchor | Package, b: Anchor | Package) -> bool:
    """全区分軸について、一致するか、どちらかが * であること(§2.2)。"""
    for axis in CATEGORICAL_AXIS_KEYS:
        value_a = getattr(a, axis)
        value_b = getattr(b, axis)
        if value_a != "*" and value_b != "*" and value_a != value_b:
            return False
    return True


def satisfies(package: Package, anchor: Anchor, side: Side) -> bool:
    """組み合わせ package が受けるアンカー anchor を満たすか(§2.2)。

    数値軸はすべて anchor 以上に良く、区分軸はすべて anchor と一致する(または anchor が *)。
    """
    return _numeric_at_least_as_good(package, anchor, side) and _categorical_compatible(package, anchor)


def contained_in(package: Package, anchor: Anchor, side: Side) -> bool:
    """組み合わせ package が受けないアンカー anchor に含まれるか(§2.2)。

    数値軸がすべて anchor 以下の良さで、区分軸がすべて一致する(または anchor が *)。
    """
    return _numeric_at_least_as_good(anchor, package, side) and _categorical_compatible(package, anchor)


def is_contradictory(accept_anchor: Anchor, reject_anchor: Anchor, side: Side) -> bool:
    """受けるアンカー accept_anchor と受けないアンカー reject_anchor が矛盾するか(§2.2)。

    accept_anchor が数値軸すべてで reject_anchor 以下の良さで、かつ区分軸が両立するなら矛盾。
    (accept_anchor 自身は、自分自身を満たすので「受けられる」でもあり、reject_anchor に
    含まれるなら「受けられない」でもある、という同時成立を検出する。)
    """
    return _numeric_at_least_as_good(reject_anchor, accept_anchor, side) and _categorical_compatible(
        accept_anchor, reject_anchor
    )


def iter_all_packages() -> Iterator[Package]:
    """グリッド上の全組み合わせ(暫定の語彙では 18,000 通り)を列挙する(§2.1)。"""
    grids = [AXES[axis].grid for axis in AXIS_KEYS]
    for combo in itertools.product(*grids):
        yield Package(**dict(zip(AXIS_KEYS, combo, strict=True)))


def evaluate(policy: Policy, package: Package) -> Verdict:
    """3 値判定(§2.2・FR-10)。

    受けるアンカーを 1 つでも満たせば ACCEPTABLE、受けないアンカーに 1 つでも
    含まれれば NOT_ACCEPTABLE、どちらでもなければ NEEDS_CONFIRMATION。
    """
    side = policy.side
    if any(satisfies(package, anchor, side) for anchor in policy.accept_anchors):
        return Verdict.ACCEPTABLE
    if any(contained_in(package, anchor, side) for anchor in policy.reject_anchors):
        return Verdict.NOT_ACCEPTABLE
    return Verdict.NEEDS_CONFIRMATION
