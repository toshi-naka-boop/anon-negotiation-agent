"""依頼者のセッションクッキー(design.md §6.3)。

本物の利用者はアカウントを作らない匿名の依頼者で、依頼者 ID は署名付きクッキーで持つ。
- 署名は itsdangerous(HMAC-SHA256)。署名の対象に、依頼者 ID と期限(exp。UNIX 秒)を入れる。
  サーバ側でも期限切れを受け付けない(ブラウザがクッキーを捨てる前提には頼らない)。
- クッキーは HttpOnly・Secure・SameSite=Lax、寿命(Max-Age)は暫定 29 日(config/params.toml)。
- 署名の鍵は環境変数から読む。コードに鍵を書かず、既定値も持たない(鍵がなければ起動を拒否する)。
- 鍵は、base64url として読めて、デコードした長さが 32 バイト以上でなければならない(台帳 X-39)。弱い鍵では、
  /start で得た自分のクッキーから鍵を総当たりして、他人のクッキーを偽造できる。満たさなければ起動を拒否する。
  鍵の作り方: `python -c "import secrets; print(secrets.token_urlsafe(32))"`(32 バイトの乱数を base64url にした 43 文字)
- 時刻は注入できる(呼び出し側が now を渡す)ので、テストは sleep せずに期限を確かめられる。

ID を発行するのは、開始ページの GET だけ(web.api の start_page)。この module は、発行の条件は
決めず、署名・検証・クッキーの形だけを持つ。
"""

import base64
import binascii
import datetime as dt
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from itsdangerous import BadData, URLSafeSerializer
from starlette.responses import Response

from negotiation_core import ID_PATTERN

SESSION_COOKIE_NAME = "principal_session"
SESSION_KEY_ENV = "SESSION_SIGNING_KEY"

# 署名の鍵の最小の長さ(base64url をデコードした後のバイト数。HMAC-SHA256 の出力と同じ 256 ビット)。
MIN_SESSION_KEY_BYTES = 32
# 鍵の作り方(エラーの文に載せる)。
_KEY_RECIPE = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}")

# 署名の用途を分けるための固定の文字列(鍵ではない)。別の用途に同じ鍵を使っても、署名が流用されない。
_SALT = "tenshokuagent.web.principal-session.v1"
_ID_RE = re.compile(ID_PATTERN)


class MissingSessionKeyError(RuntimeError):
    """署名の鍵がない(または空)。web は、この場合に起動を拒否する(既定値を持たない。§6.3)。"""


class WeakSessionKeyError(RuntimeError):
    """署名の鍵が弱い(base64url として読めない、またはデコードして 32 バイト未満)。web は、この場合に起動を拒否する(§6.3)。

    エラーの文に、鍵の値は入れない。
    """


def _decoded_length(key: str) -> int | None:
    """key が base64url として読めれば、デコードしたバイト数。読めなければ None。

    使える文字は A-Z・a-z・0-9・`-`・`_`(標準の base64 の `+`・`/` は不可)。末尾の `=`(パディング)は、あってもなくてもよい。
    """
    if _BASE64URL_RE.fullmatch(key) is None:
        return None
    padded = key + "=" * (-len(key) % 4)
    try:
        return len(base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True))
    except binascii.Error:
        return None


def validate_session_key(key: str) -> None:
    """署名の鍵が、base64url として読めて、デコードした長さが 32 バイト以上であることを確かめる。

    満たさなければ WeakSessionKeyError(鍵の値はエラーの文に入れない)。鍵の作り方(32 バイトの乱数):
    `python -c "import secrets; print(secrets.token_urlsafe(32))"`
    """
    length = _decoded_length(key)
    if length is None or length < MIN_SESSION_KEY_BYTES:
        raise WeakSessionKeyError(
            f"the session signing key ({SESSION_KEY_ENV}) must be a base64url string that decodes to at least "
            f"{MIN_SESSION_KEY_BYTES} bytes; generate one with: {_KEY_RECIPE}"
        )


def load_session_key(environ: Mapping[str, str] | None = None) -> str:
    """環境変数 SESSION_SIGNING_KEY から署名の鍵を読み、強さを確かめる(起動時の確認)。

    なければ MissingSessionKeyError、base64url として読めない・32 バイト未満なら WeakSessionKeyError
    (どちらも、web は起動を拒否する)。前後の空白は取り除く(Secret Manager に `print` の出力をそのまま入れると、
    値の末尾に改行が付くため)。鍵の作り方: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
    """
    source = os.environ if environ is None else environ
    key = source.get(SESSION_KEY_ENV, "").strip()
    if not key:
        raise MissingSessionKeyError(
            f"the environment variable {SESSION_KEY_ENV} is not set; "
            f"the web service refuses to start without a session signing key (generate one with: {_KEY_RECIPE})"
        )
    validate_session_key(key)
    return key


@dataclass(frozen=True)
class PrincipalSession:
    """有効なクッキーを持つリクエストの、依頼者の情報(ミドルウェアが作り、ルートが読む)。

    registered は、利用記録 principals_meta がある(面談を送っている)かどうか。
    """

    principal_id: str
    registered: bool


class SessionCodec:
    """依頼者 ID を署名付きクッキーの値にし、検証する。"""

    def __init__(self, secret_key: str, *, max_age_seconds: int) -> None:
        if not secret_key or not secret_key.strip():
            raise MissingSessionKeyError("the session signing key must not be empty")
        validate_session_key(secret_key)  # base64url で 32 バイト以上(create_app の経路でも、弱い鍵では起動しない)
        self._serializer = URLSafeSerializer(
            secret_key,
            salt=_SALT,
            signer_kwargs={"digest_method": hashlib.sha256, "key_derivation": "hmac"},
        )
        self._max_age_seconds = max_age_seconds

    def issue(self, principal_id: str, now: dt.datetime) -> str:
        """principal_id のクッキーの値を作る(期限は now から寿命だけ先。署名の中にも入れる)。"""
        if _ID_RE.fullmatch(principal_id) is None:
            raise ValueError("a principal id must be 16 lowercase hex digits")
        payload = {"pid": principal_id, "exp": int(now.timestamp()) + self._max_age_seconds}
        return self._serializer.dumps(payload)

    def read(self, token: str | None, now: dt.datetime) -> str | None:
        """クッキーの値を検証して依頼者 ID を返す。署名が違う・形が違う・期限切れなら None。"""
        if not token:
            return None
        try:
            payload = self._serializer.loads(token)
        except BadData:
            return None
        if not isinstance(payload, dict):
            return None
        principal_id = payload.get("pid")
        expires_at = payload.get("exp")
        if not isinstance(principal_id, str) or _ID_RE.fullmatch(principal_id) is None:
            return None
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            return None
        if expires_at <= now.timestamp():
            return None
        return principal_id

    def set_cookie(self, response: Response, token: str) -> None:
        """response にセッションクッキーを付ける(署名付き・HttpOnly・Secure・SameSite=Lax・Max-Age)。"""
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=self._max_age_seconds,
            path="/",
            httponly=True,
            secure=True,
            samesite="lax",
        )

    def clear_cookie(self, response: Response) -> None:
        """response でセッションクッキーを消す(本人の「データを消す」のとき)。"""
        response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax")

    def set_cookie_header(self, token: str) -> bytes:
        """`Set-Cookie` ヘッダの値(ミドルウェアが、応答の期限の延長に使う)。set_cookie と同じ形。"""
        holder = Response()
        self.set_cookie(holder, token)
        return next(value for name, value in holder.raw_headers if name == b"set-cookie")
