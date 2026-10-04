"""LLM に実際に送る回数(物理の呼び出し数)の計上と、作成の入場の制限(design.md §4.1・§8.2)。

費用の保証は物理の数で行う(台帳 X-46・X-47・X-50・X-56)。LLM に向けて送るすべての呼び出し(交渉エージェントの計画・決定、
面談、壁 1 の生メッセージ。レフェリーの再試行を含む)を、送る前に、`(default)` の 1 つのトランザクションで数える:
「上限に達していなければ 1 進める。達していれば進めずに断る」(台帳 L13-7)。429・5xx が返っても戻さない(処理済みの要求が途中で
5xx に化けることがあり、戻すと金額の上限が破れる。数えすぎる側にだけ倒す。台帳 X-56)。

- 1 日の数: `llm_call_counters/{日本時間の日付 YYYY-MM-DD}` の `count`。日付をキーにするので、日付が変わると 0 から数え直す。
  日付をまたいで動く交渉の呼び出しは、翌日の数に入る。文書には 7 日の TTL(`ttl_at`)を付ける。1 文書への書き込みは 1 秒に 1 回程度が
  目安なので、発表の日に上限を大きく上げるときは分散カウンタにする(台帳 L14-7。いまは 1 文書)。
- 交渉ごとの数: `stages/{nid}` の `llm_calls`(TTL と削除は段の状態と同じ。台帳 L13-5)。
- 面談・壁 1 の生メッセージは交渉を持たないので、`reserve(None)` で 1 日の数だけを数える(面談は web.interview.agent、壁 1 は web.attack.router の
  send_raw_message が呼ぶ。数える関数は共通)。

カウンタに書けないとき(Firestore の失敗、交渉の段の状態がまだない)は LlmBudgetUnavailable を投げる。呼び出し側は、送らずに待って
読み直す(閉じる側に倒す。台帳 X-50)。段の状態は、交渉の作成直後に作り、作り損ねても見回りが作る(§6.2)。

入場の制限(`admits_new_negotiation`)は保証ではない。読むだけで、予約の記録は持たない(二度押し・再送・金庫の拒否が何も消費しないように。
台帳 C-45・X-53)。「その日の物理の数 ＋ 進行中の交渉の未消化分(上限 − その交渉の物理の数)の合計 ＋ 新しい交渉 1 件ぶんの上限」が
1 日の枠を超えるなら断る。並行した作成が同時に判定を通ることはあるが、保証は物理の上限が受け持つ。

Firestore(同期クライアント)の呼び出しは別スレッドで行う(web.stages と同じ)。値は、ログにも例外にも書かない。
"""

import asyncio
import datetime as dt
import logging
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from google.api_core.exceptions import Aborted
from google.cloud import firestore

from vault.clock import Clock

from web.config import DEFAULT_WEB_CONFIG, LlmBudgetConfig
from web.stages import STAGES_COLLECTION

_log = logging.getLogger(__name__)

LLM_COUNTERS_COLLECTION = "llm_call_counters"
STAGE_COUNT_FIELD = "llm_calls"

# 1 日の区切りは日本時間の 0 時(§8.2)。
JST = dt.timezone(dt.timedelta(hours=9))

# トランザクションの再試行は「内側 5 回(google-cloud-firestore の max_attempts)× 外側 3 回」。1 日のカウンタは、全交渉が書く 1 つの
# 文書なので、競合しやすい。本番の Firestore は、内側の再試行で順番を保つ。エミュレータでは、同じ文書を読んだ多数の呼び出しが、一斉に
# 中止され、一斉にやり直して、また中止される。そのため、内側を使い切ったら、乱数の待ちを入れてから、新しいトランザクションでやり直す
# (vault.store と同じ考え方。台帳 I-5)。それでも書けなければ、LlmBudgetUnavailable(送らない側)。
_INNER_MAX_ATTEMPTS = 5
_OUTER_ATTEMPTS = 3
_OUTER_BACKOFF_SECONDS = (0.010, 0.200)


class _CorruptCounter(ValueError):
    """カウンタの文書はあるが、数の項目が欠けている・整数でない・負(台帳 X-62: 0 とみなして送らず、閉じる側に倒す)。"""


def _count_in(data: dict | None, field: str) -> int:
    """文書の数の項目を読む。文書がある以上、項目は 0 以上の整数でなければならない(bool も不可)。"""
    value = (data or {}).get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _CorruptCounter(field)
    return value


class LlmBudgetUnavailable(Exception):
    """物理の呼び出し数のカウンタに書けない・読めない。呼び出し側は、送らずに待って読み直す(台帳 X-50)。

    メッセージには、原因の例外の型名だけを入れる(値は含まない)。
    """


class _StageMissing(Exception):
    """交渉の段の状態 stages/{nid} がまだない(作成直後の作り損ね)。見回りが作るまで、送らない。"""


@dataclass(frozen=True)
class Reservation:
    """reserve の結果。granted=False なら、何も進めていない(refused_by が、どちらの上限か)。"""

    granted: bool
    refused_by: Literal["daily", "negotiation"] | None = None


def jst_date(now: dt.datetime) -> str:
    """now の、日本時間の日付(1 日のカウンタの文書 ID。例: 2026-10-03)。"""
    return now.astimezone(JST).strftime("%Y-%m-%d")


class LlmBudget:
    """物理の呼び出し数の計上。時刻は注入できる時計から取る。"""

    def __init__(
        self,
        db: firestore.Client,
        clock: Clock,
        config: LlmBudgetConfig = DEFAULT_WEB_CONFIG.llm_budget,
    ) -> None:
        self._db = db
        self._clock = clock
        self._config = config
        self._ttl = dt.timedelta(seconds=config.daily_counter_ttl_seconds)

    def _daily_ref(self, now: dt.datetime):
        return self._db.collection(LLM_COUNTERS_COLLECTION).document(jst_date(now))

    def _stage_ref(self, nid: str):
        return self._db.collection(STAGES_COLLECTION).document(nid)

    # ------------------------------------------------------------------
    # 送る前の計上(1 つのトランザクション)
    # ------------------------------------------------------------------

    def _reserve_sync(self, nid: str | None) -> Reservation:
        now = self._clock.now()
        daily_ref = self._daily_ref(now)
        stage_ref = self._stage_ref(nid) if nid is not None else None

        def txn_fn(txn: firestore.Transaction) -> Reservation:
            # Firestore のトランザクションは、読み出しをすべて終えてから書く。
            daily_snap = daily_ref.get(transaction=txn)
            daily_count = _count_in(daily_snap.to_dict(), "count") if daily_snap.exists else 0
            stage_count = 0
            if stage_ref is not None:
                stage_snap = stage_ref.get(transaction=txn)
                if not stage_snap.exists:
                    raise _StageMissing
                stage_count = _count_in(stage_snap.to_dict(), STAGE_COUNT_FIELD)

            if daily_count >= self._config.daily_limit:
                return Reservation(False, "daily")
            if stage_ref is not None and stage_count >= self._config.per_negotiation_limit:
                return Reservation(False, "negotiation")

            txn.set(daily_ref, {"count": daily_count + 1, "ttl_at": now + self._ttl})
            if stage_ref is not None:
                txn.update(stage_ref, {STAGE_COUNT_FIELD: stage_count + 1})
            return Reservation(True)

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

    async def reserve(self, nid: str | None = None) -> Reservation:
        """LLM に 1 回送る前に、1 日の数と(nid があれば)交渉ごとの数を、1 つのトランザクションで 1 進める。

        どちらかの上限に達していれば、何も進めずに granted=False を返す(送らない)。
        カウンタに書けないとき(Firestore の失敗・段の状態がない)は LlmBudgetUnavailable(送らない)。
        nid=None は、面談・壁 1 の生メッセージ(交渉を持たない呼び出し)で、1 日の数だけを数える。
        """
        try:
            return await asyncio.to_thread(self._reserve_sync, nid)
        except Exception as exc:
            _log.error("llm call counter unavailable error=%s", type(exc).__name__)
            raise LlmBudgetUnavailable(type(exc).__name__) from exc

    # ------------------------------------------------------------------
    # 読み出し(ログ・判定・入場の制限)
    # ------------------------------------------------------------------

    def _daily_count_sync(self) -> int:
        snap = self._daily_ref(self._clock.now()).get()
        return _count_in(snap.to_dict(), "count") if snap.exists else 0

    async def daily_count(self) -> int:
        """今日(日本時間)の物理の呼び出し数。"""
        try:
            return await asyncio.to_thread(self._daily_count_sync)
        except Exception as exc:
            raise LlmBudgetUnavailable(type(exc).__name__) from exc

    def _negotiation_counts_sync(self, nids: list[str]) -> dict[str, int]:
        snapshots = self._db.get_all([self._stage_ref(nid) for nid in nids]) if nids else []
        return {snap.reference.id: _count_in(snap.to_dict(), STAGE_COUNT_FIELD) for snap in snapshots if snap.exists}

    async def negotiation_counts(self, nids: Iterable[str]) -> dict[str, int]:
        """交渉ごとの物理の呼び出し数。段の状態がない交渉は含めない(呼び出し側は 0 と読む)。"""
        try:
            return await asyncio.to_thread(self._negotiation_counts_sync, list(nids))
        except Exception as exc:
            raise LlmBudgetUnavailable(type(exc).__name__) from exc

    async def negotiation_count(self, nid: str) -> int:
        """nid の物理の呼び出し数(段の状態がなければ 0)。"""
        return (await self.negotiation_counts([nid])).get(nid, 0)

    async def admits_new_negotiation(self, running_nids: Iterable[str]) -> bool:
        """新しい交渉(ライブ・デモ・攻撃)を 1 件受け付けてよいか(入場の制限。保証ではない。読むだけ)。

        その日の物理の数 ＋ 進行中の交渉の未消化分(上限 − その交渉の物理の数)の合計 ＋ 上限(新しい交渉 1 件ぶん)が、
        1 日の枠を超えるなら受け付けない。running_nids は、進行中の交渉(レフェリーのタスクの一覧)。
        数えられない(Firestore の失敗)ときは LlmBudgetUnavailable(閉じる側に倒す)。
        """
        nids = list(running_nids)
        limit = self._config.per_negotiation_limit
        daily = await self.daily_count()
        counts = await self.negotiation_counts(nids)
        unconsumed = sum(max(0, limit - counts.get(nid, 0)) for nid in nids)
        return daily + unconsumed + limit <= self._config.daily_limit
