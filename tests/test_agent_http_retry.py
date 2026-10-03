"""DV-17 の agents 側: 偽の HTTP 層で「HTTP の要求が 1 回」(design.md §4.2・台帳 X-55・C-54)。

クライアント側の自動再試行を切る(ADK の `Gemini` に `retry_options=HttpRetryOptions(attempts=1)` を渡す)ことは、スタブの
BaseLlm では確かめられない(スタブは google-genai の再試行の下を通らない)。そこで、本物の `Gemini` モデルクラスを使い、その
下の HTTP の通信路だけを、httpx の MockTransport(Vertex AI の代わりに 429・503 などを返す偽物)に差し替えて、HTTP の要求が
何回出るかを数える。本物の Vertex AI には出ない形にする:

- 通信路は、google-genai の `http_options.async_client_args={"transport": ...}` で差し込む(カスタムの transport があれば、
  google-genai は aiohttp ではなく httpx を使う)。ADK が組み立てるクライアントの設定(再試行の設定・ヘッダー)は、そのまま使う。
  `google.genai.Client` をラップして、transport と、偽の認証情報(ネットワークに出ない)・プロジェクト・場所だけを足す。
- 名前解決(socket.getaddrinfo)を禁止して、偽の通信路をすり抜けた要求が、本物の API に出ずに失敗するようにする。
- 対照: 再試行を残した `Gemini`(attempts=3)では、同じ偽の通信路が要求を 3 回数える。数え方が空振りしていないことを示す。

あわせて、起動時の検証(設定の再試行の回数が 1 でなければ、起動を止める)を確かめる。
"""

import dataclasses
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass, field

import google.auth.credentials
import google.genai
import httpx
import pytest
from google.adk.models.google_llm import Gemini
from google.genai import types
from google.protobuf import json_format, struct_pb2
from negotiation_core.schema import Plan

from agents.app import create_app
from agents.config import DEFAULT_AGENTS_CONFIG
from agents.instructions import load_instruction
from agents.llm_agents import build_gemini_model, require_no_http_retry
from agents.wire import PHASES, value_to_python
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    PACKAGE,
    StubLlm,
    anyio_backend,
    asgi_client,
    data_part,
    move_data_of,
    plan_json,
    send_message,
    usage_of,
    valid_data,
)

pytestmark = pytest.mark.anyio

MAX_OUTPUT_TOKENS = DEFAULT_AGENTS_CONFIG.max_output_tokens

# --- 起動時の検証 ---


def test_the_gemini_model_is_built_with_the_client_side_retries_off():
    # §4.2・台帳 X-55 (ADK の Gemini に、retry_options=HttpRetryOptions(attempts=1) を渡す。モデル名は設定のとおり)
    model = build_gemini_model(DEFAULT_AGENTS_CONFIG)
    assert isinstance(model, Gemini)
    assert model.model == DEFAULT_AGENTS_CONFIG.model
    assert model.retry_options is not None
    assert model.retry_options.attempts == 1 == DEFAULT_AGENTS_CONFIG.http_retry_attempts


@pytest.mark.parametrize("attempts", [0, 2, 3, 5, -1])
def test_startup_stops_when_the_config_leaves_the_client_side_retries_on(attempts):
    # §4.2・台帳 X-55 (起動時(アプリの組み立て時)に、設定の http_retry_attempts が 1 であることを検証し、違えば起動を止める。
    # 0 も止める(再試行なしと同じ動きでも、設定は 1 と決めてある))
    config = dataclasses.replace(DEFAULT_AGENTS_CONFIG, http_retry_attempts=attempts)
    with pytest.raises(ValueError, match="http_retry_attempts must be 1"):
        create_app(config=config)
    with pytest.raises(ValueError, match="http_retry_attempts must be 1"):
        build_gemini_model(config)
    with pytest.raises(ValueError, match="http_retry_attempts must be 1"):
        require_no_http_retry(config)


def test_startup_checks_the_config_even_when_a_stub_model_is_given():
    # §4.2・台帳 X-55 (検証は設定に対して行う。テスト用のスタブを差し込んでも、設定が違えば止まる)
    config = dataclasses.replace(DEFAULT_AGENTS_CONFIG, http_retry_attempts=3)
    with pytest.raises(ValueError, match="http_retry_attempts must be 1"):
        create_app(model=StubLlm(), config=config)


def test_a_config_with_one_attempt_starts():
    # §4.2 (対照: 設定が 1 なら起動する)
    assert DEFAULT_AGENTS_CONFIG.http_retry_attempts == 1
    require_no_http_retry(DEFAULT_AGENTS_CONFIG)
    assert create_app().state.runners


# --- 偽の HTTP 層 ---


class _FakeCredentials(google.auth.credentials.Credentials):
    """ネットワークに出ない偽の認証情報(トークンが入っていて、期限切れでない)。更新が呼ばれたら失敗する。"""

    def __init__(self) -> None:
        super().__init__()
        self.token = "fake-token-for-tests"

    def refresh(self, request) -> None:
        raise AssertionError("the fake credentials must never be refreshed")


@dataclass
class FakeVertex:
    """Vertex AI の代わりの偽の HTTP 層。受けた要求を記録し、respond が返す応答を返す(既定は 429)。"""

    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[google.genai.Client] = field(default_factory=list)  # ADK が組み立てた(通信路だけ偽の)google-genai のクライアント
    respond: Callable[[httpx.Request], httpx.Response] = lambda request: error_response(429)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "aiplatform.googleapis.com", f"unexpected host: {request.url.host}"
        self.requests.append(request)
        return self.respond(request)

    def bodies(self) -> list[dict]:
        """受けた要求の本文(JSON)。"""
        return [json.loads(request.content) for request in self.requests]


_ERROR_STATUS = {429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 503: "UNAVAILABLE"}


def error_response(code: int) -> httpx.Response:
    """Vertex AI が返すエラーの形(429・5xx)。"""
    body = {"error": {"code": code, "message": "fake error for tests", "status": _ERROR_STATUS[code]}}
    return httpx.Response(code, json=body)


def generate_content_response(text: str | None, *, finish_reason: str = "STOP", usage: dict | None = None) -> httpx.Response:
    """Vertex AI の generateContent が返す 200 の形(text があれば応答の本文、なければ本文なし)。"""
    candidate: dict = {"finishReason": finish_reason, "index": 0}
    if text is not None:
        candidate["content"] = {"role": "model", "parts": [{"text": text}]}
    usage = usage or {"promptTokenCount": 1500, "candidatesTokenCount": 40, "thoughtsTokenCount": 200, "totalTokenCount": 1740}
    return httpx.Response(200, json={"candidates": [candidate], "usageMetadata": usage, "modelVersion": "fake"})


@pytest.fixture
def fake_vertex(monkeypatch) -> FakeVertex:
    """本物の Gemini モデルクラスの下の HTTP の通信路を、偽の Vertex AI に差し替える(本物の API には出ない)。"""
    fake = FakeVertex()
    transport = httpx.MockTransport(fake.handler)
    real_client = google.genai.Client

    def client_with_fake_transport(**kwargs):
        # ADK が組み立てた http_options(retry_options・ヘッダー・base_url)はそのまま使い、通信路だけを差し替える。
        http_options = kwargs.pop("http_options").model_copy(update={"async_client_args": {"transport": transport}})
        client = real_client(
            vertexai=True,
            project="test-project",
            location="global",
            credentials=_FakeCredentials(),
            http_options=http_options,
            **kwargs,
        )
        fake.clients.append(client)
        return client

    monkeypatch.setattr(google.genai, "Client", client_with_fake_transport)

    def refuse_name_resolution(*args, **kwargs):
        raise OSError("name resolution is refused in these tests: nothing may reach a real API")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_name_resolution)
    return fake


def test_the_fake_http_layer_cannot_reach_a_real_api(fake_vertex):
    # この試験の前提(DV-17: 本物の API には出ない形にする): 偽の通信路をすり抜けた要求は、名前解決で失敗する
    with pytest.raises(OSError, match="refused"):
        socket.getaddrinfo("aiplatform.googleapis.com", 443)


async def _post_plan(app, role="candidate", phase="plan") -> dict:
    async with asgi_client(app) as http:
        return await send_message(http, role, [data_part(valid_data(role, phase))])


# --- HTTP の要求が 1 回(台帳 X-55・C-54) ---


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.parametrize("phase", PHASES)
async def test_a_429_or_5xx_from_vertex_ai_produces_exactly_one_http_request(status, phase, fake_vertex):
    # §4.2・台帳 X-55・C-54・DV-17 (本物の Gemini モデルクラスの下を、429・5xx を返す偽の通信路に差し替えると、HTTP の要求は
    # 1 回だけ出る。再試行はレフェリーだけが行い、レフェリーの 1 計上が Vertex AI への要求 1 回に対応する。受信口は、
    # 一時的なエラー(レフェリーが再試行する)として返す)
    fake_vertex.respond = lambda request: error_response(status)
    app = create_app()  # スタブを差し込まない: 設定のモデル名の、本物の ADK Gemini(attempts=1)

    body = await _post_plan(app, phase=phase)

    assert len(fake_vertex.requests) == 1
    request = fake_vertex.requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/publishers/google/models/gemini-3.5-flash:generateContent")  # Vertex AI の呼び出し
    assert "result" not in body
    assert body["error"]["code"] == -32603
    info = [d for d in body["error"]["data"] if d["@type"].endswith("ErrorInfo")][0]["metadata"]
    assert info.get("transient") == "true"


async def test_every_call_is_one_http_request_even_across_many_calls(fake_vertex):
    # §4.2・台帳 X-55 (呼び出しごとに 1 回。受信口の呼び出しを重ねても、要求の数は呼び出しの数と同じ)
    app = create_app()
    for _ in range(3):
        await _post_plan(app)
    assert len(fake_vertex.requests) == 3


async def test_with_the_retries_left_on_the_same_fake_http_layer_counts_three_requests(fake_vertex):
    # 台帳 X-49・X-55 の対照 (数え方が空振りしていないことの確認: 再試行を残した Gemini(attempts=3)を同じ偽の通信路につなぐと、
    # 429 に対して要求が 3 回出る。attempts=1 の「1 回」は、再試行を切ったためで、数えそこなったためではない)
    fake_vertex.respond = lambda request: error_response(429)
    retrying = Gemini(
        model=DEFAULT_AGENTS_CONFIG.model,
        retry_options=types.HttpRetryOptions(attempts=3, initial_delay=0.001, max_delay=0.005, jitter=0.001),
    )
    app = create_app(model=retrying)

    body = await _post_plan(app)

    assert len(fake_vertex.requests) == 3
    assert body["error"]["code"] == -32603


async def test_the_retry_options_given_to_the_gemini_reach_the_google_genai_client(fake_vertex):
    # §4.2・台帳 X-55 (入れている google-genai 2.25.0 は、retry_options がなければ再試行しないので、「要求が 1 回」と数えただけでは、
    # attempts=1 を渡した配線の保証にならない(版が変われば、既定が再試行ありになり得る)。ADK の Gemini に渡した retry_options が、
    # google-genai のクライアントの設定になり、再試行の方針(tenacity)が 1 回で止まる設定になっていることを、直接確かめる。
    # クライアントの中身(_api_client・_http_options・_async_retry)は google-genai の内部で、版が変われば、ここで気づく)
    await _post_plan(create_app())

    (client,) = fake_vertex.clients  # すべての LlmAgent が 1 つの Gemini を共有し、クライアントは(イベントループごとに)1 つ
    api_client = client._api_client
    assert api_client._http_options.retry_options is not None
    assert api_client._http_options.retry_options.attempts == 1
    assert api_client._async_retry.stop.max_attempt_number == 1
    # 対照: 再試行を残した Gemini は、同じ経路で attempts=3 のクライアントになる(配線が、渡した値を運んでいることの確認)
    retrying = Gemini(model=DEFAULT_AGENTS_CONFIG.model, retry_options=types.HttpRetryOptions(attempts=3, initial_delay=0.001))
    await _post_plan(create_app(model=retrying))
    assert fake_vertex.clients[-1]._api_client._async_retry.stop.max_attempt_number == 3


# --- 本物の Gemini モデルクラスを通した、リクエストの中身と応答の取り込み ---


@pytest.mark.parametrize("phase", PHASES)
async def test_the_http_request_carries_the_configured_generation_settings(phase, fake_vertex):
    # §4.2・DV-17 (Vertex AI に実際に出る HTTP の要求の本文に、設定どおりの thinking_level(計画と決定で別)・temperature・
    # maxOutputTokens・出力スキーマ・前文・TurnInput が載る。スタブの BaseLlm では見えない、線の上の確認)
    fake_vertex.respond = lambda request: generate_content_response(plan_json("accept") if phase == "plan" else '{"schema": "move/v1", "move": "end"}')
    body = await _post_plan(create_app(), phase=phase)
    assert "error" not in body, body

    (sent,) = fake_vertex.bodies()
    config = sent["generationConfig"]
    expected_level = DEFAULT_AGENTS_CONFIG.plan_thinking_level if phase == "plan" else DEFAULT_AGENTS_CONFIG.decide_thinking_level
    # google-genai 2.25.0 は、thinkingConfig の中のキーを snake_case(thinking_level)のまま送る(Vertex AI は proto の
    # フィールド名も受け付ける。実機で効いていることは、DV-15 の thoughts_token_count で確かめる)。どちらの綴りでも通す。
    thinking = config["thinkingConfig"]
    assert (thinking.get("thinkingLevel") or thinking.get("thinking_level")) == expected_level
    assert config["maxOutputTokens"] == DEFAULT_AGENTS_CONFIG.max_output_tokens
    assert config["temperature"] == DEFAULT_AGENTS_CONFIG.temperature == 0
    assert config["responseMimeType"] == "application/json"
    assert "responseSchema" not in config  # 計画・決定とも JSON モード(応答スキーマなし。台帳 I-19)
    assert "responseJsonSchema" not in config  # どちらの形の応答スキーマも実ペイロードに付かない(制約付きデコードに戻らない。台帳 X-68)
    assert sent["systemInstruction"]["parts"] == [{"text": load_instruction("candidate")}]  # 前文だけ
    (content,) = sent["contents"]
    assert content["role"] == "user" and len(content["parts"]) == 1  # TurnInput の JSON が 1 件だけ
    assert json.loads(content["parts"][0]["text"])["phase"] == phase
    assert "tools" not in sent


async def test_the_usage_is_built_from_the_http_response_of_the_real_model_class(fake_vertex):
    # §4.3・台帳 X-58 (本物の Gemini モデルクラスが返す usage_metadata(promptTokenCount・cachedContentTokenCount・
    # thoughtsTokenCount・candidatesTokenCount)から usage が作られる。model は設定の値、requests は 1)
    fake_vertex.respond = lambda request: generate_content_response(
        plan_json("accept"),
        usage={
            "promptTokenCount": 1500,
            "cachedContentTokenCount": 100,
            "candidatesTokenCount": 40,
            "thoughtsTokenCount": 200,
            "totalTokenCount": 1740,
        },
    )
    body = await _post_plan(create_app())

    assert usage_of(body) == {
        "model": DEFAULT_AGENTS_CONFIG.model,
        "prompt_tokens": 1500,
        "cached_tokens": 100,
        "thoughts_tokens": 200,
        "output_tokens": 40,
        "requests": 1,
    }
    assert len(fake_vertex.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        generate_content_response(
            '{"schema": "plan/v1", "checks": [{"salary": "65', finish_reason="MAX_TOKENS",
            usage={"promptTokenCount": 900, "candidatesTokenCount": 48, "thoughtsTokenCount": MAX_OUTPUT_TOKENS - 48},
        ),
        generate_content_response(
            None, finish_reason="MAX_TOKENS",
            usage={"promptTokenCount": 900, "thoughtsTokenCount": MAX_OUTPUT_TOKENS},
        ),
    ],
    ids=["cut_in_the_middle", "thoughts_used_everything"],
)
async def test_max_tokens_from_the_real_model_class_is_a_truncated_error_with_usage(response, fake_vertex):
    # §4.3・台帳 C-53・DV-17 (Vertex AI が finishReason=MAX_TOKENS を返すと、受信口は truncated の印と usage つきの
    # 一時的でないエラーを返す。本物のモデルクラス経由で finish_reason が取れることの確認。要求は 1 回)
    fake_vertex.respond = lambda request: response
    body = await _post_plan(create_app())

    assert "result" not in body
    assert body["error"]["code"] == -32603
    info = [d for d in body["error"]["data"] if d["@type"].endswith("ErrorInfo")][0]["metadata"]
    assert info["truncated"] == "true" and "transient" not in info
    usage = info["usage"]
    assert usage["model"] == DEFAULT_AGENTS_CONFIG.model and usage["requests"] == 1
    assert usage["prompt_tokens"] == 900
    assert usage["thoughts_tokens"] + usage["output_tokens"] <= MAX_OUTPUT_TOKENS
    assert len(fake_vertex.requests) == 1


async def test_a_plan_from_the_real_model_class_validates_as_a_plan(fake_vertex):
    # §2.7・§4.2 (本物のモデルクラス経由で、STRING の enum の数値軸(例: "650")を整数に戻した Plan が返り、Plan として検証が通る)
    as_strings = {**PACKAGE, **{axis: str(PACKAGE[axis]) for axis in ("salary", "remote_days", "night_duty", "review_months")}}
    fake_vertex.respond = lambda request: generate_content_response(json.dumps({"schema": "plan/v1", "checks": [as_strings]}))
    body = await _post_plan(create_app())

    restored = value_to_python(json_format.ParseDict(move_data_of(body), struct_pb2.Value()))  # 線の上の double(650.0)を整数に戻す
    assert Plan.model_validate_json(json.dumps(restored)).checks[0].salary == PACKAGE["salary"]
