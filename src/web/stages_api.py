"""段階開示の API(design.md §6.2・§7・§6.3)。別の APIRouter にして、web.api の build_router から include する。

本人のセッションが必要なもの。権限は web.api と同じ(§6.3): 交渉は、セッションの依頼者が当事者であること(違えば 403。存在しない交渉も、
他人の交渉も、同じ 403)。セッションがなければ 401。状態を変える POST は、ミドルウェアが X-Requested-With を必須にする。
- GET  /v1/negotiations/{nid}/stage          候補者から見た段の状態(StageView)。見るたびに、段の状態がなければ作り(台帳 L9-4)、合意なら
                                             判定を記録して、架空の求人の自動応答を行う。判定の前は、見込みを出さない(FR-26)。
- POST /v1/negotiations/{nid}/stage/meet     「会う」。本文は {"job_summary": "..."}(匿名職務要約。前後の空白を除いて 1 文字以上
                                             [web.stages] job_summary_max_chars 文字以下。本文は [web.stages] max_request_body_bytes まで)。
                                             合意で終わった交渉だけ(判定の前は 409 not_judged、見込み「なし」は 409 not_agreed)。冪等。
- POST /v1/negotiations/{nid}/stage/approve  「承認」。段 1 が開いてから(段 0 は 409 stage_not_open)。冪等。
- GET  /v1/principals/{pid}/ledger           開示台帳(FR-38)。自分の台帳だけ(違えば 403)。
セッションを見ないもの(/v1/demo/ の下。web.api のデモ用のエンドポイントと同じ扱い。本物の依頼者には触れない):
- GET  /v1/demo/negotiations/{nid}/stage     候補者が架空人物の交渉(デモ・攻撃)の段の状態。金庫のデモ用の読み出し(正本。台帳 X-38)と、
                                             web の段の状態(補助)の両方で確かめ、どちらかが断れば 403。架空の候補者・求人の自動応答まで行う。

求人側の「会う」「承認」を押す口は作らない(§6.2: ほかの訪問者が求人側を操作する経路は作らない)。架空の求人の操作は、サーバが自動で行う
(web.stages.StageFlow)。実ユーザーの段 2 は模擬表示で、連絡先は集めない(StageView の simulated)。

ログには何も書かない(依頼者 ID・職務要約を扱うため)。
"""

from collections.abc import Awaitable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from negotiation_core.policy import StrictModel

from vault.api_models import PrincipalNegotiationSummary

from web.ledger import LedgerEntry
from web.services import WebServices
from web.session import PrincipalSession
from web.stages import StageBusy, StageRefused, StagesConfig, StageView
from web.vault_client import VaultNotFoundError

_T = TypeVar("_T")


class MeetBody(StrictModel):
    """候補者の「会う」。匿名職務要約(氏名・勤務先・連絡先など、個人が特定できることは書かない。画面で示す)を添える。"""

    job_summary: str


async def _read_limited(request: Request, limit: int) -> bytes:
    """リクエスト本文を、limit バイトまでで読む。超えたら、読み切らずに 413。"""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="request_too_large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_job_summary(request: Request, config: StagesConfig) -> str:
    """「会う」の本文から、匿名職務要約を取り出す。エラーの文に、入力の値は入れない。"""
    raw = await _read_limited(request, config.max_request_body_bytes)
    try:
        body = MeetBody.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid_body") from None
    job_summary = body.job_summary.strip()
    if not job_summary or len(job_summary) > config.job_summary_max_chars:
        raise HTTPException(status_code=422, detail="job_summary_invalid")
    return job_summary


async def _translated(call: Awaitable[_T]) -> _T:
    """段階開示の例外を HTTP にする: 受け付けない操作は 409(reason が detail)、競合で書けなかったときは 503(呼び直してよい)。"""
    try:
        return await call
    except StageRefused as refused:
        raise HTTPException(status_code=409, detail=refused.reason) from None
    except StageBusy:
        raise HTTPException(status_code=503, detail="temporarily_unavailable") from None


def build_stages_router(services: WebServices) -> APIRouter:
    """services の部品を使う、段階開示のルートを作る。"""
    router = APIRouter()
    flow = services.stage_flow
    vault = services.vault

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

    async def own_negotiation(
        nid: str, session: PrincipalSession = Depends(require_session)
    ) -> PrincipalNegotiationSummary:
        """URL の交渉 ID が、セッションの依頼者が当事者の交渉であること(金庫の一覧で確かめる)。その交渉の一覧の 1 件を返す。"""
        for summary in await vault.list_principal_negotiations(session.principal_id):
            if summary.nid == nid:
                return summary
        raise HTTPException(status_code=403, detail="forbidden")

    @router.get("/v1/negotiations/{nid}/stage", response_model=StageView)
    async def get_stage(
        summary: PrincipalNegotiationSummary = Depends(own_negotiation),
        session: PrincipalSession = Depends(require_session),
    ) -> StageView:
        """候補者から見た段の状態。"""
        return await _translated(flow.view(flow.facts_for_principal(summary, session.principal_id)))

    @router.post("/v1/negotiations/{nid}/stage/meet", response_model=StageView)
    async def meet(
        request: Request,
        summary: PrincipalNegotiationSummary = Depends(own_negotiation),
        session: PrincipalSession = Depends(require_session),
    ) -> StageView:
        """「会う」。本物の候補者は、匿名職務要約を書く(段 1 で求人側に出す)。"""
        job_summary = await _read_job_summary(request, flow.config)
        return await _translated(flow.meet(flow.facts_for_principal(summary, session.principal_id), job_summary))

    @router.post("/v1/negotiations/{nid}/stage/approve", response_model=StageView)
    async def approve(
        summary: PrincipalNegotiationSummary = Depends(own_negotiation),
        session: PrincipalSession = Depends(require_session),
    ) -> StageView:
        """「承認」(段 2 で氏名と連絡先を出す)。"""
        return await _translated(flow.approve(flow.facts_for_principal(summary, session.principal_id)))

    @router.get("/v1/principals/{pid}/ledger", response_model=list[LedgerEntry])
    async def get_ledger(pid: str, session: PrincipalSession = Depends(require_own_principal)) -> list[LedgerEntry]:
        """開示台帳(FR-38): 段の遷移の記録を、時系列で全件。生の値(職務要約の本文・氏名・連絡先)は含まない。"""
        return await services.ledger.list_entries(pid)

    @router.get("/v1/demo/negotiations/{nid}/stage", response_model=StageView)
    async def demo_stage(nid: str) -> StageView:
        """候補者が架空人物の交渉(デモ・攻撃)の段の状態。確認は 2 段: 金庫のデモ用の読み出し(正本)と、web の段の状態(補助)。"""
        try:
            events = await vault.get_demo_events(nid, "candidate")
        except VaultNotFoundError:
            raise HTTPException(status_code=403, detail="forbidden") from None
        await services.stages.ensure(nid, None)  # 金庫が架空の候補者の交渉と認めた交渉だけ。作り損ねていれば作る(冪等)
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        return await _translated(flow.view(flow.facts_for_fictional(nid, events)))

    return router
