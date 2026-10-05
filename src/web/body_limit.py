"""リクエスト本文の全体の上限(design.md §8.2「本文の全体の上限」。台帳 X-85)。

FastAPI は、本文を宣言したルートでは、依存(レート制限・認証)を評価するより先に、本文の全体を読んで JSON として解析する。そのため、ルートごとの上限
(段階開示・攻撃・面談の LLM 入力はそれぞれ 32 KB)だけでは、本文を宣言した別のルートに大きな本文を送られて、枠の外でメモリと CPU を使われる。
そこで、web のすべての要求の本文を、ルートの前(ASGI のミドルウェア。セッションのミドルウェアより外)で、[web.limits] max_request_body_bytes(暫定 64 KB)までに抑える。

- Content-Length が上限を超えていれば、本文を読まずに、ルートにも渡さずに 413。
- 宣言がなければ(チャンク送信)・偽っているときは、ルートが本文を読むたびに受け取ったバイト数を数え、超えた時点で、本文の読み取りの中で HTTPException(413) を投げる
  (FastAPI は、本文の読み取りで投げられた HTTPException をそのまま伝える。ほかの例外は 400 にしてしまうので、HTTPException にする)。解析にも、依存にも進まない。
- どちらも、本文は {"detail": "request_body_too_large"}。読み切っていない本文を残さないよう、応答には Connection: close を付ける。
- ルートごとの上限(32 KB)は、そのまま残る。これは、その上の天井。

agents の BodySizeLimitMiddleware(A2A のエラーで返す)と同じ考え方だが、あちらは A2A の口の中で例外を A2A のエラーにする作りなので、web は HTTP の 413 を返す別の作りにした。
"""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_BODY_TOO_LARGE = "request_body_too_large"


class RequestBodyLimitMiddleware:
    """本文の大きさに上限を掛ける、純粋な ASGI ミドルウェア(BaseHTTPMiddleware は本文の読み取りを包めないので使わない)。"""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        refused = False

        async def send_closing(message: Message) -> None:
            if refused and message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"connection", b"close")]}
            await send(message)

        declared = max(
            (int(value) for name, value in scope["headers"] if name == b"content-length" and value.isdigit()),
            default=None,
        )
        if declared is not None and declared > self.max_bytes:
            refused = True
            await JSONResponse({"detail": REQUEST_BODY_TOO_LARGE}, status_code=413)(scope, receive, send_closing)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    refused = True
                    raise HTTPException(status_code=413, detail=REQUEST_BODY_TOO_LARGE)
            return message

        await self.app(scope, limited_receive, send_closing)
