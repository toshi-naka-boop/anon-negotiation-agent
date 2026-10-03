"""削除の流れ(design.md §6.3 の web 側。金庫側は §3.8)。

本人の「データを消す」ボタンと、30 日の自動削除(依頼者の見回り。web.principal_sweeper)の両方が、
この同じ流れを使う。
  1. principals_meta の deletion_state を deleting にする(削除中の印)。以後、その依頼者の操作は
     すべて拒否する(web.session_middleware)。
  1′. 面談の途中状態(web のメモリ。web.interview.state)を消す。メモリを消すだけなので失敗しない。冪等(なければ何もしない)。
     流れの最初(_run)で行うので、自動削除も、途中から続ける見回りも、同じように消す。
  2. 金庫の削除を呼ぶ(冪等。すでに消えていても成功)。
  3. web 側で、開示台帳(principals/{pid}/ledger)と、本人が当事者の段の状態(段 1 の職務要約を含む)を
     消す。段の状態は、stages/{nid} に持たせた候補者の依頼者 ID で引く。
  4. 最後に principals_meta を消す。本人のボタンからのときは、あわせてクッキーも消す(呼び出し側)。
途中で失敗しても、削除中の印が残るので、依頼者の見回りが最初からやり直して最後まで進める(各段は冪等)。
本人が押し直す必要はない。印(principals_meta)は最後に消すので、印だけが先に消えてデータが残ることはない。

同じ依頼者の操作と削除の流れは、1 つずつ順に処理する(台帳 I-4。web.locks)。削除の流れは、印を立てる前に
ロックを取り、最後まで持つ。本人のボタンは、リクエストを受けたミドルウェアがすでにロックを持っている
ので、delete_by_user は取らない(ロックは再入できない)。自動削除(delete_expired・resume)は、
自分でロックを取る。

利用記録がなければ(面談を送っていなければ)、サーバに永続のデータはないので、金庫にも (default) にも触れずに終える
(クッキーを消すのは呼び出し側)。ただし面談の途中状態は、送信の前(利用記録を作る前)にもメモリにあるので、1′ だけは利用記録がなくても行う。

ログには、止まった段の名前と例外の型名だけを書く(依頼者 ID・入力は書かない)。
"""

import datetime as dt
import enum
import logging
from collections.abc import Awaitable, Callable

from web.interview.state import InterviewStateStore
from web.ledger import DisclosureLedger
from web.locks import PrincipalLocks
from web.principals_meta import DELETION_DELETING, PrincipalsMetaStore
from web.stages import StageStore
from web.vault_client import VaultClient

_log = logging.getLogger(__name__)


class DeletionOutcome(enum.Enum):
    NO_RECORD = "no_record"  # 利用記録がない(面談を送っていない)。サーバに永続のデータはない(面談の途中状態〔メモリ〕は消した)
    COMPLETED = "completed"  # 最後の段(利用記録の削除)まで終わった
    INCOMPLETE = "incomplete"  # どこかの段で失敗した。削除中の印が残るので、依頼者の見回りが続ける
    SKIPPED = "skipped"  # 見回りが対象にしなかった(delete_after が延びた・すでに消えた・削除中でない)


class PrincipalDeletion:
    """削除の流れ。金庫・利用記録・段の状態・開示台帳・面談の途中状態・ロックを、外から渡す。"""

    def __init__(
        self,
        *,
        vault: VaultClient,
        meta: PrincipalsMetaStore,
        stages: StageStore,
        ledger: DisclosureLedger,
        interview_states: InterviewStateStore,
        locks: PrincipalLocks,
    ) -> None:
        self._vault = vault
        self._meta = meta
        self._stages = stages
        self._ledger = ledger
        self._interview_states = interview_states
        self._locks = locks

    # ------------------------------------------------------------------
    # 本人の「データを消す」(呼び出し側が、その依頼者のロックを持っていること)
    # ------------------------------------------------------------------

    async def delete_by_user(self, principal_id: str) -> DeletionOutcome:
        """§6.3 の 1〜4。利用記録がなければ NO_RECORD(面談の途中状態〔メモリ〕を消して、クッキーを消すだけ)。"""
        marked = await self._meta.mark_deleting(principal_id)  # 1
        if marked == "absent":
            self._interview_states.discard(principal_id)  # 1′(面談を送る前は利用記録がないが、途中の状態はメモリにある)
            return DeletionOutcome.NO_RECORD
        return await self._run(principal_id)

    # ------------------------------------------------------------------
    # 依頼者の見回り(自分でロックを取る)
    # ------------------------------------------------------------------

    async def delete_expired(self, principal_id: str, expected_delete_after: dt.datetime) -> DeletionOutcome:
        """delete_after を過ぎた依頼者を、削除中にして削除の流れを進める(§4.1)。

        ロックを取ってから、delete_after が読んだ値から変わっていないことをトランザクションで確かめて
        印を立てる。変わっていれば(見回りが読んだ後に、本人が使って延びた)、削除中にしない。
        """
        async with self._locks.lock(principal_id):
            marked = await self._meta.mark_deleting(principal_id, expected_delete_after=expected_delete_after)
            if marked in ("changed", "absent"):
                return DeletionOutcome.SKIPPED
            return await self._run(principal_id)

    async def resume(self, principal_id: str) -> DeletionOutcome:
        """削除中のまま残っている依頼者の、削除の流れを最初からやり直す(各段は冪等。§4.1)。"""
        async with self._locks.lock(principal_id):
            meta = await self._meta.get(principal_id)
            if meta is None or meta.deletion_state != DELETION_DELETING:
                return DeletionOutcome.SKIPPED
            return await self._run(principal_id)

    # ------------------------------------------------------------------
    # 1′〜4(印は立て済み。ロックは呼び出し側が持っている)
    # ------------------------------------------------------------------

    async def _run(self, principal_id: str) -> DeletionOutcome:
        self._interview_states.discard(principal_id)  # 1′(メモリを消すだけ。失敗しない。金庫などの段が失敗しても、メモリはすでに消えている)
        steps: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
            ("vault", lambda: self._vault.delete_principal(principal_id)),  # 2
            ("ledger", lambda: self._ledger.delete_all(principal_id)),  # 3
            ("stages", lambda: self._stages.delete_for_principal(principal_id)),  # 3
            ("meta", lambda: self._meta.delete(principal_id)),  # 4(最後)
        )
        for name, action in steps:
            try:
                await action()
            except Exception as exc:
                _log.error("principal deletion stopped step=%s error=%s", name, type(exc).__name__)
                return DeletionOutcome.INCOMPLETE
        return DeletionOutcome.COMPLETED
