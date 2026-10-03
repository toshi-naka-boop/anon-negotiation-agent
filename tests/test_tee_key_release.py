"""TEE 版の金庫の鍵の解放と起動(src/vault/tee/key_release.py・metadata.py・main.py・config.py の [vault.tee]。契約 §1・§4・§5)。

STS と Cloud KMS は偽物(httpx.MockTransport)、Firestore は conftest が起動するエミュレータ。確かめること:

- 鍵の解放: 初回は 32 バイトの DEK を作って KMS で包み、`_tee/dek` に `{wrapped_dek, kek, kek_version, created_at, image_digest}` を
  create() で書く(kek_version は encrypt の応答の name)。
  2 回目は書かずに、読んで decrypt する(同じ DEK)。STS の要求は契約どおり(audience は番号で組む)。KMS の鍵の名前は
  プロジェクト ID・リージョン・キーリング・鍵で組み、Bearer はアクセストークン。
- 鍵の版の確認(批評 C-56): 保管された DEK は、包んだ版(kek_version)が鍵の primary と完全に一致するときだけ解く。primary の版は、
  1 バイトの探りを encrypt した応答の name(`GET <鍵の名前>` は cloudkms.cryptoKeys.get が要り、本番で 403 になるので使わない)。
  一致しない・kek_version がない文書は、解かずに失敗する(固定文と 2 つの版の名前をログに書き、DEK も暗号文も書かない)。
- 失敗の扱い: STS・KMS の 4xx はやり直さずに失敗(ステータスだけをログに書く)。5xx・通信エラーは 2・4・8 秒空けて最大 3 回やり直す。
  先に別のインスタンスが `_tee/dek` を作っていたら、自分の DEK を捨てて、保管されている方を解く。
- 既定トークンの中身・アクセストークン・DEK は、ログにも例外の文にも出ない。
- メタデータサーバの読み取り(ヘッダ・値の形・リージョンの導き方)と、設定 [vault.tee] の読み込み。
- 封印の自己試験(批評 X-70): `_tee/selftest` は、なければ作り(probe・probe_sha256・created_at)、あれば開封して SHA-256 を確かめる。
  開かない・一致しない・項目がないときは、固定文をログに書いて失敗する(値は出さない)。
- 起動の順序(契約 §4)と、段が失敗したときの非 0 終了、uvicorn に渡す TLS と待ち受け。
GCP には接続しない。
"""

import base64
import dataclasses
import hashlib
import json
import logging
import logging.config
import os
import runpy
import ssl
import stat
import sys
from functools import partial
from pathlib import Path

import httpx
import pytest
import uvicorn.config
from fastapi.testclient import TestClient
from google.api_core.exceptions import AlreadyExists

import vault.tee.main as main_module
from vault.config import VaultTeeConfig, load_vault_config, load_vault_tee_config
from vault.tee.key_release import (
    KMS_BASE_URL,
    RETRY_DELAYS_SECONDS,
    STS_URL,
    KeyReleaseError,
    kms_key_name,
    release_dek,
    sts_audience,
)
from vault.tee.metadata import METADATA_BASE_URL, InstanceMetadata, MetadataError, read_instance_metadata
from vault.tee.sealing import SealError, Sealer

PROJECT_ID = "demo-project"
PROJECT_NUMBER = "123456789012"
REGION = "asia-northeast1"
METADATA = InstanceMetadata(
    project_id=PROJECT_ID,
    project_number=PROJECT_NUMBER,
    zone="asia-northeast1-b",
    region=REGION,
    instance_name="vault-tee-1",
)
KEY_NAME = f"projects/{PROJECT_ID}/locations/{REGION}/keyRings/vault-tee/cryptoKeys/vault-kek"
NON_PRIMARY = (
    "DEK was wrapped by a non-primary key version; refusing to start. "
    "If live data exists do NOT reset the DEK: re-enable and re-promote the stored version, or re-wrap the DEK."
)
DIGEST = "sha256:" + "ab" * 32
ACCESS_TOKEN = "ya29.ACCESS-TOKEN-SECRET"
SIGNATURE = "CLAIMS-SIGNATURE-SECRET"
KNOWN_DEK = bytes(range(32))
PARAMS_TOML = Path(__file__).resolve().parents[1] / "config" / "params.toml"


def version_name(number: int) -> str:
    """KMS の鍵の版の名前(encrypt の応答の name の形)。"""
    return f"{KEY_NAME}/cryptoKeyVersions/{number}"


def _b64url(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


# launcher が書く既定のトークンに似た、署名のない JWT(中身は秘密として扱い、ログに出ないことを確かめる)
CLAIMS_TOKEN = ".".join(
    [
        _b64url({"alg": "RS256"}),
        _b64url({"aud": "https://sts.googleapis.com", "submods": {"container": {"image_digest": DIGEST}}}),
        SIGNATURE,
    ]
)


def wrap(plaintext: bytes) -> bytes:
    """偽の KMS の「包み方」(本物の暗号ではない)。"""
    return b"WRAPPED|" + plaintext[::-1]


def unwrap(ciphertext: bytes) -> bytes:
    assert ciphertext.startswith(b"WRAPPED|")
    return ciphertext[len(b"WRAPPED|") :][::-1]


class FakeGoogle:
    """STS と Cloud KMS の代わり。要求を記録し、応答を先頭から順に差し替えられる(尽きたら成功を返す)。

    KMS の呼び出しは POST の encrypt・decrypt だけ(鍵の GET は、呼ばれたら落ちる。cloudkms.cryptoKeys.get は IAM に含まれない)。
    primary_version は、いま鍵の primary の版の番号。encrypt は常に primary の版を使うので、encrypt の応答の name がこの版の名前に
    なる(DEK を包む encrypt も、起動時に primary を調べる 1 バイトの探りの encrypt も)。
    """

    def __init__(self, primary_version: int = 1) -> None:
        self.primary_version = primary_version
        self.sts_requests: list[httpx.Request] = []
        self.kms_requests: list[tuple[str, httpx.Request]] = []
        self.sts_script: list[httpx.Response | Exception] = []
        self.kms_script: dict[str, list[httpx.Response | Exception]] = {"encrypt": [], "decrypt": []}
        self.on_encrypt = None  # 最初の encrypt の途中で 1 回だけ呼ぶ(別のインスタンスが先に作った、という競合の再現)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == STS_URL:
            self.sts_requests.append(request)
            scripted = self.sts_script.pop(0) if self.sts_script else None
            if isinstance(scripted, Exception):
                raise scripted
            if scripted is not None:
                return scripted
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        assert request.method == "POST" and url.startswith(f"{KMS_BASE_URL}{KEY_NAME}:"), (request.method, url)
        action = url.rsplit(":", 1)[1]
        self.kms_requests.append((action, request))
        scripted = self.kms_script[action].pop(0) if self.kms_script[action] else None
        if isinstance(scripted, Exception):
            raise scripted
        if scripted is not None:
            return scripted
        body = json.loads(request.content)
        if action == "encrypt":
            if self.on_encrypt is not None:
                hook, self.on_encrypt = self.on_encrypt, None
                hook()
            ciphertext = wrap(base64.b64decode(body["plaintext"]))
            return httpx.Response(
                200,
                json={"name": version_name(self.primary_version), "ciphertext": base64.b64encode(ciphertext).decode()},
            )
        plaintext = unwrap(base64.b64decode(body["ciphertext"]))
        return httpx.Response(200, json={"plaintext": base64.b64encode(plaintext).decode()})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def kms_calls(self, action: str) -> list[httpx.Request]:
        return [request for name, request in self.kms_requests if name == action]


def encrypted_plaintext(request: httpx.Request) -> bytes:
    """encrypt の要求で KMS に渡した平文(DEK か、1 バイトの探り)。"""
    return base64.b64decode(json.loads(request.content)["plaintext"])


@pytest.fixture
def config(tmp_path) -> VaultTeeConfig:
    claims_file = tmp_path / "attestation_verifier_claims_token"
    claims_file.write_text(CLAIMS_TOKEN + "\n")
    return dataclasses.replace(load_vault_tee_config(), claims_token_file=str(claims_file), tls_dir=str(tmp_path / "tls"))


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def release(firestore_client, config, google, sleeps):
    """release_dek を、偽の STS・KMS(既定は google フィクスチャ)と、記録する sleep で呼ぶ。"""

    def call(fake: FakeGoogle | None = None) -> bytes:
        return release_dek(
            firestore_client,
            metadata=METADATA,
            config=config,
            transport=(fake or google).transport,
            sleep=sleeps.append,
        )

    return call


def stored_dek_document(firestore_client) -> dict | None:
    return firestore_client.document("_tee/dek").get().to_dict()


def secret_forms(dek: bytes | None = None) -> list[str]:
    forms = [CLAIMS_TOKEN, SIGNATURE, ACCESS_TOKEN]
    if dek is not None:
        forms += [dek.hex(), base64.b64encode(dek).decode(), base64.urlsafe_b64encode(dek).decode(), repr(dek)]
    return forms


# --- 鍵の解放: 正常系 ---


def test_the_first_start_creates_a_dek_wraps_it_with_kms_and_stores_it(release, google, firestore_client):
    dek = release()

    assert isinstance(dek, bytes) and len(dek) == 32
    [encrypt] = google.kms_calls("encrypt")
    assert encrypted_plaintext(encrypt) == dek  # KMS に渡したのは、作った DEK(作るときは primary の探りをしない)
    document = stored_dek_document(firestore_client)
    assert set(document) == {"wrapped_dek", "kek", "kek_version", "created_at", "image_digest"}
    assert document["wrapped_dek"] == wrap(dek) and dek not in document["wrapped_dek"]
    assert document["kek"] == KEY_NAME
    assert document["kek_version"] == version_name(1)  # encrypt の応答の name
    assert document["image_digest"] == DIGEST  # 既定トークンの submods.container.image_digest(署名は見ない)
    assert document["created_at"] is not None  # サーバ時刻
    assert len(google.sts_requests) == 1 and google.kms_calls("decrypt") == []


def test_the_second_start_reads_the_stored_dek_and_decrypts_it_without_writing(release, firestore_client):
    first = release()
    before = stored_dek_document(firestore_client)
    second_google = FakeGoogle()

    second = release(second_google)

    assert second == first
    [probe] = second_google.kms_calls("encrypt")
    assert len(encrypted_plaintext(probe)) == 1  # DEK を包み直してはいない。あるのは primary を調べる 1 バイトの探りだけ
    assert len(second_google.kms_calls("decrypt")) == 1
    assert stored_dek_document(firestore_client) == before  # created_at も変わらない(書き直していない)
    [decrypt] = second_google.kms_calls("decrypt")
    assert json.loads(decrypt.content) == {"ciphertext": base64.b64encode(wrap(first)).decode()}


def test_the_sts_request_follows_the_contract(release, google, config):
    release()

    [request] = google.sts_requests
    assert request.method == "POST" and str(request.url) == "https://sts.googleapis.com/v1/token"
    assert "authorization" not in request.headers  # STS には Bearer を付けない(既定トークンが subject_token)
    assert json.loads(request.content) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "audience": "//iam.googleapis.com/projects/123456789012/locations/global"
        "/workloadIdentityPools/vault-tee-pool/providers/attestation-verifier",
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "subject_token": CLAIMS_TOKEN,  # ファイルの中身(前後の空白・改行は除く)
    }
    assert sts_audience(METADATA, config).startswith("//iam.googleapis.com/projects/123456789012/")  # 番号で組む(ID ではない)


def test_the_kms_requests_use_the_key_name_and_the_access_token(release, google, config):
    release()

    [encrypt] = google.kms_calls("encrypt")
    assert str(encrypt.url) == f"https://cloudkms.googleapis.com/v1/{KEY_NAME}:encrypt"
    assert encrypt.method == "POST" and encrypt.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert set(json.loads(encrypt.content)) == {"plaintext"}
    assert kms_key_name(METADATA, config) == KEY_NAME


def test_the_decrypt_request_also_uses_the_key_name_and_the_access_token(release):
    release()
    second_google = FakeGoogle()

    release(second_google)

    [decrypt] = second_google.kms_calls("decrypt")
    assert str(decrypt.url) == f"https://cloudkms.googleapis.com/v1/{KEY_NAME}:decrypt"
    assert decrypt.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


# --- 鍵の解放: 失敗 ---


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_an_sts_4xx_fails_at_once_without_retrying_and_logs_only_the_status(
    release, google, sleeps, firestore_client, caplog, status
):
    caplog.set_level(logging.DEBUG)
    google.sts_script = [httpx.Response(status, json={"error": "invalid_grant", "error_description": "SECRET-DESCRIPTION"})]

    with pytest.raises(KeyReleaseError) as raised:
        release()

    assert len(google.sts_requests) == 1 and sleeps == []
    assert google.kms_requests == [] and stored_dek_document(firestore_client) is None
    assert f"status {status}" in caplog.text
    for text in (caplog.text, str(raised.value)):
        assert "SECRET-DESCRIPTION" not in text and "invalid_grant" not in text  # 応答の本文は書かない(ステータスだけ)
        assert all(secret not in text for secret in secret_forms())


@pytest.mark.parametrize("action", ["encrypt", "decrypt"])
@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_kms_4xx_fails_at_once_without_retrying(release, google, sleeps, firestore_client, action, status):
    failing = google
    if action == "decrypt":
        release()  # 先に保管しておく
        failing = FakeGoogle()
    failing.kms_script[action] = [httpx.Response(status, json={"error": {"message": "SECRET-DESCRIPTION"}})]
    before = stored_dek_document(firestore_client)

    with pytest.raises(KeyReleaseError):
        release(failing)

    assert len(failing.kms_calls(action)) == 1 and sleeps == []
    assert stored_dek_document(firestore_client) == before  # encrypt が断られたら、何も保管しない


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_an_sts_5xx_is_retried_up_to_three_times_with_2_4_8_second_waits(release, google, sleeps, failures):
    google.sts_script = [httpx.Response(503)] * failures

    dek = release()

    assert len(dek) == 32
    assert len(google.sts_requests) == failures + 1
    assert sleeps == [2.0, 4.0, 8.0][:failures]
    assert RETRY_DELAYS_SECONDS == (2.0, 4.0, 8.0)


def test_an_sts_that_keeps_failing_gives_up_after_three_retries(release, google, sleeps, firestore_client):
    google.sts_script = [httpx.Response(500)] * 10

    with pytest.raises(KeyReleaseError):
        release()

    assert len(google.sts_requests) == 4  # 最初の 1 回 + やり直し 3 回
    assert sleeps == [2.0, 4.0, 8.0]
    assert google.kms_requests == [] and stored_dek_document(firestore_client) is None


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("unreachable"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("reset")]
)
def test_communication_errors_are_retried_like_5xx(release, google, sleeps, error):
    google.sts_script = [error, error]
    google.kms_script["encrypt"] = [error]

    assert len(release()) == 32

    assert sleeps == [2.0, 4.0, 2.0]  # STS で 2 回、KMS で 1 回(待ちは、呼び出しごとに 2 秒から数え直す)
    assert len(google.sts_requests) == 3 and len(google.kms_calls("encrypt")) == 2


def test_a_kms_5xx_is_retried_and_a_persistent_one_gives_up(release, google, sleeps, firestore_client):
    google.kms_script["encrypt"] = [httpx.Response(500)] * 10

    with pytest.raises(KeyReleaseError):
        release()

    assert len(google.kms_calls("encrypt")) == 4 and sleeps == [2.0, 4.0, 8.0]
    assert stored_dek_document(firestore_client) is None


@pytest.mark.parametrize(
    "sts_response",
    [
        pytest.param(httpx.Response(200, text="not json"), id="not-json"),
        pytest.param(httpx.Response(200, json=["not", "an", "object"]), id="not-an-object"),
        pytest.param(httpx.Response(200, json={}), id="no-access-token"),
        pytest.param(httpx.Response(200, json={"access_token": ""}), id="empty-access-token"),
        pytest.param(httpx.Response(200, json={"access_token": 123}), id="numeric-access-token"),
    ],
)
def test_an_sts_success_without_an_access_token_fails_without_retrying(release, google, sleeps, sts_response):
    google.sts_script = [sts_response]

    with pytest.raises(KeyReleaseError):
        release()

    assert len(google.sts_requests) == 1 and sleeps == []


@pytest.mark.parametrize(
    "kms_response",
    [
        pytest.param(httpx.Response(200, text="not json"), id="not-json"),
        pytest.param(httpx.Response(200, json={}), id="no-ciphertext"),
        pytest.param(httpx.Response(200, json={"ciphertext": 123}), id="numeric-ciphertext"),
        pytest.param(httpx.Response(200, json={"ciphertext": "***not base64***"}), id="not-base64"),
        pytest.param(httpx.Response(200, json={"ciphertext": ""}), id="empty-ciphertext"),
    ],
)
def test_a_kms_success_without_a_usable_ciphertext_fails_and_stores_nothing(
    release, google, firestore_client, sleeps, kms_response
):
    google.kms_script["encrypt"] = [kms_response]

    with pytest.raises(KeyReleaseError):
        release()

    assert sleeps == [] and stored_dek_document(firestore_client) is None


@pytest.mark.parametrize(
    "stored",
    [{}, {"wrapped_dek": "text"}, {"wrapped_dek": None}, {"wrapped_dek": 12}],
    ids=["no-field", "a-string", "null", "a-number"],
)
def test_a_stored_document_without_a_wrapped_dek_fails(release, google, firestore_client, stored):
    firestore_client.document("_tee/dek").set(stored)

    with pytest.raises(KeyReleaseError):
        release()

    assert google.kms_requests == []


def test_a_decrypted_dek_of_the_wrong_size_fails(release, firestore_client):
    firestore_client.document("_tee/dek").set({"wrapped_dek": wrap(b"short"), "kek_version": version_name(1)})

    with pytest.raises(KeyReleaseError, match="32 bytes"):
        release()


@pytest.mark.parametrize("content", [None, "", "  \n"], ids=["missing-file", "empty", "blank"])
def test_an_unreadable_or_empty_claims_token_file_fails_before_any_request(release, google, config, content):
    if content is None:
        os.remove(config.claims_token_file)
    else:
        with open(config.claims_token_file, "w") as f:
            f.write(content)

    with pytest.raises(KeyReleaseError):
        release()

    assert google.sts_requests == [] and google.kms_requests == []


def test_when_another_instance_created_the_dek_first_its_dek_is_used(release, google, firestore_client):
    other_dek = b"\x07" * 32
    google.on_encrypt = lambda: firestore_client.document("_tee/dek").set(
        {"wrapped_dek": wrap(other_dek), "kek_version": version_name(1)}
    )

    dek = release()

    assert dek == other_dek  # 自分が作った DEK は捨てて、保管されている方を使う
    assert len(google.kms_calls("decrypt")) == 1
    assert stored_dek_document(firestore_client)["wrapped_dek"] == wrap(other_dek)  # 上書きしていない


def test_a_stored_dek_that_vanishes_after_a_lost_race_is_a_release_error(config, google, sleeps):
    class Snapshot:
        exists = False

        def to_dict(self):
            return None

    class Ref:
        def get(self):
            return Snapshot()

        def create(self, data):
            raise AlreadyExists("the document already exists")

    class Db:
        def collection(self, name):
            return self

        def document(self, name):
            return Ref()

    with pytest.raises(KeyReleaseError, match="missing or malformed"):
        release_dek(Db(), metadata=METADATA, config=config, transport=google.transport, sleep=sleeps.append)


@pytest.mark.parametrize(
    "broken",
    [
        "not-a-jwt",
        "a.b.c",
        f"x.{_b64url({'submods': 'text'})}.y",
        f"x.{_b64url({'submods': {'container': {'image_digest': 5}}})}.y",
        f"x.{_b64url(['not', 'an', 'object'])}.y",
    ],
    ids=["not-a-jwt", "not-base64-json", "submods-is-text", "digest-is-a-number", "payload-is-a-list"],
)
def test_the_image_digest_is_none_when_the_claims_token_does_not_have_one(release, firestore_client, config, broken):
    with open(config.claims_token_file, "w") as f:
        f.write(broken)

    release()

    assert stored_dek_document(firestore_client)["image_digest"] is None


@pytest.mark.parametrize("stored_before", [False, True], ids=["first-start", "restart"])
def test_neither_the_tokens_nor_the_dek_reach_the_log(release, firestore_client, caplog, stored_before):
    caplog.set_level(logging.DEBUG)
    if stored_before:
        release()
    flaky = FakeGoogle()
    # 例外の文にトークンが入っていても、書かれない(型名だけを書く)
    flaky.sts_script = [httpx.Response(503), httpx.ConnectError(ACCESS_TOKEN + CLAIMS_TOKEN)]

    dek = release(flaky)

    assert "key release" in caplog.text  # 何かは書いている
    assert all(secret not in caplog.text for secret in secret_forms(dek))


def test_no_secret_reaches_the_log_or_the_error_when_the_release_fails(release, google, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("vault.tee.key_release.secrets.token_bytes", lambda size: KNOWN_DEK)
    google.kms_script["encrypt"] = [httpx.Response(403, text=f"denied {ACCESS_TOKEN} {CLAIMS_TOKEN} {KNOWN_DEK.hex()}")]

    with pytest.raises(KeyReleaseError) as raised:
        release()

    for text in (caplog.text, str(raised.value), repr(raised.value)):
        assert all(secret not in text for secret in secret_forms(KNOWN_DEK))


# --- 鍵の版の確認(批評 C-56) ---


def test_the_version_that_encrypt_reports_is_stored_as_kek_version(release, google, firestore_client):
    google.primary_version = 7

    release()

    assert stored_dek_document(firestore_client)["kek_version"] == version_name(7)
    assert len(google.kms_calls("encrypt")) == 1  # 作るときは探りをしない(DEK を包む encrypt が使った版を、そのまま保存する)


def test_the_stored_dek_is_decrypted_only_after_the_primary_version_is_checked_with_a_one_byte_encrypt(release):
    first = release()
    second_google = FakeGoogle()

    second = release(second_google)

    assert second == first
    assert [name for name, _ in second_google.kms_requests] == ["encrypt", "decrypt"]  # 解く前に、探りの encrypt で primary を調べる
    [probe, decrypt] = [request for _, request in second_google.kms_requests]
    assert probe.method == "POST" and str(probe.url) == f"https://cloudkms.googleapis.com/v1/{KEY_NAME}:encrypt"
    assert probe.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert len(encrypted_plaintext(probe)) == 1
    assert str(decrypt.url) == f"https://cloudkms.googleapis.com/v1/{KEY_NAME}:decrypt"  # 鍵の GET はしない(FakeGoogle が落ちる)


def test_a_dek_wrapped_by_a_version_that_is_no_longer_primary_is_refused_without_decrypting(
    release, firestore_client, caplog
):
    # 運営者が、debug イメージの間に写した _tee/dek を、本番へ切り替えたあとに書き戻した。鍵は新しい版(v2)が primary になっている。
    caplog.set_level(logging.DEBUG)
    dek = release()  # v1 で包んだ DEK
    rotated = FakeGoogle(primary_version=2)
    before = stored_dek_document(firestore_client)

    with pytest.raises(KeyReleaseError) as raised:
        release(rotated)

    assert str(raised.value) == NON_PRIMARY
    assert rotated.kms_calls("decrypt") == []  # 解いていない
    assert NON_PRIMARY in caplog.text
    assert version_name(1) in caplog.text and version_name(2) in caplog.text  # 2 つの版の名前
    wrapped, probe_ciphertext = wrap(dek), wrap(b"\x00")
    leaked = [
        *secret_forms(dek),
        *(form for ciphertext in (wrapped, probe_ciphertext) for form in (base64.b64encode(ciphertext).decode(), ciphertext.hex(), repr(ciphertext))),
    ]
    assert all(secret not in caplog.text + str(raised.value) for secret in leaked)  # DEK も暗号文(探りの分も)も書かない
    assert stored_dek_document(firestore_client) == before  # 文書は変えない(直すのは運営者の手順)


def test_a_document_from_before_kek_version_existed_is_refused_too(release, google, firestore_client, caplog):
    caplog.set_level(logging.DEBUG)
    firestore_client.document("_tee/dek").set({"wrapped_dek": wrap(KNOWN_DEK), "kek": KEY_NAME})

    with pytest.raises(KeyReleaseError, match="non-primary"):
        release()

    assert google.kms_calls("decrypt") == []
    assert NON_PRIMARY in caplog.text and "stored: None" in caplog.text


@pytest.mark.parametrize(
    "stored_version",
    [
        pytest.param(version_name(2), id="another-version"),
        pytest.param(version_name(11), id="longer-number"),
        pytest.param(version_name(1) + " ", id="trailing-space"),
        pytest.param(version_name(1).upper(), id="another-case"),
        pytest.param(version_name(1)[:-2], id="truncated"),
        pytest.param("", id="empty"),
        pytest.param(1, id="number"),
        pytest.param(["x"], id="list"),
    ],
)
def test_only_an_exact_match_with_the_primary_version_is_accepted(release, google, firestore_client, stored_version):
    firestore_client.document("_tee/dek").set({"wrapped_dek": wrap(KNOWN_DEK), "kek_version": stored_version})

    with pytest.raises(KeyReleaseError, match="non-primary"):
        release()

    assert google.kms_calls("decrypt") == []


def test_an_exact_match_is_accepted_and_the_dek_is_decrypted(release, google, firestore_client):
    google.primary_version = 3
    firestore_client.document("_tee/dek").set({"wrapped_dek": wrap(KNOWN_DEK), "kek_version": version_name(3)})

    assert release() == KNOWN_DEK


def test_the_stored_version_cannot_forge_log_lines_or_flood_the_log(release, google, firestore_client, caplog):
    caplog.set_level(logging.DEBUG)
    forged = "v1\nERROR forged: DEK was fine" + "x" * 1000  # 運営者が書き換えられる値
    firestore_client.document("_tee/dek").set({"wrapped_dek": wrap(KNOWN_DEK), "kek_version": forged})

    with pytest.raises(KeyReleaseError):
        release()

    assert "\nERROR forged" not in caplog.text  # 改行は repr で escape される
    assert "x" * 201 not in caplog.text  # 長さを抑える


def test_the_race_path_also_refuses_a_dek_wrapped_by_a_non_primary_version(release, google, firestore_client):
    google.primary_version = 2
    google.on_encrypt = lambda: firestore_client.document("_tee/dek").set(
        {"wrapped_dek": wrap(KNOWN_DEK), "kek_version": version_name(1)}
    )

    with pytest.raises(KeyReleaseError, match="non-primary"):
        release()

    assert google.kms_calls("decrypt") == []


def test_a_kms_4xx_on_the_primary_probe_fails_without_retrying_and_does_not_decrypt(release, firestore_client, sleeps):
    release()
    denied = FakeGoogle()
    denied.kms_script["encrypt"] = [httpx.Response(403, json={"error": {"message": "SECRET-DESCRIPTION"}})]

    with pytest.raises(KeyReleaseError, match="primary probe"):
        release(denied)

    assert len(denied.kms_calls("encrypt")) == 1 and denied.kms_calls("decrypt") == [] and sleeps == []


def test_a_kms_5xx_on_the_primary_probe_is_retried(release, firestore_client, sleeps):
    first = release()
    flaky = FakeGoogle()
    flaky.kms_script["encrypt"] = [httpx.Response(503)]

    assert release(flaky) == first

    assert len(flaky.kms_calls("encrypt")) == 2 and sleeps == [2.0]


@pytest.mark.parametrize(
    "probe_response",
    [
        pytest.param(httpx.Response(200, text="not json"), id="not-json"),
        pytest.param(httpx.Response(200, json={"ciphertext": "AA=="}), id="no-name"),
        pytest.param(httpx.Response(200, json={"name": "", "ciphertext": "AA=="}), id="empty-name"),
        pytest.param(httpx.Response(200, json={"name": 1, "ciphertext": "AA=="}), id="numeric-name"),
        pytest.param(httpx.Response(200, json={"name": [version_name(1)], "ciphertext": "AA=="}), id="name-in-a-list"),
        pytest.param(httpx.Response(200, json={"name": version_name(1)}), id="no-ciphertext"),
    ],
)
def test_a_probe_response_without_a_usable_version_name_is_not_a_reason_to_decrypt(release, firestore_client, probe_response):
    release()
    broken = FakeGoogle()
    broken.kms_script["encrypt"] = [probe_response]

    with pytest.raises(KeyReleaseError) as raised:
        release(broken)

    assert broken.kms_calls("decrypt") == []
    assert NON_PRIMARY not in str(raised.value)  # 版の不一致ではなく、primary を調べられなかった失敗


@pytest.mark.parametrize(
    "encrypt_response",
    [
        pytest.param(httpx.Response(200, json={"ciphertext": base64.b64encode(wrap(KNOWN_DEK)).decode()}), id="no-name"),
        pytest.param(
            httpx.Response(200, json={"name": "", "ciphertext": base64.b64encode(wrap(KNOWN_DEK)).decode()}),
            id="empty-name",
        ),
        pytest.param(
            httpx.Response(200, json={"name": 5, "ciphertext": base64.b64encode(wrap(KNOWN_DEK)).decode()}),
            id="numeric-name",
        ),
    ],
)
def test_an_encrypt_response_without_a_key_version_name_stores_nothing(
    release, google, firestore_client, encrypt_response
):
    google.kms_script["encrypt"] = [encrypt_response]

    with pytest.raises(KeyReleaseError):
        release()

    assert stored_dek_document(firestore_client) is None


# --- メタデータサーバ ---


def metadata_server(**overrides):
    values = {
        "project/project-id": PROJECT_ID,
        "project/numeric-project-id": PROJECT_NUMBER,
        "instance/zone": f"projects/{PROJECT_NUMBER}/zones/asia-northeast1-b",
        "instance/name": "vault-tee-1",
    } | overrides
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        value = values[request.url.path.removeprefix("/computeMetadata/v1/")]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, text=value + "\n")

    return httpx.MockTransport(handler), requests


def test_the_instance_metadata_is_read_from_the_metadata_server():
    transport, requests = metadata_server()

    assert read_instance_metadata(transport=transport) == METADATA

    assert sorted(str(request.url) for request in requests) == sorted(
        METADATA_BASE_URL + path
        for path in ("project/project-id", "project/numeric-project-id", "instance/zone", "instance/name")
    )
    assert all(request.headers["Metadata-Flavor"] == "Google" for request in requests)


@pytest.mark.parametrize(
    ("zone", "region"),
    [
        ("asia-northeast1-b", "asia-northeast1"),
        ("us-central1-a", "us-central1"),
        ("europe-west12-c", "europe-west12"),
        ("northamerica-northeast1-b", "northamerica-northeast1"),
    ],
)
def test_the_region_is_the_zone_without_its_last_letter(zone, region):
    transport, _ = metadata_server(**{"instance/zone": f"projects/{PROJECT_NUMBER}/zones/{zone}"})

    metadata = read_instance_metadata(transport=transport)

    assert (metadata.zone, metadata.region) == (zone, region)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"project/project-id": httpx.Response(404)}, id="404"),
        pytest.param({"instance/name": httpx.Response(500)}, id="500"),
        pytest.param({"project/project-id": httpx.ConnectError("no metadata server")}, id="unreachable"),
        pytest.param({"project/project-id": ""}, id="empty-project-id"),
        pytest.param({"instance/name": "  "}, id="blank-instance-name"),
        pytest.param({"project/project-id": "demo/project"}, id="slash-in-project-id"),
        pytest.param({"project/project-id": "demo?x=1"}, id="query-in-project-id"),
        pytest.param({"project/numeric-project-id": "12ab"}, id="non-numeric-project-number"),
        pytest.param({"instance/zone": "projects/1/zones/asia-northeast1"}, id="zone-without-a-letter"),
        pytest.param({"instance/zone": "projects/1/zones/asia-northeast1-b/../x"}, id="zone-path-trick"),
        pytest.param({"instance/zone": ""}, id="empty-zone"),
    ],
)
def test_metadata_that_cannot_be_read_or_is_malformed_fails(overrides):
    transport, _ = metadata_server(**overrides)

    with pytest.raises(MetadataError):
        read_instance_metadata(transport=transport)


# --- 設定 [vault.tee] ---


def test_the_tee_settings_are_read_from_params_toml():
    assert load_vault_tee_config() == VaultTeeConfig(
        port=8443,
        attestation_audience="https://vault.anon-nego.internal/attestation",
        caller_audience="https://vault.anon-nego.internal",
        caller_service_account="web-run",
        workload_identity_pool="vault-tee-pool",
        workload_identity_provider="attestation-verifier",
        kms_key_ring="vault-tee",
        kms_key="vault-kek",
        launcher_socket="/run/container_launcher/teeserver.sock",
        claims_token_file="/run/container_launcher/attestation_verifier_claims_token",
        min_attestation_interval_seconds=1.0,
        tls_certificate_days=90,
        tls_dir="/dev/shm/vault-tls",
        allowed_hwmodels=("GCP_AMD_SEV", "GCP_INTEL_TDX"),
        attestation_issuer="https://confidentialcomputing.googleapis.com",
        attestation_signer_certs_url=(
            "https://www.googleapis.com/service_accounts/v1/metadata/x509/signer@confidentialspace-sign.iam.gserviceaccount.com"
        ),
        caller_certs_url="https://www.googleapis.com/oauth2/v1/certs",
    )


def test_a_missing_tee_section_or_key_is_a_value_error_and_the_cloud_run_settings_do_not_need_it(tmp_path):
    without_tee = tmp_path / "without_tee.toml"
    without_tee.write_text(PARAMS_TOML.read_text().split("[vault.tee]")[0])
    with pytest.raises(ValueError, match=r"\[vault\.tee\]"):
        load_vault_tee_config(without_tee)
    assert load_vault_config(without_tee).t_high == 10  # Cloud Run 版は [vault.tee] を読まない

    missing_key = tmp_path / "missing_key.toml"
    missing_key.write_text(PARAMS_TOML.read_text().replace("kms_key = ", "kms_key_renamed = "))
    with pytest.raises(ValueError, match=r"\[vault\.tee\]"):
        load_vault_tee_config(missing_key)


def test_an_unknown_tee_key_is_a_value_error(tmp_path):
    unknown = tmp_path / "unknown.toml"
    unknown.write_text(PARAMS_TOML.read_text() + 'extra_key = "x"\n')

    with pytest.raises(ValueError, match="extra_key"):
        load_vault_tee_config(unknown)


# --- 封印の自己試験(批評 X-70) ---

SELFTEST_FAILED = "sealing self-test failed: existing ciphertext does not open"
SELFTEST_PATH = "_tee/selftest"


def stored_selftest(firestore_client) -> dict | None:
    return firestore_client.document(SELFTEST_PATH).get().to_dict()


def test_the_first_start_creates_the_probe_with_its_hash(firestore_client, caplog):
    caplog.set_level(logging.INFO)
    sealer = Sealer(os.urandom(32))

    main_module.run_sealing_self_test(firestore_client, sealer)

    document = stored_selftest(firestore_client)
    assert set(document) == {"probe", "probe_sha256", "created_at"}
    assert isinstance(document["probe"], bytes) and len(document["probe"]) == 12 + 32 + 16  # nonce + 乱数 32 バイト + タグ
    opened = sealer.open(SELFTEST_PATH, "probe", document["probe"])
    assert len(opened) == 32 and document["probe_sha256"] == hashlib.sha256(opened).hexdigest()  # 小文字の 16 進
    assert document["created_at"] is not None
    assert "sealing self-test ok" in caplog.text


def test_a_restart_with_the_same_dek_opens_the_existing_probe_without_rewriting_it(firestore_client, caplog):
    dek = os.urandom(32)
    main_module.run_sealing_self_test(firestore_client, Sealer(dek))
    before = stored_selftest(firestore_client)
    caplog.clear()
    caplog.set_level(logging.INFO)

    main_module.run_sealing_self_test(firestore_client, Sealer(dek))  # 再起動(同じ DEK から作った別の Sealer)

    assert stored_selftest(firestore_client) == before  # 書き直さない(probe も created_at も変わらない)
    assert "sealing self-test ok" in caplog.text and "created the probe" not in caplog.text


def test_a_probe_sealed_by_another_dek_fails_the_self_test_without_logging_any_value(firestore_client, caplog):
    dek, other_dek = os.urandom(32), os.urandom(32)
    main_module.run_sealing_self_test(firestore_client, Sealer(dek))
    foreign_probe = os.urandom(32)
    foreign = Sealer(other_dek).seal(SELFTEST_PATH, "probe", foreign_probe)
    firestore_client.document(SELFTEST_PATH).update({"probe": foreign})  # DEK の作り直しのあとに残った、古い DEK の暗号文
    sha256 = stored_selftest(firestore_client)["probe_sha256"]
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SealError) as raised:
        main_module.run_sealing_self_test(firestore_client, Sealer(dek))

    assert str(raised.value) == SELFTEST_FAILED
    assert SELFTEST_FAILED in caplog.text and "sealing self-test ok" not in caplog.text
    leaked = [dek.hex(), other_dek.hex(), foreign_probe.hex(), foreign.hex(), repr(foreign), sha256]
    assert all(value not in caplog.text + str(raised.value) for value in leaked)  # 値は出さない


def _flipped(sealed: bytes) -> bytes:
    flipped = bytearray(sealed)
    flipped[20] ^= 0x01
    return bytes(flipped)


def _sealed_elsewhere(dek: bytes, path: str, field: str) -> dict:
    """ハッシュは合っているが、別の文書・項目として封印した probe(AAD が違うので、開かない)。"""
    probe = os.urandom(32)
    return {"probe": Sealer(dek).seal(path, field, probe), "probe_sha256": hashlib.sha256(probe).hexdigest()}


TAMPERED_DOCUMENTS = {
    "wrong-hash": lambda doc, dek: {**doc, "probe_sha256": "0" * 64},
    "upper-case-hash": lambda doc, dek: {**doc, "probe_sha256": doc["probe_sha256"].upper()},
    "no-hash": lambda doc, dek: {key: value for key, value in doc.items() if key != "probe_sha256"},  # 古い形の文書
    "numeric-hash": lambda doc, dek: {**doc, "probe_sha256": 123},
    "no-probe": lambda doc, dek: {key: value for key, value in doc.items() if key != "probe"},
    "probe-is-text": lambda doc, dek: {**doc, "probe": "text"},
    "probe-is-null": lambda doc, dek: {**doc, "probe": None},
    "probe-too-short": lambda doc, dek: {**doc, "probe": b"\x00" * 10},
    "probe-bit-flipped": lambda doc, dek: {**doc, "probe": _flipped(doc["probe"])},
    "sealed-for-another-document": lambda doc, dek: _sealed_elsewhere(dek, "_tee/dek", "probe"),
    "sealed-for-another-field": lambda doc, dek: _sealed_elsewhere(dek, SELFTEST_PATH, "other"),
    "empty-document": lambda doc, dek: {},
}


@pytest.mark.parametrize("tamper", TAMPERED_DOCUMENTS.values(), ids=TAMPERED_DOCUMENTS.keys())
def test_an_existing_probe_that_does_not_open_or_does_not_match_its_hash_fails(firestore_client, caplog, tamper):
    dek = os.urandom(32)
    main_module.run_sealing_self_test(firestore_client, Sealer(dek))
    firestore_client.document(SELFTEST_PATH).set(tamper(stored_selftest(firestore_client), dek))
    caplog.clear()
    caplog.set_level(logging.INFO)

    with pytest.raises(SealError, match="existing ciphertext does not open"):
        main_module.run_sealing_self_test(firestore_client, Sealer(dek))

    assert SELFTEST_FAILED in caplog.text and "sealing self-test ok" not in caplog.text


def test_a_sealer_that_does_not_round_trip_fails_the_self_test_even_when_it_creates_the_probe(firestore_client, caplog):
    caplog.set_level(logging.INFO)

    class BrokenSealer(Sealer):
        def open(self, path, field, sealed):
            return b"not the probe"

    with pytest.raises(SealError):
        main_module.run_sealing_self_test(firestore_client, BrokenSealer(os.urandom(32)))

    assert "sealing self-test ok" not in caplog.text


class _Snapshot:
    def __init__(self, data: dict | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict | None:
        return self._data


class _ProbeCreatedByAnotherInstance:
    """get() の最初の答えは「まだない」、create() は「もうある」、次の get() は別のインスタンスが作った文書。"""

    def __init__(self, theirs: dict) -> None:
        self._theirs = theirs
        self._reads = 0

    def document(self, path: str):
        return self

    def get(self) -> _Snapshot:
        self._reads += 1
        return _Snapshot(None if self._reads == 1 else self._theirs)

    def create(self, data: dict) -> None:
        raise AlreadyExists("the document already exists")


def test_when_another_instance_created_the_probe_first_that_probe_is_the_one_checked(caplog):
    caplog.set_level(logging.INFO)
    dek = os.urandom(32)
    theirs = _sealed_elsewhere(dek, SELFTEST_PATH, "probe")

    main_module.run_sealing_self_test(_ProbeCreatedByAnotherInstance(theirs), Sealer(dek))

    assert "sealing self-test ok" in caplog.text
    with pytest.raises(SealError):  # その probe が、別の DEK のものなら失敗する
        main_module.run_sealing_self_test(_ProbeCreatedByAnotherInstance(theirs), Sealer(os.urandom(32)))


# --- 起動の順序 ---

STEP_ORDER = ["config", "metadata", "firestore", "key release", "store", "sealing self-test", "tls", "app", "serve"]
PATCHED_NAME = {
    "config": "load_vault_tee_config",
    "metadata": "read_instance_metadata",
    "firestore": "create_client",
    "key release": "release_dek",
    "store": "VaultStore",
    "sealing self-test": "run_sealing_self_test",
    "tls": "prepare_tls",
    "app": "build_app",
}


class Startup:
    """main() の各段を記録するために差し替えた環境。VaultStore・封印の自己試験・TLS・app の組み立ては本物のまま動く。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.served: dict = {}
        self.constructed: dict[str, dict] = {}
        self.certs_requests = 0
        self.certs_status = 200


@pytest.fixture
def startup(monkeypatch, config, firestore_client, caplog) -> Startup:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)  # root にハンドラを足さない
    state = Startup()

    def recorded(name, function):
        def call(*args, **kwargs):
            state.calls.append(name)
            return function(*args, **kwargs)

        return call

    def create_client(project=None):
        state.constructed["firestore"] = {"project": project}
        return firestore_client

    def certs_transport(request: httpx.Request) -> httpx.Response:
        state.certs_requests += 1
        assert str(request.url) == config.caller_certs_url
        return httpx.Response(state.certs_status, json={"kid-1": "-----BEGIN CERTIFICATE-----\nplaceholder\n-----END CERTIFICATE-----"})

    def recording_constructor(name, cls, **extra):
        def construct(*args, **kwargs):
            state.constructed[name] = kwargs | {"_args": args}
            return cls(*args, **kwargs, **extra)

        return construct

    def serve(app, serve_config, key_path, cert_path):
        state.served.update(app=app, config=serve_config, key_path=key_path, cert_path=cert_path)

    patches = {
        "mask_ids_in_logs": recorded("mask_ids_in_logs", main_module.mask_ids_in_logs),
        "load_vault_tee_config": recorded("config", lambda: config),
        "read_instance_metadata": recorded("metadata", lambda: METADATA),
        "create_client": recorded("firestore", create_client),
        "release_dek": recorded("key release", lambda db, *, metadata, config: KNOWN_DEK),
        "VaultStore": recorded("store", main_module.VaultStore),
        "run_sealing_self_test": recorded("sealing self-test", main_module.run_sealing_self_test),
        "prepare_tls": recorded("tls", main_module.prepare_tls),
        "build_app": recorded("app", main_module.build_app),
        "serve": recorded("serve", serve),
        "CallerCerts": recording_constructor(
            "certs", main_module.CallerCerts, transport=httpx.MockTransport(certs_transport)
        ),
        "CallerVerifier": recording_constructor("verifier", main_module.CallerVerifier),
        "AttestationService": recording_constructor("attestation", main_module.AttestationService),
        "LauncherClient": recording_constructor("launcher", main_module.LauncherClient),
    }
    for name, replacement in patches.items():
        monkeypatch.setattr(main_module, name, replacement)
    return state


def test_the_startup_runs_the_steps_in_the_order_of_the_contract(startup, firestore_client):
    assert main_module.main() == 0

    assert startup.calls == ["mask_ids_in_logs", *STEP_ORDER]
    assert startup.constructed["firestore"] == {"project": PROJECT_ID}  # メタデータのプロジェクト ID を明示する
    assert firestore_client.document("_tee/selftest").get().exists  # 本物の自己試験が走った


def test_the_app_is_wired_with_the_verifier_the_attestation_part_and_the_served_certificate(startup, config):
    main_module.main()

    served = startup.served
    assert served["config"] == config
    key_path, cert_path = served["key_path"], served["cert_path"]
    assert stat.S_IMODE(os.stat(config.tls_dir).st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == stat.S_IMODE(cert_path.stat().st_mode) == 0o600
    served_sha256 = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_path.read_text())).hexdigest()

    verifier = startup.constructed["verifier"]
    assert verifier["audience"] == config.caller_audience
    assert verifier["allowed_email"] == "web-run@demo-project.iam.gserviceaccount.com"  # <名前>@<メタデータのプロジェクト ID>
    attestation = startup.constructed["attestation"]
    assert attestation["audience"] == config.attestation_audience
    assert attestation["certificate_sha256"] == served_sha256  # 実際に配る証明書のハッシュを launcher の nonce に入れる
    assert attestation["min_interval_seconds"] == config.min_attestation_interval_seconds
    assert startup.constructed["launcher"]["socket_path"] == config.launcher_socket
    assert startup.constructed["certs"]["_args"] == (config.caller_certs_url,)

    client = TestClient(served["app"])
    assert client.get("/v1/attestation").status_code == 400  # 認証なしで届き、nonce を求める
    assert client.get("/v1/principals/0123456789abcdef/policy").status_code == 401
    assert client.get("/openapi.json").status_code == 404


def test_the_caller_certificates_are_fetched_once_at_startup(startup):
    assert main_module.main() == 0

    assert startup.certs_requests == 1


def test_a_failed_fetch_of_the_caller_certificates_does_not_stop_the_startup(startup, caplog):
    startup.certs_status = 500

    assert main_module.main() == 0

    assert startup.calls[-1] == "serve"
    assert "caller certs could not be fetched at startup" in caplog.text


def test_a_second_start_with_the_same_dek_verifies_the_probe_the_first_start_created(startup, firestore_client, caplog):
    assert main_module.main() == 0
    created = stored_selftest(firestore_client)

    assert main_module.main() == 0

    assert stored_selftest(firestore_client) == created
    assert caplog.text.count("sealing self-test: created the probe") == 1
    assert caplog.text.count("sealing self-test ok") == 2


def test_an_existing_probe_that_does_not_open_stops_the_startup_until_it_is_reset(startup, firestore_client, caplog):
    assert main_module.main() == 0
    # 鍵の版の切り替えなどで DEK が変わったあと(古い DEK で封印された暗号文が残っている)を再現する
    foreign = Sealer(os.urandom(32)).seal(SELFTEST_PATH, "probe", os.urandom(32))
    firestore_client.document(SELFTEST_PATH).update({"probe": foreign})
    startup.served.clear()

    assert main_module.main() == 1

    assert startup.served == {}  # uvicorn は起動しない
    assert SELFTEST_FAILED in caplog.text
    assert "startup failed at the step 'sealing self-test' (SealError)" in caplog.text

    # 手順(DEK を作り直すときに _tee/selftest も消す)のあとは、作り直して起動する
    firestore_client.document(SELFTEST_PATH).delete()
    assert main_module.main() == 0
    assert startup.served


@pytest.mark.parametrize("failing", STEP_ORDER[:-1])
def test_a_failing_step_stops_the_startup_with_a_nonzero_code_and_logs_only_the_step_and_the_error_type(
    startup, monkeypatch, caplog, failing
):
    def explode(*args, **kwargs):
        startup.calls.append(failing)
        raise RuntimeError("SECRET-MESSAGE with a token and a DEK")

    monkeypatch.setattr(main_module, PATCHED_NAME[failing], explode)

    assert main_module.main() == 1

    assert startup.calls == ["mask_ids_in_logs", *STEP_ORDER[: STEP_ORDER.index(failing) + 1]]
    assert startup.served == {}  # uvicorn は起動しない
    assert f"startup failed at the step '{failing}' (RuntimeError)" in caplog.text
    assert "SECRET-MESSAGE" not in caplog.text


def test_a_refused_key_release_is_a_nonzero_exit(startup, monkeypatch):
    def refuse(db, *, metadata, config):
        raise KeyReleaseError("STS token exchange was refused (403)")

    monkeypatch.setattr(main_module, "release_dek", refuse)

    assert main_module.main() == 1
    assert startup.served == {}


def test_a_dek_wrapped_by_a_non_primary_version_stops_the_startup_and_the_message_says_not_to_reset_the_dek(
    startup, monkeypatch, firestore_client, config, sleeps, caplog
):
    # 本物の release_dek と偽の KMS で、起動口を通す。v1 で作った DEK が保管されたあと、運営者が v2 を primary にした。
    release_dek(firestore_client, metadata=METADATA, config=config, transport=FakeGoogle().transport, sleep=sleeps.append)
    rotated = FakeGoogle(primary_version=2)
    monkeypatch.setattr(
        main_module, "release_dek", partial(release_dek, transport=rotated.transport, sleep=sleeps.append)
    )

    assert main_module.main() == 1

    assert startup.served == {}  # uvicorn は起動しない
    assert rotated.kms_calls("decrypt") == []
    assert NON_PRIMARY in caplog.text and "startup failed at the step 'key release' (KeyReleaseError)" in caplog.text
    assert "do NOT reset the DEK" in caplog.text  # 実データがあるときは DEK を消さない(批評 C-60)

    # 実データのない試験の間だけ: DEK と自己試験の文書を消して作り直せば(scripts/tee_reset_dek.py)、新しい版(v2)で起動する
    firestore_client.document("_tee/dek").delete()
    firestore_client.document("_tee/selftest").delete()
    assert main_module.main() == 0
    assert stored_dek_document(firestore_client)["kek_version"] == version_name(2)


def test_re_promoting_the_stored_version_lets_the_startup_continue_with_the_same_dek_and_data(
    startup, monkeypatch, firestore_client, sleeps
):
    # 固定文が勧める直し方(DEK を消さずに、保管された版を再び primary に戻す)で、同じ DEK のまま起動できる
    kms = FakeGoogle(primary_version=1)
    monkeypatch.setattr(main_module, "release_dek", partial(release_dek, transport=kms.transport, sleep=sleeps.append))
    assert main_module.main() == 0  # v1 で DEK と自己試験の文書ができる
    dek_document, selftest_document = stored_dek_document(firestore_client), stored_selftest(firestore_client)

    kms.primary_version = 2  # 運営者が v2 を primary にした
    startup.served.clear()
    assert main_module.main() == 1
    assert startup.served == {}

    kms.primary_version = 1  # 保管された版を再び有効にして、primary に戻した
    assert main_module.main() == 0
    assert startup.served  # 起動した
    assert stored_dek_document(firestore_client) == dek_document  # DEK は作り直していない
    assert stored_selftest(firestore_client) == selftest_document  # 既存の暗号文は、同じ DEK で開いた


def test_running_the_module_as_main_exits_nonzero_when_a_step_fails(monkeypatch):
    # `python -m vault.tee.main` と同じ実行(run_name="__main__")。メタデータサーバに届かない環境を再現する。
    # import しただけでは何も起動しない(このテストファイルの先頭で import 済み)ことも、これで確かめている。
    def unreachable(*, transport=None):
        raise MetadataError("could not reach the metadata server")

    monkeypatch.setattr("vault.tee.metadata.read_instance_metadata", unreachable)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)  # root にハンドラを足さない
    monkeypatch.delitem(sys.modules, "vault.tee.main")  # runpy の「すでに読み込み済み」の警告を避ける

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("vault.tee.main", run_name="__main__")

    assert raised.value.code == 1


# --- uvicorn と ID を伏せるログ ---


def test_serve_listens_on_all_interfaces_with_the_tls_files(monkeypatch, config, tmp_path):
    started: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(main_module.uvicorn, "run", lambda *args, **kwargs: started.append((args, kwargs)))
    app = object()

    main_module.serve(app, config, tmp_path / "key.pem", tmp_path / "cert.pem")

    assert started == [
        (
            (app,),
            {
                "host": "0.0.0.0",
                "port": 8443,
                "ssl_keyfile": str(tmp_path / "key.pem"),
                "ssl_certfile": str(tmp_path / "cert.pem"),
            },
        )
    ]


def test_the_id_mask_is_applied_at_startup_and_survives_uvicorns_own_log_configuration(monkeypatch):
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)  # root にハンドラを足さない
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).level, logging.getLogger(name).propagate)
        for name in names
    }
    try:
        main_module.configure_logging()
        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)  # uvicorn.run が起動時に行う設定
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("10.20.0.5:4242", "GET", "/v1/principals/0123456789abcdef/policy", "1.1", 200),
            None,
        )

        assert logging.getLogger("uvicorn.access").filter(record)
        assert "0123456789abcdef" not in record.getMessage() and "/v1/principals/<id>/policy" in record.getMessage()
    finally:
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:], logger.level, logger.propagate = handlers, level, propagate
