"""TEE(Confidential Space)の設定の読み込み(config/params.toml の [vault.tee]。design.md §9、research/tee-spike-contract.md §1)。

web と検証スクリプトが使う。金庫の TEE 版(vault.config)も同じ節を読む(二重に読むのはスパイクの割り切り)。
プロジェクト ID・番号・ゾーンは、設定にもコードにも書かない(web は環境変数で受け、金庫は実行時にメタデータサーバから取る)。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/negotiation_core/tee_settings.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"


@dataclass(frozen=True)
class TeeSettings:
    """[vault.tee] のキーそのまま。"""

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


def _text(raw: dict, key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[vault.tee] {key} must be a non-empty string")
    return value


def load_tee_settings(path: Path = _CONFIG_PATH) -> TeeSettings:
    """config/params.toml から [vault.tee] を読み込む。節やキーがなければ ValueError。"""
    with path.open("rb") as f:
        raw = tomllib.load(f).get("vault", {}).get("tee")
    if raw is None:
        raise ValueError(f"{path} is missing the [vault.tee] section")
    try:
        hwmodels = raw["allowed_hwmodels"]
        if not isinstance(hwmodels, list) or not hwmodels or not all(isinstance(m, str) and m for m in hwmodels):
            raise ValueError("[vault.tee] allowed_hwmodels must be a non-empty list of strings")
        settings = TeeSettings(
            port=int(raw["port"]),
            attestation_audience=_text(raw, "attestation_audience"),
            caller_audience=_text(raw, "caller_audience"),
            caller_service_account=_text(raw, "caller_service_account"),
            workload_identity_pool=_text(raw, "workload_identity_pool"),
            workload_identity_provider=_text(raw, "workload_identity_provider"),
            kms_key_ring=_text(raw, "kms_key_ring"),
            kms_key=_text(raw, "kms_key"),
            launcher_socket=_text(raw, "launcher_socket"),
            claims_token_file=_text(raw, "claims_token_file"),
            min_attestation_interval_seconds=float(raw["min_attestation_interval_seconds"]),
            tls_certificate_days=int(raw["tls_certificate_days"]),
            tls_dir=_text(raw, "tls_dir"),
            allowed_hwmodels=tuple(hwmodels),
            attestation_issuer=_text(raw, "attestation_issuer"),
            attestation_signer_certs_url=_text(raw, "attestation_signer_certs_url"),
            caller_certs_url=_text(raw, "caller_certs_url"),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [vault.tee] key: {exc}") from exc
    if not 1 <= settings.port <= 65535:
        raise ValueError(f"{path}: [vault.tee] port must be between 1 and 65535")
    if settings.min_attestation_interval_seconds < 0 or settings.tls_certificate_days < 1:
        raise ValueError(f"{path}: [vault.tee] min_attestation_interval_seconds and tls_certificate_days are out of range")
    return settings
