"""鍵の解放(research/tee-spike-contract.md §5。research/tee-spike.md の 2)。

launcher が約 1 時間ごとに書き直す既定の attestation トークン(ファイル)を、STS で連合アクセストークンに交換し、Cloud KMS の
KEK で DEK(データ暗号鍵)を包む・解く。KMS の権限は、Workload Identity Pool の principalSet(イメージの digest)にだけ付いているので、
検証済みのイメージのワークロードだけが DEK を得られる。google-auth の identity_pool も requests も使わず、httpx で REST を直接呼ぶ。

DEK は `vault-db` の文書 `_tee/dek` に、KMS で包んだ形(wrapped_dek)で保管する。初回は 32 バイトの乱数を VM の中で作り、
包んで `create()` で書く(すでにあれば、書かずに読み直して解く)。DEK はメモリにだけ置く。
文書の項目: wrapped_dek・kek(鍵の名前)・kek_version(包んだ鍵の版。KMS の `:encrypt` の応答の `name` をそのまま)・created_at・
image_digest(DEK を作ったイメージの digest。既定トークンから署名を確かめずに読んだ記録用の値)。

鍵の版の確認(批評 C-56): debug イメージの間に運営者が写した `_tee/dek` を、本番に切り替えた後に書き戻す手口を止めるため、
DEK を解く前に `GET <鍵の名前>` で鍵の `primary.name` を取り、`_tee/dek.kek_version` と完全に一致しなければ、解かずに失敗する
(運営者が新しい版を primary にし、古い版を無効化していれば、写し取った古い版の DEK は解けない)。`kek_version` のない古い文書も拒否する。
拒否したときは、固定文と 2 つの版の名前をログに書く(DEK も暗号文も書かない)。直すには DEK を作り直す(`_tee/dek` と、古い DEK で
封印した `_tee/selftest` を消す。scripts/tee_reset_dek.py)。

失敗の扱い: STS・KMS の 4xx は「条件に合わない」として、ステータスだけをログに書いて失敗(やり直さない)。5xx・通信エラーは、
2 秒・4 秒・8 秒の間隔で最大 3 回やり直してから失敗する。失敗は KeyReleaseError(起動口は非 0 で終了し、再起動ポリシーが再起動する)。

既定トークンの中身・アクセストークン・DEK・KMS の応答は、ログにも例外の文にも書かない。
"""

import base64
import json
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from vault.config import VaultTeeConfig
from vault.tee.metadata import InstanceMetadata
from vault.tee.sealing import DEK_BYTES

logger = logging.getLogger(__name__)

STS_URL = "https://sts.googleapis.com/v1/token"
KMS_BASE_URL = "https://cloudkms.googleapis.com/v1/"
TEE_COLLECTION = "_tee"
DEK_DOCUMENT = "dek"
RETRY_DELAYS_SECONDS = (2.0, 4.0, 8.0)  # 5xx・通信エラーのやり直しの前に待つ秒数(最大 3 回)
NON_PRIMARY_MESSAGE = "DEK was wrapped by a non-primary key version; rotate the DEK"
_TIMEOUT_SECONDS = 10.0


class KeyReleaseError(Exception):
    """鍵を解放できなかった。メッセージは固定の語とステータス・例外の型名だけで、トークンも鍵も入らない。"""


def kms_key_name(metadata: InstanceMetadata, config: VaultTeeConfig) -> str:
    return (
        f"projects/{metadata.project_id}/locations/{metadata.region}"
        f"/keyRings/{config.kms_key_ring}/cryptoKeys/{config.kms_key}"
    )


def sts_audience(metadata: InstanceMetadata, config: VaultTeeConfig) -> str:
    return (
        f"//iam.googleapis.com/projects/{metadata.project_number}/locations/global"
        f"/workloadIdentityPools/{config.workload_identity_pool}/providers/{config.workload_identity_provider}"
    )


class _Rest:
    """STS・KMS への JSON の呼び出し。4xx はやり直さず、5xx・通信エラーはやり直す(契約 §5 の 5)。"""

    def __init__(self, http: httpx.Client, sleep: Callable[[float], None]) -> None:
        self._http = http
        self._sleep = sleep

    def get(self, step: str, url: str, *, access_token: str) -> dict:
        return self._send("GET", step, url, None, access_token)

    def post(self, step: str, url: str, body: dict, *, access_token: str | None = None) -> dict:
        return self._send("POST", step, url, body, access_token)

    def _send(self, method: str, step: str, url: str, body: dict | None, access_token: str | None) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                response = self._http.request(method, url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
            else:
                if response.status_code == 200:
                    return _json_object(step, response)
                if response.status_code < 500:
                    logger.error("key release: %s was refused (status %d)", step, response.status_code)
                    raise KeyReleaseError(f"{step} was refused ({response.status_code})")
                reason = f"status {response.status_code}"
            if attempt == len(RETRY_DELAYS_SECONDS):
                break
            logger.warning("key release: %s failed (%s); retrying in %.0f s", step, reason, RETRY_DELAYS_SECONDS[attempt])
            self._sleep(RETRY_DELAYS_SECONDS[attempt])
        logger.error("key release: %s failed after %d retries (%s)", step, len(RETRY_DELAYS_SECONDS), reason)
        raise KeyReleaseError(f"{step} failed after retries")


def _json_object(step: str, response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise KeyReleaseError(f"{step} returned a body that is not a JSON object")
    return data


def _read_claims_token(path: str) -> str:
    try:
        token = Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyReleaseError(f"could not read the claims token file ({type(exc).__name__})") from exc
    if not token:
        raise KeyReleaseError("the claims token file is empty")
    return token


def _image_digest(claims_token: str) -> str | None:
    """既定トークンの submods.container.image_digest を、署名を確かめずに読む(DEK を作ったイメージの記録用)。読めなければ None。"""
    try:
        payload = claims_token.split(".")[1]
        digest = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["submods"]["container"]["image_digest"]
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return digest if isinstance(digest, str) else None


def _exchange(rest: _Rest, claims_token: str, audience: str) -> str:
    """STS で、既定の attestation トークンを連合アクセストークンに交換する。"""
    data = rest.post(
        "STS token exchange",
        STS_URL,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": audience,
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "subject_token": claims_token,
        },
    )
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise KeyReleaseError("STS token exchange returned no access token")
    return access_token


def _bytes_field(data: dict, field: str, step: str) -> bytes:
    """KMS の応答の base64 の項目を bytes にする。無い・base64 でない・空は KeyReleaseError。"""
    try:
        value = base64.b64decode(data[field], validate=True)
    except (KeyError, TypeError, ValueError):  # binascii.Error は ValueError
        raise KeyReleaseError(f"{step} returned no {field}") from None
    if not value:
        raise KeyReleaseError(f"{step} returned an empty {field}")
    return value


def _encrypt(rest: _Rest, access_token: str, key_name: str, dek: bytes) -> tuple[bytes, str]:
    """KMS の encrypt。(暗号文, 使った鍵の版の名前)を返す。版の名前は、応答の `name` をそのまま使う。"""
    data = rest.post(
        "KMS encrypt",
        f"{KMS_BASE_URL}{key_name}:encrypt",
        {"plaintext": base64.b64encode(dek).decode("ascii")},
        access_token=access_token,
    )
    ciphertext = _bytes_field(data, "ciphertext", "KMS encrypt")
    version = data.get("name")
    if not isinstance(version, str) or not version:
        raise KeyReleaseError("KMS encrypt returned no key version name")
    return ciphertext, version


def _decrypt(rest: _Rest, access_token: str, key_name: str, wrapped_dek: bytes) -> bytes:
    data = rest.post(
        "KMS decrypt",
        f"{KMS_BASE_URL}{key_name}:decrypt",
        {"ciphertext": base64.b64encode(wrapped_dek).decode("ascii")},
        access_token=access_token,
    )
    return _bytes_field(data, "plaintext", "KMS decrypt")


def _primary_version(rest: _Rest, access_token: str, key_name: str) -> str:
    """KMS の鍵の `primary.name`(いま encrypt に使われる版の名前)。"""
    data = rest.get("KMS get key", f"{KMS_BASE_URL}{key_name}", access_token=access_token)
    primary = data.get("primary")
    name = primary.get("name") if isinstance(primary, dict) else None
    if not isinstance(name, str) or not name:
        raise KeyReleaseError("KMS get key returned no primary version")
    return name


def _unwrap(rest: _Rest, access_token: str, key_name: str, snapshot) -> bytes:
    """保管されている DEK を解く。包んだ鍵の版が primary でなければ(kek_version がなくても)、解かずに失敗する(批評 C-56)。"""
    stored = snapshot.to_dict() or {}  # 文書がなければ to_dict() は None
    wrapped = stored.get("wrapped_dek")
    if not isinstance(wrapped, bytes):
        raise KeyReleaseError("the stored wrapped DEK is missing or malformed")
    stored_version, primary_version = stored.get("kek_version"), _primary_version(rest, access_token, key_name)
    if stored_version != primary_version:
        # 保管された値は運営者が書き換えられる。ログの行を偽造されないよう、repr で書いて、長さを抑える。
        logger.error("%s (stored: %.200r, primary: %.200r)", NON_PRIMARY_MESSAGE, stored_version, primary_version)
        raise KeyReleaseError(NON_PRIMARY_MESSAGE)
    dek = _decrypt(rest, access_token, key_name, wrapped)
    if len(dek) != DEK_BYTES:
        raise KeyReleaseError(f"the unwrapped DEK is not {DEK_BYTES} bytes")
    return dek


def release_dek(
    db: firestore.Client,
    *,
    metadata: InstanceMetadata,
    config: VaultTeeConfig,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """DEK(32 バイト)を返す。`_tee/dek` がなければ作って KMS で包んで書き、あれば KMS で解く。失敗は KeyReleaseError。"""
    claims_token = _read_claims_token(config.claims_token_file)
    key_name = kms_key_name(metadata, config)
    ref = db.collection(TEE_COLLECTION).document(DEK_DOCUMENT)
    # trust_env=False: 環境変数のプロキシ設定が、トークンを載せた通信を横取りしないように。
    with httpx.Client(transport=transport, timeout=_TIMEOUT_SECONDS, trust_env=False) as http:
        rest = _Rest(http, sleep)
        access_token = _exchange(rest, claims_token, sts_audience(metadata, config))
        logger.info("key release: exchanged the claims token for an access token")
        snapshot = ref.get()
        if snapshot.exists:
            dek = _unwrap(rest, access_token, key_name, snapshot)
            logger.info("key release: unwrapped the stored DEK")
            return dek
        dek = secrets.token_bytes(DEK_BYTES)
        wrapped, kek_version = _encrypt(rest, access_token, key_name, dek)
        try:
            ref.create(
                {
                    "wrapped_dek": wrapped,
                    "kek": key_name,
                    "kek_version": kek_version,
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "image_digest": _image_digest(claims_token),
                }
            )
        except AlreadyExists:
            # 別のインスタンスが先に作った。こちらの DEK は捨てて、保管されている方を解く。
            dek = _unwrap(rest, access_token, key_name, ref.get())
            logger.info("key release: another instance created the DEK first; unwrapped the stored DEK")
            return dek
        logger.info("key release: created a new DEK and stored it wrapped")
        return dek
