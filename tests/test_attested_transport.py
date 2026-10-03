"""金庫への「検証してからピン留めする」transport(web.attested_transport。design.md §9、契約 research/tee-spike-contract.md §8)。

本物の TLS を通す: 契約 §11 と同じ形の自己署名の証明書で TLS を話す、金庫の偽物(tests/attestation_helpers.py の FakeVaultServer。
127.0.0.1 の空きポート)を立てる。偽物の /v1/attestation は、要求の nonce と、そのサーバの証明書の DER の SHA-256 を eat_nonce に入れた、
テスト鍵で署名したトークンを返す。GCP にも Google にも接続しない。確かめること:

- 正常系: 最初の要求で、証明書を控え、認証ヘッダなしで /v1/attestation を呼び、検証してから(ID トークンを付けた)要求を流す。以後は検証しない。
- 検証が通らないときは、要求を金庫に送らず、httpx.ConnectError(金庫のクライアントを通すと VaultUnavailableError): 証明書の結び付きが違う・nonce 違い・
  署名を 1 バイト壊した・debug・digest が許可リストにない・期限切れ。
- 金庫を別の証明書で立て直すと、接続エラーのあとに検証し直して付け替え、同じ要求を 1 回だけ送り直す(本文も同じ)。検証し直す間隔より短い連続の
  失敗では、検証し直さない。
- 金庫が /v1/attestation を一時的に断る(429: 金庫全体の最短の間隔、503: launcher の失敗)ときは、1.1 秒待って 1 回だけやり直す。
- 検証の single-flight: 同時の要求は 1 つの検証を待って共有し(金庫の /v1/attestation は 1 回)、検証が失敗すれば、待っていた全員が同じ失敗になる。
- 一定の間隔(reverify_interval_seconds)をすぎたら、接続エラーがなくても検証し直す。確定した否定(トークンが取れて、検証の結果が否定)なら
  ピンを外し、通るまで全要求が ConnectError(各要求が最短 2 秒の間隔で検証し直して、通ったら復帰する)。一時的な失敗(429・503・時間切れ・
  応答の形違い)なら、ピンを外さず、いまの接続を使い続け、要求を待たせずに、通るまで検証し直しを続ける(WARNING は理由が変わったときだけ)。
  ただし、最後に検証が通ってから max_unverified_seconds(既定 1800 秒)をこえたら、業務の要求を閉じる(ピンの証明書は残すが、データは送らない)。
  閉じている間も、最短の間隔で検証し直しを試み、通ったら復帰する。
- attest(nonce): 金庫の API が使う口。ピン留めした接続で検証して返す。失敗の理由とトークン(表示用)を例外に持つ。確定した否定なら、その場でピンを外す
  (画面が verified=false のままデータが流れ続けないように)。一時的な失敗(unavailable)では外さない。
- トークンの値は、ログにも例外の文にも出ない。
"""

import asyncio
import logging
import re
import ssl
import time

import httpx
import pytest
from attestation_helpers import (
    DIGEST,
    REJECTED_REASONS,
    attestation_payload,
    bad_token,
    certs_of,
    fake_vault,  # noqa: F401  (フィクスチャは import して使う)
    flip_signature_byte,
    make_policy,
    mint_token,
    token_with,
)
from negotiation_core.attestation import AttestationError
from web.attested_transport import AttestedVaultTransport
from web.vault_client import VaultClient, VaultNotFoundError, VaultUnavailableError

NONCE_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
ID_TOKEN = "stub-id-token-for-the-vault"


class FakeClock:
    """検証の間隔を測る時計(単調時計の代わり)。テストが進める。"""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSleep:
    """金庫が /v1/attestation を一時的に断ったときの待ち(1.1 秒)の代わり。待たずに、待とうとした秒数を記録する。"""

    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


def make_transport(vault, clock: FakeClock | None = None, sleeper: FakeSleep | None = None, **kwargs) -> AttestedVaultTransport:
    return AttestedVaultTransport(
        vault.url,
        policy=kwargs.pop("policy", make_policy()),
        certs=certs_of(),
        clock=clock if clock is not None else FakeClock(),
        sleep=sleeper if sleeper is not None else FakeSleep(),
        request_timeout_seconds=kwargs.pop("request_timeout_seconds", 5.0),
        **kwargs,
    )


def make_client(vault, transport: AttestedVaultTransport, **kwargs) -> httpx.AsyncClient:
    """金庫を呼ぶ AsyncClient(本番と同じ作り: base_url と transport。ID トークンの代わりに、固定の Authorization を付ける)。"""
    return httpx.AsyncClient(
        base_url=vault.url, transport=transport, headers={"Authorization": f"Bearer {ID_TOKEN}"}, **kwargs
    )


def paths(vault) -> list[str]:
    return [request.path for request in list(vault.requests)]


# --- 正常系 ---------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_first_request_verifies_the_vault_then_pins_the_certificate_and_later_requests_pass(fake_vault):
    transport = make_transport(fake_vault)
    async with make_client(fake_vault, transport) as client:
        first = await client.get("/v1/ping")
        second = await client.get("/v1/ping")

    assert (first.status_code, first.json()) == (200, {"ok": True})
    assert second.status_code == 200
    # 検証(認証ヘッダなしの /v1/attestation)が先で、そのあとに要求。検証は 1 回だけ。
    assert paths(fake_vault) == ["/v1/attestation", "/v1/ping", "/v1/ping"]
    attestation, *requests = fake_vault.requests
    assert NONCE_FORMAT.fullmatch(attestation.query["nonce"][0])
    assert "authorization" not in attestation.headers  # まだ信用していない相手に、ID トークンを渡さない
    assert all(request.headers["authorization"] == f"Bearer {ID_TOKEN}" for request in requests)
    verified = transport.last_verification
    assert verified is not None and verified.image_digest == DIGEST
    assert verified.nonces == (attestation.query["nonce"][0], fake_vault.material.certificate_sha256)
    assert transport.last_verified_at is not None and abs(transport.last_verified_at - time.time()) < 30
    assert transport.certificate_sha256 == fake_vault.material.certificate_sha256


@pytest.mark.anyio
async def test_the_id_token_is_attached_to_the_vault_requests_but_never_to_the_attestation_request(fake_vault):
    from web.service_auth import IdTokenAuth

    class StubProvider:
        def __init__(self) -> None:
            self.audiences: list[str] = []

        async def token(self, audience: str) -> str:
            self.audiences.append(audience)
            return ID_TOKEN

        def invalidate(self, audience, token=None) -> None:
            pass

    provider = StubProvider()
    auth = IdTokenAuth(provider, fake_vault.url, audience="https://vault.anon-nego.internal")  # type: ignore[arg-type]
    transport = make_transport(fake_vault)
    async with httpx.AsyncClient(base_url=fake_vault.url, transport=transport, auth=auth) as client:
        response = await client.get("/v1/ping")

    assert response.status_code == 200
    attestation, ping = fake_vault.requests
    assert "authorization" not in attestation.headers
    assert ping.headers["authorization"] == f"Bearer {ID_TOKEN}"
    assert provider.audiences == ["https://vault.anon-nego.internal"]  # URL ではなく、固定の audience


@pytest.mark.anyio
async def test_the_vault_client_works_through_the_transport(fake_vault):
    # 金庫のクライアント(web.vault_client)は変えない。ID トークンを付けた要求が、金庫(偽物)の認可まで届く(404 = 認可を通った)。
    transport = make_transport(fake_vault)
    async with make_client(fake_vault, transport) as http:
        with pytest.raises(VaultNotFoundError):
            await VaultClient(http).get_policy("0000000000000000")


# --- 検証が通らないとき ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("reason", REJECTED_REASONS)
async def test_a_vault_that_fails_the_attestation_is_refused_as_a_connection_error_and_gets_no_request(fake_vault, reason):
    fake_vault.token_factory = bad_token(reason)
    transport = make_transport(fake_vault)
    async with make_client(fake_vault, transport) as client:
        with pytest.raises(httpx.ConnectError, match=f"vault attestation failed: {reason}$") as excinfo:
            await client.get("/v1/ping")

    assert isinstance(excinfo.value.__cause__, AttestationError) and excinfo.value.__cause__.reason == reason
    assert paths(fake_vault) == ["/v1/attestation"]  # 要求(ID トークンつき)は、金庫に送られていない
    assert transport.last_verification is None


@pytest.mark.anyio
async def test_the_vault_client_turns_a_failed_attestation_into_vault_unavailable(fake_vault):
    # 金庫のクライアントは、通信エラーを「一時的に応えない」にする。レフェリーは待ってやり直す。
    fake_vault.token_factory = bad_token("debug")
    async with make_client(fake_vault, make_transport(fake_vault)) as http:
        with pytest.raises(VaultUnavailableError):
            await VaultClient(http).get_policy("0000000000000000")


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 503, 500])
async def test_an_attestation_endpoint_that_does_not_answer_with_a_token_is_unavailable(fake_vault, status):
    fake_vault.attestation_status = status
    transport = make_transport(fake_vault)

    with pytest.raises(AttestationError) as excinfo:
        await transport.attest("a" * 43)

    assert excinfo.value.reason == "unavailable" and excinfo.value.token is None
    async with make_client(fake_vault, transport) as client:
        with pytest.raises(httpx.ConnectError, match="unavailable"):
            await client.get("/v1/ping")


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 503])
async def test_a_temporary_refusal_of_the_vault_is_retried_once_after_a_pause_and_then_passes(fake_vault, status):
    # 金庫の /v1/attestation の最短の間隔(429)や launcher の失敗(503)は、一時的な失敗。1.1 秒待って 1 回だけやり直す。
    fake_vault.attestation_script = [status]  # 最初の 1 回だけ断り、次は 200
    sleeper = FakeSleep()
    transport = make_transport(fake_vault, sleeper=sleeper)
    async with make_client(fake_vault, transport) as client:
        response = await client.get("/v1/ping")

    assert response.status_code == 200
    assert sleeper.durations == [1.1]
    first, second = fake_vault.attestation_requests
    assert first.query["nonce"] == second.query["nonce"]  # 同じ nonce でやり直す(断られただけで、トークンは発行されていない)
    assert transport.last_verification is not None


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 503])
async def test_a_refusal_that_continues_is_unavailable_after_exactly_one_retry(fake_vault, status):
    fake_vault.attestation_status = status
    sleeper = FakeSleep()
    transport = make_transport(fake_vault, sleeper=sleeper)
    async with make_client(fake_vault, transport) as client:
        with pytest.raises(httpx.ConnectError, match="vault attestation failed: unavailable"):
            await client.get("/v1/ping")

    assert sleeper.durations == [1.1] and len(fake_vault.attestation_requests) == 2
    assert "/v1/ping" not in paths(fake_vault)  # 検証が通らない金庫には、要求を送らない


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 404, 500, 502])
async def test_other_refusals_are_not_retried(fake_vault, status):
    fake_vault.attestation_status = status
    sleeper = FakeSleep()
    transport = make_transport(fake_vault, sleeper=sleeper)

    with pytest.raises(AttestationError) as excinfo:
        await transport.attest("a" * 43)

    assert excinfo.value.reason == "unavailable" and sleeper.durations == [] and len(fake_vault.attestation_requests) == 1


@pytest.mark.anyio
async def test_attest_on_the_pinned_connection_also_retries_a_temporary_refusal_once(fake_vault):
    # 金庫の API(GET /api/tee/attestation)の検証が、web の検証し直しや検証スクリプトと重なって 429 になっても、1 回だけやり直す。
    sleeper = FakeSleep()
    transport = make_transport(fake_vault, sleeper=sleeper)
    await transport.attest("a" * 43)  # ピン留め
    fake_vault.attestation_script = [429]

    _token, verified = await transport.attest("b" * 43)

    assert verified.nonces[0] == "b" * 43 and sleeper.durations == [1.1]
    assert [r.query["nonce"][0] for r in fake_vault.attestation_requests] == ["a" * 43, "b" * 43, "b" * 43]


@pytest.mark.anyio
async def test_concurrent_requests_share_the_one_retry_of_a_temporary_refusal(fake_vault):
    fake_vault.attestation_script = [429]
    sleeper = FakeSleep()
    transport = make_transport(fake_vault, sleeper=sleeper)
    async with make_client(fake_vault, transport) as client:
        responses = await asyncio.gather(*(client.get("/v1/ping") for _ in range(5)))

    assert [response.status_code for response in responses] == [200] * 5
    assert len(fake_vault.attestation_requests) == 2 and sleeper.durations == [1.1]  # 検証は 1 つ。やり直しも 1 回


@pytest.mark.anyio
async def test_a_huge_response_is_unavailable_without_being_parsed(fake_vault):
    # 検証前の(信用していない相手の)応答の大きさを抑える。上限(64 KiB)をこえたら、読むのをやめて unavailable。
    fake_vault.token_factory = lambda nonce, cert: "a" * 100_000
    transport = make_transport(fake_vault)

    with pytest.raises(AttestationError) as excinfo:
        await transport.attest("a" * 43)

    assert excinfo.value.reason == "unavailable" and excinfo.value.token is None


@pytest.mark.anyio
async def test_a_vault_that_cannot_be_reached_is_a_connection_error_and_recovers_when_it_comes_back(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    fake_vault.stop()
    async with make_client(fake_vault, transport) as client:
        with pytest.raises(httpx.ConnectError, match="could not get the vault certificate"):
            await client.get("/v1/ping")
        fake_vault.restart()
        clock.advance(3)

        assert (await client.get("/v1/ping")).status_code == 200


@pytest.mark.parametrize("url", ["http://127.0.0.1:8443", "127.0.0.1:8443", "https://", "ftp://127.0.0.1"])
def test_the_vault_url_must_be_https(url):
    with pytest.raises((ValueError, httpx.InvalidURL)):
        AttestedVaultTransport(url, policy=make_policy(), certs=certs_of())


@pytest.mark.anyio
async def test_a_request_to_another_destination_is_never_sent(fake_vault):
    # ID トークンを、別の宛先に送らない(この transport は、検証した金庫への要求だけを流す)。
    transport = make_transport(fake_vault)

    for url in ("https://other.example/v1/ping", f"http://127.0.0.1:{fake_vault.port}/v1/ping", "https://127.0.0.1:1/v1/ping"):
        with pytest.raises(ValueError, match="only sends requests to the attested vault"):
            await transport.handle_async_request(httpx.Request("GET", url))

    assert paths(fake_vault) == []


# --- 金庫の再起動(証明書が変わる) -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_after_the_vault_restarts_with_another_certificate_the_transport_verifies_again_and_resends_once(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.post("/v1/echo", json={"n": 0})
        old_hash = transport.certificate_sha256
        new_material = fake_vault.restart()  # 別の証明書で立て直す(金庫の再起動)
        clock.advance(3)  # 最後の検証から、検証し直す間隔(2 秒)がすぎた

        response = await client.post("/v1/echo", json={"n": 1})

    assert response.json() == {"received": {"n": 1}}  # 送り直した要求の本文も、同じ
    assert paths(fake_vault) == ["/v1/attestation", "/v1/echo", "/v1/attestation", "/v1/echo"]
    assert old_hash != new_material.certificate_sha256 == transport.certificate_sha256
    assert transport.last_verification.nonces[1] == new_material.certificate_sha256  # 新しい証明書で検証した
    assert all("authorization" not in r.headers for r in fake_vault.attestation_requests)


@pytest.mark.anyio
async def test_a_failure_shorter_than_the_reverify_interval_after_the_last_verification_does_not_verify_again(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.restart()  # 証明書が変わった。でも、最後の検証から 2 秒たっていない

        for _ in range(3):  # 連続の失敗でも、検証し直さない(金庫と Google の呼び出しを守る)
            with pytest.raises(httpx.ConnectError):
                await client.get("/v1/ping")
        assert paths(fake_vault) == ["/v1/attestation", "/v1/ping"]  # 再起動のあとの /v1/attestation は 0 回

        clock.advance(2.5)
        assert (await client.get("/v1/ping")).status_code == 200  # 間隔をすぎたら、検証し直して復帰する

    assert paths(fake_vault).count("/v1/attestation") == 2


@pytest.mark.anyio
@pytest.mark.parametrize("error", [httpx.RemoteProtocolError("server disconnected"), ssl.SSLError("handshake failure")])
async def test_remote_protocol_errors_and_ssl_errors_also_verify_again_and_resend_once(fake_vault, error):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        pinned_transport = transport._pinned.transport
        original, failures = pinned_transport.handle_async_request, [error]

        async def flaky(request):
            if failures:
                raise failures.pop()
            return await original(request)

        pinned_transport.handle_async_request = flaky
        clock.advance(3)

        assert (await client.get("/v1/ping")).status_code == 200

    assert paths(fake_vault) == ["/v1/attestation", "/v1/ping", "/v1/attestation", "/v1/ping"]


@pytest.mark.anyio
async def test_other_errors_are_not_retried_and_do_not_verify_again(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")

        async def slow(request):
            raise httpx.ReadTimeout("timed out")

        transport._pinned.transport.handle_async_request = slow
        clock.advance(3)

        with pytest.raises(httpx.ReadTimeout):
            await client.get("/v1/ping")

    assert paths(fake_vault) == ["/v1/attestation", "/v1/ping"]


@pytest.mark.anyio
async def test_the_resend_is_made_only_once(fake_vault):
    # 検証し直しが通っても、送り直した要求がまた接続エラーなら、それ以上は送り直さない。
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        calls = []

        async def always_broken(request):
            calls.append(request)
            raise httpx.ConnectError("broken")

        transport._pinned.transport.handle_async_request = always_broken
        clock.advance(3)

        with pytest.raises(httpx.ConnectError, match="broken"):
            await client.get("/v1/ping")

    assert len(calls) == 2  # 最初の 1 回と、検証し直したあとの 1 回(検証し直しでは、同じ証明書なので、今の接続のまま)


# --- single-flight ----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_requests_share_one_verification(fake_vault):
    transport = make_transport(fake_vault)
    async with make_client(fake_vault, transport) as client:
        responses = await asyncio.gather(*(client.get("/v1/ping") for _ in range(5)))

    assert [response.status_code for response in responses] == [200] * 5
    assert paths(fake_vault).count("/v1/attestation") == 1  # 5 つの要求が、それぞれ金庫の /v1/attestation を呼ばない
    assert paths(fake_vault).count("/v1/ping") == 5


@pytest.mark.anyio
async def test_a_failed_verification_fails_every_waiting_request_and_the_next_attempt_waits_for_the_interval(fake_vault):
    clock = FakeClock()
    fake_vault.token_factory = bad_token("debug")
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        results = await asyncio.gather(*(client.get("/v1/ping") for _ in range(5)), return_exceptions=True)

        assert all(isinstance(result, httpx.ConnectError) and "debug" in str(result) for result in results)
        assert paths(fake_vault) == ["/v1/attestation"]  # 検証は 1 回。待っていた 5 つの要求が、同じ失敗になった

        with pytest.raises(httpx.ConnectError, match="debug"):  # 間隔のうちは、検証し直さず、その失敗を返す
            await client.get("/v1/ping")
        assert paths(fake_vault) == ["/v1/attestation"]

        clock.advance(2.5)
        fake_vault.token_factory = token_with()  # 直った
        assert (await client.get("/v1/ping")).status_code == 200

    assert paths(fake_vault) == ["/v1/attestation", "/v1/attestation", "/v1/ping"]


@pytest.mark.anyio
async def test_requests_that_fail_together_after_a_restart_share_one_verification(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.restart()
        clock.advance(3)

        responses = await asyncio.gather(*(client.get("/v1/ping") for _ in range(5)))

    assert [response.status_code for response in responses] == [200] * 5
    assert paths(fake_vault).count("/v1/attestation") == 2  # 最初と、再起動のあとの 1 回


@pytest.mark.anyio
async def test_a_cancelled_request_does_not_cancel_the_verification_the_others_wait_for(fake_vault):
    transport = make_transport(fake_vault)
    async with make_client(fake_vault, transport) as client:
        first = asyncio.ensure_future(client.get("/v1/ping"))
        await asyncio.sleep(0)  # first が検証を始める
        others = [asyncio.ensure_future(client.get("/v1/ping")) for _ in range(2)]
        await asyncio.sleep(0)
        first.cancel()

        responses = await asyncio.gather(*others)

    assert [response.status_code for response in responses] == [200, 200]
    assert paths(fake_vault).count("/v1/attestation") == 1


# --- 一定の間隔での検証し直し -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_certificate_is_verified_again_after_the_reverify_interval_even_without_an_error(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        clock.advance(599)
        await client.get("/v1/ping")
        assert paths(fake_vault).count("/v1/attestation") == 1  # 間隔の前: 検証し直さない

        clock.advance(2)  # 最初の検証から 601 秒
        responses = await asyncio.gather(*(client.get("/v1/ping") for _ in range(5)))
        assert [response.status_code for response in responses] == [200] * 5
        assert paths(fake_vault).count("/v1/attestation") == 2  # 間隔をすぎた最初の要求が、1 回だけ検証し直した(single-flight)

        clock.advance(100)
        await client.get("/v1/ping")
        assert paths(fake_vault).count("/v1/attestation") == 2  # 検証し直した時刻から数え直す


@pytest.mark.anyio
async def test_a_periodic_verification_with_the_same_certificate_keeps_the_connection_in_use(fake_vault):
    # 通信中の要求を切らない: 証明書が同じなら、今の接続をそのまま使い続ける(検証に使った接続は閉じる)。
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        first = transport._pinned
        in_flight = asyncio.ensure_future(client.get("/v1/slow"))  # 古いピンで通信中の要求
        await asyncio.sleep(0.1)
        clock.advance(601)

        assert (await client.get("/v1/ping")).status_code == 200  # この要求が、検証し直す
        assert (await in_flight).status_code == 200  # 通信中の要求は、切れずに終わる

        assert transport._pinned is first and first.verified_at == clock()
        assert paths(fake_vault).count("/v1/attestation") == 2


@pytest.mark.anyio
async def test_a_periodic_verification_that_sees_another_certificate_replaces_the_connection(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        first = transport._pinned
        fake_vault.restart()
        clock.advance(601)

        assert (await client.get("/v1/ping")).status_code == 200

        assert transport._pinned is not first
        assert transport.certificate_sha256 == fake_vault.material.certificate_sha256


@pytest.mark.anyio
async def test_the_default_reverify_interval_is_600_seconds(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        clock.advance(599)
        await client.get("/v1/ping")
        assert paths(fake_vault).count("/v1/attestation") == 1
        clock.advance(2)
        await client.get("/v1/ping")

    assert paths(fake_vault).count("/v1/attestation") == 2


@pytest.mark.anyio
async def test_when_the_periodic_verification_fails_the_pin_is_dropped_and_every_request_fails_until_it_passes(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.token_factory = token_with(swname="GCE")  # イメージが失効すると swname が GCE になる(接続は生きている)
        clock.advance(601)

        for _ in range(3):
            with pytest.raises(httpx.ConnectError, match="swname"):
                await client.get("/v1/ping")
        assert transport._pinned is None  # ピンを外した
        assert paths(fake_vault) == ["/v1/attestation", "/v1/ping", "/v1/attestation"]  # 失敗のあとの要求は、金庫に届かない

        clock.advance(2.5)
        with pytest.raises(httpx.ConnectError, match="swname"):  # 通るまで、間隔を置いて検証し直し、そのたびに失敗する
            await client.get("/v1/ping")
        assert paths(fake_vault).count("/v1/attestation") == 3 and paths(fake_vault).count("/v1/ping") == 1

        fake_vault.token_factory = token_with()  # 直った
        clock.advance(2.5)
        assert (await client.get("/v1/ping")).status_code == 200
        assert transport._pinned is not None


@pytest.mark.anyio
async def test_a_debug_token_after_the_interval_stops_the_data_and_a_normal_one_keeps_it_flowing(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        clock.advance(601)
        await client.get("/v1/ping")  # 正常: 続けて成功し、検証の呼び出しが 1 回増える
        assert paths(fake_vault).count("/v1/attestation") == 2

        fake_vault.token_factory = bad_token("debug")
        clock.advance(601)
        with pytest.raises(httpx.ConnectError, match="debug"):
            await client.post("/v1/echo", json={"secret": "must not be sent"})

    assert "/v1/echo" not in paths(fake_vault)


# --- attest(nonce): 金庫の API が使う口 --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_attest_pins_the_certificate_when_it_is_the_first_call_and_uses_a_single_attestation_call(fake_vault):
    transport = make_transport(fake_vault)
    nonce = "a" * 43

    token, verified = await transport.attest(nonce)

    assert verified.image_digest == DIGEST and verified.nonces[0] == nonce
    assert token.count(".") == 2
    assert [r.query["nonce"] for r in fake_vault.attestation_requests] == [[nonce]]  # 呼び出しは 1 回(ピン留めと、この nonce の検証を兼ねる)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")  # ピン留め済み: 検証し直さない
    assert paths(fake_vault).count("/v1/attestation") == 1
    assert transport.certificate_sha256 == fake_vault.material.certificate_sha256


@pytest.mark.anyio
async def test_attest_on_a_pinned_transport_calls_the_vault_once_with_the_given_nonce_and_updates_the_record(fake_vault):
    transport = make_transport(fake_vault)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
    first_at = transport.last_verified_at
    time.sleep(0.01)

    _token, verified = await transport.attest("b" * 43)

    assert verified.nonces[0] == "b" * 43
    assert [r.query["nonce"][0] for r in fake_vault.attestation_requests][-1] == "b" * 43
    assert len(fake_vault.attestation_requests) == 2
    assert all("authorization" not in r.headers for r in fake_vault.attestation_requests)
    assert transport.last_verified_at > first_at and transport.last_verification is verified


@pytest.mark.anyio
@pytest.mark.parametrize("reason", REJECTED_REASONS)
async def test_attest_raises_the_reason_with_the_token_for_display(fake_vault, reason):
    fake_vault.token_factory = bad_token(reason)
    transport = make_transport(fake_vault)

    with pytest.raises(AttestationError) as excinfo:
        await transport.attest("c" * 43)

    assert excinfo.value.reason == reason
    assert excinfo.value.token is not None and excinfo.value.token.count(".") == 2
    assert excinfo.value.token not in str(excinfo.value)


@pytest.mark.anyio
async def test_attest_after_a_restart_verifies_again_with_the_same_nonce_in_one_call(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    await transport.attest("d" * 43)
    fake_vault.restart()
    clock.advance(3)
    before = len(fake_vault.attestation_requests)

    _token, verified = await transport.attest("e" * 43)

    new_requests = fake_vault.attestation_requests[before:]
    assert [r.query["nonce"][0] for r in new_requests] == ["e" * 43]  # 再起動のあとの検証が、この nonce の検証を兼ねる(1 回)
    assert verified.nonces == ("e" * 43, fake_vault.material.certificate_sha256)


@pytest.mark.anyio
async def test_attest_raises_a_connection_error_when_the_vault_cannot_be_reached(fake_vault):
    transport = make_transport(fake_vault)
    fake_vault.stop()

    with pytest.raises(httpx.ConnectError):
        await transport.attest("f" * 43)


# --- トークンの値を出さない -----------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_token_is_never_logged_or_put_in_an_error_message(fake_vault, caplog):
    caplog.set_level(logging.DEBUG)
    captured: list[str] = []

    def spying_factory(nonce: str, cert: str) -> str:
        token = flip_signature_byte(mint_token(attestation_payload([nonce, cert], now=time.time())))
        captured.append(token)
        return token

    fake_vault.token_factory = spying_factory
    transport = make_transport(fake_vault)
    async with make_client(fake_vault, transport) as client:
        with pytest.raises(httpx.ConnectError) as excinfo:
            await client.get("/v1/ping")
    with pytest.raises(AttestationError) as attest_error:
        await transport.attest("g" * 43)

    (token, *_) = captured
    payload_part = token.split(".")[1]
    messages = [str(excinfo.value), str(excinfo.value.__cause__), str(attest_error.value), repr(attest_error.value)]
    assert all(token not in message and payload_part not in message for message in messages)
    assert token not in caplog.text and payload_part not in caplog.text
    assert "vault attestation failed reason=signature" in caplog.text  # 理由は残す


# --- 一時的な失敗と、確定した否定(C-61) ------------------------------------------------------------------------
#
# 確定した否定: トークンが取れて、検証の結果が否定(reason が unavailable 以外)→ ピンを外し、通るまで全要求が ConnectError。
#   外したあとは、各要求が最短 2 秒の間隔で検証し直しを試み、通ったら復帰する。
# 一時的な失敗: 429・503・接続エラー・時間切れ・応答の形違い(reason は unavailable、または ConnectError)→ ピンを外さず、いまの接続を使い続け、
#   次の要求が(single-flight で、最短 2 秒の間隔で)、通るまで検証し直しを続ける。

TEMPORARY_FAILURES = ["status_503", "timeout", "bad_json", "no_token_key"]


def break_attestation(vault, kind: str) -> None:
    """金庫の /v1/attestation だけを、一時的に失敗させる(データの口は動いたまま)。"""
    if kind == "status_503":
        vault.attestation_status = 503
    elif kind == "timeout":
        vault.attestation_delay = 1.0  # transport の時間切れ(0.3 秒)より長い
    elif kind == "bad_json":
        vault.attestation_raw = b"<html>not json</html>"
    elif kind == "no_token_key":
        vault.attestation_raw = b'{"detail": "there is no token here"}'
    else:
        raise AssertionError(kind)


def repair_attestation(vault) -> None:
    vault.attestation_status, vault.attestation_delay, vault.attestation_raw = 200, 0.0, None


async def settle(transport: AttestedVaultTransport) -> None:
    """要求を待たせずに始まった検証し直しが、終わるまで待つ。"""
    for _ in range(500):
        if transport._flight is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the verification did not finish")


@pytest.mark.anyio
@pytest.mark.parametrize("kind", TEMPORARY_FAILURES)
async def test_a_temporary_failure_of_the_periodic_verification_keeps_the_pin_and_the_retries_go_on_without_a_limit(
    fake_vault, kind
):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600, request_timeout_seconds=0.3)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        pinned = transport._pinned
        break_attestation(fake_vault, kind)
        clock.advance(601)

        assert (await client.get("/v1/ping")).status_code == 200  # 検証し直しが一時的に失敗しても、いまの接続で通る
        assert transport._pinned is pinned and pinned.retrying
        attempts = len(fake_vault.attestation_requests)
        assert attempts >= 2
        for _ in range(3):  # 最短の間隔(2 秒)のうちは、検証し直さず、そのまま通る
            assert (await client.get("/v1/ping")).status_code == 200
        assert len(fake_vault.attestation_requests) == attempts

        for _ in range(3):  # 間隔をすぎるたびに、次の要求が検証し直す。回数の上限はない
            clock.advance(2.5)
            assert (await client.get("/v1/ping")).status_code == 200
            await settle(transport)
            assert len(fake_vault.attestation_requests) > attempts
            attempts = len(fake_vault.attestation_requests)
        assert transport._pinned is pinned and pinned.retrying

        repair_attestation(fake_vault)
        clock.advance(2.5)
        assert (await client.get("/v1/ping")).status_code == 200
        await settle(transport)
        assert not pinned.retrying and pinned.verified_at == clock()  # 通った: 検証し直した時刻から数え直す
        attempts = len(fake_vault.attestation_requests)
        clock.advance(100)
        await client.get("/v1/ping")
        assert len(fake_vault.attestation_requests) == attempts  # もう検証し直さない


@pytest.mark.anyio
async def test_while_the_verification_is_being_retried_the_requests_do_not_wait_for_it(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.attestation_status = 503
        clock.advance(601)
        await client.get("/v1/ping")  # 最初の検証し直し: 要求が結果を待つ(否定なら、要求を送らないため)。一時的に失敗した
        assert transport._pinned.retrying

        repair_attestation(fake_vault)
        fake_vault.attestation_delay = 0.5  # 検証が遅い
        clock.advance(2.5)
        assert (await client.get("/v1/ping")).status_code == 200
        assert transport._flight is not None  # 検証を待たずに通った。検証し直しは、うしろで動いている

        await settle(transport)
        assert not transport._pinned.retrying


@pytest.mark.anyio
async def test_concurrent_requests_during_the_retries_share_one_verification_and_none_of_them_waits(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.attestation_status = 503
        clock.advance(601)
        await client.get("/v1/ping")
        before = len(fake_vault.attestation_requests)
        clock.advance(2.5)

        responses = await asyncio.gather(*(client.get("/v1/ping") for _ in range(5)))
        await settle(transport)

    assert [response.status_code for response in responses] == [200] * 5
    assert len(fake_vault.attestation_requests) - before == 2  # 検証し直しは 1 回(503 と、そのやり直し)


@pytest.mark.anyio
@pytest.mark.parametrize("reason", REJECTED_REASONS)
async def test_a_definite_negative_result_of_the_periodic_verification_drops_the_pin_and_the_requests_recover_when_it_passes(
    fake_vault, reason
):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.token_factory = bad_token(reason)
        clock.advance(601)

        with pytest.raises(httpx.ConnectError, match=f"vault attestation failed: {reason}$"):
            await client.get("/v1/ping")  # 否定の結果を知ってから、要求を送らない
        assert transport._pinned is None
        attempts = len(fake_vault.attestation_requests)
        for _ in range(3):  # 最短の間隔のうちは、金庫に何も送らず、同じ失敗
            with pytest.raises(httpx.ConnectError, match=reason):
                await client.get("/v1/ping")
        assert len(fake_vault.attestation_requests) == attempts and paths(fake_vault).count("/v1/ping") == 1

        clock.advance(2.5)  # 「次の 10 分」を待たない: 間隔をすぎた次の要求が、検証し直す。まだ否定なら、また失敗
        with pytest.raises(httpx.ConnectError, match=reason):
            await client.get("/v1/ping")
        assert len(fake_vault.attestation_requests) == attempts + 1

        fake_vault.token_factory = token_with()  # 直った
        clock.advance(2.5)
        assert (await client.get("/v1/ping")).status_code == 200
        assert transport._pinned is not None and len(fake_vault.attestation_requests) == attempts + 2


@pytest.mark.anyio
@pytest.mark.parametrize("reason", REJECTED_REASONS)
async def test_a_definite_negative_result_from_attest_drops_the_pin_on_the_spot(fake_vault, reason):
    # 画面(GET /api/tee/attestation)に verified=false を返すときは、データも止める。
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.token_factory = bad_token(reason)

        with pytest.raises(AttestationError) as excinfo:
            await transport.attest("a" * 43)
        assert excinfo.value.reason == reason and transport._pinned is None

        with pytest.raises(httpx.ConnectError, match=reason):  # すぐに止まる(定期の検証し直しを待たない)
            await client.get("/v1/ping")
        attempts = len(fake_vault.attestation_requests)
        with pytest.raises(httpx.ConnectError, match=reason):  # 最短の間隔のうちは、金庫に何も送らない
            await client.get("/v1/ping")
        assert len(fake_vault.attestation_requests) == attempts and paths(fake_vault).count("/v1/ping") == 1

        fake_vault.token_factory = token_with()  # 直った
        clock.advance(2.5)
        assert (await client.get("/v1/ping")).status_code == 200  # 最短 2 秒の間隔で検証し直して、復帰する
        assert transport._pinned is not None and len(fake_vault.attestation_requests) == attempts + 1


@pytest.mark.anyio
@pytest.mark.parametrize("kind", TEMPORARY_FAILURES)
async def test_a_temporary_failure_from_attest_does_not_drop_the_pin(fake_vault, kind):
    transport = make_transport(fake_vault, request_timeout_seconds=0.3)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        pinned = transport._pinned
        break_attestation(fake_vault, kind)

        with pytest.raises((AttestationError, httpx.HTTPError)) as excinfo:
            await transport.attest("a" * 43)
        if isinstance(excinfo.value, AttestationError):
            assert excinfo.value.reason == "unavailable"

        assert transport._pinned is pinned
        assert (await client.get("/v1/ping")).status_code == 200  # 要求は、そのまま通る
        assert paths(fake_vault).count("/v1/ping") == 2


@pytest.mark.anyio
async def test_a_connection_error_followed_by_a_temporary_failure_of_the_verification_keeps_the_pin(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        pinned = transport._pinned
        original, failures = pinned.transport.handle_async_request, [httpx.ConnectError("a blip in the network")]

        async def blip(request):
            if failures:
                raise failures.pop()
            return await original(request)

        pinned.transport.handle_async_request = blip
        fake_vault.attestation_status = 503
        clock.advance(3)

        with pytest.raises(httpx.ConnectError):  # 接続が壊れ、検証し直しも一時的に失敗した: この要求だけが失敗する
            await client.get("/v1/ping")
        assert transport._pinned is pinned  # ピンは残るので、金庫が戻れば(同じ証明書なら)検証し直さずに通る
        assert (await client.get("/v1/ping")).status_code == 200


@pytest.mark.anyio
async def test_the_warning_is_logged_once_for_each_change_of_the_reason_not_for_each_retry(fake_vault, caplog):
    caplog.set_level(logging.WARNING, logger="web.attested_transport")
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600, request_timeout_seconds=0.3)

    def warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "web.attested_transport" and r.levelno == logging.WARNING]

    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        assert warnings() == []

        break_attestation(fake_vault, "status_503")
        clock.advance(601)
        for _ in range(4):  # 検証し直しが 4 回、同じ理由で失敗する
            await client.get("/v1/ping")
            await settle(transport)
            clock.advance(2.5)
        assert warnings() == ["vault attestation failed reason=unavailable"]

        repair_attestation(fake_vault)
        break_attestation(fake_vault, "timeout")
        for _ in range(3):  # 理由が変わった(時間切れ): 1 行だけ増える
            await client.get("/v1/ping")
            await settle(transport)
            clock.advance(2.5)
        assert len(warnings()) == 2 and "ReadTimeout" in warnings()[1]

        repair_attestation(fake_vault)
        await client.get("/v1/ping")  # 通った
        await settle(transport)
        break_attestation(fake_vault, "timeout")  # 通ったあとに、直前に書いたのと同じ理由で失敗する
        clock.advance(601)
        await client.get("/v1/ping")
        await settle(transport)
        assert len(warnings()) == 3 and warnings()[2] == warnings()[1]  # 通ったあとの失敗は、同じ理由でも、また 1 行書く

    assert all("eyJ" not in message for message in warnings())  # トークンの値は書かない


# --- 検証が通らないまま長くたったら、業務の要求を閉じる(X-77) -----------------------------------------------------


@pytest.mark.anyio
async def test_requests_are_closed_when_the_attestation_has_not_passed_for_too_long_and_recover_when_it_passes(
    fake_vault, caplog
):
    # 一時的な失敗(503)のあいだは、ピンを保持して要求を通す。でも、最後に検証が通ってから 1800 秒(既定)をこえたら、データを送らない。
    caplog.set_level(logging.WARNING, logger="web.attested_transport")
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")  # 最後に検証が通った時刻(clock = 1000)
        pinned = transport._pinned
        fake_vault.attestation_status = 503
        clock.advance(601)
        assert (await client.get("/v1/ping")).status_code == 200  # 一時的な失敗: ピンを保持して通す
        clock.advance(1100)  # 最後に検証が通ってから 1701 秒
        assert (await client.get("/v1/ping")).status_code == 200  # まだ通す(うしろで検証し直しを続けている)
        await settle(transport)

        clock.advance(100)  # 1801 秒: 上限をこえた
        pings = paths(fake_vault).count("/v1/ping")
        with pytest.raises(httpx.ConnectError):
            await client.get("/v1/ping")  # 業務の要求を閉じる(ピンの証明書は保持するが、データは送らない)
        assert transport._pinned is pinned and paths(fake_vault).count("/v1/ping") == pings
        attempts = len(fake_vault.attestation_requests)
        with pytest.raises(httpx.ConnectError):  # 閉じている間も、直前の失敗から最短の間隔のうちは、金庫に何も送らない
            await client.post("/v1/echo", json={"secret": "must not be sent"})
        assert len(fake_vault.attestation_requests) == attempts and "/v1/echo" not in paths(fake_vault)

        clock.advance(2.5)  # 閉じている間も、最短の間隔で検証し直しを試みる
        with pytest.raises(httpx.ConnectError):
            await client.get("/v1/ping")
        assert len(fake_vault.attestation_requests) == attempts + 2  # 503 と、そのやり直し

        fake_vault.attestation_status = 200  # 200 に戻る
        clock.advance(2.5)
        assert (await client.get("/v1/ping")).status_code == 200  # 検証が通った要求から、復帰する
        assert transport._pinned is pinned and not pinned.retrying
        assert (await client.get("/v1/ping")).status_code == 200

        fake_vault.attestation_status = 503  # 復帰したあとに、また通らなくなる
        clock.advance(601)
        assert (await client.get("/v1/ping")).status_code == 200
        clock.advance(1801)
        with pytest.raises(httpx.ConnectError):
            await client.get("/v1/ping")

    closing = [r.getMessage() for r in caplog.records if "requests are closed" in r.getMessage()]
    message = "vault requests are closed: the attestation has not passed for more than 1800.0 seconds"
    assert closing == [message, message]  # 閉じ始めるたびに 1 行だけ(閉じている間は、書き足さない)


@pytest.mark.anyio
async def test_the_longest_time_without_a_verification_can_be_set_and_a_passing_attest_reopens_the_requests(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=60, max_unverified_seconds=120)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        fake_vault.attestation_status = 503
        clock.advance(61)
        assert (await client.get("/v1/ping")).status_code == 200
        clock.advance(60)  # 最後に検証が通ってから 121 秒
        with pytest.raises(httpx.ConnectError):
            await client.get("/v1/ping")

        fake_vault.attestation_status = 200
        clock.advance(2.5)
        await transport.attest("a" * 43)  # 画面の検証が通った(最後に検証が通った時刻が新しくなる)
        assert (await client.get("/v1/ping")).status_code == 200
        await settle(transport)


@pytest.mark.anyio
async def test_a_verification_is_made_when_the_longest_time_without_one_is_reached_even_before_the_regular_interval(fake_vault):
    clock = FakeClock()
    transport = make_transport(fake_vault, clock, reverify_interval_seconds=600, max_unverified_seconds=100)
    async with make_client(fake_vault, transport) as client:
        await client.get("/v1/ping")
        clock.advance(99)
        await client.get("/v1/ping")
        assert paths(fake_vault).count("/v1/attestation") == 1

        clock.advance(2)  # 最後に検証が通ってから 101 秒: 定期の検証(600 秒)の前でも、データを送る前に検証し直す
        assert (await client.get("/v1/ping")).status_code == 200

    assert paths(fake_vault).count("/v1/attestation") == 2
