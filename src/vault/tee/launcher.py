"""launcher(Confidential Space のコンテナ起動器)から、独自の audience・nonce の attestation トークンを取る(契約 §3)。

launcher は Unix ソケット(`launcher_socket`。既定 /run/container_launcher/teeserver.sock)で HTTP を話す。
`POST http://localhost/v1/token`、本文 `{"audience": ..., "token_type": "OIDC", "nonces": [...]}`。応答の本文(文字列)がトークン(JWT)。
audience は最大 512 バイト、nonce は 1 個 10〜74 バイトで最大 6 個、launcher への要求は Google Cloud Attestation に毎秒 5 件まで。

transport は差し込める(テストは httpx.MockTransport。本物は httpx.HTTPTransport(uds=...))。
トークンの値は、例外の文にもログにも書かない(attestation トークンは公開情報だが、書かない方針で揃える)。
"""

from collections.abc import Sequence

import httpx

LAUNCHER_TOKEN_URL = "http://localhost/v1/token"
_TIMEOUT_SECONDS = 10.0


class LauncherError(Exception):
    """launcher からトークンを取れなかった(届かない・2xx 以外・空)。メッセージは、ステータスか例外の型名だけ。"""


class LauncherClient:
    def __init__(self, *, socket_path: str, transport: httpx.BaseTransport | None = None) -> None:
        self._socket_path = socket_path
        self._transport = transport

    def get_token(self, *, audience: str, nonces: Sequence[str]) -> str:
        """audience と nonces を入れた OIDC の attestation トークンを 1 つ取る。"""
        # 呼び出しごとに接続する(launcher 側が閉じた接続を使い回して、失敗しないように)。
        transport = self._transport or httpx.HTTPTransport(uds=self._socket_path)
        body = {"audience": audience, "token_type": "OIDC", "nonces": list(nonces)}
        try:
            with httpx.Client(transport=transport, timeout=_TIMEOUT_SECONDS, trust_env=False) as http:
                response = http.post(LAUNCHER_TOKEN_URL, json=body)
        except httpx.HTTPError as exc:
            raise LauncherError(f"could not reach the launcher ({type(exc).__name__})") from exc
        if not response.is_success:
            raise LauncherError(f"the launcher returned {response.status_code}")
        token = response.text.strip()
        if not token:
            raise LauncherError("the launcher returned an empty token")
        return token
