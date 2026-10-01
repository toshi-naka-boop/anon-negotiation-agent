"""段階開示の状態 stages/{nid}(design.md §6.2)。`web` の (default) の Firestore に持つ。

1d-1 では作成だけを行った(段 0)。1d-2 で足したのは、次の 3 つ。
- 本人の削除と 30 日の自動削除(§6.3)が、本人の段の状態を消す口(delete_for_principal)。
- デモ・攻撃の段の状態に、金庫と同じ 96 時間の期限の項目(ttl_at)を付ける(台帳 I-6)。
- デモ用のエンドポイントが、架空の候補者の交渉かを確かめる口(is_fictional_negotiation)。
段の遷移(会う・承認・匿名職務要約)は後の段(④)で足す。

stages/{nid} は、交渉の作成直後に作る。作り損ねても、見回り(§4.1)と、画面で交渉を開いたときに、
なければ作る(冪等)。そのため、判定の後に web が落ちても、段階開示の状態は失われない(DV-08)。
本物の候補者の依頼者 ID を持たせるのは、削除のときにこれで引くため(金庫の削除が先に済んで、
金庫から交渉が消えていても見つけられるように。§6.2)。候補者が架空人物(デモ・攻撃)なら None。

期限の項目 ttl_at は、候補者が架空人物のものにだけ付ける(本物の利用者の段の状態は、本人の削除と
30 日の自動削除で消える。TTL では消さない)。Firestore の TTL ポリシー自体の設定はデプロイの段で行う。
"""

import asyncio
import datetime as dt
import re

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict

from negotiation_core import ID_PATTERN

from vault.clock import Clock

from web.config import DEFAULT_WEB_CONFIG, RetentionConfig

STAGES_COLLECTION = "stages"

_NID_RE = re.compile(ID_PATTERN)
# 1 回のバッチで消す文書の数(Firestore のバッチの上限 500 より小さく)。
_DELETE_BATCH_SIZE = 300


class StageDocument(BaseModel):
    """stages/{nid} 文書の形。stage=0 は段 0(見込みと組み合わせを双方に自動で表示する段)。"""

    model_config = ConfigDict(extra="forbid")

    nid: str
    candidate_principal_id: str | None
    stage: int = 0
    created_at: dt.datetime
    ttl_at: dt.datetime | None = None  # デモ・攻撃(候補者が架空人物)にだけ付ける(台帳 I-6)


class StageStore:
    """stages/{nid} の作成・削除・確認。Firestore(同期クライアント)の呼び出しは別スレッドで行う。"""

    def __init__(
        self,
        db: firestore.Client,
        clock: Clock,
        config: RetentionConfig = DEFAULT_WEB_CONFIG.retention,
    ) -> None:
        self._db = db
        self._clock = clock
        self._fictional_ttl = dt.timedelta(seconds=config.fictional_stage_ttl_seconds)

    def _create_sync(self, nid: str, candidate_principal_id: str | None) -> bool:
        now = self._clock.now()
        document = StageDocument(
            nid=nid,
            candidate_principal_id=candidate_principal_id,
            stage=0,
            created_at=now,
            ttl_at=now + self._fictional_ttl if candidate_principal_id is None else None,
        )
        data = document.model_dump(mode="python")
        if data["ttl_at"] is None:
            del data["ttl_at"]  # 本物の利用者の段の状態には、期限の項目そのものを付けない
        try:
            # create は「なければ作る」を 1 回の書き込みで行う。すでにあれば何も変えない
            # (段が進んだ文書を、段 0 で上書きしない。冪等)。
            self._db.collection(STAGES_COLLECTION).document(nid).create(data)
        except AlreadyExists:
            return False
        return True

    async def ensure(self, nid: str, candidate_principal_id: str | None) -> bool:
        """stages/{nid} がなければ段 0 で作る。作ったら True、すでにあれば False(何も変えない)。

        candidate_principal_id が None(候補者が架空人物。デモ・攻撃)なら、96 時間の ttl_at を付ける。
        """
        return await asyncio.to_thread(self._create_sync, nid, candidate_principal_id)

    def _is_fictional_sync(self, nid: str) -> bool:
        if _NID_RE.fullmatch(nid) is None:
            return False  # Firestore の文書 ID にできない形の値は、そもそも交渉 ID ではない
        snap = self._db.collection(STAGES_COLLECTION).document(nid).get()
        if not snap.exists:
            return False
        data = snap.to_dict()
        # 項目が欠けた文書は、架空と読まない(.get(...) is None では、欠けも「架空」になってしまう。台帳 X-38)。
        return "candidate_principal_id" in data and data["candidate_principal_id"] is None

    async def is_fictional_negotiation(self, nid: str) -> bool:
        """nid が、候補者が架空人物の交渉(デモ・攻撃)と分かっているか(§6.3 のデモ用エンドポイントの確認)。

        これは web の補助の確認(金庫の確認が正本。台帳 X-38)。段の状態がない(まだ作っていない)交渉、
        candidate_principal_id の項目が欠けた文書、本物の候補者の交渉、交渉 ID の形でない値は False
        (拒否する側に倒す)。
        """
        return await asyncio.to_thread(self._is_fictional_sync, nid)

    def _delete_for_principal_sync(self, principal_id: str) -> int:
        query = self._db.collection(STAGES_COLLECTION).where(
            filter=FieldFilter("candidate_principal_id", "==", principal_id)
        )
        deleted = 0
        while True:
            documents = list(query.limit(_DELETE_BATCH_SIZE).stream())
            if not documents:
                return deleted
            batch = self._db.batch()
            for snap in documents:
                batch.delete(snap.reference)
            batch.commit()
            deleted += len(documents)

    async def delete_for_principal(self, principal_id: str) -> int:
        """principal_id が候補者の段の状態(段 1 の職務要約を含む)をすべて消す(冪等)。消した数を返す。

        金庫の削除が先に済んで、金庫から交渉が消えていても、stages/{nid} に持たせた候補者の依頼者 ID
        で引ける(§6.3 の削除の流れの 3)。
        """
        return await asyncio.to_thread(self._delete_for_principal_sync, principal_id)
