"""金庫の内部 HTTP・JSON API(design.md §3.3。呼べるのは web だけという前提)。

ここでは FastAPI のルーティングと、VaultStore の例外を HTTP ステータスへ変換するだけを行う。
秘密に触れる判断はすべて VaultStore(と、その先の negotiation_core)が行う。

サービス間の認証は、Cloud Run の IAM(認証を必須にし、呼べるのは web のサービスアカウントだけ。§1.1)に任せ、
アプリの中ではトークンを検証しない(台帳 X-37)。呼ぶ側(web)が、金庫の URL を audience にした ID トークンを付ける
(web.service_auth)。
TEE(Confidential Space)版では Cloud Run の IAM が効かないので、create_app に caller_verifier(vault.tee.caller_auth の依存)を渡し、
アプリの中で ID トークンを検証する。attestation(vault.tee.attestation_api)を渡すと、認証なしの `GET /v1/attestation` を付ける(§9)。
`GET /health`(死活確認。AC-22)は、常に、認証なしの素の経路として付ける(caller_verifier の依存の外。attestation と同じ)。

本番の起動口は create_app_from_env(`uvicorn vault.app:create_app_from_env --factory`)。起動時に、uvicorn のアクセスログの
URL から ID(依頼者 ID・交渉 ID)を伏せる(§3.8。台帳 X-40)。伏せる処理は web の起動口と共通で、negotiation_core にある
(vault は web に依存しない)。あわせて、架空人物のテンプレートを、イメージ内の fixtures/ から vault-db に投入する(§3.7。
vault.seed。台帳 P-15。環境変数 VAULT_SEED_TEMPLATES=false で切れる)。
"""

import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore

from negotiation_core import Side
from negotiation_core.log_privacy import mask_ids_in_logs

from vault.api_models import (
    ControlRequest,
    ControlResponse,
    CreateNegotiationRequest,
    CreateNegotiationResponse,
    EventViewItem,
    ExpireResponse,
    MoveRequest,
    MoveResponse,
    NegotiationByRequestResponse,
    NegotiationViewResponse,
    OpenNegotiationsPage,
    PolicyView,
    PrincipalAnswerRequest,
    PrincipalAnswerResponse,
    PrincipalNegotiationSummary,
    PutBlocklistRequest,
    PutPolicyRequest,
)
from vault.clock import SystemClock
from vault.config import DEFAULT_VAULT_CONFIG
from vault.errors import (
    MovePreconditionFailed,
    NotFoundError,
    PolicyValidationError,
    PrincipalDeletingError,
    TransactionRetryExhausted,
)
from vault.firestore_client import create_client
from vault.fixtures import FIXTURES_DIRECTORY
from vault.seed import seed_templates
from vault.store import VaultStore

if TYPE_CHECKING:
    from vault.tee.attestation_api import AttestationService

SEED_TEMPLATES_ENV = "VAULT_SEED_TEMPLATES"


async def _healthz(request: Request) -> JSONResponse:
    """死活確認(AC-22)。認証なしで 200 {"status":"ok"}。ストレージには触れない。"""
    return JSONResponse({"status": "ok"})


def create_app(
    store: VaultStore,
    *,
    caller_verifier: Callable[..., object] | None = None,
    attestation: "AttestationService | None" = None,
) -> FastAPI:
    """VaultStore を注入した FastAPI アプリを作る(1b-1 は uvicorn を起動しない。
    テストは TestClient から直接叩く)。

    caller_verifier と attestation は TEE 版だけが渡す(どちらも省けば、Cloud Run 版と同じ。契約 §9・§3)。
    - caller_verifier: FastAPI の依存。アプリ全体に掛けるので、このあとの `@app.get` などで足す経路にも掛かる(足し忘れで認証が
      抜けない)。呼び出し元の ID トークンの検証を通らなければ 401・403。FastAPI の依存の外にある /docs・/openapi.json は出さない。
    - attestation: `GET /v1/attestation` を付ける。素の Starlette の経路なので、caller_verifier の依存は掛からない
      (web は、この口の応答を確かめるまで金庫を信用しないので、ID トークンをまだ送らない)。
    """
    if caller_verifier is None:
        app = FastAPI(title="vault")
    else:
        app = FastAPI(
            title="vault",
            dependencies=[Depends(caller_verifier)],
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
    app.add_route("/health", _healthz, methods=["GET"])  # caller_verifier の依存の外(素の経路)。TEE 版でも認証なしで通る
    if attestation is not None:
        app.add_route("/v1/attestation", attestation.endpoint, methods=["GET"])

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(MovePreconditionFailed)
    async def _precondition_failed(request: Request, exc: MovePreconditionFailed) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PrincipalDeletingError)
    async def _principal_deleting(request: Request, exc: PrincipalDeletingError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PolicyValidationError)
    async def _policy_invalid(request: Request, exc: PolicyValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(TransactionRetryExhausted)
    async def _retry_exhausted(request: Request, exc: TransactionRetryExhausted) -> JSONResponse:
        # 冪等な操作(control・expire)と作成が競合で再試行を使い切ったとき。409 にはしない
        # (DV-02)。呼び出し側は再試行してよい。
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # --- §3.3: PUT/GET .../policy ---

    @app.put("/v1/principals/{pid}/policy", status_code=204)
    def put_policy(pid: str, body: PutPolicyRequest) -> None:
        store.put_policy(pid, body)

    @app.get("/v1/principals/{pid}/policy", response_model=PolicyView)
    def get_policy(pid: str) -> PolicyView:
        return store.get_policy(pid)

    # --- §3.3: PUT .../blocklist ---

    @app.put("/v1/principals/{pid}/blocklist", status_code=204)
    def put_blocklist(pid: str, body: PutBlocklistRequest) -> None:
        store.put_blocklist(pid, body)

    # --- §3.3・§3.8: DELETE 本人のデータを消す(冪等。すでに消えていても成功) ---

    @app.delete("/v1/principals/{pid}", status_code=204)
    def delete_principal(pid: str) -> None:
        store.delete_principal(pid)

    # --- §3.3: 本人の交渉一覧 ---

    @app.get("/v1/principals/{pid}/negotiations", response_model=list[PrincipalNegotiationSummary])
    def list_principal_negotiations(pid: str) -> list[PrincipalNegotiationSummary]:
        return store.list_principal_negotiations(pid)

    # --- §3.3: 交渉の作成、見回り用の一覧 ---

    @app.post("/v1/negotiations", response_model=CreateNegotiationResponse)
    def create_negotiation(body: CreateNegotiationRequest) -> CreateNegotiationResponse:
        return store.create_negotiation(body)

    # --- §3.3: 作成の冪等キーから交渉の nid を引く(台帳 X-57。なければ 404)。
    #     {nid}/view・{nid}/events より前に登録する(request_id が "view"・"events" でも、nid と取り違えない)。
    #     :path にするのは、web が付ける request_id(依頼者 ID:画面の値)に "/" が入っても、別のパスとして
    #     404 にならないようにするため(既知のキーが見つからないと、作成の冪等性が崩れる) ---

    @app.get("/v1/negotiations/by-request/{request_id:path}", response_model=NegotiationByRequestResponse)
    def get_negotiation_by_request(request_id: str) -> NegotiationByRequestResponse:
        return store.get_negotiation_by_request(request_id)

    @app.get("/v1/negotiations", response_model=OpenNegotiationsPage)
    def list_open_negotiations(
        open: bool = Query(default=True), cursor: str | None = Query(default=None)
    ) -> OpenNegotiationsPage:
        return store.list_open_negotiations(cursor=cursor)

    # --- §3.3: view・events ---

    @app.get("/v1/negotiations/{nid}/view", response_model=NegotiationViewResponse)
    def get_view(nid: str, side: Side) -> NegotiationViewResponse:
        return store.get_view(nid, side)

    @app.get("/v1/negotiations/{nid}/events", response_model=list[EventViewItem])
    def get_events(nid: str, side: Side, after_seq: int = Query(default=0, ge=0)) -> list[EventViewItem]:
        return store.get_events(nid, side, after_seq)

    # --- デモ用の読み出し(台帳 X-38): mode が demo・attack で、候補者が架空人物の交渉だけ。
    #     それ以外は、交渉があるかどうかを知らせないよう 404 ---

    @app.get("/v1/demo/negotiations/{nid}/events", response_model=list[EventViewItem])
    def get_demo_events(nid: str, side: Side, after_seq: int = Query(default=0, ge=0)) -> list[EventViewItem]:
        return store.get_demo_events(nid, side, after_seq)

    # --- §3.3: moves・principal-answer・control・expire ---

    @app.post("/v1/negotiations/{nid}/moves", response_model=MoveResponse)
    def post_move(nid: str, body: MoveRequest) -> MoveResponse:
        return store.process_move(nid, body)

    @app.post("/v1/negotiations/{nid}/principal-answer", response_model=PrincipalAnswerResponse)
    def post_principal_answer(nid: str, body: PrincipalAnswerRequest) -> PrincipalAnswerResponse:
        return store.process_principal_answer(nid, body)

    @app.post("/v1/negotiations/{nid}/control", response_model=ControlResponse)
    def post_control(nid: str, body: ControlRequest) -> ControlResponse:
        return store.control(nid, body)

    @app.post("/v1/negotiations/{nid}/expire", response_model=ExpireResponse)
    def post_expire(nid: str) -> ExpireResponse:
        return store.expire(nid)

    return app


def _create_vault_db() -> firestore.Client:
    """vault-db の Firestore クライアント(本番用。(default) は web 用なので使わない。§1.1)。

    接続先(プロジェクト・認証・エミュレータか)は環境で決まる。テストは、ここを差し替える。
    """
    return create_client()


def _seed_templates_enabled(source: Mapping[str, str]) -> bool:
    """環境変数 VAULT_SEED_TEMPLATES から、起動時にテンプレートを投入するかを決める。true・false(未設定は true)だけ。
    ほかの値は、起動を拒否する(打ち間違いで、意図せず切れたり入ったりしないように)。"""
    value = source.get(SEED_TEMPLATES_ENV, "true").strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"the environment variable {SEED_TEMPLATES_ENV} must be true or false")


def create_app_from_env(environ: Mapping[str, str] | None = None) -> FastAPI:
    """本番の起動口(`uvicorn vault.app:create_app_from_env --factory`)。

    読む環境変数は VAULT_SEED_TEMPLATES だけ(true・false。未設定は true): true なら、起動時に、イメージ内の fixtures/ の
    架空人物のテンプレートを vault-db に冪等に書く(vault.seed。§3.7・台帳 P-15)。失敗したら起動しない。ローカルの開発では
    false にできる。Firestore の接続先は、クライアントが環境から決める(Cloud Run では、サービスアカウントと
    サービスの属するプロジェクト)。時計は SystemClock、暫定値は config/params.toml。封印はしない(sealer を渡さない = NoopSealer。
    TEE 版は vault.tee.main が Sealer を渡す。§9 の 2)。
    起動時に、uvicorn のアクセスログの URL から ID(依頼者 ID・交渉 ID)を伏せる(mask_ids_in_logs。台帳 X-40)。
    """
    seed = _seed_templates_enabled(os.environ if environ is None else environ)
    mask_ids_in_logs()
    db = _create_vault_db()
    if seed:
        seed_templates(db, FIXTURES_DIRECTORY)
    return create_app(VaultStore(db=db, clock=SystemClock(), config=DEFAULT_VAULT_CONFIG))
