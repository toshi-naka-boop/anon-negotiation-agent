"""攻撃モードの設定の読み込み(config/params.toml の [web.attack]。design.md §8.2・§3.7。台帳 P-17)。

攻撃モードの交渉の相手(架空人物のテンプレートの ID)は、ここで決める。攻撃画面からは選ばせない(架空人物以外には攻撃できない。AC-13)。
fixtures/case3.toml ができたら、設定の 2 つの名前を差し替えるだけで済む。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/web/attack/config.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "params.toml"


@dataclass(frozen=True)
class AttackConfig:
    """攻撃モードの設定。

    candidate_template_id・employer_template_id: 攻撃モードの交渉の相手(金庫の templates/{id})。
    context_ttl_seconds: 攻撃の指示を web のメモリに持つ時間。
    llm_context_max_negotiations: 壁 2 のために、直近の LLM の入力を覚えておく交渉の数の上限。
    max_body_bytes: 攻撃の API の本文の上限(バイト)。
    """

    candidate_template_id: str
    employer_template_id: str
    context_ttl_seconds: int
    llm_context_max_negotiations: int
    max_body_bytes: int

    def __post_init__(self) -> None:
        for name in ("candidate_template_id", "employer_template_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"[web.attack] {name} must be a non-empty string")
        for name in ("context_ttl_seconds", "llm_context_max_negotiations", "max_body_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"[web.attack] {name} must be a positive integer (got {value!r})")


def load_attack_config(path: Path = _CONFIG_PATH) -> AttackConfig:
    """config/params.toml から [web.attack] を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    try:
        attack = raw["web"]["attack"]
        return AttackConfig(
            candidate_template_id=attack["candidate_template_id"],
            employer_template_id=attack["employer_template_id"],
            context_ttl_seconds=attack["context_ttl_seconds"],
            llm_context_max_negotiations=attack["llm_context_max_negotiations"],
            max_body_bytes=attack["max_body_bytes"],
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web.attack] key: {exc}") from exc


DEFAULT_ATTACK_CONFIG: AttackConfig = load_attack_config()
