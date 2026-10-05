"""交渉エージェントの A2A 受信口の AgentExecutor(design.md §4.2・§4.3)。

受信口ごとに 1 つ作る(候補者側・求人側・攻撃モードの求人側)。1 回の実行の流れ:

1. 受信メッセージを検証する(agents.validation)。違反したら A2A のエラー(InvalidParamsError)を
   返し、ここで終わる。ADK の Runner も LLM も動かない。
2. `TurnInput.phase` で、計画の Runner(出力は Plan)か、決定の Runner(出力は Move)かを選ぶ(§4.2)。A2A のタスクごとに
   新しいセッションを作って Runner を動かす。LLM に渡るのは「指示文＋TurnInput」だけで、ID(metadata の nid)は入らない。
   セッションは実行のあとで捨てる。
3. LLM の出力(JSON)を、DataPart で返す(計画は Plan、決定は Move)。Plan・Move としての検証はレフェリー(web)の仕事なので、
   ここでは中身を検証しない(スキーマ違反の出力も、そのままレフェリーに届く。§4.1)。応答の A2A タスクの artifact の
   metadata に、その呼び出しの使用量 `usage`(negotiation_core.schema.Usage。ADK の最終応答の usage_metadata から作る。
   取れない項目は 0。requests は、その実行で LLM を呼んだ回数)を載せる(台帳 X-58)。
4. 出力が `max_output_tokens` で切れた(finish_reason が MAX_TOKENS)ときは、結果を返さず、一時的ではない A2A のエラー
   (InternalError に `truncated` の印と、その呼び出しの usage を付けたもの)を返す(台帳 C-53)。レフェリーは
   `last_error=output_truncated` の無効手として登録する。

LLM の呼び出しで一時的なエラー(Vertex AI の 429・5xx、ネットワークの失敗、時間切れ)が起きたら、
一時的なエラーだと分かる A2A のエラー(InternalError に `transient` の印を付けたもの)を返す。
レフェリーはこれを再試行する(§4.1)。それ以外の失敗(LLM の出力が JSON でない・ADK の検証エラーなど)は、
一時的ではない失敗として、印のない InternalError にする。クライアント(agents.client.send_turn)は印のない失敗を
ValueError にし、レフェリーは再試行せずに schema_invalid として登録する(同じ入力を送り直しても、temperature 0 では
同じ失敗を繰り返すため。台帳 L9-5)。

ログと A2A のエラーには、入力・出力の値を書かない。例外の型と、場所だけを書く(usage は数とモデル名だけで、値を含まない)。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.helpers import new_data_artifact, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState
from a2a.utils.errors import A2AError, InternalError, UnsupportedOperationError
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.genai import errors as genai_errors
from google.genai import types

from agents.llm_agents import APP_NAME, USER_ID
from agents.output_schema import restore_numeric_axes
from agents.validation import validate_request
from agents.wire import DATA_MEDIA_TYPE, TRANSIENT_ERROR_KEY, TRUNCATED_ERROR_KEY, USAGE_KEY, Role
from negotiation_core.schema import Phase, TurnInput, Usage

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


@dataclass
class _RunMeter:
    """1 回の実行で ADK が返した、モデルの応答(イベント)から、使用量と、出力が切れたかどうかを集める。

    ツールのない LlmAgent は、LLM の応答 1 つにつきイベントを 1 つ出すので、イベントの数が LLM を呼んだ回数になる。
    トークン数は応答の usage_metadata から取り、取れない項目は 0 にする(prompt_token_count はキャッシュ済みを含む。
    candidates_token_count は思考を含まず、thoughts_token_count が別にある。調査 R-9)。
    """

    requests: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    thoughts_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False

    def add(self, event: Event) -> None:
        """モデルの応答のイベント 1 つを数える。"""
        self.requests += 1
        usage = event.usage_metadata
        if usage is not None:
            self.prompt_tokens += usage.prompt_token_count or 0
            self.cached_tokens += usage.cached_content_token_count or 0
            self.thoughts_tokens += usage.thoughts_token_count or 0
            self.output_tokens += usage.candidates_token_count or 0
        if event.finish_reason == types.FinishReason.MAX_TOKENS:
            self.truncated = True

    def usage(self, model: str) -> Usage:
        """集めた使用量を Usage にする(model は設定のモデル名)。"""
        return Usage(
            model=model,
            prompt_tokens=self.prompt_tokens,
            cached_tokens=self.cached_tokens,
            thoughts_tokens=self.thoughts_tokens,
            output_tokens=self.output_tokens,
            requests=max(self.requests, 1),
        )


class NegotiationExecutor(AgentExecutor):
    """1 つの受信口(role)の AgentExecutor。input_model は、その受信口が受け付ける型。

    runners は、呼び出しの種類(phase)ごとの Runner(plan は Plan を、decide は Move を出す)。model_name は、
    応答の usage に載せるモデル名(設定のモデル名)。
    """

    def __init__(
        self,
        *,
        role: Role,
        input_model: type[TurnInput],
        runners: Mapping[Phase, Runner],
        model_name: str,
        llm_timeout_seconds: float,
    ) -> None:
        self._role = role
        self._input_model = input_model
        self._runners = runners
        self._model_name = model_name
        self._llm_timeout_seconds = llm_timeout_seconds

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """メッセージを検証し、通ったときだけ、phase の Runner で LLM を動かして、Plan・Move を DataPart で返す。"""
        turn_input = validate_request(context.message, context.metadata, self._input_model)
        phase = turn_input.phase
        try:
            output, usage = await self._run_llm(self._runners[phase], phase, llm_input_text(turn_input))
        except A2AError:
            raise
        except Exception as exc:
            transient = _is_transient(exc)
            logger.warning(
                "role=%s phase=%s LLM run failed: %s transient=%s", self._role, phase, type(exc).__name__, transient
            )
            if transient:
                raise InternalError(
                    message="temporary failure while running the model; retry later",
                    data={TRANSIENT_ERROR_KEY: "true"},
                ) from None
            raise InternalError(message="agent execution failed") from None

        artifact = new_data_artifact(
            name="plan" if phase == "plan" else "move", data=output, media_type=DATA_MEDIA_TYPE
        )
        artifact.metadata.update({USAGE_KEY: usage.model_dump(mode="json")})
        await event_queue.enqueue_event(
            new_task(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                artifacts=[artifact],
            )
        )
        logger.info("role=%s phase=%s turn completed", self._role, phase)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """このエージェントのタスクは 1 回の呼び出しで終わるので、取り消せない。"""
        raise UnsupportedOperationError(message="tasks of this agent finish within one call and cannot be canceled")

    async def _run_llm(self, runner: Runner, phase: Phase, llm_input: str) -> tuple[dict[str, Any], Usage]:
        """新しいセッションで runner を動かし、LLM の出力(JSON のオブジェクト)と、その呼び出しの使用量を返す。

        出力が切れていたら(finish_reason が MAX_TOKENS)、一時的ではない InternalError(`truncated` の印と usage つき。
        台帳 C-53)を投げる。
        """
        session_service = runner.session_service
        # A2A のタスクごとに新しいセッション。状態を持たない(§4.2)。
        session_id = uuid.uuid4().hex
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        meter = _RunMeter()
        try:
            async with asyncio.timeout(self._llm_timeout_seconds):
                text = await self._final_text(runner, session_id, llm_input, meter)
        finally:
            await session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)

        usage = meter.usage(self._model_name)
        if meter.truncated:
            logger.warning("role=%s phase=%s the model output was cut off at max_output_tokens", self._role, phase)
            raise InternalError(
                message="the model output was cut off at max_output_tokens",
                data={TRUNCATED_ERROR_KEY: "true", USAGE_KEY: usage.model_dump(mode="json")},
            )
        if text is None:
            raise ValueError("the model returned no text")
        output = json.loads(text)
        if not isinstance(output, dict):
            raise ValueError("the model output is not a JSON object")
        return restore_numeric_axes(output), usage  # 出力スキーマで STRING の enum にした数値軸を、整数に戻す(R-3)

    async def _final_text(self, runner: Runner, session_id: str, llm_input: str, meter: _RunMeter) -> str | None:
        """runner の最終応答のテキストを返す(なければ None)。モデルの応答は、meter に数える。"""
        message = types.Content(role="user", parts=[types.Part(text=llm_input)])
        final_text: str | None = None
        run = runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message)
        async with aclosing(run) as events:
            async for event in events:
                if not event.partial:
                    meter.add(event)
                if event.is_final_response() and event.content and event.content.parts:
                    text = "".join(part.text for part in event.content.parts if part.text and not part.thought)
                    if text:
                        final_text = text
        return final_text
