"""Turning statements and two-choice answers into anchors, and removing an axis.

(design.md §2.3 部分的な発言をアンカーに直す規則、§2.4 軸を外す、§5 手順 4 パッケージ二択)

面談の自由コメントなどから出てくる発言は、一部の軸にしか触れないことが多い。触れていない軸は
§2.3 の規則で埋める。軸を「外す」(§2.4)場合は、二択で外した軸を最悪値(順序のある軸)または
「どちらでも」(順序のない区分軸)として見せる。「行く/行かない/迷う」・自由コメントを
保存するかどうか、保存するならどの値で保存するかは、§2.4 の表の規則にそのまま従う
(convert_two_choice_answer_to_anchor・convert_statement_to_anchor の removed_axis 引数)。

ここでは、発言・二択の回答をすでに軸ごとに構造化したもの(PartialStatement・二択の組み合わせ)
を受け取る。自由文からこの構造を取り出す処理(LLM を使う)は web/agents 側の仕事で、ここには
含まない。どの入力を保存しなかったかは、戻り値が None かどうかで分かる(画面表示は web の仕事)。
"""

from typing import Literal

from pydantic import Field

from negotiation_core.policy import Anchor, StrictModel, Wildcard
from negotiation_core.rounding import round_anchor
from negotiation_core.vocabulary import (
    AXES,
    CATEGORICAL_AXIS_KEYS,
    NUMERIC_AXIS_KEYS,
    AnchorType,
    NightDutyValue,
    RemoteDaysValue,
    ReviewMonthsValue,
    SalaryValue,
    Side,
    SideJobValue,
    StartValue,
    TrainingValue,
    best_value,
    worst_value,
)

TwoChoiceResponse = Literal["go", "no_go", "undecided"]  # 「行く」/「行かない」/「迷う」(§5 手順4)


class PartialStatement(StrictModel):
    """発言から取り出した、軸ごとの言及内容(§2.3)。触れていない軸は None のままにする。

    数値軸は、丸め前の生の値(グリッド外もあり得る)を持てるよう、グリッドの最小・最大の
    範囲だけで緩く検証する(グリッドちょうどへの丸めは §2.5 の round_anchor が行う)。
    区分軸は、値そのものがグリッド(=取り得る値の全体)なので、列挙値のまま検証する。
    """

    polarity: AnchorType  # 「〜なら行く」(accept) / 「〜なら行かない」(reject)
    salary: float | None = Field(default=None, ge=AXES["salary"].grid[0], le=AXES["salary"].grid[-1])
    remote_days: float | None = Field(
        default=None, ge=AXES["remote_days"].grid[0], le=AXES["remote_days"].grid[-1]
    )
    night_duty: float | None = Field(
        default=None, ge=AXES["night_duty"].grid[0], le=AXES["night_duty"].grid[-1]
    )
    review_months: float | None = Field(
        default=None, ge=AXES["review_months"].grid[0], le=AXES["review_months"].grid[-1]
    )
    training: TrainingValue | None = None
    side_job: SideJobValue | None = None
    start: StartValue | None = None


def convert_statement_to_anchor(
    statement: PartialStatement, side: Side, removed_axis: str | None = None
) -> Anchor | None:
    """発言をアンカーに直す(§2.3)。外した軸に触れる発言は保存しない(§2.4 の表)。

    removed_axis に触れている発言(その軸の値が None でない)は None を返す(保存しない)。
    触れていなければ、これまでどおり §2.3 の規則で埋める(埋めた値は本人の意向ではなく
    規則によるものなので、removed_axis について中立になり、意向は漏れない)。
    触れていない数値軸は依頼者にとって最も良い値、触れていない区分軸は * で埋めてから、
    §2.5 の規則でグリッドに丸める(数値軸が触れられていても、丸め前の生の値のことがあるため)。
    """
    if removed_axis is not None and getattr(statement, removed_axis) is not None:
        return None  # §2.4: 外した軸に触れる発言は保存しない(その軸についての意向を含むため)

    raw_values: dict = {}
    for axis in NUMERIC_AXIS_KEYS:
        value = getattr(statement, axis)
        raw_values[axis] = value if value is not None else best_value(axis, side)
    for axis in CATEGORICAL_AXIS_KEYS:
        value = getattr(statement, axis)
        raw_values[axis] = value if value is not None else "*"
    return round_anchor(raw_values, statement.polarity, side)


def neutral_fill_value_for_removal(
    axis: str, side: Side
) -> SalaryValue | RemoteDaysValue | NightDutyValue | ReviewMonthsValue | Wildcard:
    """軸 axis を外したとき、二択で見せる/「行く」の保存に使う値(§2.4)。

    順序のある軸(数値軸)は axis について中立になる最も悪い値、順序のない区分軸は
    「どちらでも(*)」。
    """
    if AXES[axis].kind == "numeric":
        return worst_value(axis, side)
    return "*"


def apply_axis_removal(base_values: dict, removed_axis: str, side: Side) -> dict:
    """base_values(二択で示した全軸の組み合わせ)のうち、外した軸だけを
    neutral_fill_value_for_removal の値に差し替える(§2.4)。

    二択の組み合わせはもともとグリッド上の値なので、差し替えた結果をそのまま
    Anchor(**apply_axis_removal(...)) として使える(丸めは不要)。
    """
    updated = dict(base_values)
    updated[removed_axis] = neutral_fill_value_for_removal(removed_axis, side)
    return updated


def _default_unspecified_categorical_axes(values: dict) -> dict:
    """二択の設問が問うていない区分軸を、既定で * にする(§2.3 最終行)。"""
    filled = dict(values)
    for axis in CATEGORICAL_AXIS_KEYS:
        if filled.get(axis) is None:
            filled[axis] = "*"
    return filled


def convert_two_choice_answer_to_anchor(
    package_values: dict,
    response: TwoChoiceResponse,
    side: Side,
    removed_axis: str | None = None,
) -> tuple[Anchor, AnchorType] | None:
    """パッケージ二択の回答をアンカーに直す(§5 手順 4、§2.4 の保存の規則、§2.3 最終行)。

    package_values は、二択で示した組み合わせ(外す前の、軸を外していなければそのまま見せる
    値)。外した軸があるときは、この関数の中で neutral_fill_value_for_removal の値に
    差し替える(呼び出し側は、表示用にあらかじめ差し替えておく必要はない)。

    保存しないときは None を、保存するときは (アンカー, "accept" または "reject") を返す。

    軸を外していないとき(§5 手順 4): 「行く」は受けるアンカー、「行かない」は受けないアンカー、
    「迷う」は保存しない。
    軸を外しているとき(§2.4 の表): 「行く」は、外した軸を中立にした受けるアンカーとして
    保存する。「行かない」は保存しない(その軸についての意向を含むため)。「迷う」は保存しない。
    """
    if response == "undecided":
        return None

    values = dict(package_values)
    if removed_axis is not None:
        values = apply_axis_removal(values, removed_axis, side)
        if response == "no_go":
            return None  # §2.4: 外した軸があるときの「行かない」は保存しない

    values = _default_unspecified_categorical_axes(values)
    anchor_type: AnchorType = "accept" if response == "go" else "reject"
    return Anchor(**values), anchor_type
