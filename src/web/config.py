"""web が使う暫定値の読み込み(config/params.toml の [web.*]。design.md §4.1・§6.3・§3.8)。

エージェント呼び出しの上限・再試行・見回りの間隔・クッキーの寿命・利用記録の更新の間隔・
自動削除までの日数・段の状態の TTL は、決まったら設定ファイル側を差し替えるだけで済むようにする
(vault/config.py と同じ立て付け)。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/web/config.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"

# クッキーの寿命は、データの自動削除までの日数より、少なくともこの秒数だけ短くする(§6.3: 1 日早く切れる)。
# クッキーの期限は書き込みと同時にしか延ばさないので、この差があれば、まだ使えるクッキーを持つ人の
# データを見回りが消し始めることはない。
_MIN_COOKIE_MARGIN_SECONDS = 86400


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
class SessionConfig:
    """依頼者のセッションクッキーの暫定値(§6.3)。"""

    cookie_max_age_seconds: int


@dataclass(frozen=True)
class PrincipalsConfig:
    """依頼者の利用記録 principals_meta の暫定値(§6.3)。"""

    touch_interval_seconds: int
    retention_seconds: int


@dataclass(frozen=True)
class PrincipalSweeperConfig:
    """依頼者の見回りの暫定値(§4.1)。"""

    interval_seconds: float


@dataclass(frozen=True)
class RetentionConfig:
    """web の (default) に置く文書の保持期間(台帳 I-6)。"""

    fictional_stage_ttl_seconds: int


@dataclass(frozen=True)
class VaultClientConfig:
    """web から金庫を呼ぶ HTTP クライアントの暫定値。"""

    timeout_seconds: float


@dataclass(frozen=True)
class LimitsConfig:
    """画面 API の入力の大きさの上限(暫定)。"""

    max_anchors_per_kind: int
    max_blocklist_entries: int
    max_company_id_length: int


@dataclass(frozen=True)
class WebConfig:
    referee: RefereeConfig
    sweeper: SweeperConfig
    session: SessionConfig
    principals: PrincipalsConfig
    principal_sweeper: PrincipalSweeperConfig
    retention: RetentionConfig
    vault_client: VaultClientConfig
    limits: LimitsConfig


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
        session = SessionConfig(cookie_max_age_seconds=int(web_raw["session"]["cookie_max_age_seconds"]))
        principals = PrincipalsConfig(
            touch_interval_seconds=int(web_raw["principals"]["touch_interval_seconds"]),
            retention_seconds=int(web_raw["principals"]["retention_seconds"]),
        )
        principal_sweeper = PrincipalSweeperConfig(
            interval_seconds=float(web_raw["principal_sweeper"]["interval_seconds"])
        )
        retention = RetentionConfig(
            fictional_stage_ttl_seconds=int(web_raw["retention"]["fictional_stage_ttl_seconds"])
        )
        vault_client = VaultClientConfig(timeout_seconds=float(web_raw["vault_client"]["timeout_seconds"]))
        limits = LimitsConfig(
            max_anchors_per_kind=int(web_raw["limits"]["max_anchors_per_kind"]),
            max_blocklist_entries=int(web_raw["limits"]["max_blocklist_entries"]),
            max_company_id_length=int(web_raw["limits"]["max_company_id_length"]),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web] key: {exc}") from exc
    if not referee.agent_retry_backoff_seconds:
        raise ValueError(f"{path}: agent_retry_backoff_seconds must not be empty")
    if session.cookie_max_age_seconds + _MIN_COOKIE_MARGIN_SECONDS > principals.retention_seconds:
        raise ValueError(
            f"{path}: the cookie lifetime (web.session.cookie_max_age_seconds) must be at least "
            f"{_MIN_COOKIE_MARGIN_SECONDS} seconds shorter than web.principals.retention_seconds (§6.3)"
        )
    return WebConfig(
        referee=referee,
        sweeper=sweeper,
        session=session,
        principals=principals,
        principal_sweeper=principal_sweeper,
        retention=retention,
        vault_client=vault_client,
        limits=limits,
    )


DEFAULT_WEB_CONFIG: WebConfig = load_web_config()
