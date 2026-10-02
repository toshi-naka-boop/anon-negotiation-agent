"""LLM に渡す output_schema(design.md §2.7・§4.2)。

計画(phase=plan)の出力は Plan、決定(phase=decide)の出力は Move。どちらも `check` という手を含まない(確かめは
Plan.checks でしか行えない。台帳 X-45)。Plan.checks は最大 3 件。構造化出力の段階でグリッド外の値を出させないため、
軸ごとにグリッド値の列挙(enum)になっている。スキーマの元は negotiation_core.schema の Plan・Move で、agents は別の定義を
持たない。

google-genai の `Schema.enum` は文字列しか受け付けず、Vertex AI は enum を持つスキーマの型が STRING でなければ
400 で断る(2026-10-02 の DV-15 の実機で確かめた。調査事項 R-3)。そのため数値軸も
`type=STRING, format=enum, enum=[グリッド値を文字列にしたもの]` で表し、LLM の出力は受信口で整数に戻す。null を許す項目は
`nullable: true`(`oneOf` は使わない。R-9 の §5)。
"""

import dataclasses
import json
from typing import get_args

import pytest
from google.genai import _transformers, types
from google.protobuf import json_format, struct_pb2
from negotiation_core import AXES, AXIS_KEYS
from negotiation_core.schema import MAX_PLANNED_CHECKS, AgentMoveType, Move, MoveType, Plan
from pydantic import ValidationError

from agents.config import DEFAULT_AGENTS_CONFIG
from agents.llm_agents import build_llm_agent, thinking_level_for
from agents.output_schema import (
    build_move_output_schema,
    build_output_schema,
    build_plan_output_schema,
    restore_numeric_axes,
)
from agents.wire import PHASES, ROLES, value_to_python
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    PACKAGE,
    agents_app,
    anyio_backend,
    data_part,
    http,
    move_data_of,
    plan_json,
    send_message,
    stub_llm,
    valid_data,
)

# (phase, 出力の型、スキーマの作り方)。plan は Plan、decide は Move
SCHEMAS = [
    pytest.param("plan", Plan, build_plan_output_schema, id="plan"),
    pytest.param("decide", Move, build_move_output_schema, id="decide"),
]


def _package_schema(schema: types.Schema) -> types.Schema:
    return schema.properties["package"]


def _walk(node: types.Schema) -> list[types.Schema]:
    """スキーマの節と、その下の節(properties・anyOf・items)をすべて集める。"""
    nodes = [node]
    for child in (node.properties or {}).values():
        nodes += _walk(child)
    for child in node.any_of or []:
        nodes += _walk(child)
    if node.items is not None:
        nodes += _walk(node.items)
    return nodes


def _package_schemas(schema: types.Schema) -> list[types.Schema]:
    """スキーマの中の、組み合わせ(package)の節。Move は package の 1 つ、Plan は package と checks の要素の 2 つ。"""
    packages = [_package_schema(schema)]
    if schema.properties.get("checks") is not None:
        packages.append(schema.properties["checks"].items)
    return packages


@pytest.mark.parametrize(("phase", "model", "build"), SCHEMAS)
@pytest.mark.parametrize("axis", AXIS_KEYS)
def test_every_axis_is_an_enum_of_its_grid_values(phase, model, build, axis):
    # §2.7 (出力スキーマの各軸は、グリッド値の列挙。数値軸も STRING の enum。Plan は checks の各要素も同じ)
    for package_schema in _package_schemas(build()):
        axis_schema = package_schema.properties[axis]
        grid = AXES[axis].grid
        # Vertex AI は enum の型が STRING でなければ断るので、数値軸も STRING の enum(R-3。DV-15 の実機で確かめた)
        assert axis_schema.type == types.Type.STRING
        if AXES[axis].kind == "numeric":
            assert axis_schema.format == "enum"
            assert axis_schema.enum == [str(value) for value in grid]
        else:
            assert axis_schema.enum == list(grid)


@pytest.mark.parametrize(("phase", "model", "build"), SCHEMAS)
def test_no_schema_node_with_an_enum_has_a_type_other_than_string(phase, model, build):
    # R-3: Vertex AI は、enum を持つ節の型が STRING でなければ 400 INVALID_ARGUMENT で断る(DV-15 の実機で確かめた)
    with_enum = [node for node in _walk(build()) if node.enum]
    assert with_enum  # 見る対象がある(空振りしない)
    assert all(node.type == types.Type.STRING for node in with_enum)


@pytest.mark.parametrize(("phase", "model", "build"), SCHEMAS)
def test_no_output_schema_offers_check_as_a_move(phase, model, build):
    # §2.7・台帳 X-45 (check はエージェントが出せる手ではない。構造化出力の列挙にも入れない)
    enums = [node.enum for node in _walk(build()) if node.enum]
    assert enums
    assert not [values for values in enums if "check" in values]
    assert "check" in get_args(MoveType)  # 対照: 金庫の手の種類には check がある(レフェリーが登録する)
    assert "check" not in get_args(AgentMoveType)


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
    odd = {"schema": "move/v1", "move": "ask_principal", "package": {"salary": "six hundred", "remote_days": 2.5}}
    assert restore_numeric_axes(odd)["package"] == {"salary": "six hundred", "remote_days": 2.5}  # 検証はレフェリーの仕事


def test_restore_numeric_axes_also_restores_every_package_in_the_checks_of_a_plan():
    # R-3 (Plan: checks の各要素と、package の両方を戻す。checks がなくても、null でも壊れない)
    as_strings = {**PACKAGE, **{axis: str(PACKAGE[axis]) for axis in ("salary", "remote_days", "night_duty", "review_months")}}
    second = dict(as_strings, salary="700")
    plan = {"schema": "plan/v1", "checks": [dict(as_strings), second], "move": None, "package": None}
    restored = restore_numeric_axes(plan)
    assert restored["checks"] == [PACKAGE, dict(PACKAGE, salary=700)]
    assert Plan.model_validate_json(json.dumps(restored)).checks[1].salary == 700

    with_move = {"schema": "plan/v1", "checks": [], "move": "propose", "package": dict(as_strings)}
    assert Plan.model_validate_json(json.dumps(restore_numeric_axes(with_move))).package.salary == PACKAGE["salary"]
    assert restore_numeric_axes({"schema": "plan/v1"}) == {"schema": "plan/v1"}


@pytest.mark.anyio
@pytest.mark.parametrize("phase", PHASES)
async def test_the_endpoint_returns_integers_when_the_model_answers_numeric_axes_as_strings(phase, http, stub_llm):
    # R-3: 本物の Gemini は、STRING の enum にした数値軸を文字列で返す。受信口はそれを整数に戻して返し、Plan・Move として通る
    as_strings = {**PACKAGE, **{axis: str(PACKAGE[axis]) for axis in ("salary", "remote_days", "night_duty", "review_months")}}
    if phase == "plan":
        output = {"schema": "plan/v1", "checks": [as_strings, dict(as_strings, salary="700")]}
    else:
        output = {"schema": "move/v1", "move": "ask_principal", "package": as_strings}
    stub_llm.behavior = lambda _request: json.dumps(output)

    body = await send_message(http, "candidate", [data_part(valid_data("candidate", phase))])

    data = move_data_of(body)
    restored = value_to_python(json_format.ParseDict(data, struct_pb2.Value()))
    if phase == "plan":
        assert Plan.model_validate(restored).checks[1].salary == 700
        assert all(isinstance(package[axis], (int, float)) for package in data["checks"] for axis in ("salary", "remote_days"))
    else:
        assert Move.model_validate(restored).package.salary == PACKAGE["salary"]
        assert all(isinstance(data["package"][axis], (int, float)) for axis in ("salary", "remote_days", "night_duty"))


def test_move_schema_fields_are_enums_too():
    # §2.7 (決定: move の種類と、スキーマ名も列挙。move は check を含まない。package は null もあり得る)
    schema = build_move_output_schema()
    assert schema.properties["move"].enum == list(get_args(AgentMoveType))
    assert schema.properties["schema"].enum == ["move/v1"]
    assert _package_schema(schema).nullable is True
    assert sorted(schema.required) == ["move", "schema"]  # package は propose・ask_principal のときだけ必須(§2.7)


def test_plan_schema_has_at_most_three_checks_and_the_move_fields_are_optional():
    # §2.7 (計画: checks は最大 3 件のグリッド値の並び。move・package は checks が空のときだけ。null を許す。check は出せない)
    schema = build_plan_output_schema()
    checks = schema.properties["checks"]
    assert checks.type == types.Type.ARRAY
    assert checks.max_items == MAX_PLANNED_CHECKS == 3
    assert checks.items.type == types.Type.OBJECT
    assert schema.properties["schema"].enum == ["plan/v1"]
    assert schema.properties["move"].enum == list(get_args(AgentMoveType))
    assert schema.properties["move"].nullable is True
    assert _package_schema(schema).nullable is True
    assert schema.required == ["schema"]  # checks・move・package は、どれも省ける(Plan の規則はレフェリーが検証する)


@pytest.mark.parametrize(("phase", "model", "build"), SCHEMAS)
def test_output_schema_has_the_same_shape_as_the_pydantic_models_json_schema(phase, model, build):
    # §2.7 (元は Plan・Move の JSON Schema。項目が変わったら、ここで気づく)
    json_schema = model.model_json_schema()
    package_json = json_schema["$defs"]["Package"]
    schema = build()
    assert set(schema.properties) == set(json_schema["properties"])
    for package_schema in _package_schemas(schema):
        assert set(package_schema.properties) == set(package_json["properties"])
        assert set(package_schema.required) == set(package_json["required"])
        for axis, axis_json in package_json["properties"].items():
            assert package_schema.properties[axis].enum == [str(v) for v in axis_json["enum"]]


@pytest.mark.parametrize(("phase", "model", "build"), SCHEMAS)
def test_output_schema_carries_no_descriptive_text(phase, model, build):
    # §4.2 (LLM の文脈は指示文＋TurnInput だけ。スキーマに説明文(docstring)を載せて、余計な文を渡さない)
    found = [text for node in _walk(build()) for text in (node.title, node.description) if text]
    assert found == []


@pytest.mark.parametrize(("phase", "model", "build"), SCHEMAS)
def test_google_genai_accepts_the_output_schema(phase, model, build):
    # §2.7 / R-3 (google-genai が実際のリクエストを作るときの変換を通る。pydantic のモデルをそのまま渡すと、
    # 数値の enum(整数)が「文字列でない」として検証エラーになる)
    with pytest.raises(ValidationError, match="valid string"):
        _transformers.t_schema(None, model)  # 対照: pydantic のモデルをそのまま渡すと失敗する
    converted = _transformers.t_schema(None, build())
    assert _package_schema(converted).properties["salary"].enum[:2] == ["300", "350"]


def test_build_output_schema_picks_the_schema_by_phase():
    # §4.2 (plan は Plan のスキーマ、decide は Move のスキーマ)
    assert build_output_schema("plan") == build_plan_output_schema()
    assert build_output_schema("decide") == build_move_output_schema()
    assert build_plan_output_schema() != build_move_output_schema()


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ROLES)
def test_llm_agent_is_configured_as_designed(role, phase):
    # §4.2 (LlmAgent: phase の出力スキーマ、ツールなし、temperature・思考の量・max_output_tokens は設定どおり、状態を持たない)
    config = DEFAULT_AGENTS_CONFIG
    agent = build_llm_agent(role, phase, model="unused", config=config)
    assert agent.output_schema == build_output_schema(phase)
    assert agent.tools == []
    assert agent.sub_agents == []
    assert agent.name == f"{role}_{phase}_agent"
    generation = agent.generate_content_config
    assert generation.temperature == 0 == config.temperature
    assert generation.max_output_tokens == config.max_output_tokens
    expected_level = config.plan_thinking_level if phase == "plan" else config.decide_thinking_level
    assert generation.thinking_config.thinking_level == types.ThinkingLevel[expected_level]
    assert generation.thinking_config.thinking_level == thinking_level_for(phase, config)


def test_thinking_levels_come_from_the_config_per_phase():
    # §4.2・R-9・台帳 I-18 (思考の量は、計画と決定で別に、設定ファイルで決まる。DV-15 の実測で調整するので、値そのものは
    # 固定しない。どちらも MINIMAL / LOW / MEDIUM / HIGH のどれかで、phase ごとに別の設定の値が使われる)
    config = DEFAULT_AGENTS_CONFIG
    valid = {"MINIMAL", "LOW", "MEDIUM", "HIGH"}
    assert {config.plan_thinking_level, config.decide_thinking_level} <= valid
    assert thinking_level_for("plan", config) == types.ThinkingLevel[config.plan_thinking_level]
    assert thinking_level_for("decide", config) == types.ThinkingLevel[config.decide_thinking_level]
    swapped = dataclasses.replace(
        config, plan_thinking_level="MINIMAL", decide_thinking_level="HIGH"
    )  # 別々に変えられる(片方の値が、もう片方に影響しない)
    assert thinking_level_for("plan", swapped) == types.ThinkingLevel.MINIMAL
    assert thinking_level_for("decide", swapped) == types.ThinkingLevel.HIGH


@pytest.mark.parametrize("bad", ["", "ULTRA", "low", "THINKING_LEVEL_UNSPECIFIED"])
def test_an_unknown_thinking_level_is_rejected(bad):
    # §4.2 (設定の名前が MINIMAL / LOW / MEDIUM / HIGH のどれでもなければ、起動のときに ValueError)
    config = dataclasses.replace(DEFAULT_AGENTS_CONFIG, plan_thinking_level=bad)
    with pytest.raises(ValueError, match="thinking level"):
        thinking_level_for("plan", config)
    with pytest.raises(ValueError, match="thinking level"):
        build_llm_agent("candidate", "plan", model="unused", config=config)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ROLES)
async def test_the_llm_receives_the_phases_output_schema_and_the_configured_settings(role, phase, http, stub_llm):
    # §4.2 (LLM に渡るリクエストの設定: phase の出力スキーマはグリッド値の列挙、temperature 0、ツールなし、思考の量と
    # max_output_tokens は設定どおり)
    await send_message(http, role, [data_part(valid_data(role, phase))])
    recorded = stub_llm.requests[0]
    assert recorded.temperature == 0
    assert not recorded.tools
    assert recorded.response_schema == build_output_schema(phase)
    assert recorded.max_output_tokens == DEFAULT_AGENTS_CONFIG.max_output_tokens
    expected_level = DEFAULT_AGENTS_CONFIG.plan_thinking_level if phase == "plan" else DEFAULT_AGENTS_CONFIG.decide_thinking_level
    assert recorded.thinking_config.thinking_level == types.ThinkingLevel[expected_level]
    salary = _package_schema(recorded.response_schema).properties["salary"]
    assert salary.enum == [str(value) for value in AXES["salary"].grid]
    # plan だけが checks を持つ(出力スキーマが phase で切り替わっている)
    assert ("checks" in recorded.response_schema.properties) == (phase == "plan")


@pytest.mark.anyio
async def test_the_default_stub_answer_for_each_phase_is_a_valid_plan_or_move(http, stub_llm):
    # テストの補助の確認(スタブの既定の応答は、phase に応じた Plan・Move。以降のテストの前提)
    plan = move_data_of(await send_message(http, "candidate", [data_part(valid_data("candidate", "plan"))]))
    move = move_data_of(await send_message(http, "candidate", [data_part(valid_data("candidate", "decide"))]))
    assert plan == json.loads(plan_json("propose"))  # 確かめの要らない手を出す Plan(checks は空)
    assert plan["checks"] == [] and plan["move"] == "propose"
    assert move["schema"] == "move/v1" and move["move"] == "propose"
