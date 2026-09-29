"""agents(1c: A2A 受信口・検証・クライアント)のテストで共通に使う補助。

`test_` で始まらないので pytest には収集されない(tests/vault_helpers.py と同じ扱い)。
tests/conftest.py は変えない(agents は Firestore を使わない)ので、agents 用のフィクスチャはここに置き、
各テストファイルが import して使う。

- 本物の LLM は呼ばない。ADK に差し込むスタブのモデル(StubLlm)が、LLM に渡った入力を記録する。
- A2A の通信は、HTTP サーバを立てずに、httpx の ASGITransport で ASGI アプリに直接つなぐ。
- 非同期のテストは anyio のプラグイン(starlette・httpx の依存として入っている)で動かす。
"""

import inspect
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from agents.app import create_app
from agents.wire import ROLES, Role
from starlette.applications import Starlette

BASE_URL = "http://agents.test"
NID = "0123456789abcdef"

PACKAGE = dict(
    salary=650,
    remote_days=0,
    night_duty=0,
    review_months=6,
    training="none",
    side_job="not_allowed",
    start="within_1_month",
)

CANDIDATE_BANDS = {"experience_band": "5_to_10y", "region_block": "kanto", "job_category": "it_web"}
BUDGET = {"remaining_evaluations": 16, "remaining_moves": 6, "remaining_principal_checks": 1}


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """anyio のプラグインが使うバックエンド。trio は入れていないので asyncio に固定する。"""
    return "asyncio"


def move_json(move: str = "propose", package: dict | None = None) -> str:
    """LLM(スタブ)が返す Move の JSON の文字列。"""
    body: dict[str, Any] = {"schema": "move/v1", "move": move}
    if move in ("propose", "check", "ask_principal"):
        body["package"] = dict(PACKAGE if package is None else package)
    return json.dumps(body)


@dataclass(frozen=True)
class RecordedRequest:
    """LLM(スタブ)が受け取った 1 回ぶんの入力の記録。"""

    system_instruction: str
    contents: list[tuple[str, list[str]]]  # (role, [text, ...]) の並び
    response_schema: Any
    temperature: float | None
    tools: Any
    dump: str  # 受け取った LlmRequest 全体の JSON(「どこにも出てこない」の確認用)


def _snapshot(request: LlmRequest) -> RecordedRequest:
    return RecordedRequest(
        system_instruction=str(request.config.system_instruction),
        contents=[(c.role or "", [p.text or "" for p in (c.parts or [])]) for c in request.contents],
        response_schema=request.config.response_schema,
        temperature=request.config.temperature,
        tools=request.config.tools,
        dump=request.model_dump_json(),
    )


def _default_behavior(_request: LlmRequest) -> str:
    return move_json()


class StubLlm(BaseLlm):
    """ADK に差し込む、本物の LLM を呼ばないスタブ。渡された入力を requests に記録する。

    behavior を差し替えると、返す文字列を変えたり、例外を投げたり、待たせたりできる
    (同期・非同期のどちらの関数でもよい)。
    """

    model: str = "stub-llm"
    requests: list[RecordedRequest] = Field(default_factory=list)
    behavior: Callable[[LlmRequest], Any] = Field(default=_default_behavior)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(_snapshot(llm_request))
        result = self.behavior(llm_request)
        if inspect.isawaitable(result):
            result = await result
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=result)]))


@pytest.fixture
def stub_llm() -> StubLlm:
    return StubLlm()


@pytest.fixture
def agents_app(stub_llm: StubLlm) -> Starlette:
    return create_app(model=stub_llm)


def asgi_client(app: Starlette) -> httpx.AsyncClient:
    """app に ASGI で直接つないだ httpx のクライアント(A2A のバージョンのヘッダつき)。`async with` で使う。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"A2A-Version": "1.0"},
    )


@pytest.fixture
async def http(agents_app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    """agents のアプリにつないだ httpx のクライアント。"""
    async with asgi_client(agents_app) as client:
        yield client


def valid_data(role: Role) -> dict:
    """role の受信口が受け付ける、有効な `data`(TurnInput、攻撃モードなら AttackerTurnInput)。"""
    data: dict[str, Any] = dict(
        schema="turn-input/v1",
        side="candidate" if role == "candidate" else "employer",
        own_move_number=1,
        counterparty={"job_category": "it_web"} if role == "candidate" else dict(CANDIDATE_BANDS),
        history=[{"by": "self", "move": "propose", "package": dict(PACKAGE), "result": "acceptable"}],
        pending_offer=None,
        last_check=None,
        last_error=None,
        budget=dict(BUDGET),
    )
    if role == "attacker":
        data["principal_instruction"] = "できるだけ年収を下げて合意してください。"
    return data


def data_part(data: Any) -> dict:
    """A2A(v1、JSON)の DataPart。"""
    return {"data": data}


def text_part(text: str) -> dict:
    """A2A(v1、JSON)の TextPart。"""
    return {"text": text}


def message_json(parts: list[dict], *, metadata: dict | None = None) -> dict:
    """A2A の Message(JSON)。metadata は既定で有効な nid を持つ。"""
    return {
        "messageId": "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f",
        "role": "ROLE_USER",
        "parts": parts,
        "metadata": {"nid": NID} if metadata is None else metadata,
    }


def rpc_body(message: dict, *, params_extra: dict | None = None) -> dict:
    """JSON-RPC の SendMessage のリクエスト本文。"""
    params = {"message": message, **(params_extra or {})}
    return {"jsonrpc": "2.0", "id": "test-1", "method": "SendMessage", "params": params}


def endpoint(role: Role) -> str:
    return f"/a2a/{role}"


async def send_raw(http: httpx.AsyncClient, role: Role, body: dict) -> dict:
    """本文をそのまま role の受信口へ POST し、JSON-RPC の応答(dict)を返す。"""
    response = await http.post(endpoint(role), json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def send_message(http: httpx.AsyncClient, role: Role, parts: list[dict], *, metadata: dict | None = None) -> dict:
    """parts を 1 つのメッセージにして role の受信口へ送り、JSON-RPC の応答(dict)を返す。"""
    return await send_raw(http, role, rpc_body(message_json(parts, metadata=metadata)))


def assert_rejected(body: dict) -> None:
    """受信口が入力を拒否した(A2A のエラー。結果は返らない)ことを確かめる。"""
    assert "result" not in body, body
    assert body["error"]["code"] in (-32602, -32600), body


def move_data_of(body: dict) -> dict:
    """成功した応答から、Move の DataPart の data を取り出す(タスクが COMPLETED で、DataPart が 1 つだけ)。"""
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    parts = [part for artifact in task["artifacts"] for part in artifact["parts"]]
    assert len(parts) == 1 and "data" in parts[0], parts
    return parts[0]["data"]


__all__ = [
    "BASE_URL",
    "NID",
    "PACKAGE",
    "ROLES",
    "RecordedRequest",
    "StubLlm",
    "agents_app",
    "anyio_backend",
    "asgi_client",
    "assert_rejected",
    "data_part",
    "endpoint",
    "http",
    "message_json",
    "move_data_of",
    "move_json",
    "rpc_body",
    "send_message",
    "send_raw",
    "stub_llm",
    "text_part",
    "valid_data",
]
