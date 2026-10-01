"""台帳 X-37: サービス間の認証(design.md §1.1)。web が金庫・agents を呼ぶときに、Google の ID トークンを付ける。

金庫と agents は、Cloud Run の IAM で認証を必須にする(アプリの中での検証は足さない)。そのため、呼び出し側(web の金庫の
クライアントと、agents.client.send_turn)が、呼び先のサービスの URL を audience にした ID トークンを
`Authorization: Bearer` で付ける。トークンは Cloud Run のメタデータサーバから取る(ここでは、httpx.MockTransport の
スタブ。本物のメタデータサーバにも GCP にも接続しない)。確かめること:

- トークンの取り方(メタデータサーバの URL・ヘッダ・audience の指定)と、期限の少し前までの使い回し・audience ごとの保持。
- 入れたときは、金庫の呼び出しにも agents の呼び出しにも、ヘッダが付く。audience は、呼び先の URL ごとに正しい。
- 切ったとき(SERVICE_AUTH_ENABLED=false)は、ヘッダが付かず、メタデータサーバも呼ばない。
- トークンを取れないときは、通信エラーとして伝わる(金庫のクライアントは「一時的に応えない」、agents のクライアントは
  ConnectionError になり、レフェリーが待って・再試行してやり直す)。トークンの値は、エラーの文に入らない。
"""

import asyncio
import base64
import json
import logging

import httpx
import pytest
from agents_helpers import NID, PACKAGE, agents_app, move_json, stub_llm, valid_data  # noqa: F401  (フィクスチャは import して使う)
from negotiation_core.schema import TurnInput

import agents.client as agents_client_module
import web.app as web_app_module
import web.service_auth as service_auth_module
from agents.client import send_turn
from vault.app import create_app as create_vault_app
from vault_helpers import put_candidate_and_employer_templates
from web.app import bind_agents_client, create_app_from_env
from web.service_auth import (
    METADATA_IDENTITY_URL,
    REFRESH_MARGIN_SECONDS,
    SERVICE_AUTH_ENV,
    IdTokenAuth,
    IdTokenProvider,
    ServiceAuthError,
    audience_for,
    id_token_provider_from_env,
)
from web.session import SESSION_KEY_ENV
from web.vault_client import VaultClient, VaultUnavailableError
from web_app_helpers import build_web_env

VAULT_URL = "https://vault-abc123-an.a.run.app"
AGENTS_URL = "https://agents-abc123-an.a.run.app"
_SESSION_KEY = "oRXjuLrpBpmCPe_O9zLtE1ZhEXY7BAssKXRaJDPp1fg"
_TOKEN_LIFETIME = 3600.0
_EXPIRE_PATH_NID = "0123456789abcdef"


def _b64(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def make_jwt(*, audience: str, expires_at: float, serial: int) -> str:
    """メタデータサーバが返す ID トークンに似た、署名のない JWT(署名は、受け取る側が確かめるので、ここでは要らない)。"""
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64({'aud': audience, 'exp': int(expires_at), 'n': serial})}.signature"


class FakeClock:
    """UNIX 秒を返す、進められる時計(IdTokenProvider の now に差し込む)。"""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeMetadataServer:
    """Cloud Run のメタデータサーバのスタブ。audience ごとに、ID トークンを返す。呼ばれた内容を記録する。"""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    @property
    def audiences(self) -> list[str]:
        return [request.url.params["audience"] for request in self.requests]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        audience = request.url.params["audience"]
        token = make_jwt(audience=audience, expires_at=self._clock() + _TOKEN_LIFETIME, serial=len(self.requests))
        return httpx.Response(200, text=token + "\n")  # メタデータサーバは、本文の末尾に改行を付け得る


def _bearer(request: httpx.Request) -> str | None:
    """リクエストの Authorization ヘッダから、Bearer のトークンを取り出す(なければ None)。"""
    value = request.headers.get("authorization")
    if value is None:
        return None
    assert value.startswith("Bearer "), value
    return value.removeprefix("Bearer ")


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@pytest.fixture
def now() -> FakeClock:
    return FakeClock()


@pytest.fixture
def metadata(now) -> FakeMetadataServer:
    return FakeMetadataServer(now)


@pytest.fixture
def provider(metadata, now) -> IdTokenProvider:
    return IdTokenProvider(transport=metadata.transport, now=now)


# --- トークンの取り方 ---


@pytest.mark.anyio
async def test_the_provider_asks_the_metadata_server_with_the_flavor_header_and_the_audience(provider, metadata):
    # §1.1・X-37: メタデータサーバの identity の口に、ヘッダ Metadata-Flavor: Google と audience を付けて 1 回呼ぶ。
    token = await provider.token(VAULT_URL)

    (request,) = metadata.requests
    assert request.method == "GET"
    assert str(request.url.copy_with(query=None)) == METADATA_IDENTITY_URL
    assert request.url.params["audience"] == VAULT_URL
    assert request.headers["Metadata-Flavor"] == "Google"
    assert _claims(token)["aud"] == VAULT_URL  # スタブが、audience を入れたトークンを返した
    assert token == token.strip()  # 本文の末尾の改行は、トークンに入らない


@pytest.mark.anyio
async def test_a_token_is_reused_until_shortly_before_its_expiry(provider, metadata, now):
    # X-37: トークンは、期限の少し前まで使い回す(呼び出しのたびにメタデータサーバを呼ばない)。
    # 期限まで 5 分(REFRESH_MARGIN_SECONDS)を切ったら、取り直す。
    first = await provider.token(VAULT_URL)
    assert await provider.token(VAULT_URL) == first
    now.advance(_TOKEN_LIFETIME - REFRESH_MARGIN_SECONDS - 1)  # 期限まで margin + 1 秒
    assert await provider.token(VAULT_URL) == first
    assert len(metadata.requests) == 1

    now.advance(2)  # 期限まで margin - 1 秒
    renewed = await provider.token(VAULT_URL)

    assert renewed != first
    assert len(metadata.requests) == 2
    assert _claims(renewed)["exp"] > _claims(first)["exp"]
    assert await provider.token(VAULT_URL) == renewed  # 取り直したトークンも、使い回す
    assert len(metadata.requests) == 2


@pytest.mark.anyio
async def test_tokens_are_kept_per_audience(provider, metadata):
    # X-37: トークンは audience ごとに持つ。別の呼び先のトークンを、使い回さない。
    vault_token = await provider.token(VAULT_URL)
    agents_token = await provider.token(AGENTS_URL)

    assert vault_token != agents_token
    assert (_claims(vault_token)["aud"], _claims(agents_token)["aud"]) == (VAULT_URL, AGENTS_URL)
    assert metadata.audiences == [VAULT_URL, AGENTS_URL]
    assert await provider.token(VAULT_URL) == vault_token  # それぞれ、使い回される
    assert await provider.token(AGENTS_URL) == agents_token
    assert len(metadata.requests) == 2


@pytest.mark.parametrize(
    ("service_url", "audience"),
    [
        (VAULT_URL, VAULT_URL),
        (VAULT_URL + "/", VAULT_URL),  # 末尾のスラッシュは、audience に入れない
        (VAULT_URL + "/v1/negotiations?open=true", VAULT_URL),  # パスもクエリも入れない
        ("http://localhost:8080", "http://localhost:8080"),  # ポートは残す
        ("https://user:secret@host.example:8443/x", "https://host.example:8443"),  # 認証情報は入れない
    ],
)
def test_the_audience_is_the_scheme_and_host_of_the_service_url(service_url, audience):
    assert audience_for(service_url) == audience
    assert IdTokenAuth(IdTokenProvider(), service_url).audience == audience


@pytest.mark.parametrize("not_a_url", ["", "vault", "/v1/negotiations", "vault.example.run.app"])
def test_a_value_that_is_not_a_service_url_is_refused(not_a_url):
    with pytest.raises(ValueError, match="scheme and a host"):
        audience_for(not_a_url)


# --- 取れないとき ---


def _provider_with(handler, now) -> IdTokenProvider:
    return IdTokenProvider(transport=httpx.MockTransport(handler), now=now)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(404, text="not found"),
        lambda request: httpx.Response(503, text="try later"),
        lambda request: httpx.Response(200, text="not-a-jwt"),
        lambda request: httpx.Response(200, text=""),
        lambda request: httpx.Response(200, text="a.!!!.c"),  # 本文が base64url でない
        lambda request: httpx.Response(200, text=f"a.{_b64({'aud': 'x'})}.c"),  # exp がない
        lambda request: httpx.Response(200, text=f"a.{_b64({'exp': 'tomorrow'})}.c"),  # exp が数でない
        lambda request: httpx.Response(200, text=f"a.{_b64({'exp': True})}.c"),  # exp が真偽値
        lambda request: httpx.Response(200, text=f"a.{base64.urlsafe_b64encode(b'[1]').decode()}.c"),  # 本文が辞書でない
    ],
    ids=["404", "503", "not_a_jwt", "empty", "payload_not_base64url", "no_exp", "exp_not_a_number", "exp_bool", "payload_list"],
)
async def test_a_response_that_is_not_an_id_token_is_a_service_auth_error(handler, now):
    # X-37: 200 でない応答・ID トークン(exp つきの JWT)でない本文は、ServiceAuthError。通信エラーの一種なので、
    # 金庫・agents のクライアントが、一時的な失敗として扱える。
    provider = _provider_with(handler, now)

    with pytest.raises(ServiceAuthError) as excinfo:
        await provider.token(VAULT_URL)

    assert isinstance(excinfo.value, httpx.TransportError)


@pytest.mark.anyio
async def test_an_unreachable_metadata_server_is_a_service_auth_error_that_says_how_to_turn_it_off(now):
    # X-37: メタデータサーバに届かない(Cloud Run の外で、切り忘れた場合など)とき、エラーの文で、切り方を示す。
    def unreachable(request):
        raise httpx.ConnectError("name or service not known")

    with pytest.raises(ServiceAuthError, match=f"{SERVICE_AUTH_ENV}=false"):
        await _provider_with(unreachable, now).token(VAULT_URL)

    def too_slow(request):
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(ServiceAuthError):  # 時間切れも、同じ通信エラーとして伝える(agents の「応答がない」と混ざらない)
        await _provider_with(too_slow, now).token(VAULT_URL)

    with pytest.raises(ServiceAuthError, match=f"404.*{SERVICE_AUTH_ENV}=false"):  # 200 でない応答でも、切り方を示す
        await _provider_with(lambda request: httpx.Response(404), now).token(VAULT_URL)


@pytest.mark.anyio
async def test_a_failure_is_not_cached_and_the_next_call_asks_again(now):
    # X-37: 取れなかったことを覚えない。次の呼び出しは、もう一度メタデータサーバに聞く。
    responses = [httpx.Response(503, text="try later")]
    server = FakeMetadataServer(now)

    def flaky(request):
        return responses.pop() if responses else server._handle(request)

    provider = _provider_with(flaky, now)
    with pytest.raises(ServiceAuthError):
        await provider.token(VAULT_URL)

    assert _claims(await provider.token(VAULT_URL))["aud"] == VAULT_URL
    assert len(server.requests) == 1


@pytest.mark.anyio
async def test_the_token_never_appears_in_an_error_message(now):
    # X-37: トークンの値を、エラーの文に入れない(ログに残らないように)。壊れたトークンを返す応答でも、同じ。
    secret = f"secret-token.{_b64({'exp': 'tomorrow'})}.signature"
    provider = _provider_with(lambda request: httpx.Response(200, text=secret), now)

    with pytest.raises(ServiceAuthError) as excinfo:
        await provider.token(VAULT_URL)

    assert secret not in str(excinfo.value) and "secret-token" not in str(excinfo.value)


# --- 設定(環境変数 SERVICE_AUTH_ENABLED) ---


@pytest.mark.parametrize("value", ["true", "TRUE", " True ", None])
def test_the_service_auth_is_on_by_default_and_when_true(value):
    # 未設定は「使う」(本番の起動口の既定)。大文字小文字・前後の空白は問わない。
    environ = {} if value is None else {SERVICE_AUTH_ENV: value}
    assert isinstance(id_token_provider_from_env(environ), IdTokenProvider)


@pytest.mark.parametrize("value", ["false", "FALSE", " false\n"])
def test_the_service_auth_is_off_when_false(value):
    assert id_token_provider_from_env({SERVICE_AUTH_ENV: value}) is None


@pytest.mark.parametrize("value", ["", "yes", "no", "1", "0", "flase", "off"])
def test_any_other_value_refuses_to_start(value):
    # 打ち間違いで、意図せず切れたり入ったりしないように、true・false 以外は拒否する。
    with pytest.raises(ValueError, match=SERVICE_AUTH_ENV):
        id_token_provider_from_env({SERVICE_AUTH_ENV: value})


# --- 金庫の呼び出し(web の金庫のクライアント。本番の組み立て create_app_from_env) ---


class RecordingTransport(httpx.AsyncBaseTransport):
    """送ったリクエストを記録して、中の通信路に渡す(中の通信路がなければ、決まった応答を返す)。"""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None, *, body: dict | None = None) -> None:
        self.inner = inner
        self.body = body
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        if self.inner is not None:
            return await self.inner.handle_async_request(request)
        return httpx.Response(200, json=self.body)


@pytest.fixture
def production_entry(monkeypatch, default_db, metadata, now):
    """create_app_from_env を、メタデータサーバと金庫をスタブにして呼ぶ(金庫の呼び出しを記録する)。"""
    vault = RecordingTransport(body={"version": 3, "status": "active", "expired": False})
    monkeypatch.setattr(web_app_module, "_create_default_db", lambda: default_db)
    monkeypatch.setattr(
        web_app_module,
        "_open_vault_http_client",
        lambda vault_url, auth: httpx.AsyncClient(transport=vault, base_url=vault_url, auth=auth),
    )
    monkeypatch.setattr(
        service_auth_module, "IdTokenProvider", lambda: IdTokenProvider(transport=metadata.transport, now=now)
    )

    def start(**environ):
        return create_app_from_env({SESSION_KEY_ENV: _SESSION_KEY, "VAULT_BASE_URL": VAULT_URL, **environ})

    start.vault = vault
    return start


@pytest.mark.anyio
@pytest.mark.parametrize("environ", [{}, {SERVICE_AUTH_ENV: "true"}], ids=["default", "true"])
async def test_the_production_vault_client_sends_an_id_token_for_the_vault_url(production_entry, metadata, environ):
    # X-37: 入れたとき(未設定を含む)は、金庫の呼び出しに Authorization: Bearer <ID トークン> が付く。audience は金庫の URL。
    app = production_entry(**environ)

    await app.state.services.vault.expire(_EXPIRE_PATH_NID)
    await app.state.services.vault.expire(_EXPIRE_PATH_NID)

    sent = production_entry.vault.requests
    assert len(sent) == 2
    tokens = {_bearer(request) for request in sent}
    assert len(tokens) == 1 and None not in tokens  # 2 回とも付き、使い回されている
    (token,) = tokens
    assert _claims(token)["aud"] == VAULT_URL
    assert metadata.audiences == [VAULT_URL]  # メタデータサーバへは 1 回だけ(トークンの使い回し)
    assert metadata.requests[0].headers["Metadata-Flavor"] == "Google"


@pytest.mark.anyio
async def test_the_production_vault_client_sends_no_authorization_when_the_service_auth_is_off(
    production_entry, metadata
):
    # X-37: 切ったとき(SERVICE_AUTH_ENABLED=false)は、Authorization ヘッダが付かず、メタデータサーバも呼ばない。
    app = production_entry(**{SERVICE_AUTH_ENV: "false"})

    await app.state.services.vault.expire(_EXPIRE_PATH_NID)

    (request,) = production_entry.vault.requests
    assert "authorization" not in request.headers
    assert metadata.requests == []


@pytest.mark.anyio
async def test_a_vault_call_fails_as_unavailable_when_the_token_cannot_be_obtained(
    monkeypatch, default_db, now, production_entry
):
    # X-37: トークンを取れないとき、金庫の呼び出しは「一時的に応えない」(VaultUnavailableError)になる。レフェリーは、
    # タスクを落とさず、待って読み直す。金庫(スタブ)には届かない。
    monkeypatch.setattr(
        service_auth_module,
        "IdTokenProvider",
        lambda: IdTokenProvider(transport=httpx.MockTransport(lambda request: httpx.Response(503)), now=now),
    )
    app = production_entry()

    with pytest.raises(VaultUnavailableError):
        await app.state.services.vault.expire(_EXPIRE_PATH_NID)

    assert production_entry.vault.requests == []


# --- agents の呼び出し(agents.client.send_turn) ---


def _turn_input() -> TurnInput:
    return TurnInput.model_validate_json(json.dumps(valid_data("candidate")))


@pytest.fixture
def agents_calls(monkeypatch, agents_app) -> RecordingTransport:
    """send_turn が使う HTTP の通信路を、本物の agents の app(スタブの LLM)につなぎ、送ったリクエストを記録する。"""
    transport = RecordingTransport(httpx.ASGITransport(app=agents_app))
    monkeypatch.setattr(
        agents_client_module,
        "_open_http_client",
        lambda timeout_s: httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout_s)),
    )
    return transport


@pytest.mark.anyio
async def test_send_turn_sends_an_id_token_for_the_agents_url_when_given_an_auth(agents_calls, provider, metadata):
    # X-37: send_turn に auth を渡すと、agents の呼び出しに Authorization: Bearer <ID トークン> が付く。
    await send_turn(
        AGENTS_URL, "candidate", _turn_input(), nid=NID, timeout_s=5, auth=IdTokenAuth(provider, AGENTS_URL)
    )

    (request,) = agents_calls.requests
    assert str(request.url) == f"{AGENTS_URL}/a2a/candidate"
    token = _bearer(request)
    assert token is not None and _claims(token)["aud"] == AGENTS_URL
    assert metadata.audiences == [AGENTS_URL]


@pytest.mark.anyio
async def test_send_turn_sends_no_authorization_without_an_auth(agents_calls, metadata):
    # X-37: auth を渡さなければ(ローカル・テスト)、ヘッダは付かず、メタデータサーバも呼ばない。
    await send_turn(AGENTS_URL, "candidate", _turn_input(), nid=NID, timeout_s=5)

    (request,) = agents_calls.requests
    assert "authorization" not in request.headers
    assert metadata.requests == []


@pytest.mark.anyio
async def test_the_web_binding_derives_the_agents_audience_from_the_base_url(agents_calls, provider, metadata):
    # X-37: web が agents を呼ぶ関数(bind_agents_client)は、束ねた base_url から audience を作る。末尾のスラッシュがあっても、
    # audience に入らない。provider を渡さなければ、認証を付けない。
    secured = bind_agents_client(AGENTS_URL + "/", provider)
    plain = bind_agents_client(AGENTS_URL + "/")

    await secured("candidate", _turn_input(), nid=NID, timeout_s=5)
    await plain("candidate", _turn_input(), nid=NID, timeout_s=5)

    with_auth, without_auth = agents_calls.requests
    assert _claims(_bearer(with_auth))["aud"] == AGENTS_URL
    assert "authorization" not in without_auth.headers
    assert metadata.audiences == [AGENTS_URL]


@pytest.mark.anyio
async def test_a_failure_to_obtain_the_token_is_a_connection_error_for_the_referee_to_retry(
    agents_calls, now, stub_llm
):
    # X-37: トークンを取れないとき、send_turn は ConnectionError(一時的な失敗。レフェリーが再試行する)。agents には届かない。
    broken = IdTokenProvider(transport=httpx.MockTransport(lambda request: httpx.Response(503)), now=now)

    with pytest.raises(ConnectionError) as excinfo:
        await send_turn(AGENTS_URL, "candidate", _turn_input(), nid=NID, timeout_s=5, auth=IdTokenAuth(broken, AGENTS_URL))

    assert type(excinfo.value) is ConnectionError
    assert agents_calls.requests == [] and stub_llm.requests == []


# --- 結合: web のレフェリー → 金庫と agents の両方(audience が呼び先ごとに正しい) ---


async def _run_demo_until_judged(env, store, stub_llm) -> str:
    """デモの交渉を作り、レフェリーが金庫と agents を呼んで判定まで進めるのを待つ。"""
    remaining = [move_json("propose", PACKAGE), move_json("accept")]
    stub_llm.behavior = lambda _request: remaining.pop(0)
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    created = await env.browser().post(
        "/v1/demo/negotiations",
        {
            "request_id": "request-demo1",
            "candidate_template_id": candidate_template.template_id,
            "employer_template_id": employer_template.template_id,
        },
    )
    assert created.status_code == 200, created.text
    nid = created.json()["nid"]
    await asyncio.wait_for(env.services.referees.task(nid), 30)
    assert store.get_view(nid, "candidate").status == "judged"
    return nid


@pytest.mark.anyio
async def test_every_call_to_the_vault_and_to_agents_carries_the_token_for_its_own_service(
    store, clock, default_db, session_key, agents_app, stub_llm, monkeypatch, provider, metadata
):
    # X-37: 1 つの交渉(web のレフェリー → agents、web → 金庫)で、金庫への呼び出しはすべて金庫の URL を audience にした
    # トークンを、agents への呼び出しはすべて agents の URL を audience にしたトークンを付ける。互いのトークンは混ざらない。
    vault_url, agents_url = "http://vault.test", "http://agents.test"
    vault_calls = RecordingTransport(httpx.ASGITransport(app=create_vault_app(store)))
    agents_calls = RecordingTransport(httpx.ASGITransport(app=agents_app))
    monkeypatch.setattr(
        agents_client_module,
        "_open_http_client",
        lambda timeout_s: httpx.AsyncClient(transport=agents_calls, timeout=httpx.Timeout(timeout_s)),
    )
    vault = VaultClient(
        httpx.AsyncClient(transport=vault_calls, base_url=vault_url, auth=IdTokenAuth(provider, vault_url))
    )
    env = build_web_env(
        store=store,
        clock=clock,
        vault=vault,
        default_db=default_db,
        session_key=session_key,
        agents_base_url=agents_url,
        use_stub_agents=False,
        run_referees=True,
        token_provider=provider,
    )
    try:
        await _run_demo_until_judged(env, store, stub_llm)
    finally:
        await env.aclose()

    assert len(vault_calls.requests) > 5 and len(agents_calls.requests) == 2  # 候補者側・求人側の 2 回
    vault_tokens = {_bearer(request) for request in vault_calls.requests}
    agents_tokens = {_bearer(request) for request in agents_calls.requests}
    assert None not in vault_tokens | agents_tokens  # どの呼び出しにもヘッダが付いている
    assert {_claims(token)["aud"] for token in vault_tokens} == {vault_url}
    assert {_claims(token)["aud"] for token in agents_tokens} == {agents_url}
    assert vault_tokens.isdisjoint(agents_tokens)
    assert sorted(metadata.audiences) == [agents_url, vault_url]  # 呼び先ごとに 1 回ずつ。あとは使い回し


@pytest.mark.anyio
async def test_nothing_carries_a_token_when_the_service_auth_is_off(
    store, clock, default_db, session_key, agents_app, stub_llm, monkeypatch, metadata
):
    # X-37: 切ったとき(認証を渡さないとき)は、金庫にも agents にも、ヘッダが付かない。メタデータサーバは呼ばれない。
    vault_url, agents_url = "http://vault.test", "http://agents.test"
    vault_calls = RecordingTransport(httpx.ASGITransport(app=create_vault_app(store)))
    agents_calls = RecordingTransport(httpx.ASGITransport(app=agents_app))
    monkeypatch.setattr(
        agents_client_module,
        "_open_http_client",
        lambda timeout_s: httpx.AsyncClient(transport=agents_calls, timeout=httpx.Timeout(timeout_s)),
    )
    env = build_web_env(
        store=store,
        clock=clock,
        vault=VaultClient(httpx.AsyncClient(transport=vault_calls, base_url=vault_url)),
        default_db=default_db,
        session_key=session_key,
        agents_base_url=agents_url,
        use_stub_agents=False,
        run_referees=True,
    )
    try:
        await _run_demo_until_judged(env, store, stub_llm)
    finally:
        await env.aclose()

    assert vault_calls.requests and agents_calls.requests
    assert all("authorization" not in request.headers for request in vault_calls.requests + agents_calls.requests)
    assert metadata.requests == []


@pytest.mark.anyio
async def test_the_token_is_not_logged(caplog, provider, metadata):
    # X-37: トークンの値は、ログに書かない(取る・使い回すどの段でも)。
    caplog.set_level(logging.DEBUG)
    token = await provider.token(VAULT_URL)
    await provider.token(VAULT_URL)

    assert token not in caplog.text
