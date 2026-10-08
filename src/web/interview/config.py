"""面談の暫定値の読み込み(config/params.toml の [web.interview]。design.md §5・§8.2)。

モデル名と temperature は [agents] の値を使う。LLM の一時的なエラーの再試行の回数・待ち時間・1 呼び出しの上限は、
レフェリーと同じ([web.referee]。§4.1)なので、ここには持たない。決まったら設定ファイル側を差し替えるだけで済むようにする
(web.config と同じ立て付け。web.config は触らず、面談の分はここで読む)。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from agents.config import MAX_OUTPUT_TOKENS_CEILING, MIN_OUTPUT_TOKENS
from negotiation_core import ATTRIBUTE_BANDS

# src/web/interview/config.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "params.toml"

# 設計書 §5 の 4: パッケージ二択は 5〜8 組。
MIN_CHOICE_PAIRS = 5
MAX_CHOICE_PAIRS = 8
THINKING_LEVELS = ("MINIMAL", "LOW", "MEDIUM", "HIGH")


@dataclass(frozen=True)
class InterviewConfig:
    """面談の設定。値の意味は config/params.toml の [web.interview] のコメントのとおり。"""

    max_request_body_bytes: int
    max_output_tokens: int
    thinking_level: str
    state_idle_ttl_seconds: float
    max_active_interviews: int
    max_lifetime_seconds: float
    max_concurrent_per_client: int
    choice_pairs: int
    min_answered_pairs: int
    max_statements_per_extraction: int
    net_to_gross_ratio: float
    experience_band_upper_bounds: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.max_request_body_bytes < 1:
            raise ValueError("[web.interview] max_request_body_bytes must be positive")
        # 台帳 X-63・C-49: 面談も 1 日の最悪の金額の見積もり(2,048)の中に入る。桁違いの値で起動できないようにする
        if not (MIN_OUTPUT_TOKENS <= self.max_output_tokens <= MAX_OUTPUT_TOKENS_CEILING):
            raise ValueError(
                f"[web.interview] max_output_tokens must be between {MIN_OUTPUT_TOKENS} and {MAX_OUTPUT_TOKENS_CEILING}"
                f" (got {self.max_output_tokens}); update design.md §8.2's estimate if the ceiling must change"
            )
        if self.thinking_level not in THINKING_LEVELS:
            raise ValueError(f"[web.interview] thinking_level must be one of {THINKING_LEVELS}")
        if self.state_idle_ttl_seconds <= 0 or self.max_active_interviews < 1:
            raise ValueError("[web.interview] state_idle_ttl_seconds and max_active_interviews must be positive")
        # 台帳 C-69・X-87: 作ってからの絶対の寿命は、アイドルの寿命より短くできない(短いと、アイドルの寿命が意味を失う)。送信元ごとの同時数は 1 以上
        if self.max_lifetime_seconds < self.state_idle_ttl_seconds:
            raise ValueError("[web.interview] max_lifetime_seconds must be at least state_idle_ttl_seconds")
        if self.max_concurrent_per_client < 1:
            raise ValueError("[web.interview] max_concurrent_per_client must be positive")
        if not (MIN_CHOICE_PAIRS <= self.min_answered_pairs <= self.choice_pairs <= MAX_CHOICE_PAIRS):
            raise ValueError(
                f"[web.interview] need {MIN_CHOICE_PAIRS} <= min_answered_pairs <= choice_pairs <= {MAX_CHOICE_PAIRS} (§5 の 4)"
            )
        if self.max_statements_per_extraction < 1:
            raise ValueError("[web.interview] max_statements_per_extraction must be positive")
        if not 0 < self.net_to_gross_ratio <= 1:
            raise ValueError("[web.interview] net_to_gross_ratio must be in (0, 1]")
        bounds = self.experience_band_upper_bounds
        if len(bounds) != len(ATTRIBUTE_BANDS["experience_band"].grid) - 1 or list(bounds) != sorted(set(bounds)):
            raise ValueError(
                "[web.interview] experience_band_upper_bounds must be strictly increasing and have one fewer entry "
                "than the experience_band grid"
            )


def load_interview_config(path: Path = _CONFIG_PATH) -> InterviewConfig:
    """config/params.toml から [web.interview] を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    section = raw.get("web", {}).get("interview")
    if section is None:
        raise ValueError(f"{path} is missing the [web.interview] section")
    try:
        return InterviewConfig(
            max_request_body_bytes=int(section["max_request_body_bytes"]),
            max_output_tokens=int(section["max_output_tokens"]),
            thinking_level=str(section["thinking_level"]),
            state_idle_ttl_seconds=float(section["state_idle_ttl_seconds"]),
            max_active_interviews=int(section["max_active_interviews"]),
            max_lifetime_seconds=float(section["max_lifetime_seconds"]),
            max_concurrent_per_client=int(section["max_concurrent_per_client"]),
            choice_pairs=int(section["choice_pairs"]),
            min_answered_pairs=int(section["min_answered_pairs"]),
            max_statements_per_extraction=int(section["max_statements_per_extraction"]),
            net_to_gross_ratio=float(section["net_to_gross_ratio"]),
            experience_band_upper_bounds=tuple(float(v) for v in section["experience_band_upper_bounds"]),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web.interview] key: {exc}") from exc


DEFAULT_INTERVIEW_CONFIG: InterviewConfig = load_interview_config()
