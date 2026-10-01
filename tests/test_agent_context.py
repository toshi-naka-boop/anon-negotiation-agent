"""AC-03(受信口の部分): design.md §2.7・§4.2・§4.3・§12.1。

LLM(スタブ)に渡る入力が「指示文＋TurnInput」だけで、A2A の metadata の ID(nid など)が入っていない。
A2A のタスクごとに新しいセッションで動き、前の手番の入力は次の手番に持ち越されない。

「ケース 1 の全手番で、フィクスチャの生の値も ID も自由文も入っていない」の確認は、ケース 1 の
フィクスチャと台本のエージェントを作る段(実装計画 ②)で足す。
"""

import asyncio
import json

import pytest
from negotiation_core.schema import AttackerTurnInput, TurnInput

import agents.llm_agents as llm_agents
from agents.app import create_app
from agents.instructions import load_instruction
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    NID,
    PACKAGE,
    ROLES,
    agents_app,
    anyio_backend,
    asgi_client,
    data_part,
    http,
    message_json,
    move_data_of,
    move_json,
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


@pytest.mark.parametrize("role", ROLES)
async def test_llm_input_is_the_instruction_and_the_turn_input_only(role, http, stub_llm):
    # AC-03 (LLM に渡る入力は、指示文＋TurnInput だけ)
    data = valid_data(role)
    body = await send_message(http, role, [data_part(data)])
    assert "error" not in body, body

    assert len(stub_llm.requests) == 1
    recorded = stub_llm.requests[0]
    # system_instruction は、指示文そのもの(ADK が足す定型の 1 文も入っていない)
    assert recorded.system_instruction == load_instruction(role)
    # ユーザー側の入力は、TurnInput の JSON が 1 つだけ
    assert recorded.contents == [("user", [_expected_llm_text(role, data)])]


@pytest.mark.parametrize("role", ROLES)
async def test_ids_in_metadata_never_reach_the_llm(role, http, stub_llm):
    # AC-03 (メッセージの metadata の ID(nid)は、LLM に渡る入力のどこにも出てこない。リクエストの metadata は、
    # 台帳 X-41 で空かなしだけを受け付けるので、ID を載せる場所はメッセージ側だけ。拒否の確認は tests/test_validation.py)
    message = message_json([data_part(valid_data(role))], metadata={"nid": NID})
    body = await send_raw(http, role, rpc_body(message))
    assert "error" not in body, body

    assert len(stub_llm.requests) == 1
    dump = stub_llm.requests[0].dump
    assert NID not in dump
    # メッセージ ID・コンテキスト ID・タスク ID(A2A が作る識別子)も入っていない
    assert message["messageId"] not in dump


async def test_free_text_only_comes_from_the_attacker_input(http, stub_llm):
    # AC-03 (自由文が LLM に入るのは、攻撃モードの受信口の principal_instruction だけ)
    marker = "自由文のカナリア-7F3A"
    for role in ("candidate", "employer"):
        data = valid_data(role)
        data["principal_instruction"] = marker
        await send_message(http, role, [data_part(data)])
    assert stub_llm.requests == []  # 拒否されたので、LLM には何も渡っていない

    data = valid_data("attacker")
    data["principal_instruction"] = marker
    await send_message(http, "attacker", [data_part(data)])
    assert len(stub_llm.requests) == 1
    assert marker in stub_llm.requests[0].contents[0][1][0]


@pytest.mark.parametrize("role", ROLES)
async def test_each_task_runs_in_a_new_session_and_keeps_no_state(role, http, stub_llm, agents_app):
    # AC-03 / §4.2 (A2A のタスクごとに新しいセッション。前の手番の入力は次に持ち越されず、セッションは残らない)
    first = valid_data(role)
    second = valid_data(role)
    second["own_move_number"] = 2
    for data in (first, second):
        body = await send_message(http, role, [data_part(data)])
        assert "error" not in body, body

    assert len(stub_llm.requests) == 2
    assert stub_llm.requests[0].contents == [("user", [_expected_llm_text(role, first)])]
    assert stub_llm.requests[1].contents == [("user", [_expected_llm_text(role, second)])]  # 1 手目は入っていない

    sessions = await agents_app.state.runners[role].session_service.list_sessions(app_name="agents")
    assert sessions.sessions == []


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


async def test_concurrent_requests_do_not_share_state(http, stub_llm):
    # §4.2 (同時に届いた手番も、それぞれ自分の入力だけで動く。答えも、自分の入力に対するもの)
    async def echo_own_move_number(request):
        await asyncio.sleep(0.01)  # 実行を重ねる
        received = json.loads(request.contents[0].parts[0].text)
        package = {**PACKAGE, "salary": 300 + 50 * received["own_move_number"]}
        return move_json("propose", package)

    stub_llm.behavior = echo_own_move_number

    async def one(n):
        data = valid_data("candidate")
        data["own_move_number"] = n
        return n, await send_message(http, "candidate", [data_part(data)])

    results = await asyncio.gather(*(one(n) for n in range(10)))
    for n, body in results:
        assert move_data_of(body)["package"]["salary"] == 300 + 50 * n
    assert all(len(recorded.contents) == 1 for recorded in stub_llm.requests)
    assert len(stub_llm.requests) == 10


async def test_braces_in_the_instruction_are_passed_through_unchanged(monkeypatch, stub_llm):
    # §4.2 (指示文は固定。ADK は文字列の指示文の {変数名} をセッションの状態で置き換えようとするので、
    # 波括弧(JSON の例など)を含む指示文が壊れないよう、置き換えを避けて渡している)
    text = '出力の例: {"schema": "move/v1", "move": "end"}。年収は {salary} のように書かない。'
    monkeypatch.setattr(llm_agents, "load_instruction", lambda role: text)
    app = create_app(model=stub_llm)
    async with asgi_client(app) as http:
        body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
    assert "error" not in body, body
    assert stub_llm.requests[0].system_instruction == text
