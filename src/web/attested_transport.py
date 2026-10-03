"""金庫への「検証してからピン留めする」transport(design.md §9・research/tee-spike.md 3-2、research/tee-spike-contract.md §8)。

TEE 版の金庫(Confidential Space の VM の中)は、起動のたびに自己署名の TLS 証明書を作る。web は、その証明書が「公開したコードを動かす
本物の金庫」のものだと次の手順で確かめてから、その 1 枚だけを信用して、以後の要求を流す。

1. 証明書を検証なしで取り(ssl.get_server_certificate)、DER の SHA-256 を計算する。
2. その 1 枚だけを信用する接続で、GET /v1/attestation?nonce=<新しい乱数> を呼ぶ。ID トークンは付けない(まだ信用していない相手に渡さない)。
3. 返ったトークンを verify_attestation_token で確かめる。nonce と、自分で計算した証明書のハッシュの両方がトークンの eat_nonce に入っていること、
   イメージ digest が許可リスト(deploy/vault-releases.json)にあること、本番イメージであること、などを見る(応答の certificate_sha256 は参考値)。
4. 通れば、その接続(SSLContext)に以後の要求を流す。通らなければ、httpx.ConnectError にして投げる(金庫のクライアントは「一時的に応えない」
   (VaultUnavailableError)にし、レフェリーは待つ)。

金庫の再起動で証明書が変わる。接続エラー(httpx.ConnectError・ssl.SSLError・httpx.RemoteProtocolError)が出たら、検証し直して付け替え、
同じ要求を 1 回だけ送り直す。接続エラーがなくても、一定の間隔(reverify_interval_seconds。既定 600 秒)で検証し直す(Confidential Space の
イメージが失効すると swname が GCE になる。画面が verified=false のままデータが流れ続けないように): 間隔をすぎた最初の要求が検証し直す。

検証の失敗は 2 種類に分ける。
- 確定した否定: トークンが取れて、検証の結果が否定(AttestationError で、reason が unavailable 以外。debug・失効・許可リストにない digest・
  swname が GCE・期限切れ・nonce 不一致など)。ピン留めを外し、通るまで、すべての要求を httpx.ConnectError にする。外したあとは、
  各要求が最短 min_reverify_interval_seconds(既定 2 秒)の間隔で検証し直しを試み、通ったら復帰する(次の定期の検証を待たない)。
- 一時的な失敗: 金庫の 429・503・接続エラー・時間切れ・応答の形違い・Google の署名鍵を取れない(AttestationError の reason が unavailable、
  または httpx.ConnectError)。ピン留めを外さず、いまの接続をそのまま使い続ける。定期の検証し直しでこれが出たら、以後、要求を待たせずに、
  最短の間隔で、通るまで検証し直しを続ける(回数の上限はない)。接続が壊れたための検証し直しでこれが出たときは、その要求だけが接続エラー
  (ピン留めは残るので、金庫が戻れば、そのまま使える)。
  ただし、最後に検証が通ってから max_unverified_seconds(既定 1800 秒)をこえたら、一時的な失敗が続いていても、業務の要求を httpx.ConnectError で
  閉じる(ピン留めの証明書は残すが、データは送らない)。閉じている間も、最短の間隔で検証し直しを試み、通ったら(その要求から)復帰する。
WARNING のログは、失敗の理由が変わったときの 1 行(と、業務の要求を閉じ始めたときの 1 行)だけ。同じ理由が続いても、検証し直すたびには書かない。

金庫の /v1/attestation は、金庫全体で最短の間隔(1 秒)を置く: web の検証し直しと、検証スクリプトなどが重なると 429、launcher の一時的な失敗は 503
になる。検証(と attest)では、この 429・503 を一時的な失敗として、1.1 秒待って 1 回だけやり直し、それでも断られたら unavailable にする。

検証(1〜3。初回と付け替え)は transport で 1 つだけ動かす(single-flight): 同時に来た要求は、動いている検証の結果を待って共有し、それぞれが
/v1/attestation を呼ぶことはない。検証が失敗したら、待っていた要求にも同じ失敗(httpx.ConnectError)を返す。検証し直す間隔の下限
(min_reverify_interval_seconds。既定 2 秒。Google Cloud Attestation は毎秒 5 件まで)は、この共有の後に適用する: 直前の検証から
間隔のうちなら、検証し直さず、その結果を使う(成功していて、その接続がもう使えないなら、元の接続エラーを返す)。

attest(nonce)(web の GET /api/tee/attestation が使う)も、ピン留めした接続で検証する。確定した否定が出たら、その場でピン留めを外す
(画面が verified=false のままデータが流れ続けないように)。一時的な失敗(unavailable)では外さない。

ログと例外の文には、トークンの値を書かない(理由 reason と、例外の型名だけ)。
"""

import asyncio
import json
import logging
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

from negotiation_core.attestation import (
    UNAVAILABLE,
    AttestationError,
    AttestationPolicy,
    SignerCerts,
    VerifiedAttestation,
    certificate_sha256_of_pem,
    pinned_ssl_context,
    verify_attestation_token,
)

_log = logging.getLogger(__name__)

# 検証し直して、同じ要求を 1 回だけ送り直すエラー(金庫の再起動で証明書が変わると、検証に失敗するか、接続が切れる)。
_RETRYABLE = (httpx.ConnectError, ssl.SSLError, httpx.RemoteProtocolError)
# 検証前の(信用していない相手の)応答の大きさの上限。実際のトークンは数 KB。
_MAX_RESPONSE_BYTES = 64 * 1024
# 金庫の /v1/attestation が一時的に断るときのステータス(429: 最短の間隔の制限、503: launcher の失敗)と、やり直すまでの待ち(金庫の最短の間隔 1 秒より少し長く)
_TRANSIENT_STATUSES = frozenset({429, 503})
_TRANSIENT_RETRY_SECONDS = 1.1


class AttestationSource(Protocol):
    """web の GET /api/tee/attestation が使う口。AttestedVaultTransport と、テストの偽物がこれを満たす。"""

    certificate_sha256: str | None  # web が計算した、ピン留めした(または最後に見た)証明書の SHA-256

    async def attest(self, nonce: str) -> tuple[str, VerifiedAttestation]: ...


@dataclass(eq=False)
class _Pinned:
    """検証済みの 1 枚の証明書と、それだけを信用する接続。"""

    transport: httpx.AsyncHTTPTransport
    certificate_sha256: str
    verified_at: float  # 検証が通った時刻(単調時計。定期の検証し直しの起点)
    retrying: bool = False  # 定期の検証し直しが一時的に失敗していて、やり直している間(要求を待たせずに、いまの接続を使い続ける)


@dataclass(eq=False)
class _Outcome:
    """1 回の検証の結果。成功なら pinned・token・verified、失敗なら error(待っていた要求にも、この結果を返す)。

    transient は、失敗が一時的なもの(ピン留めを外さない)か、確定した否定(外す)か。
    """

    finished_at: float
    nonce: str
    pinned: _Pinned | None = None
    token: str | None = None
    verified: VerifiedAttestation | None = None
    error: AttestationError | httpx.ConnectError | None = None
    transient: bool = False


class _TooSoon(Exception):
    """直前の検証から間隔のうちで、その接続がもう使えない。検証し直さない(呼び出し側は、元のエラーを返す)。"""


def _replay(error: AttestationError | httpx.ConnectError) -> AttestationError | httpx.ConnectError:
    """待っていた要求に返す、同じ失敗(例外の実体は要求ごとに分ける)。"""
    if isinstance(error, AttestationError):
        return AttestationError(error.reason, token=error.token)
    return httpx.ConnectError(str(error))


class AttestedVaultTransport(httpx.AsyncBaseTransport):
    """金庫の証明書を、attestation で確かめてからピン留めする httpx の transport。

    vault_url は https://<host>[:port](金庫の URL)。この transport は、その宛先への要求だけを送る(トークンを別の宛先に
    送らないため)。certs は検証に使う署名鍵(kid → PEM の辞書、または SignerCerts)。clock は検証の間隔を測る単調時計(テストで差し込む)。
    min_reverify_interval_seconds: 検証の間隔の下限(失敗が続いても、これより短い間隔では検証し直さない)。
    reverify_interval_seconds: 検証が通っていても、この間隔をすぎたら検証し直す。
    max_unverified_seconds: 最後に検証が通ってから、この秒数をこえたら、業務の要求を閉じる(一時的な失敗が続いても、検証されないまま、データを送り続けない)。
        reverify_interval_seconds より長くする。
    sleep: 金庫が /v1/attestation を一時的に断った(429・503)ときの待ちに使う(テストで差し込む)。

    公開する属性: last_verification・last_verified_at(最後に成功した検証とその時刻。UNIX 秒)・certificate_sha256
    (ピン留めした、または最後に見た証明書の SHA-256)。
    """

    def __init__(
        self,
        vault_url: str,
        *,
        policy: AttestationPolicy,
        certs: Mapping[str, str] | SignerCerts,
        min_reverify_interval_seconds: float = 2.0,
        reverify_interval_seconds: float = 600.0,
        max_unverified_seconds: float = 1800.0,
        request_timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        url = httpx.URL(vault_url)
        if url.scheme != "https" or not url.host:
            raise ValueError("the vault URL must be https://<host>[:port] (the vault speaks TLS inside the TEE)")
        self._url = url
        self._host, self._port = url.host, url.port or 443
        self._policy = policy
        self._certs = certs
        self._min_interval = min_reverify_interval_seconds
        self._reverify_interval = reverify_interval_seconds
        self._max_unverified = max_unverified_seconds
        self._timeout = httpx.Timeout(request_timeout_seconds)
        self._clock = clock
        self._sleep = sleep
        self._pinned: _Pinned | None = None
        self._flight: asyncio.Future[_Outcome] | None = None  # 動いている検証(1 つだけ)
        self._last: _Outcome | None = None  # 直前に終わった検証の結果
        self._logged_failure: str | None = None  # WARNING に書いた最後の失敗の理由(同じ理由が続いても、また書かない)
        self._verified_mono: float | None = None  # 最後に検証が通った時刻(clock。flight と attest のどちらでも)
        self._closed_logged = False  # 業務の要求を閉じ始めたことを、WARNING に書いたか
        self.last_verification: VerifiedAttestation | None = None
        self.last_verified_at: float | None = None
        self.certificate_sha256: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme, request.url.host, request.url.port or 443) != ("https", self._host, self._port):
            raise ValueError("this transport only sends requests to the attested vault")
        try:
            pinned, _ = await self._pinned_for(None)
            try:
                return await pinned.transport.handle_async_request(request)
            except _RETRYABLE as exc:
                pinned, _ = await self._repin(pinned, exc)
                return await pinned.transport.handle_async_request(request)
        except AttestationError as exc:
            raise httpx.ConnectError(f"vault attestation failed: {exc.reason}") from exc

    async def attest(self, nonce: str) -> tuple[str, VerifiedAttestation]:
        """ピン留めした接続で GET /v1/attestation?nonce= を呼び、検証して (トークン, 検証結果) を返す(web の API が使う)。

        検証できなければ AttestationError(reason。トークンが取れていれば .token に入る)。金庫に届かなければ httpx の例外。
        確定した否定(reason が unavailable 以外)なら、その場でピン留めを外す。
        まだピン留めしていなければ、この nonce で検証してピン留めする(金庫を呼ぶのは 1 回。金庫は最短の間隔を置く)。
        """
        pinned, outcome = await self._pinned_for(None, nonce)
        if outcome is not None and outcome.nonce == nonce:  # この呼び出しが始めた検証が、ピン留めと、この nonce の検証を兼ねた
            return outcome.token, outcome.verified
        try:
            return await self._attest_checked(pinned, nonce)
        except _RETRYABLE as exc:
            pinned, outcome = await self._repin(pinned, exc, nonce)
            if outcome is not None and outcome.nonce == nonce:
                return outcome.token, outcome.verified
            return await self._attest_checked(pinned, nonce)

    async def aclose(self) -> None:
        flight, self._flight = self._flight, None
        if flight is not None:
            flight.cancel()
            await asyncio.gather(flight, return_exceptions=True)
        self._last = None
        await self._drop_pin()

    # --- ピン留め(single-flight) ---------------------------------------------------------------------------------

    async def _repin(
        self, stale: _Pinned, error: Exception, nonce: str | None = None
    ) -> tuple[_Pinned, _Outcome | None]:
        """使ったら壊れていた接続 stale の代わりを得る。直前の検証から間隔のうちで、代わりがなければ、元のエラーを返す。"""
        try:
            return await self._pinned_for(stale, nonce)
        except _TooSoon:
            raise error from None

    async def _pinned_for(self, stale: _Pinned | None, nonce: str | None = None) -> tuple[_Pinned, _Outcome | None]:
        """使ってよい、検証済みの接続。stale は、使ったら壊れていた接続(なければ None)。

        使える接続(stale でなく、検証から reverify_interval_seconds 以内)があれば、それを返す。検証が動いていれば、その結果を待つ
        (確定した否定なら同じ失敗を投げる。一時的な失敗なら、使える接続のまま)。動いていなければ、直前の検証から間隔のうちかを見て
        (間隔のうちなら、検証し直さない。確定した否定は同じ失敗を投げ、一時的な失敗は使える接続のまま、成功した接続が stale ならもう
        使えないので _TooSoon)、間隔をすぎていれば検証を始める。定期の検証し直しが一時的に失敗してやり直している間(retrying)は、
        検証を待たずに、使える接続を返す。ただし、最後に検証が通ってから max_unverified_seconds をこえていれば、一時的な失敗でも
        使える接続のままにはせず(検証の結果を待ち、通らなければ失敗)、業務の要求を閉じる。
        2 つ目の値は、この呼び出しが待った検証の結果(すでにある接続を返したときは None)。
        """
        now = self._clock()
        current = self._pinned
        usable = current is not None and current is not stale
        too_long = self._unverified_too_long()
        if usable and not too_long and now - current.verified_at < self._reverify_interval:
            return current, None
        keep = usable and not too_long  # 一時的な失敗のあいだも、いまの接続を使い続けてよいか
        background = keep and current.retrying  # 要求を待たせずにやり直す検証
        if self._flight is None:
            last = self._last
            if last is not None and now - last.finished_at < self._min_interval:
                if last.error is not None:
                    if keep and last.transient:
                        return current, None
                    raise _replay(last.error)
                if current is not None:
                    if usable:
                        return current, None
                    raise _TooSoon
            # 待たずにやり直す検証は、呼び出し側の nonce を使わない(待たない呼び出し側は、自分で金庫を呼ぶ)
            self._flight = asyncio.ensure_future(
                self._run_flight(nonce if nonce and not background else secrets.token_urlsafe(32))
            )
        if background:
            return current, None
        outcome = await asyncio.shield(self._flight)  # 待っている要求が取り消されても、ほかの要求のために検証は続ける
        if outcome.error is None:
            return outcome.pinned, outcome
        if outcome.transient and usable and self._pinned is current and not self._unverified_too_long():
            return current, None
        raise _replay(outcome.error)

    def _unverified_too_long(self) -> bool:
        return self._verified_mono is not None and self._clock() - self._verified_mono > self._max_unverified

    async def _run_flight(self, nonce: str) -> _Outcome:
        try:
            try:
                outcome = await self._verify_and_pin(nonce)
            except AttestationError as exc:
                outcome = _Outcome(finished_at=self._clock(), nonce=nonce, error=exc, transient=exc.reason == UNAVAILABLE)
            except httpx.ConnectError as exc:
                outcome = _Outcome(finished_at=self._clock(), nonce=nonce, error=exc, transient=True)
            self._last = outcome
            if outcome.error is None:
                self._note_success()
            else:
                self._note_failure(outcome.error)
                if not outcome.transient:
                    await self._drop_pin()  # 確定した否定: 以前に検証した接続にも、要求を流さない
                else:
                    if self._pinned is not None and self._clock() - self._pinned.verified_at >= self._reverify_interval:
                        self._pinned.retrying = True  # 定期の検証し直しが一時的に失敗: いまの接続を使い続け、要求を待たせずにやり直す
                    if self._unverified_too_long() and not self._closed_logged:
                        self._closed_logged = True
                        _log.warning(
                            "vault requests are closed: the attestation has not passed for more than %s seconds", self._max_unverified
                        )
            return outcome
        finally:
            self._flight = None

    async def _drop_pin(self) -> None:
        pinned, self._pinned = self._pinned, None
        if pinned is not None:
            await pinned.transport.aclose()

    async def _verify_and_pin(self, nonce: str) -> _Outcome:
        """契約 §8 の 1〜4。通れば pinned を差し替える。通らなければ AttestationError か httpx.ConnectError。"""
        try:
            pem = await asyncio.to_thread(ssl.get_server_certificate, (self._host, self._port), timeout=self._timeout.connect)
        except OSError as exc:  # 接続の拒否・時間切れ・TLS の失敗(ssl.SSLError)は OSError の一種
            raise httpx.ConnectError(f"could not get the vault certificate ({type(exc).__name__})") from exc
        certificate_sha256 = certificate_sha256_of_pem(pem)
        self.certificate_sha256 = certificate_sha256
        candidate = httpx.AsyncHTTPTransport(verify=pinned_ssl_context(pem))
        try:
            try:
                token = await self._fetch_token(candidate, nonce)
            except (httpx.TransportError, ssl.SSLError) as exc:
                raise httpx.ConnectError(f"could not call the vault attestation endpoint ({type(exc).__name__})") from exc
            verified = await self._verify(token, nonce, certificate_sha256)
        except BaseException:
            await candidate.aclose()
            raise
        previous = self._pinned
        if previous is not None and previous.certificate_sha256 == certificate_sha256:
            await candidate.aclose()  # 同じ証明書: 今の接続をそのまま使う(通信中の要求を切らない)
            previous.verified_at = self._clock()
            previous.retrying = False
            pinned = previous
        else:
            pinned = _Pinned(candidate, certificate_sha256, self._clock())
            self._pinned = pinned
            if previous is not None:
                await previous.transport.aclose()
        self._record(verified)
        _log.info("vault attestation verified; the certificate is pinned")
        return _Outcome(finished_at=self._clock(), nonce=nonce, pinned=pinned, token=token, verified=verified)

    # --- 金庫の /v1/attestation ---------------------------------------------------------------------------------------

    async def _attest_checked(self, pinned: _Pinned, nonce: str) -> tuple[str, VerifiedAttestation]:
        """_attest_on に、確定した否定のときのピンの取り外しを足したもの。"""
        try:
            return await self._attest_on(pinned, nonce)
        except AttestationError as exc:
            if exc.reason != UNAVAILABLE and self._pinned is pinned:
                # 画面に verified=false を返すときは、この接続にも要求を流さない。以後は、最短の間隔で検証し直して、通るまで ConnectError
                self._last = _Outcome(finished_at=self._clock(), nonce=nonce, error=exc)
                await self._drop_pin()
            raise

    async def _attest_on(self, pinned: _Pinned, nonce: str) -> tuple[str, VerifiedAttestation]:
        try:
            token = await self._fetch_token(pinned.transport, nonce)
            verified = await self._verify(token, nonce, pinned.certificate_sha256)
        except AttestationError as exc:
            self._note_failure(exc)
            raise
        self._note_success()
        self._record(verified)
        return token, verified

    async def _fetch_token(self, transport: httpx.AsyncHTTPTransport, nonce: str) -> str:
        """GET /v1/attestation?nonce=(認証ヘッダなし)。トークンを返す。使えない応答は AttestationError(UNAVAILABLE)、通信の失敗は httpx の例外。

        金庫が一時的に断った(429・503)ときは、1.1 秒待って 1 回だけやり直す。
        """
        status, body = await self._get_attestation(transport, nonce)
        if status in _TRANSIENT_STATUSES:
            await self._sleep(_TRANSIENT_RETRY_SECONDS)
            status, body = await self._get_attestation(transport, nonce)
        if status != 200:
            raise AttestationError(UNAVAILABLE)
        try:
            token = json.loads(body)["token"]
        except (ValueError, KeyError, TypeError, RecursionError):
            raise AttestationError(UNAVAILABLE) from None
        if not isinstance(token, str) or not token:
            raise AttestationError(UNAVAILABLE)
        return token

    async def _get_attestation(self, transport: httpx.AsyncHTTPTransport, nonce: str) -> tuple[int, bytes]:
        """GET /v1/attestation?nonce= を 1 回呼ぶ。(ステータス, 本文)。本文が大きすぎれば AttestationError(UNAVAILABLE)。"""
        url = self._url.copy_with(path="/v1/attestation", params={"nonce": nonce})
        response = await transport.handle_async_request(httpx.Request("GET", url, extensions={"timeout": self._timeout.as_dict()}))
        body = bytearray()
        try:
            async for chunk in response.stream:
                body += chunk
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise AttestationError(UNAVAILABLE)
        finally:
            await response.aclose()
        return response.status_code, bytes(body)

    async def _verify(self, token: str, nonce: str, certificate_sha256: str) -> VerifiedAttestation:
        # 署名鍵の取得(SignerCerts)は同期の HTTP なので、スレッドで動かす
        return await asyncio.to_thread(
            verify_attestation_token,
            token,
            certs=self._certs,
            policy=self._policy,
            nonce=nonce,
            certificate_sha256=certificate_sha256,
        )

    def _record(self, verified: VerifiedAttestation) -> None:
        self.last_verification = verified
        self.last_verified_at = time.time()
        self._verified_mono = self._clock()
        self._closed_logged = False

    def _note_success(self) -> None:
        self._logged_failure = None

    def _note_failure(self, error: AttestationError | httpx.ConnectError) -> None:
        """WARNING は、失敗の理由が変わったときの 1 行だけ。理由は固定の語か、例外の型名だけ(トークンの値を含まない)。"""
        key = error.reason if isinstance(error, AttestationError) else str(error)
        if key == self._logged_failure:
            return
        self._logged_failure = key
        if isinstance(error, AttestationError):
            _log.warning("vault attestation failed reason=%s", error.reason)
        else:
            _log.warning("vault attestation could not be checked: %s", error)
