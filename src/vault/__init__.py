"""vault: 金庫(design.md §3)。LLM を使わない決定的コードで、交渉の状態機械・イベント列・
架空人物のテンプレート・最終判定を持つ。negotiation_core をそのまま使い、同じ処理を
重複して書かない。
"""

from vault.clock import Clock, FixedClock, SystemClock
from vault.config import DEFAULT_VAULT_CONFIG, VaultConfig, load_vault_config
from vault.store import VaultStore

__all__ = [
    "DEFAULT_VAULT_CONFIG",
    "Clock",
    "FixedClock",
    "SystemClock",
    "VaultConfig",
    "VaultStore",
    "load_vault_config",
]
