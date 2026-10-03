"""A2A message schemas exchanged with negotiation agents (design.md §2.7).

pydantic, extra="forbid", strict。ID はシステムが乱数で作る 16 桁の 16 進数とし、
A2A メッセージの metadata に載せる。LLM に渡す入力(TurnInput 等)には含めない
(受信側の AgentExecutor が、LLM に渡す前に取り除く。§2.7・§4.3)。
ID を実際に使う受信口・vault の API は、1a では作らないので、ここでは形式(パターン)
だけを公開する。
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from negotiation_core.policy import Package, StrictModel, Verdict
from negotiation_core.vocabulary import ExperienceBandValue, JobCategoryValue, RegionBlockValue, Side

# --- ID の扱い(§2.7) ---
ID_PATTERN = r"^[0-9a-f]{16}$"
Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]


# 金庫の手の種類(履歴・金庫の API で使う)。check は、v14 からはレフェリーが計画の中で登録する手(§4.1)。
MoveType = Literal["propose", "accept", "reject", "check", "ask_principal", "end"]
# エージェントが出せる手。check は含まない(確かめは Plan.checks でしか行えない。§2.7・台帳 X-45)。
AgentMoveType = Literal["propose", "accept", "reject", "ask_principal", "end"]
# 呼び出しの種類(§4.1)。plan は計画(確かめたい案を出す)、decide は決定(確かめの結果を見て手を 1 つ出す)。
Phase = Literal["plan", "decide"]

# 直前の自分の手が無効だった理由(§2.7)。グリッド外の値は Package の検証で落ちるので schema_invalid になる(off_grid という理由はない。台帳 L16-2)。
LastErrorReason = Literal[
    "not_acceptable_to_own_principal",
    "no_pending_offer",
    "question_not_applicable",
    "question_budget_exhausted",
    "evaluation_budget_exhausted",
    "schema_invalid",
    "agent_timeout",
    "output_truncated",  # 出力が max_output_tokens で切れた(§2.7・台帳 C-53)
]

# 計画で出せる確かめの数の上限(§2.7)
MAX_PLANNED_CHECKS = 3


class EvaluatedPackage(StrictModel):
    """組み合わせと、自分側の 3 値評価(pending_offer・last_check で使う。§2.7)。"""

    package: Package
    own_evaluation: Verdict


class CheckedPackage(StrictModel):
    """TurnInput.checked の 1 件: 計画した確かめの結果(§2.7)。

    計画の順に並べる。evaluation が None なら、確かめなかった(残りの評価を残した・先に「受けられる」が出た)。
    """

    package: Package
    evaluation: Verdict | None


class Usage(StrictModel):
    """1 回の LLM 呼び出しの使用量(受信口 → レフェリー。A2A の artifact の metadata に載せる。§4.3・台帳 X-58)。

    数だけを持ち、入力・出力の中身は含まない。requests は、その呼び出しで Vertex AI に送った要求の数
    (クライアント側の自動再試行を切るので、通常は 1。§4.2)。
    """

    model: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    prompt_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    thoughts_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    requests: int = Field(ge=1)


class HistoryEntry(StrictModel):
    """TurnInput.history の 1 件(自分または相手の過去の手。§2.7)。

    イベント列のその側の見え方から作る(相手側の確認手・途中確認・評価は含まれない)。
    """

    by: Literal["self", "counterparty"]
    move: MoveType
    package: Package
    result: Verdict


class Budget(StrictModel):
    """自分側の残りの評価回数・手数・途中確認数(相手の使い方は入らない。§2.7)。"""

    remaining_evaluations: int = Field(ge=0)
    remaining_moves: int = Field(ge=0)
    remaining_principal_checks: int = Field(ge=0)


class CandidateAttributeBands(StrictModel):
    """counterparty が候補者のときの属性帯(求人側への TurnInput で使う。§2.6・§2.7)。"""

    experience_band: ExperienceBandValue
    region_block: RegionBlockValue
    job_category: JobCategoryValue


class JobCategoryInfo(StrictModel):
    """counterparty が公開求人のときの区分情報(候補者側への TurnInput で使う。§2.7)。

    design.md §2.7 は「公開求人の区分情報」とだけ書き、具体的な項目を決めていない。
    ここでは、設計書の他所(§2.6)で名前が出ている職種(job_category)だけに限定した、
    最も保守的な読み方をした。実装計画上、求人属性が増える段階(④ 段階開示や、より後の段)
    で見直しが要る可能性がある。
    """

    job_category: JobCategoryValue


class LastInvalid(StrictModel):
    """TurnInput.last_invalid: 直前の自分の手が無効だったときの、打とうとした手の中身(台帳 C-40)。

    状態を持たないエージェントが、無効になった手(とその組み合わせ)を繰り返さないための情報。
    自分側のエージェントだけに返す値で、相手への漏れは増えない。
    - move: 打とうとした手の種類。レフェリーが登録した無効手(schema_invalid・agent_timeout・output_truncated)では
      分からないので None。
    - package: 打とうとした組み合わせ。手が組み合わせを伴わない場合と、レフェリーが登録した無効手では None。
    - evaluation: その組み合わせについての、自分側の 3 値評価。金庫が自分側のポリシーで評価して無効と
      判断した手(提案のガードの失敗・途中確認の対象外など)だけが持つ。評価しなかった無効手では None。
    3 つとも必須の項目にして(値だけ None を許す)、線の上の形をいつも同じにする。
    last_error と last_invalid は、エージェントの手(check 以外)についてだけ作る。レフェリーの確かめが
    間に入っても消えない(§2.7・台帳 X-51)。
    """

    move: AgentMoveType | None
    package: Package | None
    evaluation: Verdict | None


class TurnInput(StrictModel):
    """レフェリー → 交渉エージェント。LLM に渡る部分(§2.7)。"""

    schema_: Literal["turn-input/v1"] = Field(alias="schema")
    side: Side
    own_move_number: int = Field(ge=0)
    counterparty: CandidateAttributeBands | JobCategoryInfo
    history: list[HistoryEntry] = Field(default_factory=list)
    pending_offer: EvaluatedPackage | None = None
    last_check: EvaluatedPackage | None = None
    last_error: LastErrorReason | None = None
    last_invalid: LastInvalid | None = None
    budget: Budget
    # 呼び出しの種類と、この手番で計画した確かめの結果(§2.7・§4.1)。plan では checked は空。
    phase: Phase
    checked: list[CheckedPackage] = Field(default_factory=list, max_length=MAX_PLANNED_CHECKS)


class AttackerTurnInput(TurnInput):
    """攻撃モードの求人エージェント専用(§2.7)。TurnInput に principal_instruction を足す。

    この型は攻撃モード用の受信口(/a2a/attacker)だけが受け付ける想定(§4.3、1a では未実装)。
    """

    principal_instruction: str = Field(max_length=400)


_MOVES_REQUIRING_PACKAGE = frozenset({"propose", "ask_principal"})


class Move(StrictModel):
    """決定の出力(エージェント → レフェリー。§2.7)。check は出せない(台帳 X-45)。"""

    schema_: Literal["move/v1"] = Field(alias="schema")
    move: AgentMoveType
    package: Package | None = None

    @model_validator(mode="after")
    def _package_required_for_certain_moves(self) -> "Move":
        """package は propose・ask_principal のときだけ必須(§2.7)。"""
        if self.move in _MOVES_REQUIRING_PACKAGE and self.package is None:
            raise ValueError(f"move={self.move!r} requires a package")
        return self


class Plan(StrictModel):
    """計画の出力(エージェント → レフェリー。§2.7・§4.1)。

    checks は、確かめたい組み合わせを出したい順に最大 3 つ。checks が空のときだけ、手(move・package)を出す。
    checks と move の両方があるときは、レフェリーが checks を実行して move を無視する(台帳 C-51)。
    どちらもないときは無効(schema_invalid。台帳 L12-3)。

    レフェリーは、エージェントの出力をこの型で一括に検証せず、parse_plan で 2 段に読む(checks が有効なら、move・package は
    型検証せずに捨てる。台帳 L16-1・X-74・X-79)。この型で検証するのは、parse_plan の 3 段目(checks が空のときの手)だけ。
    """

    schema_: Literal["plan/v1"] = Field(alias="schema")
    checks: list[Package] = Field(default_factory=list, max_length=MAX_PLANNED_CHECKS)
    move: AgentMoveType | None = None
    package: Package | None = None

    @model_validator(mode="after")
    def _checks_or_move(self) -> "Plan":
        """checks が空なら move が必須。その move の package の規則は Move と同じ(§2.7)。"""
        if not self.checks:
            if self.move is None:
                raise ValueError("a plan needs either checks or a move")
            if self.move in _MOVES_REQUIRING_PACKAGE and self.package is None:
                raise ValueError(f"move={self.move!r} requires a package")
        return self


class PlanEnvelope(StrictModel):
    """計画の出力の 1 段目の読み(§2.7・§4.1 の 2。台帳 X-74・X-79): schema と checks だけを持つ strict な型。

    ほかの項目(move・package・未定義の項目)は受け流す(extra="ignore")。checks が空でなければ、それらは parse_plan が型検証せずに捨てる。
    JSON モードでは、move の形の違反(check・グリッド外など)が計画全体を手がかりのない schema_invalid にしてしまうため、
    checks が有効なら move・package の中身で計画を無効にしない(台帳 L16-1。履歴の check はレフェリーの確かめで、エージェントの手ではない)。
    """

    model_config = ConfigDict(extra="ignore")

    schema_: Literal["plan/v1"] = Field(alias="schema")
    checks: list[Package] = Field(default_factory=list, max_length=MAX_PLANNED_CHECKS)


def parse_plan(payload: dict) -> Plan:
    """計画の出力(エージェントが返した dict)を 2 段で読んで Plan にする(§2.7・§4.1 の 2。台帳 X-74・X-79・L16-1)。

    1. PlanEnvelope(schema と checks だけ。strict)で検証する。違反(スキーマ名・checks のグリッド外や未定義の項目・4 件以上)は ValueError。
    2. checks が空でなければ、move・package は型検証せずに捨てて、move・package が None の Plan を返す(checks を優先する寛容な読み。
       前提 P-14 の案 1: 捨てた値はどこにも渡らないので、AC-04 の目的(FR-16)は保たれる)。
    3. checks が空なら、payload 全体を Plan(move・package は Move の規則。strict。未定義の項目も拒否)で検証する。違反は ValueError。
    どちらの段の違反も、レフェリーは schema_invalid の無効手にする(pydantic の ValidationError は ValueError の一種)。
    """
    envelope = PlanEnvelope.model_validate(payload)
    if envelope.checks:
        return Plan(schema="plan/v1", checks=envelope.checks)
    return Plan.model_validate(payload)
