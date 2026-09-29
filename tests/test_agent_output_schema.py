"""LLM に渡す output_schema(design.md §2.7 の最後の行・§4.2)。

構造化出力の段階でグリッド外の値を出させないため、軸ごとにグリッド値の列挙(enum)になっている。
スキーマの元は negotiation_core.schema.Move で、agents は別の定義を持たない。

google-genai の `Schema.enum` は文字列しか受け付けないので、数値軸は
`type=INTEGER, format=enum, enum=[グリッド値を文字列にしたもの]` で表す(調査事項 R-3 の一部。
Vertex AI の実機で通るかは R-3 で確かめる)。
"""

from typing import get_args

import pytest
from google.genai import _transformers, types
from negotiation_core import AXES, AXIS_KEYS
from negotiation_core.schema import Move, MoveType
from pydantic import ValidationError

from agents.config import DEFAULT_AGENTS_CONFIG
from agents.llm_agents import build_llm_agent
from agents.output_schema import build_move_output_schema
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    ROLES,
    agents_app,
    anyio_backend,
    data_part,
    http,
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
    if AXES[axis].kind == "numeric":
        assert axis_schema.type == types.Type.INTEGER
        assert axis_schema.format == "enum"
        assert axis_schema.enum == [str(value) for value in grid]
    else:
        assert axis_schema.type == types.Type.STRING
        assert axis_schema.enum == list(grid)


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
