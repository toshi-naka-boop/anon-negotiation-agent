"""金庫が Firestore に持つ文書の形(design.md §3.1・§3.2・§3.7・§3.8)。

negotiation_core の Package・Policy・Verdict・EvaluatedPackage・CandidateAttributeBands を
そのまま使い、同じ処理を重複して書かない。ここに置くのは、金庫の状態機械・イベント列・
架空人物のテンプレート・依頼者文書の「形」だけ。
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from negotiation_core import CandidateAttributeBands, EvaluatedPackage, Package, Policy, Side, Verdict


class VaultModel(BaseModel):
    """金庫のモデル共通設定(negotiation_core の StrictModel に合わせる)。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- §3.1 状態機械 ---

NegotiationStatus = Literal["active", "awaiting_principal", "judged"]
EndReason = Literal[
    "agreed", "ended_by_agent", "stopped_budget", "stopped_invalid", "cancelled", "timeout"
]
NegotiationMode = Literal["live", "demo", "attack"]
Likelihood = Literal["high", "medium", "none"]

# vault の moves API が受け付ける手の種類。negotiation_core.MoveType(エージェントが出す
# propose/accept/reject/check/ask_principal/end)に、レフェリーが無効手を登録するための
# "invalid" を足したもの(§3.3: "move=invalid は、レフェリーが見つけた無効手の登録")。
VaultMoveKind = Literal["propose", "accept", "reject", "check", "ask_principal", "end", "invalid"]

# move="invalid" で外から登録できる理由。金庫自身のガードが見つける理由
# (off_grid・not_acceptable_to_own_principal・... )は、金庫がその場で自動的に付けるので、
# 外からの登録では受け付けない(§3.5 の表の最終行: "レフェリーが見つけた無効手
# (スキーマ違反、タイムアウト、A2A のエラー)を登録する")。
RegisteredInvalidReason = Literal["schema_invalid", "agent_timeout"]

# POST .../principal-answer が受け付ける回答(§3.3・§4.4)。「受ける」は受けるアンカー、
# 「受けない」は受けないアンカーへの追記に対応する。
PrincipalAnswerKind = Literal["accept", "reject"]


class SideCounters(VaultModel):
    """側ごとの回数(§3.1 counters)。両側で共有するものはない。"""

    evaluations_used: int = 0
    moves_used: int = 0
    principal_checks_used: int = 0
    consecutive_invalid: int = 0


class SeqBySide(VaultModel):
    """側ごとのイベント番号(§3.1 seq)。"""

    candidate: int = 0
    employer: int = 0


class CountersBySide(VaultModel):
    candidate: SideCounters = Field(default_factory=SideCounters)
    employer: SideCounters = Field(default_factory=SideCounters)


class LastCheckBySide(VaultModel):
    """側ごとの、直前に確認した組み合わせと自分側の評価(§3.1 last_check)。"""

    candidate: EvaluatedPackage | None = None
    employer: EvaluatedPackage | None = None


class PendingOffer(VaultModel):
    """§3.1 pending_offer。receiver_evaluation は受け手の側にしか見せない
    (view・TurnInput の組み立て側で side によって隠す。ここでは値そのものを持つ)。
    """

    by: Side
    package: Package
    receiver_evaluation: Verdict


class PendingQuestion(VaultModel):
    """§3.1 pending_question(awaiting_principal のとき)。"""

    side: Side
    package: Package


class NegotiationResult(VaultModel):
    """§3.1 result・§3.6 の判定結果。理由を含まない(AC-08)。"""

    likelihood: Likelihood
    package: Package | None = None


class Participant(VaultModel):
    """§3.1 participants の側ごとの 1 エントリ。

    is_fictional=False なら principal_id、True なら template_id を持つ。
    attribute_bands は候補者側だけ、job_id・company_id は求人側だけで使う
    (§3.7 の最終行により、1b-1 の範囲では求人側は常にテンプレート由来)。
    """

    is_fictional: bool
    principal_id: str | None = None
    template_id: str | None = None
    attribute_bands: CandidateAttributeBands | None = None
    job_id: str | None = None
    company_id: str | None = None


class Participants(VaultModel):
    candidate: Participant
    employer: Participant


class Snapshots(VaultModel):
    """§3.1 snapshots(両者のポリシーの交渉用コピー)。終了処理で None にする(FR-14)。"""

    candidate: Policy
    employer: Policy


class NegotiationDocument(VaultModel):
    """negotiations/{nid} 文書そのもの(§3.1)。"""

    nid: str
    status: NegotiationStatus
    paused: bool = False
    end_reason: EndReason | None = None
    version: int = 0
    seq: SeqBySide = Field(default_factory=SeqBySide)
    to_move: Side
    pending_offer: PendingOffer | None = None
    last_check: LastCheckBySide = Field(default_factory=LastCheckBySide)
    pending_question: PendingQuestion | None = None
    counters: CountersBySide = Field(default_factory=CountersBySide)
    created_at: dt.datetime
    expires_at: dt.datetime
    deadline: dt.datetime | None = None
    paused_at: dt.datetime | None = None
    snapshots: Snapshots | None
    participants: Participants
    result: NegotiationResult | None = None
    request_id: str
    mode: NegotiationMode
    ttl_at: dt.datetime | None = None


# --- §3.2 イベント列 ---

EventKind = Literal[
    "check",
    "propose",
    "offer_received",
    "invalid",
    "reject",
    "offer_rejected",
    "ask_principal",
    "principal_answer",
    "pause",
    "resume",
    "final_result",
]


class EventView(VaultModel):
    """イベント 1 件の、片側だけの見え方(§3.2 の表)。

    answer は principal_answer 専用(§3.2: 「回答と、評価し直した結果」の「回答」の部分。
    「評価し直した結果」は own_evaluation を使い回す)。reason は無効手の理由専用のまま
    (意味の異なる値を混在させない)。
    """

    seq: int
    kind: EventKind
    package: Package | None = None
    own_evaluation: Verdict | None = None
    reason: str | None = None
    answer: PrincipalAnswerKind | None = None
    result: NegotiationResult | None = None


class EventViews(VaultModel):
    candidate: EventView | None = None
    employer: EventView | None = None


class EventRecord(VaultModel):
    """negotiations/{nid}/events/{version} 文書(§3.2)。"""

    version: int
    views: EventViews
    ttl_at: dt.datetime | None = None


# --- §3.8 依頼者文書 ---


class EvaluationBudgetWindow(VaultModel):
    """本物の依頼者ごとの 24 時間の評価予算(§3.5)。固定窓: window_started_at から
    24 時間たったら used を 0 に戻して窓を今の時刻から張り直す(読み方は報告に記載)。
    """

    window_started_at: dt.datetime
    used: int = 0


class PrincipalDocument(VaultModel):
    """principals/{pid} 文書(§3.8)。

    attribute_bands は候補者側だけで使う(差し戻し対応: 台帳 I-2。求人側の本物の
    依頼者はハッカソンにはいないため、求人側のこの文書には書かれない)。ポリシーと
    一緒に PUT /v1/principals/{pid}/policy で保存し、交渉の作成時はここから読む
    (作成のたびに web から渡させない。交渉ごとに違う帯を渡せると、求人側の
    帯ごとのルールを探れてしまうため)。deleting は本人の削除(§3.8)の 1 段目で立てる
    印で、以後この依頼者が関わる principal-answer を拒否する(1b-2)。
    """

    policy: Policy | None = None
    removed_axes: list[str] = Field(default_factory=list)
    blocklist: list[str] = Field(default_factory=list)
    evaluation_budget: EvaluationBudgetWindow | None = None
    attribute_bands: CandidateAttributeBands | None = None
    deleting: bool = False


# --- §3.7 架空人物のテンプレート ---


class EmployerRule(VaultModel):
    """求人側ポリシーの 1 ルール(§2.2)。

    when は属性帯キー(experience_band・region_block・job_category の一部または全部)
    から値への対応。値が "*" のキー、または when に現れないキーは、そのキーについて
    無条件(どの値にもマッチ)。ルールは上から順に見て、最初に条件が合ったものを使う。
    """

    when: dict[str, str] = Field(default_factory=dict)
    policy: Policy


class CandidateTemplate(VaultModel):
    """架空の候補者(デモの候補者、攻撃モードの相手)のテンプレート(§3.7)。"""

    template_id: str
    side: Literal["candidate"] = "candidate"
    policy: Policy
    removed_axes: list[str] = Field(default_factory=list)
    attribute_bands: CandidateAttributeBands


class EmployerTemplate(VaultModel):
    """架空の求人(フィクスチャの求人)のテンプレート(§3.7)。"""

    template_id: str
    side: Literal["employer"] = "employer"
    company_id: str
    job_id: str
    rules: list[EmployerRule] = Field(default_factory=list)


Template = CandidateTemplate | EmployerTemplate
