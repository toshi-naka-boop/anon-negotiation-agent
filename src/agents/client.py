"""web(レフェリー)から交渉エージェントの受信口を呼ぶクライアント(design.md §4.1・§4.3)。

a2a-sdk のクライアントで、受信口へ TurnInput(または AttackerTurnInput)を DataPart で 1 回だけ送り、
返ってきた DataPart の `data`(dict)をそのまま返す。

- `Move` としての検証はしない。再試行もしない。どちらもレフェリーの仕事(§4.1・§4.3)。
- Agent Card は取りに行かない(受信口の場所は role から決まる)。Card は探索用に公開してある。
- 失敗は、組み込みの例外だけで伝える。

| 起きたこと | 例外 |
|---|---|
| 時間切れ(timeout_s。全体の壁時計) | `TimeoutError` |
| A2A の通信エラー(接続できない・HTTP エラー・応答の形が A2A でない) | `ConnectionError` |
| 受信口が返した一時的なエラー(LLM の 429・5xx・時間切れ) | `ConnectionError` |
| 受信口が返したそのほかの失敗(実行の失敗) | `ConnectionError`(レフェリーが再試行し、駄目なら agent_timeout にする) |
| 受信口が入力を拒否した(壁 1。スキーマ違反・大きすぎる本文など) | `ValueError` |
"""

import asyncio
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
from a2a.utils.errors import A2AError, InvalidParamsError, InvalidRequestError
from google.protobuf.json_format import ParseError

from agents.wire import DATA_MEDIA_TYPE, ROLES, TRANSIENT_ERROR_KEY, Role, endpoint_path, part_kind, value_to_python
from negotiation_core.schema import AttackerTurnInput, TurnInput


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


def _move_data(response: StreamResponse) -> dict:
    """応答から、Move の DataPart の `data` を取り出す。A2A の形でなければ ConnectionError。"""
    if response.HasField("task"):
        task = response.task
        if task.status.state != TaskState.TASK_STATE_COMPLETED:
            raise ConnectionError(f"the agent task ended in state {TaskState.Name(task.status.state)}")
        parts = [part for artifact in task.artifacts for part in artifact.parts]
    elif response.HasField("message"):
        parts = list(response.message.parts)
    else:
        raise ConnectionError("the agent response has neither a task nor a message")
    if len(parts) != 1 or part_kind(parts[0]) != "data":
        raise ConnectionError("the agent response must contain exactly one DataPart")
    data = value_to_python(parts[0].data)
    if not isinstance(data, dict):
        raise ConnectionError("the agent response DataPart is not a JSON object")
    return data


async def send_turn(
    base_url: str,
    role: Role,
    turn_input: TurnInput | AttackerTurnInput,
    *,
    nid: str,
    timeout_s: float,
) -> dict:
    """role の受信口へ turn_input を 1 回だけ送り、返ってきた DataPart の `data`(dict)を返す。

    base_url は agents サービスの URL(例: `https://agents.example`)。送り先は `{base_url}/a2a/{role}`。
    `nid`(交渉 ID)は A2A のメッセージの metadata に載せる。LLM には渡らない(§2.7)。
    失敗の伝え方はモジュールの docstring の表のとおり。
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
    response: StreamResponse | None = None
    try:
        # httpx の時間切れは段階ごと(接続・読み取りなど)なので、全体の上限を別に掛ける。
        async with asyncio.timeout(timeout_s):
            async with _open_http_client(timeout_s) as http:
                client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(_card_for(url))
                async for event in client.send_message(request):
                    response = event
    except TimeoutError:
        raise TimeoutError(f"the agent did not answer within {timeout_s} seconds") from None
    except A2AClientTimeoutError as exc:
        raise TimeoutError(f"the agent did not answer within {timeout_s} seconds") from exc
    except (InvalidParamsError, InvalidRequestError) as exc:
        raise ValueError(f"the agent endpoint rejected the input: {exc}") from exc
    except A2AClientError as exc:  # 通信の失敗(接続できない・HTTP エラー・応答が JSON でない)
        raise ConnectionError(f"could not talk to the agent endpoint: {exc}") from exc
    except A2AError as exc:  # 受信口が返した、入力の拒否以外の失敗
        transient = isinstance(exc.data, dict) and exc.data.get(TRANSIENT_ERROR_KEY) == "true"
        kind = "a temporary error" if transient else "an error"
        raise ConnectionError(f"the agent endpoint returned {kind}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ConnectionError(f"could not talk to the agent endpoint: {type(exc).__name__}") from exc
    except (ValueError, TypeError, KeyError, ParseError) as exc:
        # a2a-sdk のクライアントは、形の崩れた応答を、これらの例外で伝える。ValueError は「拒否」の意味に
        # 使うので、応答が A2A の形でないときはここで ConnectionError にする。
        raise ConnectionError(
            f"the agent endpoint returned a response that is not valid A2A: {type(exc).__name__}"
        ) from exc

    if response is None:
        raise ConnectionError("the agent endpoint returned no response")
    return _move_data(response)
