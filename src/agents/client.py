"""web(レフェリー)から交渉エージェントの受信口を呼ぶクライアント(design.md §4.1・§4.3)。

a2a-sdk のクライアントで、受信口へ TurnInput(または AttackerTurnInput)を DataPart で 1 回だけ送り、返ってきた応答の
封筒を検証して、`(payload, usage)` を返す。payload は DataPart の `data`(dict。`TurnInput.phase` が plan なら Plan、
decide なら Move の形)、usage はその呼び出しの使用量(`negotiation_core.schema.Usage`)。

- Plan・Move としての検証はしない。再試行もしない。どちらもレフェリーの仕事(§4.1・§4.3)。
- Agent Card は取りに行かない(受信口の場所は role から決まる)。Card は探索用に公開してある。
- 応答の封筒の検証(§4.3・台帳 X-58。X-41 の検証を応答にも延ばしたもの)。完了したタスクが 1 件、artifact が 1 件、その parts は
  DataPart 1 件(data は JSON のオブジェクト)、artifact の metadata は `usage` だけで、usage は `Usage` として strict に
  検証する(未知の項目・欠落・負の値・型違いは拒否)。トークン数は上限の範囲(prompt_tokens は設定の max_prompt_tokens 以下、
  output_tokens ＋ thoughts_tokens は設定の max_output_tokens 以下、cached_tokens は prompt_tokens 以下)で、model は
  設定のモデル名と一致すること。違えば ValueError。設定は、send_turn の config 引数(既定は DEFAULT_AGENTS_CONFIG)。
- 失敗は、組み込みの例外(と、ValueError の派生の TruncatedOutputError)だけで伝える。

| 起きたこと | 例外 |
|---|---|
| 時間切れ(timeout_s。全体の壁時計) | `TimeoutError` |
| A2A の通信エラー(接続できない・HTTP エラー・応答の形が A2A でない・完了していないタスク) | `ConnectionError` |
| 受信口が返した一時的なエラー(LLM の 429・5xx・時間切れ。`transient` の印つき) | `ConnectionError`(レフェリーが再試行し、駄目なら agent_timeout にする) |
| 受信口が返した、出力が `max_output_tokens` で切れたエラー(`truncated` の印つき。台帳 C-53) | `TruncatedOutputError`(ValueError の派生。属性 `usage` は、エラーに付いた使用量。検証に通らなければ None。レフェリーは output_truncated の無効手にする) |
| 受信口が返した、一時的と印のない失敗(LLM の出力が JSON でない・ADK の検証エラーなど。台帳 L9-5) | `ValueError`(同じ入力を送り直しても直らないので、レフェリーは再試行せず schema_invalid にする) |
| 受信口が入力を拒否した(壁 1。スキーマ違反・大きすぎる本文など) | `ValueError` |
| 応答の封筒の違反(上の検証。タスクでなくメッセージで返った・タスクや artifact や part が 1 件でない・DataPart でない・metadata が usage 以外・usage が不正) | `ValueError`(レフェリーは schema_invalid にする) |

認証(台帳 X-37): `auth` を渡すと、その httpx の認証(`web.service_auth.IdTokenAuth`。呼び先の URL を audience にした
ID トークンを `Authorization: Bearer` で付ける)を、この呼び出しの HTTP クライアントに付ける。渡さなければ付けない
(ローカル・テスト)。agents の側は、Cloud Run の IAM で認証を必須にする(§1.1)ので、アプリの中での検証はしない。
"""

import asyncio
import json
import uuid

import httpx
from a2a.client import A2AClientError, A2AClientTimeoutError, ClientConfig, ClientFactory
from a2a.helpers import new_data_part
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    Message,
    SendMessageRequest,
    StreamResponse,
    TaskState,
)
from a2a.types import Role as A2ARole
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from a2a.utils.errors import A2AError, InternalError, InvalidParamsError, InvalidRequestError
from google.protobuf.json_format import ParseError
from pydantic import ValidationError

from agents.config import DEFAULT_AGENTS_CONFIG, AgentsConfig
from agents.wire import (
    DATA_MEDIA_TYPE,
    ROLES,
    TRANSIENT_ERROR_KEY,
    TRUNCATED_ERROR_KEY,
    USAGE_KEY,
    Role,
    endpoint_path,
    part_kind,
    struct_to_python,
    value_to_python,
)
from negotiation_core.schema import AttackerTurnInput, TurnInput, Usage


class TruncatedOutputError(ValueError):
    """出力が `max_output_tokens` で切れた(受信口が `truncated` の印つきの InternalError を返した。台帳 C-53)。

    再試行しても同じ入力では同じように切れ得るので、ValueError の派生にして、レフェリーが output_truncated の無効手として
    登録する(次の呼び出しの last_error で、モデルに「短く答える」手がかりを渡す)。usage は、エラーに付いてきた使用量
    (切れた呼び出しも課金されるので、費用の記録に使う)。付いていない・検証に通らないときは None。
    """

    def __init__(self, message: str, *, usage: Usage | None) -> None:
        super().__init__(message)
        self.usage = usage


def _open_http_client(timeout_s: float) -> httpx.AsyncClient:
    """HTTP クライアントを作る(テストは、ここを ASGI の口につなぐものに差し替える)。"""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))


def _card_for(url: str) -> AgentCard:
    """受信口 url を呼ぶための、a2a-sdk のクライアント用の最小の Card(取りに行かずに作る)。"""
    return AgentCard(
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=[DATA_MEDIA_TYPE],
        default_output_modes=[DATA_MEDIA_TYPE],
    )


def _validated_usage(raw: object, config: AgentsConfig) -> Usage:
    """応答の usage を Usage として strict に検証し、設定(config)の上限の範囲を確かめる。違えば ValueError(値は、メッセージに載せない)。"""
    if not isinstance(raw, dict):
        raise ValueError("the agent response usage is not a JSON object")
    try:
        # 線の上のデータは JSON なので、JSON として検証する(スキーマは strict。agents.validation と同じ方法)。
        usage = Usage.model_validate_json(json.dumps(raw))
    except ValidationError as exc:
        # 場所は、スキーマが決めた項目名だけ。未知の項目の名前は、相手が作れる値なので、載せずに `<unknown>` にする(台帳 X-43 と同じ方針)。
        fields = sorted(
            {
                "<unknown>" if error["type"] == "extra_forbidden" else ".".join(str(part) for part in error["loc"]) or "<usage>"
                for error in exc.errors()
            }
        )
        raise ValueError(f"the agent response usage is invalid (fields: {', '.join(fields)})") from None
    if usage.model != config.model:
        raise ValueError("the agent response usage names a model other than the configured one")
    if usage.prompt_tokens > config.max_prompt_tokens:
        raise ValueError(f"the agent response usage has more than {config.max_prompt_tokens} prompt tokens")
    if usage.output_tokens + usage.thoughts_tokens > config.max_output_tokens:
        raise ValueError(f"the agent response usage has more than {config.max_output_tokens} output and thought tokens")
    if usage.cached_tokens > usage.prompt_tokens:
        raise ValueError("the agent response usage has more cached tokens than prompt tokens")
    if usage.requests != 1:
        # 台帳 X-61: 1 計上 = Vertex AI への要求 1 回(§4.1)。複数の要求が出た応答は受け付けない
        raise ValueError("the agent response usage must report exactly one model request")
    return usage


def _checked_response(responses: list[StreamResponse], config: AgentsConfig) -> tuple[dict, Usage]:
    """応答の封筒を検証し、DataPart の `data`(dict)と、その呼び出しの usage を返す(台帳 X-58)。

    A2A の形でない・完了していないタスクは ConnectionError、それ以外の封筒の違反は ValueError。
    """
    if len(responses) != 1:
        raise ValueError(f"the agent endpoint must return exactly one response (got {len(responses)})")
    response = responses[0]
    if not response.HasField("task"):
        if response.HasField("message"):
            raise ValueError("the agent response is a message, not a completed task")
        raise ConnectionError("the agent response has neither a task nor a message")
    task = response.task
    if task.status.state != TaskState.TASK_STATE_COMPLETED:
        raise ConnectionError(f"the agent task ended in state {TaskState.Name(task.status.state)}")
    if len(task.artifacts) != 1:
        raise ValueError(f"the agent response must contain exactly one artifact (got {len(task.artifacts)})")
    artifact = task.artifacts[0]
    if len(artifact.parts) != 1 or part_kind(artifact.parts[0]) != "data":
        raise ValueError("the agent response artifact must contain exactly one DataPart")
    data = value_to_python(artifact.parts[0].data)
    if not isinstance(data, dict):
        raise ValueError("the agent response DataPart is not a JSON object")
    metadata = struct_to_python(artifact.metadata)
    if set(metadata) != {USAGE_KEY}:
        raise ValueError("the agent response artifact metadata must contain only usage")
    return data, _validated_usage(metadata[USAGE_KEY], config)


def _usage_in_error(data: dict, config: AgentsConfig) -> Usage | None:
    """切れた出力のエラー(truncated の印つき)の data にある usage を検証して返す。付いていない・不正なら None。"""
    try:
        return _validated_usage(data.get(USAGE_KEY), config)
    except ValueError:
        return None


async def send_turn(
    base_url: str,
    role: Role,
    turn_input: TurnInput | AttackerTurnInput,
    *,
    nid: str,
    timeout_s: float,
    auth: httpx.Auth | None = None,
    config: AgentsConfig = DEFAULT_AGENTS_CONFIG,
) -> tuple[dict, Usage]:
    """role の受信口へ turn_input を 1 回だけ送り、返ってきた DataPart の `data`(dict)と、その呼び出しの usage を返す。

    base_url は agents サービスの URL(例: `https://agents.example`)。送り先は `{base_url}/a2a/{role}`。
    `nid`(交渉 ID)は A2A のメッセージの metadata に載せる。LLM には渡らない(§2.7)。
    `auth` は、この呼び出しの HTTP クライアントに付ける認証(サービス間の ID トークン。台帳 X-37)。
    config は、応答の usage の検証に使う設定(max_prompt_tokens・max_output_tokens・model。web.app.bind_agents_client が渡す)。
    返り値は `(payload, usage)`。応答の封筒の検証と、失敗の伝え方は、モジュールの docstring のとおり。
    """
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    url = base_url.rstrip("/") + endpoint_path(role)
    request = SendMessageRequest(
        message=Message(
            message_id=uuid.uuid4().hex,
            role=A2ARole.ROLE_USER,
            parts=[new_data_part(turn_input.model_dump(mode="json", by_alias=True), media_type=DATA_MEDIA_TYPE)],
            metadata={"nid": nid},
        )
    )
    responses: list[StreamResponse] = []
    try:
        # httpx の時間切れは段階ごと(接続・読み取りなど)なので、全体の上限を別に掛ける。
        async with asyncio.timeout(timeout_s):
            async with _open_http_client(timeout_s) as http:
                if auth is not None:
                    http.auth = auth
                client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(_card_for(url))
                async for event in client.send_message(request):
                    responses.append(event)
    except TimeoutError:
        raise TimeoutError(f"the agent did not answer within {timeout_s} seconds") from None
    except A2AClientTimeoutError as exc:
        raise TimeoutError(f"the agent did not answer within {timeout_s} seconds") from exc
    except (InvalidParamsError, InvalidRequestError) as exc:
        raise ValueError(f"the agent endpoint rejected the input: {exc}") from exc
    except A2AClientError as exc:  # 通信の失敗(接続できない・HTTP エラー・応答が JSON でない)
        raise ConnectionError(f"could not talk to the agent endpoint: {exc}") from exc
    except A2AError as exc:  # 受信口が返した、入力の拒否以外の失敗
        data = exc.data if isinstance(exc.data, dict) else {}
        if data.get(TRANSIENT_ERROR_KEY) == "true":
            raise ConnectionError(f"the agent endpoint returned a temporary error: {exc}") from exc
        if isinstance(exc, InternalError) and data.get(TRUNCATED_ERROR_KEY) == "true":
            # 出力が max_output_tokens で切れた(台帳 C-53)。一時的ではない。usage は、付いていて検証に通るときだけ載せる。
            raise TruncatedOutputError(
                "the agent output was cut off at max_output_tokens", usage=_usage_in_error(data, config)
            ) from exc
        # 一時的と印のない失敗(LLM の出力が JSON でない・ADK の検証エラーなど)は、temperature 0 では送り直しても同じ
        # 失敗を繰り返す。ConnectionError(再試行する)にせず、ValueError(再試行しない)にする(台帳 L9-5)。
        raise ValueError(f"the agent endpoint returned a non-transient failure: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ConnectionError(f"could not talk to the agent endpoint: {type(exc).__name__}") from exc
    except (ValueError, TypeError, KeyError, ParseError) as exc:
        # a2a-sdk のクライアントは、形の崩れた応答を、これらの例外で伝える。ValueError は「拒否」の意味に
        # 使うので、応答が A2A の形でないときはここで ConnectionError にする。
        raise ConnectionError(
            f"the agent endpoint returned a response that is not valid A2A: {type(exc).__name__}"
        ) from exc

    if not responses:
        raise ConnectionError("the agent endpoint returned no response")
    return _checked_response(responses, config)
