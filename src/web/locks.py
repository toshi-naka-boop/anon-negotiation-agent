"""依頼者ごとのロック(design.md §6.3・台帳 I-4)。

本人の削除と、途中にある同じ依頼者の書き込み(policy・blocklist の PUT など)が競合すると、金庫に依頼者の
文書が作り直されて残る(墓標がないので、金庫は「新しい依頼者」と区別できない)。これを避けるため、
同じ依頼者の操作と削除の流れを、1 つずつ順に処理する。

web は 1 インスタンス(§1.1)なので、プロセスの中の asyncio のロックで足りる。ロックは再入できない
(同じ依頼者のロックを持ったまま、もう一度取ると止まる)。デッドロックを作らないため、次を守る。
- 1 回の操作の中で取るのは、1 人の依頼者のロックだけ(別の依頼者のロックを入れ子にしない)。
- ロックを持ったまま、別のタスクの完了を待たない(レフェリーのタスクなど)。
- レフェリーの金庫への操作は、1 回の呼び出しごとにロックを取り、LLM の呼び出しの間は持たない
  (PrincipalScopedVault)。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from negotiation_core import Side

from vault.api_models import (
    EventViewItem,
    MoveRequest,
    MoveResponse,
    NegotiationViewResponse,
    PrincipalAnswerRequest,
    PrincipalAnswerResponse,
)

from web.vault_client import VaultClient


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0  # ロックを持っている数と、待っている数の合計


class PrincipalLocks:
    """依頼者 ID ごとの asyncio のロック。取った順(FIFO)に 1 つずつ進める。

    誰も持っても待ってもいない依頼者のロックは、覚えておかない(長く動くプロセスで、
    依頼者の数だけ増え続けないように)。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    @asynccontextmanager
    async def _hold(self, principal_id: str) -> AsyncIterator[None]:
        entry = self._entries.get(principal_id)
        if entry is None:
            entry = self._entries[principal_id] = _Entry()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._entries.get(principal_id) is entry:
                del self._entries[principal_id]

    def lock(self, principal_id: str) -> AbstractAsyncContextManager[None]:
        """`async with locks.lock(pid):` で、pid のロックを取る(待っている間にキャンセルされてもよい)。"""
        return self._hold(principal_id)


class PrincipalScopedVault:
    """本物の候補者の交渉についての、レフェリーの金庫への操作を、1 回ごとにその依頼者のロックの下で行う。

    台帳 I-4(1d-1 の申し送り): レフェリーの操作も、本人の削除と同じ種類の競合を起こし得るので、
    同じロックの対象にする。ロックを持つのは金庫の 1 回の呼び出しの間だけで、エージェント(LLM)を
    呼んでいる間は持たない(持つと、本人の操作と削除を長く止めてしまうため)。
    """

    def __init__(self, vault: VaultClient, locks: PrincipalLocks, principal_id: str) -> None:
        self._vault = vault
        self._locks = locks
        self._principal_id = principal_id

    async def get_view(self, nid: str, side: Side) -> NegotiationViewResponse:
        async with self._locks.lock(self._principal_id):
            return await self._vault.get_view(nid, side)

    async def get_events(self, nid: str, side: Side, after_seq: int = 0) -> list[EventViewItem]:
        async with self._locks.lock(self._principal_id):
            return await self._vault.get_events(nid, side, after_seq)

    async def post_move(self, nid: str, request: MoveRequest) -> MoveResponse:
        async with self._locks.lock(self._principal_id):
            return await self._vault.post_move(nid, request)

    async def post_principal_answer(
        self, nid: str, request: PrincipalAnswerRequest
    ) -> PrincipalAnswerResponse:
        async with self._locks.lock(self._principal_id):
            return await self._vault.post_principal_answer(nid, request)
