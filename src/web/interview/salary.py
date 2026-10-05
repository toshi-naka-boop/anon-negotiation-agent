"""年収の正規化(design.md §5 の 2・§2.1。FR-01)。

面談エージェントが 3 問の自由記述から取り出した SalaryBasis を、決定的なコードで「比較基準年収」に換算する。
比較基準年収は、額面の年間総額から固定残業代を除き、賞与を含めたもの(§2.1)。

    比較基準年収 = 額面の年間総額(賞与を含む) − 固定残業代の年額(月額 × 12)

- 手取りで答えたときは、手取りが額面に占める割合(設定 net_to_gross_ratio。暫定 0.8)で、額面に直す。
- 月収で答えたときは、月収 × 12 に賞与(月収 × 賞与の月数)を足す。年収で答えたとき、賞与が含まれていなければ、年収 ÷ 12 × 賞与の月数を足す。
- 固定残業代は、額面の金額として年額(月額 × 12)を除く。
換算の式と、その前提(どう読んだか)の文を、画面に出して、本人に確かめてもらう。本人は SalaryBasis の各項目を直せる。

SalaryBasis は LLM の出力の型。応答スキーマは使わず JSON モードで出させて(台帳 I-19)、この型で検証する。
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from negotiation_core import AXES
from negotiation_core.policy import StrictModel

_YEARS_AND_MONTHS = 12


class SalaryBasis(StrictModel):
    """3 問の回答から取り出した、年収の定義(額面か手取りか・固定残業代・賞与の月数。§5 の 2)。金額は万円。

    amount_man_yen は、本人が答えた金額(amount_period が annual なら年額、monthly なら月額)。
    bonus_included は、amount_man_yen に賞与がすでに含まれているか(monthly では、賞与は含まれない扱い)。
    bonus_months は、賞与が年に月給の何か月分か(ないなら 0)。fixed_overtime_man_yen_per_month は、固定残業代の月額(ないなら 0)。
    """

    amount_man_yen: Annotated[float, Field(gt=0, le=100_000, allow_inf_nan=False)]
    amount_period: Literal["annual", "monthly"]
    amount_kind: Literal["gross", "net"]
    bonus_included: bool
    bonus_months: Annotated[float, Field(ge=0, le=24, allow_inf_nan=False)]
    fixed_overtime_man_yen_per_month: Annotated[float, Field(ge=0, le=1_000, allow_inf_nan=False)]


@dataclass(frozen=True)
class NormalizedSalary:
    """換算の結果。formula は換算式(数字入り)、assumptions は前提の文(どう読んだか)。"""

    man_yen: float
    formula: str
    assumptions: tuple[str, ...]


class SalaryConversionError(ValueError):
    """換算の結果が使えない(固定残業代が大きすぎて、比較基準年収が 0 以下になるなど)。"""


def _n(value: float) -> str:
    """金額・月数の表示(余計な 0 を付けない。丸めは小数 2 桁まで)。"""
    return f"{round(value, 2):g}"


def normalize_salary(basis: SalaryBasis, net_to_gross_ratio: float) -> NormalizedSalary:
    """SalaryBasis を、比較基準年収(万円)に換算する。結果が 0 以下なら SalaryConversionError。"""
    assumptions: list[str] = []
    amount = basis.amount_man_yen
    period_text = "年収(1 年分)" if basis.amount_period == "annual" else "月収(1 か月分)"

    # 1. 額面にそろえる
    if basis.amount_kind == "net":
        gross = amount / net_to_gross_ratio
        assumptions.append(
            f"答えた {_n(amount)} 万円は、{period_text}の手取りとして扱いました。手取りは額面のおよそ "
            f"{_n(net_to_gross_ratio * 100)}% と仮定して、額面 {_n(gross)} 万円に直しました。"
        )
    else:
        gross = amount
        assumptions.append(f"答えた {_n(amount)} 万円は、{period_text}の額面(税引き前)として扱いました。")

    # 2. 年間総額(賞与を含む)にする
    if basis.amount_period == "monthly":
        monthly = gross
        annual_base = monthly * _YEARS_AND_MONTHS
        bonus = monthly * basis.bonus_months
        assumptions.append(
            f"月収 {_n(monthly)} 万円 × {_YEARS_AND_MONTHS} か月 = {_n(annual_base)} 万円に、賞与 年 {_n(basis.bonus_months)} か月分"
            f"({_n(bonus)} 万円)を足しました。"
        )
    elif basis.bonus_included:
        annual_base, bonus = gross, 0.0
        assumptions.append("賞与は、答えた金額にすでに含まれているものとして扱いました。")
    else:
        monthly = gross / _YEARS_AND_MONTHS
        annual_base = gross
        bonus = monthly * basis.bonus_months
        assumptions.append(
            f"賞与は答えた金額に含まれていないものとして、年収 ÷ {_YEARS_AND_MONTHS} か月 = {_n(monthly)} 万円の "
            f"{_n(basis.bonus_months)} か月分({_n(bonus)} 万円)を足しました。"
        )
    gross_total = annual_base + bonus

    # 3. 固定残業代を除く
    overtime_annual = basis.fixed_overtime_man_yen_per_month * _YEARS_AND_MONTHS
    if overtime_annual > 0:
        assumptions.append(
            f"固定残業代は月 {_n(basis.fixed_overtime_man_yen_per_month)} 万円(年 {_n(overtime_annual)} 万円)として、除きました。"
        )
    else:
        assumptions.append("固定残業代は、ないものとして扱いました。")

    value = round(gross_total - overtime_annual, 2)
    if value <= 0:
        raise SalaryConversionError("the normalized salary is not positive")
    formula = (
        f"比較基準年収 = 額面の年間総額(賞与を含む){_n(gross_total)} 万円 − 固定残業代の年額 {_n(overtime_annual)} 万円"
        f" = {_n(value)} 万円"
    )

    # 4. グリッドの範囲の外なら、二択の質問を作るときに端へ寄せることを伝える
    grid = AXES["salary"].grid
    if value < grid[0]:
        assumptions.append(f"年収の範囲({grid[0]}〜{grid[-1]} 万円)より低いので、質問は {grid[0]} 万円の周辺で作ります。")
    elif value > grid[-1]:
        assumptions.append(f"年収の範囲({grid[0]}〜{grid[-1]} 万円)より高いので、質問は {grid[-1]} 万円の周辺で作ります。")
    return NormalizedSalary(man_yen=value, formula=formula, assumptions=tuple(assumptions))


def nearest_grid_index(value: float) -> int:
    """年収 value(万円)に最も近いグリッド点の位置(同じ近さなら高い方。範囲の外は端)。"""
    grid = AXES["salary"].grid
    return min(range(len(grid)), key=lambda i: (abs(grid[i] - value), -grid[i]))
