"""web が使う暫定値の読み込み(config/params.toml の [web.*]。design.md §4.1)。

エージェント呼び出しの上限・再試行・見回りの間隔は、決まったら設定ファイル側を差し替える
だけで済むようにする(vault/config.py と同じ立て付け)。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/web/config.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"


@dataclass(frozen=True)
class RefereeConfig:
    """レフェリーの暫定値(§4.1)。時間はすべて秒。"""

    agent_call_timeout_seconds: float
    agent_max_retries: int
    agent_retry_backoff_seconds: tuple[float, ...]
    wait_poll_interval_seconds: float


@dataclass(frozen=True)
class SweeperConfig:
    """交渉の見回りの暫定値(§4.1)。"""

    interval_seconds: float


@dataclass(frozen=True)
class WebConfig:
    referee: RefereeConfig
    sweeper: SweeperConfig


def load_web_config(path: Path = _CONFIG_PATH) -> WebConfig:
    """config/params.toml から [web.*] を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    web_raw = raw.get("web")
    if web_raw is None:
        raise ValueError(f"{path} is missing the [web] section")
    try:
        referee_raw = web_raw["referee"]
        referee = RefereeConfig(
            agent_call_timeout_seconds=float(referee_raw["agent_call_timeout_seconds"]),
            agent_max_retries=int(referee_raw["agent_max_retries"]),
            agent_retry_backoff_seconds=tuple(float(v) for v in referee_raw["agent_retry_backoff_seconds"]),
            wait_poll_interval_seconds=float(referee_raw["wait_poll_interval_seconds"]),
        )
        sweeper = SweeperConfig(interval_seconds=float(web_raw["sweeper"]["interval_seconds"]))
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web] key: {exc}") from exc
    if not referee.agent_retry_backoff_seconds:
        raise ValueError(f"{path}: agent_retry_backoff_seconds must not be empty")
    return WebConfig(referee=referee, sweeper=sweeper)


DEFAULT_WEB_CONFIG: WebConfig = load_web_config()
