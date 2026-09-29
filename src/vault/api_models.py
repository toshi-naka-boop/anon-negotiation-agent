"""FastAPI のリクエスト・レスポンス DTO(design.md §3.3 の API のうち、1b-1・1b-2 で作る分)。

内部 HTTP・JSON。呼べるのは web だけという前提(§3.3)なので、認可はここでは行わない
(§6.3 の対象ごとの権限確認は web 側の仕事。1b-1・1b-2 の範囲外)。
"""

import datetime as dt
from typing import Literal

from pydantic import Field, model_validator

from negotiation_core import (
    Budget,
    CandidateAttributeBands,
    EvaluatedPackage,
    JobCategoryInfo,
    Package,
    Policy,
    Side,
)

from vault.models import (
    EventKind,
    Likelihood,
    NegotiationMode,
    NegotiationResult,
    NegotiationStatus,
    PrincipalAnswerKind,
    RegisteredInvalidReason,
    VaultModel,
    VaultMoveKind,
)

# --- PUT/GET .../policy ---


class PutPolicyRequest(VaultModel):
    """PUT /v1/principals/{pid}/policy(§3.3)。

    グリッド外・矛盾の拒否は negotiation_core.Policy 自身の検証(pydantic)にまかせる
    (二重に書かない)。attribute_bands は候補者側の依頼者について、ポリシーと一緒に
    保存する(台帳 I-2)。省略した場合は、すでに保存済みの値をそのまま残す(消さない)。
    """

    policy: Policy
    removed_axes: list[str] = Field(default_factory=list)
    attribute_bands: CandidateAttributeBands | None = None


class PolicyView(VaultModel):
    """GET /v1/principals/{pid}/policy の応答。本人向けの表示のために帯も返す。"""

    policy: Policy
    removed_axes: list[str]
    attribute_bands: CandidateAttributeBands | None = None


# --- PUT .../blocklist ---


class PutBlocklistRequest(VaultModel):
    """PUT /v1/principals/{pid}/blocklist(§3.3)。候補者のブロック先(企業 ID)を置き換える。"""

    blocklist: list[str] = Field(default_factory=list)


# --- POST /v1/negotiations ---


class CandidateParticipantRequest(VaultModel):
    """交渉作成時の候補者側の指定(§3.1 participants)。

    is_fictional=False(本物)なら principal_id を指定する。属性帯はここでは受け付けない
    (台帳 I-2: 交渉ごとに違う帯を渡せると、求人側の帯ごとのルールを探れてしまうため。
    金庫が principals/{pid} に保存済みの帯を読む。extra="forbid" により、それでも
    attribute_bands を送ると 422 になる)。
    is_fictional=True(架空人物)なら template_id を指定する。属性帯はテンプレート自身が
    持つものを使う(§3.7)。
    """

    is_fictional: bool
    principal_id: str | None = None
    template_id: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "CandidateParticipantRequest":
        if self.is_fictional:
            if self.template_id is None:
                raise ValueError("is_fictional candidate requires template_id")
        else:
            if self.principal_id is None:
                raise ValueError("real candidate requires principal_id")
        return self


class EmployerParticipantRequest(VaultModel):
    """交渉作成時の求人側の指定。

    design.md §3.7 の最終行(「ハッカソンでは求人はすべてフィクスチャなので、求人側は
    いつもテンプレートから写す」)により、1b-1 の実装範囲では求人側は常にテンプレートから
    作る(本物の求人の経路は、実装しない・テストしない機能として持たない)。
    """

    template_id: str


class CreateNegotiationRequest(VaultModel):
    """POST /v1/negotiations(§3.5)。"""

    request_id: str = Field(min_length=1, max_length=200)
    mode: NegotiationMode
    candidate: CandidateParticipantRequest
    employer: EmployerParticipantRequest

    @model_validator(mode="after")
    def _mode_matches_candidate(self) -> "CreateNegotiationRequest":
        # live は本物の候補者、demo/attack は架空人物(§6.3: デモ・攻撃モードは必ず
        # テンプレートからのコピーを使う)。この対応を金庫側でも確かめる(最も保守的な読み方。
        # 報告の 4 に記載)。
        expected_fictional = self.mode != "live"
        if self.candidate.is_fictional != expected_fictional:
            raise ValueError(
                f"mode={self.mode!r} requires candidate.is_fictional={expected_fictional!r}"
            )
        return self


CreationRefusalReason = Literal[
    "already_active", "budget_exhausted", "blocked", "attribute_bands_missing", "principal_deleting"
]


class CreateNegotiationResponse(VaultModel):
    status: Literal["created", "refused"]
    nid: str | None = None
    version: int | None = None
    reason: CreationRefusalReason | None = None


# --- GET .../view ---


class NegotiationViewResponse(VaultModel):
    """GET /v1/negotiations/{nid}/view?side=(§3.3)。相手側の評価・確認結果・残りは含めない。

    counterparty は TurnInput.counterparty(§2.7)の元(1d-1 で追加。台帳 I-2: 候補者の属性帯は
    金庫にしかないため、web が組み立てるには金庫が返す必要がある)。
    求人側(side=employer)には候補者の属性帯、候補者側(side=candidate)には公開求人の区分情報を返す。
    """

    status: NegotiationStatus
    to_move: Side
    paused: bool
    counterparty: CandidateAttributeBands | JobCategoryInfo
    pending_offer: EvaluatedPackage | None
    last_check: EvaluatedPackage | None
    awaiting_principal_package: Package | None
    budget: Budget
    deadline: dt.datetime | None
    expires_at: dt.datetime
    version: int
    result: NegotiationResult | None


# --- GET .../events ---


class EventViewItem(VaultModel):
    seq: int
    kind: EventKind
    package: Package | None = None
    own_evaluation: str | None = None
    reason: str | None = None
    answer: PrincipalAnswerKind | None = None
    result: NegotiationResult | None = None


# --- POST .../moves ---


class MoveRequest(VaultModel):
    """POST /v1/negotiations/{nid}/moves(§3.3・§3.5)。"""

    expected_version: int = Field(ge=0)
    side: Side
    move: VaultMoveKind
    package: Package | None = None
    reason: RegisteredInvalidReason | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "MoveRequest":
        if self.move in ("propose", "check", "ask_principal") and self.package is None:
            raise ValueError(f"move={self.move!r} requires a package")
        if self.move == "invalid":
            if self.reason is None:
                raise ValueError("move='invalid' requires a reason")
        elif self.reason is not None:
            raise ValueError(f"move={self.move!r} must not include reason")
        return self


class MoveResponse(VaultModel):
    version: int
    status: NegotiationStatus
    valid: bool
    error: str | None = None
    end_reason: str | None = None


# --- POST .../principal-answer ---


class PrincipalAnswerRequest(VaultModel):
    """POST /v1/negotiations/{nid}/principal-answer(§3.3・§4.4)。

    pending_question と一致するとき(status・side・package のすべて)だけ受け付ける
    (手の操作と同じ扱いで、不一致は 409。store.process_principal_answer を参照)。
    """

    expected_version: int = Field(ge=0)
    side: Side
    package: Package
    answer: PrincipalAnswerKind


class PrincipalAnswerResponse(VaultModel):
    version: int
    status: NegotiationStatus
    end_reason: str | None = None


# --- POST .../control ---


class ControlRequest(VaultModel):
    side: Side
    action: Literal["pause", "resume", "cancel"]


class ControlResponse(VaultModel):
    version: int
    status: NegotiationStatus
    paused: bool


# --- POST .../expire ---


class ExpireResponse(VaultModel):
    version: int
    status: NegotiationStatus
    expired: bool


# --- 一覧 ---


class PrincipalNegotiationSummary(VaultModel):
    nid: str
    job_id: str
    created_at: dt.datetime
    state: Literal["active", "paused", "ended"]
    result: NegotiationResult | None = None


class OpenNegotiationSummary(VaultModel):
    """見回り用の一覧の 1 件(§3.3)。

    mode と candidate_principal_id は 1d-1 で追加した。見回り(§4.1)がタスクと段階開示の状態
    (stages/{nid}。本物の候補者の依頼者 ID を持つ。§6.2)を作り直すときに、web が持っていない
    交渉の性質(レフェリーが呼ぶエージェントの種類は mode で決まる)と、本物の候補者の依頼者 ID を
    金庫から取るため。candidate_principal_id は、候補者が架空人物なら None。
    """

    nid: str
    status: Literal["active", "awaiting_principal"]
    paused: bool
    deadline: dt.datetime | None
    expires_at: dt.datetime
    mode: NegotiationMode
    candidate_principal_id: str | None


class OpenNegotiationsPage(VaultModel):
    items: list[OpenNegotiationSummary]
    next_cursor: str | None = None


__all__ = [
    "CandidateParticipantRequest",
    "ControlRequest",
    "ControlResponse",
    "CreateNegotiationRequest",
    "CreateNegotiationResponse",
    "CreationRefusalReason",
    "EmployerParticipantRequest",
    "EventViewItem",
    "ExpireResponse",
    "Likelihood",
    "MoveRequest",
    "MoveResponse",
    "NegotiationViewResponse",
    "OpenNegotiationSummary",
    "OpenNegotiationsPage",
    "PolicyView",
    "PrincipalAnswerRequest",
    "PrincipalAnswerResponse",
    "PrincipalNegotiationSummary",
    "PutBlocklistRequest",
    "PutPolicyRequest",
]
