"""依頼者の見回り(design.md §4.1・§6.3。P-6)。暫定 10 分ごと(起動時にも 1 回)。

交渉の見回り(web.sweeper)とは別に、web の principals_meta を直接調べる。30 日使っていない依頼者は
進行中の交渉を持たないので、金庫の交渉の一覧からは拾えないため。
- delete_after を過ぎていて、削除中でない依頼者: ロックを取り、delete_after が読んだ値から変わって
  いないことをトランザクションで確かめてから、deletion_state=deleting にして、削除の流れを進める。
- deletion_state=deleting のまま残っている依頼者(本人のボタンや前回の見回りで、途中で失敗した):
  削除の流れを最初からやり直す(各段は冪等)。

1 人の処理が失敗しても、ほかの依頼者の処理は続ける(削除中の印が残るので、次の見回りでやり直す)。
時刻と待ち時間は注入できる(clock・sleep)ので、テストは sleep せずに 1 回ずつ見回れる(sweep_once)。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from vault.clock import Clock, SystemClock

from web.config import DEFAULT_WEB_CONFIG, PrincipalSweeperConfig
from web.deletion import DeletionOutcome, PrincipalDeletion
from web.principals_meta import PrincipalsMetaStore
from web.referee import Sleep

_log = logging.getLogger(__name__)


@dataclass
class PrincipalSweepReport:
    """見回り 1 回の結果(テスト・ログ用)。値は数だけ。"""

    due: int = 0  # delete_after を過ぎていた依頼者
    deleting: int = 0  # 削除中のまま残っていた依頼者
    completed: int = 0  # 削除の流れが最後まで進んだ
    incomplete: int = 0  # どこかの段で失敗した(次の見回りでやり直す)
    skipped: int = 0  # 対象にしなかった(delete_after が延びた・すでに消えた)
    errors: int = 0  # 印を立てる・確かめる段で失敗した


class PrincipalSweeper:
    """依頼者の見回り。sweep_once() が 1 回ぶん、run() が起動時と一定間隔ごとの繰り返し。"""

    def __init__(
        self,
        *,
        meta: PrincipalsMetaStore,
        deletion: PrincipalDeletion,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
        config: PrincipalSweeperConfig = DEFAULT_WEB_CONFIG.principal_sweeper,
    ) -> None:
        self._meta = meta
        self._deletion = deletion
        self._clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._config = config

    async def run(self) -> None:
        """起動時に 1 回、その後は interval_seconds ごとに見回る。止めるにはタスクを cancel する。"""
        while True:
            try:
                await self.sweep_once()
            except Exception as exc:
                # 一覧を読めないなど。見回りのループは止めず、次の見回りでやり直す。
                _log.error("principal sweep failed error=%s", type(exc).__name__)
            await self._sleep(self._config.interval_seconds)

    async def sweep_once(self) -> PrincipalSweepReport:
        """delete_after を過ぎた依頼者と、削除中のまま残っている依頼者を、1 回ずつ処理する。"""
        report = PrincipalSweepReport()
        # 2 つの一覧を先に読む。この見回りで削除の流れが止まった依頼者は、次の見回りでやり直す
        # (同じ見回りの中で、失敗した直後に何度もやり直さない)。
        due = await self._meta.list_due(self._clock.now())
        deleting = await self._meta.list_deleting()
        report.due = len(due)
        report.deleting = len(deleting)

        for entry in due:
            await self._process(report, partial(self._deletion.delete_expired, entry.principal_id, entry.delete_after))
        for principal_id in deleting:
            await self._process(report, partial(self._deletion.resume, principal_id))
        return report

    async def _process(self, report: PrincipalSweepReport, call: Callable[[], Awaitable[DeletionOutcome]]) -> None:
        """1 人ぶんの処理。失敗は数えて記録するだけで、ほかの依頼者の処理は続ける。"""
        try:
            outcome = await call()
        except Exception as exc:
            report.errors += 1
            _log.error("principal sweep step failed error=%s", type(exc).__name__)
            return
        if outcome is DeletionOutcome.COMPLETED:
            report.completed += 1
        elif outcome is DeletionOutcome.INCOMPLETE:
            report.incomplete += 1
        else:
            report.skipped += 1
