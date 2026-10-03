"""面談の API のリクエストの型(design.md §5)。

すべて pydantic の strict・extra="forbid"(negotiation_core.StrictModel と同じ設定。web.api_models と同じ)。依頼者の ID は、
リクエストには含めない(URL の pid が、セッションの依頼者 ID と同じことを確かめる)。
"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from negotiation_core import AXES, AXIS_KEYS, TwoChoiceResponse
from negotiation_core.policy import StrictModel

from web.interview.salary import SalaryBasis

# 文章の入力は、空でないこと(長さは、リクエスト本文の上限〔既定 32 KB〕が受け持つ)。
_Text = Annotated[str, StringConstraints(min_length=1)]


class BeginBody(StrictModel):
    """面談を始める。restart が true なら、途中の状態を捨てて、最初からやり直す。"""

    restart: bool = False


class ProfileBody(StrictModel):
    """プロフィール(§5 の 1)。正確な値を受け取り、その場で帯に変換して、正確な値は捨てる(§2.6)。"""

    experience_years: Annotated[float, Field(ge=0, le=80, allow_inf_nan=False)]
    prefecture: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    job_category: Annotated[str, StringConstraints(min_length=1, max_length=40)]


class SalaryAnswersBody(StrictModel):
    """年収の正規化の 3 問への自由記述の回答(§5 の 2)。質問と同じ順に、3 つ。"""

    answers: Annotated[list[_Text], Field(min_length=3, max_length=3)]


class SalaryConfirmBody(StrictModel):
    """換算の結果と前提を見た本人が、年収の定義を確かめる(§5 の 2)。読み取りが違っていれば、直した値を送る。"""

    salary_basis: SalaryBasis


class AxesBody(StrictModel):
    """外す軸(§5 の 3)。離散軸(年収以外)だけ。外さないなら空。"""

    removed_axes: list[str]

    @field_validator("removed_axes")
    @classmethod
    def _distinct_discrete_axes(cls, value: list[str]) -> list[str]:
        discrete = {axis for axis in AXIS_KEYS if AXES[axis].discrete}
        if any(axis not in discrete for axis in value) or len(set(value)) != len(value):
            raise ValueError("removed_axes must be distinct discrete axes")
        return value


class ChoiceAnswerBody(StrictModel):
    """二択への回答(§5 の 4)。pair_id の組の、option(a・b)の選択肢に対する、行く・行かない・迷う。"""

    pair_id: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    option: Literal["a", "b"]
    response: TwoChoiceResponse


class TextBody(StrictModel):
    """自由コメント・辞めた理由の文章(§5 の 4・5)。LLM に送る。送ったあとは、どこにも持たない。"""

    text: _Text


class EntryActiveBody(StrictModel):
    """確認画面の項目を、消す(false)・付け直す(true)(§5 の 6)。"""

    active: bool


class ConfirmBody(StrictModel):
    """平文での確認(§5 の 6)。受けるアンカーが 0 件のとき(I-1)は、そのまま進むことを、はっきり選ぶ。"""

    proceed_without_accept_anchors: bool = False


class BlocklistBody(StrictModel):
    """ブロック先の企業(§5 の 8)。企業の一覧の company_id から選ぶ。空にもできる。"""

    company_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]]
