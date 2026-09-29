"""web の画面 API(design.md §5 の手順 9・§6.3・§3.3・§4.4)。画面(静的な HTML/JS)は作らず、API だけ。

設計書が API の形を細かく決めていないので、最小限の形にした(一覧は報告に書く)。

権限(§6.3)
- 本人のデータと交渉への操作は、すべて「セッションの依頼者 ID が、その対象の当事者であること」を
  確かめてから行う。違えば 403。セッションがなければ 401。
  - 依頼者 ID を指す URL(/v1/principals/{pid}/...)は、pid がセッションの依頼者 ID と同じであること。
  - 交渉 ID を指す URL(/v1/negotiations/{nid}/...)は、nid が、金庫の「本人が当事者の交渉の一覧」に
    あること(当事者かどうかの正本は金庫)。存在しない交渉 ID も、他人の交渉 ID も、同じ 403。
- 状態を変えるリクエストは POST に限り、X-Requested-With を必須にする(ミドルウェア)。
- 依頼者 ID は、開始ページの GET(/start)でしか発行しない。ほかのルートは、クッキーがなければ 401 で、
  ID を発行しない。有効なクッキーがあれば、開始ページを開き直しても ID は変わらない。
- デモ用のエンドポイント(/v1/demo/...)は、セッションを見ない。本物の依頼者には触れない: 交渉は
  架空人物のテンプレートからだけ作り(モードは demo 固定)、読めるのは、候補者が架空人物と分かっている
  交渉(デモ・攻撃)だけ。金庫の側でも、demo・attack の交渉は本物の依頼者を持てない(作成の検証)。

金庫に書く前に、利用記録 principals_meta がなければならない(面談の送信が作る。§5 の手順 9)。
ブロックリストの登録と交渉の作成は、面談を送っていない(利用記録がない)依頼者には 409 で断る。

ログには、例外の型名だけを書く(組み合わせの値・クッキー・依頼者の入力は書かない)。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from negotiation_core import Side

from vault.api_models import (
    CandidateParticipantRequest,
    ControlRequest,
    CreateNegotiationRequest,
    EmployerParticipantRequest,
    EventViewItem,
    PolicyView,
    PrincipalAnswerRequest,
    PrincipalNegotiationSummary,
    PutBlocklistRequest,
)
from vault.ids import generate_id
from vault.models import NegotiationMode

from web.api_models import (
    BlocklistRequest,
    ControlBody,
    CreateNegotiationBody,
    DemoCreateBody,
    InterviewSubmitRequest,
    PrincipalAnswerBody,
)
from web.deletion import DeletionOutcome
from web.referee import NegotiationContext
from web.services import WebServices
from web.session import PrincipalSession

_log = logging.getLogger(__name__)

# デモ用のエンドポイントのパス。ミドルウェアは、ここでは依頼者のセッションを見ない(§6.3)。
DEMO_PATH_PREFIX = "/v1/demo/"


def build_router(services: WebServices) -> APIRouter:
    """services の部品を使うルートを作る。"""
    router = APIRouter()
    vault = services.vault

    # ------------------------------------------------------------------
    # 権限の確認
    # ------------------------------------------------------------------

    def require_session(request: Request) -> PrincipalSession:
        session = getattr(request.state, "principal_session", None)
        if session is None:
            raise HTTPException(status_code=401, detail="no_session")
        return session

    def require_own_principal(pid: str, session: PrincipalSession = Depends(require_session)) -> PrincipalSession:
        """URL の依頼者 ID が、セッションの依頼者 ID と同じであること。"""
        if pid != session.principal_id:
            raise HTTPException(status_code=403, detail="forbidden")
        return session

    async def require_own_negotiation(nid: str, session: PrincipalSession = Depends(require_session)) -> str:
        """URL の交渉 ID が、セッションの依頼者が当事者の交渉であること(金庫の一覧で確かめる)。"""
        summaries = await vault.list_principal_negotiations(session.principal_id)
        if all(summary.nid != nid for summary in summaries):
            raise HTTPException(status_code=403, detail="forbidden")
        return nid

    def require_registered(session: PrincipalSession) -> None:
        """金庫に書く前に、利用記録がなければならない(面談を送っていない依頼者は、まだ書けない)。"""
        if not session.registered:
            raise HTTPException(status_code=409, detail="interview_not_submitted")

    async def register_created_negotiation(nid: str, mode: NegotiationMode, principal_id: str | None) -> None:
        """作った交渉の段の状態(段 0)を作り、レフェリーのタスクを動かす(どちらも冪等)。

        段の状態を作れなくても、交渉の作成は成功として返す(見回りが、なければ作る。§6.2)。
        """
        try:
            await services.stages.ensure(nid, principal_id)
        except Exception as exc:
            _log.error("stage creation failed after negotiation creation error=%s", type(exc).__name__)
        services.referees.start(NegotiationContext(nid=nid, mode=mode, candidate_principal_id=principal_id))

    # ------------------------------------------------------------------
    # 開始ページ(§6.3: 依頼者 ID を発行するのは、ここでだけ)
    # ------------------------------------------------------------------

    @router.get("/start")
    async def start_page(request: Request, response: Response) -> dict[str, str]:
        """面談の開始ページの GET。有効なクッキーがなければ、新しい依頼者 ID を発行する。

        有効なクッキーがあれば、新しい ID を発行しない(同じ ID のまま。期限の延長はミドルウェアが行う)。
        利用記録は、ここでは作らない(開始ページを開いただけの訪問者やクローラーには作らない)。
        """
        if getattr(request.state, "principal_session", None) is None:
            services.codec.set_cookie(response, services.codec.issue(generate_id(), services.clock.now()))
        response.headers["Cache-Control"] = "no-store"  # ID を発行するかもしれない応答を、共有の置き場に残さない
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # 面談の送信(§5 の手順 9)・ポリシーの閲覧・ブロックリスト
    # ------------------------------------------------------------------

    @router.post("/v1/principals/{pid}/interview")
    async def submit_interview(
        pid: str, body: InterviewSubmitRequest, session: PrincipalSession = Depends(require_own_principal)
    ) -> dict[str, str]:
        """面談の結果(生の値のアンカー・外した軸・属性帯)を、web で丸めて金庫に置く。

        金庫に初めて書く前に、利用記録 principals_meta を作る(§5 の手順 9・§6.3)。
        """
        try:
            request = body.to_put_policy_request()  # 丸め(§2.5)と矛盾検査。エラーの文面に値が入るので返さない
        except ValueError:
            raise HTTPException(status_code=422, detail="policy_invalid") from None
        if await services.meta.create_if_absent(pid) == "deleting":
            raise HTTPException(status_code=409, detail="principal_deleting")
        await vault.put_policy(pid, request)
        return {"status": "submitted"}

    @router.get("/v1/principals/{pid}/policy", response_model=PolicyView)
    async def get_policy(pid: str, session: PrincipalSession = Depends(require_own_principal)) -> PolicyView:
        """本人向けの、丸め済みポリシーの表示。金庫から読むだけで、web には保存しない(§3.3)。"""
        return await vault.get_policy(pid)

    @router.post("/v1/principals/{pid}/blocklist")
    async def set_blocklist(
        pid: str, body: BlocklistRequest, session: PrincipalSession = Depends(require_own_principal)
    ) -> dict[str, str]:
        require_registered(session)
        await vault.put_blocklist(pid, PutBlocklistRequest(blocklist=body.blocklist))
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # 交渉の作成・一覧
    # ------------------------------------------------------------------

    @router.post("/v1/principals/{pid}/negotiations")
    async def create_negotiation(
        pid: str, body: CreateNegotiationBody, session: PrincipalSession = Depends(require_own_principal)
    ) -> dict[str, str]:
        """本物の候補者が、求人(フィクスチャのテンプレート)を 1 件選んで交渉を始める(§6.1)。"""
        require_registered(session)
        created = await vault.create_negotiation(
            CreateNegotiationRequest(
                request_id=f"{pid}:{body.request_id}",  # 依頼者ごとの名前空間(他人の交渉 ID を返されない)
                mode="live",
                candidate=CandidateParticipantRequest(is_fictional=False, principal_id=pid),
                employer=EmployerParticipantRequest(template_id=body.employer_template_id),
            )
        )
        if created.status == "refused" or created.nid is None:
            # already_active・budget_exhausted・blocked・attribute_bands_missing・principal_deleting
            raise HTTPException(status_code=409, detail=created.reason or "refused")
        await register_created_negotiation(created.nid, "live", pid)
        return {"nid": created.nid}

    @router.get("/v1/principals/{pid}/negotiations", response_model=list[PrincipalNegotiationSummary])
    async def list_negotiations(
        pid: str, session: PrincipalSession = Depends(require_own_principal)
    ) -> list[PrincipalNegotiationSummary]:
        """本人が当事者の交渉の一覧。金庫が返す項目(交渉 ID・求人 ID・作成時刻・状態・最終結果)だけ。"""
        return await vault.list_principal_negotiations(pid)

    # ------------------------------------------------------------------
    # 交渉への操作(本人が当事者の交渉だけ。本人はいつも候補者側)
    # ------------------------------------------------------------------

    @router.get("/v1/negotiations/{nid}/events", response_model=list[EventViewItem])
    async def negotiation_events(
        nid: str = Depends(require_own_negotiation), after_seq: int = Query(default=0, ge=0)
    ) -> list[EventViewItem]:
        """活動ログ(FR-37): イベント列の、本人の側(候補者側)の見え方だけ。相手の側は読めない。"""
        return await vault.get_events(nid, "candidate", after_seq)

    @router.post("/v1/negotiations/{nid}/principal-answer")
    async def answer_question(
        body: PrincipalAnswerBody, nid: str = Depends(require_own_negotiation)
    ) -> dict[str, str]:
        """途中確認への回答(§4.4)。本人が見た質問(組み合わせ)に対する回答のときだけ受け付ける。"""
        view = await vault.get_view(nid, "candidate")
        if view.status != "awaiting_principal" or view.awaiting_principal_package != body.package:
            raise HTTPException(status_code=409, detail="no_matching_question")
        # version は、金庫の手の操作の前提として web が使うだけで、画面には出さない(§3.3)。
        answered = await vault.post_principal_answer(
            nid,
            PrincipalAnswerRequest(
                expected_version=view.version, side="candidate", package=body.package, answer=body.answer
            ),
        )
        return {"status": answered.status}

    @router.post("/v1/negotiations/{nid}/control")
    async def control_negotiation(
        body: ControlBody, nid: str = Depends(require_own_negotiation)
    ) -> dict[str, str | bool]:
        """一時停止・再開・取消(FR-40。§3.4)。金庫の control にそのまま送る。"""
        controlled = await vault.control(nid, ControlRequest(side="candidate", action=body.action))
        return {"status": controlled.status, "paused": controlled.paused}

    # ------------------------------------------------------------------
    # データの削除(§6.3 の削除の流れ)
    # ------------------------------------------------------------------

    @router.post("/v1/principals/{pid}/delete")
    async def delete_data(
        pid: str, response: Response, session: PrincipalSession = Depends(require_own_principal)
    ) -> dict[str, str]:
        """本人の「データを消す」。30 日の自動削除と同じ流れを使う。

        最後の段(利用記録の削除)まで終われば、クッキーも消す。利用記録がなければ(面談を送っていなければ)、
        サーバにデータはないので、クッキーを消すだけ。途中で失敗したときは 202: 削除中の印が残るので、
        依頼者の見回りが最後までやり直す(本人が押し直す必要はない)。
        """
        outcome = await services.deletion.delete_by_user(pid)
        if outcome is DeletionOutcome.INCOMPLETE:
            response.status_code = 202
            return {"status": "deleting"}
        services.codec.clear_cookie(response)
        return {"status": "deleted"}

    # ------------------------------------------------------------------
    # デモ用のエンドポイント(セッションを見ない。本物の依頼者には触れない。§6.3)
    # ------------------------------------------------------------------

    @router.post("/v1/demo/negotiations")
    async def create_demo_negotiation(body: DemoCreateBody) -> dict[str, str]:
        """デモの交渉を、架空人物のテンプレートから作る(§3.7)。モードは demo 固定、依頼者は関わらない。"""
        created = await vault.create_negotiation(
            CreateNegotiationRequest(
                request_id=f"demo:{body.request_id}",
                mode="demo",
                candidate=CandidateParticipantRequest(is_fictional=True, template_id=body.candidate_template_id),
                employer=EmployerParticipantRequest(template_id=body.employer_template_id),
            )
        )
        if created.status == "refused" or created.nid is None:
            raise HTTPException(status_code=409, detail=created.reason or "refused")
        await register_created_negotiation(created.nid, "demo", None)
        return {"nid": created.nid}

    @router.get("/v1/demo/negotiations/{nid}/events", response_model=list[EventViewItem])
    async def demo_events(
        nid: str, side: Side, after_seq: int = Query(default=0, ge=0)
    ) -> list[EventViewItem]:
        """架空人物の側の見え方(§3.2。推定区間メーターなどに使う)。

        読めるのは、候補者が架空人物と分かっている交渉(デモ・攻撃)だけ。本物の利用者の交渉・存在しない
        交渉・段の状態がまだない交渉は、どれも 403(本物の依頼者の側の見え方を、ここから読めないように)。
        """
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        return await vault.get_events(nid, side, after_seq)

    return router
