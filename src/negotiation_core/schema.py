"""A2A message schemas exchanged with negotiation agents (design.md §2.7).

pydantic, extra="forbid", strict。ID はシステムが乱数で作る 16 桁の 16 進数とし、
A2A メッセージの metadata に載せる。LLM に渡す入力(TurnInput 等)には含めない
(受信側の AgentExecutor が、LLM に渡す前に取り除く。§2.7・§4.3)。
ID を実際に使う受信口・vault の API は、1a では作らないので、ここでは形式(パターン)
だけを公開する。
"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from negotiation_core.policy import Package, StrictModel, Verdict
from negotiation_core.vocabulary import ExperienceBandValue, JobCategoryValue, RegionBlockValue, Side

# --- ID の扱い(§2.7) ---
ID_PATTERN = r"^[0-9a-f]{16}$"
Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]


MoveType = Literal["propose", "accept", "reject", "check", "ask_principal", "end"]

LastErrorReason = Literal[
    "off_grid",
    "not_acceptable_to_own_principal",
    "no_pending_offer",
    "question_not_applicable",
    "question_budget_exhausted",
    "evaluation_budget_exhausted",
    "schema_invalid",
    "agent_timeout",
]


class EvaluatedPackage(StrictModel):
    """組み合わせと、自分側の 3 値評価(pending_offer・last_check で使う。§2.7)。"""

    package: Package
    own_evaluation: Verdict


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
    budget: Budget


class AttackerTurnInput(TurnInput):
    """攻撃モードの求人エージェント専用(§2.7)。TurnInput に principal_instruction を足す。

    この型は攻撃モード用の受信口(/a2a/attacker)だけが受け付ける想定(§4.3、1a では未実装)。
    """

    principal_instruction: str = Field(max_length=400)


_MOVES_REQUIRING_PACKAGE = frozenset({"propose", "check", "ask_principal"})


class Move(StrictModel):
    """エージェント → レフェリー(§2.7)。"""

    schema_: Literal["move/v1"] = Field(alias="schema")
    move: MoveType
    package: Package | None = None

    @model_validator(mode="after")
    def _package_required_for_certain_moves(self) -> "Move":
        """package は propose・check・ask_principal のときだけ必須(§2.7)。"""
        if self.move in _MOVES_REQUIRING_PACKAGE and self.package is None:
            raise ValueError(f"move={self.move!r} requires a package")
        return self
