"""TEE スパイクの probe(Cloud Run Job。スパイクだけで使う。research/tee-spike-contract.md §12)。

web と同じサービスアカウントで動き、web が金庫を呼ぶ経路(ID トークン → 金庫の attestation の検証 → 証明書のピン留め → API)を通して、
次の 4 点を確かめる。結果だけを出力する(トークンの値は出さない)。Cloud Run の中(メタデータサーバがある所)でだけ動く。

  (1) メタデータサーバから caller_audience の ID トークンを取り、claim(iss・aud・azp・email・email_verified・exp)を出力する。
  (2) 金庫の attestation を検証してから、証明書をピン留めする(web と同じ AttestedVaultTransport)。結果と claims を出力する。
  (3) ピン留めした接続で GET /v1/principals/0000000000000000/policy を Bearer つきで呼ぶ。404 が期待(認可を通り、依頼者がいない)。
  (4) 同じ経路を Bearer なしで呼ぶ。401 が期待。
(2) が通り、(3) が 404、(4) が 401 なら終了コード 0、それ以外は 1。

環境変数(Cloud Run Job に設定する)
  VAULT_BASE_URL         金庫の URL(例 https://10.10.0.10:8443)
  VAULT_SERVICE_ACCOUNT  金庫のサービスアカウントのメール(トークンの google_service_accounts と照合)
  GOOGLE_CLOUD_PROJECT   省略できる。なければ、メタデータサーバの project/project-id
  VAULT_RELEASES_FILE    省略できる。digest の表(既定 deploy/vault-releases.json)
  TEE_PROBE_ALLOW_DEBUG  true で、debug イメージのトークンも通す(スパイクの途中だけ)
"""

import asyncio
import os
import secrets
import sys
from collections.abc import Awaitable, Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx  # noqa: E402

from negotiation_core.attestation import (  # noqa: E402
    AttestationError,
    AttestationPolicy,
    SignerCerts,
    active_digests,
    decode_claims_unverified,
    load_releases,
    summarize_claims,
)
from negotiation_core.tee_settings import load_tee_settings  # noqa: E402
from web.attested_transport import AttestedVaultTransport  # noqa: E402
from web.service_auth import IdTokenAuth, IdTokenProvider, ServiceAuthError  # noqa: E402

REQUIRED_ENVIRONMENT = ("VAULT_BASE_URL", "VAULT_SERVICE_ACCOUNT")
DEFAULT_RELEASES_PATH = PROJECT_ROOT / "deploy" / "vault-releases.json"
PROBE_PATH = "/v1/principals/0000000000000000/policy"  # 存在しない依頼者(形だけ正しい ID)
ID_TOKEN_CLAIMS = ("iss", "aud", "azp", "email", "email_verified", "exp")
_PROJECT_ID_URL = "http://metadata.google.internal/computeMetadata/v1/project/project-id"
_TIMEOUT_SECONDS = 30.0


async def _project_id_from_metadata_server() -> str:
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        response = await client.get(_PROJECT_ID_URL, headers={"Metadata-Flavor": "Google"})
    response.raise_for_status()
    return response.text.strip()


def _unverified_claims(token: str | None) -> dict:
    """検証に失敗したトークンの、取れた範囲の claims(署名を確かめていない。表示用)。読めなければ空。"""
    try:
        return decode_claims_unverified(token) if token else {}
    except AttestationError:
        return {}


async def _status(call: Awaitable[httpx.Response]) -> int | str:
    """呼び出しのステータス。つながらなければ、エラーの型名(トークンの値も本文も出さない)。"""
    try:
        return (await call).status_code
    except httpx.HTTPError as exc:
        return f"error:{type(exc).__name__}"


async def _run(environ: Mapping[str, str]) -> int:
    settings = load_tee_settings()
    vault_url = environ["VAULT_BASE_URL"].strip()
    allow_debug = environ.get("TEE_PROBE_ALLOW_DEBUG", "").strip().lower() == "true"
    project_id = environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    try:
        releases = load_releases(environ.get("VAULT_RELEASES_FILE", "").strip() or DEFAULT_RELEASES_PATH)
        project_id = project_id or await _project_id_from_metadata_server()
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"エラー: 設定を読めません: {type(exc).__name__}", file=sys.stderr)
        return 1

    # (1) ID トークン
    provider = IdTokenProvider()
    try:
        id_token = await provider.token(settings.caller_audience)
    except ServiceAuthError as exc:
        print(f"[1/4] NG: ID トークンを取れません: {exc}")
        return 1
    print(f"[1/4] ID トークン(audience={settings.caller_audience})の claim:")
    claims = decode_claims_unverified(id_token)
    for name in ID_TOKEN_CLAIMS:
        print(f"  {name:<15} {claims.get(name)}")

    # (2) 金庫の attestation の検証とピン留め
    policy = AttestationPolicy(
        audience=settings.attestation_audience,
        issuer=settings.attestation_issuer,
        allowed_hwmodels=frozenset(settings.allowed_hwmodels),
        allowed_digests=active_digests(releases),
        project_id=project_id,
        service_account=environ["VAULT_SERVICE_ACCOUNT"].strip(),
        require_production=not allow_debug,
    )
    transport = AttestedVaultTransport(
        vault_url, policy=policy, certs=SignerCerts(settings.attestation_signer_certs_url)
    )
    attested = False
    try:
        _, verified = await transport.attest(secrets.token_urlsafe(32))
        attested = True
        print("[2/4] OK: 金庫の attestation を検証し、証明書をピン留めしました。claims:")
        claims_to_show = verified.claims
    except AttestationError as exc:
        print(f"[2/4] NG: 検証に失敗しました。reason={exc.reason}")
        claims_to_show = _unverified_claims(exc.token)
    except (httpx.HTTPError, OSError) as exc:
        print(f"[2/4] NG: 金庫に届きません({type(exc).__name__})")
        claims_to_show = {}
    for name, value in summarize_claims(claims_to_show).items():
        print(f"  {name:<20} {value}")

    # (3)(4) ピン留めした接続で、Bearer あり・なし
    auth = IdTokenAuth(provider, vault_url, audience=settings.caller_audience)
    async with httpx.AsyncClient(
        base_url=vault_url, transport=transport, auth=auth, timeout=_TIMEOUT_SECONDS, trust_env=False
    ) as client:
        with_bearer = await _status(client.get(PROBE_PATH))
        without_bearer = await _status(client.get(PROBE_PATH, auth=None))
    print(f"[3/4] Bearer あり: {with_bearer}(期待 404)")
    print(f"[4/4] Bearer なし: {without_bearer}(期待 401)")

    passed = attested and with_bearer == 404 and without_bearer == 401
    print(f"結果: {'合格' if passed else '不合格'}(終了コード {0 if passed else 1})")
    return 0 if passed else 1


def main(environ: Mapping[str, str] | None = None) -> int:
    """probe を動かして、終了コードを返す。必須の環境変数がなければ、何も呼ばずに説明して 1。"""
    source = os.environ if environ is None else environ
    missing = [name for name in REQUIRED_ENVIRONMENT if not source.get(name, "").strip()]
    if missing:
        print(f"エラー: 環境変数がありません: {', '.join(missing)}(Cloud Run Job の設定を確かめてください)", file=sys.stderr)
        return 1
    return asyncio.run(_run(source))


if __name__ == "__main__":
    sys.exit(main())
