"""交渉の見回り(design.md §4.1・§3.4・§6.2)。起動時と、暫定 60 秒ごとに行う。

金庫の一覧(open=true)を読み、交渉ごとに次を行う。どれも冪等なので、同じ交渉を何度拾っても安全。
- 段階開示の状態 stages/{nid} がなければ作る(§6.2)。
- 期限・寿命・最長の停止時間を過ぎていれば、expire を呼ぶ(§3.4)。誰も操作しない交渉も、
  期限か寿命で必ず終わる。
- タスクがなければ、レフェリーのタスクを作り直す(同じ手番から続く。金庫が状態の正本なので)。

進行中の一覧のあとに、判定が済んだのに段の決着処理(判定の検出・架空人物の自動応答・台帳。web.stages.StageSettler)がまだの段を拾う(台帳 X-84)。
GET が段の状態を書かなくなった(状態を変えるのは POST に限る。§6.3)ので、決着処理はレフェリーの完了のフックと、ここで行う。フックが失敗した・
判定の直後に web が落ちた交渉(終わっているので、金庫の進行中の一覧には現れない)も、ここで決着する。拾う対象は、段の状態の settled_at が null
(決着の印がない)で、金庫の進行中の一覧にないもの。判定が出ていなければ(一覧を読んだ後に作られた交渉など)、StageSettler が何もしない。

面談の途中の状態(web.interview.state。サーバのメモリ)のうち、最後に使ってから寿命(state_idle_ttl_seconds)を過ぎたものを、メモリから消す(台帳 C-66・L19-6)。
新しい面談が始まらなければ、放置された面談の生の年収や取り出した発言が、再起動までメモリに残ってしまうため。メモリの中だけの掃除なので、
金庫の一覧を読む前に行う(金庫が応えなくても消す)。evict_idle を渡していなければ(面談を持たない組み立て)、何もしない。

一覧の 1 件の処理が失敗しても、ほかの交渉の見回りは続ける(次の見回りでやり直す)。1 件の中でも、
3 つの処理は互いに独立に失敗を扱う(段階開示の状態を作れなくても、期限切れとタスクの作り直しは行う)。決着処理の失敗も、1 件ごとに数えて続ける。

最初の 1 回の見回りが終わるまで(first_sweep_done が False の間)は、新しい交渉の作成を受け付けない(503。§8.2・台帳 X-53)。
進行中の交渉の一覧(レフェリーのタスクの一覧)が、一覧を読み終えて、タスクを作り直すまでは、作成の入場の制限が、進行中の交渉の
未消化分を少なく数えるため。

依頼者の見回り(30 日使われていない依頼者の削除。§4.1・§6.3)は、別の見回り(web.principal_sweeper)。

本物の候補者の交渉の stages/{nid} の作成は、その依頼者のロックの下で行う(locks と meta を渡したとき。台帳 I-4)。
一覧を読んだ後に本人の削除が済むと、古い一覧から段の状態を作り直して、削除した依頼者の文書が
残ってしまう。そのため、ロックを取った後に、web の利用記録 principals_meta を見て、依頼者が(削除中でも
削除済みでもなく)使える状態であることを確かめる(台帳 C-41)。金庫の交渉を見て確かめる形では、相手が本物の
交渉(削除した側の見え方だけを消して、交渉の文書は残る。§3.8 の手順 3)で、「ある」と読んでしまうため。
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from vault.api_models import OpenNegotiationSummary
from vault.clock import Clock, SystemClock

from web.config import DEFAULT_WEB_CONFIG, SweeperConfig
from web.locks import PrincipalLocks
from web.principals_meta import DELETION_ACTIVE, PrincipalsMetaStore
from web.referee import NegotiationContext, RefereeManager, Sleep
from web.stages import StageStore
from web.vault_client import VaultClient

_log = logging.getLogger(__name__)

# 段の決着処理を行う口(web.stages.StageSettler.settle): 交渉 ID と候補者の依頼者 ID(架空の候補者なら None)を受け取り、行ったら True。
SettleStage = Callable[[str, str | None], Awaitable[bool]]

# アイドルの面談の状態をメモリから消す口(web.interview.state.InterviewStateStore.evict_idle): いまの時刻を受け取り、消した数を返す。
EvictIdleInterviews = Callable[[dt.datetime], int]


@dataclass
class SweepReport:
    """見回り 1 回の結果(テスト・ログ用)。値は数だけ。"""

    listed: int = 0
    stages_created: int = 0
    expire_calls: int = 0
    expired: int = 0
    tasks_started: int = 0
    stages_settled: int = 0  # 判定が済んで、決着処理を行った段(台帳 X-84)
    interview_states_evicted: int = 0  # メモリから消した、アイドルの面談の状態(台帳 C-66・L19-6)
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
        meta: PrincipalsMetaStore | None = None,
        settle: SettleStage | None = None,
        evict_idle: EvictIdleInterviews | None = None,
    ) -> None:
        if (locks is None) != (meta is None):
            # 片方だけでは、本物の候補者の段の状態を、確かめずに作る(または、ロックなしで確かめる)ことになる。
            raise ValueError("locks and meta must be given together")
        self._vault = vault
        self._stages = stages
        self._referees = referees
        self._clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._config = config
        self._locks = locks
        self._meta = meta
        self._settle = settle
        self._evict_idle = evict_idle
        self._first_sweep_done = False

    @property
    def first_sweep_done(self) -> bool:
        """起動してから、見回りが 1 回終わったか(金庫の一覧を読み、交渉ごとの処理を済ませた。作成の入場の制限が使う)。"""
        return self._first_sweep_done

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
        """金庫の一覧を 1 回読み、交渉ごとに stages・expire・タスクを整える。先に、アイドルの面談の状態をメモリから消す(金庫が応えなくても行う)。"""
        report = SweepReport()
        self._evict_idle_interviews(report)
        items = await self._vault.list_open_negotiations()
        report.listed = len(items)
        for item in items:
            await self._sweep_item(item, report)
        self._first_sweep_done = True  # 進行中の交渉の処理(タスクの作り直し)は済んだ。決着処理の数に、新しい交渉の作成を待たせない
        await self._settle_ended(items, report)
        return report

    def _evict_idle_interviews(self, report: SweepReport) -> None:
        """アイドルの面談の状態を、メモリから消す(台帳 C-66・L19-6)。失敗は数えて記録するだけで、見回りは続ける。"""
        if self._evict_idle is None:
            return
        try:
            report.interview_states_evicted = self._evict_idle(self._clock.now())
        except Exception as exc:
            report.errors += 1
            _log.error("sweep step failed step=evict_idle_interviews error=%s", type(exc).__name__)

    async def _settle_ended(self, open_items: list[OpenNegotiationSummary], report: SweepReport) -> None:
        """判定が済んで、段の決着処理がまだの段を拾う(台帳 X-84)。settle を渡していなければ(段階開示の部品を持たない組み立て)、何もしない。

        拾うのは、決着の印(settled_at)がなく、金庫の進行中の一覧にない段。進行中の交渉は、レフェリーの完了のフックが決着させる。
        1 件の失敗は数えて記録するだけで、ほかの段は続ける(次の見回りでやり直す)。
        """
        if self._settle is None:
            return
        try:
            unsettled = await self._stages.list_unsettled()
        except Exception as exc:
            report.errors += 1
            _log.error("sweep step failed step=list_unsettled error=%s", type(exc).__name__)
            return
        open_nids = {item.nid for item in open_items}
        for stage in unsettled:
            if stage.nid in open_nids:
                continue
            try:
                if await self._settle(stage.nid, stage.candidate_principal_id):
                    report.stages_settled += 1
            except Exception as exc:
                report.errors += 1
                _log.error("sweep step failed step=settle_stage error=%s", type(exc).__name__)

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
            _log.error("sweep step failed step=%s error=%s", action.__name__, type(exc).__name__)
            return False

    async def _ensure_stage(self, item: OpenNegotiationSummary, report: SweepReport) -> bool:
        principal_id = item.candidate_principal_id
        if principal_id is None or self._locks is None:
            created = await self._stages.ensure(item.nid, principal_id)
        else:
            async with self._locks.lock(principal_id):
                if not await self._principal_is_active(principal_id):
                    return False
                created = await self._stages.ensure(item.nid, principal_id)
        if created:
            report.stages_created += 1
        return created

    async def _principal_is_active(self, principal_id: str) -> bool:
        """依頼者が、利用記録 principals_meta の上で使える状態か。ロックを持ったまま確かめる(台帳 I-4・C-41)。

        利用記録がない(削除済み。面談を送っていない依頼者は、金庫に交渉を持てない)・削除中
        (deletion_state=deleting)なら False: 一覧を読んだ後に削除の流れが進んだので、段の状態は作らない
        (削除の流れが依頼者 ID で段の状態を消すので、作り直すと残ってしまう)。削除の流れは、印を立てる前に
        同じロックを取って最後まで持つので、ここで見える状態は、途中ではなく、前の流れが済んだ後のもの。
        利用記録を読めないときは、例外のまま伝える(次の見回りでやり直す)。
        """
        assert self._meta is not None  # locks と一緒に渡される(__init__ で確かめている)
        meta = await self._meta.get(principal_id)
        return meta is not None and meta.deletion_state == DELETION_ACTIVE

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
