"""交渉エージェントの A2A 受信口の AgentExecutor(design.md §4.2・§4.3)。

受信口ごとに 1 つ作る(候補者側・求人側・攻撃モードの求人側)。1 回の実行の流れ:

1. 受信メッセージを検証する(agents.validation)。違反したら A2A のエラー(InvalidParamsError)を
   返し、ここで終わる。ADK の Runner も LLM も動かない。
2. A2A のタスクごとに新しいセッションを作り、ADK の Runner を動かす。LLM に渡るのは
   「指示文＋TurnInput」だけで、ID(metadata の nid)は入らない。セッションは実行のあとで捨てる。
3. LLM の出力(JSON)を、Move として DataPart で返す。Move としての検証はレフェリー(web)の
   仕事なので、ここでは中身を検証しない(スキーマ違反の手も、そのままレフェリーに届く。§4.1)。

LLM の呼び出しで一時的なエラー(Vertex AI の 429・5xx、ネットワークの失敗、時間切れ)が起きたら、
一時的なエラーだと分かる A2A のエラー(InternalError に `transient` の印を付けたもの)を返す。
レフェリーはこれを再試行する(§4.1)。それ以外の失敗(LLM の出力が JSON でない・ADK の検証エラーなど)は、
一時的ではない失敗として、印のない InternalError にする。クライアント(agents.client.send_turn)は印のない失敗を
ValueError にし、レフェリーは再試行せずに schema_invalid として登録する(同じ入力を送り直しても、temperature 0 では
同じ失敗を繰り返すため。台帳 L9-5)。

ログと A2A のエラーには、入力・出力の値を書かない。例外の型と、場所だけを書く。
"""

import asyncio
import json
import logging
import uuid
from contextlib import aclosing
from typing import Any

import httpx
from a2a.helpers import new_data_artifact, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState
from a2a.utils.errors import A2AError, InternalError, UnsupportedOperationError
from google.adk.runners import Runner
from google.genai import errors as genai_errors
from google.genai import types

from agents.llm_agents import APP_NAME, USER_ID
from agents.output_schema import restore_numeric_axes
from agents.validation import validate_request
from agents.wire import DATA_MEDIA_TYPE, TRANSIENT_ERROR_KEY, Role
from negotiation_core.schema import TurnInput

logger = logging.getLogger(__name__)


def llm_input_text(turn_input: TurnInput) -> str:
    """検証済みの TurnInput を、LLM に渡す入力(JSON の文字列)にする。ID は入っていない。"""
    return json.dumps(turn_input.model_dump(mode="json", by_alias=True), ensure_ascii=False, separators=(",", ":"))


def _is_transient(exc: BaseException) -> bool:
    """LLM の呼び出しの一時的なエラーか(429・5xx、ネットワークの失敗、時間切れ。§4.1・R-3)。"""
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transient(inner) for inner in exc.exceptions)
    if isinstance(exc, genai_errors.APIError):
        code = exc.code
        return isinstance(code, int) and (code == 429 or 500 <= code <= 599)
    return isinstance(exc, TimeoutError | httpx.TimeoutException | httpx.TransportError)


class NegotiationExecutor(AgentExecutor):
    """1 つの受信口(role)の AgentExecutor。input_model は、その受信口が受け付ける型。"""

    def __init__(
        self,
        *,
        role: Role,
        input_model: type[TurnInput],
        runner: Runner,
        llm_timeout_seconds: float,
    ) -> None:
        self._role = role
        self._input_model = input_model
        self._runner = runner
        self._llm_timeout_seconds = llm_timeout_seconds

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """メッセージを検証し、通ったときだけ LLM を動かして、Move を DataPart で返す。"""
        turn_input = validate_request(context.message, context.metadata, self._input_model)
        try:
            move = await self._run_llm(llm_input_text(turn_input))
        except A2AError:
            raise
        except Exception as exc:
            transient = _is_transient(exc)
            logger.warning("role=%s LLM run failed: %s transient=%s", self._role, type(exc).__name__, transient)
            if transient:
                raise InternalError(
                    message="temporary failure while running the model; retry later",
                    data={TRANSIENT_ERROR_KEY: "true"},
                ) from None
            raise InternalError(message="agent execution failed") from None

        await event_queue.enqueue_event(
            new_task(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                artifacts=[new_data_artifact(name="move", data=move, media_type=DATA_MEDIA_TYPE)],
            )
        )
        logger.info("role=%s turn completed", self._role)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """このエージェントのタスクは 1 回の呼び出しで終わるので、取り消せない。"""
        raise UnsupportedOperationError(message="tasks of this agent finish within one call and cannot be canceled")

    async def _run_llm(self, llm_input: str) -> dict[str, Any]:
        """新しいセッションで Runner を動かし、LLM の出力(JSON のオブジェクト)を返す。"""
        session_service = self._runner.session_service
        # A2A のタスクごとに新しいセッション。状態を持たない(§4.2)。
        session_id = uuid.uuid4().hex
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        try:
            async with asyncio.timeout(self._llm_timeout_seconds):
                text = await self._final_text(session_id, llm_input)
        finally:
            await session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        move = json.loads(text)
        if not isinstance(move, dict):
            raise ValueError("the model output is not a JSON object")
        return restore_numeric_axes(move)  # 出力スキーマで STRING の enum にした数値軸を、整数に戻す(R-3)

    async def _final_text(self, session_id: str, llm_input: str) -> str:
        """Runner の最終応答のテキストを返す。なければ ValueError。"""
        message = types.Content(role="user", parts=[types.Part(text=llm_input)])
        final_text: str | None = None
        run = self._runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message)
        async with aclosing(run) as events:
            async for event in events:
                if event.is_final_response() and event.content and event.content.parts:
                    text = "".join(part.text for part in event.content.parts if part.text and not part.thought)
                    if text:
                        final_text = text
        if final_text is None:
            raise ValueError("the model returned no text")
        return final_text
