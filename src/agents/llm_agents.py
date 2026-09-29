"""ADK の LlmAgent と Runner の組み立て(design.md §4.2)。

- LlmAgent は 3 体(候補者側・求人側・攻撃モードの求人側)。ツールは持たず、temperature は 0。
- `output_schema` は、グリッド値を列挙値にした Move のスキーマ(agents.output_schema)。
- 指示文は固定(agents.instructions)。LLM の文脈は「指示文＋TurnInput」だけにする。
- 状態は持たない。セッションは A2A のタスクごとに新しく作って捨てる(agents.executor)。
"""

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types

from agents.instructions import load_instruction
from agents.output_schema import build_move_output_schema
from agents.wire import Role

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


def build_llm_agent(role: Role, *, model: str | BaseLlm, temperature: float) -> LlmAgent:
    """role の LlmAgent を作る。model は、モデル名(設定ファイル)か、差し込む BaseLlm(テスト用のスタブ)。"""
    instruction = load_instruction(role)
    return LlmAgent(
        name=f"{role}_agent",
        model=model,
        instruction=_fixed_instruction(instruction),
        output_schema=build_move_output_schema(),
        generate_content_config=types.GenerateContentConfig(temperature=temperature),
        before_model_callback=_pin_system_instruction(instruction),
    )


def build_runner(agent: LlmAgent, session_service: BaseSessionService) -> Runner:
    """agent を動かす Runner を作る。"""
    return Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
