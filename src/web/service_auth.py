"""サービス間の認証(design.md §1.1・台帳 X-37)。web が金庫・agents を呼ぶときに、Google の ID トークンを付ける。

金庫と agents は、Cloud Run の IAM で認証を必須にする(呼べるのは web のサービスアカウントだけ。§1.1)。IAM は、
`Authorization: Bearer <ID トークン>` のない呼び出しを、アプリに届く前に拒否する。そのため、web の金庫のクライアントと
agents のクライアント(agents.client.send_turn)は、呼び先のサービスの URL を audience にした ID トークンを付ける。
受け取る側(金庫・agents)のアプリの中では、トークンの検証をしない(IAM に任せる)。

- トークンは、Cloud Run のメタデータサーバから、httpx で取る(新しい依存は要らない)。
  `GET http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=<audience>`
  で、ヘッダ `Metadata-Flavor: Google` が必須。
- トークンは audience ごとに持ち、期限(JWT の exp)の少し前(既定 5 分前)まで使い回す。署名は確かめない(受け取る側が確かめる)。
- キャッシュする前に、トークンの未署名の claim(本文を base64url で読む)を確かめる(台帳 X-42): `aud` が要求した audience と
  完全に一致すること、`exp` が有限で未来であること。合わなければキャッシュせず、取れなかったもの(ServiceAuthError)として扱う。
  メタデータサーバが宛先違いのトークンを返しても、そのトークンを送らず、長く使い回さない(JWT の検証ライブラリは使わない)。
- 呼び先が 401・403 で断ったら、その audience のキャッシュを捨て、トークンを取り直して、1 回だけ送り直す(IdTokenAuth。台帳 X-42)。
  2 回目も断られたら、そのまま返す(送り直しを繰り返さない)。
- 設定で入り切りできる: 環境変数 SERVICE_AUTH_ENABLED(true・false)。ローカルとテストでは false にして、メタデータ
  サーバを呼ばない(Authorization ヘッダも付かない)。本番の起動口(web.app.create_app_from_env)は、未設定なら true。
- 使い方: 金庫は、`httpx.AsyncClient(base_url=..., auth=IdTokenAuth(provider, 金庫の URL))`。agents は、
  `send_turn(..., auth=IdTokenAuth(provider, agents の URL))`(web.app.bind_agents_client が束ねる)。audience は
  呼び先の URL から作るので、呼び先ごとに正しい値になる。

トークンの値は、ログにもエラーの文にも書かない。
"""

import base64
import json
import math
import os
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

SERVICE_AUTH_ENV = "SERVICE_AUTH_ENABLED"

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
_METADATA_HEADERS = {"Metadata-Flavor": "Google"}
_METADATA_TIMEOUT_SECONDS = 5.0
_TURN_OFF_HINT = f"set {SERVICE_AUTH_ENV}=false when running outside Cloud Run"
# 期限(exp)のこの秒数前になったら、新しいトークンを取り直す(メタデータサーバのトークンの寿命は 1 時間)。
REFRESH_MARGIN_SECONDS = 300.0
# 呼び先(Cloud Run の IAM)がトークンを断ったときの HTTP ステータス。キャッシュを捨てて、取り直して、1 回だけ送り直す。
_REJECTED_STATUS_CODES = frozenset({401, 403})


class ServiceAuthError(httpx.TransportError):
    """ID トークンを取れなかった(メタデータサーバに届かない・応答が ID トークンでない)。

    httpx の通信エラー(TransportError)の一種にしてある。金庫のクライアントは通信エラーを「一時的に応えない」
    (VaultUnavailableError)に、agents のクライアントは ConnectionError に写すので、レフェリーは待って(または
    再試行して)やり直す。メッセージに、トークンの値は入れない。
    """


def audience_for(service_url: str) -> str:
    """呼び先のサービスの URL から、ID トークンの audience(スキーム・ホスト・ポートだけ。パスは含めない)を作る。

    Cloud Run の audience は、サービスの URL(例: `https://vault-xxxx-an.a.run.app`)。末尾のスラッシュやパスは付けない。
    """
    parts = urlsplit(service_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError("a service URL must have a scheme and a host (for example https://vault.example.run.app)")
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def _claims_of(token: str) -> dict:
    """ID トークン(JWT)の claim(本文)を読む。読めなければ ServiceAuthError。署名は確かめない(受け取る側が確かめる)。"""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:  # binascii.Error・JSONDecodeError・UnicodeDecodeError は ValueError
        raise ServiceAuthError("the metadata server returned a value that is not an ID token (a JWT)") from exc
    if not isinstance(claims, dict):
        raise ServiceAuthError("the claims of the ID token are not a JSON object")
    return claims


def _checked_expiry(token: str, audience: str, now: float) -> float:
    """トークンが audience 宛てで、期限が有限かつ未来なら、期限(exp。UNIX 秒)を返す。そうでなければ ServiceAuthError。

    キャッシュする前に呼ぶ(台帳 X-42): 宛先違いのトークンや、期限の壊れたトークンを、キャッシュして使い回さないため。
    `aud` は、文字列として audience と完全に一致しなければならない(配列・末尾のスラッシュ違いなども不可)。
    署名は確かめない(受け取る側が確かめる)。トークンの値・claim の値は、エラーの文に入れない。
    """
    claims = _claims_of(token)
    if claims.get("aud") != audience:
        raise ServiceAuthError("the ID token is not for the requested audience")
    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, int | float):
        raise ServiceAuthError("the ID token has an exp claim that is not a number")
    try:
        expires_at = float(expiry)
    except OverflowError:  # float にできないほど大きい整数
        raise ServiceAuthError("the ID token has an exp claim that is not finite") from None
    if not math.isfinite(expires_at):
        raise ServiceAuthError("the ID token has an exp claim that is not finite")
    if expires_at <= now:
        raise ServiceAuthError("the ID token has already expired (its exp claim is not in the future)")
    return expires_at


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float  # UNIX 秒


class IdTokenProvider:
    """Cloud Run のメタデータサーバから、audience ごとの ID トークンを取る。期限の少し前まで使い回す。

    transport(テストは、メタデータサーバのスタブ)と now(UNIX 秒を返す関数)は、差し込める。
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] = time.time,
        refresh_margin_seconds: float = REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._transport = transport
        self._now = now
        self._margin = refresh_margin_seconds
        self._cache: dict[str, _CachedToken] = {}

    async def token(self, audience: str) -> str:
        """audience の ID トークン。期限まで margin 以上あるキャッシュがあればそれを、なければ取り直す。

        取り直したトークンは、キャッシュする前に確かめる(audience 宛てで、期限が有限かつ未来)。合わなければ
        キャッシュせず、ServiceAuthError(台帳 X-42)。
        """
        cached = self._cache.get(audience)
        if cached is not None and cached.expires_at - self._now() > self._margin:
            return cached.token
        token = await self._fetch(audience)
        expires_at = _checked_expiry(token, audience, self._now())
        self._cache[audience] = _CachedToken(token=token, expires_at=expires_at)
        return token

    def invalidate(self, audience: str, token: str | None = None) -> None:
        """audience のキャッシュを捨てる(呼び先がトークンを断ったとき。次の token() は、取り直す。台帳 X-42)。

        token を渡したときは、キャッシュがそのトークンのときだけ捨てる。並行の要求が取り直した新しいトークンを、
        遅れて届いた古いトークンの「断られた」で消さないため(台帳 X-44)。
        """
        cached = self._cache.get(audience)
        if cached is not None and (token is None or cached.token == token):
            del self._cache[audience]

    async def _fetch(self, audience: str) -> str:
        try:
            # trust_env=False: 環境変数のプロキシ設定が、メタデータサーバへの呼び出しを横取りしないように。
            async with httpx.AsyncClient(
                transport=self._transport, timeout=httpx.Timeout(_METADATA_TIMEOUT_SECONDS), trust_env=False
            ) as http:
                response = await http.get(
                    METADATA_IDENTITY_URL, params={"audience": audience}, headers=_METADATA_HEADERS
                )
        except httpx.HTTPError as exc:
            raise ServiceAuthError(
                f"could not get an ID token from the metadata server ({type(exc).__name__}); {_TURN_OFF_HINT}"
            ) from exc
        if response.status_code != 200:
            raise ServiceAuthError(
                f"the metadata server returned {response.status_code} for an ID token; {_TURN_OFF_HINT}"
            )
        return response.text.strip()


class IdTokenAuth(httpx.Auth):
    """httpx の認証: リクエストのたびに、呼び先の audience の ID トークンを `Authorization: Bearer` で付ける。

    service_url は、呼び先のサービスの URL(audience はこの URL から作る。audience_for)。AsyncClient 用
    (async_auth_flow だけを持つ)。トークンを取れなければ ServiceAuthError(通信エラーとして伝わる)。

    呼び先が 401・403 で断ったら(Cloud Run の IAM は、アプリに届く前に断る)、その audience のキャッシュを捨て、
    トークンを取り直して、同じリクエストを 1 回だけ送り直す(台帳 X-42)。2 回目も断られたら、そのトークンもキャッシュ
    から捨て(次の呼び出しで、断られたトークンを送らないように。台帳 X-44)、その応答をそのまま返す。
    送り直しは、1 つのリクエストにつき 1 回までで、繰り返さない。
    """

    def __init__(self, provider: IdTokenProvider, service_url: str) -> None:
        self._provider = provider
        self.audience = audience_for(service_url)

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        await request.aread()  # 送り直しのために、本文を読み込んでおく(ストリームの本文は、1 回しか送れない)
        first_token = await self._provider.token(self.audience)
        request.headers["Authorization"] = f"Bearer {first_token}"
        response = yield request
        if response.status_code not in _REJECTED_STATUS_CODES:
            return
        self._provider.invalidate(self.audience, first_token)
        second_token = await self._provider.token(self.audience)
        request.headers["Authorization"] = f"Bearer {second_token}"
        response = yield request
        if response.status_code in _REJECTED_STATUS_CODES:
            self._provider.invalidate(self.audience, second_token)  # 3 回目は送らない


def id_token_provider_from_env(environ: Mapping[str, str] | None = None) -> IdTokenProvider | None:
    """環境変数 SERVICE_AUTH_ENABLED から、サービス間の認証を使うかを決める。使うなら IdTokenProvider、切るなら None。

    未設定なら使う(本番の起動口の既定。切り忘れて、認証なしで呼ぶことがないように)。ローカルでは false にする。
    値は true・false(大文字小文字・前後の空白は問わない)だけ。ほかの値は、起動を拒否する(打ち間違いで、意図せず
    切れたり入ったりしないように)。
    """
    source = os.environ if environ is None else environ
    value = source.get(SERVICE_AUTH_ENV, "true").strip().lower()
    if value == "true":
        return IdTokenProvider()
    if value == "false":
        return None
    raise ValueError(f"the environment variable {SERVICE_AUTH_ENV} must be true or false")
