"""依頼者の利用記録 principals_meta/{pid}(design.md §6.3・§5 の手順 9・§4.1)。`(default)` に持つ。

持つ項目は `last_active_at`・`delete_after`・`deletion_state` の 3 つだけ。
- 面談の送信(§5 の手順 9)で、金庫に初めて書く前に作る(create_if_absent)。開始ページを開いただけの
  訪問者やクローラーには作らない。先に作るので、金庫にデータがあって利用記録がない状態は生じない。
- 有効なクッキーを持つリクエスト(閲覧だけの GET を含む)があり、last_active_at から 1 時間以上たって
  いれば、1 つのトランザクションで deletion_state を確かめてから、last_active_at=今・
  delete_after=今+30日を書く(touch)。削除中なら書かずに拒否する。
- 依頼者の見回りは、delete_after を過ぎた依頼者を、delete_after が読んだ値から変わっていないことを
  トランザクションで確かめてから、削除中にする(mark_deleting)。
- 削除の流れの最後の段で消す(delete)。それより先には消えない(印だけが先に消えて、データが残ることを
  防ぐため)。

Firestore(同期クライアント)の呼び出しは別スレッドで行う(web.stages と同じ)。
"""

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Literal

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from vault.clock import Clock

from web.config import DEFAULT_WEB_CONFIG, PrincipalsConfig

PRINCIPALS_META_COLLECTION = "principals_meta"

DELETION_ACTIVE = "active"
DELETION_DELETING = "deleting"


@dataclass(frozen=True)
class PrincipalMeta:
    last_active_at: dt.datetime
    delete_after: dt.datetime
    deletion_state: str


@dataclass(frozen=True)
class TouchResult:
    """touch の結果。

    - absent: 利用記録がない(面談を送っていない。何も書かない)
    - deleting: 削除中(書かない。呼び出し側は、リクエストを拒否する)
    - unchanged: 1 時間たっていない(書かない)
    - updated: last_active_at と delete_after を書いた。written_at はその時刻(クッキーの延長に使う)
    """

    outcome: Literal["absent", "deleting", "unchanged", "updated"]
    written_at: dt.datetime | None = None


@dataclass(frozen=True)
class DueEntry:
    """delete_after を過ぎた依頼者(依頼者の見回りが読んだ値)。"""

    principal_id: str
    delete_after: dt.datetime


CreateResult = Literal["created", "exists", "deleting"]
MarkResult = Literal["marked", "already_deleting", "changed", "absent"]


def _meta_from(data: dict) -> PrincipalMeta:
    return PrincipalMeta(
        last_active_at=data["last_active_at"],
        delete_after=data["delete_after"],
        deletion_state=data["deletion_state"],
    )


class PrincipalsMetaStore:
    """principals_meta/{pid} の読み書き。時刻は注入できる時計から取る。"""

    def __init__(
        self,
        db: firestore.Client,
        clock: Clock,
        config: PrincipalsConfig = DEFAULT_WEB_CONFIG.principals,
    ) -> None:
        self._db = db
        self._clock = clock
        self._touch_interval = dt.timedelta(seconds=config.touch_interval_seconds)
        self._retention = dt.timedelta(seconds=config.retention_seconds)

    def _collection(self):
        return self._db.collection(PRINCIPALS_META_COLLECTION)

    def _run_transaction(self, txn_fn):
        return firestore.transactional(txn_fn)(self._db.transaction())

    # ------------------------------------------------------------------
    # 読み出し
    # ------------------------------------------------------------------

    def _get_sync(self, principal_id: str) -> PrincipalMeta | None:
        snap = self._collection().document(principal_id).get()
        return _meta_from(snap.to_dict()) if snap.exists else None

    async def get(self, principal_id: str) -> PrincipalMeta | None:
        return await asyncio.to_thread(self._get_sync, principal_id)

    # ------------------------------------------------------------------
    # 作成(面談の送信。§5 の手順 9)
    # ------------------------------------------------------------------

    def _create_if_absent_sync(self, principal_id: str) -> CreateResult:
        ref = self._collection().document(principal_id)

        def txn_fn(txn: firestore.Transaction) -> CreateResult:
            snap = ref.get(transaction=txn)
            if snap.exists:
                return "deleting" if snap.get("deletion_state") == DELETION_DELETING else "exists"
            now = self._clock.now()
            txn.create(
                ref,
                {
                    "last_active_at": now,
                    "delete_after": now + self._retention,
                    "deletion_state": DELETION_ACTIVE,
                },
            )
            return "created"

        return self._run_transaction(txn_fn)

    async def create_if_absent(self, principal_id: str) -> CreateResult:
        """利用記録がなければ作る(last_active_at=今・delete_after=今+30日)。すでにあれば何も変えない。

        削除中の依頼者には作り直さず、"deleting" を返す(呼び出し側は、面談の送信を拒否する)。
        """
        return await asyncio.to_thread(self._create_if_absent_sync, principal_id)

    # ------------------------------------------------------------------
    # 更新(有効なクッキーを持つリクエストのたび。1 時間に 1 回まで書く)
    # ------------------------------------------------------------------

    def _touch_sync(self, principal_id: str) -> TouchResult:
        ref = self._collection().document(principal_id)
        # 大半のリクエストは書かないので、まずトランザクションなしで読んで、書く必要のないときは抜ける。
        snap = ref.get()
        if not snap.exists:
            return TouchResult("absent")
        meta = _meta_from(snap.to_dict())
        if meta.deletion_state == DELETION_DELETING:
            return TouchResult("deleting")
        if self._clock.now() - meta.last_active_at < self._touch_interval:
            return TouchResult("unchanged")

        def txn_fn(txn: firestore.Transaction) -> TouchResult:
            # 書く前に、同じトランザクションの中で deletion_state と 1 時間の経過を確かめ直す。
            current_snap = ref.get(transaction=txn)
            if not current_snap.exists:
                return TouchResult("absent")
            current = _meta_from(current_snap.to_dict())
            if current.deletion_state == DELETION_DELETING:
                return TouchResult("deleting")
            now = self._clock.now()
            if now - current.last_active_at < self._touch_interval:
                return TouchResult("unchanged")
            txn.update(ref, {"last_active_at": now, "delete_after": now + self._retention})
            return TouchResult("updated", now)

        return self._run_transaction(txn_fn)

    async def touch(self, principal_id: str) -> TouchResult:
        """有効なクッキーを持つリクエストの利用時刻を、1 時間に 1 回まで書く(§6.3)。"""
        return await asyncio.to_thread(self._touch_sync, principal_id)

    # ------------------------------------------------------------------
    # 削除の印(§6.3 の削除の流れの 1・§4.1 の依頼者の見回り)
    # ------------------------------------------------------------------

    def _mark_deleting_sync(
        self, principal_id: str, expected_delete_after: dt.datetime | None
    ) -> MarkResult:
        ref = self._collection().document(principal_id)

        def txn_fn(txn: firestore.Transaction) -> MarkResult:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return "absent"
            meta = _meta_from(snap.to_dict())
            if meta.deletion_state == DELETION_DELETING:
                return "already_deleting"
            if expected_delete_after is not None and meta.delete_after != expected_delete_after:
                return "changed"  # 見回りが読んだ後に、delete_after が延びた
            txn.update(ref, {"deletion_state": DELETION_DELETING})
            return "marked"

        return self._run_transaction(txn_fn)

    async def mark_deleting(
        self, principal_id: str, *, expected_delete_after: dt.datetime | None = None
    ) -> MarkResult:
        """deletion_state を deleting にする(削除中の印)。

        expected_delete_after を渡すと(依頼者の見回り)、delete_after がその値のままのときだけ立てる。
        変わっていれば "changed"(削除中にしない)。すでに deleting なら "already_deleting"。
        """
        return await asyncio.to_thread(self._mark_deleting_sync, principal_id, expected_delete_after)

    # ------------------------------------------------------------------
    # 削除(削除の流れの最後の段)
    # ------------------------------------------------------------------

    def _delete_sync(self, principal_id: str) -> None:
        self._collection().document(principal_id).delete()

    async def delete(self, principal_id: str) -> None:
        """利用記録を消す(削除の流れの最後の段。すでになければ何もしない)。"""
        await asyncio.to_thread(self._delete_sync, principal_id)

    # ------------------------------------------------------------------
    # 依頼者の見回りの一覧
    # ------------------------------------------------------------------

    def _list_due_sync(self, now: dt.datetime) -> list[DueEntry]:
        query = self._collection().where(filter=FieldFilter("delete_after", "<", now))
        due = []
        for snap in query.stream():
            meta = _meta_from(snap.to_dict())
            if meta.deletion_state == DELETION_ACTIVE:
                due.append(DueEntry(principal_id=snap.id, delete_after=meta.delete_after))
        return due

    async def list_due(self, now: dt.datetime) -> list[DueEntry]:
        """delete_after を過ぎていて、削除中でない依頼者(now をちょうど過ぎたものだけ。境目では消さない)。"""
        return await asyncio.to_thread(self._list_due_sync, now)

    def _list_deleting_sync(self) -> list[str]:
        query = self._collection().where(filter=FieldFilter("deletion_state", "==", DELETION_DELETING))
        return [snap.id for snap in query.stream()]

    async def list_deleting(self) -> list[str]:
        """削除中のまま残っている依頼者(削除の流れを最初からやり直す対象)。"""
        return await asyncio.to_thread(self._list_deleting_sync)
