"""web の画面 API のリクエストの型(design.md §5 の手順 9・§6.3・§3.3)。

すべて pydantic の strict・extra="forbid"(negotiation_core.StrictModel と同じ設定)。設計書が API の
形を細かく決めていないので、最小限の形にした。

- 依頼者の ID・候補者の側・モードは、リクエストには含めない。ID はセッションのクッキーから、側は
  いつも候補者側(本物の依頼者は候補者側だけ。§6.1)、モードは live(デモ用は demo)で、サーバが決める。
- 面談の送信(InterviewSubmitRequest)は、すでに構造化された面談の結果だけを受け取る: 受けるアンカー・
  受けないアンカーの生の値(グリッド外もあり得る数値)、外した軸、属性帯(すでに帯に変換済み。§5 の手順 1)。
  丸め(§2.5)は web の決定的コード(negotiation_core.round_anchor)で行い、丸め済みの値だけを金庫に送る。
"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from negotiation_core import (
    AXES,
    AXIS_KEYS,
    CandidateAttributeBands,
    Package,
    Policy,
    round_anchor,
)
from negotiation_core.policy import StrictModel, Wildcard
from negotiation_core.vocabulary import SideJobValue, StartValue, TrainingValue

from vault.api_models import PutPolicyRequest
from vault.models import PrincipalAnswerKind

from web.config import DEFAULT_WEB_CONFIG

# 入力の大きさの上限(config/params.toml の [web.limits]。矛盾検査の計算量と保存量を抑えるための暫定値)。
_LIMITS = DEFAULT_WEB_CONFIG.limits

_REQUEST_ID = Annotated[str, StringConstraints(min_length=8, max_length=64)]
_TEMPLATE_ID = Annotated[str, StringConstraints(min_length=1, max_length=100)]


def _numeric_range(axis: str):
    grid = AXES[axis].grid
    return Field(ge=grid[0], le=grid[-1], allow_inf_nan=False)


class RawAnchor(StrictModel):
    """丸める前のアンカー(全軸の値を持つ)。数値軸はグリッド外の値もあり得る(範囲だけを検証する)。

    区分軸は、グリッドの値そのもの、または「どれでもよい(*)」(§2.2)。
    """

    salary: float = _numeric_range("salary")
    remote_days: float = _numeric_range("remote_days")
    night_duty: float = _numeric_range("night_duty")
    review_months: float = _numeric_range("review_months")
    training: TrainingValue | Wildcard
    side_job: SideJobValue | Wildcard
    start: StartValue | Wildcard


class InterviewSubmitRequest(StrictModel):
    """面談の送信の入力(§5 の手順 9)。丸める前のアンカーと属性帯を持つ。

    HTTP の本文としては受けない(旧 POST /v1/principals/{pid}/interview は、3 問・二択・確認・承認を経ずに金庫へ書けるので、公開面から外した。
    台帳 X-81)。面談の進行(web.interview.service)が、確認と「最悪ここまで」の承認のあとに作り、内部の submit_policy が受ける。
    """

    accept_anchors: list[RawAnchor] = Field(default_factory=list, max_length=_LIMITS.max_anchors_per_kind)
    reject_anchors: list[RawAnchor] = Field(default_factory=list, max_length=_LIMITS.max_anchors_per_kind)
    removed_axes: list[str] = Field(default_factory=list)
    attribute_bands: CandidateAttributeBands

    @field_validator("removed_axes")
    @classmethod
    def _removed_axes_are_distinct_discrete_axes(cls, value: list[str]) -> list[str]:
        """外せるのは離散軸だけ(§2.4・§5 の手順 3)。存在しない軸・年収・重複は受け付けない。"""
        discrete = {axis for axis in AXIS_KEYS if AXES[axis].discrete}
        if any(axis not in discrete for axis in value) or len(set(value)) != len(value):
            raise ValueError("removed_axes must be distinct discrete axes")
        return value

    def to_put_policy_request(self) -> PutPolicyRequest:
        """web で丸めて(§2.5)、金庫の PUT policy のリクエストにする。

        受けるアンカーは依頼者にとって良い側、受けないアンカーは悪い側の、最も近いグリッド点に寄せる。
        矛盾(同じ組み合わせが両方に当てはまる状態)は、Policy の検証が拒否する(ValueError)。
        """
        accept = [round_anchor(anchor.model_dump(), "accept", "candidate") for anchor in self.accept_anchors]
        reject = [round_anchor(anchor.model_dump(), "reject", "candidate") for anchor in self.reject_anchors]
        policy = Policy(side="candidate", accept_anchors=accept, reject_anchors=reject)
        return PutPolicyRequest(
            policy=policy, removed_axes=list(self.removed_axes), attribute_bands=self.attribute_bands
        )


class BlocklistRequest(StrictModel):
    """ブロック先の企業 ID(置き換え。§3.3・FR-07)。"""

    blocklist: list[Annotated[str, StringConstraints(min_length=1, max_length=_LIMITS.max_company_id_length)]] = Field(
        max_length=_LIMITS.max_blocklist_entries
    )


class CreateNegotiationBody(StrictModel):
    """本物の候補者が交渉を始める(§6.1)。求人(フィクスチャのテンプレート)を 1 件選ぶ。

    request_id は、画面が作る乱数(§3.5)。サーバが依頼者 ID を前に付けて金庫に渡すので、依頼者どうしで
    request_id が重なっても、他人の交渉の ID を返されることはない。
    """

    request_id: _REQUEST_ID
    employer_template_id: _TEMPLATE_ID


class PrincipalAnswerBody(StrictModel):
    """途中確認への回答(§4.4)。package は、本人が見た質問の組み合わせ(別の質問への回答にしないため)。"""

    package: Package
    answer: PrincipalAnswerKind


class ControlBody(StrictModel):
    """一時停止・再開・取消(§3.4)。"""

    action: Literal["pause", "resume", "cancel"]


class DemoCreateBody(StrictModel):
    """デモの交渉を、架空人物のテンプレートから作る(§3.7・§6.3)。

    依頼者の ID・モードは含めない(本物の依頼者には触れない。モードは demo 固定)。
    """

    request_id: _REQUEST_ID
    candidate_template_id: _TEMPLATE_ID
    employer_template_id: _TEMPLATE_ID
