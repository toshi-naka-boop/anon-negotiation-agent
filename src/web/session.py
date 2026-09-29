"""依頼者のセッションクッキー(design.md §6.3)。

本物の利用者はアカウントを作らない匿名の依頼者で、依頼者 ID は署名付きクッキーで持つ。
- 署名は itsdangerous(HMAC-SHA256)。署名の対象に、依頼者 ID と期限(exp。UNIX 秒)を入れる。
  サーバ側でも期限切れを受け付けない(ブラウザがクッキーを捨てる前提には頼らない)。
- クッキーは HttpOnly・Secure・SameSite=Lax、寿命(Max-Age)は暫定 29 日(config/params.toml)。
- 署名の鍵は環境変数から読む。コードに鍵を書かず、既定値も持たない(鍵がなければ起動を拒否する)。
- 時刻は注入できる(呼び出し側が now を渡す)ので、テストは sleep せずに期限を確かめられる。

ID を発行するのは、開始ページの GET だけ(web.api の start_page)。この module は、発行の条件は
決めず、署名・検証・クッキーの形だけを持つ。
"""

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

# 署名の用途を分けるための固定の文字列(鍵ではない)。別の用途に同じ鍵を使っても、署名が流用されない。
_SALT = "tenshokuagent.web.principal-session.v1"
_ID_RE = re.compile(ID_PATTERN)


class MissingSessionKeyError(RuntimeError):
    """署名の鍵がない(または空)。web は、この場合に起動を拒否する(既定値を持たない。§6.3)。"""


def load_session_key(environ: Mapping[str, str] | None = None) -> str:
    """環境変数 SESSION_SIGNING_KEY から署名の鍵を読む。なければ MissingSessionKeyError。"""
    source = os.environ if environ is None else environ
    key = source.get(SESSION_KEY_ENV, "")
    if not key.strip():
        raise MissingSessionKeyError(
            f"the environment variable {SESSION_KEY_ENV} is not set; "
            "the web service refuses to start without a session signing key"
        )
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
