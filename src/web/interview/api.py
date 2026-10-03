"""面談の API(design.md §5)。web.api の build_router が include する、別の APIRouter。画面は別に作る。JSON は素直な形にした。

本人のセッションが必要(§6.3): URL の依頼者 ID が、セッションの依頼者 ID と同じこと(違えば 403、セッションがなければ 401)。
状態を変えるリクエストは POST で、独自ヘッダ X-Requested-With が必須(ミドルウェアが確かめる)。応答には Cache-Control: no-store を付ける
(生の値が本文に入るので、共有の置き場やブラウザのディスクに残さない)。

面談の途中の状態は、サーバのメモリに依頼者 ID ごとに持つ(web.interview.state)。金庫に書くのは submit だけ。

| メソッド | パス(/v1/principals/{pid}/interview 以下) | 内容 |
|---|---|---|
| POST | /begin | 面談を始める(または続ける。restart で最初から)。最初の応答に入口の注記(notice)と設問の文面(texts)を含める |
| GET | /state | 今の状態(次にやる手順 stage など) |
| POST | /profile | 経験年数・都道府県・職種 → 属性帯(正確な値は捨てる) |
| POST | /salary/answers | 年収の正規化の 3 問の回答(自由記述)→ 換算の結果と前提(LLM。本文 32 KB まで) |
| POST | /salary/confirm | 年収の定義を確かめる(直した値を送れる) |
| GET・POST | /axes | 外せる軸の一覧・外す軸を決める(変えたら二択からやり直し) |
| GET | /choices | パッケージ二択(5〜8 組。外した軸は最悪値/「どちらでも」で見せ、設問文に書く) |
| POST | /choices/answer | 二択への回答(行く・行かない・迷う) |
| POST | /comment | 自由コメント(LLM。本文 32 KB まで) |
| POST | /reason | 辞めた理由(LLM。本文 32 KB まで。原文は持たない) |
| GET | /confirmation | 平文での確認(アンカーの一覧・矛盾・受けるアンカー 0 件の警告)。二択を 5 組答えるまで出せない |
| POST | /anchors/{key}/active | 項目を消す(active=false)・付け直す(true) |
| POST | /confirm | 確認した(受けるアンカーが 0 件なら proceed_without_accept_anchors が要る) |
| GET | /worst-case | 軸ごとの「最悪ここまで」(丸めた後のマス) |
| POST | /worst-case/approve | 「最悪ここまで」を承認した |
| GET | /companies | ブロック先に選べる企業の一覧(求人の有無は示さない) |
| POST | /blocklist | ブロック先の企業を選ぶ |
| POST | /submit | web で丸めて金庫に送る(利用記録を先に作る)。面談の状態を消す |
| POST | /discard | 面談を破棄する(途中の状態をメモリから消す) |

LLM を呼ぶ 3 つ(/salary/answers・/comment・/reason)には、入口の枠 interview_llm(web.limits。クライアント IP ごと・3 つで 1 つの枠)を、
本文を読む前に掛ける。超えたら 429(Retry-After つき。detail は {"code": "rate_limited", "entrance": "interview_llm", ...} の辞書)、数えられなければ 503。
本人のセッションの確認(401・403)のあとに数える(他人の ID への要求は数えない)。LLM を呼ばない手順(プロフィール・二択の回答など)には掛けない。
1 日の物理の数の上限(web.llm_budget)は別の歯止めで、こちらの 429 は detail が文字列 "daily_limit_reached"。

エラーの detail は理由の名前(入力の値は含めない)。検証エラー(422)は、場所・理由の種類だけを返す(web.app の既定と同じ)。
"""

import json
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError

from web.interview.bodies import (
    AxesBody,
    BeginBody,
    BlocklistBody,
    ChoiceAnswerBody,
    ConfirmBody,
    EntryActiveBody,
    ProfileBody,
    SalaryAnswersBody,
    SalaryConfirmBody,
    TextBody,
)

if TYPE_CHECKING:
    from web.services import WebServices

_Body = TypeVar("_Body", bound=BaseModel)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _require_own_principal(pid: str, request: Request) -> str:
    """URL の依頼者 ID が、セッションの依頼者 ID と同じこと(§6.3)。"""
    session = getattr(request.state, "principal_session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="no_session")
    if pid != session.principal_id:
        raise HTTPException(status_code=403, detail="forbidden")
    return pid


async def _read_limited_json(request: Request, limit: int) -> Any:
    """リクエスト本文を、上限(バイト)まで読んで JSON として返す。超えたら 413(LLM を動かさない。台帳 C-49)。

    Content-Length が上限を超えていれば、本文を読まずに断る。ないとき・偽っているときは、受け取ったバイト数を数え、超えた時点で断る。
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="payload_too_large")
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(status_code=413, detail="payload_too_large")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_json") from None


def _validated(model: type[_Body], data: Any) -> _Body:
    """body を model として検証する。違反は 422(場所・理由の種類だけ。入力の値は返さない)。"""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        detail = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        raise HTTPException(status_code=422, detail=detail) from None


def build_interview_router(services: "WebServices") -> APIRouter:
    """面談の API のルート(services.interview の面談の進行を呼ぶ)。"""
    router = APIRouter(
        prefix="/v1/principals/{pid}/interview",
        dependencies=[Depends(_no_store), Depends(_require_own_principal)],
    )

    llm_entrance = services.limiter.guard("interview_llm")  # LLM を呼ぶ 3 つの API の入口の枠(web.limits)

    def service():
        return services.interview  # テストが差し替えられるよう、リクエストごとに取り出す

    @router.post("/begin")
    async def begin(pid: str, body: BeginBody | None = None) -> dict[str, Any]:
        return service().begin(pid, body.restart if body is not None else False)

    @router.get("/state")
    async def state(pid: str) -> dict[str, Any]:
        return service().view(pid)

    @router.post("/discard")
    async def discard(pid: str) -> dict[str, str]:
        return service().discard(pid)

    @router.post("/profile")
    async def profile(pid: str, body: ProfileBody) -> dict[str, Any]:
        return service().set_profile(pid, body.experience_years, body.prefecture, body.job_category)

    @router.post("/salary/answers")
    async def salary_answers(pid: str, request: Request, _limit: None = Depends(llm_entrance)) -> dict[str, Any]:
        body = _validated(SalaryAnswersBody, await _read_limited_json(request, service().max_body_bytes))
        return await service().propose_salary(pid, body.answers)

    @router.post("/salary/confirm")
    async def salary_confirm(pid: str, body: SalaryConfirmBody) -> dict[str, Any]:
        return service().confirm_salary(pid, body.salary_basis)

    @router.get("/axes")
    async def axes(pid: str) -> dict[str, Any]:
        return service().axes_view(pid)

    @router.post("/axes")
    async def set_axes(pid: str, body: AxesBody) -> dict[str, Any]:
        return service().set_axes(pid, body.removed_axes)

    @router.get("/choices")
    async def choices(pid: str) -> dict[str, Any]:
        return service().choices_view(pid)

    @router.post("/choices/answer")
    async def choice_answer(pid: str, body: ChoiceAnswerBody) -> dict[str, Any]:
        return service().answer_choice(pid, body.pair_id, body.option, body.response)

    @router.post("/comment")
    async def comment(pid: str, request: Request, _limit: None = Depends(llm_entrance)) -> dict[str, Any]:
        body = _validated(TextBody, await _read_limited_json(request, service().max_body_bytes))
        return await service().add_statements(pid, "free_comment", body.text)

    @router.post("/reason")
    async def reason(pid: str, request: Request, _limit: None = Depends(llm_entrance)) -> dict[str, Any]:
        body = _validated(TextBody, await _read_limited_json(request, service().max_body_bytes))
        return await service().add_statements(pid, "reason_for_leaving", body.text)

    @router.get("/confirmation")
    async def confirmation(pid: str) -> dict[str, Any]:
        return service().confirmation(pid)

    @router.post("/anchors/{key}/active")
    async def entry_active(pid: str, key: str, body: EntryActiveBody) -> dict[str, Any]:
        return service().set_entry_active(pid, key, body.active)

    @router.post("/confirm")
    async def confirm(pid: str, body: ConfirmBody | None = None) -> dict[str, Any]:
        return service().confirm(pid, body.proceed_without_accept_anchors if body is not None else False)

    @router.get("/worst-case")
    async def worst_case(pid: str) -> dict[str, Any]:
        return service().worst_case(pid)

    @router.post("/worst-case/approve")
    async def approve_worst_case(pid: str) -> dict[str, Any]:
        return service().approve_worst_case(pid)

    @router.get("/companies")
    async def companies(pid: str) -> dict[str, Any]:
        return service().companies()

    @router.post("/blocklist")
    async def blocklist(pid: str, body: BlocklistBody) -> dict[str, Any]:
        return service().set_blocklist(pid, body.company_ids)

    @router.post("/submit")
    async def submit(pid: str) -> dict[str, str]:
        return await service().submit(pid)

    return router
