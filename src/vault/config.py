"""金庫が使う暫定値の読み込み(config/params.toml の [vault.*]。design.md §3)。

上限・期限・寿命・TTL・T_high・1 日の予算は、すべて未決事項(U-03・U-04・U-06)の
暫定値であり、決まったら設定ファイル側を差し替えるだけで済むようにする。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/vault/config.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"


@dataclass(frozen=True)
class VaultLimits:
    """側ごとの上限(§3.5)。"""

    evaluation_budget_per_side: int
    moves_budget_per_side: int
    principal_checks_per_side: int
    consecutive_invalid_limit: int
    daily_evaluation_budget_per_principal: int


@dataclass(frozen=True)
class VaultDeadlines:
    """期限・寿命(§3.4。すべて秒)。"""

    negotiation_lifetime_seconds: int
    move_deadline_seconds: int
    principal_check_deadline_seconds: int
    max_pause_seconds: int


@dataclass(frozen=True)
class VaultConfig:
    limits: VaultLimits
    deadlines: VaultDeadlines
    t_high: int
    fictional_negotiation_ttl_seconds: int


def _load_raw_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_vault_config(path: Path = _CONFIG_PATH) -> VaultConfig:
    """config/params.toml から [vault.*] を読み込む。"""
    raw = _load_raw_config(path)
    vault_raw = raw.get("vault")
    if vault_raw is None:
        raise ValueError(f"{path} is missing the [vault] section")
    try:
        limits = VaultLimits(**vault_raw["limits"])
        deadlines = VaultDeadlines(**vault_raw["deadlines"])
        t_high = vault_raw["judgment"]["t_high"]
        ttl = vault_raw["retention"]["fictional_negotiation_ttl_seconds"]
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [vault] key: {exc}") from exc
    return VaultConfig(
        limits=limits,
        deadlines=deadlines,
        t_high=t_high,
        fictional_negotiation_ttl_seconds=ttl,
    )


DEFAULT_VAULT_CONFIG: VaultConfig = load_vault_config()
