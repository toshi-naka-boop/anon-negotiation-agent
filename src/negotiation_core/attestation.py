"""Confidential Space の attestation トークン(OIDC の JWT)の検証(design.md §9・§12.1 AC-23、research/tee-spike-contract.md §10)。

web(検証してからピン留めする transport。web.attested_transport)と、検証スクリプト(scripts/verify_attestation.py)が共有する。
金庫のコードは、これを使わない(検証する側の部品)。

- verify_attestation_token: 契約 §10 の順(malformed → signature → expired → audience → issuer → nonce → certificate →
  swname → debug → support_attributes → hwmodel → image_digest → override → project → service_account → zone → instance)に
  確かめ、最初に外れた条件の理由を AttestationError.reason に入れる。通れば VerifiedAttestation。
  トークンの値は、例外の文にもログにも書かない(AttestationError.token は表示用の属性で、文には入らない)。
- SignerCerts: トークンの署名鍵(kid → PEM)の取得とキャッシュ。取得は httpx(同期)。
- load_releases・active_digests・release_for_digest: deploy/vault-releases.json の digest → コミットの表(L0。research/tee-spike.md 6-3)。
  各要素の status は active(通す)か revoked(失効。通さない)。

署名は google.auth.crypt.verify_signature で確かめる(google.auth.jwt.decode が内部で使うのと同じ部品)。jwt.decode は、
exp・iat を実時計で見るため時計を差し込めず、exp・iat の欠落を署名不正と区別できないので、使わない。
"""

import base64
import hashlib
import json
import math
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from google.auth import crypt

# 検証の順(契約 §10 の表)。AttestationError.reason は、このどれか。
VERIFICATION_REASONS = (
    "malformed",
    "signature",
    "expired",
    "audience",
    "issuer",
    "nonce",
    "certificate",
    "swname",
    "debug",
    "support_attributes",
    "hwmodel",
    "image_digest",
    "override",
    "project",
    "service_account",
    "zone",
    "instance",
)
# 表の外の理由: 検証に至らなかった(金庫の応答にトークンがない・署名鍵を取れない)。ネットワークの失敗そのものは含めない。
UNAVAILABLE = "unavailable"

CLOCK_SKEW_SECONDS = 60  # exp・iat の許容のずれ(契約 §10 の 3)
_MAX_TOKEN_CHARS = 65536  # 実際のトークンは数 KB。検証前の(信用していない相手の)入力の大きさを抑える
_BASE64URL = re.compile(r"[A-Za-z0-9_-]*")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_PRODUCTION_DBGSTAT = "disabled-since-boot"
RELEASE_STATUSES = ("active", "revoked")


@dataclass(frozen=True)
class AttestationPolicy:
    """トークンに求める条件(契約 §10)。project_id・service_account が None のものは、照合しない。"""

    audience: str
    issuer: str
    allowed_hwmodels: frozenset[str]
    allowed_digests: frozenset[str]
    project_id: str | None
    service_account: str | None
    zone: str | None = None
    instance_name: str | None = None
    require_production: bool = True  # dbgstat == disabled-since-boot と STABLE を要求する(--allow-debug で False)


class AttestationError(ValueError):
    """検証できなかった。reason は VERIFICATION_REASONS のどれか(検証に至らなかったときは UNAVAILABLE)。

    token は、検証しようとしたトークン(取れていれば。画面・スクリプトが、取れた範囲の claims を見せるために使う)。
    例外の文には reason しか入れない。
    """

    def __init__(self, reason: str, *, token: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.token = token


@dataclass(frozen=True)
class VerifiedAttestation:
    """検証を通ったトークンの要点。claims は全文(表示・記録用)。"""

    image_digest: str
    hwmodel: str
    swname: str
    swversion: tuple[str, ...]
    dbgstat: str | None
    support_attributes: tuple[str, ...]
    project_id: str | None
    zone: str | None
    instance_name: str | None
    service_accounts: tuple[str, ...]
    issued_at: float
    expires_at: float
    nonces: tuple[str, ...]
    claims: dict


# --- トークンの読み取り ---------------------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    if not _BASE64URL.fullmatch(segment):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _parse(token: object) -> tuple[dict, dict, bytes, bytes]:
    """JWT を (ヘッダ, 本文, 署名の対象, 署名) に分ける。形が違えば AttestationError("malformed")。署名は確かめない。"""
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
        raise AttestationError("malformed")
    parts = token.split(".")
    if len(parts) != 3:
        raise AttestationError("malformed")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, RecursionError):  # base64・UTF-8・JSON の誤りはすべて ValueError の一種
        raise AttestationError("malformed") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise AttestationError("malformed")
    return header, payload, f"{parts[0]}.{parts[1]}".encode("ascii"), signature


def decode_claims_unverified(token: str) -> dict:
    """トークンの本文(claims)を、署名を確かめずに読む。表示用で、検証には使わない。形が違えば AttestationError("malformed")。"""
    return _parse(token)[1]


def _section(claims: Mapping, *path: str) -> Mapping:
    """claims の入れ子の辞書(例: submods → container)。なければ(どこかが辞書でなければ)空の辞書。"""
    node: Any = claims
    for key in path:
        node = node.get(key) if isinstance(node, Mapping) else None
    return node if isinstance(node, Mapping) else {}


def _strings(value: object) -> tuple[str, ...]:
    """文字列、または文字列の配列を、文字列の tuple にする(eat_nonce・swversion はどちらの形もある)。それ以外は空。"""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # float にできないほど大きい整数
        return None
    return number if math.isfinite(number) else None


def summarize_claims(claims: Mapping) -> dict[str, Any]:
    """画面・スクリプトに出す claims の要点(契約 §8 の claims の 9 項目)。なければ None(配列の項目は空の配列)。

    署名を確かめていない claims にも使える(表示用)。値は JSON にそのまま出せる形。
    """
    return {
        "image_digest": _text(_section(claims, "submods", "container").get("image_digest")),
        "hwmodel": _text(claims.get("hwmodel")),
        "swname": _text(claims.get("swname")),
        "swversion": list(_strings(claims.get("swversion"))),
        "dbgstat": _text(claims.get("dbgstat")),
        "support_attributes": list(_strings(_section(claims, "submods", "confidential_space").get("support_attributes"))),
        "project_id": _text(_section(claims, "submods", "gce").get("project_id")),
        "zone": _text(_section(claims, "submods", "gce").get("zone")),
        "instance_name": _text(_section(claims, "submods", "gce").get("instance_name")),
    }


# --- 署名鍵 ---------------------------------------------------------------------------------------------------


def _fetch_signer_certs(url: str) -> dict[str, str]:
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    return response.json()


class SignerCerts:
    """署名鍵(kid → 証明書の PEM)の取得とキャッシュ(契約 §10)。取得は httpx(同期。テストは fetch を差し替える)。

    get(): キャッシュを返す。なければ(ttl_seconds をすぎていれば)取り直す。
    refresh(): 未知の kid のとき、キャッシュを取り直す。前回の取得から min_refetch_interval_seconds 以内なら、取らずにキャッシュを返す。
    取り直しに失敗しても、前に取れた鍵があれば、それで続ける(最初の取得に失敗したときだけ、例外を投げる)。
    スレッドの間で共有できる(web は asyncio.to_thread で検証を呼ぶ)。
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float = 3600.0,
        min_refetch_interval_seconds: float = 60.0,
        fetch: Callable[[str], Mapping[str, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._min_interval = min_refetch_interval_seconds
        self._fetch = fetch if fetch is not None else _fetch_signer_certs
        self._clock = clock
        self._lock = threading.Lock()
        self._certs: dict[str, str] | None = None
        self._loaded_at = 0.0
        self._attempted_at: float | None = None

    def get(self) -> dict[str, str]:
        return self._current(force=False)

    def refresh(self) -> dict[str, str]:
        return self._current(force=True)

    def _current(self, *, force: bool) -> dict[str, str]:
        with self._lock:
            now = self._clock()
            expired = self._certs is None or now - self._loaded_at >= self._ttl
            limited = self._attempted_at is not None and now - self._attempted_at < self._min_interval
            if self._certs is None or ((force or expired) and not limited):
                self._attempted_at = now
                try:
                    fetched = dict(self._fetch(self._url))
                    if not fetched or not all(isinstance(k, str) and isinstance(v, str) for k, v in fetched.items()):
                        raise ValueError("the signer certificates are not a non-empty {kid: PEM} object")
                except Exception:
                    if self._certs is None:
                        raise
                else:
                    self._certs, self._loaded_at = fetched, now
            return dict(self._certs)


def _current_certs(certs: "Mapping[str, str] | SignerCerts", kid: object) -> Mapping[str, str]:
    """検証に使う鍵。SignerCerts なら、未知の kid のときに 1 回だけ取り直す(鍵は回転する)。取れなければ UNAVAILABLE。"""
    if not isinstance(certs, SignerCerts):
        return certs
    try:
        current = certs.get()
        if isinstance(kid, str) and kid not in current:
            current = certs.refresh()
    except Exception as exc:
        raise AttestationError(UNAVAILABLE) from exc
    return current


def _signature_is_valid(certs: Mapping[str, str], kid: object, signing_input: bytes, signature: bytes) -> bool:
    if kid is None:  # ヘッダに kid がなければ、鍵のどれかで通ればよい(google.auth.jwt.decode と同じ)
        candidates = list(certs.values())
    elif isinstance(kid, str) and kid in certs:
        candidates = [certs[kid]]
    else:
        return False
    try:
        return crypt.verify_signature(signing_input, signature, candidates)
    except Exception:  # 鍵が読めない・種類が違う、など。確かめられないものは通さない
        return False


# --- 検証 -----------------------------------------------------------------------------------------------------


def verify_attestation_token(
    token: str,
    *,
    certs: "Mapping[str, str] | SignerCerts",
    policy: AttestationPolicy,
    nonce: str,
    certificate_sha256: str | None,
    now: float | None = None,
) -> VerifiedAttestation:
    """attestation トークンを、契約 §10 の順に確かめる。最初に外れた条件の reason で AttestationError。

    certs は kid → PEM(証明書でも公開鍵でもよい)。SignerCerts を渡すと、未知の kid のときに 1 回だけ取り直す。
    certificate_sha256 が None のときは、証明書の結び付き(7)を確かめない(web 経由で確かめるスクリプトの場合)。
    now は UNIX 秒(省略すると現在時刻。テストで期限切れを作るために差し込める)。
    """
    try:
        return _verify(token, certs, policy, nonce, certificate_sha256, now)
    except AttestationError as exc:
        exc.token = token if isinstance(token, str) else None
        raise


def _verify(
    token: str,
    certs: "Mapping[str, str] | SignerCerts",
    policy: AttestationPolicy,
    nonce: str,
    certificate_sha256: str | None,
    now: float | None,
) -> VerifiedAttestation:
    # 1. 形
    header, claims, signing_input, signature = _parse(token)
    # 2. 署名(RS256)
    kid = header.get("kid")
    if header.get("alg") != "RS256" or not _signature_is_valid(_current_certs(certs, kid), kid, signing_input, signature):
        raise AttestationError("signature")
    # 3. 期限
    current = time.time() if now is None else now
    issued_at, expires_at = _finite_number(claims.get("iat")), _finite_number(claims.get("exp"))
    if (
        issued_at is None
        or expires_at is None
        or current < issued_at - CLOCK_SKEW_SECONDS
        or current > expires_at + CLOCK_SKEW_SECONDS
    ):
        raise AttestationError("expired")
    # 4・5. 宛先と発行者
    if claims.get("aud") != policy.audience:
        raise AttestationError("audience")
    if claims.get("iss") != policy.issuer:
        raise AttestationError("issuer")
    # 6・7. nonce と、証明書の結び付き(eat_nonce は、1 個なら文字列、複数なら配列)
    nonces = _strings(claims.get("eat_nonce"))
    if nonce not in nonces:
        raise AttestationError("nonce")
    if certificate_sha256 is not None and certificate_sha256 not in nonces:
        raise AttestationError("certificate")
    # 8〜13. ワークロード
    summary = summarize_claims(claims)
    if summary["swname"] != "CONFIDENTIAL_SPACE":
        raise AttestationError("swname")
    if policy.require_production and summary["dbgstat"] != _PRODUCTION_DBGSTAT:
        raise AttestationError("debug")
    if policy.require_production and "STABLE" not in summary["support_attributes"]:
        raise AttestationError("support_attributes")
    if summary["hwmodel"] not in policy.allowed_hwmodels:
        raise AttestationError("hwmodel")
    if summary["image_digest"] not in policy.allowed_digests:
        raise AttestationError("image_digest")
    container = _section(claims, "submods", "container")
    if container.get("cmd_override") or container.get("env_override"):
        raise AttestationError("override")
    # 14〜16. 場所と実行者
    if policy.project_id is not None and summary["project_id"] != policy.project_id:
        raise AttestationError("project")
    service_accounts = _strings(claims.get("google_service_accounts"))
    if policy.service_account is not None and set(service_accounts) != {policy.service_account}:
        raise AttestationError("service_account")
    if policy.zone is not None and summary["zone"] != policy.zone:
        raise AttestationError("zone")
    if policy.instance_name is not None and summary["instance_name"] != policy.instance_name:
        raise AttestationError("instance")
    return VerifiedAttestation(
        image_digest=summary["image_digest"],
        hwmodel=summary["hwmodel"],
        swname=summary["swname"],
        swversion=tuple(summary["swversion"]),
        dbgstat=summary["dbgstat"],
        support_attributes=tuple(summary["support_attributes"]),
        project_id=summary["project_id"],
        zone=summary["zone"],
        instance_name=summary["instance_name"],
        service_accounts=service_accounts,
        issued_at=issued_at,
        expires_at=expires_at,
        nonces=nonces,
        claims=claims,
    )


# --- 証明書のピン留めの部品(web の transport と、検証スクリプトが共有する) ---------------------------------------------


def certificate_sha256_of_pem(pem: str) -> str:
    """証明書(PEM)の DER の SHA-256(小文字の 16 進 64 文字。契約 §2 の certificate_sha256)。"""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def pinned_ssl_context(pem: str) -> ssl.SSLContext:
    """この 1 枚の証明書だけを信用する、クライアントの SSLContext(自己署名の金庫の証明書をピン留めする。契約 §8 の 2)。

    ホスト名は見ない(金庫は VPC 内の IP アドレスで呼ぶ)。その代わり、証明書の中身はトークンの nonce で結び付けて確かめる。
    """
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


# --- digest → コミットの表(deploy/vault-releases.json) -------------------------------------------------------------


def load_releases(path: str | Path) -> list[dict]:
    """deploy/vault-releases.json(`{"releases": [{"digest", "commit", "built_at", "status"}]}`)を読む。

    status は active か revoked(なければ active とみなして、返す要素に入れる)。ファイルがなければ FileNotFoundError、形が違えば ValueError。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON") from exc
    releases = data.get("releases") if isinstance(data, dict) else None
    if not isinstance(releases, list):
        raise ValueError(f'{path} must be a JSON object with a "releases" list')
    for entry in releases:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("digest"), str)
            and _SHA256_DIGEST.fullmatch(entry["digest"])
            and isinstance(entry.get("commit"), str)
            and _COMMIT.fullmatch(entry["commit"])
            and entry.get("status", "active") in RELEASE_STATUSES
        ):
            raise ValueError(
                f"{path} has a release that is not {{digest: sha256:<64 hex>, commit: <commit sha>, built_at, status: active|revoked}}"
            )
    return [{**entry, "status": entry.get("status", "active")} for entry in releases]


def active_digests(releases: list[dict]) -> frozenset[str]:
    """検証で通す digest(status が active のものだけ。revoked は通さない)。"""
    return frozenset(release["digest"] for release in releases if release.get("status", "active") == "active")


def release_for_digest(releases: list[dict], digest: str) -> dict | None:
    """表から digest のリリースを引く。なければ None。revoked の要素も返す(失効したことを表示するため。検証では通さない)。"""
    return next((release for release in releases if release.get("digest") == digest), None)
