"""web の FastAPI アプリの組み立てと起動(design.md §1.1・§4.1・§6.3)。

- create_app: 外部の部品(金庫のクライアント・(default) の Firestore・エージェントを呼ぶ関数・時計・sleep)を
  差し込んで、アプリを作る。テストは、金庫の app と agents の app を ASGI のままつなぐ。
- 起動時(lifespan)に、交渉の見回り・依頼者の見回りを動かす。レフェリーのタスクは、交渉の見回りと、
  交渉の作成のときに動く(RefereeManager)。止めるときは、見回りとレフェリーのタスクをすべて止める。
- エージェントを呼ぶ関数は、agents.client.send_turn に、設定の base_url([agents] public_base_url)を
  束ねたものを、レフェリーに差し込む。サービス間の認証(ID トークン)はデプロイの段で足す。
- 署名の鍵は環境変数 SESSION_SIGNING_KEY から読む。コードに鍵を書かず、既定値も持たない。鍵がなければ
  起動を拒否する(create_app_from_env・create_app のどちらも MissingSessionKeyError)。
"""

import asyncio
import functools
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google.cloud import firestore

from agents.client import send_turn as agents_send_turn
from agents.config import DEFAULT_AGENTS_CONFIG
from vault.clock import Clock

from web.api import DEMO_PATH_PREFIX, build_router
from web.config import DEFAULT_WEB_CONFIG, WebConfig
from web.referee import FictionalAnswerer, SendTurn, Sleep
from web.services import build_services
from web.session import load_session_key
from web.session_middleware import PrincipalSessionMiddleware
from web.vault_client import (
    VaultClient,
    VaultClientError,
    VaultConflictError,
    VaultNotFoundError,
    VaultUnavailableError,
)

VAULT_BASE_URL_ENV = "VAULT_BASE_URL"


def bind_agents_client(base_url: str) -> SendTurn:
    """agents.client.send_turn に base_url を束ねる(レフェリーの SendTurn の形にする。§4.1)。"""
    return functools.partial(agents_send_turn, base_url)


def create_app(
    *,
    vault: VaultClient,
    default_db: firestore.Client,
    session_key: str,
    agents_base_url: str = DEFAULT_AGENTS_CONFIG.public_base_url,
    send_turn: SendTurn | None = None,
    clock: Clock | None = None,
    sleep: Sleep = asyncio.sleep,
    config: WebConfig = DEFAULT_WEB_CONFIG,
    answerer: FictionalAnswerer | None = None,
) -> FastAPI:
    """web の FastAPI アプリを作る。session_key が空なら MissingSessionKeyError(起動を拒否する)。

    send_turn を省くと、agents_base_url を束ねた agents.client.send_turn を使う(本番の経路)。
    テストは、スタブの send_turn か、agents の app につないだ経路を差し込む。
    """
    services = build_services(
        vault=vault,
        default_db=default_db,
        session_key=session_key,
        send_turn=send_turn if send_turn is not None else bind_agents_client(agents_base_url),
        clock=clock,
        sleep=sleep,
        config=config,
        answerer=answerer,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        workers = [
            asyncio.create_task(services.sweeper.run(), name="web-sweeper"),
            asyncio.create_task(services.principal_sweeper.run(), name="web-principal-sweeper"),
        ]
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await services.referees.stop_all()

    app = FastAPI(title="web", lifespan=lifespan)
    app.state.services = services
    app.include_router(build_router(services))
    app.add_middleware(
        PrincipalSessionMiddleware,
        codec=services.codec,
        meta=services.meta,
        locks=services.locks,
        clock=services.clock,
        session_free_prefixes=(DEMO_PATH_PREFIX,),
    )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 既定の応答は、拒否した入力の値(面談の生の値など)をそのまま返すので、場所と理由の種類だけにする。
        detail = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    # 金庫の応答を HTTP に直す(金庫の detail は返さない)。
    @app.exception_handler(VaultNotFoundError)
    async def _vault_not_found(request: Request, exc: VaultNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "not_found"})

    @app.exception_handler(VaultConflictError)
    async def _vault_conflict(request: Request, exc: VaultConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "conflict"})

    @app.exception_handler(VaultUnavailableError)
    async def _vault_unavailable(request: Request, exc: VaultUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "temporarily_unavailable"})

    @app.exception_handler(VaultClientError)
    async def _vault_error(request: Request, exc: VaultClientError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": "vault_error"})

    return app


def _create_default_db() -> firestore.Client:
    """(default) の Firestore クライアント(web 用。§1.1)。接続先は環境(FIRESTORE_EMULATOR_HOST の有無)で決まる。"""
    return firestore.Client()


def create_app_from_env(environ: Mapping[str, str] | None = None) -> FastAPI:
    """本番の起動口(`uvicorn web.app:create_app_from_env --factory`)。環境変数から組み立てる。

    - SESSION_SIGNING_KEY: セッションクッキーの署名の鍵(必須。なければ MissingSessionKeyError で起動を拒否する)
    - VAULT_BASE_URL: 金庫の URL(必須)。サービス間の認証(ID トークン)はデプロイの段で足す。
    エージェントの URL は、設定ファイルの [agents] public_base_url を使う。
    """
    source = os.environ if environ is None else environ
    session_key = load_session_key(source)
    vault_url = source.get(VAULT_BASE_URL_ENV, "").strip()
    if not vault_url:
        raise RuntimeError(f"the environment variable {VAULT_BASE_URL_ENV} is not set")
    http = httpx.AsyncClient(
        base_url=vault_url, timeout=httpx.Timeout(DEFAULT_WEB_CONFIG.vault_client.timeout_seconds)
    )
    return create_app(vault=VaultClient(http), default_db=_create_default_db(), session_key=session_key)
