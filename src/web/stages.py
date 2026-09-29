"""段階開示の状態 stages/{nid}(design.md §6.2)。`web` の (default) の Firestore に持つ。

1d-1 では作成だけを行う(段 0)。段の遷移(会う・承認・匿名職務要約)は後の段で足す。

stages/{nid} は、交渉の作成直後に作る。作り損ねても、見回り(§4.1)と、画面で交渉を開いたときに、
なければ作る(冪等)。そのため、判定の後に web が落ちても、段階開示の状態は失われない(DV-08)。
本物の候補者の依頼者 ID を持たせるのは、削除のときにこれで引くため(金庫の削除が先に済んで、
金庫から交渉が消えていても見つけられるように。§6.2)。候補者が架空人物なら None。
"""

import asyncio
import datetime as dt

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from pydantic import BaseModel, ConfigDict

from vault.clock import Clock

STAGES_COLLECTION = "stages"


class StageDocument(BaseModel):
    """stages/{nid} 文書の形。stage=0 は段 0(見込みと組み合わせを双方に自動で表示する段)。"""

    model_config = ConfigDict(extra="forbid")

    nid: str
    candidate_principal_id: str | None
    stage: int = 0
    created_at: dt.datetime


class StageStore:
    """stages/{nid} の作成。Firestore(同期クライアント)の呼び出しは別スレッドで行う。"""

    def __init__(self, db: firestore.Client, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def _create_sync(self, nid: str, candidate_principal_id: str | None) -> bool:
        document = StageDocument(
            nid=nid,
            candidate_principal_id=candidate_principal_id,
            stage=0,
            created_at=self._clock.now(),
        )
        try:
            # create は「なければ作る」を 1 回の書き込みで行う。すでにあれば何も変えない
            # (段が進んだ文書を、段 0 で上書きしない。冪等)。
            self._db.collection(STAGES_COLLECTION).document(nid).create(document.model_dump(mode="python"))
        except AlreadyExists:
            return False
        return True

    async def ensure(self, nid: str, candidate_principal_id: str | None) -> bool:
        """stages/{nid} がなければ段 0 で作る。作ったら True、すでにあれば False(何も変えない)。"""
        return await asyncio.to_thread(self._create_sync, nid, candidate_principal_id)
