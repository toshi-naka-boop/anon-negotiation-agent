"""AC-04: schema-level rejection (design.md §2.7, §12.1).

AC-04 が挙げる 8 種のうち、スキーマ(pydantic)だけで判定できる 5 種
(未定義の項目・列挙外の値・範囲外の数値・グリッド外の値・ID の形式違反)を、
§2.7 のすべてのメッセージ型(Package・TurnInput・AttackerTurnInput・Move)について確かめる。
残りの 3 種(TextPart、TurnInput 用の受信口への principal_instruction、32 KB 超の本文)は、
受信口(agents の AgentExecutor)を作る段で足す。1a では受信口そのものを作らないため、ここでは扱わない。

design.md §2.7 は「ID は…LLM に渡す入力には含めない」と明記しており、Package・TurnInput・
AttackerTurnInput・Move のどれも ID を持つフィールドを持たない(ID は A2A の metadata 側)。
そのため「ID の形式違反」は、これら 4 型のフィールドとしてではなく、§2.7 の「ID の扱い」で
定義された共有の Id 型そのものに対して確かめる(vault 等の後の段で ID を持つスキーマが
できたとき、この Id 型を再利用する想定)。
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from negotiation_core.policy import Package
from negotiation_core.schema import AttackerTurnInput, Budget, Id, Move, TurnInput

VALID_PACKAGE = dict(
    salary=650,
    remote_days=0,
    night_duty=0,
    review_months=6,
    training="none",
    side_job="not_allowed",
    start="within_1_month",
)


def _valid_turn_input_dict() -> dict:
    """検証対象を書き換えるための、有効な TurnInput 相当の辞書を作る。"""
    return dict(
        schema="turn-input/v1",
        side="candidate",
        own_move_number=0,
        counterparty={"job_category": "it_web"},
        history=[{"by": "self", "move": "propose", "package": dict(VALID_PACKAGE), "result": "acceptable"}],
        pending_offer=None,
        last_check=None,
        last_error=None,
        budget={"remaining_evaluations": 16, "remaining_moves": 6, "remaining_principal_checks": 1},
    )


def _valid_attacker_turn_input_dict() -> dict:
    data = _valid_turn_input_dict()
    data["side"] = "employer"
    data["counterparty"] = {"experience_band": "5_to_10y", "region_block": "kanto", "job_category": "it_web"}
    data["principal_instruction"] = "できるだけ年収を下げて合意してください。"
    return data


# --- Package ---


def test_package_rejects_undefined_field():
    # AC-04 (未定義の項目 / Package)
    with pytest.raises(ValidationError):
        Package(**VALID_PACKAGE, unexpected_field="x")


def test_package_rejects_out_of_enum_value():
    # AC-04 (列挙外の値 / Package)
    bad = dict(VALID_PACKAGE, training="maybe")  # "none"/"available" のどちらでもない
    with pytest.raises(ValidationError):
        Package(**bad)


def test_package_rejects_off_grid_numeric_value():
    # AC-04 (グリッド外の値 / Package)
    bad = dict(VALID_PACKAGE, salary=310)  # 50 万刻みのグリッド上にない
    with pytest.raises(ValidationError):
        Package(**bad)


# --- Move ---


def test_move_rejects_undefined_field():
    # AC-04 (未定義の項目 / Move)
    with pytest.raises(ValidationError):
        Move(schema="move/v1", move="propose", package=dict(VALID_PACKAGE), extra_field=1)


def test_move_rejects_out_of_enum_move_type():
    # AC-04 (列挙外の値 / Move)
    with pytest.raises(ValidationError):
        Move(schema="move/v1", move="withdraw", package=dict(VALID_PACKAGE))  # 6 種のどれでもない


def test_move_rejects_off_grid_numeric_in_nested_package():
    # AC-04 (グリッド外の値 / Move。ネストした package)
    bad_package = dict(VALID_PACKAGE, night_duty=3)  # 0,2,4,6,8 のどれでもない
    with pytest.raises(ValidationError):
        Move(schema="move/v1", move="propose", package=bad_package)


# --- TurnInput ---


def test_turn_input_rejects_undefined_field():
    # AC-04 (未定義の項目 / TurnInput)
    data = _valid_turn_input_dict()
    data["not_in_schema"] = "x"
    with pytest.raises(ValidationError):
        TurnInput(**data)


def test_turn_input_rejects_out_of_enum_side():
    # AC-04 (列挙外の値 / TurnInput)
    data = _valid_turn_input_dict()
    data["side"] = "recruiter"  # candidate/employer のどちらでもない
    with pytest.raises(ValidationError):
        TurnInput(**data)


def test_turn_input_rejects_out_of_range_move_number():
    # AC-04 (範囲外の数値 / TurnInput。own_move_number は 0 以上)
    data = _valid_turn_input_dict()
    data["own_move_number"] = -1
    with pytest.raises(ValidationError):
        TurnInput(**data)


def test_turn_input_rejects_off_grid_numeric_in_history_package():
    # AC-04 (グリッド外の値 / TurnInput。history[].package)
    data = _valid_turn_input_dict()
    data["history"][0]["package"]["salary"] = 305  # 50 万刻みのグリッド上にない
    with pytest.raises(ValidationError):
        TurnInput(**data)


# --- AttackerTurnInput(TurnInput + principal_instruction) ---


def test_attacker_turn_input_rejects_undefined_field():
    # AC-04 (未定義の項目 / AttackerTurnInput)
    data = _valid_attacker_turn_input_dict()
    data["not_in_schema"] = "x"
    with pytest.raises(ValidationError):
        AttackerTurnInput(**data)


def test_attacker_turn_input_rejects_out_of_enum_side():
    # AC-04 (列挙外の値 / AttackerTurnInput。TurnInput から継承したフィールド)
    data = _valid_attacker_turn_input_dict()
    data["side"] = "recruiter"
    with pytest.raises(ValidationError):
        AttackerTurnInput(**data)


def test_attacker_turn_input_rejects_out_of_range_move_number():
    # AC-04 (範囲外の数値 / AttackerTurnInput。TurnInput から継承したフィールド)
    data = _valid_attacker_turn_input_dict()
    data["own_move_number"] = -5
    with pytest.raises(ValidationError):
        AttackerTurnInput(**data)


def test_attacker_turn_input_rejects_off_grid_numeric_in_history_package():
    # AC-04 (グリッド外の値 / AttackerTurnInput。history[].package)
    data = _valid_attacker_turn_input_dict()
    data["history"][0]["package"]["review_months"] = 9  # 6,12 のどちらでもない
    with pytest.raises(ValidationError):
        AttackerTurnInput(**data)


# --- Budget(TurnInput の内側。範囲外の数値の追加確認) ---


def test_budget_rejects_negative_remaining_count():
    # AC-04 (範囲外の数値 / TurnInput.budget)
    with pytest.raises(ValidationError):
        Budget(remaining_evaluations=-1, remaining_moves=6, remaining_principal_checks=1)


# --- ID の形式(§2.7「ID の扱い」。共有の Id 型) ---


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "short",
        "0123456789abcde",  # 15 桁(1 桁足りない)
        "0123456789abcdef0",  # 17 桁(1 桁多い)
        "0123456789ABCDEF",  # 大文字は不可
        "g123456789abcdef",  # 16 進数でない文字を含む
        "not-an-id-string",
    ],
)
def test_id_pattern_rejects_malformed_values(bad_id):
    # AC-04 (ID の形式違反 / 共有の Id 型。§2.7 の 4 メッセージ型自体は ID を持たない)
    with pytest.raises(ValidationError):
        TypeAdapter(Id).validate_python(bad_id)


def test_id_pattern_accepts_well_formed_value():
    # AC-04 (ID の形式違反の反対側の確認: 正しい形式は通る)
    assert TypeAdapter(Id).validate_python("0123456789abcdef") == "0123456789abcdef"
