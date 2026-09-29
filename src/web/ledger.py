"""開示台帳 principals/{pid}/ledger(design.md §6.2・§7。`(default)`)。

段の遷移の記録(FR-38)を書くのは段階開示の段(④)で、1d-2 の範囲は削除だけ。本人の削除と 30 日の
自動削除の流れ(§6.3 の 3)が、この依頼者の台帳の各文書を消す。Firestore は親の文書を消しても
下の階層を消さないので、台帳の各文書を明示的に消す(金庫のイベント列の削除と同じ扱い)。
"""

import asyncio

from google.cloud import firestore

PRINCIPALS_COLLECTION = "principals"
LEDGER_SUBCOLLECTION = "ledger"

# 1 回のバッチで消す文書の数(Firestore のバッチの上限 500 より小さく)。
_DELETE_BATCH_SIZE = 300


class DisclosureLedger:
    """開示台帳の削除。Firestore(同期クライアント)の呼び出しは別スレッドで行う。"""

    def __init__(self, db: firestore.Client) -> None:
        self._db = db

    def _delete_all_sync(self, principal_id: str) -> int:
        ledger = self._db.collection(PRINCIPALS_COLLECTION).document(principal_id).collection(LEDGER_SUBCOLLECTION)
        deleted = 0
        while True:
            documents = list(ledger.limit(_DELETE_BATCH_SIZE).stream())
            if not documents:
                return deleted
            batch = self._db.batch()
            for snap in documents:
                batch.delete(snap.reference)
            batch.commit()
            deleted += len(documents)

    async def delete_all(self, principal_id: str) -> int:
        """principal_id の台帳の文書をすべて消す(冪等。なければ 0 件)。消した数を返す。"""
        return await asyncio.to_thread(self._delete_all_sync, principal_id)
