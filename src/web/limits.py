"""入口ごとのレート制限(design.md §8.2「レート制限」。台帳 L4-2・C-3・X-10・X-30。調査事項 R-7)。

LLM を呼ぶか交渉を作るか、1 回で金庫を何件も読むすべての入口に、クライアントごと・入口ごとの回数の上限を掛け、全入口の合計にも上限を掛ける。
入口は ENTRANCES の 7 つ: 面談の LLM 呼び出し・デモの実行・ライブ交渉の作成・攻撃モードの交渉の作成・攻撃モードの指示(攻撃の手)・
壁 1 の生メッセージ・推定区間メーター(POST /v1/demo/meter。1 回で金庫と Firestore を最大 20 件ずつ読む)。枠は入口ごとに別なので、
ある入口の枠を使い切っても、別の入口は使える(台帳 L4-2)。上限は config/params.toml の [web.limits](rate_*・per_client)。
発表の日は設定で上げる(そこに運用メモがある)。

- 回数は、`(default)` の Firestore の時間窓カウンタに、トランザクションで数える(台帳 X-10)。再起動や新しいリビジョンでも消えない。
  窓は固定の区切り(UNIX 時刻を窓の長さで割った商が同じ間。暫定 10 分)。窓の境目の前後で短い間に最大 2 倍通ることは、固定の窓の
  性質として受け入れる(費用の保証は、LLM の呼び出し数の上限 web.llm_budget が受け持つ)。
- 1 回の要求で、(入口, クライアント, 窓)の文書と、(全体, 窓)の文書を、1 つのトランザクションで読み、どちらも上限に達していなければ
  両方を 1 進める。どちらかが上限に達していれば、何も進めずに断る(拒否した要求は、どの枠にも数えない。web.llm_budget と同じ)。
  断る理由は、クライアントの枠(scope=client)を先に見て、次に全体(scope=overall)。
- クライアントは、web.client_ip.client_ip(Cloud Run のフロントエンドが X-Forwarded-For に追記した、末尾の IP。先頭側の、利用者が
  書ける値は使わない。台帳 C-3)。文書の ID には IP をそのまま入れず、SHA-256 の先頭 32 桁にする(文書 ID に使えない文字が入っても
  壊れない)。文書には TTL 用の ttl_at を持たせる(Firestore の TTL ポリシーの設定はデプロイの段)。
- カウンタに書けない・読めないとき(Firestore の失敗、数の項目が壊れている)は、通さない(閉じる側に倒す。web.llm_budget と同じ。
  台帳 X-50): RateLimiterUnavailable(HTTP では 503)。値は、ログにも例外にも書かない。

使い方: `Depends(services.limiter.guard("demo_run"))`。超えたら 429(Retry-After は窓の終わりまでの秒数。本文は
{"detail": {"code": "rate_limited", "entrance", "scope", "limit", "window_seconds", "retry_after_seconds"}}。画面が「実演」として
理由を出せるように)。面談の LLM 呼び出し(3 問・自由コメント・辞めた理由)は web.interview.api が `guard("interview_llm")` を、
メーターの POST は web.meter_api が `guard("meter")` を付ける(LLM も金庫も呼ばない GET .../meter/simulation には付けない)。

Firestore(同期クライアント)の呼び出しは別スレッドで行う(web.llm_budget と同じ)。
"""

import asyncio
import datetime as dt
import hashlib
import logging
import math
import random
import time
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from fastapi import HTTPException, Request
from google.api_core.exceptions import Aborted
from google.cloud import firestore

from vault.clock import Clock

from web.client_ip import client_ip

_log = logging.getLogger(__name__)

# src/web/limits.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"

RATE_LIMITS_COLLECTION = "rate_limits"
_OVERALL_DOCUMENT_PREFIX = "overall"

# レート制限を掛ける入口(§8.2)。名前は config/params.toml の [web.limits.per_client] と同じ。
Entrance = Literal[
    "interview_llm",
    "demo_run",
    "live_negotiation_create",
    "attack_create",
    "attack_instruction",
    "raw_message",
    "meter",
]
ENTRANCES: tuple[Entrance, ...] = get_args(Entrance)

LimitScope = Literal["client", "overall"]

# トランザクションの再試行は「内側 5 回 × 外側 3 回」(web.llm_budget と同じ考え方。全入口の合計の文書は全要求が書く 1 つの文書なので、
# 競合しやすい。内側を使い切ったら、乱数の待ちを入れてから、新しいトランザクションでやり直す)。
_INNER_MAX_ATTEMPTS = 5
_OUTER_ATTEMPTS = 3
_OUTER_BACKOFF_SECONDS = (0.010, 0.200)


def _positive_int(value: object, name: str) -> int:
    """設定の値が 1 以上の整数であること(bool・小数は不可)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"[web.limits] {name} must be a positive integer (got {value!r})")
    return value


@dataclass(frozen=True)
class RateLimitConfig:
    """入口ごとのレート制限の設定([web.limits])。

    window_seconds は窓の長さ、overall_limit は全入口の合計の窓あたりの上限、counter_ttl_seconds は文書の TTL(窓の終わりから)、
    per_client は入口ごとの、クライアント 1 つあたりの窓あたりの上限。per_client の名前は ENTRANCES とちょうど同じでなければならない
    (足りない入口が枠なしで通ったり、打ち間違いが黙って無視されたりしないように)。
    """

    window_seconds: int
    overall_limit: int
    counter_ttl_seconds: int
    per_client: Mapping[str, int]

    def __post_init__(self) -> None:
        _positive_int(self.window_seconds, "rate_window_seconds")
        _positive_int(self.overall_limit, "rate_overall_limit")
        _positive_int(self.counter_ttl_seconds, "rate_counter_ttl_seconds")
        if set(self.per_client) != set(ENTRANCES):
            missing, extra = set(ENTRANCES) - set(self.per_client), set(self.per_client) - set(ENTRANCES)
            raise ValueError(
                f"[web.limits.per_client] must name exactly the entrances {list(ENTRANCES)} "
                f"(missing: {sorted(missing)}, unknown: {sorted(extra)})"
            )
        for entrance, limit in self.per_client.items():
            _positive_int(limit, f"per_client.{entrance}")


def load_rate_limit_config(path: Path = _CONFIG_PATH) -> RateLimitConfig:
    """config/params.toml から入口ごとのレート制限([web.limits] の rate_*・per_client)を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    try:
        limits = raw["web"]["limits"]
        return RateLimitConfig(
            window_seconds=limits["rate_window_seconds"],
            overall_limit=limits["rate_overall_limit"],
            counter_ttl_seconds=limits["rate_counter_ttl_seconds"],
            per_client=dict(limits["per_client"]),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web.limits] key: {exc}") from exc


DEFAULT_RATE_LIMIT_CONFIG: RateLimitConfig = load_rate_limit_config()


class RateLimitExceeded(Exception):
    """上限に達している。scope は、どの枠か(client = その入口のクライアントごとの枠、overall = 全入口の合計の枠)。"""

    def __init__(
        self, entrance: Entrance, scope: LimitScope, *, limit: int, window_seconds: int, retry_after_seconds: int
    ) -> None:
        super().__init__(f"rate limit reached entrance={entrance} scope={scope}")
        self.entrance = entrance
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after_seconds = retry_after_seconds


class RateLimiterUnavailable(Exception):
    """カウンタに書けない・読めない。呼び出し側は通さない(閉じる側。メッセージは原因の例外の型名だけ)。"""


class _CorruptCounter(ValueError):
    """カウンタの文書はあるが、数の項目が欠けている・整数でない・負(0 とみなして通さず、閉じる側に倒す)。"""


def _count_in(data: dict | None) -> int:
    value = (data or {}).get("count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _CorruptCounter
    return value


def _client_digest(client: str) -> str:
    """クライアント(IP)から、文書 ID に使う値(SHA-256 の先頭 32 桁)を作る。"""
    return hashlib.sha256(client.encode("utf-8")).hexdigest()[:32]


class RateLimiter:
    """入口ごとのレート制限。時刻は注入できる時計から取る。"""

    def __init__(
        self,
        db: firestore.Client,
        clock: Clock,
        config: RateLimitConfig = DEFAULT_RATE_LIMIT_CONFIG,
    ) -> None:
        self._db = db
        self._clock = clock
        self._config = config

    def _run_transaction(self, txn_fn):
        last_exc: Exception | None = None
        for attempt in range(_OUTER_ATTEMPTS):
            if attempt > 0:
                time.sleep(random.uniform(*_OUTER_BACKOFF_SECONDS))
            try:
                return firestore.transactional(txn_fn)(self._db.transaction(max_attempts=_INNER_MAX_ATTEMPTS))
            except Aborted as exc:  # トランザクションの中の読み取りで出た競合(内側では再試行されない)
                last_exc = exc
            except ValueError as exc:  # コミットの競合が、内側の再試行を使い切った
                if not isinstance(exc.__cause__, Aborted):
                    raise
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def _admit_sync(self, entrance: Entrance, client: str) -> None:
        config = self._config
        now_seconds = self._clock.now().timestamp()
        window = int(now_seconds) // config.window_seconds
        window_start = window * config.window_seconds
        window_end = window_start + config.window_seconds
        started_at = dt.datetime.fromtimestamp(window_start, dt.timezone.utc)
        ttl_at = dt.datetime.fromtimestamp(window_end, dt.timezone.utc) + dt.timedelta(
            seconds=config.counter_ttl_seconds
        )
        collection = self._db.collection(RATE_LIMITS_COLLECTION)
        client_ref = collection.document(f"{entrance}.{window}.{_client_digest(client)}")
        overall_ref = collection.document(f"{_OVERALL_DOCUMENT_PREFIX}.{window}")
        client_limit = config.per_client[entrance]

        def txn_fn(txn: firestore.Transaction) -> LimitScope | None:
            # Firestore のトランザクションは、読み出しをすべて終えてから書く。
            client_snap = client_ref.get(transaction=txn)
            overall_snap = overall_ref.get(transaction=txn)
            client_count = _count_in(client_snap.to_dict()) if client_snap.exists else 0
            overall_count = _count_in(overall_snap.to_dict()) if overall_snap.exists else 0
            if client_count >= client_limit:
                return "client"
            if overall_count >= config.overall_limit:
                return "overall"
            txn.set(
                client_ref,
                {"count": client_count + 1, "entrance": entrance, "window_start": started_at, "ttl_at": ttl_at},
            )
            txn.set(overall_ref, {"count": overall_count + 1, "window_start": started_at, "ttl_at": ttl_at})
            return None

        denied = self._run_transaction(txn_fn)
        if denied is not None:
            raise RateLimitExceeded(
                entrance,
                denied,
                limit=client_limit if denied == "client" else config.overall_limit,
                window_seconds=config.window_seconds,
                retry_after_seconds=max(1, math.ceil(window_end - now_seconds)),
            )

    async def admit(self, entrance: Entrance, client: str) -> None:
        """entrance への要求を 1 回数える。上限に達していれば RateLimitExceeded(何も数えない)、数えられなければ RateLimiterUnavailable。"""
        try:
            await asyncio.to_thread(self._admit_sync, entrance, client)
        except RateLimitExceeded:
            raise
        except Exception as exc:
            _log.error("rate limit counter unavailable error=%s", type(exc).__name__)
            raise RateLimiterUnavailable(type(exc).__name__) from exc

    def guard(self, entrance: Entrance) -> Callable[[Request], Awaitable[None]]:
        """FastAPI の依存(`Depends(limiter.guard("demo_run"))`)。要求を数え、超えていれば 429、数えられなければ 503 にする。

        依存は、本文を読む前に動く(本文を宣言しないルートなら、本文を読む前に断れる)。
        """

        async def dependency(request: Request) -> None:
            try:
                await self.admit(entrance, client_ip(request))
            except RateLimitExceeded as exc:
                _log.info("rate limited entrance=%s scope=%s", exc.entrance, exc.scope)
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "rate_limited",
                        "entrance": exc.entrance,
                        "scope": exc.scope,
                        "limit": exc.limit,
                        "window_seconds": exc.window_seconds,
                        "retry_after_seconds": exc.retry_after_seconds,
                    },
                    headers={"Retry-After": str(exc.retry_after_seconds)},
                ) from None
            except RateLimiterUnavailable:
                raise HTTPException(status_code=503, detail="temporarily_unavailable") from None

        return dependency
