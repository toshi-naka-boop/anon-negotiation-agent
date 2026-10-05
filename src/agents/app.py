"""agents サービスの A2A サーバ(design.md §4.3)。

a2a-sdk のサーバで、受信口ごとに自前の AgentExecutor(agents.executor)を持つ。

| 受信口 | 受け付ける型 | 使う場面 |
|---|---|---|
| `/a2a/candidate` | TurnInput | 通常の交渉と攻撃モードの候補者側。壁 1 の生メッセージもここに届く |
| `/a2a/employer` | TurnInput | 通常の交渉の求人側 |
| `/a2a/attacker` | AttackerTurnInput | 攻撃モードの求人側だけ |

- `GET /health`(死活確認。AC-22)は 200 `{"status":"ok"}` を返す。LLM は動かさない。
- 各受信口は JSON-RPC の口で、その下の `/.well-known/agent-card.json` に Agent Card を公開する
  (A2A の標準の場所。例: `/a2a/candidate/.well-known/agent-card.json`)。
- 各受信口は、`TurnInput.phase` で、計画の LlmAgent(出力は Plan)か決定の LlmAgent(出力は Move)かを選ぶ。つまり
  LlmAgent は 側 × 呼び出しの種類 の 6 体で、指示文は側ごとに 1 つを共有する(§4.2)。応答の artifact の metadata に、その
  呼び出しの使用量 `usage` を載せる(§4.3・台帳 X-58)。
- モデルは、ADK の `Gemini` に、クライアント側の自動再試行を切る設定(attempts=1)を渡して作る。起動時(この
  `create_app`)に、設定の再試行の回数が 1 であることを検証し、違えば起動を止める(台帳 X-55)。
- 受信本文の上限は 32 KB(設定ファイル)。超えたら、LLM を動かさずに A2A のエラーを返す。
- agents はストレージを持たない。A2A のタスクは保存せず(受け取った入力を保持しない)、
  ADK のセッションも実行のたびに作って捨てる(§1.1・§4.2)。
- サービス間の認証は、Cloud Run の IAM(認証を必須にし、呼べるのは web のサービスアカウントだけ。§1.1)に任せ、
  アプリの中ではトークンを検証しない(台帳 X-37)。呼ぶ側(web)が、agents の URL を audience にした ID トークンを
  付ける(agents.client.send_turn の auth。web.service_auth)。

調査事項 R-1(ADK の `to_a2a` で置き換えられるか)の結論: 置き換えない。ADK 2.10 の `to_a2a` /
`A2aAgentExecutor` では、次の制御ができない(または、実験的な部品を組み替えて、ADK の内部の
経路を上書きすることになる)。

- 検証の違反が、A2A のエラーではなく、失敗したタスク(メッセージに例外の文字列つき)になる。
- ADK のセッションが、呼び出し側が決める `contextId` になり、手番をまたいで状態が残り得る。
- 受け取った DataPart が LLM の内部の形に変換され、Part の metadata で function_call などを
  差し込める。TextPart・ファイルの Part も LLM に渡る。
- LLM の出力が TextPart で返り(Move が DataPart にならない)、タスクは標準の保存先に残る。
- 1 つのアプリに 3 つの受信口を置きにくい(`to_a2a` は 1 エージェントにつき 1 アプリ)。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.agent_execution import RequestContext, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    Task,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_1_0, TransportProtocol
from google.adk.models.base_llm import BaseLlm
from google.adk.sessions import InMemorySessionService
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agents.config import DEFAULT_AGENTS_CONFIG, AgentsConfig
from agents.executor import NegotiationExecutor
from agents.llm_agents import build_gemini_model, build_llm_agent, build_runner, require_no_http_retry
from agents.validation import validate_request
from agents.wire import DATA_MEDIA_TYPE, PHASES, ROLES, Role, endpoint_path, struct_to_python
from negotiation_core.schema import AttackerTurnInput, TurnInput

_AGENT_VERSION = "0.1.0"

# 受信口ごとの、受け付ける型(§4.3 の表)。
_INPUT_MODELS: dict[Role, type[TurnInput]] = {
    "candidate": TurnInput,
    "employer": TurnInput,
    "attacker": AttackerTurnInput,
}

_CARD_DESCRIPTIONS: dict[Role, str] = {
    "candidate": (
        "Negotiation agent for the candidate side. Takes one turn-input/v1 DataPart, "
        "returns one plan/v1 DataPart (phase=plan) or one move/v1 DataPart (phase=decide)."
    ),
    "employer": (
        "Negotiation agent for the employer side. Takes one turn-input/v1 DataPart, "
        "returns one plan/v1 DataPart (phase=plan) or one move/v1 DataPart (phase=decide)."
    ),
    "attacker": (
        "Employer-side negotiation agent for attack mode. Takes one turn-input/v1 DataPart with a "
        "principal_instruction field, returns one plan/v1 DataPart (phase=plan) or one move/v1 DataPart "
        "(phase=decide)."
    ),
}


class _NullTaskStore(TaskStore):
    """何も保存しない TaskStore。

    agents はストレージを持たず、手番ごとに状態を持たない(§1.1・§4.2)。a2a-sdk の標準の
    InMemoryTaskStore は、終わったタスクと、受け取った入力(拒否したものを含む)を消さずに
    持ち続けるので使わない。過去のタスクは読み出せない(GetTask は「見つからない」を返す)。
    """

    async def save(self, task: Task, context: ServerCallContext) -> None:
        return None

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        return None

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        return ListTasksResponse()

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        return None


class _ValidatingRequestContextBuilder(SimpleRequestContextBuilder):
    """A2A のタスクを作る前に、受信メッセージを検証する。

    違反したメッセージは、タスクも executor も作られないうちに、A2A のエラーで返る(a2a-sdk は、
    executor が投げたエラーを、タスクの失敗として長い記録つきでログに残すので、拒否のたびにそれを
    出さないための前置きでもある)。executor も同じ検証をする(二重の防御。検証コードは 1 つ)。
    """

    def __init__(self, input_model: type[TurnInput]) -> None:
        super().__init__()
        self._input_model = input_model

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        if params is not None:
            validate_request(params.message, struct_to_python(params.metadata), self._input_model)
        return await super().build(context, params, task_id, context_id, task)


class BodySizeLimitMiddleware:
    """受信本文の大きさに上限を掛ける、純粋な ASGI ミドルウェア(§4.3 の 32 KB)。

    Content-Length が上限を超えていれば、本文を読まずに拒否する。Content-Length がない(チャンク送信)
    または偽っているときは、受け取ったバイト数を数え、超えた時点で拒否する。拒否は、本文を読む
    `receive` の中で HTTPException(413) を投げて行う。a2a-sdk の JSON-RPC の口は、これを
    「Payload too large」の A2A エラー(InvalidRequest)にして返す。LLM は動かない。
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = next(
            (int(value) for key, value in scope["headers"] if key == b"content-length" and value.isdigit()),
            None,
        )
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            if declared is not None and declared > self.max_bytes:
                raise HTTPException(status_code=413)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413)
            return message

        await self.app(scope, limited_receive, send)


def _build_agent_card(role: Role, config: AgentsConfig) -> AgentCard:
    """role の Agent Card。JSON-RPC の口(`/a2a/{role}`)を 1 つだけ公開する。"""
    return AgentCard(
        name=f"{role}-negotiation-agent",
        description=_CARD_DESCRIPTIONS[role],
        version=_AGENT_VERSION,
        supported_interfaces=[
            AgentInterface(
                url=config.public_base_url.rstrip("/") + endpoint_path(role),
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=[DATA_MEDIA_TYPE],
        default_output_modes=[DATA_MEDIA_TYPE],
        skills=[
            AgentSkill(
                id="negotiate-turn",
                name="Negotiate one turn",
                description=(
                    "Plan which packages to check, or decide one move (propose, accept, reject, ask_principal "
                    "or end), for the given turn input."
                ),
                tags=["negotiation"],
                input_modes=[DATA_MEDIA_TYPE],
                output_modes=[DATA_MEDIA_TYPE],
            )
        ],
    )


async def _healthz(request: Request) -> JSONResponse:
    """死活確認(AC-22)。認証なしで 200 {"status":"ok"}。LLM は動かさない。"""
    return JSONResponse({"status": "ok"})


def create_app(*, model: BaseLlm | None = None, config: AgentsConfig = DEFAULT_AGENTS_CONFIG) -> Starlette:
    """3 つの受信口(候補者側・求人側・攻撃モードの求人側)を持つ Starlette アプリを作る。

    LlmAgent は 側 × 呼び出しの種類(計画・決定)の 6 体で、受信口ごとに、phase で動かす Runner を選ぶ(§4.2)。
    model を渡すと、その BaseLlm(テスト用のスタブ)を 6 体とも使う。渡さなければ、設定のモデル名の ADK `Gemini` を、
    クライアント側の自動再試行を切って(attempts=1)作る。設定の再試行の回数が 1 でなければ、ここで ValueError を
    投げて起動を止める(台帳 X-55。スタブを渡したときも検証する)。LLM を呼ぶ前に接続することはない
    (モデルへの接続は、最初の実行のときに行われる)。
    """
    require_no_http_retry(config)
    llm = build_gemini_model(config) if model is None else model
    routes = [Route("/health", _healthz, methods=["GET"])]
    handlers = []
    runners = {}
    for role in ROLES:
        role_runners = {}
        for phase in PHASES:
            agent = build_llm_agent(role, phase, model=llm, config=config)
            role_runners[phase] = build_runner(agent, InMemorySessionService())
            runners[(role, phase)] = role_runners[phase]
        card = _build_agent_card(role, config)
        handler = DefaultRequestHandler(
            agent_executor=NegotiationExecutor(
                role=role,
                input_model=_INPUT_MODELS[role],
                runners=role_runners,
                model_name=config.model,
                llm_timeout_seconds=config.llm_timeout_seconds,
            ),
            task_store=_NullTaskStore(),
            agent_card=card,
            request_context_builder=_ValidatingRequestContextBuilder(_INPUT_MODELS[role]),
        )
        routes += create_jsonrpc_routes(handler, rpc_url=endpoint_path(role))
        routes += create_agent_card_routes(card, card_url=endpoint_path(role) + AGENT_CARD_WELL_KNOWN_PATH)
        handlers.append(handler)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        for handler in handlers:
            await handler.aclose()

    app = Starlette(
        routes=routes,
        middleware=[Middleware(BodySizeLimitMiddleware, max_bytes=config.max_request_body_bytes)],
        lifespan=lifespan,
    )
    # (側, 呼び出しの種類) -> ADK の Runner(セッションが残っていないことの確認、診断用のプラグインの登録などに使う)
    app.state.runners = runners
    return app
