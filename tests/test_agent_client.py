"""web(レフェリー)から呼ぶクライアント `agents.client.send_turn`(design.md §4.1・§4.3)。

A2A で 1 回だけ送り、返ってきた DataPart の `data`(dict)をそのまま返す。Move としての検証も、再試行もしない。
失敗は組み込みの例外だけで伝える。

- 時間切れ → TimeoutError
- A2A の通信エラーと、受信口が返した一時的なエラー(`transient` の印つき) → ConnectionError
- 受信口が入力を拒否した(壁 1)、または、一時的と印のない失敗(LLM の出力が JSON でない・ADK の検証エラーなど。
  台帳 L9-5。送り直しても直らないので、レフェリーは再試行しない) → ValueError
- 成功 → dict

通信は、HTTP サーバを立てずに、httpx の ASGITransport でアプリにつなぐ。クライアントが待たされる場面
(時間切れ・通信の失敗・壊れた応答)は、MockTransport で作る(ASGI は、サーバの処理がクライアントと同じ
タスクの中で動くため、クライアント側の時間切れの確認には向かない)。
"""

import asyncio
import json
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from negotiation_core.schema import AttackerTurnInput, Move, TurnInput
from pydantic import ValidationError

import agents.client as client_module
from agents.app import create_app
from agents.client import send_turn
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    BASE_URL,
    NID,
    PACKAGE,
    ROLES,
    StubLlm,
    agents_app,
    anyio_backend,
    move_json,
    stub_llm,
    valid_data,
)

pytestmark = pytest.mark.anyio


def turn_input_for(role) -> TurnInput:
    """role の受信口に送る入力(attacker は AttackerTurnInput)。線の上と同じく JSON から作る。"""
    model = AttackerTurnInput if role == "attacker" else TurnInput
    return model.model_validate_json(json.dumps(valid_data(role)))


class RecordingTransport(httpx.AsyncBaseTransport):
    """送ったリクエストを記録して、中の通信路に渡す。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


@pytest.fixture
def use_transport(monkeypatch):
    """send_turn が使う HTTP クライアントの通信路を差し替える。"""

    def install(transport: httpx.AsyncBaseTransport) -> None:
        monkeypatch.setattr(
            client_module,
            "_open_http_client",
            lambda timeout_s: httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout_s)),
        )

    return install


@pytest.fixture
def recorded(use_transport, agents_app) -> RecordingTransport:
    """本物の agents アプリ(スタブの LLM)につなぎ、送ったリクエストを記録する。"""
    transport = RecordingTransport(httpx.ASGITransport(app=agents_app))
    use_transport(transport)
    return transport


async def call(role="candidate", *, nid=NID, timeout_s=5.0) -> dict:
    return await send_turn(BASE_URL, role, turn_input_for(role), nid=nid, timeout_s=timeout_s)


def mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- 成功 ---


@pytest.mark.parametrize("role", ROLES)
async def test_send_turn_returns_the_move_data_as_a_dict(role, recorded, stub_llm):
    # §4.3 (成功は dict。数値軸は int のまま返る)
    data = await call(role)
    assert data == json.loads(move_json())
    assert type(data) is dict
    assert all(type(data["package"][axis]) is int for axis in ("salary", "remote_days", "night_duty", "review_months"))
    assert len(stub_llm.requests) == 1


async def test_send_turn_sends_exactly_one_request_with_one_datapart_and_the_nid_in_metadata(recorded):
    # §2.7・§4.3 (A2A で 1 回だけ送る。DataPart は 1 つで、nid は metadata に載せる。Card は取りに行かない)
    await call("candidate")

    assert len(recorded.requests) == 1
    request = recorded.requests[0]
    assert (request.method, str(request.url)) == ("POST", f"{BASE_URL}/a2a/candidate")
    assert request.headers["A2A-Version"] == "1.0"
    payload = json.loads(request.content)
    assert payload["method"] == "SendMessage"
    message = payload["params"]["message"]
    assert message["metadata"] == {"nid": NID}
    assert len(message["parts"]) == 1
    sent = message["parts"][0]["data"]
    assert sent == json.loads(turn_input_for("candidate").model_dump_json(by_alias=True))


async def test_send_turn_returns_the_move_as_is_without_validating_it(recorded, stub_llm):
    # §4.1・§4.3 (Move としての検証はレフェリーの仕事。グリッド外の手も、そのまま返す)
    stub_llm.behavior = lambda _request: move_json("propose", dict(PACKAGE, salary=610))
    data = await call("candidate")
    assert data["package"]["salary"] == 610


# --- 時間切れ ---


async def test_timeout_raises_timeout_error(use_transport):
    # §4.3 (時間切れは TimeoutError。timeout_s は全体の壁時計)
    async def stalled(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    use_transport(mock_transport(stalled))
    with pytest.raises(TimeoutError) as exc_info:
        await call("candidate", timeout_s=0.2)
    assert type(exc_info.value) is TimeoutError


async def test_httpx_timeouts_raise_timeout_error(use_transport):
    # §4.3 (httpx の段ごとの時間切れ(読み取りなど)も TimeoutError)
    async def slow_read(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    use_transport(mock_transport(slow_read))
    with pytest.raises(TimeoutError) as exc_info:
        await call("candidate")
    assert type(exc_info.value) is TimeoutError


# --- 通信エラー(ConnectionError) ---


def _json_rpc_result(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result})


def _json_rpc_error(error: dict) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "error": error})


BROKEN_RESPONSES = {
    "connection refused": httpx.ConnectError("connection refused"),
    "http 502": httpx.Response(502, text="bad gateway"),
    "http 401": httpx.Response(401, text="unauthorized"),
    "http 413": httpx.Response(413, text="too large"),
    "not json": httpx.Response(200, text="<html>hi</html>"),
    "json array": httpx.Response(200, json=[]),
    "empty object": httpx.Response(200, json={}),
    "unknown result": _json_rpc_result({"foo": 1}),
    "empty result": _json_rpc_result({}),
    "unknown error code": _json_rpc_error({"code": -32050, "message": "x"}),
    "error without code": _json_rpc_error({"message": "x"}),
    "text reply": _json_rpc_result({"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "hi"}]}}),
    "two parts": _json_rpc_result(
        {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"data": {"a": 1}}, {"data": {"b": 2}}]}}
    ),
    "failed task": _json_rpc_result({"task": {"id": "t", "contextId": "c", "status": {"state": "TASK_STATE_FAILED"}}}),
    "data is not an object": _json_rpc_result(
        {
            "task": {
                "id": "t",
                "contextId": "c",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"artifactId": "a", "parts": [{"data": "a string"}]}],
            }
        }
    ),
}


@pytest.mark.parametrize("scenario", list(BROKEN_RESPONSES))
async def test_communication_errors_and_broken_responses_raise_connection_error(scenario, use_transport):
    # §4.3 (A2A の通信エラー(接続・HTTP・形の崩れた応答)は ConnectionError。ValueError(拒否)や、
    # a2a-sdk・protobuf の例外を、そのまま外に出さない)
    outcome = BROKEN_RESPONSES[scenario]

    async def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    use_transport(mock_transport(handler))
    with pytest.raises(ConnectionError) as exc_info:
        await call("candidate")
    assert type(exc_info.value) is ConnectionError


# --- 受信口が返した失敗 ---


@pytest.mark.parametrize(
    "error",
    [
        genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}),
        genai_errors.ServerError(503, {"error": {"code": 503, "message": "down", "status": "UNAVAILABLE"}}),
    ],
    ids=["429", "503"],
)
async def test_transient_endpoint_error_raises_connection_error_and_is_not_retried(error, recorded, stub_llm):
    # §4.3 (受信口が返した一時的なエラーは ConnectionError。再試行はレフェリーの仕事なので、1 回しか送らない)
    def fail(_request):
        raise error

    stub_llm.behavior = fail
    with pytest.raises(ConnectionError, match="temporary") as exc_info:
        await call("candidate")
    assert type(exc_info.value) is ConnectionError
    assert len(recorded.requests) == 1
    assert len(stub_llm.requests) == 1


def _adk_validation_error() -> ValidationError:
    """モデルの出力をスキーマで検証して失敗したときの、pydantic の検証エラー(ADK が投げ得るもの)。"""
    try:
        Move.model_validate({"schema": "move/v1", "move": "withdraw"})
    except ValidationError as exc:
        return exc
    raise AssertionError("the invalid move must be rejected")


# 受信口が「一時的」と印を付けない失敗(LLM の呼び出しの 429・5xx・時間切れ・ネットワークの失敗のどれでもない)。
NON_TRANSIENT_FAILURES = {
    "unexpected_exception": ZeroDivisionError("x"),
    "adk_validation_error": _adk_validation_error(),
    "json_decode_error": json.JSONDecodeError("Expecting value", "not json", 0),
    "bad_request_400": genai_errors.ClientError(
        400, {"error": {"code": 400, "message": "bad request", "status": "INVALID_ARGUMENT"}}
    ),
    "permission_denied_403": genai_errors.ClientError(
        403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}
    ),
}


@pytest.mark.parametrize("failure", list(NON_TRANSIENT_FAILURES))
async def test_a_failure_without_the_transient_mark_raises_value_error_and_is_not_retried(failure, recorded, stub_llm):
    # §4.3・台帳 L9-5 (一時的と印のない実行の失敗は、ValueError。ConnectionError にすると、レフェリーが送り直して、
    # temperature 0 では同じ失敗を 4 回繰り返し、agent_timeout になる。受信口には 1 回しか送らない)
    def fail(_request):
        raise NON_TRANSIENT_FAILURES[failure]

    stub_llm.behavior = fail
    with pytest.raises(ValueError, match="non-transient") as exc_info:
        await call("candidate")
    assert type(exc_info.value) is ValueError
    assert "temporary" not in str(exc_info.value)
    assert len(recorded.requests) == 1
    assert len(stub_llm.requests) == 1


@pytest.mark.parametrize("output", ["hello", "[1, 2]", "", "123", "{not json"])
async def test_a_model_output_that_is_not_a_json_object_raises_value_error(output, recorded, stub_llm):
    # §4.3・台帳 L9-5 (LLM の出力が JSON のオブジェクトでない(空・配列・数値・壊れた JSON を含む)のは、一時的ではない失敗。
    # ValueError で、レフェリーが schema_invalid にする)
    stub_llm.behavior = lambda _request: output
    with pytest.raises(ValueError, match="non-transient") as exc_info:
        await call("candidate")
    assert type(exc_info.value) is ValueError
    assert len(stub_llm.requests) == 1


async def test_an_internal_error_response_without_the_transient_mark_raises_value_error(use_transport):
    # §4.3・台帳 L9-5 (受信口が返した InternalError に、transient の印がない(または true でない)なら、ValueError。
    # 印が true のものは ConnectionError のまま。印の付き方が崩れても、ConnectionError にして再試行を続けない)
    def error_response(data) -> httpx.Response:
        error: dict[str, Any] = {"code": -32603, "message": "agent execution failed"}
        if data is not None:
            error["data"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "x", "metadata": data}]
        return _json_rpc_error(error)

    for data in (None, {"transient": "false"}, {"other": "true"}):
        use_transport(mock_transport(lambda request, data=data: error_response(data)))
        with pytest.raises(ValueError, match="non-transient"):
            await call("candidate")

    use_transport(mock_transport(lambda request: error_response({"transient": "true"})))  # 対照
    with pytest.raises(ConnectionError, match="temporary") as exc_info:
        await call("candidate")
    assert type(exc_info.value) is ConnectionError


# --- 受信口が入力を拒否した(ValueError) ---


@pytest.mark.parametrize("role", ["candidate", "employer"])
async def test_attacker_input_rejected_by_the_other_endpoints_raises_value_error(role, recorded, stub_llm):
    # §4.3・DV-04 (AttackerTurnInput を候補者側・求人側の受信口へ送ると、拒否されて ValueError。理由が分かる)
    with pytest.raises(ValueError, match="principal_instruction") as exc_info:
        await send_turn(BASE_URL, role, turn_input_for("attacker"), nid=NID, timeout_s=5)
    assert type(exc_info.value) is ValueError
    assert stub_llm.requests == []


async def test_malformed_nid_rejected_by_the_endpoint_raises_value_error(recorded, stub_llm):
    # §2.7・§4.3 (ID の形式違反は、受信口が拒否して ValueError)
    with pytest.raises(ValueError, match="ID"):
        await call("candidate", nid="not-an-id-string")
    assert stub_llm.requests == []


async def test_oversize_body_rejected_by_the_endpoint_raises_value_error(recorded, stub_llm):
    # §4.3 (32 KB を超える本文は、受信口が拒否して ValueError)
    turn_input = turn_input_for("candidate")
    entry = turn_input.history[0]
    oversized = turn_input.model_copy(update={"history": [entry] * 400})
    with pytest.raises(ValueError, match="too large"):
        await send_turn(BASE_URL, "candidate", oversized, nid=NID, timeout_s=5)
    assert stub_llm.requests == []


async def test_unknown_role_raises_value_error():
    # §4.3 (role は 3 つのどれか)
    with pytest.raises(ValueError, match="unknown role"):
        bad_role: Any = "referee"
        await send_turn(BASE_URL, bad_role, turn_input_for("candidate"), nid=NID, timeout_s=5)


# --- 別に作ったアプリにも同じように動く ---


async def test_send_turn_works_against_a_separately_created_app(use_transport):
    # §4.3 (クライアントは、受信口の場所(role)だけで送る。アプリの作り方に依存しない)
    app = create_app(model=StubLlm())
    use_transport(httpx.ASGITransport(app=app))
    assert (await call("employer"))["move"] == "propose"
