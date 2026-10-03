"""TEE 版の金庫の attestation の口 `GET /v1/attestation?nonce=`(src/vault/tee/attestation_api.py・launcher.py。契約 §3)。

launcher は偽物(httpx.MockTransport)。要求の JSON の audience・token_type・nonces を記録して検査する。確かめること:

- 200 `{"token", "certificate_sha256"}`。launcher への要求は `POST http://localhost/v1/token`、本文は
  `{"audience": attestation_audience, "token_type": "OIDC", "nonces": [nonce, certificate_sha256]}`(この順。どちらも 10〜74 バイト)。
- 400 `{"detail": "invalid nonce"}`(形が違う・無い・2 個以上)。launcher は呼ばれない。
- 429 `{"detail": "attestation rate limited"}`(前回の launcher 呼び出しから min_attestation_interval_seconds 未満)。時計は差し込む。
- 503 `{"detail": "attestation unavailable"}`(launcher に届かない・2xx 以外・空)。detail は固定文で、launcher の応答を載せない。
- この口だけは認証なし(caller_verifier が全部断っても通る)。ほかの経路は認証が要る。attestation の部品を渡さない app には、この口がない。
- 本物の Unix ソケットの launcher(手元の HTTP サーバ)へ届く。本物の uvicorn の TLS の上で、web と同じ「その証明書だけを信用する」接続が
  通り、web が計算する証明書のハッシュが launcher に渡った nonce の 2 番目と一致する。
GCP には接続しない。
"""

import hashlib
import http.server
import json
import logging
import socket
import socketserver
import ssl
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from fastapi.testclient import TestClient

from vault.app import create_app
from vault.tee import tls
from vault.tee.attestation_api import AttestationService
from vault.tee.launcher import LAUNCHER_TOKEN_URL, LauncherClient, LauncherError

AUDIENCE = "https://vault.anon-nego.internal/attestation"
CERTIFICATE_SHA256 = "0123456789abcdef" * 4
TOKEN = "header.payload.signature"
NONCE = "kT7mN2xQ9vR4wZ8aB1cD5eF6gH3jL0pS7uY-_A2bC4d"  # 43 文字(32 バイトの base64url)


class FakeLauncher:
    """launcher の代わり(httpx.MockTransport のハンドラ)。要求を記録し、応答を差し替えられる。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.response: httpx.Response | Exception = httpx.Response(200, text=TOKEN)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def monotonic() -> FakeMonotonic:
    return FakeMonotonic()


@pytest.fixture
def service(launcher, monotonic) -> AttestationService:
    return AttestationService(
        audience=AUDIENCE,
        certificate_sha256=CERTIFICATE_SHA256,
        launcher=LauncherClient(socket_path="/unused/teeserver.sock", transport=httpx.MockTransport(launcher)),
        min_interval_seconds=1.0,
        now=monotonic,
    )


@pytest.fixture
def client(store, service) -> TestClient:
    return TestClient(create_app(store, attestation=service))


def get(client: TestClient, nonce: str | None = NONCE, **extra):
    params = {} if nonce is None else {"nonce": nonce}
    return client.get("/v1/attestation", params=params | extra)


# --- 200 と launcher への要求 ---


def test_the_attestation_endpoint_returns_the_token_and_the_certificate_hash(client):
    response = get(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"token": TOKEN, "certificate_sha256": CERTIFICATE_SHA256}


def test_the_launcher_is_asked_for_an_oidc_token_with_the_nonce_then_the_certificate_hash(client, launcher):
    get(client)

    [request] = launcher.requests
    assert request.method == "POST"
    assert str(request.url) == LAUNCHER_TOKEN_URL == "http://localhost/v1/token"
    assert request.headers["content-type"] == "application/json"
    assert launcher.bodies == [
        {"audience": AUDIENCE, "token_type": "OIDC", "nonces": [NONCE, CERTIFICATE_SHA256]}
    ]
    for nonce in launcher.bodies[0]["nonces"]:
        assert 10 <= len(nonce.encode()) <= 74  # launcher の制限(1 個 10〜74 バイト)


@pytest.mark.parametrize("length", [16, 43, 74])
def test_nonces_of_the_accepted_lengths_are_forwarded_as_they_are(client, launcher, length):
    nonce = ("aB3-_" * 20)[:length]

    assert get(client, nonce).status_code == 200
    assert launcher.bodies[0]["nonces"][0] == nonce


def test_the_launcher_token_is_returned_without_surrounding_whitespace(client, launcher):
    launcher.response = httpx.Response(200, text=f"  {TOKEN}\n")

    assert get(client).json()["token"] == TOKEN


# --- 400 ---


@pytest.mark.parametrize(
    "nonce",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("a" * 15, id="15-characters"),
        pytest.param("a" * 75, id="75-characters"),
        pytest.param("a" * 15 + "+" + "b" * 10, id="plus"),
        pytest.param("a" * 20 + "/", id="slash"),
        pytest.param("a" * 20 + "=", id="padding"),
        pytest.param("a" * 10 + " " + "b" * 10, id="space"),
        pytest.param("a" * 20 + "\n", id="trailing-newline"),
        pytest.param("\n" + "a" * 20, id="leading-newline"),
        pytest.param("あ" * 20, id="non-ascii"),
    ],
)
def test_a_malformed_nonce_is_400_and_the_launcher_is_not_called(client, launcher, nonce):
    response = get(client, nonce)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid nonce"}
    assert launcher.requests == []


def test_two_nonces_are_400(client, launcher):
    response = client.get("/v1/attestation", params=[("nonce", "a" * 20), ("nonce", "b" * 20)])

    assert response.status_code == 400
    assert launcher.requests == []


def test_a_bad_nonce_does_not_use_up_the_interval(client, launcher):
    assert get(client, "short").status_code == 400

    assert get(client).status_code == 200
    assert len(launcher.requests) == 1


# --- 429 ---


def test_a_second_call_within_the_interval_is_429_and_does_not_reach_the_launcher(client, launcher, monotonic):
    assert get(client).status_code == 200

    response = get(client, "b" * 43)

    assert response.status_code == 429
    assert response.json() == {"detail": "attestation rate limited"}
    assert len(launcher.requests) == 1


def test_the_interval_is_measured_from_the_last_launcher_call(client, launcher, monotonic):
    assert get(client).status_code == 200  # t = 0

    monotonic.advance(0.9)
    assert get(client).status_code == 429  # 断った呼び出しは、間隔の起点にならない
    monotonic.advance(0.1)  # t = 1.0
    assert get(client).status_code == 200
    assert len(launcher.requests) == 2


def test_a_failed_launcher_call_also_counts_toward_the_interval(client, launcher, monotonic):
    launcher.response = httpx.Response(500)
    assert get(client).status_code == 503

    assert get(client).status_code == 429  # launcher を呼んだので、すぐの呼び直しは断る(Google への要求を溢れさせない)
    launcher.response = httpx.Response(200, text=TOKEN)
    monotonic.advance(1.0)
    assert get(client).status_code == 200


# --- 503 ---


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(httpx.ConnectError("no socket"), id="unreachable"),
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
        pytest.param(httpx.Response(500, text="LEAK-MARKER"), id="500"),
        pytest.param(httpx.Response(404, text="LEAK-MARKER"), id="404"),
        pytest.param(httpx.Response(400, json={"error": "LEAK-MARKER"}), id="400"),
        pytest.param(httpx.Response(200, text=""), id="empty-body"),
        pytest.param(httpx.Response(200, text=" \n"), id="blank-body"),
    ],
)
def test_a_launcher_failure_is_503_with_a_fixed_message(client, launcher, caplog, failure):
    caplog.set_level(logging.DEBUG)
    launcher.response = failure

    response = get(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "attestation unavailable"}
    assert "LEAK-MARKER" not in response.text + caplog.text  # launcher の応答は、レスポンスにもログにも出ない


@pytest.mark.parametrize(
    ("failure", "logged"),
    [
        (httpx.Response(500, text="LEAK-MARKER"), "launcher returned 500"),
        (httpx.ConnectError("LEAK-MARKER"), "ConnectError"),
    ],
)
def test_the_reason_for_a_launcher_failure_is_logged_without_the_response(client, launcher, caplog, failure, logged):
    # ログに書くのは、ステータスか例外の型名だけ(launcher の応答の本文・例外の文は書かない)
    caplog.set_level(logging.WARNING)
    launcher.response = failure

    get(client)

    assert logged in caplog.text
    assert "LEAK-MARKER" not in caplog.text


def test_the_launcher_client_reports_each_failure_as_a_launcher_error():
    def client_for(handler) -> LauncherClient:
        return LauncherClient(socket_path="/unused/teeserver.sock", transport=httpx.MockTransport(handler))

    def refuse(request):
        raise httpx.ConnectError("no socket")

    with pytest.raises(LauncherError, match="ConnectError"):
        client_for(refuse).get_token(audience=AUDIENCE, nonces=[NONCE])
    with pytest.raises(LauncherError, match="503"):
        client_for(lambda request: httpx.Response(503)).get_token(audience=AUDIENCE, nonces=[NONCE])
    with pytest.raises(LauncherError, match="empty"):
        client_for(lambda request: httpx.Response(200, text="")).get_token(audience=AUDIENCE, nonces=[NONCE])


# --- 認証との関係・経路 ---


def test_only_the_attestation_endpoint_is_open_when_the_caller_must_authenticate(store, service):
    def deny_everyone() -> None:
        raise HTTPException(status_code=401, detail="unauthenticated")

    client = TestClient(create_app(store, caller_verifier=deny_everyone, attestation=service))

    assert get(client).status_code == 200
    assert client.get("/v1/principals/0123456789abcdef/policy").status_code == 401
    assert client.get("/v1/negotiations").status_code == 401
    assert client.get("/openapi.json").status_code == 404


def test_the_endpoint_is_get_only(client, launcher):
    assert client.post("/v1/attestation", params={"nonce": NONCE}).status_code == 405
    assert client.put("/v1/attestation", params={"nonce": NONCE}).status_code == 405
    assert launcher.requests == []


def test_an_app_without_the_attestation_part_has_no_such_endpoint(store):
    assert get(TestClient(create_app(store))).status_code == 404

    def deny_everyone() -> None:
        raise HTTPException(status_code=401, detail="unauthenticated")

    assert get(TestClient(create_app(store, caller_verifier=deny_everyone))).status_code == 404


# --- 本物の Unix ソケット ---


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def unix_socket_launcher(tmp_path, monkeypatch):
    """Unix ソケットで HTTP を話す、手元の launcher。ソケットの長いパスを避けるため、相対パスで使う。"""
    monkeypatch.chdir(tmp_path)
    received: list[tuple[str, str, dict]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers["Content-Type"], body))
            payload = b"unix.socket.token"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    server = _UnixHTTPServer("teeserver.sock", Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield "teeserver.sock", received
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_the_launcher_client_talks_to_a_unix_socket(unix_socket_launcher):
    socket_path, received = unix_socket_launcher

    token = LauncherClient(socket_path=socket_path).get_token(audience=AUDIENCE, nonces=[NONCE, CERTIFICATE_SHA256])

    assert token == "unix.socket.token"
    assert received == [
        ("/v1/token", "application/json", {"audience": AUDIENCE, "token_type": "OIDC", "nonces": [NONCE, CERTIFICATE_SHA256]})
    ]


def test_a_missing_unix_socket_is_a_launcher_error(tmp_path):
    with pytest.raises(LauncherError):
        LauncherClient(socket_path=str(tmp_path / "missing.sock")).get_token(audience=AUDIENCE, nonces=[NONCE])


# --- 本物の uvicorn の TLS の上で ---


def _pinning_context(cert_pem: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=cert_pem)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def test_the_pinned_tls_connection_reaches_the_attestation_endpoint_and_the_hash_matches(store, launcher, tmp_path):
    material = tls.generate(90)
    key_path, cert_path = tls.write(material, tmp_path / "tls")
    service = AttestationService(
        audience=AUDIENCE,
        certificate_sha256=material.certificate_sha256,
        launcher=LauncherClient(socket_path="/unused", transport=httpx.MockTransport(launcher)),
        min_interval_seconds=0.0,
    )

    def deny_everyone() -> None:
        raise HTTPException(status_code=401, detail="unauthenticated")

    app = create_app(store, caller_verifier=deny_everyone, attestation=service)
    listener = socket.create_server(("127.0.0.1", 0))
    host, port = listener.getsockname()
    server = uvicorn.Server(
        uvicorn.Config(app, ssl_keyfile=str(key_path), ssl_certfile=str(cert_path), log_level="warning")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started

        # web の手順(契約 §8): 証明書を検証なしで取って DER の SHA-256 を計算し、その 1 枚だけを信用する接続で呼ぶ
        presented_pem = ssl.get_server_certificate((host, port))
        certificate_sha256 = hashlib.sha256(ssl.PEM_cert_to_DER_cert(presented_pem)).hexdigest()
        assert certificate_sha256 == material.certificate_sha256

        with httpx.Client(verify=_pinning_context(presented_pem), base_url=f"https://{host}:{port}") as pinned:
            attestation = pinned.get("/v1/attestation", params={"nonce": NONCE})
            unauthenticated = pinned.get("/v1/principals/0123456789abcdef/policy")
        assert attestation.status_code == 200
        assert attestation.json() == {"token": TOKEN, "certificate_sha256": certificate_sha256}
        assert launcher.bodies[-1]["nonces"] == [NONCE, certificate_sha256]
        assert unauthenticated.status_code == 401

        # 別の証明書をピン留めした接続は、握手で断られる
        other = tls.generate(90)
        with httpx.Client(verify=_pinning_context(other.cert_pem.decode()), base_url=f"https://{host}:{port}") as wrong:
            with pytest.raises(httpx.ConnectError):
                wrong.get("/v1/attestation", params={"nonce": NONCE})
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
