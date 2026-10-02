"""agents が使う暫定値の読み込み(config/params.toml の [agents]。design.md §4.2・§4.3)。

モデル名・本文の上限などは未決事項(U-03、調査事項 R-3)の暫定値で、決まったら設定ファイル側を
差し替えるだけで済むようにする。思考の量(計画・決定)・1 呼び出しの出力の上限・クライアント側の再試行の回数も、
ここから読む(v14。調査 R-9・台帳 X-50・X-55)。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/agents/config.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"


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
    llm_timeout_seconds: float
    public_base_url: str


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
            llm_timeout_seconds=agents_raw["llm_timeout_seconds"],
            public_base_url=agents_raw["public_base_url"],
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [agents] key: {exc}") from exc


DEFAULT_AGENTS_CONFIG: AgentsConfig = load_agents_config()
