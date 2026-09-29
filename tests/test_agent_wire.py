"""A2A の線(wire)上の値の変換(agents.wire。design.md §4.3)。

A2A(protobuf)の DataPart の `data` は google.protobuf.Value で、数値はすべて double になる。
strict なスキーマ(negotiation_core.schema)は int に 0.0 を受け付けないので、整数として読める
数値は int に戻してから検証する。これを忘れると、有効な TurnInput が A2A を通ると必ず拒否される。
"""

import json

import pytest
from a2a.helpers import new_data_part, new_text_part
from google.protobuf import json_format, struct_pb2
from negotiation_core.schema import AttackerTurnInput, Move, TurnInput

from agents.wire import ROLES, endpoint_path, part_kind, struct_to_python, value_to_python
from agents_helpers import PACKAGE, valid_data


def _through_protobuf(data):
    """JSON 相当の値を、A2A と同じく google.protobuf.Value を通して読み戻す。"""
    return value_to_python(json_format.ParseDict(data, struct_pb2.Value()))


def test_integral_numbers_come_back_as_int():
    # §4.3 (A2A を通っても整数は整数。0.0 ではない)
    restored = _through_protobuf({"a": 1, "b": [2, 3.5, "x", None, True, False], "c": {"d": 0}})
    assert restored == {"a": 1, "b": [2, 3.5, "x", None, True, False], "c": {"d": 0}}
    assert type(restored["a"]) is int
    assert type(restored["b"][0]) is int
    assert type(restored["b"][1]) is float  # 小数はそのまま(strict なスキーマが拒否する)
    assert restored["b"][4] is True and restored["b"][5] is False  # bool は int にならない
    assert type(restored["c"]["d"]) is int


def test_struct_to_python_restores_integers():
    # §4.3 (metadata の Struct も同じ変換)
    struct = struct_pb2.Struct()
    struct.update({"n": 3, "s": "x"})
    assert struct_to_python(struct) == {"n": 3, "s": "x"}
    assert type(struct_to_python(struct)["n"]) is int


@pytest.mark.parametrize("role", ROLES)
def test_valid_message_data_survives_the_protobuf_round_trip(role):
    # §4.3 (有効な TurnInput は、A2A を通したあとでも strict なスキーマで有効)
    # 線の上のデータは JSON なので、JSON として検証する(strict のスキーマは、Python の辞書の文字列を
    # 列挙型 Verdict として受け付けない。受信口の検証と同じ)。
    model = AttackerTurnInput if role == "attacker" else TurnInput
    data = valid_data(role)
    through_wire = model.model_validate_json(json.dumps(_through_protobuf(data)))
    assert through_wire == model.model_validate_json(json.dumps(data))


def test_move_round_trip_keeps_grid_values_as_int():
    # §4.3 (Move の数値軸は int で返る)
    move = {"schema": "move/v1", "move": "propose", "package": dict(PACKAGE)}
    restored = _through_protobuf(move)
    assert Move.model_validate(restored).package.salary == 650
    numeric_axes = ("salary", "remote_days", "night_duty", "review_months")
    assert all(type(restored["package"][axis]) is int for axis in numeric_axes)


def test_part_kind_distinguishes_data_and_text():
    # §4.3 (DataPart だけを通すための判定)
    assert part_kind(new_data_part({"a": 1})) == "data"
    assert part_kind(new_text_part("x")) == "text"


def test_endpoint_paths_follow_the_design_table():
    # §4.3 (受信口のパス)
    assert [endpoint_path(role) for role in ROLES] == ["/a2a/candidate", "/a2a/employer", "/a2a/attacker"]
