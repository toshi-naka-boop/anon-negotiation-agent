"""TEE 版の金庫の、呼び出し元(web)の Google ID トークンの検証(src/vault/tee/caller_auth.py。research/tee-spike-contract.md §9)。

Cloud Run の起動元 IAM が効かない TEE 版では、金庫が自分で `Authorization: Bearer <ID トークン>` を検証する。確かめること:

- 通るのは、Google が署名し(RS256)、aud が caller_audience で、iss が Google で、email が許可されたサービスアカウントで、
  email_verified が真のトークンだけ。
- 401 `{"detail": "unauthenticated"}`: ヘッダなし・Bearer でない・形式不正・署名不正・期限切れ・aud 違い・iss 違い
  (alg の取り違え〔HS256 に公開鍵を鍵として使う・none〕と、kid のないトークンも)。
- 403 `{"detail": "forbidden"}`: 署名は正しいが、email が許可された SA でない(email_verified が真でないときも)。
- Google の証明書のキャッシュ: 1 時間。未知の kid のときと期限が切れたときだけ取り直す(試みは 1 分に 1 回まで)。取り直しに失敗しても、
  キャッシュが有効なうちは動く。使える表がなければ 503(検証できないので通さない)。
- トークンの値は、レスポンスにもログにも出ない(google-auth の例外の文にはトークンの一部が入るので、文を書いていないことの確認)。
- 本物の金庫の app に掛けたとき、/v1/attestation 以外のすべての経路が認証を要求する(/docs・/openapi.json は出さない)。

テストは、手元で作った RSA 鍵で署名したトークンと、その公開鍵の自己署名の証明書の PEM(Google の証明書の置き場と同じ形)で行う。
証明書の置き場は httpx.MockTransport、キャッシュの時計は差し込んだ偽の時計。GCP には接続しない。
JWT の exp・iat は google-auth が実時計で見るので、トークンは実時間を基準に作る。
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from vault.app import create_app
from vault.tee.caller_auth import ALLOWED_ISSUERS, CallerCerts, CallerVerifier
from vault_helpers import accept_all_policy

PROJECT_ID = "demo-project"
ALLOWED_EMAIL = f"web-run@{PROJECT_ID}.iam.gserviceaccount.com"
AUDIENCE = "https://vault.anon-nego.internal"
CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
KID = "kid-1"
_DROP = object()  # トークンの claim を省く印


class SigningKey:
    """RSA の鍵 1 組(署名用)と、Google の証明書の置き場と同じ形の公開鍵(自己署名の X.509 の PEM)。"""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        self.signer = crypt.RSASigner.from_string(private_pem, key_id=kid)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-signer")])
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
        self.certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
        self.public_key_pem = (
            private_key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )

    def token(self, **overrides) -> str:
        """既定の claim(許可された SA の、取りたての、金庫宛てのトークン)に overrides を重ねて署名する。_DROP は claim を省く。"""
        now = int(time.time())
        payload = {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE,
            "azp": "108000000000000000001",
            "sub": "108000000000000000001",
            "email": ALLOWED_EMAIL,
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
        }
        payload.update(overrides)
        payload = {name: value for name, value in payload.items() if value is not _DROP}
        return jwt.encode(self.signer, payload).decode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unsigned_parts(header: dict, payload: dict) -> str:
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"


@pytest.fixture(scope="module")
def key() -> SigningKey:
    return SigningKey(KID)


@pytest.fixture(scope="module")
def same_kid_other_key() -> SigningKey:
    """kid は同じで鍵が違う(署名不正の試験)。"""
    return SigningKey(KID)


@pytest.fixture(scope="module")
def rotated_key() -> SigningKey:
    """Google が新しく配った鍵(未知の kid の試験)。"""
    return SigningKey("kid-2")


class CertsServer:
    """Google の証明書の置き場(oauth2/v1/certs)の代わり。取得の回数を数え、失敗させられる。"""

    def __init__(self, certs: dict[str, str]) -> None:
        self.certs = certs
        self.requests = 0
        self.failure: httpx.Response | Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CERTS_URL
        self.requests += 1
        if isinstance(self.failure, Exception):
            raise self.failure
        if self.failure is not None:
            return self.failure
        return httpx.Response(200, json=self.certs)


class FakeMonotonic:
    """キャッシュの時計(秒)。sleep せず進める。"""

    def __init__(self) -> None:
        self.value = 10_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def certs_server(key) -> CertsServer:
    return CertsServer({key.kid: key.certificate_pem})


@pytest.fixture
def monotonic() -> FakeMonotonic:
    return FakeMonotonic()


@pytest.fixture
def certs(certs_server, monotonic) -> CallerCerts:
    return CallerCerts(CERTS_URL, transport=httpx.MockTransport(certs_server), now=monotonic)


@pytest.fixture
def verifier(certs) -> CallerVerifier:
    assert certs.refresh()  # 起動時に 1 回取る(契約 §9)
    return CallerVerifier(audience=AUDIENCE, allowed_email=ALLOWED_EMAIL, certs=certs)


def make_app(verifier) -> FastAPI:
    """create_app と同じ配線(アプリ全体に依存として掛ける)の、最小の app。"""
    app = FastAPI(dependencies=[Depends(verifier)])

    @app.get("/protected")
    def protected() -> dict:
        return {"ok": True}

    return app


@pytest.fixture
def client(verifier) -> TestClient:
    return TestClient(make_app(verifier))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def assert_unauthenticated(response) -> None:
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthenticated"}
    assert response.headers["www-authenticate"] == "Bearer"


def assert_forbidden(response) -> None:
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}


# --- 通るもの ---


def test_a_valid_token_is_accepted(client, key):
    response = client.get("/protected", headers=bearer(key.token()))

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("issuer", sorted(ALLOWED_ISSUERS))
def test_both_forms_of_the_google_issuer_are_accepted(client, key, issuer):
    assert client.get("/protected", headers=bearer(key.token(iss=issuer))).status_code == 200


def test_the_bearer_scheme_is_case_insensitive(client, key):
    assert client.get("/protected", headers={"Authorization": f"bearer {key.token()}"}).status_code == 200


def test_a_public_key_pem_is_also_accepted_in_place_of_a_certificate(key, monotonic):
    # google.auth.jwt.decode は、証明書の PEM でも公開鍵の PEM でも受ける。Google の置き場は証明書の PEM を返す。
    server = CertsServer({key.kid: key.public_key_pem})
    certs = CallerCerts(CERTS_URL, transport=httpx.MockTransport(server), now=monotonic)
    client = TestClient(make_app(CallerVerifier(audience=AUDIENCE, allowed_email=ALLOWED_EMAIL, certs=certs)))

    assert client.get("/protected", headers=bearer(key.token())).status_code == 200


def test_a_token_a_few_seconds_past_its_expiry_is_accepted_for_clock_skew(client, key):
    assert client.get("/protected", headers=bearer(key.token(exp=int(time.time()) - 5))).status_code == 200


# --- 401 ---


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(None, id="no-header"),
        pytest.param("", id="empty"),
        pytest.param("Basic dXNlcjpwYXNz", id="basic"),
        pytest.param("Token abc.def.ghi", id="other-scheme"),
        pytest.param("Bearer", id="bearer-without-token"),
        pytest.param("Bearer ", id="bearer-with-blank-token"),
    ],
)
def test_a_missing_or_non_bearer_header_is_unauthenticated(client, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}

    assert_unauthenticated(client.get("/protected", headers=headers))


@pytest.mark.parametrize("scheme", ["Basic", "Token", "Bear", "Bearer:", "JWT"])
def test_a_valid_token_under_another_scheme_is_unauthenticated(client, key, scheme):
    assert_unauthenticated(client.get("/protected", headers={"Authorization": f"{scheme} {key.token()}"}))


def test_a_valid_token_without_any_scheme_is_unauthenticated(client, key):
    assert_unauthenticated(client.get("/protected", headers={"Authorization": key.token()}))


@pytest.mark.parametrize(
    "token",
    [
        "LEAKME-not-a-jwt",
        "LEAKME-part1.LEAKME-part2",
        "LEAKME-1.LEAKME-2.LEAKME-3.LEAKME-4",
        "!!!.@@@.###",
        "e30.e30.e30",  # 空の JSON の header・payload
        "bm90LWpzb24.bm90LWpzb24.c2ln",  # JSON でない
    ],
)
def test_a_malformed_token_is_unauthenticated_and_its_pieces_are_not_logged(client, caplog, token):
    caplog.set_level(logging.DEBUG)

    response = client.get("/protected", headers=bearer(token))

    assert_unauthenticated(response)
    assert "LEAKME" not in response.text + caplog.text  # google-auth の例外の文は、トークンの一部を含む。書かない


def test_a_token_signed_by_another_key_is_unauthenticated(client, same_kid_other_key):
    assert_unauthenticated(client.get("/protected", headers=bearer(same_kid_other_key.token())))


def test_a_tampered_payload_is_unauthenticated(client, key):
    header, payload, signature = key.token().split(".")
    forged_payload = _b64(json.dumps({**json.loads(base64.urlsafe_b64decode(payload + "==")), "email": "evil@x"}).encode())

    assert_unauthenticated(client.get("/protected", headers=bearer(f"{header}.{forged_payload}.{signature}")))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, id="expired"),
        pytest.param({"exp": int(time.time()) - 60}, id="expired-beyond-the-skew"),
        pytest.param({"iat": int(time.time()) + 3600, "exp": int(time.time()) + 7200}, id="issued-in-the-future"),
        pytest.param({"exp": _DROP}, id="no-exp"),
        pytest.param({"iat": _DROP}, id="no-iat"),
        pytest.param({"aud": "https://vault.example.run.app"}, id="audience-of-another-service"),
        pytest.param({"aud": AUDIENCE + "/"}, id="audience-with-a-trailing-slash"),
        pytest.param({"aud": [AUDIENCE]}, id="audience-in-a-list"),
        pytest.param({"aud": _DROP}, id="no-audience"),
        pytest.param({"iss": "https://evil.example.com"}, id="issuer-is-not-google"),
        pytest.param({"iss": "https://accounts.google.com.evil.example"}, id="issuer-with-a-google-prefix"),
        pytest.param({"iss": _DROP}, id="no-issuer"),
    ],
)
def test_a_validly_signed_token_with_a_bad_standard_claim_is_unauthenticated(client, key, caplog, overrides):
    caplog.set_level(logging.DEBUG)
    token = key.token(**overrides)

    response = client.get("/protected", headers=bearer(token))

    assert_unauthenticated(response)
    assert token not in response.text + caplog.text
    assert token.split(".")[1] not in caplog.text  # 本文(claim)の部分もログに出ない


def test_the_messages_of_google_auth_are_not_logged_only_the_error_type_is(client, key, caplog):
    # 署名の確認を通ったあとの失敗(aud 違い)でも、google-auth の例外の文(claim の値を含む)は書かず、型名だけを書く
    caplog.set_level(logging.DEBUG)

    response = client.get("/protected", headers=bearer(key.token(aud="https://LEAKME-audience.example")))

    assert_unauthenticated(response)
    assert "LEAKME" not in response.text + caplog.text
    assert "InvalidValue" in caplog.text


def test_an_hs256_token_that_uses_the_public_certificate_as_the_secret_is_unauthenticated(client, key):
    # alg の取り違え: 公開鍵を HMAC の鍵にして署名したトークン。RS256 以外は受けない。
    now = int(time.time())
    signing_input = _unsigned_parts(
        {"alg": "HS256", "typ": "JWT", "kid": KID},
        {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": ALLOWED_EMAIL, "email_verified": True,
         "iat": now, "exp": now + 3600},
    )
    signature = hmac.new(key.certificate_pem.encode(), signing_input.encode(), hashlib.sha256).digest()

    assert_unauthenticated(client.get("/protected", headers=bearer(f"{signing_input}.{_b64(signature)}")))


@pytest.mark.parametrize("alg", ["none", "None", "RS512", "ES256", "HS256"])
def test_an_algorithm_other_than_rs256_is_unauthenticated(client, key, alg):
    now = int(time.time())
    unsigned = _unsigned_parts(
        {"alg": alg, "typ": "JWT", "kid": KID},
        {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": ALLOWED_EMAIL, "email_verified": True,
         "iat": now, "exp": now + 3600},
    )

    assert_unauthenticated(client.get("/protected", headers=bearer(f"{unsigned}.")))


def test_a_token_without_a_key_id_is_unauthenticated(client, key):
    now = int(time.time())
    payload = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": ALLOWED_EMAIL, "email_verified": True,
               "iat": now, "exp": now + 3600}
    unsigned = _unsigned_parts({"alg": "RS256", "typ": "JWT"}, payload)
    signature = key.signer.sign(unsigned.encode())

    assert_unauthenticated(client.get("/protected", headers=bearer(f"{unsigned}.{_b64(signature)}")))


# --- 403 ---


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"email": f"other-sa@{PROJECT_ID}.iam.gserviceaccount.com"}, id="another-sa-of-the-project"),
        pytest.param({"email": "web-run@other-project.iam.gserviceaccount.com"}, id="same-name-in-another-project"),
        pytest.param({"email": ALLOWED_EMAIL.upper()}, id="another-case"),
        pytest.param({"email": "someone@example.com"}, id="a-user"),
        pytest.param({"email": [ALLOWED_EMAIL]}, id="email-in-a-list"),
        pytest.param({"email": _DROP}, id="no-email"),
        pytest.param({"email_verified": False}, id="email-not-verified"),
        pytest.param({"email_verified": "true"}, id="email-verified-as-a-string"),
        pytest.param({"email_verified": 1}, id="email-verified-as-a-number"),
        pytest.param({"email_verified": _DROP}, id="no-email-verified"),
    ],
)
def test_a_validly_signed_token_of_another_account_is_forbidden(client, key, caplog, overrides):
    caplog.set_level(logging.DEBUG)
    token = key.token(**overrides)

    response = client.get("/protected", headers=bearer(token))

    assert_forbidden(response)
    assert token not in response.text + caplog.text


def test_a_forbidden_caller_is_logged_with_its_email_but_not_the_token(client, key, caplog):
    # 署名は Google のものなので、claim の値は切り分けのためにログに書いてよい(トークンそのものは書かない)。
    caplog.set_level(logging.DEBUG)
    token = key.token(email="other-sa@demo-project.iam.gserviceaccount.com")

    assert_forbidden(client.get("/protected", headers=bearer(token)))

    assert "other-sa@demo-project.iam.gserviceaccount.com" in caplog.text
    assert token not in caplog.text


def test_the_issuer_is_checked_before_the_account(client, key):
    # 発行者が Google でなければ、email が許可された SA でも 401(403 にしない)。
    assert_unauthenticated(client.get("/protected", headers=bearer(key.token(iss="https://evil.example.com"))))


# --- Google の証明書のキャッシュ ---


def test_the_certificates_are_fetched_once_and_cached_for_an_hour(client, key, certs_server, monotonic):
    assert certs_server.requests == 1  # 起動時の 1 回

    for _ in range(5):
        assert client.get("/protected", headers=bearer(key.token())).status_code == 200
    monotonic.advance(3599)
    assert client.get("/protected", headers=bearer(key.token())).status_code == 200
    assert certs_server.requests == 1

    monotonic.advance(2)  # 1 時間を過ぎた
    assert client.get("/protected", headers=bearer(key.token())).status_code == 200
    assert certs_server.requests == 2


def test_an_unknown_key_id_makes_one_refetch_and_a_rotated_key_is_then_accepted(
    client, key, rotated_key, certs_server, monotonic
):
    certs_server.certs = {key.kid: key.certificate_pem, rotated_key.kid: rotated_key.certificate_pem}
    monotonic.advance(61)  # 取り直しの試みは 1 分に 1 回まで。起動時の取得から 1 分たった

    assert client.get("/protected", headers=bearer(rotated_key.token())).status_code == 200
    assert certs_server.requests == 2
    assert client.get("/protected", headers=bearer(rotated_key.token())).status_code == 200
    assert certs_server.requests == 2  # 表に入ったので、もう取り直さない


def test_an_unknown_key_id_does_not_refetch_more_than_once_a_minute(client, rotated_key, certs_server, monotonic):
    monotonic.advance(61)

    for _ in range(10):
        assert_unauthenticated(client.get("/protected", headers=bearer(rotated_key.token())))
    assert certs_server.requests == 2  # 起動時 + 最初の未知の kid の 1 回だけ

    monotonic.advance(60)
    assert_unauthenticated(client.get("/protected", headers=bearer(rotated_key.token())))
    assert certs_server.requests == 3  # 1 分たったので、もう 1 回だけ


def test_a_known_key_keeps_working_when_a_refetch_fails_while_the_cache_is_valid(
    client, key, rotated_key, certs_server, monotonic
):
    certs_server.failure = httpx.Response(500)
    monotonic.advance(61)

    assert_unauthenticated(client.get("/protected", headers=bearer(rotated_key.token())))  # 未知の kid: 取り直しは失敗
    assert certs_server.requests == 2
    assert client.get("/protected", headers=bearer(key.token())).status_code == 200  # 既知の kid: キャッシュで動く


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(404),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"kid-1": 123}),
        httpx.ConnectError("unreachable"),
        httpx.ReadTimeout("slow"),
    ],
    ids=["500", "404", "not-json", "list", "empty", "non-string-pem", "connect-error", "timeout"],
)
def test_without_usable_certificates_the_caller_is_not_verified_and_gets_503(key, certs_server, monotonic, failure):
    certs_server.failure = failure
    certs = CallerCerts(CERTS_URL, transport=httpx.MockTransport(certs_server), now=monotonic)
    client = TestClient(make_app(CallerVerifier(audience=AUDIENCE, allowed_email=ALLOWED_EMAIL, certs=certs)))
    assert certs.refresh() is False

    response = client.get("/protected", headers=bearer(key.token()))

    assert response.status_code == 503
    assert response.json() == {"detail": "caller verification unavailable"}


def test_the_failed_fetch_is_not_repeated_by_every_request(key, certs_server, monotonic):
    certs_server.failure = httpx.Response(500)
    certs = CallerCerts(CERTS_URL, transport=httpx.MockTransport(certs_server), now=monotonic)
    client = TestClient(make_app(CallerVerifier(audience=AUDIENCE, allowed_email=ALLOWED_EMAIL, certs=certs)))

    assert certs.refresh() is False  # 起動時の取得が失敗した
    for _ in range(5):  # 要求のたびに取りに行かない(1 分に 1 回まで)
        assert client.get("/protected", headers=bearer(key.token())).status_code == 503
    assert certs_server.requests == 1

    certs_server.failure = None  # 置き場が戻った
    assert client.get("/protected", headers=bearer(key.token())).status_code == 503  # まだ 1 分たっていない
    monotonic.advance(60)
    assert client.get("/protected", headers=bearer(key.token())).status_code == 200


def test_an_expired_cache_that_cannot_be_refreshed_stops_verifying(client, key, certs_server, monotonic):
    certs_server.failure = httpx.Response(500)
    monotonic.advance(3601)  # キャッシュの期限が切れ、取り直しも失敗する

    assert client.get("/protected", headers=bearer(key.token())).status_code == 503


# --- 本物の金庫の app に掛けたとき ---


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "0123456789abcdef", path)


@pytest.fixture
def guarded_client(store, verifier) -> TestClient:
    return TestClient(create_app(store, caller_verifier=verifier))


def test_every_route_of_the_vault_requires_a_valid_token(guarded_client, key, same_kid_other_key):
    routes = guarded_client.app.routes
    assert all(isinstance(route, APIRoute) for route in routes)  # 認証の外にある素の経路・文書の経路がない
    assert len(routes) >= 14
    requests = [(method, _concrete(route.path)) for route in routes for method in sorted(route.methods)]
    assert ("GET", "/v1/principals/0123456789abcdef/policy") in requests

    for method, path in requests:
        assert_unauthenticated(guarded_client.request(method, path))
        assert_unauthenticated(guarded_client.request(method, path, headers=bearer(same_kid_other_key.token())))
        assert_forbidden(
            guarded_client.request(method, path, headers=bearer(key.token(email="other@demo-project.iam.gserviceaccount.com")))
        )
        # 通ったあとは、金庫自身の応答(本文がなければ 422、無ければ 404 など)になる。認証の応答ではない
        assert guarded_client.request(method, path, headers=bearer(key.token())).status_code not in (401, 403, 405)


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"])
def test_the_api_documentation_is_not_served_when_the_caller_is_verified(guarded_client, path):
    assert guarded_client.get(path).status_code == 404


def test_a_request_without_a_valid_token_does_not_reach_the_store(guarded_client, key):
    body = {"policy": accept_all_policy("candidate").model_dump(mode="json"), "removed_axes": []}
    path = "/v1/principals/0123456789abcdef/policy"

    assert_unauthenticated(guarded_client.put(path, json=body))
    assert guarded_client.get(path, headers=bearer(key.token())).status_code == 404  # 書かれていない

    assert guarded_client.put(path, json=body, headers=bearer(key.token())).status_code == 204
    assert guarded_client.get(path, headers=bearer(key.token())).status_code == 200


def test_without_a_verifier_the_vault_is_unchanged(store):
    # Cloud Run 版(既定)は、アプリの中では認証しない。文書の経路もそのまま。/v1/attestation はない。
    client = TestClient(create_app(store))

    assert client.get("/v1/principals/0123456789abcdef/policy").status_code == 404
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/v1/attestation", params={"nonce": "x" * 43}).status_code == 404
