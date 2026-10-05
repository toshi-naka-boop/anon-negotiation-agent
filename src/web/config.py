"""web が使う暫定値の読み込み(config/params.toml の [web.*]。design.md §4.1・§6.3・§3.8・§8.2)。

エージェント呼び出しの上限・再試行・見回りの間隔・LLM の物理の呼び出し数の上限・クッキーの寿命・利用記録の更新の間隔・
自動削除までの日数・段の状態の TTL は、決まったら設定ファイル側を差し替えるだけで済むようにする
(vault/config.py と同じ立て付け)。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from negotiation_core import MAX_PLANNED_CHECKS

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
    # 金庫が、送り直しても直らないエラー(404・409 以外の 4xx)を返した後に、次に試すまで待つ時間。見回りの間隔と同じ
    # ([web.sweeper] interval_seconds から作る。台帳 L10-1)。
    client_error_wait_seconds: float


@dataclass(frozen=True)
class SweeperConfig:
    """交渉の見回りの暫定値(§4.1)。"""

    interval_seconds: float


@dataclass(frozen=True)
class LlmBudgetConfig:
    """LLM に実際に送る回数の上限(物理の呼び出し数。§4.1・§8.2。台帳 X-46・X-47・X-50・X-56)。

    daily_limit は日本時間の 0 時区切りの 1 日の上限、per_negotiation_limit は交渉ごとの上限。
    daily_counter_ttl_seconds は 1 日のカウンタの文書の TTL。max_checks_per_plan は 1 回の計画から実行する確かめの数の上限
    (negotiation_core.MAX_PLANNED_CHECKS 以下)。
    """

    daily_limit: int
    per_negotiation_limit: int
    daily_counter_ttl_seconds: int
    max_checks_per_plan: int


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
    """画面 API の入力の大きさの上限(暫定)。

    max_request_body_bytes は、すべての要求の本文の全体の上限(バイト。台帳 X-85)。ルートごとの上限(32 KB)は別にそのまま残る。
    """

    max_anchors_per_kind: int
    max_blocklist_entries: int
    max_company_id_length: int
    max_request_body_bytes: int


@dataclass(frozen=True)
class WebConfig:
    referee: RefereeConfig
    sweeper: SweeperConfig
    llm_budget: LlmBudgetConfig
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
        sweeper = SweeperConfig(interval_seconds=float(web_raw["sweeper"]["interval_seconds"]))
        referee_raw = web_raw["referee"]
        referee = RefereeConfig(
            agent_call_timeout_seconds=float(referee_raw["agent_call_timeout_seconds"]),
            agent_max_retries=int(referee_raw["agent_max_retries"]),
            agent_retry_backoff_seconds=tuple(float(v) for v in referee_raw["agent_retry_backoff_seconds"]),
            wait_poll_interval_seconds=float(referee_raw["wait_poll_interval_seconds"]),
            client_error_wait_seconds=sweeper.interval_seconds,
        )
        llm_budget_raw = web_raw["llm_budget"]
        llm_budget = LlmBudgetConfig(
            daily_limit=int(llm_budget_raw["daily_limit"]),
            per_negotiation_limit=int(llm_budget_raw["per_negotiation_limit"]),
            daily_counter_ttl_seconds=int(llm_budget_raw["daily_counter_ttl_seconds"]),
            max_checks_per_plan=int(llm_budget_raw["max_checks_per_plan"]),
        )
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
            max_request_body_bytes=int(web_raw["limits"]["max_request_body_bytes"]),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web] key: {exc}") from exc
    if not referee.agent_retry_backoff_seconds:
        raise ValueError(f"{path}: agent_retry_backoff_seconds must not be empty")
    if limits.max_request_body_bytes < 1:
        raise ValueError(f"{path}: web.limits.max_request_body_bytes must be positive")
    if llm_budget.daily_limit < 1 or llm_budget.per_negotiation_limit < 1:
        raise ValueError(f"{path}: web.llm_budget limits must be positive")
    if not 1 <= llm_budget.max_checks_per_plan <= MAX_PLANNED_CHECKS:
        raise ValueError(
            f"{path}: web.llm_budget.max_checks_per_plan must be between 1 and {MAX_PLANNED_CHECKS} (§2.7)"
        )
    if session.cookie_max_age_seconds + _MIN_COOKIE_MARGIN_SECONDS > principals.retention_seconds:
        raise ValueError(
            f"{path}: the cookie lifetime (web.session.cookie_max_age_seconds) must be at least "
            f"{_MIN_COOKIE_MARGIN_SECONDS} seconds shorter than web.principals.retention_seconds (§6.3)"
        )
    return WebConfig(
        referee=referee,
        sweeper=sweeper,
        llm_budget=llm_budget,
        session=session,
        principals=principals,
        principal_sweeper=principal_sweeper,
        retention=retention,
        vault_client=vault_client,
        limits=limits,
    )


DEFAULT_WEB_CONFIG: WebConfig = load_web_config()
