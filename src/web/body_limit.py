"""リクエスト本文の全体の上限と、読み取りの期限(design.md §8.2「本文の全体の上限」。台帳 X-85・X-90)。

FastAPI は、本文を宣言したルートでは、依存(レート制限・認証)を評価するより先に、本文の全体を読んで JSON として解析する。そのため、ルートごとの上限
(段階開示・攻撃・面談の LLM 入力はそれぞれ 32 KB)だけでは、本文を宣言した別のルートに大きな本文を送られて、枠の外でメモリと CPU を使われる。
そこで、web のすべての要求の本文を、ルートの前(ASGI のミドルウェア。セッションのミドルウェアより外)で、[web.limits] max_request_body_bytes(暫定 64 KB)までに抑える。
さらに、本文は、このミドルウェアが上限まで読み切ってから、セッションのミドルウェアとルートに渡す(読み切った本文を渡し直す。台帳 X-90): 宣言のない大きな本文(チャンク送信)で、
413 より先に、セッションの確認(Firestore の利用記録の更新・依頼者のロック)や、本文を読まないルートの状態の変更が走らないように。

- Content-Length が上限を超えていれば、本文を読まずに、内側に渡さずに 413。
- 本文を持ちうる要求(Content-Length が 0 でない・Transfer-Encoding がある)は、内側に渡す前に、本文のすべてを読む。受け取ったバイト数を数え、超えた時点で、読むのをやめて、
  内側に渡さずに 413(宣言を偽っていても、宣言がなくても同じ)。読み切れた本文は、1 つのメッセージにして、内側に渡し直す(渡し直したあとの receive は、元の receive のまま。
  切断の知らせも、そのまま届く)。
- 本文の読み取りには期限([web.limits] request_body_timeout_seconds。暫定 10 秒)を設ける。要求の本文を、少しずつ(または途中で止めて)送り続けて、接続を居座らせられないように。
  この秒数のうちに読み切れなければ、内側に渡さずに 408(期限は、本文の全体に対する。チャンクごとではない)。
- 本文を持たない要求(GET・HEAD など、Content-Length がない・0 で、Transfer-Encoding もない)は、何も読まず、待たずに、そのまま内側へ渡す(SSE を遅らせない)。
- 断る応答(413・408)は、{"detail": "request_body_too_large"}・{"detail": "request_body_timeout"} で、読み切っていない本文を残さないよう、Connection: close を付ける。
  読んでいる途中でクライアントが切れたときは、応答を返さずに(返す相手がいない)、内側にも渡さずに終わる。
- ルートごとの上限(32 KB)は、そのまま残る。これは、その上の天井。

agents の BodySizeLimitMiddleware(A2A のエラーで返す)と同じ考え方だが、あちらは A2A の口の中で例外を A2A のエラーにする作りなので、web は HTTP の 413・408 を返す別の作りにした。
"""

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_BODY_TOO_LARGE = "request_body_too_large"
REQUEST_BODY_TIMEOUT = "request_body_timeout"


class _BodyTooLarge(Exception):
    """本文が上限を超えた(宣言ではなく、読んで数えた結果)。"""


class _ClientGone(Exception):
    """本文を読み切る前に、クライアントが切れた。"""


def _might_carry_a_body(headers: list[tuple[bytes, bytes]]) -> bool:
    """本文を持ちうる要求か。Transfer-Encoding があるか、Content-Length が 0 でない(数字でない宣言も、信じずに、持ちうるとみなす)。

    どちらも無い要求の本文の長さは 0(RFC 9112 の 6.3)なので、読まずに通す。読もうとすると、本文のない要求の receive が、(サーバによっては)
    切断まで返らず、SSE を止めてしまう。
    """
    lengths = [value for name, value in headers if name == b"content-length"]
    if any(name == b"transfer-encoding" for name, _ in headers):
        return True
    return any(not (value.isdigit() and int(value) == 0) for value in lengths)


class RequestBodyLimitMiddleware:
    """本文の大きさに上限と読み取りの期限を掛け、読み切った本文を内側に渡す、純粋な ASGI ミドルウェア(BaseHTTPMiddleware は本文の読み取りを包めないので使わない)。"""

    def __init__(self, app: ASGIApp, *, max_bytes: int, timeout_seconds: float) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = list(scope["headers"])
        declared = max(
            (int(value) for name, value in headers if name == b"content-length" and value.isdigit()),
            default=None,
        )
        if declared is not None and declared > self.max_bytes:
            await self._refuse(scope, receive, send, 413, REQUEST_BODY_TOO_LARGE)
            return
        if not _might_carry_a_body(headers):
            await self.app(scope, receive, send)
            return

        try:
            body = await self._read_body(receive)
        except _BodyTooLarge:
            await self._refuse(scope, receive, send, 413, REQUEST_BODY_TOO_LARGE)
            return
        except TimeoutError:
            await self._refuse(scope, receive, send, 408, REQUEST_BODY_TIMEOUT)
            return
        except _ClientGone:
            return

        handed_over = False

        async def replay_receive() -> Message:
            nonlocal handed_over
            if not handed_over:
                handed_over = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _read_body(self, receive: Receive) -> bytes:
        """本文のすべてを、上限と期限の中で読む。超えたら _BodyTooLarge(それ以上は読まない)・期限なら TimeoutError・切れたら _ClientGone。"""
        chunks: list[bytes] = []
        received = 0
        async with asyncio.timeout(self.timeout_seconds):
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    raise _ClientGone
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > self.max_bytes:
                    raise _BodyTooLarge
                chunks.append(chunk)
                if not message.get("more_body", False):
                    return b"".join(chunks)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, status_code: int, detail: str) -> None:
        """断る(Connection: close つき。読み切っていない本文を残さない)。"""

        async def send_closing(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"connection", b"close")]}
            await send(message)

        await JSONResponse({"detail": detail}, status_code=status_code)(scope, receive, send_closing)
