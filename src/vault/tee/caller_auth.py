"""呼び出し元(web)の Google ID トークンの検証(research/tee-spike-contract.md §9。design.md §9 の 4)。

Confidential Space は Compute Engine の VM なので、Cloud Run の起動元 IAM は効かない。代わりに、金庫が自分で、
`Authorization: Bearer <ID トークン>` を検証する(web の IdTokenAuth が、caller_audience を audience にしたトークンを付ける)。
`/v1/attestation` 以外のすべての経路に、FastAPI の依存として掛ける(vault.app.create_app の caller_verifier)。

確かめること(`google.auth.jwt.decode` が署名・exp・iat・aud を、このモジュールが残りを確かめる):
- 401 `{"detail": "unauthenticated"}`: ヘッダなし・Bearer でない・形式不正・署名不正(RS256 以外の alg も)・期限切れ・
  aud 違い・iss が Google でない・kid が証明書の表にない。
- 403 `{"detail": "forbidden"}`: 署名は正しいが、email が許可されたサービスアカウントでない(email_verified が真でないときも)。
- 503 `{"detail": "caller verification unavailable"}`: Google の証明書を取れず、使える表もない(検証のしようがない。fail closed)。
  web の金庫のクライアントは 503 を「一時的に応えない」として待つ(401・403 のように、トークンを取り直して送り直さない)。

Google の証明書(`caller_certs_url`。kid → PEM)は 1 時間キャッシュする。未知の kid のときと、期限が切れたときに取り直す(取り直しの
試みは、成否にかかわらず 1 分に 1 回まで)。取り直しに失敗しても、キャッシュが有効なうちは動く。取得は httpx(同期。金庫のエンドポイントは
スレッドプールで動くので、止まるのは取り直しを待つ要求だけ)。起動時に 1 回取り(CallerCerts.refresh)、以後は要求の中で取り直す。

トークンの値は、レスポンスにもログにも書かない。google-auth の例外の文にはトークンの一部が入る(例: 区切りの数が違うとトークン全体)ので、
ログには例外の型名だけを書く。
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Annotated

import httpx
from fastapi import Header, HTTPException
from google.auth import jwt

logger = logging.getLogger(__name__)

ALLOWED_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
CERTS_TTL_SECONDS = 3600.0
MIN_REFETCH_INTERVAL_SECONDS = 60.0
# exp・iat の許容のずれ(秒)。金庫の VM と Cloud Run の時計がわずかにずれても、取りたてのトークンを断らないように。
CLOCK_SKEW_SECONDS = 10
_CERTS_TIMEOUT_SECONDS = 5.0


class CertsUnavailableError(Exception):
    """Google の証明書を取れず、使える表もない。"""


class CallerCerts:
    """Google の署名用証明書(kid → PEM)のキャッシュ。スレッドから呼んでよい。"""

    def __init__(
        self,
        url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.monotonic,
        ttl_seconds: float = CERTS_TTL_SECONDS,
        min_refetch_interval_seconds: float = MIN_REFETCH_INTERVAL_SECONDS,
    ) -> None:
        self._url = url
        self._transport = transport
        self._now = now
        self._ttl = ttl_seconds
        self._min_interval = min_refetch_interval_seconds
        self._certs: dict[str, str] | None = None
        self._fetched_at = 0.0
        self._last_attempt_at: float | None = None
        self._lock = threading.Lock()

    def refresh(self) -> bool:
        """今すぐ取り直す(起動時用。間隔の制限は受けない)。取れたら True。失敗の理由はログに書く。"""
        with self._lock:
            return self._attempt(self._now())

    def for_key(self, kid: str) -> Mapping[str, str]:
        """検証に使う証明書の表。表が古い・kid が表にないときは、(間隔の制限の範囲で)取り直す。

        使える(期限内の)表がなければ CertsUnavailableError。kid が表にないままのこともある(呼び出し側が、署名不正として扱う)。
        """
        with self._lock:
            now = self._now()
            needs_fetch = not self._is_fresh(now) or kid not in (self._certs or {})
            if needs_fetch and (self._last_attempt_at is None or now - self._last_attempt_at >= self._min_interval):
                self._attempt(now)
            if not self._is_fresh(now):
                raise CertsUnavailableError
            return self._certs  # 期限内(= None ではない)

    def _is_fresh(self, now: float) -> bool:
        return self._certs is not None and now - self._fetched_at < self._ttl

    def _attempt(self, now: float) -> bool:
        self._last_attempt_at = now
        try:
            with httpx.Client(transport=self._transport, timeout=_CERTS_TIMEOUT_SECONDS, trust_env=False) as http:
                response = http.get(self._url)
            if response.status_code != 200:
                logger.warning("caller certs: the fetch returned %d", response.status_code)
                return False
            certs = response.json()
        except (httpx.HTTPError, ValueError) as exc:  # JSON でない本文は ValueError
            logger.warning("caller certs: the fetch failed (%s)", type(exc).__name__)
            return False
        well_formed = isinstance(certs, dict) and certs and all(isinstance(v, str) for v in certs.values())
        if not well_formed:
            logger.warning("caller certs: the response is not a table of key IDs and certificates")
            return False
        self._certs, self._fetched_at = certs, now
        return True


def _unauthenticated(reason: str) -> HTTPException:
    """401。reason は固定の短い語(ログ用)。レスポンスの detail は固定文で、理由もトークンも載せない。"""
    logger.warning("caller rejected: %s", reason)
    return HTTPException(status_code=401, detail="unauthenticated", headers={"WWW-Authenticate": "Bearer"})


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


class CallerVerifier:
    """FastAPI の依存: 呼び出し元の Google ID トークンを検証する。通れば何も返さない。通らなければ HTTPException。"""

    def __init__(self, *, audience: str, allowed_email: str, certs: CallerCerts) -> None:
        self._audience = audience
        self._allowed_email = allowed_email
        self._certs = certs

    def __call__(self, authorization: Annotated[str | None, Header()] = None) -> None:
        token = _bearer_token(authorization)
        if token is None:
            raise _unauthenticated("no bearer token")
        claims = self._decode(token)
        if claims.get("iss") not in ALLOWED_ISSUERS:
            raise _unauthenticated("issuer is not Google")
        email, verified = claims.get("email"), claims.get("email_verified")
        if email != self._allowed_email or verified is not True:
            # 署名は Google のもので正しいので、claim の値はログに書いてよい(切り分けのため。長さは抑える)。
            logger.warning("caller forbidden: email=%.100r email_verified=%.20r", email, verified)
            raise HTTPException(status_code=403, detail="forbidden")

    def _decode(self, token: str) -> Mapping:
        """署名・exp・iat・aud を確かめて claim を返す。通らなければ 401(証明書の表がなければ 503)。"""
        try:
            header = jwt.decode_header(token)
        except Exception as exc:
            raise _unauthenticated(f"the JWT header could not be read ({type(exc).__name__})") from None
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            raise _unauthenticated("the JWT header is not RS256 with a key ID")
        try:
            certs = self._certs.for_key(kid)
        except CertsUnavailableError:
            logger.error("caller verification is unavailable: no usable Google certificates")
            raise HTTPException(status_code=503, detail="caller verification unavailable") from None
        try:
            return jwt.decode(token, certs=certs, audience=self._audience, clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
        except Exception as exc:
            # google-auth の例外(MalformedError・InvalidValue は ValueError)の文には、トークンの一部が入る。型名だけを書く。
            # 想定外の例外も、500 にして文をログに流さず、401 にする(認証の関門は、迷ったら通さない)。
            raise _unauthenticated(f"the JWT was not accepted ({type(exc).__name__})") from None
