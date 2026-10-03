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
  (WeakSessionKeyError。台帳 X-39)。デコードした鍵の異なるバイト値が 16 種類未満(全部ゼロ・短い繰り返しなど、明らかに
  乱数でない鍵)でも拒否する。create_app_from_env・create_app のどちらでも同じ。
- ログに ID を残さない(§3.8。台帳 X-40)。本番の起動口(create_app_from_env)が、uvicorn のアクセスログの URL の ID を
  伏せる(mask_ids_in_logs。vault の起動口と共通の処理で、negotiation_core.log_privacy にある)。
- TEE モード(環境変数 VAULT_TEE=true。design.md §9、research/tee-spike-contract.md §7): 金庫は Confidential Space の VM で動く。
  金庫への ID トークンの audience を、URL からではなく設定 [vault.tee] caller_audience の固定の値にし、金庫の接続を、attestation を
  検証してから証明書をピン留めする transport(web.attested_transport)にする。create_app に tee を渡すと、GET /api/tee/attestation も動く。
"""

import asyncio
import functools
import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google.cloud import firestore

from agents.client import send_turn as agents_send_turn
from agents.config import DEFAULT_AGENTS_CONFIG
from negotiation_core.attestation import AttestationPolicy, SignerCerts, active_digests, load_releases
from negotiation_core.log_privacy import mask_ids_in_logs
from negotiation_core.tee_settings import load_tee_settings
from vault.clock import Clock

from web.api import DEMO_PATH_PREFIX, TEE_PATH_PREFIX, TeeAttestationConfig, build_router
from web.attested_transport import AttestedVaultTransport
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

_log = logging.getLogger(__name__)

VAULT_BASE_URL_ENV = "VAULT_BASE_URL"
# TEE モード(契約 §7)
VAULT_TEE_ENV = "VAULT_TEE"
VAULT_SERVICE_ACCOUNT_ENV = "VAULT_SERVICE_ACCOUNT"
GOOGLE_CLOUD_PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"
VAULT_RELEASES_FILE_ENV = "VAULT_RELEASES_FILE"
VAULT_EXPECTED_ZONE_ENV = "VAULT_EXPECTED_ZONE"
VAULT_EXPECTED_INSTANCE_ENV = "VAULT_EXPECTED_INSTANCE"
GITHUB_REPO_URL_ENV = "GITHUB_REPO_URL"
# digest の許可リストの既定の場所(src/web/app.py から見て、プロジェクト直下の deploy/vault-releases.json)
DEFAULT_RELEASES_PATH = Path(__file__).resolve().parents[2] / "deploy" / "vault-releases.json"


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
    tee: TeeAttestationConfig | None = None,
) -> FastAPI:
    """web の FastAPI アプリを作る。

    session_key が空なら MissingSessionKeyError、base64url として読めない・32 バイト未満・明らかに乱数でないなら
    WeakSessionKeyError(どちらも起動を拒否する。台帳 X-39)。send_turn を省くと、agents_base_url を束ねた
    agents.client.send_turn を使う(本番の経路。token_provider があれば、agents の呼び出しに ID トークンを付ける)。
    テストは、スタブの send_turn か、agents の app につないだ経路を差し込む。金庫への認証は、vault の AsyncClient 側に
    付ける(create_app_from_env)。tee を渡すと(TEE モード)、GET /api/tee/attestation も動く(渡さなければ、このルートはなく 404)。
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
    app.include_router(build_router(services, tee))
    app.add_middleware(
        PrincipalSessionMiddleware,
        codec=services.codec,
        meta=services.meta,
        locks=services.locks,
        clock=services.clock,
        session_free_prefixes=(DEMO_PATH_PREFIX, TEE_PATH_PREFIX),
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


def _open_vault_http_client(
    vault_url: str, auth: httpx.Auth | None, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """金庫を呼ぶ HTTP クライアント(本番用。テストは、ここを MockTransport などにつなぐものに差し替える)。

    transport は TEE モードのとき(attestation を検証してから証明書をピン留めする transport)。環境のプロキシ設定
    (trust_env)は見ない: ピン留めした接続を迂回して、ID トークンが別の経路に流れないように。
    """
    extra = {} if transport is None else {"transport": transport, "trust_env": False}
    return httpx.AsyncClient(
        base_url=vault_url,
        timeout=httpx.Timeout(DEFAULT_WEB_CONFIG.vault_client.timeout_seconds),
        auth=auth,
        **extra,
    )


def _tee_enabled(source: Mapping[str, str]) -> bool:
    """環境変数 VAULT_TEE から、TEE モードかを決める。true・false(未設定は false)だけ。ほかの値は、起動を拒否する。"""
    value = source.get(VAULT_TEE_ENV, "false").strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"the environment variable {VAULT_TEE_ENV} must be true or false")


def _required_for_tee(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip()
    if not value:
        raise RuntimeError(f"the environment variable {name} is not set (it is required when {VAULT_TEE_ENV}=true)")
    return value


def _build_tee_mode(
    source: Mapping[str, str], vault_url: str
) -> tuple[AttestedVaultTransport, TeeAttestationConfig, str]:
    """TEE モードの部品を作る: (金庫の attestation を検証してからピン留めする transport, GET /api/tee/attestation の設定,
    金庫への ID トークンの固定の audience)。必須の環境変数がない・digest の表のファイルがない・金庫の URL が https でないなら、
    起動を拒否する。
    """
    service_account = _required_for_tee(source, VAULT_SERVICE_ACCOUNT_ENV)
    project_id = _required_for_tee(source, GOOGLE_CLOUD_PROJECT_ENV)
    settings = load_tee_settings()
    releases_path = Path(source.get(VAULT_RELEASES_FILE_ENV, "").strip() or DEFAULT_RELEASES_PATH)
    try:
        releases = load_releases(releases_path)
    except FileNotFoundError:
        raise RuntimeError(f"the releases file {releases_path} does not exist (set {VAULT_RELEASES_FILE_ENV})") from None
    if not releases:
        _log.warning("the releases file has no entries; no vault image is accepted until one is recorded")
    policy = AttestationPolicy(
        audience=settings.attestation_audience,
        issuer=settings.attestation_issuer,
        allowed_hwmodels=frozenset(settings.allowed_hwmodels),
        allowed_digests=active_digests(releases),
        project_id=project_id,
        service_account=service_account,
        zone=source.get(VAULT_EXPECTED_ZONE_ENV, "").strip() or None,
        instance_name=source.get(VAULT_EXPECTED_INSTANCE_ENV, "").strip() or None,
    )
    transport = AttestedVaultTransport(
        vault_url, policy=policy, certs=SignerCerts(settings.attestation_signer_certs_url)
    )
    api = TeeAttestationConfig(
        transport=transport,
        releases=releases,
        github_repo_url=source.get(GITHUB_REPO_URL_ENV, "").strip() or None,
    )
    return transport, api, settings.caller_audience


def create_app_from_env(environ: Mapping[str, str] | None = None) -> FastAPI:
    """本番の起動口(`uvicorn web.app:create_app_from_env --factory --workers 1`)。環境変数から組み立てる。

    - SESSION_SIGNING_KEY: セッションクッキーの署名の鍵(必須。なければ MissingSessionKeyError、base64url として
      読めない・32 バイト未満・明らかに乱数でないなら WeakSessionKeyError で、起動を拒否する)。作り方:
      `python -c "import secrets; print(secrets.token_urlsafe(32))"`
    - VAULT_BASE_URL: 金庫の URL(必須)。
    - SERVICE_AUTH_ENABLED: サービス間の認証(Cloud Run の ID トークン。台帳 X-37)を使うか(true・false。未設定は true)。
      ローカルでは false にする(メタデータサーバを呼ばない)。使うときは、金庫・agents を呼ぶたびに、その URL を audience に
      した ID トークンを付ける(agents の URL は、設定ファイルの [agents] public_base_url)。
    - VAULT_TEE: 金庫が TEE(Confidential Space)かを決める(true・false。未設定は false。ほかの値は起動を拒否)。true のときは、
      VAULT_BASE_URL は https の URL(例 https://10.10.0.10:8443)で、ほかに VAULT_SERVICE_ACCOUNT(金庫のサービスアカウントのメール。
      トークンの google_service_accounts と照合)と GOOGLE_CLOUD_PROJECT(トークンの project_id と照合)が必須。任意:
      VAULT_RELEASES_FILE(digest の許可リスト。既定 deploy/vault-releases.json。なければ起動を拒否)・VAULT_EXPECTED_ZONE・
      VAULT_EXPECTED_INSTANCE(あれば照合)・GITHUB_REPO_URL(コミットのリンクの土台)。
    起動時に、uvicorn のアクセスログの URL から ID を伏せる(mask_ids_in_logs。台帳 X-40)。uvicorn は、依頼者ごとの
    ロック(台帳 I-4)が 1 つのプロセスの中の asyncio のロックなので、workers=1 で動かす。
    """
    source = os.environ if environ is None else environ
    session_key = load_session_key(source)
    vault_url = source.get(VAULT_BASE_URL_ENV, "").strip()
    if not vault_url:
        raise RuntimeError(f"the environment variable {VAULT_BASE_URL_ENV} is not set")
    token_provider = id_token_provider_from_env(source)
    tee_mode = _tee_enabled(source)
    mask_ids_in_logs()
    if tee_mode:
        transport, tee, caller_audience = _build_tee_mode(source, vault_url)
        vault_auth = IdTokenAuth(token_provider, vault_url, audience=caller_audience) if token_provider is not None else None
        http = _open_vault_http_client(vault_url, vault_auth, transport)
    else:
        tee = None
        vault_auth = IdTokenAuth(token_provider, vault_url) if token_provider is not None else None
        http = _open_vault_http_client(vault_url, vault_auth)
    return create_app(
        vault=VaultClient(http),
        default_db=_create_default_db(),
        session_key=session_key,
        token_provider=token_provider,
        tee=tee,
    )
