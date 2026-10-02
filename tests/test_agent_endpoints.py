"""agents の A2A 受信口(design.md §4.2・§4.3・§8.1 の壁 1)。

- 壁 1 の経路: 有効な TurnInput が /a2a/candidate に届くと、LLM(スタブ)が動き、計画(phase=plan)なら Plan、決定(phase=decide)なら
  Move が DataPart で返る。応答の artifact の metadata に、その呼び出しの usage(台帳 X-58)が載る。
- 出力が max_output_tokens で切れた(finish_reason が MAX_TOKENS)ときは、一時的でない A2A のエラー(truncated の印と usage つき。
  台帳 C-53)になる。
- Agent Card が A2A の標準の場所で公開される。
- LLM の一時的なエラー(429・5xx・時間切れ)は、一時的だと分かる A2A のエラーで返る。それ以外の失敗は印なし。
- 入力・出力の値が、エラーにもログにも出てこない。何も保存しない(タスク・セッション)。
"""

import asyncio
import dataclasses
import json
import logging
import re

import httpx
import pytest
from a2a.client import A2ACardResolver
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.genai import errors as genai_errors
from google.genai import types
from google.protobuf import json_format, struct_pb2
from negotiation_core.schema import Move, Plan, Usage
from pydantic import ValidationError

import agents.executor as executor_module
from agents.app import create_app
from agents.config import DEFAULT_AGENTS_CONFIG
from agents.wire import PHASES, value_to_python
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    BASE_URL,
    NID,
    PACKAGE,
    ROLES,
    StubLlm,
    agents_app,
    anyio_backend,
    asgi_client,
    assert_rejected,
    data_part,
    http,
    llm_response,
    message_json,
    move_data_of,
    move_json,
    plan_json,
    remaining_sessions,
    rpc_body,
    send_message,
    send_raw,
    stub_llm,
    text_part,
    usage_of,
    valid_data,
)

pytestmark = pytest.mark.anyio


def _raises(exc):
    """呼ばれたら exc を投げる、スタブの behavior。"""

    def behavior(_request):
        raise exc

    return behavior


def _error_info(body: dict) -> dict:
    """JSON-RPC のエラーの、google.rpc.ErrorInfo の metadata(a2a-sdk が data を載せる場所)。"""
    infos = [d for d in body["error"]["data"] if d["@type"].endswith("ErrorInfo")]
    assert len(infos) == 1
    return infos[0]["metadata"]


# --- 壁 1 の経路 ---


@pytest.mark.parametrize("phase", PHASES)
async def test_valid_turn_input_reaches_the_llm_and_the_plan_or_move_comes_back_as_a_datapart(phase, http, stub_llm):
    # §8.1 壁 1 (有効な TurnInput が /a2a/candidate に届くと、LLM が動き、phase=plan なら Plan、phase=decide なら Move が
    # DataPart で返る。レフェリーも壁 1 の画面も、返ってきた data をその型として検証する)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate", phase))])

    assert len(stub_llm.requests) == 1
    data = move_data_of(body)  # タスクは COMPLETED で、artifact も DataPart もちょうど 1 つ
    expected = plan_json("propose") if phase == "plan" else move_json()
    assert data == json.loads(expected)
    artifact = body["result"]["task"]["artifacts"][0]
    assert artifact["name"] == ("plan" if phase == "plan" else "move")
    assert artifact["parts"][0]["mediaType"] == "application/json"
    # 線の上の数値(double)は、整数に戻せば有効
    restored = value_to_python(json_format.ParseDict(data, struct_pb2.Value()))
    model = Plan if phase == "plan" else Move
    assert model.model_validate_json(json.dumps(restored)).package.salary == PACKAGE["salary"]


@pytest.mark.parametrize("phase", PHASES)
async def test_the_plan_or_move_is_returned_as_is_without_validation(phase, http, stub_llm):
    # §4.1・§4.3 (Plan・Move としての検証はレフェリーの仕事。スキーマ違反の出力も、そのままレフェリーに届く)
    off_grid = dict(PACKAGE, salary=610)
    if phase == "plan":
        stub_llm.behavior = lambda _request: plan_json(checks=[off_grid])
    else:
        stub_llm.behavior = lambda _request: move_json("propose", off_grid)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate", phase))])
    data = move_data_of(body)
    assert (data["checks"][0] if phase == "plan" else data["package"])["salary"] == 610


# --- 使用量(usage。台帳 X-58) ---


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ROLES)
async def test_the_response_artifact_metadata_carries_the_usage_of_the_call(role, phase, http, stub_llm):
    # §4.3・台帳 X-58 (応答の artifact の metadata は usage だけ。ADK の最終応答の usage_metadata から作る。model は設定の値、
    # requests はその実行で LLM を呼んだ回数(1))
    output = plan_json("propose") if phase == "plan" else move_json()
    stub_llm.behavior = lambda _request: llm_response(output, prompt=1234, cached=100, thoughts=300, output=50)
    body = await send_message(http, role, [data_part(valid_data(role, phase))])

    usage = usage_of(body)
    assert usage == {
        "model": DEFAULT_AGENTS_CONFIG.model,
        "prompt_tokens": 1234,
        "cached_tokens": 100,
        "thoughts_tokens": 300,
        "output_tokens": 50,
        "requests": 1,
    }
    # 線の上の数値(double)を整数に戻せば、strict な Usage として有効
    restored = value_to_python(json_format.ParseDict(usage, struct_pb2.Value()))
    assert Usage.model_validate_json(json.dumps(restored)).thoughts_tokens == 300


async def test_usage_items_that_the_model_did_not_report_are_zero(http, stub_llm):
    # §4.3 (取れない項目は 0。usage_metadata が丸ごとない応答でも、requests は 1 で、model は設定の値)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])  # スタブの既定の応答は usage_metadata なし
    assert usage_of(body) == {
        "model": DEFAULT_AGENTS_CONFIG.model,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "thoughts_tokens": 0,
        "output_tokens": 0,
        "requests": 1,
    }

    stub_llm.behavior = lambda _request: llm_response(plan_json("propose"), prompt=40, thoughts=None, output=7, cached=None)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
    assert usage_of(body) == {
        "model": DEFAULT_AGENTS_CONFIG.model,
        "prompt_tokens": 40,
        "cached_tokens": 0,
        "thoughts_tokens": 0,
        "output_tokens": 7,
        "requests": 1,
    }


async def test_usage_names_the_configured_model_not_the_stub_model(http, stub_llm):
    # §4.3 (usage.model は、設定ファイルのモデル名。差し込んだスタブの名前(stub-llm)ではない)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
    assert stub_llm.model == "stub-llm"
    assert usage_of(body)["model"] == DEFAULT_AGENTS_CONFIG.model == "gemini-3.5-flash"


# --- 出力が max_output_tokens で切れた(finish_reason が MAX_TOKENS。台帳 C-53) ---


def _cut_off(output_text: str | None) -> LlmResponse:
    """finish_reason が MAX_TOKENS の応答。text があれば途中で切れた出力、なければ思考だけで尽きた(ADK の error_code の形)。"""
    usage_metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=900, thoughts_token_count=2000, candidates_token_count=48
    )
    if output_text is None:
        return LlmResponse(
            error_code="MAX_TOKENS",
            error_message="max tokens",
            usage_metadata=usage_metadata,
            finish_reason=types.FinishReason.MAX_TOKENS,
        )
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=output_text)]),
        usage_metadata=usage_metadata,
        finish_reason=types.FinishReason.MAX_TOKENS,
    )


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(
    "cut_off_output",
    ['{"schema": "plan/v1", "checks": [{"salary": "65', None, '{"schema": "move/v1", "move": "end"}'],
    ids=["cut_in_the_middle_of_the_json", "no_output_at_all", "complete_json_but_finish_reason_is_max_tokens"],
)
async def test_an_output_cut_off_at_max_output_tokens_is_a_truncated_non_transient_error_with_usage(
    phase, cut_off_output, http, stub_llm, agents_app
):
    # §4.3・台帳 C-53・DV-17 (finish_reason が MAX_TOKENS なら、結果を返さず、一時的でない InternalError に truncated の印と、
    # その呼び出しの usage を付けて返す。切れ方によらない: JSON の途中で切れた・出力が 1 文字もない(思考で尽きた)・
    # たまたま完結している。レフェリーは output_truncated の無効手として登録する)
    stub_llm.behavior = lambda _request: _cut_off(cut_off_output)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate", phase))])

    assert "result" not in body
    assert body["error"]["code"] == -32603  # InternalError
    info = _error_info(body)
    assert info["truncated"] == "true"
    assert "transient" not in info  # 一時的ではない(再試行しても同じ入力では同じように切れ得る)
    assert info["usage"] == {
        "model": DEFAULT_AGENTS_CONFIG.model,
        "prompt_tokens": 900,
        "cached_tokens": 0,
        "thoughts_tokens": 2000,
        "output_tokens": 48,
        "requests": 1,
    }
    assert len(stub_llm.requests) == 1  # 受信口は再試行しない
    assert await remaining_sessions(agents_app) == []


async def test_a_finished_output_is_not_marked_truncated(http, stub_llm):
    # §4.3 (対照: finish_reason が STOP の応答は、切れた印のない成功。他の終わり方(finish_reason なし)も同じ)
    for finish_reason in (types.FinishReason.STOP, None):
        stub_llm.behavior = lambda _request, reason=finish_reason: llm_response(
            plan_json("propose"), finish_reason=reason, prompt=10, thoughts=5, output=5
        )
        body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
        assert "error" not in body, body
        assert usage_of(body)["thoughts_tokens"] == 5


async def test_the_cut_off_output_does_not_appear_in_the_error_or_the_logs(http, stub_llm, caplog):
    # §7・台帳 C-53 (切れた出力は、入力の値を含み得る。エラーにもログにも出さない。載せるのは usage の数とモデル名だけ)
    caplog.set_level(logging.INFO)
    stub_llm.behavior = lambda _request: _cut_off(f'{{"schema": "plan/v1", "note": "{CANARY}')
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
    assert _error_info(body)["truncated"] == "true"
    assert CANARY not in json.dumps(body, ensure_ascii=False)
    assert CANARY not in caplog.text
    assert "cut off" in caplog.text  # 対照: 切れたことは、値なしでログに残る


@pytest.mark.parametrize("role", ROLES)
async def test_agent_card_is_published_at_the_standard_location(role, http):
    # §4.3 (各エージェントの Agent Card を A2A の標準の場所 /.well-known/agent-card.json で公開する)
    base = f"{BASE_URL}/a2a/{role}"
    card = await A2ACardResolver(http, base).get_agent_card()

    assert card.name == f"{role}-negotiation-agent"
    assert [(i.url, i.protocol_binding, i.protocol_version) for i in card.supported_interfaces] == [
        (DEFAULT_AGENTS_CONFIG.public_base_url + f"/a2a/{role}", "JSONRPC", "1.0")
    ]
    assert list(card.default_input_modes) == ["application/json"]
    assert list(card.default_output_modes) == ["application/json"]
    assert not card.capabilities.streaming
    assert [skill.id for skill in card.skills] == ["negotiate-turn"]


# --- メッセージの形(exactly one DataPart) ---


@pytest.mark.parametrize("role", ROLES)
async def test_message_must_have_exactly_one_data_part(role, http, stub_llm):
    # §4.3 (parts はちょうど 1 つの DataPart。0 個も、有効な DataPart が 2 個も、拒否)
    data = valid_data(role)
    for parts in ([], [data_part(data), data_part(data)]):
        assert_rejected(await send_message(http, role, parts))
    assert stub_llm.requests == []


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("not_an_object", ["a string", 5, [1, 2], None, True])
async def test_data_must_be_a_json_object(role, not_an_object, http, stub_llm):
    # §4.3 (data は JSON のオブジェクト。文字列・数値・配列・null・真偽値は拒否)
    assert_rejected(await send_message(http, role, [data_part(not_an_object)]))
    assert stub_llm.requests == []


@pytest.mark.parametrize("role", ROLES)
async def test_non_integral_number_is_rejected_but_a_json_integer_is_accepted(role, http, stub_llm):
    # §4.3 (整数のフィールドに小数は入らない。A2A を通ると 0 は 0.0 で届くが、整数として読めるので通る)
    data = valid_data(role)
    data["own_move_number"] = 0.5
    assert_rejected(await send_message(http, role, [data_part(data)]))
    assert stub_llm.requests == []

    data["own_move_number"] = 3
    body = await send_message(http, role, [data_part(data)])
    assert "error" not in body, body
    assert len(stub_llm.requests) == 1


async def test_body_just_under_the_limit_is_accepted(http, stub_llm):
    # §4.3 (32 KB 以下の本文は通る。上限が厳しすぎないことの確認)
    limit = DEFAULT_AGENTS_CONFIG.max_request_body_bytes
    data = valid_data("candidate")
    entry = data["history"][0]

    def size() -> int:
        return len(httpx.Request("POST", BASE_URL, json=rpc_body(message_json([data_part(data)]))).content)

    while size() + 300 <= limit:
        data["history"].append(dict(entry))
    assert limit - 600 < size() <= limit

    body = await send_message(http, "candidate", [data_part(data)])
    assert "error" not in body, body
    assert len(stub_llm.requests) == 1


# --- LLM の失敗 ---


TRANSIENT_ERRORS = [
    genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}),
    genai_errors.ServerError(500, {"error": {"code": 500, "message": "oops", "status": "INTERNAL"}}),
    genai_errors.ServerError(503, {"error": {"code": 503, "message": "down", "status": "UNAVAILABLE"}}),
    genai_errors.ServerError(504, {"error": {"code": 504, "message": "slow", "status": "DEADLINE_EXCEEDED"}}),
    TimeoutError("timed out"),
    httpx.ReadTimeout("read timed out"),
    httpx.ConnectError("connection refused"),
]

def _adk_validation_error() -> ValidationError:
    """モデルの出力をスキーマで検証して失敗したときの、pydantic の検証エラー(ADK が投げ得るもの。入力の値を含む)。"""
    try:
        Move.model_validate({"schema": "move/v1", "move": "secret 623 in the message"})
    except ValidationError as exc:
        return exc
    raise AssertionError("the invalid move must be rejected")


NON_TRANSIENT_ERRORS = [
    genai_errors.ClientError(400, {"error": {"code": 400, "message": "bad request", "status": "INVALID_ARGUMENT"}}),
    genai_errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}),
    ZeroDivisionError("secret 623 in the message"),
    ValueError("secret 623 in the message"),
    json.JSONDecodeError("secret 623 in the message", "document", 0),  # LLM の出力が JSON でない(台帳 L9-5)
    _adk_validation_error(),  # ADK の検証エラー(台帳 L9-5)
]


@pytest.mark.parametrize("error", TRANSIENT_ERRORS, ids=lambda e: f"{type(e).__name__}-{getattr(e, 'code', '')}")
async def test_transient_llm_errors_are_returned_as_transient_a2a_errors(error, http, stub_llm):
    # §4.3 (LLM の呼び出しで一時的なエラー(429・5xx・時間切れ・ネットワーク)が起きたら、一時的だと分かる A2A のエラーを返す)
    stub_llm.behavior = _raises(error)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])

    assert "result" not in body
    assert body["error"]["code"] == -32603  # InternalError
    assert _error_info(body).get("transient") == "true"


@pytest.mark.parametrize("error", NON_TRANSIENT_ERRORS, ids=lambda e: f"{type(e).__name__}-{getattr(e, 'code', '')}")
async def test_other_llm_failures_are_internal_errors_without_the_transient_mark(error, http, stub_llm):
    # §4.3・台帳 L9-5 (一時的でない失敗(LLM の出力が JSON でない・ADK の検証エラーを含む)は、印のない InternalError。
    # 例外の中身(値を含み得る)を返さない。web のクライアントは、印のない失敗を再試行しない)
    stub_llm.behavior = _raises(error)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])

    assert body["error"]["code"] == -32603
    assert "transient" not in _error_info(body)
    assert "623" not in json.dumps(body)


@pytest.mark.parametrize("text", ["hello", "[1, 2]", "", "123"])
async def test_llm_output_that_is_not_a_json_object_is_an_error_not_a_move(text, http, stub_llm):
    # §4.3 (LLM の出力が JSON のオブジェクトでなければ、Move として返せない。印のない InternalError)
    stub_llm.behavior = lambda _request: text
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
    assert "result" not in body
    assert body["error"]["code"] == -32603
    assert "transient" not in _error_info(body)


async def test_llm_run_that_takes_too_long_is_a_transient_error():
    # §4.3 (止まった LLM 呼び出しを残さない。サーバ側の上限を超えたら、一時的なエラーで返す)
    async def hang(_request):
        await asyncio.sleep(30)
        return move_json()

    stub = StubLlm(behavior=hang)
    app = create_app(model=stub, config=dataclasses.replace(DEFAULT_AGENTS_CONFIG, llm_timeout_seconds=0.2))
    async with asgi_client(app) as http:
        body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
    assert body["error"]["code"] == -32603
    assert _error_info(body).get("transient") == "true"
    assert await remaining_sessions(app) == []  # 途中で止めても、セッションは残らない


async def test_default_model_is_the_one_r3_confirmed_and_the_app_does_not_connect_until_the_first_run():
    # §4.2・R-3・台帳 I-10・X-55 (設定ファイルのモデル名は、R-3 で確かめた gemini-3.5-flash。プロジェクトと場所は、設定に書かず
    # 環境変数で渡す)。スタブを差し込まない create_app は、ADK の Gemini を、クライアント側の自動再試行を切って
    # (retry_options.attempts=1)作るだけで、最初の実行まで Vertex AI に接続しない(実行すると本物の Gemini を呼ぶので、ここでは
    # 動かさない。本物の実行は scripts/run_demo.py --live。HTTP の要求が 1 回であることは tests/test_agent_http_retry.py)
    assert DEFAULT_AGENTS_CONFIG.model == "gemini-3.5-flash"
    app = create_app()
    models = [runner.agent.model for runner in app.state.runners.values()]
    assert len(models) == 6  # 側 × 呼び出しの種類
    assert all(isinstance(model, Gemini) for model in models)
    assert {model.model for model in models} == {"gemini-3.5-flash"}
    assert {model.retry_options.attempts for model in models} == {1}


# --- 何も保存しない ---


@pytest.mark.parametrize(
    "error",
    [None, ZeroDivisionError("x"), TRANSIENT_ERRORS[0]],
    ids=["success", "failure", "transient"],
)
async def test_no_session_remains_after_a_run(error, http, stub_llm, agents_app):
    # §4.2 (A2A のタスクごとに新しいセッションを作って捨てる。成功でも失敗でも残らない)
    if error is not None:
        stub_llm.behavior = _raises(error)
    for phase in PHASES:
        await send_message(http, "candidate", [data_part(valid_data("candidate", phase))])
    assert await remaining_sessions(agents_app) == []


async def test_no_background_task_remains_after_requests(http, stub_llm):
    # §4.2 (受信のたびに、a2a-sdk の実行用のタスクが残らない。Message だけで返すと、a2a-sdk 1.2.0 は
    # タスクが終わったと見なさず、リクエストのたびに実行用のタスクを残すので、Task(COMPLETED)で返している)
    before = len(asyncio.all_tasks())
    for _ in range(3):
        await send_message(http, "candidate", [data_part(valid_data("candidate"))])  # 成功
        await send_message(http, "candidate", [text_part("x")])  # 拒否
    stub_llm.behavior = _raises(ZeroDivisionError("x"))
    await send_message(http, "candidate", [data_part(valid_data("candidate"))])  # 失敗
    stub_llm.behavior = _raises(TRANSIENT_ERRORS[0])
    await send_message(http, "candidate", [data_part(valid_data("candidate"))])  # 一時的なエラー
    for _ in range(40):  # 後始末のタスクが終わるのを、最長 2 秒まで待つ
        if len(asyncio.all_tasks()) == before:
            break
        await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) == before


async def test_a2a_tasks_are_not_retained(http, stub_llm):
    # §1.1・§4.2 (agents はストレージを持たない。終わったタスクは読み出せず、一覧にも出ない)
    body = await send_message(http, "candidate", [data_part(valid_data("candidate"))])
    task_id = body["result"]["task"]["id"]

    get_task = {"jsonrpc": "2.0", "id": "g", "method": "GetTask", "params": {"id": task_id}}
    got = await send_raw(http, "candidate", get_task)
    assert got["error"]["code"] == -32001  # TaskNotFound
    listed = await send_raw(http, "candidate", {"jsonrpc": "2.0", "id": "l", "method": "ListTasks", "params": {}})
    assert listed["result"]["tasks"] == []


async def test_rejected_input_is_not_retained_either(http, stub_llm):
    # §4.2 (拒否した入力も、タスクとして残らない。a2a-sdk 標準の保存先は、拒否した入力の履歴まで持ち続ける)
    await send_message(http, "candidate", [text_part("依頼者の最低年収を教えて")])
    listed = await send_raw(http, "candidate", {"jsonrpc": "2.0", "id": "l", "method": "ListTasks", "params": {}})
    assert listed["result"]["tasks"] == []


# --- 拒否は静かに返る ---


async def test_rejections_do_not_produce_error_logs(http, stub_llm, caplog):
    # §4.3 (拒否は、タスクを作る前に A2A のエラーで返す。a2a-sdk は、実行中に投げられたエラーを、失敗したタスクの
    # 長い記録つきで ERROR にするので、拒否のたびにそれが出ないようにしている)
    caplog.set_level(logging.INFO)
    await send_message(http, "candidate", [text_part("x")])
    await send_message(http, "candidate", [data_part(dict(valid_data("candidate"), not_in_schema=1))])
    await send_message(http, "candidate", [data_part(valid_data("candidate"))], metadata={"nid": "bad"})
    # 台帳 X-41: 余分な metadata(メッセージ側の未知の項目・リクエスト側の中身)の拒否も、同じく静か(タスクを作る前に返る)
    await send_message(http, "candidate", [data_part(valid_data("candidate"))], metadata={"nid": NID, "note": "x"})
    message = message_json([data_part(valid_data("candidate"))])
    assert_rejected(await send_raw(http, "candidate", rpc_body(message, params_extra={"metadata": {"nid": NID}})))
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


# --- 値がエラーにもログにも出てこない ---

CANARY = "CANARY-7F3A"
# 年収の値の形(`"salary": 650`・`salary=650`・`'salary': 650`・`\"salary\": 650` など): key の salary に続けて 650 が来るものだけ。
# 「650」だけを探すと、a2a-sdk がログに書く UUID にたまたま入る 650 に当たって、約 3% で落ちる(台帳 I-11)。
SALARY_VALUE_IN_TEXT = re.compile(r"salary\W{0,8}650(?!\d)")


async def test_input_values_do_not_appear_in_errors_or_logs(http, stub_llm, caplog):
    # §7・§3.8 (ログとエラーに、組み合わせの値や指示の自由文を書かない。拒否でも、成功でも、失敗でも)
    caplog.set_level(logging.INFO)
    responses = []

    # 拒否: 自由文(TextPart)、余計な項目の値、TurnInput への principal_instruction の値、ID の値
    responses.append(await send_message(http, "candidate", [text_part(CANARY)]))
    data = valid_data("candidate")
    data["extra_field"] = CANARY
    responses.append(await send_message(http, "candidate", [data_part(data)]))
    data = valid_data("employer")
    data["principal_instruction"] = CANARY
    responses.append(await send_message(http, "employer", [data_part(data)]))
    valid_candidate = [data_part(valid_data("candidate"))]
    responses.append(await send_message(http, "candidate", valid_candidate, metadata={"nid": CANARY}))
    # 成功(攻撃モードの自由文が LLM に渡る)と、LLM の失敗(自由文を持つ入力のときの失敗)
    data = valid_data("attacker")
    data["principal_instruction"] = CANARY
    responses.append(await send_message(http, "attacker", [data_part(data)]))
    stub_llm.behavior = _raises(ZeroDivisionError("boom"))
    responses.append(await send_message(http, "attacker", [data_part(data)]))
    stub_llm.behavior = _raises(TRANSIENT_ERRORS[0])
    responses.append(await send_message(http, "attacker", [data_part(data)]))

    for body in responses:
        assert CANARY not in json.dumps(body), body
    assert CANARY not in caplog.text
    assert not SALARY_VALUE_IN_TEXT.search(caplog.text), "the salary value leaked into the logs"  # 組み合わせの値(年収)も


@pytest.mark.parametrize(
    "leaked",
    [
        '{"salary": 650, "remote_days": 0}',
        '{"salary":650}',
        "salary=650",
        "{'salary': 650}",
        'data=\\"salary\\": 650',
        "salary: 650",
    ],
)
def test_salary_leak_check_catches_every_shape_of_a_leaked_value(leaked):
    # 台帳 I-11 (絞った検査が、値の漏れを見逃さない)
    assert SALARY_VALUE_IN_TEXT.search(f"INFO agents.executor: received {leaked} ok")


@pytest.mark.parametrize(
    "harmless",
    [
        "INFO a2a: task 3f2650ab-1c4e-4b1a-9d0e-650650650650 completed",
        "salary=6500",
        "salary=1650",
        "role=candidate turn completed",
    ],
)
def test_salary_leak_check_ignores_text_that_only_contains_650(harmless):
    # 台帳 I-11 (偶然の UUID や、別の数字には当たらない。以前の「650 を含まない」の検査は、約 3% でここに当たって落ちた)
    assert not SALARY_VALUE_IN_TEXT.search(harmless)


async def test_the_log_check_fails_when_the_salary_value_leaks_into_the_logs(http, stub_llm, caplog, monkeypatch):
    # 台帳 I-11 (直した検査が、値が漏れたときに落ちる。自由文のカナリアは漏らさず、年収の値だけをログに書く)
    original = executor_module.llm_input_text

    def leaking(turn_input):
        executor_module.logger.info("received %s", {"salary": 650})
        return original(turn_input)

    monkeypatch.setattr(executor_module, "llm_input_text", leaking)
    with pytest.raises(AssertionError, match="the salary value leaked into the logs"):
        await test_input_values_do_not_appear_in_errors_or_logs(http, stub_llm, caplog)
