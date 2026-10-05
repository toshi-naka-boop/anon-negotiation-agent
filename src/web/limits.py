"""入口ごとのレート制限(design.md §8.2「レート制限」。台帳 L4-2・C-3・X-10・X-30・C-65・C-66・C-68・C-71・L19-7・L19-12。調査事項 R-7)。

LLM を呼ぶか交渉を作るか、1 回で金庫を何件も読むか、面談の状態をメモリに作るすべての入口に、クライアントごと・入口ごとの回数の上限を掛ける。
入口は ENTRANCES の 9 つで、全体の枠(全入口の合計の上限)に数えるかどうかで 2 つに分かれる。
- 全体の枠にも数える 6 つ(OVERALL_ENTRANCES): 面談の LLM 呼び出し・デモの実行・ライブ交渉の作成・攻撃モードの交渉の作成・
  攻撃モードの指示(攻撃の手)・壁 1 の生メッセージ。LLM を呼ぶか、交渉を作る。
- クライアントごとの枠だけの 3 つ(CLIENT_ONLY_ENTRANCES。台帳 L19-7): 推定区間メーター(POST /v1/demo/meter。1 回で金庫と Firestore を最大 20 件ずつ読む)・
  開始ページ(GET /start。依頼者 ID を発行する。台帳 C-66)・面談の開始(POST .../interview/begin。面談の状態をメモリに作る。台帳 C-66)。
  LLM を呼ばず、金庫かメモリを読むだけ。全体の枠に数えず、全体の枠が埋まっていても断らない(読み出しが、本物の利用者の LLM・作成の枠を食わないように)。
枠は入口ごとに別なので、ある入口の枠を使い切っても、別の入口は使える(台帳 L4-2)。上限は config/params.toml の [web.limits](rate_*・per_client)。
発表の日は設定で上げる(そこに運用メモがある)。

- 回数は、`(default)` の Firestore の時間窓カウンタに、トランザクションで数える(台帳 X-10)。再起動や新しいリビジョンでも消えない。
  窓は固定の区切り(UNIX 時刻を窓の長さで割った商が同じ間。暫定 10 分)。窓の境目の前後で短い間に最大 2 倍通ることは、固定の窓の
  性質として受け入れる(費用の保証は、LLM の呼び出し数の上限 web.llm_budget が受け持つ)。
- 1 回の要求で、(入口, クライアント, 窓)の文書と、(全体, 窓)の文書を、1 つのトランザクションで読み、どちらも上限に達していなければ
  両方を 1 進める。どちらかが上限に達していれば、何も進めずに断る(拒否した要求は、どの枠にも数えない。web.llm_budget と同じ)。
  断る理由は、クライアントの枠(scope=client)を先に見て、次に全体(scope=overall)。クライアントごとの枠だけの入口は、(入口, クライアント, 窓)の文書だけを読み・進める。
- クライアントは、web.client_ip.client_key(Cloud Run のフロントエンドが X-Forwarded-For に追記した、末尾の IP。先頭側の、利用者が
  書ける値は使わない。台帳 C-3。IPv6 は /64 の接頭辞にまとめる。台帳 C-71)。文書の ID には IP をそのまま入れず、鍵つきの HMAC-SHA256 の先頭 32 桁にする(文書 ID に使えない文字が入っても
  壊れない)。鍵がない SHA-256 だと、IPv4 の全数(約 43 億)を試して IP を戻せて、`(default)` を読める者に、直近の窓で入口を使った IP の一覧が渡る
  (台帳 L19-12)。鍵は、セッションの署名の鍵(SESSION_SIGNING_KEY。web.session)から、用途を表す固定のラベルで派生させる(derive_limiter_key)。
  鍵を替えると文書の ID が変わる(その窓の数え直しになる)。文書には TTL 用の ttl_at を持たせる(Firestore の TTL ポリシーの設定はデプロイの段)。
- カウンタに書けない・読めないとき(Firestore の失敗、数の項目が壊れている)は、通さない(閉じる側に倒す。web.llm_budget と同じ。
  台帳 X-50): RateLimiterUnavailable(HTTP では 503)。値は、ログにも例外にも書かない。

使い方: `Depends(services.limiter.guard("demo_run"))`。超えたら 429(Retry-After は窓の終わりまでの秒数。本文は
{"detail": {"code": "rate_limited", "entrance", "scope", "limit", "window_seconds", "retry_after_seconds"}}。画面が「実演」として
理由を出せるように)。面談の LLM 呼び出し(3 問・自由コメント・辞めた理由)は web.interview.api が `guard("interview_llm")` を、
面談の開始は `guard("interview_begin")` を、開始ページの GET は web.api が `guard("session_start")` を、
メーターの POST は web.meter_api が `guard("meter")` を付ける(LLM も金庫も呼ばない GET .../meter/simulation には付けない)。

SSE の同時本数(SseConnectionLimiter。台帳 C-65): SSE(/v1/stream/。web.ui_api)は 1 本が最長 30 秒つながるので、web が 1 インスタンスのとき、
匿名のクライアントが、同時リクエストの枠を SSE で埋められる。そこで、いまつないである本数を、全体(sse_max_connections)と、クライアント IP ごと
(sse_max_connections_per_client)に数え、超えたら 429(入口は sse。Retry-After は 2 秒。画面は、通常の GET の再取得に切り替える)。
時間窓の回数ではなく、メモリの中の同時本数なので、Firestore には触れず、全体の枠にも数えない(ENTRANCES には入れない)。接続が終われば、
必ず戻す(SseSlot.release。web.ui_api の応答が、終わり方によらず呼ぶ)。

ログインなしの読み取りの枠(AnonymousReadLimiter・AnonymousReadLimitMiddleware。台帳 C-68): セッションなしで金庫(と Firestore)を読む GET(デモの活動・2 パネル・
イベント・段、攻撃のイベント・壁 2・壁 3。web.api の ANONYMOUS_READ_PATH_PREFIXES)に、クライアントごとの 1 分あたりの回数の枠([web.limits] の
anonymous_read_per_minute。暫定 120 回)を掛ける。1 回の GET が金庫を 1〜2 回・Firestore を数十件読むので、匿名の 1 クライアントが、金庫への接続(web の全員で共有)と
読み出しの課金を増やせないように。web は 1 インスタンスなので、メモリの中だけで数える(Firestore に書くと、読み取りのたびに書き込みの課金が増える)。
全体の枠(rate_overall_limit)には数えず、Firestore には触れない。超えたら 429(入口は anonymous_read、Retry-After は窓の終わりまでの秒数。本文は同じ形)。
SSE(自前の同時本数の上限)・TEE の attestation(自前の転送の間隔)・静的ファイル・/health・面談の注記は、対象にしない。

Firestore(同期クライアント)の呼び出しは別スレッドで行う(web.llm_budget と同じ)。
"""

import asyncio
import datetime as dt
import hashlib
import hmac
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
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from vault.clock import Clock

from web.client_ip import client_key
from web.session import validate_session_key

_log = logging.getLogger(__name__)

# src/web/limits.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"

RATE_LIMITS_COLLECTION = "rate_limits"
_OVERALL_DOCUMENT_PREFIX = "overall"

# レート制限を掛ける入口(§8.2)。名前は config/params.toml の [web.limits.per_client] と同じ。
# 前半の 6 つは、LLM を呼ぶか交渉を作る入口で、全体の枠にも数える。後半の 3 つは、LLM を呼ばず、金庫・Firestore・メモリを読むだけの入口で、
# クライアントごとの枠だけで数える(CLIENT_ONLY_ENTRANCES。台帳 L19-7)。
Entrance = Literal[
    "interview_llm",
    "demo_run",
    "live_negotiation_create",
    "attack_create",
    "attack_instruction",
    "raw_message",
    "meter",
    "session_start",
    "interview_begin",
]
ENTRANCES: tuple[Entrance, ...] = get_args(Entrance)
# 全体の枠に数えない入口(台帳 L19-7)。数えず、全体の枠が埋まっていても断らない。クライアントごとの枠だけで守る。
CLIENT_ONLY_ENTRANCES: frozenset[Entrance] = frozenset({"meter", "session_start", "interview_begin"})
# 全体の枠にも数える入口(ENTRANCES の残り。並びは ENTRANCES と同じ)。
OVERALL_ENTRANCES: tuple[Entrance, ...] = tuple(name for name in ENTRANCES if name not in CLIENT_ONLY_ENTRANCES)

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

    window_seconds は窓の長さ、overall_limit は全体の枠(OVERALL_ENTRANCES の合計)の窓あたりの上限、counter_ttl_seconds は文書の TTL(窓の終わりから)、
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


@dataclass(frozen=True)
class SseLimitConfig:
    """SSE の同時につなげておく本数の上限([web.limits] の sse_max_connections・sse_max_connections_per_client。台帳 C-65)。

    max_connections は全体の本数、max_connections_per_client はクライアント IP 1 つあたりの本数。
    """

    max_connections: int
    max_connections_per_client: int

    def __post_init__(self) -> None:
        _positive_int(self.max_connections, "sse_max_connections")
        _positive_int(self.max_connections_per_client, "sse_max_connections_per_client")


def load_sse_limit_config(path: Path = _CONFIG_PATH) -> SseLimitConfig:
    """config/params.toml から SSE の同時本数の上限([web.limits] の sse_*)を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    try:
        limits = raw["web"]["limits"]
        return SseLimitConfig(
            max_connections=limits["sse_max_connections"],
            max_connections_per_client=limits["sse_max_connections_per_client"],
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web.limits] key: {exc}") from exc


DEFAULT_SSE_LIMIT_CONFIG: SseLimitConfig = load_sse_limit_config()


@dataclass(frozen=True)
class AnonymousReadLimitConfig:
    """ログインなしの読み取りの枠の上限([web.limits] の anonymous_read_per_minute。台帳 C-68)。

    per_minute は、クライアント 1 つあたりの、1 分(ANONYMOUS_READ_WINDOW_SECONDS)あたりの回数。
    """

    per_minute: int

    def __post_init__(self) -> None:
        _positive_int(self.per_minute, "anonymous_read_per_minute")


def load_anonymous_read_limit_config(path: Path = _CONFIG_PATH) -> AnonymousReadLimitConfig:
    """config/params.toml からログインなしの読み取りの枠の上限([web.limits] の anonymous_read_per_minute)を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    try:
        return AnonymousReadLimitConfig(per_minute=raw["web"]["limits"]["anonymous_read_per_minute"])
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web.limits] key: {exc}") from exc


DEFAULT_ANONYMOUS_READ_LIMIT_CONFIG: AnonymousReadLimitConfig = load_anonymous_read_limit_config()


# 文書 ID の HMAC の鍵を、セッションの署名の鍵から派生させるときの、用途を表す固定のラベル(秘密ではない)。
_LIMITER_KEY_LABEL = b"rate-limits"


def derive_limiter_key(session_signing_key: str) -> bytes:
    """文書 ID に使う HMAC の鍵を、セッションの署名の鍵(SESSION_SIGNING_KEY)から作る(台帳 L19-12)。

    署名の鍵そのものを別の用途に使い回さず、用途を表す固定のラベル(b"rate-limits")で HMAC-SHA256 をとって派生させる。
    署名の鍵は web.session.validate_session_key で確かめる(弱い鍵・空の鍵からは作らない。WeakSessionKeyError)。
    """
    validate_session_key(session_signing_key)
    return hmac.new(session_signing_key.encode("utf-8"), _LIMITER_KEY_LABEL, hashlib.sha256).digest()


def rate_limited_error(
    entrance: str, scope: LimitScope, *, limit: int, window_seconds: int | None, retry_after_seconds: int
) -> HTTPException:
    """429 の応答(本文は {"detail": {"code": "rate_limited", "entrance", "scope", "limit", "window_seconds", "retry_after_seconds"}}、Retry-After つき)。

    window_seconds は、時間窓の長さ。時間窓のない上限(SSE の同時本数・面談の同時数)は None。面談の同時数(web.interview.service)も、この形で断る。
    """
    return HTTPException(
        status_code=429,
        detail={
            "code": "rate_limited",
            "entrance": entrance,
            "scope": scope,
            "limit": limit,
            "window_seconds": window_seconds,
            "retry_after_seconds": retry_after_seconds,
        },
        headers={"Retry-After": str(retry_after_seconds)},
    )


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


def _client_digest(key: bytes, client: str) -> str:
    """クライアント(IP)から、文書 ID に使う値(鍵 key の HMAC-SHA256 の先頭 32 桁)を作る(台帳 L19-12)。"""
    return hmac.new(key, client.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


class RateLimiter:
    """入口ごとのレート制限。時刻は注入できる時計から取る。

    key は、文書 ID に使うクライアントの IP のハッシュ(HMAC-SHA256)の鍵(derive_limiter_key で作る。台帳 L19-12)。空は拒否する(鍵なしのハッシュに戻さない)。
    """

    def __init__(
        self,
        db: firestore.Client,
        clock: Clock,
        config: RateLimitConfig = DEFAULT_RATE_LIMIT_CONFIG,
        *,
        key: bytes,
    ) -> None:
        if not isinstance(key, bytes) or not key:
            raise ValueError("the rate limiter key must be non-empty bytes (see derive_limiter_key)")
        self._db = db
        self._clock = clock
        self._config = config
        self._key = key

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
        counts_overall = entrance not in CLIENT_ONLY_ENTRANCES  # 全体の枠に数える入口か(台帳 L19-7)
        now_seconds = self._clock.now().timestamp()
        window = int(now_seconds) // config.window_seconds
        window_start = window * config.window_seconds
        window_end = window_start + config.window_seconds
        started_at = dt.datetime.fromtimestamp(window_start, dt.timezone.utc)
        ttl_at = dt.datetime.fromtimestamp(window_end, dt.timezone.utc) + dt.timedelta(
            seconds=config.counter_ttl_seconds
        )
        collection = self._db.collection(RATE_LIMITS_COLLECTION)
        client_ref = collection.document(f"{entrance}.{window}.{_client_digest(self._key, client)}")
        overall_ref = collection.document(f"{_OVERALL_DOCUMENT_PREFIX}.{window}")
        client_limit = config.per_client[entrance]

        def txn_fn(txn: firestore.Transaction) -> LimitScope | None:
            # Firestore のトランザクションは、読み出しをすべて終えてから書く。全体の文書は、全体の枠に数える入口だけが読み、進める。
            client_snap = client_ref.get(transaction=txn)
            overall_snap = overall_ref.get(transaction=txn) if counts_overall else None
            client_count = _count_in(client_snap.to_dict()) if client_snap.exists else 0
            overall_count = 0
            if overall_snap is not None and overall_snap.exists:
                overall_count = _count_in(overall_snap.to_dict())
            if client_count >= client_limit:
                return "client"
            if counts_overall and overall_count >= config.overall_limit:
                return "overall"
            txn.set(
                client_ref,
                {"count": client_count + 1, "entrance": entrance, "window_start": started_at, "ttl_at": ttl_at},
            )
            if counts_overall:
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

        依存は、本文を読む前に動く(本文を宣言しないルートなら、本文を読む前に断れる)。クライアントは client_key(IPv6 は /64 にまとめる。台帳 C-71)。
        """

        async def dependency(request: Request) -> None:
            try:
                await self.admit(entrance, client_key(request))
            except RateLimitExceeded as exc:
                _log.info("rate limited entrance=%s scope=%s", exc.entrance, exc.scope)
                raise rate_limited_error(
                    exc.entrance,
                    exc.scope,
                    limit=exc.limit,
                    window_seconds=exc.window_seconds,
                    retry_after_seconds=exc.retry_after_seconds,
                ) from None
            except RateLimiterUnavailable:
                raise HTTPException(status_code=503, detail="temporarily_unavailable") from None

        return dependency


# ----------------------------------------------------------------------
# SSE の同時本数(台帳 C-65)
# ----------------------------------------------------------------------

# 429 の本文の入口の名前。ENTRANCES には入れない(時間窓の回数ではなく、同時本数。Firestore には数えない)。
SSE_ENTRANCE = "sse"
# 面談の同時数(送信元ごとに同時に持てる面談の状態の数。web.interview.state・service。台帳 C-69・X-87)の 429 の入口の名前。これも時間窓ではなく、メモリの中の同時数
# (window_seconds は null)。上限は [web.interview] max_concurrent_per_client。
INTERVIEW_CONCURRENT_ENTRANCE = "interview_concurrent"
# 429 の Retry-After(秒)。画面が、SSE をあきらめて通常の GET の再取得に切り替えたときの間隔と同じ(web.ui_api の StreamConfig の周期)。
SSE_RETRY_AFTER_SECONDS = 2


class SseLimitReached(Exception):
    """SSE の同時本数が上限に達している。scope は、どの上限か(client = クライアント IP ごと、overall = 全体)。limit は、その上限の本数。"""

    def __init__(self, scope: LimitScope, *, limit: int) -> None:
        super().__init__(f"sse connection limit reached scope={scope}")
        self.scope = scope
        self.limit = limit


class SseSlot:
    """つないでいる SSE 1 本ぶんの席。release は、何度呼んでも 1 回しか戻さない(応答の終わりと、確認の失敗の両方から呼ばれても、数がずれない)。"""

    def __init__(self, limiter: "SseConnectionLimiter", client: str) -> None:
        self._limiter: SseConnectionLimiter | None = limiter
        self._client = client

    def release(self) -> None:
        limiter, self._limiter = self._limiter, None
        if limiter is not None:
            limiter._release(self._client)


class SseConnectionLimiter:
    """SSE の同時につなげておく本数の上限(全体と、クライアント IP ごと)。メモリの中だけで数える(web は 1 インスタンス。台帳 C-65)。

    acquire で席を取り(上限なら SseLimitReached。何も増やさない)、SseSlot.release で戻す。数えるのも戻すのも、await をはさまない
    (1 つのイベントループの中で、途中で割り込まれない)。クライアントごとの本数が 0 になれば、その IP の記録は消す(覚えているのは、つないでいる IP だけ)。
    """

    def __init__(self, config: SseLimitConfig = DEFAULT_SSE_LIMIT_CONFIG) -> None:
        self._config = config
        self._open = 0
        self._open_by_client: dict[str, int] = {}

    def __len__(self) -> int:
        """いまつないである本数(全体)。"""
        return self._open

    def open_for(self, client: str) -> int:
        """client がいまつないでいる本数。"""
        return self._open_by_client.get(client, 0)

    def acquire(self, client: str) -> SseSlot:
        """client の席を 1 つ取る。クライアントごとの上限(scope=client)を先に見て、次に全体(scope=overall)。超えていれば SseLimitReached。"""
        config = self._config
        if self.open_for(client) >= config.max_connections_per_client:
            raise SseLimitReached("client", limit=config.max_connections_per_client)
        if self._open >= config.max_connections:
            raise SseLimitReached("overall", limit=config.max_connections)
        self._open += 1
        self._open_by_client[client] = self.open_for(client) + 1
        return SseSlot(self, client)

    def acquire_for_request(self, request: Request) -> SseSlot:
        """request を送ってきたクライアント(web.client_ip.client_key。IPv6 は /64 にまとめる。台帳 C-71)の席を取る。上限なら 429(本文は rate_limited_error の形。入口は sse、Retry-After は 2 秒)。"""
        try:
            return self.acquire(client_key(request))
        except SseLimitReached as exc:
            _log.info("sse connection limit reached scope=%s", exc.scope)
            raise rate_limited_error(
                SSE_ENTRANCE, exc.scope, limit=exc.limit, window_seconds=None, retry_after_seconds=SSE_RETRY_AFTER_SECONDS
            ) from None

    def _release(self, client: str) -> None:
        self._open -= 1
        remaining = self.open_for(client) - 1
        if remaining > 0:
            self._open_by_client[client] = remaining
        else:
            self._open_by_client.pop(client, None)


# ----------------------------------------------------------------------
# ログインなしの読み取りの枠(台帳 C-68)
# ----------------------------------------------------------------------

# 429 の本文の入口の名前。ENTRANCES には入れない(Firestore の時間窓カウンタではなく、メモリの中の窓。全体の枠にも数えない)。
ANONYMOUS_READ_ENTRANCE = "anonymous_read"
# 窓の長さ(秒)。設計は「1 分」。上限の回数だけを設定ファイルに置く。
ANONYMOUS_READ_WINDOW_SECONDS = 60


class AnonymousReadLimitReached(Exception):
    """ログインなしの読み取りの枠に達している。limit は、その上限の回数(窓あたり)。retry_after_seconds は、窓の終わりまでの秒数(1 以上)。"""

    def __init__(self, *, limit: int, retry_after_seconds: int) -> None:
        super().__init__("anonymous read limit reached")
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


class AnonymousReadLimiter:
    """ログインなしで金庫を読む GET の、クライアントごとの回数の枠(固定の 1 分の窓。メモリの中だけ。台帳 C-68)。

    web は 1 インスタンスなので、メモリで足りる(Firestore に書くと、読み取りのたびに書き込みの課金が増える)。窓は UNIX 時刻を窓の長さで割った商が同じ間
    (境目の前後で短い間に最大 2 倍通ることは、固定の窓の性質として受け入れる。Firestore の時間窓カウンタと同じ)。断った要求は数えない。
    窓が変わったら、前の窓の数を全部捨てる(覚えているのは、いまの窓で読んだクライアントだけ。クライアントごとの記録が、増え続けない)。
    数えるのに await をはさまない(1 つのイベントループの中で、途中で割り込まれない)。時刻は注入できる時計から取る。
    """

    def __init__(self, clock: Clock, config: AnonymousReadLimitConfig = DEFAULT_ANONYMOUS_READ_LIMIT_CONFIG) -> None:
        self._clock = clock
        self._config = config
        self._window: int | None = None
        self._counts: dict[str, int] = {}

    def __len__(self) -> int:
        """いまの窓で数えているクライアントの数。"""
        return len(self._counts)

    def admit(self, client: str) -> None:
        """client の読み取りを 1 回数える。上限に達していれば AnonymousReadLimitReached(何も数えない)。"""
        now_seconds = self._clock.now().timestamp()
        window = int(now_seconds) // ANONYMOUS_READ_WINDOW_SECONDS
        if window != self._window:
            self._window, self._counts = window, {}
        count = self._counts.get(client, 0)
        if count >= self._config.per_minute:
            window_end = (window + 1) * ANONYMOUS_READ_WINDOW_SECONDS
            raise AnonymousReadLimitReached(
                limit=self._config.per_minute, retry_after_seconds=max(1, math.ceil(window_end - now_seconds))
            )
        self._counts[client] = count + 1


class AnonymousReadLimitMiddleware:
    """ログインなしで金庫を読む GET に、クライアントごとの枠(AnonymousReadLimiter)を掛ける、純粋な ASGI ミドルウェア(台帳 C-68)。

    prefixes のどれかで始まる経路への GET だけを、クライアント(web.client_ip.client_key。IPv6 は /64 にまとめる。台帳 C-71)ごとに数える。
    超えたら、ルートに渡さずに 429(本文は {"detail": {"code": "rate_limited", "entrance": "anonymous_read", "scope": "client", "limit",
    "window_seconds": 60, "retry_after_seconds"}}、Retry-After つき)。ほかの要求は、そのまま通す。
    経路の前置きで選ぶので、ルートごとの依存を付け忘れない(同じルーターに、セッションの要る経路と要らない経路が混ざっていても、前置きで分けられる)。
    """

    def __init__(self, app: ASGIApp, *, limiter: AnonymousReadLimiter, prefixes: tuple[str, ...]) -> None:
        self.app = app
        self._limiter = limiter
        self._prefixes = prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"].startswith(self._prefixes):
            try:
                self._limiter.admit(client_key(Request(scope)))
            except AnonymousReadLimitReached as reached:
                refused = rate_limited_error(
                    ANONYMOUS_READ_ENTRANCE,
                    "client",
                    limit=reached.limit,
                    window_seconds=ANONYMOUS_READ_WINDOW_SECONDS,
                    retry_after_seconds=reached.retry_after_seconds,
                )
                await JSONResponse({"detail": refused.detail}, status_code=refused.status_code, headers=refused.headers)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
