"""web の TEE モード(design.md §9、契約 research/tee-spike-contract.md §7・§8): GET /api/tee/attestation と、起動口(create_app_from_env)の組み立て。

金庫の attestation の検証は、偽の transport(attest() を差し替えられる)で行う。本物の TLS を通した検証は tests/test_attested_transport.py。
GCP にも Google にも接続しない(トークンは、手元の鍵で署名したもの)。確かめること:

- TEE モードでなければ、このルートはなく 404。
- nonce あり(契約 §2 の形。違えば 400): その nonce で金庫に確かめさせる。クライアント IP(X-Forwarded-For の最後の要素。web/client_ip.py)ごとに
  前回の転送から 10 秒未満、または、全体の前回の転送から 2 秒未満なら 429(429 は転送に数えない。契約 §19・台帳 L18-5)。金庫の発行枠
  (毎秒 1 回)を匿名の利用者が使い切れないように、かつ、匿名の 1 人が枠を独占して審査員の確認(AC-23 の --web)を 429 にしないように。
- nonce なし: 直近 5 分の結果を返す。なければ新しい nonce で 1 回だけ検証する(同時の要求は 1 回にまとめる)。金庫に届かなかった結果は覚えない。
- 応答の形(契約 §8 の JSON のキーがすべてある。token を含む)。失敗のときも、取れた範囲の claims を返す。
- release の照合: digest が表にあれば commit・url・built_at・status。表にない・失効(revoked)した digest は、検証が通っていても
  verified=false(reason=image_digest)。失効は release.status で分かる。署名が確かめられなかったトークンの claims からは、release を引かない。
- 起動口: VAULT_TEE の値、必須の環境変数、digest の表のファイル、https、ID トークンの固定の audience、active の digest だけを許可リストに入れること。
"""

import asyncio
import datetime as dt
import json
import logging
import pathlib
import time

import httpx
import pytest
from attestation_helpers import (
    BUILT_AT,
    COMMIT,
    DIGEST,
    OTHER_DIGEST,
    PROJECT_ID,
    SERVICE_ACCOUNT,
    attestation_payload,
    certs_of,
    make_policy,
    mint_token,
)
from negotiation_core.attestation import AttestationError, verify_attestation_token
from negotiation_core.tee_settings import load_tee_settings
from starlette.requests import Request

import web.app as web_app_module
from web.api import TeeAttestationConfig
from web.app import create_app, create_app_from_env
from web.attested_transport import AttestedVaultTransport
from web.client_ip import UNKNOWN_CLIENT, client_ip
from web.service_auth import IdTokenAuth
from web.session import SESSION_COOKIE_NAME, SESSION_KEY_ENV

REPO = "https://github.com/acme/vault"
CERT_HASH = "e" * 64
NONCE = "N" * 43
RELEASES = [{"digest": DIGEST, "commit": COMMIT, "built_at": BUILT_AT, "status": "active"}]
CONTRACT_KEYS = {"verified", "reason", "checked_at", "nonce", "certificate_sha256", "claims", "release", "token"}
CLAIM_KEYS = {
    "image_digest",
    "hwmodel",
    "swname",
    "swversion",
    "dbgstat",
    "support_attributes",
    "project_id",
    "zone",
    "instance_name",
}


def signed_token(nonce: str, **changes) -> str:
    payload = attestation_payload([nonce, CERT_HASH], now=time.time())
    payload.update(changes)
    return mint_token(payload)


def verified_for(nonce: str, *, digest: str = DIGEST, **changes):
    """本物の検証を通した (トークン, VerifiedAttestation)(digest が許可リストにあるポリシーで)。"""
    token = signed_token(nonce, **changes)
    verified = verify_attestation_token(
        token,
        certs=certs_of(),
        policy=make_policy(allowed_digests=frozenset({DIGEST, OTHER_DIGEST})),
        nonce=nonce,
        certificate_sha256=CERT_HASH,
    )
    return token, verified


def token_of_digest(nonce: str, digest: str) -> str:
    payload = attestation_payload([nonce, CERT_HASH], now=time.time())
    payload["submods"]["container"]["image_digest"] = digest
    return mint_token(payload)


class FakeAttestationSource:
    """AttestedVaultTransport の代わり(web の API が使う口: certificate_sha256 と attest())。outcome を差し替えて使う。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.certificate_sha256: str | None = CERT_HASH
        self.delay = 0.0
        self.outcome = self.valid

    async def valid(self, nonce: str):
        return verified_for(nonce)

    async def attest(self, nonce: str):
        self.calls.append(nonce)
        if self.delay:
            await asyncio.sleep(self.delay)
        return await self.outcome(nonce)


@pytest.fixture
def source() -> FakeAttestationSource:
    return FakeAttestationSource()


def build_app(vault_client, default_db, session_key, clock, *, tee=None):
    return create_app(vault=vault_client, default_db=default_db, session_key=session_key, clock=clock, tee=tee)


@pytest.fixture
async def tee_client(vault_client, default_db, session_key, clock, source):
    """TEE モードの web(偽の transport、digest の表、GitHub のリポジトリ)に ASGI のまま入る client。"""
    app = build_app(
        vault_client, default_db, session_key, clock, tee=TeeAttestationConfig(source, RELEASES, github_repo_url=REPO)
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://web.test") as client:
        yield client


async def get(client, nonce=None, *, ip=None):
    """GET /api/tee/attestation。ip を渡すと、X-Forwarded-For にそのクライアント IP を載せる(Cloud Run のフロントエンドが追記する形)。"""
    headers = None if ip is None else {"X-Forwarded-For": ip}
    return await client.get("/api/tee/attestation", params=None if nonce is None else {"nonce": nonce}, headers=headers)


IP_A, IP_B, IP_C, IP_D = "203.0.113.10", "203.0.113.20", "198.51.100.30", "198.51.100.40"


# --- ルートの有無 ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_route_does_not_exist_when_tee_mode_is_off(vault_client, default_db, session_key, clock):
    app = build_app(vault_client, default_db, session_key, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://web.test") as client:
        assert (await get(client)).status_code == 404
        assert (await get(client, NONCE)).status_code == 404


@pytest.mark.anyio
async def test_the_route_needs_no_session_and_no_custom_header(tee_client, source):
    # 公開情報だけ。クッキーも X-Requested-With もなしで読める(スクリプト --web も同じ)。
    response = await get(tee_client)

    assert response.status_code == 200 and "set-cookie" not in response.headers


@pytest.mark.anyio
async def test_the_route_does_not_touch_the_session_even_when_a_valid_cookie_is_sent(
    vault_client, default_db, session_key, clock, source
):
    # 公開情報の口は、セッションを見ない: 有効なクッキーがあっても、利用記録(last_active_at)を更新せず、クッキーも延ばさない。
    # 対照として、同じクッキーで通常のルートを呼ぶと、利用記録が更新され、クッキーの期限が延びる。
    app = build_app(
        vault_client, default_db, session_key, clock, tee=TeeAttestationConfig(source, RELEASES, github_repo_url=REPO)
    )
    services, pid = app.state.services, "0123456789abcdef"
    assert await services.meta.create_if_absent(pid) == "created"
    created = await services.meta.get(pid)
    cookie = services.codec.issue(pid, clock.now())
    clock.advance(dt.timedelta(hours=2))  # 利用記録の更新の間隔(1 時間)をすぎた
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://web.test") as client:
        client.cookies.set(SESSION_COOKIE_NAME, cookie, domain="web.test")

        tee_response = await get(client)
        assert tee_response.status_code == 200 and "set-cookie" not in tee_response.headers
        assert await services.meta.get(pid) == created  # 更新されていない

        normal_response = await client.get(f"/v1/principals/{pid}/negotiations")
        assert normal_response.status_code == 200 and "set-cookie" in normal_response.headers
        assert (await services.meta.get(pid)).last_active_at > created.last_active_at


# --- 応答の形 -------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_answer_has_every_key_of_the_contract_and_the_verified_values(tee_client, source, clock):
    response = await get(tee_client, NONCE)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    assert (body["verified"], body["reason"]) == (True, None)
    assert body["checked_at"] == clock.now().isoformat()
    assert body["nonce"] == NONCE and source.calls == [NONCE]  # 渡した nonce で、金庫に確かめさせた
    assert body["certificate_sha256"] == CERT_HASH
    assert set(body["claims"]) == CLAIM_KEYS
    assert body["claims"] | {} == {
        "image_digest": DIGEST,
        "hwmodel": "GCP_AMD_SEV",
        "swname": "CONFIDENTIAL_SPACE",
        "swversion": ["250800"],
        "dbgstat": "disabled-since-boot",
        "support_attributes": ["LATEST", "STABLE", "USABLE"],
        "project_id": PROJECT_ID,
        "zone": "asia-northeast1-b",
        "instance_name": "vault-tee-1",
    }
    assert body["release"] == {"commit": COMMIT, "url": f"{REPO}/commit/{COMMIT}", "built_at": BUILT_AT, "status": "active"}
    token = body["token"]
    assert token.count(".") == 2
    # 返したトークンは、利用者が自分で検証できる(web の「verified」を信用しなくてよい)。
    assert verify_attestation_token(
        token, certs=certs_of(), policy=make_policy(), nonce=NONCE, certificate_sha256=None
    ).image_digest == DIGEST


@pytest.mark.anyio
async def test_the_commit_link_is_null_without_a_repository_url_and_has_no_double_slash_with_a_trailing_slash(
    vault_client, default_db, session_key, clock, source
):
    for repo, expected in ((None, None), (REPO + "/", f"{REPO}/commit/{COMMIT}")):
        app = build_app(vault_client, default_db, session_key, clock, tee=TeeAttestationConfig(source, RELEASES, repo))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://web.test") as client:
            body = (await get(client)).json()
        assert body["release"]["url"] == expected and body["release"]["commit"] == COMMIT


# --- nonce なし: 5 分の保持 ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_without_a_nonce_the_latest_result_is_returned_for_five_minutes(tee_client, source, clock):
    first = (await get(tee_client)).json()
    clock.advance(dt.timedelta(minutes=4, seconds=59))
    again = (await get(tee_client)).json()

    assert again == first and len(source.calls) == 1  # 金庫を呼び直さない
    assert len(first["nonce"]) == 43  # 新しい nonce を作った(契約 §2: 32 バイトの base64url)

    clock.advance(dt.timedelta(seconds=1))  # 最初の結果から 5 分
    fresh = (await get(tee_client)).json()

    assert len(source.calls) == 2 and fresh["nonce"] != first["nonce"]
    assert fresh["checked_at"] != first["checked_at"]


@pytest.mark.anyio
async def test_concurrent_requests_without_a_nonce_make_one_check(tee_client, source):
    source.delay = 0.05

    responses = await asyncio.gather(*(get(tee_client) for _ in range(5)))

    assert len(source.calls) == 1
    assert len({response.text for response in responses}) == 1


@pytest.mark.anyio
async def test_a_request_with_a_nonce_neither_uses_nor_replaces_the_kept_result(tee_client, source, clock):
    kept = (await get(tee_client)).json()
    clock.advance(dt.timedelta(seconds=10))

    forwarded = (await get(tee_client, NONCE)).json()
    after = (await get(tee_client)).json()

    assert forwarded["nonce"] == NONCE != kept["nonce"]
    assert after == kept  # nonce つきの結果で、保持した結果を置き換えない
    assert len(source.calls) == 2


# --- nonce あり: 形と、10 秒の制限 --------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "nonce",
    ["", "abc", "a" * 15, "a" * 75, "a" * 16 + "\n", "a" * 15 + "!", "a" * 15 + " ", "あ" * 20, "a" * 15 + "="],
    ids=["empty", "short", "15_chars", "75_chars", "trailing_newline", "bang", "space", "non_ascii", "padding"],
)
async def test_a_nonce_of_the_wrong_form_is_a_400_and_the_vault_is_not_asked(tee_client, source, nonce):
    response = await get(tee_client, nonce)

    assert (response.status_code, response.json()) == (400, {"detail": "invalid_nonce"})
    assert source.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("nonce", ["a" * 16, "A" * 74, "a-b_c" * 4], ids=["16_chars", "74_chars", "with_dash_underscore"])
async def test_nonces_of_the_allowed_form_are_forwarded(tee_client, source, nonce):
    assert (await get(tee_client, nonce)).status_code == 200
    assert source.calls == [nonce]


@pytest.mark.anyio
async def test_a_second_forward_from_the_same_client_within_ten_seconds_is_a_429_and_a_rejection_does_not_extend_the_window(
    tee_client, source, clock
):
    # 契約 §19・台帳 L18-5: 同じクライアント IP は、10 秒に 1 回(ヘッダなしのテストの接続元は、どれも同じアドレス)
    assert (await get(tee_client, "a" * 43)).status_code == 200

    clock.advance(dt.timedelta(seconds=1))
    rejected = await get(tee_client, "b" * 43)
    assert (rejected.status_code, rejected.json()) == (429, {"detail": "rate_limited"})
    assert rejected.headers["retry-after"] == "10"
    clock.advance(dt.timedelta(seconds=8.9))
    assert (await get(tee_client, "c" * 43)).status_code == 429  # 前回の転送から 9.9 秒
    assert source.calls == ["a" * 43]  # 429 のとき、金庫には転送していない

    clock.advance(dt.timedelta(seconds=0.1))  # 前回の転送から 10 秒(429 は数えない)
    assert (await get(tee_client, "d" * 43)).status_code == 200
    assert source.calls == ["a" * 43, "d" * 43]


@pytest.mark.anyio
async def test_a_different_client_is_forwarded_two_seconds_after_the_previous_forward_and_not_before(
    tee_client, source, clock
):
    # 契約 §19・台帳 L18-5: 別のクライアント IP は、全体の前回の転送から 2 秒空けば通る(10 秒は待たない)。拒否は、全体の枠にも数えない
    assert (await get(tee_client, "a" * 43, ip=IP_A)).status_code == 200

    clock.advance(dt.timedelta(seconds=1.9))
    rejected = await get(tee_client, "b" * 43, ip=IP_B)
    assert (rejected.status_code, rejected.json()) == (429, {"detail": "rate_limited"})
    assert rejected.headers["retry-after"] == "2"  # 全体の枠で断った(同じクライアントの 10 秒ではない)
    assert source.calls == ["a" * 43]  # 429 のとき、金庫には転送していない

    clock.advance(dt.timedelta(seconds=0.1))  # 全体の前回の転送から 2 秒(拒否した 1.9 秒の要求は数えない)
    assert (await get(tee_client, "c" * 43, ip=IP_B)).status_code == 200
    assert source.calls == ["a" * 43, "c" * 43]


@pytest.mark.anyio
async def test_each_client_may_forward_once_per_ten_seconds_and_all_clients_together_once_per_two_seconds(
    tee_client, source, clock
):
    # 契約 §19・台帳 L18-5: 2 つの枠は独立している。同じクライアントは、全体の 2 秒が過ぎていても 10 秒待つ(429、Retry-After 10)。
    # 別のクライアントは、全体の 2 秒を待つ(429、Retry-After 2)。拒否した要求は、どちらの枠にも数えない。
    # (経過ミリ秒, クライアント, 期待する結果: 200 / ("429", Retry-After))
    schedule = [
        (0, IP_A, 200),
        (500, IP_B, (429, "2")),  # 全体: 前回の転送(0)から 0.5 秒
        (2000, IP_A, (429, "10")),  # A: 前回(0)から 2 秒。全体の 2 秒は過ぎているが、A の 10 秒は過ぎていない
        (2000, IP_B, 200),  # B は 0.5 秒の拒否を数えないので、まだ一度も転送していない。全体: 前回(0)から 2 秒
        (3000, IP_C, (429, "2")),  # 全体: 前回(2000)から 1 秒
        (4000, IP_C, 200),  # 全体: 前回(2000)から 2 秒
        (4000, IP_B, (429, "10")),  # B: 前回(2000)から 2 秒
        (9900, IP_A, (429, "10")),  # A: 前回(0)から 9.9 秒
        (10000, IP_A, 200),  # A: 前回(0)から 10 秒。全体: 前回(4000)から 6 秒
        (10500, IP_D, (429, "2")),  # 初めてのクライアントでも、全体: 前回(10000)から 0.5 秒
        (12000, IP_B, 200),  # B: 前回(2000)から 10 秒。全体: 前回(10000)から 2 秒
    ]
    elapsed = 0
    forwarded = []
    for index, (at, ip, expected) in enumerate(schedule):
        clock.advance(dt.timedelta(milliseconds=at - elapsed))
        elapsed = at
        nonce = f"{index:02d}" + "x" * 41
        response = await get(tee_client, nonce, ip=ip)
        if expected == 200:
            assert response.status_code == 200, (at, ip)
            forwarded.append(nonce)
        else:
            status, retry_after = expected
            assert (response.status_code, response.headers["retry-after"]) == (status, retry_after), (at, ip)
    assert source.calls == forwarded  # 通した要求だけが、金庫に転送された
    assert len(forwarded) == 5


@pytest.mark.anyio
async def test_one_client_flooding_with_nonces_does_not_keep_another_client_out(tee_client, source, clock):
    # 契約 §19・台帳 L18-5: 匿名の 1 人が枠を独占して、審査員の確認(AC-23 の --web)を 429 にしない。A が毎秒、先に要求し続けても、
    # B は 10 秒に 1 回通る(全体の枠だけを 10 秒に 1 回にしていたときは、A が 10 秒ごとに枠を取り、B は永久に通れなかった)。
    statuses = {IP_A: [], IP_B: []}
    for second in range(30):
        for ip in (IP_A, IP_B):
            response = await get(tee_client, f"{ip[-2:]}{second:02d}" + "x" * 39, ip=ip)
            statuses[ip].append(response.status_code)
        clock.advance(dt.timedelta(seconds=1))

    passed = {ip: [second for second, status in enumerate(codes) if status == 200] for ip, codes in statuses.items()}
    assert passed == {IP_A: [0, 10, 20], IP_B: [2, 12, 22]}  # どちらも、10 秒に 1 回。B は A の 2 秒後
    assert len(source.calls) == 6


@pytest.mark.anyio
async def test_the_client_is_the_last_element_of_x_forwarded_for_and_what_the_user_writes_first_does_not_matter(
    tee_client, source, clock
):
    # 契約 §19・台帳 C-3: Cloud Run のフロントエンドが追記する末尾の IP をクライアントとする。先頭側は利用者が書ける値なので、使わない
    assert (await get(tee_client, "a" * 43, ip=f"1.1.1.1, {IP_A}")).status_code == 200

    clock.advance(dt.timedelta(seconds=3))
    # 先頭側を変えても、末尾が同じなら同じクライアント(10 秒の枠に入る。先頭を書き換えて枠を逃れられない)
    assert (await get(tee_client, "b" * 43, ip=f"2.2.2.2, {IP_A}")).status_code == 429
    # 先頭が A の IP でも、末尾が別なら別のクライアント(全体の 2 秒は過ぎている)
    assert (await get(tee_client, "c" * 43, ip=f"{IP_A}, {IP_B}")).status_code == 200
    assert source.calls == ["a" * 43, "c" * 43]


@pytest.mark.anyio
async def test_an_ipv6_client_is_one_client_per_64_prefix_for_the_ten_second_window(tee_client, source, clock):
    # 台帳 C-71: IPv6 は /64 単位(web.client_ip.client_key)。同じ /64 の中でアドレスを変えても、10 秒の枠は逃れられない。別の /64 は別のクライアント。
    assert (await get(tee_client, "a" * 43, ip="2001:db8:1:2::1")).status_code == 200

    clock.advance(dt.timedelta(seconds=3))  # 全体の 2 秒は過ぎた
    same_prefix = await get(tee_client, "b" * 43, ip="2001:db8:1:2:abcd::9")
    assert (same_prefix.status_code, same_prefix.headers["retry-after"]) == (429, "10")  # 同じ /64: 10 秒の枠(全体の 2 秒ではない)
    assert (await get(tee_client, "c" * 43, ip="2001:db8:1:3::1")).status_code == 200  # 別の /64
    assert source.calls == ["a" * 43, "c" * 43]


@pytest.mark.anyio
async def test_without_the_header_the_client_is_the_connection_address(vault_client, default_db, session_key, clock, source):
    # ヘッダがないとき(ローカル・テスト)は、接続元のアドレスがクライアント: 同じ接続元は 10 秒に 1 回、別の接続元は 2 秒空けば通る
    app = build_app(
        vault_client, default_db, session_key, clock, tee=TeeAttestationConfig(source, RELEASES, github_repo_url=REPO)
    )
    first = httpx.ASGITransport(app=app, client=("192.0.2.1", 5000))
    second = httpx.ASGITransport(app=app, client=("192.0.2.2", 5000))
    async with (
        httpx.AsyncClient(transport=first, base_url="https://web.test") as one,
        httpx.AsyncClient(transport=second, base_url="https://web.test") as other,
    ):
        assert (await get(one, "a" * 43)).status_code == 200
        clock.advance(dt.timedelta(seconds=2))
        assert (await get(one, "b" * 43)).status_code == 429  # 同じ接続元: 10 秒の枠
        assert (await get(other, "c" * 43)).status_code == 200  # 別の接続元: 全体の 2 秒は過ぎた
        clock.advance(dt.timedelta(seconds=8))
        assert (await get(one, "d" * 43)).status_code == 200  # 最初の転送から 10 秒
        assert (await get(other, "e" * 43)).status_code == 429  # other は 8 秒前に転送した(全体の 2 秒は過ぎているが、other の 10 秒の枠の中)
    assert source.calls == ["a" * 43, "c" * 43, "d" * 43]


@pytest.mark.anyio
async def test_a_forward_without_a_nonce_counts_in_the_overall_window_only(tee_client, source, clock):
    # 台帳 L18-5: nonce なしの転送(保持した結果がないとき)は、金庫の発行枠を使うので、全体の 2 秒の枠には数える。クライアントごとの
    # 10 秒の枠は使わない(nonce なしの要求は、保持した結果を返すので、クライアントごとには制限しない)。
    assert (await get(tee_client, ip=IP_A)).status_code == 200  # nonce なし: 転送した
    assert len(source.calls) == 1

    clock.advance(dt.timedelta(seconds=1))
    rejected = await get(tee_client, "b" * 43, ip=IP_B)
    assert (rejected.status_code, rejected.headers["retry-after"]) == (429, "2")  # 全体: nonce なしの転送から 1 秒

    clock.advance(dt.timedelta(seconds=1))
    assert (await get(tee_client, "c" * 43, ip=IP_A)).status_code == 200  # A は、nonce なしの転送で 10 秒の枠を使っていない
    assert len(source.calls) == 2


# --- クライアント IP の取り方(web/client_ip.py。台帳 C-3。攻撃モードのレート制限も同じ関数を使う) ---


def make_request(headers=(), *, client=("203.0.113.9", 4321)) -> Request:
    """ヘッダ(名前と値の組)と接続元だけを持つ、最小の ASGI の scope から作った Request。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers],
        "client": client,
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # ヘッダなし: 接続元のアドレス
        ([], "203.0.113.9"),
        ([("User-Agent", "x")], "203.0.113.9"),
        # 1 つ: その値
        ([("X-Forwarded-For", "198.51.100.7")], "198.51.100.7"),
        ([("x-forwarded-for", "198.51.100.7")], "198.51.100.7"),  # 名前の大文字小文字は問わない
        ([("X-Forwarded-For", "2001:db8::1")], "2001:db8::1"),
        # 複数: 最後の要素(Cloud Run が追記する。先頭側は利用者が書ける)
        ([("X-Forwarded-For", "10.0.0.1, 192.0.2.5, 198.51.100.7")], "198.51.100.7"),
        ([("X-Forwarded-For", "10.0.0.1,198.51.100.7")], "198.51.100.7"),
        ([("X-Forwarded-For", "198.51.100.7, 2001:db8::1")], "2001:db8::1"),
        # 空白: 要素の前後は除く
        ([("X-Forwarded-For", "  198.51.100.7  ")], "198.51.100.7"),
        ([("X-Forwarded-For", "10.0.0.1 ,   198.51.100.7   ")], "198.51.100.7"),
        ([("X-Forwarded-For", "10.0.0.1\t,\t198.51.100.7")], "198.51.100.7"),
        # ヘッダが複数行: つなげた全体の最後の要素
        ([("X-Forwarded-For", "10.0.0.1"), ("X-Forwarded-For", "198.51.100.7")], "198.51.100.7"),
        ([("X-Forwarded-For", "10.0.0.1, 192.0.2.5"), ("X-Forwarded-For", "198.51.100.7")], "198.51.100.7"),
        # 最後の要素が空: ヘッダがないものとして扱う(先頭側の、利用者が書ける値に頼らない)
        ([("X-Forwarded-For", "")], "203.0.113.9"),
        ([("X-Forwarded-For", "   ")], "203.0.113.9"),
        ([("X-Forwarded-For", "10.0.0.1,")], "203.0.113.9"),
        ([("X-Forwarded-For", "10.0.0.1, ,  ")], "203.0.113.9"),
        ([("X-Forwarded-For", ",")], "203.0.113.9"),
    ],
    ids=[
        "no_header",
        "other_headers_only",
        "one_value",
        "lower_case_name",
        "ipv6",
        "several_values",
        "several_values_without_spaces",
        "ipv4_then_ipv6",
        "spaces_around_the_value",
        "spaces_around_the_commas",
        "tabs_around_the_commas",
        "two_header_lines",
        "two_header_lines_with_several_values",
        "empty_header",
        "blank_header",
        "trailing_comma",
        "trailing_blank_elements",
        "only_a_comma",
    ],
)
def test_client_ip_is_the_last_element_of_x_forwarded_for_or_the_connection_address(headers, expected):
    # 設計書 §8.2「クライアントの見分け方」・台帳 C-3: X-Forwarded-For があれば、Cloud Run が末尾に追記した IP(最後の要素)。なければ接続元のアドレス
    assert client_ip(make_request(headers)) == expected


def test_client_ip_is_one_shared_key_when_nothing_tells_the_client():
    # ヘッダも接続元もなければ、分からない要求は 1 つのキーを共有する(制限の側に倒す)
    assert client_ip(make_request([], client=None)) == UNKNOWN_CLIENT == "unknown"
    assert client_ip(make_request([("X-Forwarded-For", "")], client=None)) == UNKNOWN_CLIENT
    assert client_ip(make_request([("X-Forwarded-For", "198.51.100.7")], client=None)) == "198.51.100.7"  # ヘッダがあれば足りる


@pytest.mark.anyio
async def test_an_anonymous_flood_with_nonces_makes_one_forward_per_ten_seconds(tee_client, source, clock):
    # 金庫の発行枠(毎秒 1 回)を、匿名の利用者が使い切れない: 同じクライアント IP が 1 秒に 1 回、30 秒続けても、転送は 3 回だけ。
    statuses = []
    for second in range(30):
        statuses.append((await get(tee_client, f"{second:02d}" + "x" * 41)).status_code)
        clock.advance(dt.timedelta(seconds=1))

    assert statuses.count(200) == 3 and len(source.calls) == 3
    assert [index for index, status in enumerate(statuses) if status == 200] == [0, 10, 20]


@pytest.mark.anyio
async def test_the_rate_limit_applies_to_requests_with_a_nonce_only(tee_client, source, clock):
    assert (await get(tee_client, "a" * 43)).status_code == 200
    clock.advance(dt.timedelta(seconds=0.5))

    assert (await get(tee_client)).status_code == 200  # nonce なしは、保持した結果がなければ検証する(10 秒の制限は受けない)
    assert len(source.calls) == 2


# --- 失敗のとき -----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_failed_verification_is_reported_with_the_reason_the_claims_the_token_and_no_release(tee_client, source):
    async def debug_image(nonce):
        raise AttestationError("debug", token=signed_token(nonce, dbgstat="enabled"))

    source.outcome = debug_image

    body = (await get(tee_client, NONCE)).json()

    assert set(body) == CONTRACT_KEYS
    assert (body["verified"], body["reason"]) == (False, "debug")
    assert body["claims"]["dbgstat"] == "enabled" and body["claims"]["image_digest"] == DIGEST  # 取れた範囲の claims
    assert body["release"] is None  # 検証が通っていないので、コミットのリンクは出さない
    assert body["token"].count(".") == 2


@pytest.mark.anyio
async def test_a_failed_result_without_a_nonce_is_kept_too_so_that_the_google_quota_is_protected(tee_client, source):
    async def tampered(nonce):
        raise AttestationError("signature", token=signed_token(nonce))

    source.outcome = tampered

    first = (await get(tee_client)).json()
    again = (await get(tee_client)).json()

    assert first["reason"] == "signature" and again == first and len(source.calls) == 1


@pytest.mark.anyio
async def test_the_release_is_not_looked_up_from_a_token_whose_signature_was_not_verified(tee_client, source):
    async def unsigned(nonce):
        raise AttestationError("signature", token=signed_token(nonce))  # 表にある digest を名乗っているが、署名が確かめられない

    source.outcome = unsigned

    body = (await get(tee_client, NONCE)).json()

    assert body["claims"]["image_digest"] == DIGEST and body["release"] is None and body["verified"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "not-a-jwt"])
async def test_a_token_that_cannot_be_read_gives_empty_claims(tee_client, source, token):
    async def broken(nonce):
        raise AttestationError("malformed", token=token)

    source.outcome = broken

    body = (await get(tee_client, NONCE)).json()

    assert body["reason"] == "malformed" and body["token"] == token
    assert set(body["claims"]) == CLAIM_KEYS and body["claims"]["image_digest"] is None and body["claims"]["swversion"] == []


@pytest.mark.anyio
async def test_an_unreachable_vault_is_reported_without_a_token_and_is_kept_for_ten_seconds_only(tee_client, source, clock):
    async def down(nonce):
        raise httpx.ConnectError("connection refused")

    source.outcome = down

    first = (await get(tee_client)).json()
    second = (await get(tee_client)).json()

    assert (first["verified"], first["reason"], first["token"], first["release"]) == (False, "unavailable", None, None)
    assert set(first["claims"]) == CLAIM_KEYS and first["claims"]["image_digest"] is None
    assert first["certificate_sha256"] == CERT_HASH and first["nonce"]
    assert second == first and len(source.calls) == 1  # 10 秒は覚える: 金庫が応えない間に、匿名の要求が金庫の発行枠を使わない
    source.outcome = source.valid
    clock.advance(dt.timedelta(seconds=9.9))
    assert (await get(tee_client)).json() == first and len(source.calls) == 1

    clock.advance(dt.timedelta(seconds=0.1))  # 10 秒たった: もう一度試す
    assert (await get(tee_client)).json()["verified"] is True and len(source.calls) == 2


@pytest.mark.anyio
async def test_a_flood_of_requests_without_a_nonce_makes_one_forward_per_ten_seconds_even_when_the_vault_is_down(
    tee_client, source, clock
):
    async def down(nonce):
        raise httpx.ConnectError("connection refused")

    source.outcome = down

    for _ in range(20):
        assert (await get(tee_client)).json()["reason"] == "unavailable"
    assert len(source.calls) == 1

    clock.advance(dt.timedelta(seconds=10))
    for _ in range(20):
        await get(tee_client)
    assert len(source.calls) == 2


@pytest.mark.anyio
async def test_a_vault_that_answers_without_a_token_is_reported_as_unavailable(tee_client, source):
    async def no_token(nonce):
        raise AttestationError("unavailable")

    source.outcome = no_token

    body = (await get(tee_client, NONCE)).json()

    assert (body["verified"], body["reason"], body["token"]) == (False, "unavailable", None)


# --- release の照合 ------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_verified_attestation_whose_digest_is_not_in_the_table_is_not_reported_as_verified(tee_client, source):
    async def other_image(nonce):
        payload = attestation_payload([nonce, CERT_HASH], now=time.time())
        payload["submods"]["container"]["image_digest"] = OTHER_DIGEST
        token = mint_token(payload)
        return token, verify_attestation_token(
            token,
            certs=certs_of(),
            policy=make_policy(allowed_digests=frozenset({OTHER_DIGEST})),  # 金庫側の許可リストが、表と食い違っている
            nonce=nonce,
            certificate_sha256=CERT_HASH,
        )

    source.outcome = other_image

    body = (await get(tee_client, NONCE)).json()

    assert (body["verified"], body["reason"], body["release"]) == (False, "image_digest", None)
    assert body["claims"]["image_digest"] == OTHER_DIGEST


@pytest.mark.anyio
async def test_a_revoked_release_is_not_verified_but_the_release_status_says_so(
    vault_client, default_db, session_key, clock, source
):
    revoked = [{**RELEASES[0], "status": "revoked"}]
    app = build_app(vault_client, default_db, session_key, clock, tee=TeeAttestationConfig(source, revoked, REPO))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://web.test") as client:
        # 1. 金庫の許可リストが表と食い違い、検証が通ってしまっても、失効した digest は検証済みにしない
        body = (await get(client, NONCE)).json()
        assert (body["verified"], body["reason"]) == (False, "image_digest")
        assert body["release"] == {
            "commit": COMMIT,
            "url": f"{REPO}/commit/{COMMIT}",
            "built_at": BUILT_AT,
            "status": "revoked",
        }

        # 2. 実際の流れ: 許可リストは active だけなので、検証は image_digest で外れる(署名・期限などは通った後)。release.status で失効が分かる
        async def rejected(nonce):
            raise AttestationError("image_digest", token=signed_token(nonce))

        source.outcome = rejected
        clock.advance(dt.timedelta(seconds=10))
        body = (await get(client, "R" * 43)).json()
        assert (body["verified"], body["reason"], body["release"]["status"]) == (False, "image_digest", "revoked")


@pytest.mark.anyio
async def test_an_unknown_digest_rejected_by_the_vault_allow_list_has_no_release(tee_client, source):
    async def unknown(nonce):
        raise AttestationError("image_digest", token=token_of_digest(nonce, "sha256:" + "0" * 64))

    source.outcome = unknown

    body = (await get(tee_client, NONCE)).json()

    assert (body["verified"], body["reason"], body["release"]) == (False, "image_digest", None)


@pytest.mark.anyio
async def test_a_release_without_a_status_is_treated_as_active(vault_client, default_db, session_key, clock, source):
    plain = [{"digest": DIGEST, "commit": COMMIT, "built_at": BUILT_AT}]
    app = build_app(vault_client, default_db, session_key, clock, tee=TeeAttestationConfig(source, plain, REPO))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://web.test") as client:
        body = (await get(client)).json()

    assert body["verified"] is True and body["release"]["status"] == "active"


# --- 起動口(create_app_from_env)の組み立て ---------------------------------------------------------------------

SESSION_KEY = "oRXjuLrpBpmCPe_O9zLtE1ZhEXY7BAssKXRaJDPp1fg"
TEE_VAULT_URL = "https://10.10.0.10:8443"


class Captured:
    """_open_vault_http_client に渡された引数。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    @property
    def vault_url(self):
        return self.calls[-1][0]

    @property
    def auth(self):
        return self.calls[-1][1]

    @property
    def transport(self):
        return self.calls[-1][2] if len(self.calls[-1]) > 2 else None


@pytest.fixture
def entry(monkeypatch, default_db, tmp_path):
    """create_app_from_env を、Firestore と金庫への接続をスタブにして呼ぶ(接続の引数を記録する)。環境は TEE の必須の変数が入っている。"""
    captured = Captured()
    monkeypatch.setattr(web_app_module, "_create_default_db", lambda: default_db)

    def open_client(vault_url, auth, transport=None):
        captured.calls.append((vault_url, auth) if transport is None else (vault_url, auth, transport))
        return httpx.AsyncClient(base_url=vault_url, auth=auth, transport=httpx.MockTransport(lambda request: httpx.Response(204)))

    monkeypatch.setattr(web_app_module, "_open_vault_http_client", open_client)
    releases = tmp_path / "releases.json"
    releases.write_text(
        json.dumps(
            {
                "releases": [
                    {"digest": DIGEST, "commit": COMMIT, "built_at": BUILT_AT, "status": "active"},
                    {"digest": OTHER_DIGEST, "commit": "f" * 40, "built_at": BUILT_AT, "status": "revoked"},
                ]
            }
        )
    )
    base = {
        SESSION_KEY_ENV: SESSION_KEY,
        "VAULT_BASE_URL": TEE_VAULT_URL,
        "VAULT_TEE": "true",
        "VAULT_SERVICE_ACCOUNT": SERVICE_ACCOUNT,
        "GOOGLE_CLOUD_PROJECT": PROJECT_ID,
        "VAULT_RELEASES_FILE": str(releases),
    }

    def start(**changes):
        environ = {**base, **changes}
        return create_app_from_env({key: value for key, value in environ.items() if value is not None})

    start.captured = captured
    start.releases_file = releases
    return start


def has_tee_route(app) -> bool:
    return "/api/tee/attestation" in app.openapi()["paths"]  # app.routes には、include_router した先のルートが並ばない


def test_tee_mode_builds_an_attested_transport_a_fixed_audience_and_the_route(entry):
    app = entry()

    transport = entry.captured.transport
    assert isinstance(transport, AttestedVaultTransport) and entry.captured.vault_url == TEE_VAULT_URL
    assert has_tee_route(app)
    settings = load_tee_settings()
    # ID トークンの audience は、金庫の URL(IP アドレス)からではなく、設定の固定の値
    assert isinstance(entry.captured.auth, IdTokenAuth) and entry.captured.auth.audience == settings.caller_audience
    policy = transport._policy
    assert (policy.audience, policy.issuer) == (settings.attestation_audience, settings.attestation_issuer)
    assert policy.allowed_hwmodels == frozenset(settings.allowed_hwmodels)
    assert policy.allowed_digests == {DIGEST}  # active だけ(失効した digest は入れない)
    assert (policy.project_id, policy.service_account) == (PROJECT_ID, SERVICE_ACCOUNT)
    assert (policy.zone, policy.instance_name, policy.require_production) == (None, None, True)


def test_tee_mode_takes_the_expected_zone_instance_and_the_repository_url_when_given(entry):
    app = entry(VAULT_EXPECTED_ZONE=" asia-northeast1-b ", VAULT_EXPECTED_INSTANCE="vault-tee-1", GITHUB_REPO_URL=REPO)

    policy = entry.captured.transport._policy
    assert (policy.zone, policy.instance_name) == ("asia-northeast1-b", "vault-tee-1")
    assert has_tee_route(app)


def test_tee_mode_is_off_by_default_and_the_vault_client_is_opened_as_before(entry):
    for value in (None, "false", " FALSE "):
        app = entry(VAULT_TEE=value, VAULT_SERVICE_ACCOUNT=None, GOOGLE_CLOUD_PROJECT=None, VAULT_RELEASES_FILE=None)

        assert len(entry.captured.calls[-1]) == 2 and entry.captured.transport is None  # 従来の呼び方(transport なし)
        assert not has_tee_route(app)
        # 非 TEE の ID トークンの audience は、従来どおり金庫の URL から
        assert entry.captured.auth.audience == TEE_VAULT_URL


@pytest.mark.parametrize("value", ["", "yes", "1", "True!", "on"])
def test_any_other_value_of_vault_tee_refuses_to_start(entry, value):
    with pytest.raises(ValueError, match="VAULT_TEE"):
        entry(VAULT_TEE=value)


@pytest.mark.parametrize("name", ["VAULT_SERVICE_ACCOUNT", "GOOGLE_CLOUD_PROJECT"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_tee_mode_refuses_to_start_without_the_service_account_or_the_project(entry, name, value):
    with pytest.raises(RuntimeError, match=name):
        entry(**{name: value})


def test_tee_mode_refuses_to_start_when_the_releases_file_is_missing_or_malformed(entry, tmp_path):
    with pytest.raises(RuntimeError, match="VAULT_RELEASES_FILE"):
        entry(VAULT_RELEASES_FILE=str(tmp_path / "missing.json"))
    broken = tmp_path / "broken.json"
    broken.write_text("not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        entry(VAULT_RELEASES_FILE=str(broken))


@pytest.mark.parametrize("url", ["http://10.10.0.10:8443", "10.10.0.10:8443"])
def test_tee_mode_refuses_to_start_when_the_vault_url_is_not_https(entry, url):
    with pytest.raises((ValueError, httpx.InvalidURL)):
        entry(VAULT_BASE_URL=url)


def test_the_default_releases_file_is_the_one_in_the_repository(entry):
    # 既定: deploy/vault-releases.json(スパイク以降、active なダイジェストが入っている。空の表の警告は次の試験で確かめる)
    repository_table = json.loads((pathlib.Path(__file__).resolve().parents[1] / "deploy" / "vault-releases.json").read_text(encoding="utf-8"))
    active = frozenset(release["digest"] for release in repository_table["releases"] if release.get("status") == "active")

    app = entry(VAULT_RELEASES_FILE=None)

    assert has_tee_route(app) and entry.captured.transport._policy.allowed_digests == active


def test_an_empty_releases_table_warns_that_no_vault_image_is_accepted(entry, caplog, tmp_path):
    caplog.set_level(logging.WARNING, logger=web_app_module.__name__)
    empty = tmp_path / "empty-releases.json"
    empty.write_text('{"releases": []}', encoding="utf-8")

    app = entry(VAULT_RELEASES_FILE=str(empty))

    assert has_tee_route(app) and entry.captured.transport._policy.allowed_digests == frozenset()
    assert "no vault image is accepted" in caplog.text


def test_tee_mode_without_the_service_auth_has_no_authorization_but_still_pins_the_certificate(entry):
    entry(SERVICE_AUTH_ENABLED="false")

    assert entry.captured.auth is None and isinstance(entry.captured.transport, AttestedVaultTransport)


def test_the_vault_client_opened_for_tee_mode_uses_the_transport_and_ignores_proxy_environment(monkeypatch):
    # 本物の _open_vault_http_client: transport を使い、環境のプロキシ設定(trust_env)で、ピン留めを迂回させない。
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    client = web_app_module._open_vault_http_client(TEE_VAULT_URL, None, transport)

    assert client._transport is transport and client.trust_env is False
    assert client._mounts == {}  # 環境のプロキシ由来の経路が作られていない
    plain = web_app_module._open_vault_http_client("https://vault.example.run.app", None)
    assert plain.trust_env is True  # 従来の呼び方は変えない
