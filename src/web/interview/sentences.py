"""平文の文(design.md §5 の 4・6、§2.3・§2.4)。

- describe_anchor: アンカーを、確認画面に出す 1 つの文にする(FR-03。例:「年収 650 万円以上・フル出社でもよい・当直なし・昇給見直しは
  6 か月以内なら行く」「当直が月 4 回以上なら(ほかの条件がどれだけ良くても)行かない」)。受けるアンカーは、§2.3 の規則で埋めた値も
  含めて見せる(本人が狭さを見て直せるように)。受けないアンカーは、制約していない軸(最も良い値)を略す。
- describe_statement: 発言 1 つを、面談エージェントがどう読み取ったかの文にする(触れた軸だけ)。
- describe_offer / display_value: 二択の選択肢の見せ方。

文面はすべて暫定(U-01)。設問そのものの文面は fixtures/interview_templates.toml に置くが、ここは確認画面の文の「形」(設計書の例の形)で、
数値と軸の組み立てが決まっているので、コードに置く。
"""

from collections.abc import Collection, Mapping
from typing import Any

from negotiation_core import AXES, CATEGORICAL_AXIS_KEYS, NUMERIC_AXIS_KEYS, PartialStatement, best_value, worst_value
from negotiation_core.vocabulary import AnchorType

REMOVED_MARK = "(外した軸。規則で埋めた値)"

# 区分軸の値の言い方(文の中)
_CATEGORICAL_PHRASE: dict[str, dict[str, str]] = {
    "training": {"none": "研修なし", "available": "研修あり"},
    "side_job": {"not_allowed": "副業不可", "allowed": "副業可"},
    "start": {
        "within_1_month": "入職は 1 か月以内",
        "within_3_months": "入職は 3 か月以内",
        "within_6_months": "入職は 6 か月以内",
    },
}
# 区分軸の値の言い方(二択の表の「値」の欄)
_CATEGORICAL_VALUE: dict[str, dict[str, str]] = {
    "training": {"none": "なし", "available": "あり"},
    "side_job": {"not_allowed": "不可", "allowed": "可"},
    "start": {"within_1_month": "1 か月以内", "within_3_months": "3 か月以内", "within_6_months": "6 か月以内"},
}
_ANY = "どちらでも"


def _n(value: float) -> str:
    """数値の表示(620.0 → 620)。"""
    return f"{value:g}"


def categorical_phrase(axis: str, value: str) -> str:
    """区分軸の値(*以外)を、文の中の言い方にする。"""
    return _CATEGORICAL_PHRASE[axis][value]


def numeric_boundary_phrase(axis: str, value: float, polarity: AnchorType) -> str:
    """数値軸の境目を、「〜なら行く」(accept)・「〜なら行かない」(reject)の言い方にする。

    受ける: 年収は「以上」、リモートは「以上」、当直・昇給見直しは「まで」「以内」(依頼者から見た、受ける最低ライン)。
    受けない: 年収は「以下」、リモートは「以下」、当直・昇給見直しは「以上」(受けない側の端)。
    """
    n = _n(value)
    accept = polarity == "accept"
    if axis == "salary":
        return f"年収 {n} 万円以上" if accept else f"年収 {n} 万円以下"
    if axis == "remote_days":
        if accept:
            if value == 0:
                return "フル出社でもよい"
            return "フルリモート" if value >= AXES[axis].grid[-1] else f"週 {n} 日以上のリモート"
        return "フル出社" if value == 0 else f"リモートが週 {n} 日以下"
    if axis == "night_duty":
        if accept:
            return "当直なし" if value == 0 else f"当直は月 {n} 回まで"
        return f"当直が月 {n} 回以上"
    if axis == "review_months":
        return f"昇給見直しは {n} か月以内" if accept else f"昇給見直しが {n} か月以上"
    raise ValueError(f"unknown numeric axis: {axis!r}")


def describe_anchor(polarity: AnchorType, raw: Mapping[str, Any], removed_axes: Collection[str] = ()) -> str:
    """アンカー(全軸の値)を、平文の 1 文にする。

    何も制約していない軸は略す(受けないアンカーの最も良い値。外した軸の中立の値 = 受けるアンカーの最も悪い値)。外した軸で、
    §2.3 の規則で埋めた値(中立でない値)が残っているときは、本人の意向ではないことが分かるよう、印(REMOVED_MARK)を付けて見せる。
    """
    parts: list[str] = []
    unconstrained: list[str] = []  # 「どちらでも」の区分軸
    for axis in NUMERIC_AXIS_KEYS:
        value = raw[axis]
        if polarity == "reject" and value == best_value(axis, "candidate"):
            continue  # 受けないアンカーで最も良い値の軸は、何も制約していない(ほかの条件がどれだけ良くても、の部分)
        if polarity == "accept" and axis in removed_axes and value == worst_value(axis, "candidate"):
            continue  # 外した軸の中立の値(二択の「行く」)は、何も制約していない。外した軸は、画面が別に示す
        parts.append(numeric_boundary_phrase(axis, value, polarity) + (REMOVED_MARK if axis in removed_axes else ""))
    for axis in CATEGORICAL_AXIS_KEYS:
        value = raw[axis]
        if value == "*":
            unconstrained.append(AXES[axis].label)
        else:
            parts.append(categorical_phrase(axis, value) + (REMOVED_MARK if axis in removed_axes else ""))
    if polarity == "accept":
        tail = f"({'・'.join(unconstrained)}は問いません)" if unconstrained else ""
        return "・".join(parts) + "なら行く" + tail
    if not parts:
        return "どんな条件でも行かない"
    return "・".join(parts) + "なら(ほかの条件がどれだけ良くても)行かない"


def describe_statement(statement: PartialStatement) -> str:
    """発言 1 つ(触れた軸だけ)を、面談エージェントの読み取りとして平文にする。"""
    parts = []
    for axis in NUMERIC_AXIS_KEYS:
        value = getattr(statement, axis)
        if value is not None:
            parts.append(numeric_boundary_phrase(axis, value, statement.polarity))
    for axis in CATEGORICAL_AXIS_KEYS:
        value = getattr(statement, axis)
        if value is not None:
            parts.append(categorical_phrase(axis, value))
    return "・".join(parts) + ("なら行く" if statement.polarity == "accept" else "なら行かない")


def display_value(axis: str, value: Any) -> str:
    """二択の選択肢の、軸ごとの値の表示(例: 650 → 650 万円、0 → フル出社、"*" → どちらでも)。"""
    if axis == "salary":
        return f"{_n(value)} 万円"
    if axis == "remote_days":
        if value == 0:
            return "フル出社"
        return "フルリモート" if value >= AXES[axis].grid[-1] else f"週 {_n(value)} 日"
    if axis == "night_duty":
        return "なし" if value == 0 else f"月 {_n(value)} 回"
    if axis == "review_months":
        return f"{_n(value)} か月後"
    return _ANY if value == "*" else _CATEGORICAL_VALUE[axis][value]


def describe_offer(values: Mapping[str, Any], removed_axes: Collection[str]) -> str:
    """二択の選択肢を、1 つの求人の条件として言い表す。外した軸と、問うていない区分軸(どちらでも)は略す。"""
    parts = [f"年収 {_n(values['salary'])} 万円"]
    for axis in NUMERIC_AXIS_KEYS:
        if axis == "salary" or axis in removed_axes:
            continue
        value = values[axis]
        if axis == "remote_days":
            parts.append("フル出社" if value == 0 else "フルリモート" if value >= AXES[axis].grid[-1] else f"週 {_n(value)} 日リモート")
        elif axis == "night_duty":
            parts.append("当直なし" if value == 0 else f"当直 月 {_n(value)} 回")
        else:
            parts.append(f"昇給見直し {_n(value)} か月後")
    for axis in CATEGORICAL_AXIS_KEYS:
        if axis not in removed_axes and values[axis] != "*":
            parts.append(categorical_phrase(axis, values[axis]))
    return "・".join(parts)
