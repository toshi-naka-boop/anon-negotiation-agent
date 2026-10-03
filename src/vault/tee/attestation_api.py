"""`GET /v1/attestation?nonce=<nonce>`: 金庫の attestation の口(research/tee-spike-contract.md §3)。

web とスクリプトは、この口の応答(attestation トークン)を確かめるまで金庫を信用しない。そのため、この口は呼び出し元の認証を
掛けない(ID トークンを、未検証の相手に渡さないため。届くのは VPC のファイアウォールと IAP の範囲だけ)。vault.app.create_app が、
caller_verifier の依存の外に、素の Starlette の経路として付ける。

launcher に `{"audience": attestation_audience, "token_type": "OIDC", "nonces": [nonce, certificate_sha256]}` を求める。
certificate_sha256 は、この金庫が TLS で出す葉の証明書(DER)の SHA-256(小文字の 16 進 64 文字)。web は、自分で計算した値が
トークンの eat_nonce にあることを確かめて、その証明書だけを信用する(検証してからピン留め)。

- 200 `{"token": "<jwt>", "certificate_sha256": "<hex64>"}`
- 400 `{"detail": "invalid nonce"}`: nonce の形が違う(`[A-Za-z0-9_-]{16,74}`。nonce が無い・2 個以上もここ)。
- 429 `{"detail": "attestation rate limited"}`: 前回の launcher 呼び出しから min_interval_seconds 未満
  (launcher の先の Google Cloud Attestation は 1 プロジェクト・1 リージョンで毎秒 5 件まで)。
- 503 `{"detail": "attestation unavailable"}`: launcher に届かない・2xx 以外・空。
detail は固定文(launcher の応答も例外の文も載せない)。
"""

import logging
import re
import threading
import time
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from vault.tee.launcher import LauncherClient, LauncherError

logger = logging.getLogger(__name__)

# launcher の制限は 1 個 10〜74 バイト。下限は web の乱数(32 バイトの base64url で 43 文字)に余裕を持たせた 16。
# fullmatch で使う(`$` は末尾の改行も許してしまうので使わない)。
NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,74}")


class AttestationService:
    """nonce と証明書のハッシュを入れた attestation トークンを、launcher から取って返す。"""

    def __init__(
        self,
        *,
        audience: str,
        certificate_sha256: str,
        launcher: LauncherClient,
        min_interval_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audience = audience
        self._certificate_sha256 = certificate_sha256
        self._launcher = launcher
        self._min_interval = min_interval_seconds
        self._now = now
        self._last_call_at: float | None = None
        self._lock = threading.Lock()

    def endpoint(self, request: Request) -> Response:
        """GET /v1/attestation の処理(素の Starlette の経路。同期なのでスレッドプールで動く)。"""
        nonces = request.query_params.getlist("nonce")
        if len(nonces) != 1 or not NONCE_PATTERN.fullmatch(nonces[0]):
            return JSONResponse(status_code=400, content={"detail": "invalid nonce"})
        if not self._may_call_launcher():
            return JSONResponse(status_code=429, content={"detail": "attestation rate limited"})
        try:
            token = self._launcher.get_token(audience=self._audience, nonces=[nonces[0], self._certificate_sha256])
        except LauncherError as exc:
            logger.warning("attestation: %s", exc)
            return JSONResponse(status_code=503, content={"detail": "attestation unavailable"})
        return JSONResponse({"token": token, "certificate_sha256": self._certificate_sha256})

    def _may_call_launcher(self) -> bool:
        """前回の launcher 呼び出し(失敗も数える)から間隔が空いていれば、今回の時刻を記録して True。"""
        with self._lock:
            now = self._now()
            if self._last_call_at is not None and now - self._last_call_at < self._min_interval:
                return False
            self._last_call_at = now
            return True
