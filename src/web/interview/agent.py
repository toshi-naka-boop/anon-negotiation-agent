"""面談エージェント(design.md §5 の 2・4・5、§4.2、§8.2)。ADK の LlmAgent 2 体。

- salary: 年収の正規化の 3 問の回答から、SalaryBasis(額面か手取りか・固定残業代・賞与の月数など)を取り出す。
- constraints: 自由コメント・辞めた理由を、発言単位の ConstraintList に構造化する。
アンカーへの変換は、決定的なコード(web.interview.statements・negotiation_core)が行う。LLM は条件の数値を決めない。

LLM の呼び出し(交渉エージェントと同じ土台。agents.llm_agents.build_gemini_model)
- JSON モード(response_mime_type=application/json)だけで出させ、応答スキーマは渡さない(台帳 I-19: 応答スキーマの制約付きデコードは
  11〜55 秒かかった)。出力は pydantic の型(SalaryBasis・ConstraintList)で検証する(知らない項目は無視し、知っている項目の型・範囲・
  列挙が違えば使わない。面談には、直させる手がかりを返す往復がない)。実機で 1 回測る約束(§5)は、この実装では未実施。
- 呼び出しごとに、新しい InMemorySessionService のセッションで動かし、終わったら捨てる。会話を積まない(台帳 L14-3: 構造化の抽出に
  履歴は要らず、1 回の入力を本文の上限内に保つ)。LLM に渡るのは「固定の指示文＋その 1 回の入力」だけ(ID は入らない)。
- max_output_tokens(暫定 2,048。[web.interview])を置く(台帳 C-49)。出力が切れたら output_truncated。
- クライアント側の自動再試行は切る(require_no_http_retry。台帳 X-55)。再試行はこのクラスだけが、レフェリーと同じ規則(§4.1)で行う:
  Vertex AI の一時的なエラー(429・5xx・通信・時間切れ)を、待ち時間を空けて最大 agent_max_retries 回、全体で agent_call_timeout_seconds の中で。
- 送る前に、物理の呼び出し数を 1 日の枠で数える(web.llm_budget.reserve(None)。再試行も 1 回と数え、429・5xx でも戻さない。台帳 X-56)。
  1 日の上限に達していれば、送らずに断る(daily_limit_reached)。カウンタに書けないときは LlmBudgetUnavailable(送らない)。
  あわせて、依頼者ごとの窓(プロセスのメモリ上)で、短い間の連打を断る(rate_limited。§8.2 の面談の枠を、IP が決まるまで暫定で依頼者ごとに)。
- ログには、種類と例外の型名だけを書く(入力・出力・依頼者 ID は書かない。§3.8・§7)。トレースのメッセージ内容のキャプチャは、環境変数
  ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false で切る(本番の必須設定。§10)。ADK の既定は「載せる」なので、このモジュールを読み込むと
  き、未設定なら false にする(明示の設定は尊重する)。このクラス自身はスパンを作らない(ADK が作るスパンの中身を、これで絞る)。
"""

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, Literal

import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from agents.config import DEFAULT_AGENTS_CONFIG, AgentsConfig
from agents.llm_agents import build_gemini_model, require_no_http_retry
from vault.clock import Clock

from web.config import RefereeConfig
from web.interview.config import InterviewConfig
from web.interview.salary import SalaryBasis
from web.interview.statements import ConstraintList, coerce_numeric_fields, known_fields_only, parse_constraint_list
from web.llm_budget import LlmBudget

_log = logging.getLogger(__name__)

# トレースのスパンに、面談の中身(利用者の文章)を載せない(§5・§7)。ADK は、既定でスパン(call_llm の gcp.vertex.agent.llm_request など)に
# LLM の入力の全文を載せる。この環境変数で切る(本番の必須設定でもある。§10)。運営者が明示に設定していれば、その値を尊重する(setdefault)。
os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")

# ADK の Runner・セッションに渡す名前。LLM の入力には入らない(ADK の app 名は、エージェントの置き場の最上位のパッケージ名と合わせる)。
APP_NAME = "web"
USER_ID = "interview"
_INSTRUCTIONS_DIRECTORY = Path(__file__).resolve().parent / "instructions"

AgentKind = Literal["salary", "constraints"]
ExtractionKind = Literal["free_comment", "reason_for_leaving"]
Sleep = Callable[[float], Awaitable[None]]

# 失敗の理由(InterviewLlmFailure.code。API の detail に使う)
DAILY_LIMIT_REACHED = "daily_limit_reached"  # 1 日の物理の呼び出し数の上限に達している(送っていない)
RATE_LIMITED = "rate_limited"  # 依頼者ごとの窓の上限に達している(送っていない)
LLM_UNAVAILABLE = "llm_unavailable"  # 一時的なエラーが続いた・時間切れ(再試行を使い切った)
LLM_FAILED = "llm_failed"  # 一時的でない失敗(権限・要求の誤り・ADK の失敗など)
OUTPUT_TRUNCATED = "output_truncated"  # 出力が max_output_tokens で切れた
OUTPUT_INVALID = "output_invalid"  # 出力が JSON でない・型の検証に通らない

_SALARY_NUMBER_KEYS = ("amount_man_yen", "bonus_months", "fixed_overtime_man_yen_per_month")


class InterviewLlmFailure(Exception):
    """面談の LLM 呼び出しが使えなかった(または、使えない出力だった)。code が理由。メッセージに入力・出力の値は入れない。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _OutputTruncated(Exception):
    """出力が max_output_tokens で切れた(finish_reason が MAX_TOKENS)。"""


def _is_transient(exc: BaseException) -> bool:
    """LLM の呼び出しの一時的なエラーか(429・5xx、ネットワークの失敗、時間切れ。§4.1)。agents.executor と同じ判定。"""
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transient(inner) for inner in exc.exceptions)
    if isinstance(exc, genai_errors.APIError):
        code = exc.code
        return isinstance(code, int) and (code == 429 or 500 <= code <= 599)
    return isinstance(exc, TimeoutError | httpx.TimeoutException | httpx.TransportError)


def _fixed_instruction(text: str):
    """固定の指示文を返す InstructionProvider(文字列で渡すと、ADK が `{変数名}` を置き換えようとして、JSON の例が壊れる)。"""

    def provider(_context: ReadonlyContext) -> str:
        return text

    return provider


def _pin_system_instruction(text: str):
    """LLM に渡る system_instruction を、指示文そのものに固定する(ADK が末尾に足すエージェント名の 1 文を外す)。"""

    def callback(_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse | None:
        llm_request.config.system_instruction = text
        return None

    return callback


def _thinking_level(name: str) -> types.ThinkingLevel:
    level = types.ThinkingLevel.__members__.get(name)
    if level is None or level is types.ThinkingLevel.THINKING_LEVEL_UNSPECIFIED:
        raise ValueError(f"unknown thinking level: {name!r} (use MINIMAL, LOW, MEDIUM or HIGH)")
    return level


def load_instruction(kind: AgentKind) -> str:
    """kind の指示文(src/web/interview/instructions/{kind}.md。前後の空白を除いた全文)。"""
    return (_INSTRUCTIONS_DIRECTORY / f"{kind}.md").read_text(encoding="utf-8").strip()


def _load_json_object(text: str) -> dict[str, Any]:
    """LLM の出力(JSON のオブジェクト)を読む。JSON でない・オブジェクトでないときは OUTPUT_INVALID。"""
    try:
        payload = json.loads(text)
    except ValueError:
        raise InterviewLlmFailure(OUTPUT_INVALID) from None
    if not isinstance(payload, dict):
        raise InterviewLlmFailure(OUTPUT_INVALID)
    return payload


class InterviewAgent:
    """面談の LLM 呼び出し(年収の読み取りと、発言の構造化)。"""

    def __init__(
        self,
        *,
        budget: LlmBudget,
        clock: Clock,
        sleep: Sleep,
        retry: RefereeConfig,
        config: InterviewConfig,
        agents_config: AgentsConfig = DEFAULT_AGENTS_CONFIG,
        model: BaseLlm | None = None,
    ) -> None:
        require_no_http_retry(agents_config)  # 起動時の検証(台帳 X-55)。モデルへの接続は、最初の実行のときに行われる
        self._budget = budget
        self._clock = clock
        self._sleep = sleep
        self._retry = retry
        self._config = config
        self._agents_config = agents_config
        self._model = model
        self._runners: dict[AgentKind, Runner] = {}
        self._windows: dict[str, deque] = {}

    def use_model(self, model: BaseLlm) -> None:
        """使うモデルを差し替える(テストが、スタブの LLM を差し込む口)。作ってある Runner は捨てる。"""
        self._model = model
        self._runners.clear()

    async def remaining_sessions(self) -> list:
        """ADK のセッションで、残っているもの(呼び出しのあとは空のはず。会話を積まないことの確認用)。"""
        found: list = []
        for runner in self._runners.values():
            found += (await runner.session_service.list_sessions(app_name=APP_NAME)).sessions
        return found

    # ------------------------------------------------------------------
    # 公開: 取り出し
    # ------------------------------------------------------------------

    async def extract_salary_basis(self, owner: str, answers: Sequence[tuple[str, str]]) -> SalaryBasis:
        """年収の正規化の 3 問の (質問, 回答) から、SalaryBasis を取り出す。owner は、依頼者ごとの窓の鍵(LLM には渡さない)。"""
        llm_input = json.dumps(
            {"task": "salary_basis", "qa": [{"question": q, "answer": a} for q, a in answers]}, ensure_ascii=False
        )
        payload = _load_json_object(await self._call(owner, "salary", llm_input))
        try:  # 知らない項目は無視し、知っている項目の型・範囲・列挙が違えば、出力を使わない
            return SalaryBasis.model_validate(
                coerce_numeric_fields(known_fields_only(payload, SalaryBasis.model_fields), _SALARY_NUMBER_KEYS)
            )
        except ValidationError:
            raise InterviewLlmFailure(OUTPUT_INVALID) from None

    async def extract_constraints(self, owner: str, kind: ExtractionKind, text: str) -> tuple[ConstraintList, int]:
        """自由コメント・辞めた理由の文章から、発言を取り出す。返り値は (発言の並び, 検証に通らず捨てた発言の数)。"""
        llm_input = json.dumps({"task": kind, "text": text}, ensure_ascii=False)
        payload = _load_json_object(await self._call(owner, "constraints", llm_input))
        try:
            return parse_constraint_list(payload, max_statements=self._config.max_statements_per_extraction)
        except ValueError:
            raise InterviewLlmFailure(OUTPUT_INVALID) from None

    # ------------------------------------------------------------------
    # 呼び出し(計上・再試行・新しいセッション)
    # ------------------------------------------------------------------

    def _runner(self, kind: AgentKind) -> Runner:
        runner = self._runners.get(kind)
        if runner is None:
            if self._model is None:
                self._model = build_gemini_model(self._agents_config)  # 自動再試行を切ったモデル(接続は最初の実行のとき)
            instruction = load_instruction(kind)
            agent = LlmAgent(
                name=f"interview_{kind}_agent",
                model=self._model,
                instruction=_fixed_instruction(instruction),
                generate_content_config=types.GenerateContentConfig(
                    temperature=self._agents_config.temperature,
                    max_output_tokens=self._config.max_output_tokens,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level=_thinking_level(self._config.thinking_level)),
                ),
                before_model_callback=_pin_system_instruction(instruction),
            )
            runner = self._runners[kind] = Runner(
                app_name=APP_NAME, agent=agent, session_service=InMemorySessionService()
            )
        return runner

    async def _admit(self, owner: str) -> None:
        """LLM に 1 回送る前の計上: 依頼者ごとの窓、1 日の物理の数(Firestore のトランザクション)。断るなら InterviewLlmFailure。"""
        now = self._clock.now()
        window = self._windows.setdefault(owner, deque())
        while window and (now - window[0]).total_seconds() >= self._config.llm_window_seconds:
            window.popleft()
        if len(window) >= self._config.llm_calls_per_window:
            raise InterviewLlmFailure(RATE_LIMITED)
        reservation = await self._budget.reserve(None)  # 書けないときは LlmBudgetUnavailable(送らない)
        if not reservation.granted:
            raise InterviewLlmFailure(DAILY_LIMIT_REACHED)
        window.append(now)
        if len(self._windows) > 2 * self._config.max_active_interviews:  # 窓の外に出た依頼者を掃除する(メモリの上限)
            span = self._config.llm_window_seconds
            self._windows = {key: dq for key, dq in self._windows.items() if dq and (now - dq[-1]).total_seconds() < span}

    async def _call(self, owner: str, kind: AgentKind, llm_input: str) -> str:
        """kind のエージェントを 1 回呼ぶ(物理の送信は、再試行を含めて、1 回ごとに計上する)。LLM の出力の文字列を返す。

        上限(agent_call_timeout_seconds)は、再試行の待ち時間を含む(§4.1)。一時的なエラーは、待ち時間を空けて最大
        agent_max_retries 回まで再試行する。
        """
        started = self._clock.now()

        def elapsed() -> float:
            return (self._clock.now() - started).total_seconds()

        retries = 0
        while True:
            remaining = self._retry.agent_call_timeout_seconds - elapsed()
            if remaining <= 0:
                raise InterviewLlmFailure(LLM_UNAVAILABLE)
            await self._admit(owner)
            try:
                return await asyncio.wait_for(self._run_once(kind, llm_input), timeout=remaining)
            except _OutputTruncated:
                _log.warning("interview llm output was cut off kind=%s", kind)
                raise InterviewLlmFailure(OUTPUT_TRUNCATED) from None
            except InterviewLlmFailure:
                raise
            except Exception as exc:
                transient = _is_transient(exc)
                _log.warning("interview llm call failed kind=%s error=%s transient=%s", kind, type(exc).__name__, transient)
                if not transient:
                    raise InterviewLlmFailure(LLM_FAILED) from None
                backoff = self._retry.agent_retry_backoff_seconds
                delay = backoff[min(retries, len(backoff) - 1)]
                if retries >= self._retry.agent_max_retries or elapsed() + delay >= self._retry.agent_call_timeout_seconds:
                    raise InterviewLlmFailure(LLM_UNAVAILABLE) from None
                retries += 1
                await self._sleep(delay)

    async def _run_once(self, kind: AgentKind, llm_input: str) -> str:
        """新しいセッションで Runner を 1 回動かし、最終応答のテキストを返す。セッションは終わったら捨てる(会話を積まない)。"""
        runner = self._runner(kind)
        service = runner.session_service
        session_id = uuid.uuid4().hex
        await service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        truncated = False
        final_text: str | None = None
        try:
            message = types.Content(role="user", parts=[types.Part(text=llm_input)])
            run = runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message)
            async with aclosing(run) as events:
                async for event in events:
                    if not event.partial and event.finish_reason == types.FinishReason.MAX_TOKENS:
                        truncated = True
                    if event.is_final_response() and event.content and event.content.parts:
                        text = "".join(part.text for part in event.content.parts if part.text and not part.thought)
                        if text:
                            final_text = text
        finally:
            await service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        if truncated:
            raise _OutputTruncated
        if final_text is None:
            raise InterviewLlmFailure(OUTPUT_INVALID)  # 応答にテキストがない
        return final_text
