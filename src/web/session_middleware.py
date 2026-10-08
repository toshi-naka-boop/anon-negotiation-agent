"""依頼者のセッションのミドルウェア(design.md §6.3)。すべてのリクエストの入口で、次を行う。

1. 状態を変えるリクエストは POST に限り、独自ヘッダ(X-Requested-With)を必須にする。他サイトからの
   単純なフォーム送信を通さないため。ヘッダのない POST は、クッキーにも Firestore にも触れずに 403 にする。
2. 有効な署名付きクッキー(署名が合い、期限内)があれば、その依頼者のロックを取り(web.locks。台帳 I-4)、
   利用記録 principals_meta を更新する(web.principals_meta の touch。閲覧だけの GET も含む)。
   - 削除中(deletion_state=deleting)なら、書かずに 409 で拒否する。以後、その依頼者の操作はすべて拒否する。
   - 書いたとき(1 時間に 1 回まで)だけ、その応答で、クッキーの期限を今から寿命だけ先に延ばす。
     クッキーの期限は、書き込みが成功したときにだけ、その応答で延ばす(データの自動削除より必ず早く切れる)。
   ロックは、リクエストの処理が終わって応答を送り終えるまで持つ。同じ依頼者の操作と削除の流れを、
   1 つずつ順に処理するため。
3. 依頼者の情報(PrincipalSession)を request.state に置く。ルートはこれを読んで、権限を確かめる。
   クッキーがない・無効・期限切れなら、置かない(ID は、ここでは発行しない。発行は開始ページの GET だけ)。

セッションを使わないパス(デモ用のエンドポイント。session_free_prefixes)は、1 だけを行う。デモ用の
エンドポイントは、本物の依頼者のセッションを見ない・触れない(§6.3)。

ログには、例外の型名だけを書く(クッキー・依頼者の ID・入力は書かない)。
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vault.clock import Clock

from web.locks import PrincipalLocks
from web.principals_meta import PrincipalsMetaStore
from web.session import SESSION_COOKIE_NAME, PrincipalSession, SessionCodec

_log = logging.getLogger(__name__)

REQUESTED_WITH_HEADER = "x-requested-with"
_SET_COOKIE = b"set-cookie"


class PrincipalSessionMiddleware:
    """純粋な ASGI ミドルウェア(BaseHTTPMiddleware は、ロックを持つ範囲が応答の送信と食い違うので使わない)。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        codec: SessionCodec,
        meta: PrincipalsMetaStore,
        locks: PrincipalLocks,
        clock: Clock,
        session_free_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self._codec = codec
        self._meta = meta
        self._locks = locks
        self._clock = clock
        self._session_free_prefixes = session_free_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        # 0. web の口は GET と POST だけ。ほかのメソッド(HEAD・PUT・DELETE など)は、セッションの確認(Firestore の利用記録の読み書き)より前に 405 で断り、Allow: GET, POST を付ける
        #    (有効なクッキーつきの要求で、確認の読み出しを増やせないように。v24 の読み取りの枠の抜け道。C-73。Allow は v25。L22-1)。
        #    セッションのクッキーを持つ要求は、メソッドによらず、これより外の読み取りの枠のミドルウェアが先に数えている(セッションを見ない経路を除く。v25。C-74)ので、ここに届く 405 も枠の中。
        if scope["method"] not in ("GET", "POST"):
            await self._reject(scope, receive, send, 405, "method_not_allowed", headers={"Allow": "GET, POST"})
            return
        # 1. 状態を変えるリクエスト(POST)は、独自ヘッダを必須にする。
        if scope["method"] == "POST" and not request.headers.get(REQUESTED_WITH_HEADER):
            await self._reject(scope, receive, send, 403, "missing_requested_with_header")
            return

        if scope["path"].startswith(self._session_free_prefixes):
            await self.app(scope, receive, send)
            return

        # 2. 有効なクッキーがなければ、セッションなしで通す(ID の発行は開始ページの GET だけ)。
        principal_id = self._codec.read(request.cookies.get(SESSION_COOKIE_NAME), self._clock.now())
        if principal_id is None:
            await self.app(scope, receive, send)
            return

        async with self._locks.lock(principal_id):
            try:
                touch = await self._meta.touch(principal_id)
            except Exception as exc:
                # 削除中かどうかを確かめられないまま、依頼者の操作を通さない。
                _log.error("session touch failed error=%s", type(exc).__name__)
                await self._reject(scope, receive, send, 503, "temporarily_unavailable")
                return
            if touch.outcome == "deleting":
                await self._reject(scope, receive, send, 409, "principal_deleting")
                return

            request.state.principal_session = PrincipalSession(
                principal_id=principal_id, registered=touch.outcome != "absent"
            )
            extension: bytes | None = None
            if touch.outcome == "updated" and touch.written_at is not None:
                # 書き込みと同じ時刻から寿命を数える(クッキーの期限は、必ず delete_after より早く切れる)。
                extension = self._codec.set_cookie_header(self._codec.issue(principal_id, touch.written_at))
            await self.app(scope, receive, self._send_with_cookie(send, extension))

    def _send_with_cookie(self, send: Send, extension: bytes | None) -> Send:
        """extension があれば、応答に期限を延ばしたクッキーを付ける(ルートが同じクッキーを付けていなければ)。"""
        if extension is None:
            return send

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(_sets_session_cookie(name, value) for name, value in headers):
                    headers.append((_SET_COOKIE, extension))
            await send(message)

        return wrapped

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, status_code: int, detail: str, headers: dict[str, str] | None = None
    ) -> None:
        await JSONResponse({"detail": detail}, status_code=status_code, headers=headers)(scope, receive, send)


def _sets_session_cookie(name: bytes, value: bytes) -> bool:
    return name.lower() == _SET_COOKIE and value.startswith(SESSION_COOKIE_NAME.encode("ascii") + b"=")
