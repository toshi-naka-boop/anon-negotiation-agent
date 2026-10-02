"""LLM の出力の受け取り方(design.md §2.7・§4.2。台帳 I-19)。

計画(phase=plan)の出力は Plan、決定(phase=decide)の出力は Move。どちらも JSON モード(応答スキーマなし)で出させ、
グリッド値・`check` という手がないこと・checks は最大 3 件、は negotiation_core.schema の Plan・Move でレフェリーが
検証する。応答スキーマで縛ると、本物の Vertex AI の制約付きデコードが 11〜55 秒かかり、レフェリーの 1 回の上限を超えて
手番が落ちた(2026-10-03 の実測)。JSON モードでも数値軸が文字列で返ることがあるので、受信口は整数に戻す。
"""

import dataclasses
import json
from typing import get_args

import pytest
from google.genai import types
from google.protobuf import json_format, struct_pb2
from negotiation_core.schema import MAX_PLANNED_CHECKS, AgentMoveType, Move, MoveType, Plan

from agents.config import DEFAULT_AGENTS_CONFIG
from agents.llm_agents import build_llm_agent, thinking_level_for
from agents.output_schema import restore_numeric_axes
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


def test_restore_numeric_axes_turns_the_numeric_strings_back_into_integers_and_leaves_the_rest():
    # 数値軸を文字列で受け取った LLM の出力を、整数に戻す。整数として読めない値と、数値軸以外は触らない
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
    # Plan: checks の各要素と、package の両方を戻す。checks がなくても、null でも壊れない
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
    # JSON モードの Gemini が数値軸を文字列で返しても、受信口はそれを整数に戻して返し、Plan・Move として通る
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


def test_the_output_rules_live_in_the_pydantic_models_not_in_a_response_schema():
    # §2.7・台帳 I-19・X-45 (応答スキーマは使わない。checks は最大 3 件、checks か move のどちらか、check は出せない、は
    # レフェリーが Plan・Move で検証する)
    package = dict(valid_data("candidate")["history"][0]["package"])
    with pytest.raises(ValueError):
        Plan.model_validate({"schema": "plan/v1", "checks": [package] * (MAX_PLANNED_CHECKS + 1)})
    with pytest.raises(ValueError):
        Plan.model_validate({"schema": "plan/v1", "checks": [], "move": "check", "package": package})
    with pytest.raises(ValueError):
        Move.model_validate({"schema": "move/v1", "move": "check", "package": package})
    with pytest.raises(ValueError):
        Move.model_validate({"schema": "move/v1", "move": "propose", "package": dict(package, salary=625)})  # グリッド外
    assert Plan.model_validate({"schema": "plan/v1", "checks": [package] * MAX_PLANNED_CHECKS}).move is None
    assert "check" in get_args(MoveType)  # 対照: 金庫の手の種類には check がある(レフェリーが登録する)
    assert "check" not in get_args(AgentMoveType)


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ROLES)
def test_llm_agent_is_configured_as_designed(role, phase):
    # §4.2 (LlmAgent: 応答スキーマなしの JSON モード、ツールなし、temperature・思考の量・max_output_tokens は設定どおり)
    config = DEFAULT_AGENTS_CONFIG
    agent = build_llm_agent(role, phase, model="unused", config=config)
    assert agent.output_schema is None
    assert agent.tools == []
    assert agent.sub_agents == []
    assert agent.name == f"{role}_{phase}_agent"
    generation = agent.generate_content_config
    assert generation.response_mime_type == "application/json"
    assert generation.response_schema is None
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


@pytest.mark.parametrize("bad", [0, 100, 4097, 100_000])
def test_max_output_tokens_outside_the_safe_range_is_rejected_at_startup(bad):
    # 台帳 X-63 (設計書 §8.2 の 1 日の最悪の金額は max_output_tokens=2,048 の見積もり。桁違いの値では起動できない)
    with pytest.raises(ValueError, match="max_output_tokens"):
        dataclasses.replace(DEFAULT_AGENTS_CONFIG, max_output_tokens=bad)
    assert dataclasses.replace(DEFAULT_AGENTS_CONFIG, max_output_tokens=4096).max_output_tokens == 4096
    assert dataclasses.replace(DEFAULT_AGENTS_CONFIG, max_output_tokens=256).max_output_tokens == 256


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
async def test_the_llm_receives_json_mode_and_the_configured_settings(role, phase, http, stub_llm):
    # §4.2 (LLM に渡るリクエストの設定: 応答スキーマなしの JSON モード、temperature 0、ツールなし、思考の量と
    # max_output_tokens は設定どおり)
    await send_message(http, role, [data_part(valid_data(role, phase))])
    recorded = stub_llm.requests[0]
    assert recorded.temperature == 0
    assert not recorded.tools
    assert recorded.response_schema is None  # 台帳 I-19
    assert recorded.response_mime_type == "application/json"
    assert recorded.max_output_tokens == DEFAULT_AGENTS_CONFIG.max_output_tokens
    expected_level = DEFAULT_AGENTS_CONFIG.plan_thinking_level if phase == "plan" else DEFAULT_AGENTS_CONFIG.decide_thinking_level
    assert recorded.thinking_config.thinking_level == types.ThinkingLevel[expected_level]


@pytest.mark.anyio
async def test_the_default_stub_answer_for_each_phase_is_a_valid_plan_or_move(http, stub_llm):
    # テストの補助の確認(スタブの既定の応答は、phase に応じた Plan・Move。以降のテストの前提)
    plan = move_data_of(await send_message(http, "candidate", [data_part(valid_data("candidate", "plan"))]))
    move = move_data_of(await send_message(http, "candidate", [data_part(valid_data("candidate", "decide"))]))
    assert plan == json.loads(plan_json("propose"))  # 確かめの要らない手を出す Plan(checks は空)
    assert plan["checks"] == [] and plan["move"] == "propose"
    assert move["schema"] == "move/v1" and move["move"] == "propose"
