"""ADK の LlmAgent と Runner の組み立て(design.md §4.2)。

- LlmAgent は、側(候補者側・求人側・攻撃モードの求人側)× 呼び出しの種類(計画 plan・決定 decide)の 6 体。出力スキーマが
  違うので分ける(計画は Plan、決定は Move。どちらも `check` を含まない)。ツールは持たない。
- 指示文は側ごとに 1 つで、計画と決定で共有する(固定。agents.instructions)。LLM の文脈は「指示文＋TurnInput」だけにする。
- `generate_content_config`: temperature(0)、思考の量(`thinking_config.thinking_level`。計画と決定で別。調査 R-9)、
  `max_output_tokens`(1 呼び出しの出力＋思考の上限。台帳 X-50)。値はすべて設定ファイルから来る。
- モデルは ADK の `Gemini` に、クライアント側の自動再試行を切る設定(`HttpRetryOptions(attempts=1)`)を渡して作る
  (台帳 X-55)。再試行はレフェリーだけが行い、レフェリーの 1 計上が Vertex AI への要求 1 回に対応するようにする。
- 状態は持たない。セッションは A2A のタスクごとに新しく作って捨てる(agents.executor)。
"""

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types

from agents.config import AgentsConfig
from agents.instructions import load_instruction
from agents.output_schema import build_output_schema
from agents.wire import Role
from negotiation_core.schema import Phase

# ADK の Runner・セッションに渡す名前。LLM の入力には入らない。
APP_NAME = "agents"
USER_ID = "referee"


def _fixed_instruction(text: str):
    """固定の指示文を返す InstructionProvider。

    文字列で渡すと、ADK は `{変数名}` をセッションの状態で置き換えようとする。指示文に波括弧が
    入っても壊れないよう、関数で渡して置き換えを避ける。
    """

    def provider(_context: ReadonlyContext) -> str:
        return text

    return provider


def _pin_system_instruction(text: str):
    """LLM に渡す system_instruction を、指示文そのものに固定するコールバック。

    ADK は system_instruction の末尾に、エージェント名を含む定型の 1 文を足す。入力には依存しないが、
    「LLM の文脈は指示文＋TurnInput だけ」(§4.2)を文字どおり保つため、モデルを呼ぶ直前に、
    指示文だけに戻す。
    """

    def callback(_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse | None:
        llm_request.config.system_instruction = text
        return None

    return callback


def require_no_http_retry(config: AgentsConfig) -> None:
    """クライアント側の自動再試行の回数が 1(再試行なし)でなければ、ValueError を投げる(起動を止める。台帳 X-55)。

    再試行はレフェリーだけが行う(§4.1)。google-genai の再試行が残ると、1 回の計上(レフェリーの送信 1 回)が Vertex AI への
    複数の要求になり、費用の上限(§8.2)を保証できなくなる。
    """
    if config.http_retry_attempts != 1:
        raise ValueError(
            "[agents] http_retry_attempts must be 1 (no client-side retry; design.md §4.2, ledger X-55), "
            f"got {config.http_retry_attempts!r}"
        )


def build_gemini_model(config: AgentsConfig) -> Gemini:
    """設定のモデル名の ADK `Gemini` を、クライアント側の自動再試行を切って作る(`HttpRetryOptions(attempts=1)`)。

    Vertex AI のプロジェクトと場所は、設定にもコードにも書かず、標準の環境変数で渡す。ここでは接続せず、最初の実行のときに
    ADK がクライアントを作る。設定の再試行の回数が 1 でなければ ValueError(require_no_http_retry)。
    """
    require_no_http_retry(config)
    return Gemini(model=config.model, retry_options=types.HttpRetryOptions(attempts=config.http_retry_attempts))


def thinking_level_for(phase: Phase, config: AgentsConfig) -> types.ThinkingLevel:
    """phase(計画・決定)の思考の量を、設定の名前(MINIMAL / LOW / MEDIUM / HIGH)から ThinkingLevel にして返す。"""
    name = config.plan_thinking_level if phase == "plan" else config.decide_thinking_level
    level = types.ThinkingLevel.__members__.get(name)
    if level is None or level is types.ThinkingLevel.THINKING_LEVEL_UNSPECIFIED:
        raise ValueError(f"unknown thinking level for phase={phase}: {name!r} (use MINIMAL, LOW, MEDIUM or HIGH)")
    return level


def build_llm_agent(role: Role, phase: Phase, *, model: str | BaseLlm, config: AgentsConfig) -> LlmAgent:
    """(role, phase)の LlmAgent を作る。model は、モデル名か、差し込む BaseLlm(Gemini、またはテスト用のスタブ)。

    出力スキーマは phase で決まる(plan は Plan、decide は Move)。指示文は role ごとに 1 つで、2 つの phase で共有する。
    """
    instruction = load_instruction(role)
    return LlmAgent(
        name=f"{role}_{phase}_agent",
        model=model,
        instruction=_fixed_instruction(instruction),
        output_schema=build_output_schema(phase),
        generate_content_config=types.GenerateContentConfig(
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level_for(phase, config)),
        ),
        before_model_callback=_pin_system_instruction(instruction),
    )


def build_runner(agent: LlmAgent, session_service: BaseSessionService) -> Runner:
    """agent を動かす Runner を作る。"""
    return Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
