"""開示台帳 principals/{pid}/ledger(design.md §6.2・§7。`(default)`)。

段の遷移の記録(FR-38)。1 行は「いつ・何を・誰に見せたか」と、誰が操作したか(§6.3 の監査)。
- 書くのは段階開示(web.stages)で、段のフラグを立てるトランザクションの中で書く(段の状態と台帳が食い違わない)。
  行の ID は、交渉 ID と出来事から決まる固定の文字列なので、同じ出来事が 2 行になることはない(並行・再送でも)。
- 行に書くのは、見せたものの種類(items)だけ。生の値(職務要約の本文・氏名・連絡先)は書かない(§7)。本文は
  stages/{nid} にだけあり、本人の削除と 30 日の自動削除で、台帳と一緒に消える。
- 本人が読めるのは、自分の台帳だけ(list_entries。web.stages_api が、セッションの依頼者 ID と一致するときだけ呼ぶ。§6.3)。
  途中確認の回答は、金庫のイベント列の自分の側の見え方から読んで、画面が同じ並びに入れる(§7)。ここには写さない。
- 本人の削除と 30 日の自動削除の流れ(§6.3 の 3)が、この依頼者の台帳の各文書を消す。Firestore は親の文書を消しても
  下の階層を消さないので、台帳の各文書を明示的に消す(金庫のイベント列の削除と同じ扱い)。
"""

import asyncio
import datetime as dt
import logging
from typing import Literal

from google.cloud import firestore
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PRINCIPALS_COLLECTION = "principals"
LEDGER_SUBCOLLECTION = "ledger"

# 1 回のバッチで消す文書の数(Firestore のバッチの上限 500 より小さく)。
_DELETE_BATCH_SIZE = 300

_log = logging.getLogger(__name__)

# 出来事の種類。disclose は、段が開いて、何かを見せたこと。meet・approve は、「会う」「承認」が押されたこと(見せるための同意)。
LedgerAction = Literal["disclose", "meet", "approve"]
# 操作した主体。principal は本人(押した依頼者)。fictional_employer は架空の求人の自動応答、fictional_candidate は架空の候補者
# (デモ・攻撃)の自動操作。system は、押す人のいない自動の出来事(段 0 の表示)。
LedgerOperator = Literal["principal", "fictional_employer", "fictional_candidate", "system"]
# 見せた相手。both は、段 0(見込みと組み合わせを双方に出す)。
LedgerRecipient = Literal["candidate", "employer", "both"]


class LedgerRow(BaseModel):
    """台帳に書く 1 行。principal_id は台帳の持ち主(= 操作した本人、または本人の交渉で自動応答が動いた先)。"""

    model_config = ConfigDict(extra="forbid")

    principal_id: str
    nid: str
    action: LedgerAction
    stage: int  # 出来事のときに開いていた段(disclose は、開いた段)
    operator: LedgerOperator
    items: list[str] = Field(default_factory=list)  # disclose のとき、見せたものの種類(値は書かない)
    to: LedgerRecipient | None = None
    simulated: bool = False  # 段 2 の氏名・連絡先が模擬表示(実ユーザーは連絡先を集めない。§6.2)
    at: dt.datetime


class LedgerEntry(BaseModel):
    """本人に返す 1 行。依頼者 ID は返さない(本人の台帳なので不要)。知らない項目と、形の違う古い行は読まない(無視・除外)。"""

    model_config = ConfigDict(extra="ignore")

    nid: str
    action: LedgerAction
    stage: int
    operator: LedgerOperator
    items: list[str] = Field(default_factory=list)
    to: LedgerRecipient | None = None
    simulated: bool = False
    at: dt.datetime


def _order(entry: LedgerEntry) -> tuple:
    """並べ順: 時刻、交渉、段(同じ段では、段を開いた記録が、その段で押された記録より先)、操作した主体(時刻が同じでも並びが決まるように)。"""
    return (entry.at, entry.nid, entry.stage * 2 + (0 if entry.action == "disclose" else 1), entry.operator)


class DisclosureLedger:
    """開示台帳の読み書き・削除。Firestore(同期クライアント)の呼び出しは別スレッドで行う。"""

    def __init__(self, db: firestore.Client) -> None:
        self._db = db

    def _collection(self, principal_id: str):
        return self._db.collection(PRINCIPALS_COLLECTION).document(principal_id).collection(LEDGER_SUBCOLLECTION)

    def row_ref(self, principal_id: str, row_id: str):
        """台帳の 1 行の参照(web.stages が、段の状態と同じトランザクションの中で書く)。"""
        return self._collection(principal_id).document(row_id)

    def _list_sync(self, principal_id: str) -> list[LedgerEntry]:
        entries = []
        skipped = 0
        for snap in self._collection(principal_id).stream():
            try:
                entries.append(LedgerEntry.model_validate(snap.to_dict()))
            except ValidationError:
                skipped += 1  # 段階開示が書いた形でない行(読み出しに出さない)
        if skipped:
            _log.warning("ledger rows skipped because they have an unknown shape count=%d", skipped)
        return sorted(entries, key=_order)

    async def list_entries(self, principal_id: str) -> list[LedgerEntry]:
        """principal_id の台帳の全行を、時系列で返す(FR-38)。なければ空。"""
        return await asyncio.to_thread(self._list_sync, principal_id)

    def _delete_all_sync(self, principal_id: str) -> int:
        ledger = self._collection(principal_id)
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
