"""web の FastAPI アプリの組み立てと起動(design.md §1.1・§4.1・§6.3)。

- create_app: 外部の部品(金庫のクライアント・(default) の Firestore・エージェントを呼ぶ関数・時計・sleep)を
  差し込んで、アプリを作る。テストは、金庫の app と agents の app を ASGI のままつなぐ。
- 起動時(lifespan)に、交渉の見回り・依頼者の見回りを動かす。レフェリーのタスクは、交渉の見回りと、
  交渉の作成のときに動く(RefereeManager)。止めるときは、見回りとレフェリーのタスクをすべて止める。
- エージェントを呼ぶ関数は、agents.client.send_turn に、設定の base_url([agents] public_base_url)を
  束ねたものを、レフェリーに差し込む。
- サービス間の認証(台帳 X-37): 金庫・agents を呼ぶときに、呼び先の URL を audience にした Google の ID トークンを
  `Authorization: Bearer` で付ける(web.service_auth)。環境変数 SERVICE_AUTH_ENABLED=false で切る(ローカル・テスト)。
- 署名の鍵は環境変数 SESSION_SIGNING_KEY から読む。コードに鍵を書かず、既定値も持たない。鍵がなければ
  起動を拒否する(MissingSessionKeyError)。base64url として読めて 32 バイト以上でなければ、これも起動を拒否する
  (WeakSessionKeyError。台帳 X-39)。create_app_from_env・create_app のどちらでも同じ。
- ログに ID を残さない(§3.8。台帳 X-40)。本番の起動口(create_app_from_env)が、uvicorn のアクセスログの URL の ID を
  伏せる(mask_ids_in_logs)。
"""

import asyncio
import functools
import logging
import os
import re
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
from web.service_auth import IdTokenAuth, IdTokenProvider, id_token_provider_from_env
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


def bind_agents_client(base_url: str, token_provider: IdTokenProvider | None = None) -> SendTurn:
    """agents.client.send_turn に base_url を束ねる(レフェリーの SendTurn の形にする。§4.1)。

    token_provider を渡すと、agents を呼ぶたびに、base_url を audience にした ID トークンを付ける(台帳 X-37)。
    渡さなければ、認証を付けない(ローカル・テスト)。
    """
    auth = IdTokenAuth(token_provider, base_url) if token_provider is not None else None
    return functools.partial(agents_send_turn, base_url, auth=auth)


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
    token_provider: IdTokenProvider | None = None,
) -> FastAPI:
    """web の FastAPI アプリを作る。

    session_key が空なら MissingSessionKeyError、base64url として読めない・32 バイト未満なら WeakSessionKeyError
    (どちらも起動を拒否する。台帳 X-39)。send_turn を省くと、agents_base_url を束ねた agents.client.send_turn を使う
    (本番の経路。token_provider があれば、agents の呼び出しに ID トークンを付ける)。テストは、スタブの send_turn か、
    agents の app につないだ経路を差し込む。金庫への認証は、vault の AsyncClient 側に付ける(create_app_from_env)。
    """
    services = build_services(
        vault=vault,
        default_db=default_db,
        session_key=session_key,
        send_turn=send_turn if send_turn is not None else bind_agents_client(agents_base_url, token_provider),
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


def _open_vault_http_client(vault_url: str, auth: httpx.Auth | None) -> httpx.AsyncClient:
    """金庫を呼ぶ HTTP クライアント(本番用。テストは、ここを MockTransport などにつなぐものに差し替える)。"""
    return httpx.AsyncClient(
        base_url=vault_url,
        timeout=httpx.Timeout(DEFAULT_WEB_CONFIG.vault_client.timeout_seconds),
        auth=auth,
    )


# 16 桁の 16 進数(依頼者 ID・交渉 ID。§2.7)。前後が 16 進数の文字でないものだけ。
_ID_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")


class _MaskIdsFilter(logging.Filter):
    """ログの引数の文字列にある ID(16 桁の 16 進数)を `<id>` に置き換える(台帳 X-40)。

    uvicorn のアクセスログは、`'%s - "%s %s HTTP/%s" %d'`(クライアント・メソッド・URL・版・ステータス)の形で、
    URL(/v1/principals/{依頼者 ID}/...・/v1/negotiations/{交渉 ID}/...)をそのまま書く。引数の並びは変えない。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_ID_IN_TEXT.sub("<id>", arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def mask_ids_in_logs() -> None:
    """URL に ID が入るログから、ID を外す(台帳 X-40)。

    §3.8: ログに残すのは、エンドポイント・side・手の種類・回数・判定結果だけ。
    - uvicorn のアクセスログ(`uvicorn.access`): URL の ID を `<id>` に伏せる(エンドポイントとステータスは残す)。
    - httpx のリクエストのログ: INFO で、金庫への呼び出しの URL(ID つき)を書くので、WARNING 以上にする。
    本番の起動口が呼ぶ。何度呼んでも、フィルタは 1 つだけ。Cloud Run 自身のリクエストログ
    (run.googleapis.com/requests)は、URL をそのまま持ち、アプリからは変えられないので、デプロイの設定で扱う。
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, _MaskIdsFilter) for existing in access_logger.filters):
        access_logger.addFilter(_MaskIdsFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app_from_env(environ: Mapping[str, str] | None = None) -> FastAPI:
    """本番の起動口(`uvicorn web.app:create_app_from_env --factory --workers 1`)。環境変数から組み立てる。

    - SESSION_SIGNING_KEY: セッションクッキーの署名の鍵(必須。なければ MissingSessionKeyError、base64url として
      読めない・32 バイト未満なら WeakSessionKeyError で、起動を拒否する)。作り方:
      `python -c "import secrets; print(secrets.token_urlsafe(32))"`
    - VAULT_BASE_URL: 金庫の URL(必須)。
    - SERVICE_AUTH_ENABLED: サービス間の認証(Cloud Run の ID トークン。台帳 X-37)を使うか(true・false。未設定は true)。
      ローカルでは false にする(メタデータサーバを呼ばない)。使うときは、金庫・agents を呼ぶたびに、その URL を audience に
      した ID トークンを付ける(agents の URL は、設定ファイルの [agents] public_base_url)。
    起動時に、uvicorn のアクセスログの URL から ID を伏せる(mask_ids_in_logs。台帳 X-40)。uvicorn は、依頼者ごとの
    ロック(台帳 I-4)が 1 つのプロセスの中の asyncio のロックなので、workers=1 で動かす。
    """
    source = os.environ if environ is None else environ
    session_key = load_session_key(source)
    vault_url = source.get(VAULT_BASE_URL_ENV, "").strip()
    if not vault_url:
        raise RuntimeError(f"the environment variable {VAULT_BASE_URL_ENV} is not set")
    token_provider = id_token_provider_from_env(source)
    mask_ids_in_logs()
    vault_auth = IdTokenAuth(token_provider, vault_url) if token_provider is not None else None
    http = _open_vault_http_client(vault_url, vault_auth)
    return create_app(
        vault=VaultClient(http),
        default_db=_create_default_db(),
        session_key=session_key,
        token_provider=token_provider,
    )
