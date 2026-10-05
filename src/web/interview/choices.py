"""パッケージ二択の生成(design.md §5 の 4・§2.4。FR-02)。

本人が言った年収(比較基準年収)の周辺で、5〜8 組の二択を、決定的に作る(同じ入力なら同じ組。LLM は使わない)。

規則
1. 組の雛形は fixtures/interview_templates.toml(U-01)。年収と 1 つの軸のトレードオフの組(トレードオフの軸が外れていないもの)を
   雛形の並びの順に取り、足りなければ年収だけを動かす組で埋めて、config の choice_pairs 組にする。
   年収だけの組が最少の組数以上あるので、すべての軸を外しても、最少の組数(5)に届く。
2. 年収は、本人の年収に最も近いグリッド点を土台にして、雛形の salary_steps だけ上下させる。グリッドの端でも、すべての選択肢が
   グリッドに収まるよう、土台を内側へずらす(選択肢の年収が重なったり、グリッドの外に出たりしない)。
3. 外した軸は、順序のある軸なら最も悪い値、順序のない区分軸なら「どちらでも(*)」で見せ、その旨を設問文に書く(§2.4)。
   問うていない区分軸も「どちらでも(*)」(§2.3 の最終行)。
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from negotiation_core import AXES, AXIS_KEYS, NUMERIC_AXIS_KEYS, worst_value

from web.interview.salary import nearest_grid_index
from web.interview.statements import shown_values
from web.interview.templates import InterviewTemplates, PairOption, PairTemplate


@dataclass(frozen=True)
class ChoicePair:
    """二択 1 組。a・b は、見せる値(全軸。外した軸は中立の値。shown_values)。"""

    id: str
    a: dict[str, Any]
    b: dict[str, Any]


def select_pair_templates(removed_axes: Collection[str], templates: InterviewTemplates, count: int) -> list[PairTemplate]:
    """出す組の雛形を選ぶ: トレードオフの軸が外れていない組、年収だけの組、の順に count 組まで。"""
    removed = set(removed_axes)
    trade_offs = [pair for pair in templates.pairs if pair.traded_axes and not pair.traded_axes <= removed]
    salary_only = [pair for pair in templates.pairs if not pair.traded_axes]
    return (trade_offs + salary_only)[:count]


def _values(option: PairOption, salary_index: int, removed_axes: Collection[str]) -> dict[str, Any]:
    values = {"salary": AXES["salary"].grid[salary_index + option.salary_steps]}
    for axis in AXIS_KEYS:
        if axis != "salary":
            values[axis] = getattr(option, axis)
    return shown_values(values, removed_axes)


def generate_pairs(
    base_salary_man_yen: float, removed_axes: Collection[str], templates: InterviewTemplates, count: int
) -> list[ChoicePair]:
    """本人の年収(万円)の周辺の二択を作る。外した軸は、中立の値で見せる。組の数は、雛形が足りる限り count。"""
    chosen = select_pair_templates(removed_axes, templates, count)
    steps = [option.salary_steps for pair in chosen for option in (pair.a, pair.b)]
    last = len(AXES["salary"].grid) - 1
    # すべての選択肢がグリッドに収まる位置まで、土台を内側へずらす(salary_steps は ±10 段までなので、暫定のグリッド〔25 点〕には必ず収まる位置がある)
    index = min(max(nearest_grid_index(base_salary_man_yen), -min(steps)), last - max(steps))
    return [
        ChoicePair(id=pair.id, a=_values(pair.a, index, removed_axes), b=_values(pair.b, index, removed_axes))
        for pair in chosen
    ]


def removed_axes_question(removed_axes: Collection[str], templates: InterviewTemplates) -> str:
    """設問文: 外した軸の見せ方の文(§2.4)を並べて、「この条件なら行きますか?」で結ぶ。外した軸がなければ、結びの文だけ。"""
    phrases = []
    for axis in AXIS_KEYS:
        if axis not in removed_axes:
            continue
        phrase = templates.two_choice.removed_axis_phrases[axis]
        phrases.append(phrase.format(value=worst_value(axis, "candidate")) if axis in NUMERIC_AXIS_KEYS else phrase)
    return ("、".join(phrases) + "、" if phrases else "") + templates.two_choice.closing
