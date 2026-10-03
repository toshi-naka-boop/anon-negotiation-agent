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
  架空人物のテンプレートからだけ作り(モードは demo 固定)、読めるのは、候補者が架空人物の交渉(デモ・攻撃)
  だけ。読み出しは、web の段の状態(stages。補助)と、金庫のデモ用の読み出しの口(正本。台帳 X-38)の両方で確かめ、
  どちらかが断れば 403。金庫の側でも、demo・attack の交渉は本物の依頼者を持てない(作成の検証)。

金庫に書く前に、利用記録 principals_meta がなければならない(面談の送信が作る。§5 の手順 9)。
ブロックリストの登録と交渉の作成は、面談を送っていない(利用記録がない)依頼者には 409 で断る。

交渉の作成(ライブ・デモ。§8.2・§3.3。台帳 C-45・X-53・X-57)は、金庫に作る前に、次の順で確かめる。
1. 金庫の by-request(冪等キーの正本)を引き、既知のキーなら、作成も入場の制限もせずに、同じ交渉を返す(再送は、起動時の 503 と
   入場の制限より先に通す)。
2. 起動時の見回りが 1 回終わるまでは、新規の作成を受け付けない(503)。
3. 入場の制限: 「その日の物理の数 ＋ 進行中の交渉の未消化分 ＋ 新しい交渉 1 件ぶんの上限」が 1 日の枠を超えるなら、429
   (画面は「本日の上限に達しました」と出す)。数えられないとき(Firestore の失敗)は 503(閉じる側)。
入場の制限は読むだけで、何も書かない(予約の記録を持たない)。二度押し・再送・金庫の拒否(already_active など)は、枠を消費しない。

TEE モード(build_router に tee を渡したときだけ。契約 research/tee-spike-contract.md §8): GET /api/tee/attestation?nonce= が、
金庫の attestation の検証結果(検証したか・理由・claims・GitHub のコミット・トークン)を返す。公開情報だけなので、セッションを
見ない。nonce つきの転送は、クライアント IP(web.client_ip)ごとに 10 秒に 1 回 ＋ 全体で 2 秒に 1 回に制限する(契約 §19・台帳 L18-5)。
TEE モードでなければ、このルートはなく、404。

ログには、例外の型名だけを書く(組み合わせの値・クッキー・依頼者の入力は書かない)。
"""

import asyncio
import datetime as dt
import logging
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from negotiation_core import Side
from negotiation_core.attestation import (
    UNAVAILABLE,
    AttestationError,
    VerifiedAttestation,
    decode_claims_unverified,
    release_for_digest,
    summarize_claims,
)

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
from web.attested_transport import AttestationSource
from web.client_ip import client_ip
from web.deletion import DeletionOutcome
from web.llm_budget import LlmBudgetUnavailable
from web.referee import NegotiationContext
from web.services import WebServices
from web.session import PrincipalSession
from web.vault_client import VaultNotFoundError

_log = logging.getLogger(__name__)

# デモ用のエンドポイントのパス。ミドルウェアは、ここでは依頼者のセッションを見ない(§6.3)。
DEMO_PATH_PREFIX = "/v1/demo/"

# TEE モードの attestation の口(契約 §8)。公開情報だけなので、ミドルウェアは、ここでも依頼者のセッションを見ない。
TEE_PATH_PREFIX = "/api/tee/"
TEE_ATTESTATION_PATH = "/api/tee/attestation"
_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,74}")  # 契約 §2。launcher の制限(1 個 10〜74 バイト)に収まる
# nonce を指定した要求を、金庫へ転送する最短の間隔(契約 §19・台帳 L18-5)。金庫の発行枠(毎秒 1 回。web の検証し直しなどと共有)を、
# 匿名の利用者が使い切れないように、クライアント IP ごとに 10 秒に 1 回 ＋ 全体で 2 秒に 1 回にする。全体の枠だけだと、匿名の 1 人が
# 枠を独占して、審査員の確認(scripts/verify_attestation.py --web。AC-23)が 429 になってしまう。
_FORWARD_INTERVAL_PER_CLIENT = dt.timedelta(seconds=10)
_FORWARD_INTERVAL_OVERALL = dt.timedelta(seconds=2)
# 金庫に届かず、トークンが取れなかった(nonce なしの)結果を、返し続ける時間。金庫が応えない間に、匿名の要求が次々と金庫の発行枠を使わないように。
_UNAVAILABLE_RESULT_LIFETIME = dt.timedelta(seconds=10)
_RESULT_LIFETIME = dt.timedelta(minutes=5)  # nonce なしの要求に返す、直近の結果を覚えておく時間(Google の 5 QPS 制限を守る)


@dataclass(frozen=True)
class TeeAttestationConfig:
    """TEE モードの設定(create_app・build_router に渡す)。

    transport: 金庫の attestation を、ピン留めした接続で検証する口(本番は web.attested_transport.AttestedVaultTransport)。
    releases: digest → コミットの表(deploy/vault-releases.json。negotiation_core.attestation.load_releases)。
    github_repo_url: コミットのリンクの土台(例 https://github.com/<owner>/<repo>)。なければ、リンクは null。
    """

    transport: AttestationSource
    releases: Sequence[Mapping[str, Any]]
    github_repo_url: str | None = None


class _TeeAttestationEndpoint:
    """GET /api/tee/attestation の中身(契約 §8)。転送の間隔の制限と、nonce なしの結果の保持を持つ。

    - nonce あり(契約 §2 の形。違えば 400): 金庫へ転送して検証する。同じクライアント IP の前回の転送から 10 秒未満、または、全体の前回の
      転送から 2 秒未満なら 429(契約 §19・台帳 L18-5。拒否した要求は、どちらの枠にも数えない)。クライアント IP は web.client_ip.client_ip
      (X-Forwarded-For の最後の要素)。状態はメモリに持つ(web は 1 インスタンス)。
    - nonce なし: 直近 5 分以内の結果があれば、それを返す。なければ、新しい nonce で 1 回だけ検証する(同時の要求は 1 回にまとめる。
      この転送は、全体の枠の時刻だけを進める)。金庫に届かず、トークンが取れなかった結果は、短く(10 秒だけ)覚える: 金庫が応えない間に、
      匿名の要求が次々と金庫の発行枠を使わないように。
    失敗のときも、取れた範囲で claims を返す(verified=false、reason)。digest が表(releases)にない・失効(status=revoked)しているものは、
    検証が通っていても verified=false(reason=image_digest)にする。release は、検証が通ったときと、reason=image_digest のとき
    (署名・期限・本番かどうかなどは通った後なので、claims は信用できる)だけ、表から引く(失効は release.status で分かる)。
    """

    def __init__(self, config: TeeAttestationConfig, clock) -> None:
        self._transport = config.transport
        self._releases = config.releases
        self._repo_url = config.github_repo_url.rstrip("/") if config.github_repo_url else None
        self._clock = clock
        self._forwarded_at: dt.datetime | None = None  # 全体の、金庫への前回の転送の時刻(nonce なしの転送も含む)
        self._forwarded_by_client: dict[str, dt.datetime] = {}  # クライアント IP ごとの、nonce つきの前回の転送の時刻
        self._cached: tuple[dt.datetime, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    async def respond(self, nonce: str | None, client: str) -> dict[str, Any]:
        """nonce つき(client は、要求を送ってきたクライアントの IP)は、制限を通れば金庫へ転送する。nonce なしは、保持した結果を返す。"""
        if nonce is not None:
            if not _NONCE_PATTERN.fullmatch(nonce):
                raise HTTPException(status_code=400, detail="invalid_nonce")
            self._admit_forward(client)
            return (await self._check(nonce))[1]
        async with self._lock:
            now = self._clock.now()
            if self._cached is not None and now - self._cached[0] < self._lifetime(self._cached[1]):
                return self._cached[1]
            self._forwarded_at = now
            checked_at, body = await self._check(secrets.token_urlsafe(32))
            self._cached = (checked_at, body)
            return body

    def _admit_forward(self, client: str) -> None:
        """nonce つきの要求を金庫へ転送してよいか。クライアントごとに 10 秒に 1 回 ＋ 全体で 2 秒に 1 回(契約 §19)。だめなら 429。

        通すときだけ、転送の時刻を両方の枠に記録する(拒否した要求は、数えない)。確かめから記録まで await をはさまないので、同時の要求が
        同じ枠を二重に通ることはない。
        """
        now = self._clock.now()
        # 10 秒たったクライアントの記録は消す(全体で 2 秒に 1 回なので、残るのは多くても 5 件)
        self._forwarded_by_client = {
            address: at for address, at in self._forwarded_by_client.items() if now - at < _FORWARD_INTERVAL_PER_CLIENT
        }
        if client in self._forwarded_by_client:
            raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": "10"})
        if self._forwarded_at is not None and now - self._forwarded_at < _FORWARD_INTERVAL_OVERALL:
            raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": "2"})
        self._forwarded_by_client[client] = now
        self._forwarded_at = now

    @staticmethod
    def _lifetime(body: dict[str, Any]) -> dt.timedelta:
        """結果を返し続ける時間。トークンが取れた結果は 5 分。金庫に届かなかった結果は、10 秒だけ。"""
        return _RESULT_LIFETIME if body["token"] is not None else _UNAVAILABLE_RESULT_LIFETIME

    async def _check(self, nonce: str) -> tuple[dt.datetime, dict[str, Any]]:
        verified: VerifiedAttestation | None = None
        token: str | None = None
        reason: str | None = None
        try:
            token, verified = await self._transport.attest(nonce)
        except AttestationError as exc:
            token, reason = exc.token, exc.reason
        except (httpx.HTTPError, OSError):  # 金庫に届かない(トークンなし)
            reason = UNAVAILABLE
        claims = summarize_claims(_claims_for_display(token, verified))
        release = None
        if claims["image_digest"] is not None and (verified is not None or reason == "image_digest"):
            release = release_for_digest(list(self._releases), claims["image_digest"])
        if verified is not None and (release is None or release.get("status", "active") != "active"):
            # 金庫の許可リストと表が食い違っても、表にない・失効した digest は「検証した」と言わない
            verified, reason = None, "image_digest"
        checked_at = self._clock.now()
        return checked_at, {
            "verified": verified is not None,
            "reason": reason,
            "checked_at": checked_at.isoformat(),
            "nonce": nonce,
            "certificate_sha256": self._transport.certificate_sha256,
            "claims": claims,
            "release": self._release_view(release) if release is not None else None,
            "token": token,
        }

    def _release_view(self, release: Mapping[str, Any]) -> dict[str, Any]:
        commit = release["commit"]
        return {
            "commit": commit,
            "url": f"{self._repo_url}/commit/{commit}" if self._repo_url else None,
            "built_at": release.get("built_at"),
            "status": release.get("status", "active"),
        }


def _claims_for_display(token: str | None, verified: VerifiedAttestation | None) -> Mapping[str, Any]:
    """画面に出す claims の元。検証が通れば検証済みの claims、通らなければ(署名を確かめていない)トークンの本文、読めなければ空。"""
    if verified is not None:
        return verified.claims
    if token is None:
        return {}
    try:
        return decode_claims_unverified(token)
    except AttestationError:
        return {}


def build_router(services: WebServices, tee: TeeAttestationConfig | None = None) -> APIRouter:
    """services の部品を使うルートを作る。tee を渡すと(TEE モード)、GET /api/tee/attestation も作る。"""
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

    async def admit_new_negotiation() -> None:
        """新しい交渉(ライブ・デモ)を受け付けてよいか(入場の制限。§8.2)。受け付けられなければ HTTPException。"""
        if not services.sweeper.first_sweep_done:
            raise HTTPException(status_code=503, detail="starting_up")  # 進行中の交渉の一覧が、まだそろっていない
        try:
            admitted = await services.llm_budget.admits_new_negotiation(services.referees.running_nids())
        except LlmBudgetUnavailable:
            raise HTTPException(status_code=503, detail="temporarily_unavailable") from None
        if not admitted:
            raise HTTPException(status_code=429, detail="daily_limit_reached")

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
        request_id = f"{pid}:{body.request_id}"  # 依頼者ごとの名前空間(他人の交渉 ID を返されない)
        known = await vault.get_negotiation_by_request(request_id)
        if known is not None:  # 同じ request_id の再送。入場の制限を通さずに、同じ交渉を返す(台帳 X-57)
            await register_created_negotiation(known, "live", pid)
            return {"nid": known}
        await admit_new_negotiation()
        created = await vault.create_negotiation(
            CreateNegotiationRequest(
                request_id=request_id,
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
        request_id = f"demo:{body.request_id}"
        known = await vault.get_negotiation_by_request(request_id)
        if known is not None:  # 同じ request_id の再送。入場の制限を通さずに、同じ交渉を返す(台帳 X-57)
            await register_created_negotiation(known, "demo", None)
            return {"nid": known}
        await admit_new_negotiation()
        created = await vault.create_negotiation(
            CreateNegotiationRequest(
                request_id=request_id,
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

        読めるのは、候補者が架空人物の交渉(デモ・攻撃)だけ。本物の利用者の交渉・存在しない交渉・段の状態が
        まだない(または項目が欠けた)交渉は、どれも 403(本物の依頼者の側の見え方を、ここから読めないように)。
        確認は 2 段: web の段の状態(補助)と、金庫のデモ用の読み出しの口(正本。mode が demo・attack で、候補者が
        架空人物のときだけ返す)。段の状態が壊れていても、金庫が本物の交渉を断る(404 を 403 に写す。台帳 X-38)。
        """
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await vault.get_demo_events(nid, side, after_seq)
        except VaultNotFoundError:
            raise HTTPException(status_code=403, detail="forbidden") from None

    # ------------------------------------------------------------------
    # TEE モード: 金庫の attestation の検証結果(セッションを見ない。公開情報だけ。契約 §8)
    # ------------------------------------------------------------------

    if tee is not None:
        endpoint = _TeeAttestationEndpoint(tee, services.clock)

        @router.get(TEE_ATTESTATION_PATH)
        async def tee_attestation(request: Request, nonce: str | None = Query(default=None)) -> dict[str, Any]:
            """金庫の attestation を検証した結果。nonce を渡すと、その nonce で金庫に確かめさせる(クライアントごとに 10 秒に 1 回まで)。"""
            return await endpoint.respond(nonce, client_ip(request))

    return router
