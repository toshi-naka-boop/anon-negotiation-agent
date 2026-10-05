"""agents が使う暫定値の読み込み(config/params.toml の [agents]。design.md §4.2・§4.3)。

モデル名・本文の上限などは未決事項(U-03、調査事項 R-3)の暫定値で、決まったら設定ファイル側を
差し替えるだけで済むようにする。思考の量(計画・決定)・1 呼び出しの出力の上限・クライアント側の再試行の回数も、
ここから読む(v14。調査 R-9・台帳 X-50・X-55)。応答の usage の prompt_tokens の上限(max_prompt_tokens)も、ここから読む(§10)。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/agents/config.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"


# max_output_tokens に許す範囲(台帳 X-63)。上限は、設計書 §8.2 の 1 日の最悪の金額の見積もり(2,048)の 2 倍まで
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS_CEILING = 4096


@dataclass(frozen=True)
class AgentsConfig:
    """agents の設定。"""

    model: str
    temperature: float
    # 思考の量(thinking_level の名前。MINIMAL / LOW / MEDIUM / HIGH)。計画と決定で別(§4.2・R-9)
    plan_thinking_level: str
    decide_thinking_level: str
    # 1 回の呼び出しの出力＋思考の上限(§4.2・台帳 X-50)。切れた出力は output_truncated になる(台帳 C-53)
    max_output_tokens: int
    # google-genai のクライアント側の自動再試行の回数。1 = 再試行なし(§4.2・台帳 X-55。起動時に 1 であることを検証する)
    http_retry_attempts: int
    max_request_body_bytes: int
    # 応答の usage の prompt_tokens の上限(§4.3・§10・台帳 X-58。agents.client が応答の封筒の検証で使う)。起動時に 1 以上を検証する
    max_prompt_tokens: int
    llm_timeout_seconds: float
    public_base_url: str

    def __post_init__(self) -> None:
        # 台帳 X-63: 設計書 §8.2 の 1 日の最悪の金額は max_output_tokens=2,048 で見積もっている。桁違いの値で起動できないように、
        # 上限(MAX_OUTPUT_TOKENS_CEILING)を超える値と、JSON を出せないほど小さい値は、起動のときに断る。
        if not (MIN_OUTPUT_TOKENS <= self.max_output_tokens <= MAX_OUTPUT_TOKENS_CEILING):
            raise ValueError(
                f"[agents] max_output_tokens must be between {MIN_OUTPUT_TOKENS} and {MAX_OUTPUT_TOKENS_CEILING}"
                f" (got {self.max_output_tokens}); update design.md §8.2's estimate if the ceiling must change"
            )
        # 0 以下の上限では、実際の応答(prompt_tokens は 1 以上)がすべて封筒の検証で断られる。起動のときに断る
        if self.max_prompt_tokens < 1:
            raise ValueError(f"[agents] max_prompt_tokens must be at least 1 (got {self.max_prompt_tokens})")


def load_agents_config(path: Path = _CONFIG_PATH) -> AgentsConfig:
    """config/params.toml から [agents] を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    agents_raw = raw.get("agents")
    if agents_raw is None:
        raise ValueError(f"{path} is missing the [agents] section")
    try:
        return AgentsConfig(
            model=agents_raw["model"],
            temperature=agents_raw["temperature"],
            plan_thinking_level=agents_raw["plan_thinking_level"],
            decide_thinking_level=agents_raw["decide_thinking_level"],
            max_output_tokens=agents_raw["max_output_tokens"],
            http_retry_attempts=agents_raw["http_retry_attempts"],
            max_request_body_bytes=agents_raw["max_request_body_bytes"],
            max_prompt_tokens=agents_raw["max_prompt_tokens"],
            llm_timeout_seconds=agents_raw["llm_timeout_seconds"],
            public_base_url=agents_raw["public_base_url"],
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [agents] key: {exc}") from exc


DEFAULT_AGENTS_CONFIG: AgentsConfig = load_agents_config()
