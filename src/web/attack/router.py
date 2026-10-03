"""攻撃モードと 3 枚の壁の API(design.md §8.1・§8.2・§8.3 の冒頭。台帳 C-1・C-3・C-9・L7-3・P-17)。

パスは `/v1/demo/attack/...`。デモ用のエンドポイントの下に置くので(web.api.DEMO_PATH_PREFIX)、依頼者のセッションを見ず
(ミドルウェアが除く)、依頼者 ID も作らない。状態を変える POST は X-Requested-With が必須(ミドルウェア。§6.3)。訪問者を見分ける ID は
作らず(台帳 L7-3)、攻撃画面は、自分が作った交渉の ID を画面の中(メモリ)だけに持つ。

| メソッドとパス | 内容 | 入口の枠(web.limits) |
|---|---|---|
| POST /v1/demo/attack/negotiations | 攻撃モードの交渉を作る。相手は架空人物(設定 [web.attack] のテンプレート)に決め打ち。本文は {request_id, instruction}。指示は 400 文字まで。web のメモリにだけ持つ | attack_create |
| POST /v1/demo/attack/negotiations/{nid}/instruction | 動いている攻撃の交渉の指示を置き換える(攻撃の手。次の手番から効く)。本文は {instruction} | attack_instruction |
| GET /v1/demo/attack/negotiations/{nid}/events?side=&after_seq= | 攻撃の交渉のイベント。side は既定で employer(攻撃側の見え方)。candidate は架空の候補者側(金庫の答え。メーターの元) | なし |
| GET /v1/demo/attack/walls/1/example | 壁 1 の初期値(生のメッセージの JSON) | なし |
| POST /v1/demo/attack/walls/1 | 壁 1: 本文の JSON を、そのまま /a2a/candidate へ送る(32 KB まで)。金庫には登録しない | raw_message |
| GET /v1/demo/attack/walls/2/{nid} | 壁 2: 候補者側エージェントの直近の手番の LLM の文脈の全文 | なし |
| GET /v1/demo/attack/walls/3/{nid} | 壁 3: 攻撃者の提案ごとの、金庫の答え(丸め済みの 3 値) | なし |

- 入口の枠を超えたら 429(Retry-After つき。web.limits)。数えられなければ 503。レート制限は本文を読む前に行う
  (本文を FastAPI に読ませず、ここで上限つきで読む)。
- 攻撃の指示は、400 文字以内(AttackerTurnInput の上限)。本文は 32 KB 以内([web.attack] max_body_bytes。超えたら 413)。
  指示はエラーにもログにも書かない(エラーは場所と理由の種類だけ)。
- 読み出し(events・壁 2・壁 3)は、候補者が架空人物の交渉(デモ・攻撃)だけ。本物の利用者の交渉・存在しない交渉・段の状態が
  まだない交渉は、どれも 403(web.api.demo_events と同じ 2 段の確認: web の段の状態と、金庫のデモ用の読み出しの口)。
- 作成は、web が相手を決める(設定のテンプレート)。依頼者の ID・モード・相手のテンプレートは、リクエストに含められない
  (extra=forbid)。金庫も、攻撃モードの交渉は架空人物の候補者でしか作らない(作成の検証)。
"""

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import StringConstraints, ValidationError

from agents.instructions import load_instruction
from negotiation_core import Side
from negotiation_core.policy import StrictModel

from vault.api_models import (
    CandidateParticipantRequest,
    CreateNegotiationRequest,
    EmployerParticipantRequest,
    EventViewItem,
)
from vault.models import NegotiationMode

from web.attack.raw_message import describe_failure, describe_reply, describe_web_refusal, example_raw_message
from web.attack.walls import llm_context_report, vault_answers_report
from web.llm_budget import LlmBudgetUnavailable
from web.vault_client import VaultNotFoundError

if TYPE_CHECKING:  # web.services が web.attack を import するので、型のためだけに読む(循環を避ける)
    from web.services import WebServices

# 攻撃の指示の長さの上限(文字数)。negotiation_core.AttackerTurnInput.principal_instruction の上限と同じ(テストで一致を確かめる)。
MAX_INSTRUCTION_CHARS = 400

_REQUEST_ID = Annotated[str, StringConstraints(min_length=8, max_length=64)]
_INSTRUCTION = Annotated[str, StringConstraints(min_length=1, max_length=MAX_INSTRUCTION_CHARS)]

_M = TypeVar("_M", bound=StrictModel)

AdmitNewNegotiation = Callable[[], Awaitable[None]]
RegisterCreatedNegotiation = Callable[[str, NegotiationMode, str | None], Awaitable[None]]


class AttackCreateBody(StrictModel):
    """攻撃モードの交渉を作る。request_id は画面が作る乱数(§3.5。再送で同じ交渉を返す)、instruction は攻撃の指示(自由文)。"""

    request_id: _REQUEST_ID
    instruction: _INSTRUCTION


class InstructionBody(StrictModel):
    """動いている攻撃の交渉の指示を置き換える。"""

    instruction: _INSTRUCTION


class _BodyTooLarge(Exception):
    """本文が上限を超えた。received は、読んだ(または宣言された)バイト数。"""

    def __init__(self, received: int) -> None:
        super().__init__("request body is too large")
        self.received = received


async def _read_body(request: Request, max_bytes: int) -> bytes:
    """本文を max_bytes までだけ読む。宣言された長さが超えていれば、読まずに断る。超えた時点で読むのをやめる(メモリを使い切らせない)。"""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise _BodyTooLarge(int(declared))
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise _BodyTooLarge(received)
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_model(request: Request, model: type[_M], max_bytes: int) -> _M:
    """本文(上限つき)を model として読む。大きすぎれば 413、形の違反は 422(場所と理由の種類だけ。入力の値は返さない)。"""
    try:
        raw = await _read_body(request, max_bytes)
    except _BodyTooLarge:
        raise HTTPException(status_code=413, detail="body_too_large") from None
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        raise HTTPException(
            status_code=422,
            detail=[{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in errors],
        ) from None


def _reject_json_constant(name: str) -> Any:
    """NaN・Infinity は JSON ではない(Python の json は受け付けてしまう)。"""
    raise ValueError(f"{name} is not valid JSON")


def build_attack_router(
    services: "WebServices",
    admit_new_negotiation: AdmitNewNegotiation,
    register_created_negotiation: RegisterCreatedNegotiation,
) -> APIRouter:
    """攻撃モードと 3 枚の壁のルートを作る。

    admit_new_negotiation(入場の制限。web.api の共通のもの)と register_created_negotiation(段の状態の作成とレフェリーの起動)は、
    ライブ・デモの作成と同じものを使う(二重に書かない)。
    """
    router = APIRouter()
    vault = services.vault
    attack = services.attack
    config = attack.config
    limit = services.limiter.guard

    async def fictional_events(nid: str, side: Side, after_seq: int = 0) -> list[EventViewItem]:
        """架空人物の側の見え方。web の段の状態(補助)と、金庫のデモ用の読み出しの口(正本)の両方で確かめ、どちらかが断れば 403。"""
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await vault.get_demo_events(nid, side, after_seq)
        except VaultNotFoundError:
            raise HTTPException(status_code=403, detail="forbidden") from None

    # ------------------------------------------------------------------
    # 攻撃モードの交渉
    # ------------------------------------------------------------------

    @router.post("/v1/demo/attack/negotiations")
    async def create_attack_negotiation(
        request: Request, _limit: None = Depends(limit("attack_create"))
    ) -> dict[str, str]:
        """攻撃モードの交渉を作る(§8.2)。相手は、設定の架空人物のテンプレートから写したコピー(§3.7)。"""
        body = await _read_model(request, AttackCreateBody, config.max_body_bytes)
        request_id = f"attack:{body.request_id}"
        known = await vault.get_negotiation_by_request(request_id)
        if known is not None:  # 同じ request_id の再送。入場の制限を通さずに、同じ交渉を返す(指示は変えない。台帳 X-57)
            await register_created_negotiation(known, "attack", None)
            return {"nid": known}
        await admit_new_negotiation()
        created = await vault.create_negotiation(
            CreateNegotiationRequest(
                request_id=request_id,
                mode="attack",
                candidate=CandidateParticipantRequest(is_fictional=True, template_id=config.candidate_template_id),
                employer=EmployerParticipantRequest(template_id=config.employer_template_id),
            )
        )
        if created.status == "refused" or created.nid is None:
            raise HTTPException(status_code=409, detail=created.reason or "refused")
        # 指示は、レフェリーを動かす前にメモリへ入れる(攻撃者の手番までに必要。メモリにだけ持つ。台帳 P-17)
        attack.contexts.add(created.nid, body.instruction)
        await register_created_negotiation(created.nid, "attack", None)
        return {"nid": created.nid}

    @router.post("/v1/demo/attack/negotiations/{nid}/instruction")
    async def update_instruction(
        nid: str, request: Request, _limit: None = Depends(limit("attack_instruction"))
    ) -> dict[str, str]:
        """動いている攻撃の交渉の指示を置き換える(攻撃の手)。次の手番から、新しい指示が攻撃者に渡る。"""
        body = await _read_model(request, InstructionBody, config.max_body_bytes)
        if nid not in attack.contexts:
            raise HTTPException(status_code=404, detail="unknown_attack_negotiation")  # この web が持っていない(再起動で消えた)
        try:
            view = await vault.get_view(nid, "employer")
        except VaultNotFoundError:
            raise HTTPException(status_code=404, detail="unknown_attack_negotiation") from None
        if view.status == "judged":
            raise HTTPException(status_code=409, detail="negotiation_ended")
        attack.contexts.set_instruction(nid, body.instruction)
        return {"status": "ok"}

    @router.get("/v1/demo/attack/negotiations/{nid}/events", response_model=list[EventViewItem])
    async def attack_events(
        nid: str, side: Side = "employer", after_seq: int = Query(default=0, ge=0)
    ) -> list[EventViewItem]:
        """攻撃の交渉のイベント。既定は攻撃側(employer)の見え方。candidate は架空の候補者側(金庫の答え。メーターの元。§8.3)。"""
        return await fictional_events(nid, side, after_seq)

    # ------------------------------------------------------------------
    # 3 枚の壁の実演(§8.1)
    # ------------------------------------------------------------------

    @router.get("/v1/demo/attack/walls/1/example")
    async def raw_message_example() -> dict[str, Any]:
        """壁 1 の初期値。TextPart と principal_instruction が入っているので、そのまま送ると拒否される。"""
        return {"limit_bytes": config.max_body_bytes, "message": example_raw_message()}

    @router.post("/v1/demo/attack/walls/1")
    async def send_raw_message(request: Request, _limit: None = Depends(limit("raw_message"))) -> JSONResponse:
        """壁 1: 本文の JSON(A2A のメッセージ)を、そのまま /a2a/candidate へ送り、止まった場所と理由を返す。金庫には登録しない。"""
        try:
            raw = await _read_body(request, config.max_body_bytes)
        except _BodyTooLarge as exc:
            return JSONResponse(
                describe_web_refusal("body_too_large", exc.received, limit_bytes=config.max_body_bytes), status_code=413
            )
        try:
            text = raw.decode("utf-8")
            message = json.loads(text, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return JSONResponse(describe_web_refusal("invalid_json", len(raw)), status_code=422)
        if not isinstance(message, dict):
            return JSONResponse(describe_web_refusal("not_a_json_object", len(raw)), status_code=422)
        if attack.send_raw is None:
            raise HTTPException(status_code=503, detail="raw_message_unavailable")
        # LLM が動くかもしれない呼び出しなので、送る前に 1 日の物理の呼び出し数を数える(§8.2。有効でなくても 1 と数える)
        try:
            reservation = await services.llm_budget.reserve(None)
        except LlmBudgetUnavailable:
            raise HTTPException(status_code=503, detail="temporarily_unavailable") from None
        if not reservation.granted:
            raise HTTPException(status_code=429, detail="daily_limit_reached")
        try:
            reply = await attack.send_raw(text.strip(), timeout_s=services.config.referee.agent_call_timeout_seconds)
        except TimeoutError:
            return JSONResponse(describe_failure("agent_timeout", len(raw)), status_code=504)
        except ConnectionError:
            return JSONResponse(describe_failure("agent_unreachable", len(raw)), status_code=502)
        return JSONResponse(describe_reply(reply, len(raw)))

    @router.get("/v1/demo/attack/walls/2/{nid}")
    async def llm_context(nid: str) -> dict[str, Any]:
        """壁 2: 候補者側エージェントの直近の手番の、LLM の文脈の全文(固定の前文＋TurnInput。計画と決定)。"""
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        texts = attack.llm_context.latest(nid)
        if not texts:
            raise HTTPException(status_code=404, detail="no_llm_context")  # まだ手番が来ていない・再起動で消えた
        return llm_context_report(load_instruction("candidate"), texts)

    @router.get("/v1/demo/attack/walls/3/{nid}")
    async def vault_answers(nid: str) -> dict[str, Any]:
        """壁 3: 攻撃者の提案ごとの、候補者側の金庫の答え(丸め済みの 3 値だけ)。"""
        return vault_answers_report(await fictional_events(nid, "candidate"))

    return router
