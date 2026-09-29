"""交渉の見回り(design.md §4.1・§3.4・§6.2)。起動時と、暫定 60 秒ごとに行う。

金庫の一覧(open=true)を読み、交渉ごとに次を行う。どれも冪等なので、同じ交渉を何度拾っても安全。
- 段階開示の状態 stages/{nid} がなければ作る(§6.2)。
- 期限・寿命・最長の停止時間を過ぎていれば、expire を呼ぶ(§3.4)。誰も操作しない交渉も、
  期限か寿命で必ず終わる。
- タスクがなければ、レフェリーのタスクを作り直す(同じ手番から続く。金庫が状態の正本なので)。

一覧の 1 件の処理が失敗しても、ほかの交渉の見回りは続ける(次の見回りでやり直す)。1 件の中でも、
3 つの処理は互いに独立に失敗を扱う(段階開示の状態を作れなくても、期限切れとタスクの作り直しは行う)。

依頼者の見回り(30 日使われていない依頼者の削除。§4.1・§6.3)は、別の見回り(web.principal_sweeper)。

本物の候補者の交渉の stages/{nid} の作成は、その依頼者のロックの下で行う(locks を渡したとき。台帳 I-4)。
一覧を読んだ後に本人の削除が済むと、古い一覧から段の状態を作り直して、削除した依頼者の文書が
残ってしまう。そのため、ロックを取った後に、金庫にその交渉が(削除中でなく)残っていることを確かめる。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from vault.api_models import OpenNegotiationSummary
from vault.clock import Clock, SystemClock

from web.config import DEFAULT_WEB_CONFIG, SweeperConfig
from web.locks import PrincipalLocks
from web.referee import NegotiationContext, RefereeManager, Sleep
from web.stages import StageStore
from web.vault_client import VaultClient, VaultConflictError, VaultNotFoundError

_log = logging.getLogger(__name__)


@dataclass
class SweepReport:
    """見回り 1 回の結果(テスト・ログ用)。値は数だけ。"""

    listed: int = 0
    stages_created: int = 0
    expire_calls: int = 0
    expired: int = 0
    tasks_started: int = 0
    errors: int = 0


class Sweeper:
    """交渉の見回り。sweep_once() が 1 回ぶん、run() が起動時と一定間隔ごとの繰り返し。"""

    def __init__(
        self,
        *,
        vault: VaultClient,
        stages: StageStore,
        referees: RefereeManager,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
        config: SweeperConfig = DEFAULT_WEB_CONFIG.sweeper,
        locks: PrincipalLocks | None = None,
    ) -> None:
        self._vault = vault
        self._stages = stages
        self._referees = referees
        self._clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._config = config
        self._locks = locks

    async def run(self) -> None:
        """起動時に 1 回、その後は interval_seconds ごとに見回る。止めるにはタスクを cancel する。"""
        while True:
            try:
                await self.sweep_once()
            except Exception as exc:
                # 一覧を読めないなど。見回りのループは止めず、次の見回りでやり直す。
                _log.error("sweep failed error=%s", type(exc).__name__)
            await self._sleep(self._config.interval_seconds)

    async def sweep_once(self) -> SweepReport:
        """金庫の一覧を 1 回読み、交渉ごとに stages・expire・タスクを整える。"""
        report = SweepReport()
        items = await self._vault.list_open_negotiations()
        report.listed = len(items)
        for item in items:
            await self._sweep_item(item, report)
        return report

    async def _sweep_item(self, item: OpenNegotiationSummary, report: SweepReport) -> None:
        # 段階開示の状態を先に整える: 判定の後に web が落ちても失われないようにするため(§6.2)。
        await self._attempt(report, item, self._ensure_stage)
        expired = await self._attempt(report, item, self._expire_if_due)
        if not expired:  # 終わった交渉には、タスクを作らない
            await self._attempt(report, item, self._ensure_task)

    async def _attempt(
        self,
        report: SweepReport,
        item: OpenNegotiationSummary,
        action: Callable[[OpenNegotiationSummary, SweepReport], Awaitable[bool]],
    ) -> bool:
        """action を行う。失敗は数えて記録するだけで、ほかの処理・ほかの交渉は続ける。"""
        try:
            return await action(item, report)
        except Exception as exc:
            report.errors += 1
            _log.error(
                "sweep step failed nid=%s step=%s error=%s", item.nid, action.__name__, type(exc).__name__
            )
            return False

    async def _ensure_stage(self, item: OpenNegotiationSummary, report: SweepReport) -> bool:
        principal_id = item.candidate_principal_id
        if principal_id is None or self._locks is None:
            created = await self._stages.ensure(item.nid, principal_id)
        else:
            async with self._locks.lock(principal_id):
                if not await self._negotiation_remains(item.nid):
                    return False
                created = await self._stages.ensure(item.nid, principal_id)
        if created:
            report.stages_created += 1
        return created

    async def _negotiation_remains(self, nid: str) -> bool:
        """金庫にその交渉が(依頼者が削除中でなく)残っているか。ロックを持ったまま確かめる(台帳 I-4)。

        見つからない(404。削除の流れが消した)・依頼者が削除中(409)なら False: 一覧を読んだ後に
        削除が進んだので、段の状態は作らない。金庫が応えないときは、例外のまま伝える(次の見回りでやり直す)。
        """
        try:
            await self._vault.get_view(nid, "candidate")
        except (VaultNotFoundError, VaultConflictError):
            return False
        return True

    async def _expire_if_due(self, item: OpenNegotiationSummary, report: SweepReport) -> bool:
        """期限を過ぎていれば expire を呼ぶ。終了処理(timeout)をしたら True。"""
        if not self._should_call_expire(item):
            return False
        report.expire_calls += 1
        expired = (await self._vault.expire(item.nid)).expired
        if expired:
            report.expired += 1
        return expired

    async def _ensure_task(self, item: OpenNegotiationSummary, report: SweepReport) -> bool:
        context = NegotiationContext(
            nid=item.nid, mode=item.mode, candidate_principal_id=item.candidate_principal_id
        )
        started = self._referees.start(context)
        if started:
            report.tasks_started += 1
        return started

    def _should_call_expire(self, item: OpenNegotiationSummary) -> bool:
        """期限・寿命を過ぎていれば expire を呼ぶ(§3.4)。判断の最終は金庫の expire が行う(冪等)。

        一覧には一時停止した時刻(paused_at)がないので、最長の停止時間(暫定 24 時間)を過ぎたかは
        ここでは分からない。一時停止中の交渉は、毎回 expire を呼んで金庫に判断させる。
        """
        now = self._clock.now()
        if item.paused:
            return True
        if now >= item.expires_at:
            return True
        return item.deadline is not None and now >= item.deadline
