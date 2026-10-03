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


@dataclass(frozen=True)
class VaultTeeConfig:
    """TEE(Confidential Space)版の金庫だけが使う設定([vault.tee]。design.md §9、research/tee-spike-contract.md §1)。

    プロジェクト ID・番号・ゾーンは持たない(金庫は実行時にメタデータサーバから取る)。allowed_hwmodels・attestation_issuer・
    attestation_signer_certs_url は web とスクリプトの検証が使う値だが、同じ節を二重に読む契約なので、ここにも持つ。
    """

    port: int
    attestation_audience: str
    caller_audience: str
    caller_service_account: str
    workload_identity_pool: str
    workload_identity_provider: str
    kms_key_ring: str
    kms_key: str
    launcher_socket: str
    claims_token_file: str
    min_attestation_interval_seconds: float
    tls_certificate_days: int
    tls_dir: str
    allowed_hwmodels: tuple[str, ...]
    attestation_issuer: str
    attestation_signer_certs_url: str
    caller_certs_url: str


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


def load_vault_tee_config(path: Path = _CONFIG_PATH) -> VaultTeeConfig:
    """config/params.toml から [vault.tee] を読み込む。

    TEE 版の起動口(vault.tee.main)だけが呼ぶ。Cloud Run 版は読まない: import 時には読まないので、この節が
    なくても Cloud Run 版は動く(DEFAULT_VAULT_CONFIG のような既定値は作らない)。
    """
    raw = _load_raw_config(path)
    tee_raw = raw.get("vault", {}).get("tee")
    if tee_raw is None:
        raise ValueError(f"{path} is missing the [vault.tee] section")
    try:
        return VaultTeeConfig(**{**tee_raw, "allowed_hwmodels": tuple(tee_raw["allowed_hwmodels"])})
    except (KeyError, TypeError) as exc:  # 足りないキー・知らないキー
        raise ValueError(f"{path} has a missing or unknown key in [vault.tee]: {exc}") from exc


DEFAULT_VAULT_CONFIG: VaultConfig = load_vault_config()
