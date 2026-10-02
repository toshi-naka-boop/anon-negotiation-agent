"""AC-03(受信口の部分): design.md §2.7・§4.2・§4.3・§12.1・§12.2 の DV-17(AC-03 の検査を含む)。

LLM(スタブ)に渡る入力が「固定の前文＋TurnInput」だけで、A2A の metadata の ID(nid など)が入っていない。計画(phase=plan)
でも決定(phase=decide)でも同じ。リクエストの設定は、設定ファイルのとおり: system_instruction のハッシュが側ごとの前文
(指示文＋グリッド)と一致し、contents は TurnInput の JSON 1 件だけで、`thinking_config.thinking_level`(計画と決定で別)・
temperature・`max_output_tokens` が設定の値と一致する。A2A のタスクごとに新しいセッションで動き、前の手番の入力は次の手番に
持ち越されない。

「ケース 1 の全手番で、フィクスチャの生の値も ID も自由文も入っていない」の確認は、ケース 1 の
フィクスチャと台本のエージェントを作る段(実装計画 ②)で足す。
"""

import asyncio
import dataclasses
import hashlib
import json

import pytest
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types
from negotiation_core.schema import AttackerTurnInput, TurnInput

import agents.llm_agents as llm_agents
from agents.app import create_app
from agents.config import DEFAULT_AGENTS_CONFIG
from agents.instructions import load_instruction
from agents.output_schema import build_output_schema
from agents.wire import PHASES
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    NID,
    PACKAGE,
    ROLES,
    StubLlm,
    agents_app,
    anyio_backend,
    asgi_client,
    data_part,
    http,
    message_json,
    move_data_of,
    move_json,
    plan_json,
    remaining_sessions,
    rpc_body,
    send_message,
    send_raw,
    stub_llm,
    valid_data,
)

pytestmark = pytest.mark.anyio


def _expected_llm_text(role, data) -> str:
    """受信口が LLM に渡す入力(検証済みの値を JSON にしたもの)。スキーマの型を通した、決まった形。"""
    model = AttackerTurnInput if role == "attacker" else TurnInput
    validated = model.model_validate_json(json.dumps(data))
    return json.dumps(validated.model_dump(mode="json", by_alias=True), ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _configured_level(phase) -> types.ThinkingLevel:
    name = DEFAULT_AGENTS_CONFIG.plan_thinking_level if phase == "plan" else DEFAULT_AGENTS_CONFIG.decide_thinking_level
    return types.ThinkingLevel[name]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ROLES)
async def test_llm_input_is_the_fixed_preamble_and_the_turn_input_only(role, phase, http, stub_llm):
    # AC-03・DV-17 (LLM に渡る入力は、固定の前文＋TurnInput だけ。計画・決定の両方。設定の値がリクエストに載る)
    data = valid_data(role, phase)
    body = await send_message(http, role, [data_part(data)])
    assert "error" not in body, body

    assert len(stub_llm.requests) == 1
    recorded = stub_llm.requests[0]
    # system_instruction は、側ごとの前文そのもの(ハッシュが一致。ADK が足す定型の 1 文も入っていない)
    assert _sha256(recorded.system_instruction) == _sha256(load_instruction(role))
    # ユーザー側の入力は、TurnInput の JSON が 1 つだけ(plan では checked は空、decide では計画の結果が入る)
    assert recorded.contents == [("user", [_expected_llm_text(role, data)])]
    sent = json.loads(recorded.contents[0][1][0])
    assert sent["phase"] == phase
    assert bool(sent["checked"]) == (phase == "decide")
    # 思考の量(計画と決定で別)・temperature・出力の上限は、設定ファイルのとおり
    assert recorded.thinking_config.thinking_level == _configured_level(phase)
    assert recorded.temperature == DEFAULT_AGENTS_CONFIG.temperature == 0
    assert recorded.max_output_tokens == DEFAULT_AGENTS_CONFIG.max_output_tokens
    # 出力スキーマは phase の Plan・Move。ツールは持たない
    assert recorded.response_schema == build_output_schema(phase)
    assert not recorded.tools


async def test_the_thinking_level_differs_between_plan_and_decide_and_follows_the_config(stub_llm):
    # §4.2・R-9 (計画と決定で、思考の量を別に設定できる。設定を変えると、リクエストの値が変わる)
    config = dataclasses.replace(DEFAULT_AGENTS_CONFIG, plan_thinking_level="HIGH", decide_thinking_level="MINIMAL")
    app = create_app(model=stub_llm, config=config)
    async with asgi_client(app) as client:
        await send_message(client, "candidate", [data_part(valid_data("candidate", "plan"))])
        await send_message(client, "candidate", [data_part(valid_data("candidate", "decide"))])
    plan_request, decide_request = stub_llm.requests
    assert plan_request.thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert decide_request.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL


async def test_max_output_tokens_follows_the_config(stub_llm):
    # §4.2・台帳 X-50 (出力＋思考の上限は設定ファイルの値。変えると、リクエストの値が変わる)
    app = create_app(model=stub_llm, config=dataclasses.replace(DEFAULT_AGENTS_CONFIG, max_output_tokens=777))
    async with asgi_client(app) as client:
        await send_message(client, "employer", [data_part(valid_data("employer", "plan"))])
    assert stub_llm.requests[0].max_output_tokens == 777


@pytest.mark.parametrize("role", ROLES)
async def test_plan_and_decide_share_one_preamble_per_role_and_the_roles_differ(role, http, stub_llm):
    # §4.2 (指示文は側ごとに 1 つで、計画と決定で共有する。壁 2 は、この前文と TurnInput をそのまま見せれば正確になる)
    for phase in PHASES:
        await send_message(http, role, [data_part(valid_data(role, phase))])
    plan_request, decide_request = stub_llm.requests
    assert _sha256(plan_request.system_instruction) == _sha256(decide_request.system_instruction)
    assert plan_request.system_instruction == load_instruction(role)
    # 側が違えば前文も違う(候補者側・求人側・攻撃モードの求人側)
    assert len({_sha256(load_instruction(other)) for other in ROLES}) == len(ROLES)


async def test_plan_and_decide_run_different_runners(agents_app, http, stub_llm):
    # §4.2・DV-17 (phase で、計画の Runner か決定の Runner かが選ばれる。同じ側でも別の LlmAgent・別の Runner)
    class AgentNames(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="agent_names")
            self.names: list[str] = []

        async def before_model_callback(self, *, callback_context, llm_request):
            self.names.append(callback_context.agent_name)
            return None

    recorder = AgentNames()
    runners = agents_app.state.runners
    assert set(runners) == {(role, phase) for role in ROLES for phase in PHASES}  # 側 × 呼び出しの種類の 6 つ
    assert len({id(runner) for runner in runners.values()}) == len(runners)  # すべて別の Runner
    for runner in runners.values():
        runner.plugin_manager.register_plugin(recorder)

    for role in ROLES:
        for phase in PHASES:
            body = await send_message(http, role, [data_part(valid_data(role, phase))])
            assert "error" not in body, body

    assert recorder.names == [f"{role}_{phase}_agent" for role in ROLES for phase in PHASES]
    assert len(stub_llm.requests) == 6
    # 計画の Runner には Plan のスキーマ、決定の Runner には Move のスキーマ
    schemas = [request.response_schema for request in stub_llm.requests]
    assert schemas == [build_output_schema(phase) for _ in ROLES for phase in PHASES]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ROLES)
async def test_ids_in_metadata_never_reach_the_llm(role, phase, http, stub_llm):
    # AC-03 (メッセージの metadata の ID(nid)は、LLM に渡る入力のどこにも出てこない。リクエストの metadata は、
    # 台帳 X-41 で空かなしだけを受け付けるので、ID を載せる場所はメッセージ側だけ。拒否の確認は tests/test_validation.py)
    message = message_json([data_part(valid_data(role, phase))], metadata={"nid": NID})
    body = await send_raw(http, role, rpc_body(message))
    assert "error" not in body, body

    assert len(stub_llm.requests) == 1
    dump = stub_llm.requests[0].dump
    assert NID not in dump
    # メッセージ ID・コンテキスト ID・タスク ID(A2A が作る識別子)も入っていない
    assert message["messageId"] not in dump


@pytest.mark.parametrize("phase", PHASES)
async def test_free_text_only_comes_from_the_attacker_input(phase, http, stub_llm):
    # AC-03 (自由文が LLM に入るのは、攻撃モードの受信口の principal_instruction だけ。計画でも決定でも)
    marker = "自由文のカナリア-7F3A"
    for role in ("candidate", "employer"):
        data = valid_data(role, phase)
        data["principal_instruction"] = marker
        await send_message(http, role, [data_part(data)])
    assert stub_llm.requests == []  # 拒否されたので、LLM には何も渡っていない

    data = valid_data("attacker", phase)
    data["principal_instruction"] = marker
    await send_message(http, "attacker", [data_part(data)])
    assert len(stub_llm.requests) == 1
    assert marker in stub_llm.requests[0].contents[0][1][0]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ROLES)
async def test_each_task_runs_in_a_new_session_and_keeps_no_state(role, phase, http, stub_llm, agents_app):
    # AC-03 / §4.2 (A2A のタスクごとに新しいセッション。前の手番の入力は次に持ち越されず、セッションは残らない)
    first = valid_data(role, phase)
    second = valid_data(role, phase)
    second["own_move_number"] = 2
    for data in (first, second):
        body = await send_message(http, role, [data_part(data)])
        assert "error" not in body, body

    assert len(stub_llm.requests) == 2
    assert stub_llm.requests[0].contents == [("user", [_expected_llm_text(role, first)])]
    assert stub_llm.requests[1].contents == [("user", [_expected_llm_text(role, second)])]  # 1 手目は入っていない

    assert await remaining_sessions(agents_app) == []


async def test_a_plan_call_is_not_carried_into_the_decide_call_of_the_same_turn(http, stub_llm):
    # AC-03 / §4.2 (同じ手番の計画と決定も、別のセッション。決定の文脈は、前文と、決定の TurnInput(確かめの結果 checked を持つ)
    # の 1 件だけで、計画の入力・出力は入らない)
    plan = valid_data("candidate", "plan")
    decide = valid_data("candidate", "decide")
    marker = dict(PACKAGE, salary=1450)  # 計画の出力にだけ出てくる組み合わせ
    stub_llm.behavior = lambda _request: plan_json(checks=[marker])
    await send_message(http, "candidate", [data_part(plan)])
    stub_llm.behavior = lambda _request: move_json("propose")
    await send_message(http, "candidate", [data_part(decide)])

    decide_request = stub_llm.requests[1]
    assert decide_request.contents == [("user", [_expected_llm_text("candidate", decide)])]
    assert "1450" not in json.dumps(decide_request.contents)


async def test_a2a_context_id_does_not_carry_state_between_calls(http, stub_llm):
    # AC-03 / §4.2 (呼び出し側が同じ contextId を使い回しても、前の入力は持ち越されない。セッションは contextId で決まらない)
    data = valid_data("candidate")
    for _ in range(2):
        message = message_json([data_part(data)])
        message["contextId"] = "shared-context"
        body = await send_raw(http, "candidate", rpc_body(message))
        assert "error" not in body, body
    assert len(stub_llm.requests) == 2
    assert stub_llm.requests[1].contents == stub_llm.requests[0].contents
    assert len(stub_llm.requests[1].contents) == 1


@pytest.mark.parametrize("phase", PHASES)
async def test_concurrent_requests_do_not_share_state(phase, http, stub_llm):
    # §4.2 (同時に届いた手番も、それぞれ自分の入力だけで動く。答えも、自分の入力に対するもの)
    async def echo_own_move_number(request):
        await asyncio.sleep(0.01)  # 実行を重ねる
        received = json.loads(request.contents[0].parts[0].text)
        package = {**PACKAGE, "salary": 300 + 50 * received["own_move_number"]}
        return plan_json("propose", package) if phase == "plan" else move_json("propose", package)

    stub_llm.behavior = echo_own_move_number

    async def one(n):
        data = valid_data("candidate", phase)
        data["own_move_number"] = n
        return n, await send_message(http, "candidate", [data_part(data)])

    results = await asyncio.gather(*(one(n) for n in range(10)))
    for n, body in results:
        assert move_data_of(body)["package"]["salary"] == 300 + 50 * n
    assert all(len(recorded.contents) == 1 for recorded in stub_llm.requests)
    assert len(stub_llm.requests) == 10


async def test_braces_in_the_instruction_are_passed_through_unchanged(monkeypatch, stub_llm):
    # §4.2 (指示文は固定。ADK は文字列の指示文の {変数名} をセッションの状態で置き換えようとするので、
    # 波括弧(JSON の例など)を含む指示文が壊れないよう、置き換えを避けて渡している。計画・決定のどちらでも)
    text = '出力の例: {"schema": "move/v1", "move": "end"}。年収は {salary} のように書かない。'
    monkeypatch.setattr(llm_agents, "load_instruction", lambda role: text)
    app = create_app(model=stub_llm)
    async with asgi_client(app) as http:
        for phase in PHASES:
            body = await send_message(http, "candidate", [data_part(valid_data("candidate", phase))])
            assert "error" not in body, body
    assert [request.system_instruction for request in stub_llm.requests] == [text, text]
