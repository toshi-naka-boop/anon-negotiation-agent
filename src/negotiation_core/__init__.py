"""negotiation_core: shared vocabulary, policy evaluation and message schemas.

語彙・グリッド・丸め・ポリシー評価・アンカーへの変換規則・スキーマをまとめたパッケージ
(design.md §2)。vault・agents・web の 3 サービスがすべてこの実装を使う想定。
"""

from negotiation_core.policy import (
    Anchor,
    Package,
    Policy,
    Verdict,
    contained_in,
    evaluate,
    is_contradictory,
    iter_all_packages,
    satisfies,
)
from negotiation_core.rounding import round_anchor, round_numeric_value
from negotiation_core.schema import (
    ID_PATTERN,
    AttackerTurnInput,
    Budget,
    CandidateAttributeBands,
    EvaluatedPackage,
    HistoryEntry,
    Id,
    JobCategoryInfo,
    LastErrorReason,
    LastInvalid,
    Move,
    MoveType,
    TurnInput,
)
from negotiation_core.statements import (
    PartialStatement,
    TwoChoiceResponse,
    apply_axis_removal,
    convert_statement_to_anchor,
    convert_two_choice_answer_to_anchor,
    neutral_fill_value_for_removal,
)
from negotiation_core.vocabulary import (
    ATTRIBUTE_BANDS,
    AXES,
    AXIS_KEYS,
    CATEGORICAL_AXIS_KEYS,
    NUMERIC_AXIS_KEYS,
    Side,
    best_value,
    goodness_rank,
    worst_value,
)

__all__ = [
    "ATTRIBUTE_BANDS",
    "AXES",
    "AXIS_KEYS",
    "CATEGORICAL_AXIS_KEYS",
    "ID_PATTERN",
    "NUMERIC_AXIS_KEYS",
    "Anchor",
    "AttackerTurnInput",
    "Budget",
    "CandidateAttributeBands",
    "EvaluatedPackage",
    "HistoryEntry",
    "Id",
    "JobCategoryInfo",
    "LastErrorReason",
    "LastInvalid",
    "Move",
    "MoveType",
    "Package",
    "PartialStatement",
    "Policy",
    "Side",
    "TurnInput",
    "TwoChoiceResponse",
    "Verdict",
    "apply_axis_removal",
    "best_value",
    "contained_in",
    "convert_statement_to_anchor",
    "convert_two_choice_answer_to_anchor",
    "evaluate",
    "goodness_rank",
    "is_contradictory",
    "iter_all_packages",
    "neutral_fill_value_for_removal",
    "round_anchor",
    "round_numeric_value",
    "satisfies",
    "worst_value",
]
