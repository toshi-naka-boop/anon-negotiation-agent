"""活動ログと、並べて見る画面(候補者側・求人側の 2 つのパネル)の API(design.md §7・§3.2・§6.3。FR-37。DV-10)。画面は含まない。

どちらも、金庫のイベント列(§3.2。正本)を側を指定して読み、画面向けの形に整えて返すだけ。web はイベントを写さず、保存しない。

| メソッド・パス | 呼べる人 | 返すもの |
|---|---|---|
| GET /v1/negotiations/{nid}/activity?after_seq= | 本人(その交渉の当事者) | 本人の側(候補者側)の活動ログ |
| GET /v1/negotiations/{nid}/panels?candidate_after_seq= | 本人(その交渉の当事者) | 本人の側のパネル 1 つ(求人側は null)と、段の参照 |
| GET /v1/demo/negotiations/{nid}/activity?side=&after_seq= | 誰でも(セッションを見ない) | 架空人物の交渉(デモ・攻撃)の、指定した側の活動ログ |
| GET /v1/demo/negotiations/{nid}/panels?candidate_after_seq=&employer_after_seq= | 誰でも(セッションを見ない) | 架空人物の交渉の両側のパネルと、段の参照 |

- **見せるもの**: 手の種類・組み合わせ・自分側の評価・途中確認(質問と回答)・無効手の理由・最終結果 `{likelihood, package}` だけ。
  金庫のイベントにあるものを、種類ごとに決めた項目だけ写す(table `_SHAPE`)。相手側の評価・残り回数・`version`・終了理由・
  期限は、そもそも金庫のイベントの見え方にない(§3.2。台帳 X-25)ので、ここにも出ない。種類ごとの項目を限っているのは、
  金庫のイベントに項目が増えても、そのまま画面に出ないようにするため。
- **時刻は出さない**: 金庫のイベントに時刻の項目がない(`EventViewItem`)。web が読んだ時刻を、記録の時刻として見せることはしない。
- **本人の側は、いつも候補者側**(本物の依頼者は候補者側だけ。§6.1)。側を指定する引数はなく、求人側は読めない(DV-01)。
- **権限**: 本人の経路は api.py の `require_own_negotiation` をそのまま使う(セッションの依頼者 ID が当事者の交渉だけ。違えば存在しない交渉も
  同じ 403、セッションがなければ 401。§6.3)。デモの経路は api.py の `demo_events` と同じ 2 段の確認(web の段の状態〔補助〕と、
  金庫のデモ用の読み出しの口〔正本。mode が demo・attack で候補者が架空人物のときだけ〕。どちらかが断れば 403。台帳 X-38)を通す。
  本物の依頼者の交渉は、デモの経路からは読めない。デモの経路は `/v1/demo/` の下に置くので、ミドルウェアはセッションを見ない(DEMO_PATH_PREFIX)。
- **段の参照**: `stages/{nid}` があれば `{"stage": 段の番号}`、なければ(または番号が読めなければ)null。段の中身(職務要約・連絡先・
  依頼者 ID・呼び出し数など)は読まず、出さない。段階開示の中身は段階開示の段(④)が作る。

ログには何も書かない(組み合わせの値・評価・依頼者 ID を、ここから出さない)。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from negotiation_core import LastErrorReason, MoveType, Package, Side, Verdict

from vault.api_models import EventViewItem
from vault.models import EventKind, NegotiationResult, PrincipalAnswerKind

from web.services import WebServices
from web.stages import STAGES_COLLECTION
from web.vault_client import VaultNotFoundError

# 1 つの側のイベント番号(seq)の上限。after_seq に巨大な値を渡されても、金庫(Firestore の整数)に届かないようにする。
_MAX_SEQ = 2**31 - 1

# 記録の主体: self はその側の本人(のエージェントと、本人の操作)、counterparty は相手、system は交渉の終わり。
Actor = Literal["self", "counterparty", "system"]
Action = Literal[
    "check", "propose", "reject", "ask_principal", "principal_answer", "invalid", "pause", "resume", "final_result"
]

# 金庫のイベントの種類 → (主体, 画面向けの手の種類, 写す項目)。
# - 相手の提案を受け取った記録(offer_received)は「相手の propose」、相手に断られた記録(offer_rejected)は「相手の reject」。
#   own_evaluation は、その側自身の評価(相手の評価ではない。§3.2)。
# - 写す項目に載っていない項目は、金庫のイベントにあっても返さない。
_SHAPE: dict[EventKind, tuple[Actor, Action, tuple[str, ...]]] = {
    "check": ("self", "check", ("package", "own_evaluation")),
    "propose": ("self", "propose", ("package",)),
    "offer_received": ("counterparty", "propose", ("package", "own_evaluation")),
    "reject": ("self", "reject", ("package",)),
    "offer_rejected": ("counterparty", "reject", ("package",)),
    "ask_principal": ("self", "ask_principal", ("package",)),
    "principal_answer": ("self", "principal_answer", ("package", "own_evaluation", "answer")),
    "invalid": ("self", "invalid", ("package", "own_evaluation", "reason", "attempted_move")),
    "pause": ("self", "pause", ()),
    "resume": ("self", "resume", ()),
    "final_result": ("system", "final_result", ("result",)),
}


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivityEntry(_ResponseModel):
    """活動ログの 1 件(その側から見た 1 つの記録)。項目は、手の種類(action)ごとに決まっていて、ほかは null。"""

    seq: int  # 側ごとの番号(§3.2)。画面は seq で重複を除く
    actor: Actor
    action: Action
    package: Package | None = None
    own_evaluation: Verdict | None = None  # check・相手の提案(propose の counterparty)・principal_answer・invalid。その側自身の評価
    answer: PrincipalAnswerKind | None = None  # principal_answer だけ
    reason: LastErrorReason | None = None  # invalid だけ
    attempted_move: MoveType | None = None  # invalid だけ(金庫が分かる無効手のみ)
    result: NegotiationResult | None = None  # final_result だけ。{likelihood, package}。理由は含まない(AC-08)


class ActivityLog(_ResponseModel):
    """ある側の活動ログ(after_seq より後の記録を、seq の順に)。next_after_seq は、次に読むときの after_seq。"""

    side: Side
    entries: list[ActivityEntry]
    next_after_seq: int


class StageRef(_ResponseModel):
    """段の参照(stages/{nid} がある)。段の番号だけで、中身は出さない。"""

    stage: int


class PanelsResponse(_ResponseModel):
    """並べて見る画面。側ごとのパネル(読めない側は null)と、段の参照(段の状態がなければ null)。

    デモ・攻撃の交渉は両側、本物の利用者の交渉は本人の側(candidate)だけ。
    """

    candidate: ActivityLog | None
    employer: ActivityLog | None
    stage: StageRef | None


def to_entry(event: EventViewItem) -> ActivityEntry:
    """金庫のイベント(その側の見え方)1 件を、活動ログの 1 件にする。知らない種類は KeyError(黙って通さない)。"""
    actor, action, fields = _SHAPE[event.kind]
    return ActivityEntry(
        seq=event.seq, actor=actor, action=action, **{name: getattr(event, name) for name in fields}
    )


def to_activity_log(side: Side, events: list[EventViewItem], after_seq: int) -> ActivityLog:
    """side の見え方のイベント(after_seq より後。seq の順)から、活動ログを作る。"""
    entries = [to_entry(event) for event in events]
    return ActivityLog(side=side, entries=entries, next_after_seq=max([after_seq, *(entry.seq for entry in entries)]))


def build_activity_router(
    services: WebServices, require_own_negotiation: Callable[..., Awaitable[str]]
) -> APIRouter:
    """活動ログとパネルのルートを作る。api.py の build_router が include する。

    require_own_negotiation は api.py の依存(セッションの依頼者が当事者の交渉だけを通し、nid を返す)。権限の確認を、ここで二重に
    書かないために受け取る。
    """
    router = APIRouter()
    vault = services.vault

    async def read_demo_events(nid: str, side: Side, after_seq: int) -> list[EventViewItem]:
        """架空人物の交渉(デモ・攻撃)の、side の見え方。api.py の demo_events と同じ 2 段の確認(台帳 X-38)。"""
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await vault.get_demo_events(nid, side, after_seq)
        except VaultNotFoundError:
            raise HTTPException(status_code=403, detail="forbidden") from None

    async def read_stage(nid: str) -> StageRef | None:
        """stages/{nid} の段の番号だけを読む(nid は、権限の確認を通ったもの)。なければ、番号が整数でなければ None。"""

        def read() -> StageRef | None:
            snap = services.default_db.collection(STAGES_COLLECTION).document(nid).get()
            stage = snap.to_dict().get("stage") if snap.exists else None
            return StageRef(stage=stage) if isinstance(stage, int) and not isinstance(stage, bool) else None

        return await asyncio.to_thread(read)

    # ------------------------------------------------------------------
    # 本人の経路(本人はいつも候補者側。側を指定する引数はない)
    # ------------------------------------------------------------------

    @router.get("/v1/negotiations/{nid}/activity", response_model=ActivityLog)
    async def negotiation_activity(
        nid: str = Depends(require_own_negotiation), after_seq: int = Query(default=0, ge=0, le=_MAX_SEQ)
    ) -> ActivityLog:
        """活動ログ(FR-37): 本人の側(候補者側)の見え方を、after_seq より後から。相手の側は読めない。"""
        return to_activity_log("candidate", await vault.get_events(nid, "candidate", after_seq), after_seq)

    @router.get("/v1/negotiations/{nid}/panels", response_model=PanelsResponse)
    async def negotiation_panels(
        nid: str = Depends(require_own_negotiation),
        candidate_after_seq: int = Query(default=0, ge=0, le=_MAX_SEQ),
    ) -> PanelsResponse:
        """並べて見る画面(本物の利用者の交渉): 本人の側のパネルだけ。求人側は null(本人に見せるのは本人の側だけ。§3.2)。"""
        events = await vault.get_events(nid, "candidate", candidate_after_seq)
        return PanelsResponse(
            candidate=to_activity_log("candidate", events, candidate_after_seq),
            employer=None,
            stage=await read_stage(nid),
        )

    # ------------------------------------------------------------------
    # デモ用の経路(セッションを見ない。DEMO_PATH_PREFIX の下。本物の依頼者の交渉は読めない)
    # ------------------------------------------------------------------

    @router.get("/v1/demo/negotiations/{nid}/activity", response_model=ActivityLog)
    async def demo_activity(
        nid: str, side: Side, after_seq: int = Query(default=0, ge=0, le=_MAX_SEQ)
    ) -> ActivityLog:
        """架空人物の交渉の、指定した側の活動ログ(§3.2)。読めるのは、候補者が架空人物の交渉(デモ・攻撃)だけ。"""
        return to_activity_log(side, await read_demo_events(nid, side, after_seq), after_seq)

    @router.get("/v1/demo/negotiations/{nid}/panels", response_model=PanelsResponse)
    async def demo_panels(
        nid: str,
        candidate_after_seq: int = Query(default=0, ge=0, le=_MAX_SEQ),
        employer_after_seq: int = Query(default=0, ge=0, le=_MAX_SEQ),
    ) -> PanelsResponse:
        """並べて見る画面(デモ・攻撃): 候補者側・求人側の両方のパネルを、1 回の呼び出しで返す。

        どちらのパネルも、その側自身の見え方だけ(相手の評価は、どちらにも入らない。DV-10)。側ごとに seq の番号が別なので、
        読み始める位置も側ごとに指定する。
        """
        candidate_events = await read_demo_events(nid, "candidate", candidate_after_seq)
        employer_events = await read_demo_events(nid, "employer", employer_after_seq)
        return PanelsResponse(
            candidate=to_activity_log("candidate", candidate_events, candidate_after_seq),
            employer=to_activity_log("employer", employer_events, employer_after_seq),
            stage=await read_stage(nid),
        )

    return router
