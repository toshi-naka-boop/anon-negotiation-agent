"""attestation の検証・検証してからピン留めする transport・検証スクリプト・web の TEE の口のテストで共通に使う部品。

`test_` で始まらないので pytest には収集されない(tests/web_helpers.py と同じ扱い)。GCP にも Google にも接続しない:
- トークンは、手元で作った RSA 鍵で署名する(google.auth.crypt.RSASigner + google.auth.jwt.encode)。Google の署名鍵の代わりに、
  その鍵の自己署名の証明書を `{kid: PEM}` として検証側に渡す(Google の x509 エンドポイントと同じ形)。
- 金庫の TLS は、本物の TLS で話す偽物(FakeVaultServer)。契約 §11 と同じ形の自己署名の証明書(P-256、CN=vault、
  SAN=DNS:vault、CA ではない、serverAuth)を使い、127.0.0.1 の空きポートで、別のスレッドの asyncio サーバとして動く。
  /v1/attestation は、要求された nonce と、そのサーバの証明書の DER の SHA-256 を eat_nonce に入れた、テスト鍵で署名したトークンを返す。
"""

import asyncio
import datetime as dt
import functools
import hashlib
import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from google.auth import crypt, jwt

from negotiation_core.attestation import AttestationPolicy

ATTESTATION_AUDIENCE = "https://vault.anon-nego.internal/attestation"
ISSUER = "https://confidentialcomputing.googleapis.com"
DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "cd" * 32
COMMIT = "0123456789abcdef0123456789abcdef01234567"
BUILT_AT = "2026-10-03T09:00:00+00:00"
PROJECT_ID = "anon-nego-test"
SERVICE_ACCOUNT = f"vault-tee@{PROJECT_ID}.iam.gserviceaccount.com"
ZONE = "asia-northeast1-b"
INSTANCE = "vault-tee-1"
NOW = 1_800_000_000.0  # 単体の検証で now に差し込む固定の時刻(UNIX 秒)

# --- 署名鍵とトークン ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_pem: str
    certificate_pem: str  # 自己署名の証明書(Google の x509 エンドポイントの値と同じ形)


@functools.cache
def signing_key(kid: str = "kid-1") -> SigningKey:
    """テスト用の RSA 鍵(kid ごとに 1 回だけ作る。RSA の鍵生成は遅い)。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"test-signer-{kid}")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(private_key, hashes.SHA256())
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return SigningKey(kid, private_pem.decode(), certificate.public_bytes(serialization.Encoding.PEM).decode())


def certs_of(*keys: SigningKey) -> dict[str, str]:
    """検証側に渡す `{kid: PEM}`(既定は kid-1 の鍵だけ)。"""
    return {key.kid: key.certificate_pem for key in (keys or (signing_key(),))}


def mint_token(
    payload: dict,
    key: SigningKey | None = None,
    *,
    kid: str | None = None,
    with_kid: bool = True,
    header: dict | None = None,
) -> str:
    """payload を、鍵(既定は kid-1)で署名した JWT にする。

    kid: ヘッダの kid を、鍵の kid ではなく、この値にする(別の鍵で署名したのに kid だけ合わせる、など)。
    with_kid=False: ヘッダに kid を入れない。header: ヘッダに足す項目(alg を入れると、その alg を名乗る)。
    """
    key = key if key is not None else signing_key()
    signer = crypt.RSASigner.from_string(key.private_pem, key_id=key.kid if with_kid else None)
    return jwt.encode(signer, payload, header=header, key_id=kid).decode()


def attestation_payload(nonces: Sequence[str] | str = (), *, now: float = NOW) -> dict:
    """本物の Confidential Space のトークンの形の payload(本番イメージ・AMD SEV。上書きや壊しは、受け取った側が辞書を直接書き換える)。

    nonces が文字列なら eat_nonce は文字列、並びなら配列(launcher は、1 個なら文字列、複数なら配列で返す)。
    cmd_override・env_override は、上書きがあったときだけ付く(ここでは付けない)。
    """
    return {
        "iss": ISSUER,
        "aud": ATTESTATION_AUDIENCE,
        "sub": f"https://www.googleapis.com/compute/v1/projects/{PROJECT_ID}/zones/{ZONE}/instances/{INSTANCE}",
        "iat": int(now),
        "nbf": int(now),
        "exp": int(now) + 3600,
        "eat_nonce": nonces if isinstance(nonces, str) else list(nonces),
        "eat_profile": "https://cloud.google.com/confidential-computing/confidential-space/docs/reference/token-claims",
        "secboot": True,
        "oemid": 11129,
        "hwmodel": "GCP_AMD_SEV",
        "swname": "CONFIDENTIAL_SPACE",
        "swversion": ["250800"],
        "dbgstat": "disabled-since-boot",
        "google_service_accounts": [SERVICE_ACCOUNT],
        "submods": {
            "confidential_space": {"support_attributes": ["LATEST", "STABLE", "USABLE"]},
            "container": {
                "image_reference": f"asia-northeast1-docker.pkg.dev/{PROJECT_ID}/vault/vault@{DIGEST}",
                "image_digest": DIGEST,
                "image_id": DIGEST,
                "restart_policy": "OnFailure",
            },
            "gce": {
                "zone": ZONE,
                "project_id": PROJECT_ID,
                "project_number": "123456789012",
                "instance_name": INSTANCE,
                "instance_id": "1234567890123456789",
            },
        },
    }


def make_policy(**overrides) -> AttestationPolicy:
    """attestation_payload() が通る検証ポリシー(上書きできる)。"""
    values = {
        "audience": ATTESTATION_AUDIENCE,
        "issuer": ISSUER,
        "allowed_hwmodels": frozenset({"GCP_AMD_SEV", "GCP_INTEL_TDX"}),
        "allowed_digests": frozenset({DIGEST}),
        "project_id": PROJECT_ID,
        "service_account": SERVICE_ACCOUNT,
    }
    values.update(overrides)
    return AttestationPolicy(**values)


def flip_signature_byte(token: str) -> str:
    """署名の先頭の 1 文字を、別の base64url の文字に変える(署名を 1 バイト壊す)。"""
    head, _, signature = token.rpartition(".")
    return f"{head}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"


# --- 金庫の TLS(契約 §11 と同じ形の自己署名の証明書) --------------------------------------------------------------


@dataclass(frozen=True)
class TlsMaterial:
    key_pem: bytes
    cert_pem: str
    certificate_sha256: str  # 証明書の DER の SHA-256(小文字の 16 進)。被験の実装とは別に、ここで計算する


def make_tls_material(days: int = 90) -> TlsMaterial:
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vault")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("vault")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    return TlsMaterial(
        key_pem=private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        cert_pem=certificate.public_bytes(serialization.Encoding.PEM).decode(),
        certificate_sha256=hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest(),
    )


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]  # 名前は小文字
    body: bytes


TokenFactory = Callable[[str, str], str]  # (要求の nonce, このサーバの証明書の SHA-256) → トークン
OTHER_HASH = "d" * 64  # 別の証明書のハッシュ(のつもりの値)


def token_with(**changes) -> TokenFactory:
    """要求の nonce と証明書のハッシュを入れた、今の時刻で発行した本番イメージのトークンを返す token_factory。changes で payload の最上位を上書きする。"""

    def factory(nonce: str, certificate_sha256: str) -> str:
        payload = attestation_payload([nonce, certificate_sha256], now=time.time())
        payload.update(changes)
        return mint_token(payload)

    return factory


# 検証が外れる理由のうち、署名・形以外を 1 つずつ作れるもの(bad_token が対応)。金庫(偽物)の token_factory に入れて使う。
REJECTED_REASONS = ["certificate", "nonce", "signature", "debug", "image_digest", "expired"]


def bad_token(reason: str) -> TokenFactory:
    """reason の確認だけが外れるトークンを返す、偽の金庫の token_factory。"""
    now = time.time()
    if reason == "certificate":  # 別の証明書のハッシュを入れた(別の相手につながっている)
        return lambda nonce, cert: mint_token(attestation_payload([nonce, OTHER_HASH], now=now))
    if reason == "nonce":  # 古いトークンの使い回し
        return lambda nonce, cert: mint_token(attestation_payload(["z" * 43, cert], now=now))
    if reason == "signature":
        return lambda nonce, cert: flip_signature_byte(mint_token(attestation_payload([nonce, cert], now=now)))
    if reason == "debug":
        return token_with(dbgstat="enabled")
    if reason == "image_digest":

        def other_image(nonce: str, cert: str) -> str:
            payload = attestation_payload([nonce, cert], now=now)
            payload["submods"]["container"]["image_digest"] = OTHER_DIGEST
            return mint_token(payload)

        return other_image
    if reason == "expired":
        return lambda nonce, cert: mint_token(attestation_payload([nonce, cert], now=now - 7200))
    raise AssertionError(reason)


class FakeVaultServer:
    """本物の TLS で話す、金庫の偽物(別のスレッドの asyncio サーバ。同期のテストからも、非同期のテストからも使える)。

    - GET /v1/attestation?nonce=: token_factory(nonce, 証明書の SHA-256) のトークンを返す。既定は、本番イメージの正しいトークン
      (eat_nonce に [nonce, 証明書の SHA-256])。attestation_status を 200 以外にすると、そのステータスで断る。attestation_script に
      ステータスを並べると、/v1/attestation の要求のたびに先頭から 1 つずつ使う(例 [429] なら、最初の 1 回だけ 429)。
      attestation_delay(秒)で応答を遅らせる(時間切れの再現)。attestation_raw を設定すると、200 でその本文をそのまま返す(応答の形違いの再現)。
    - GET /v1/principals/{pid}/policy: Authorization がなければ 401、あれば 404(金庫の認可の期待どおり)。
    - GET /v1/ping: 200。POST /v1/echo: 受け取った JSON をそのまま返す。GET /v1/slow: 0.5 秒おいて 200(通信中の要求の再現)。
    応答のたびに接続を閉じる(Connection: close)。受け取った要求は requests に記録する(スレッドをまたいで読める)。
    restart() で、別の証明書・同じポートで立て直す(金庫の再起動の再現)。
    """

    def __init__(self, directory: Path, *, token_factory: TokenFactory | None = None) -> None:
        self._directory = directory
        self.material = make_tls_material()
        self.token_factory: TokenFactory = token_factory if token_factory is not None else self.valid_token
        self.attestation_status = 200
        self.attestation_script: list[int] = []
        self.attestation_delay = 0.0
        self.attestation_raw: bytes | None = None
        self.authorization_required = True  # False にすると、Authorization なしの呼び出しも通す(認可の壊れた金庫の再現)
        self.requests: list[RecordedRequest] = []
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    @property
    def attestation_requests(self) -> list[RecordedRequest]:
        return [request for request in list(self.requests) if request.path == "/v1/attestation"]

    def valid_token(self, nonce: str, certificate_sha256: str, **claims) -> str:
        """このサーバの本番イメージの、正しいトークン(今の時刻で発行)。claims で上書きできる。"""
        payload = attestation_payload([nonce, certificate_sha256], now=time.time())
        payload.update(claims)
        return mint_token(payload)

    def start(self) -> None:
        cert_file, key_file = self._directory / "cert.pem", self._directory / "key.pem"
        cert_file.write_text(self.material.cert_pem)
        key_file.write_bytes(self.material.key_pem)
        ready, failure = threading.Event(), []

        def serve() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.set_exception_handler(lambda _loop, _context: None)  # クライアントが握手の途中で切るときのログを出さない
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert_file, key_file)
                server = loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", self.port, ssl=context))
            except BaseException as exc:  # noqa: BLE001  (起動の失敗は、start() を呼んだスレッドに伝える)
                failure.append(exc)
                ready.set()
                loop.close()
                return
            ready.set()
            try:
                loop.run_forever()
            finally:
                server.close()
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.close()

        self._thread = threading.Thread(target=serve, name="fake-vault", daemon=True)
        self._thread.start()
        if not ready.wait(10) or failure:
            raise RuntimeError("the fake vault did not start") from (failure[0] if failure else None)

    def stop(self) -> None:
        if self._thread is None:
            return
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        self._thread = None

    def restart(self, material: TlsMaterial | None = None) -> TlsMaterial:
        """止めて、別の証明書(省略すれば新しく作る)で、同じポートに立て直す。新しい証明書を返す。"""
        self.stop()
        self.material = material if material is not None else make_tls_material()
        self.start()
        return self.material

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            method, target, _version = head[0].split(" ", 2)
            headers = {}
            for line in head[1:]:
                name, separator, value = line.partition(":")
                if separator:
                    headers[name.strip().lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            path, _, query = target.partition("?")
            request = RecordedRequest(method, path, parse_qs(query), headers, body)
            self.requests.append(request)
            if request.path == "/v1/slow":  # 通信中の要求の再現(応答を遅らせる)
                await asyncio.sleep(0.5)
            if request.path == "/v1/attestation" and self.attestation_delay:
                await asyncio.sleep(self.attestation_delay)
            status, payload = self._respond(request)
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    def _respond(self, request: RecordedRequest) -> tuple[int, dict | bytes]:
        if request.path == "/v1/attestation":
            status = self.attestation_script.pop(0) if self.attestation_script else self.attestation_status
            if status != 200:  # 金庫の本物と同じ固定の文(429: 最短の間隔の制限、503: launcher に届かない)
                return status, {"detail": "attestation rate limited" if status == 429 else "attestation unavailable"}
            if self.attestation_raw is not None:
                return 200, self.attestation_raw
            nonce = request.query.get("nonce", [""])[0]
            token = self.token_factory(nonce, self.material.certificate_sha256)
            return 200, {"token": token, "certificate_sha256": self.material.certificate_sha256}
        if request.path.startswith("/v1/principals/") and request.path.endswith("/policy"):
            if self.authorization_required and "authorization" not in request.headers:
                return 401, {"detail": "unauthenticated"}
            return 404, {"detail": "not_found"}
        if request.path == "/v1/echo":
            return 200, {"received": json.loads(request.body or b"null")}
        if request.path in ("/v1/ping", "/v1/slow"):
            return 200, {"ok": True}
        return 404, {"detail": "not_found"}


@pytest.fixture
def fake_vault(tmp_path):
    """本物の TLS で話す金庫の偽物を起動して渡す(終わったら止める)。"""
    server = FakeVaultServer(tmp_path)
    server.start()
    yield server
    server.stop()
