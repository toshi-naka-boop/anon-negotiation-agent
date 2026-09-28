"""金庫の内部 HTTP・JSON API(design.md §3.3。呼べるのは web だけという前提)。

ここでは FastAPI のルーティングと、VaultStore の例外を HTTP ステータスへ変換するだけを行う。
秘密に触れる判断はすべて VaultStore(と、その先の negotiation_core)が行う。
"""

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from negotiation_core import Side

from vault.api_models import (
    ControlRequest,
    ControlResponse,
    CreateNegotiationRequest,
    CreateNegotiationResponse,
    EventViewItem,
    ExpireResponse,
    MoveRequest,
    MoveResponse,
    NegotiationViewResponse,
    OpenNegotiationsPage,
    PolicyView,
    PrincipalNegotiationSummary,
    PutBlocklistRequest,
    PutPolicyRequest,
)
from vault.errors import (
    MovePreconditionFailed,
    NotFoundError,
    PolicyValidationError,
    TransactionRetryExhausted,
)
from vault.store import VaultStore


def create_app(store: VaultStore) -> FastAPI:
    """VaultStore を注入した FastAPI アプリを作る(1b-1 は uvicorn を起動しない。
    テストは TestClient から直接叩く)。
    """
    app = FastAPI(title="vault")

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(MovePreconditionFailed)
    async def _precondition_failed(request: Request, exc: MovePreconditionFailed) -> JSONResponse:
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

    # --- §3.3: 本人の交渉一覧 ---

    @app.get("/v1/principals/{pid}/negotiations", response_model=list[PrincipalNegotiationSummary])
    def list_principal_negotiations(pid: str) -> list[PrincipalNegotiationSummary]:
        return store.list_principal_negotiations(pid)

    # --- §3.3: 交渉の作成、見回り用の一覧 ---

    @app.post("/v1/negotiations", response_model=CreateNegotiationResponse)
    def create_negotiation(body: CreateNegotiationRequest) -> CreateNegotiationResponse:
        return store.create_negotiation(body)

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

    # --- §3.3: moves・control・expire ---

    @app.post("/v1/negotiations/{nid}/moves", response_model=MoveResponse)
    def post_move(nid: str, body: MoveRequest) -> MoveResponse:
        return store.process_move(nid, body)

    @app.post("/v1/negotiations/{nid}/control", response_model=ControlResponse)
    def post_control(nid: str, body: ControlRequest) -> ControlResponse:
        return store.control(nid, body)

    @app.post("/v1/negotiations/{nid}/expire", response_model=ExpireResponse)
    def post_expire(nid: str) -> ExpireResponse:
        return store.expire(nid)

    return app
