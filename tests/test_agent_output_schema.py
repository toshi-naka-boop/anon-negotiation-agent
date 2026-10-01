"""LLM に渡す output_schema(design.md §2.7 の最後の行・§4.2)。

構造化出力の段階でグリッド外の値を出させないため、軸ごとにグリッド値の列挙(enum)になっている。
スキーマの元は negotiation_core.schema.Move で、agents は別の定義を持たない。

google-genai の `Schema.enum` は文字列しか受け付けず、Vertex AI は enum を持つスキーマの型が STRING でなければ
400 で断る(2026-10-02 の DV-15 の実機で確かめた。調査事項 R-3)。そのため数値軸も
`type=STRING, format=enum, enum=[グリッド値を文字列にしたもの]` で表し、LLM の出力は受信口で整数に戻す。
"""

import json
from typing import get_args

import pytest
from google.genai import _transformers, types
from google.protobuf import json_format, struct_pb2
from negotiation_core import AXES, AXIS_KEYS
from negotiation_core.schema import Move, MoveType
from pydantic import ValidationError

from agents.config import DEFAULT_AGENTS_CONFIG
from agents.llm_agents import build_llm_agent
from agents.output_schema import build_move_output_schema, restore_numeric_axes
from agents.wire import value_to_python
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    PACKAGE,
    ROLES,
    agents_app,
    anyio_backend,
    data_part,
    http,
    move_data_of,
    send_message,
    stub_llm,
    valid_data,
)


def _package_schema(schema: types.Schema) -> types.Schema:
    return schema.properties["package"]


@pytest.mark.parametrize("axis", AXIS_KEYS)
def test_every_axis_is_an_enum_of_its_grid_values(axis):
    # §2.7 (出力スキーマの各軸は、グリッド値の列挙)
    axis_schema = _package_schema(build_move_output_schema()).properties[axis]
    grid = AXES[axis].grid
    # Vertex AI は enum の型が STRING でなければ断るので、数値軸も STRING の enum(R-3。DV-15 の実機で確かめた)
    assert axis_schema.type == types.Type.STRING
    if AXES[axis].kind == "numeric":
        assert axis_schema.format == "enum"
        assert axis_schema.enum == [str(value) for value in grid]
    else:
        assert axis_schema.enum == list(grid)


def test_no_schema_node_with_an_enum_has_a_type_other_than_string():
    # R-3: Vertex AI は、enum を持つ節の型が STRING でなければ 400 INVALID_ARGUMENT で断る(DV-15 の実機で確かめた)
    def walk(node: types.Schema) -> list[types.Schema]:
        nodes = [node]
        for child in (node.properties or {}).values():
            nodes += walk(child)
        for child in node.any_of or []:
            nodes += walk(child)
        if node.items is not None:
            nodes += walk(node.items)
        return nodes

    with_enum = [node for node in walk(build_move_output_schema()) if node.enum]
    assert with_enum  # 見る対象がある(空振りしない)
    assert all(node.type == types.Type.STRING for node in with_enum)


def test_restore_numeric_axes_turns_the_numeric_strings_back_into_integers_and_leaves_the_rest():
    # R-3: 数値軸を STRING の enum で受け取った LLM の出力を、整数に戻す。整数として読めない値と、数値軸以外は触らない
    move = {
        "schema": "move/v1",
        "move": "propose",
        "package": {
            "salary": "650", "remote_days": "2", "night_duty": "0", "review_months": "6",
            "training": "none", "side_job": "allowed", "start": "within_1_month",
        },
    }
    restored = restore_numeric_axes(move)
    assert restored["package"] == {
        "salary": 650, "remote_days": 2, "night_duty": 0, "review_months": 6,
        "training": "none", "side_job": "allowed", "start": "within_1_month",
    }
    assert Move.model_validate_json(json.dumps(restored)).package.salary == 650
    assert restore_numeric_axes({"schema": "move/v1", "move": "accept"}) == {"schema": "move/v1", "move": "accept"}
    odd = {"schema": "move/v1", "move": "check", "package": {"salary": "six hundred", "remote_days": 2.5}}
    assert restore_numeric_axes(odd)["package"] == {"salary": "six hundred", "remote_days": 2.5}  # 検証はレフェリーの仕事


@pytest.mark.anyio
async def test_the_endpoint_returns_integers_when_the_model_answers_numeric_axes_as_strings(http, stub_llm):
    # R-3: 本物の Gemini は、STRING の enum にした数値軸を文字列で返す。受信口はそれを整数に戻して返し、Move として通る
    package = {**PACKAGE, **{axis: str(PACKAGE[axis]) for axis in ("salary", "remote_days", "night_duty", "review_months")}}
    stub_llm.behavior = lambda _request: json.dumps({"schema": "move/v1", "move": "check", "package": package})

    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])

    data = move_data_of(body)
    assert all(isinstance(data["package"][axis], (int, float)) for axis in ("salary", "remote_days", "night_duty", "review_months"))
    restored = value_to_python(json_format.ParseDict(data, struct_pb2.Value()))
    assert Move.model_validate(restored).package.salary == PACKAGE["salary"]


def test_move_and_schema_fields_are_enums_too():
    # §2.7 (move の種類と、スキーマ名も列挙。package は null もあり得る)
    schema = build_move_output_schema()
    assert schema.properties["move"].enum == list(get_args(MoveType))
    assert schema.properties["schema"].enum == ["move/v1"]
    assert _package_schema(schema).nullable is True
    assert sorted(schema.required) == ["move", "schema"]  # package は propose・check・ask_principal のときだけ必須(§2.7)


def test_output_schema_has_the_same_shape_as_the_move_json_schema():
    # §2.7 (元は Move の JSON Schema。Move の項目が変わったら、ここで気づく)
    json_schema = Move.model_json_schema()
    package_json = json_schema["$defs"]["Package"]
    schema = build_move_output_schema()
    assert set(schema.properties) == set(json_schema["properties"])
    assert set(_package_schema(schema).properties) == set(package_json["properties"])
    assert set(_package_schema(schema).required) == set(package_json["required"])
    for axis, axis_json in package_json["properties"].items():
        assert _package_schema(schema).properties[axis].enum == [str(v) for v in axis_json["enum"]]


def test_output_schema_carries_no_descriptive_text():
    # §4.2 (LLM の文脈は指示文＋TurnInput だけ。スキーマに説明文(docstring)を載せて、余計な文を渡さない)
    def texts(node: types.Schema) -> list[str]:
        found = [t for t in (node.title, node.description) if t]
        for child in (node.properties or {}).values():
            found += texts(child)
        return found

    assert texts(build_move_output_schema()) == []


def test_google_genai_accepts_the_output_schema():
    # §2.7 / R-3 (google-genai が実際のリクエストを作るときの変換を通る。pydantic の Move をそのまま渡すと、
    # 数値の enum(整数)が「文字列でない」として検証エラーになる)
    with pytest.raises(ValidationError, match="valid string"):
        _transformers.t_schema(None, Move)  # 対照: Move をそのまま渡すと失敗する
    converted = _transformers.t_schema(None, build_move_output_schema())
    assert converted.properties["package"].properties["salary"].enum[:2] == ["300", "350"]


def test_llm_agent_is_configured_as_designed():
    # §4.2 (LlmAgent: output_schema はグリッド値の列挙、ツールなし、temperature 0、状態を持たない)
    agent = build_llm_agent("candidate", model="unused", temperature=DEFAULT_AGENTS_CONFIG.temperature)
    assert agent.output_schema == build_move_output_schema()
    assert agent.tools == []
    assert agent.generate_content_config.temperature == 0
    assert DEFAULT_AGENTS_CONFIG.temperature == 0
    assert agent.sub_agents == []


@pytest.mark.anyio
@pytest.mark.parametrize("role", ROLES)
async def test_the_llm_receives_the_output_schema_and_temperature_zero(role, http, stub_llm):
    # §4.2 (LLM に渡るリクエストの設定: 出力スキーマはグリッド値の列挙、temperature 0、ツールなし)
    await send_message(http, role, [data_part(valid_data(role))])
    recorded = stub_llm.requests[0]
    assert recorded.temperature == 0
    assert not recorded.tools
    assert recorded.response_schema == build_move_output_schema()
    salary = recorded.response_schema.properties["package"].properties["salary"]
    assert salary.enum == [str(value) for value in AXES["salary"].grid]
