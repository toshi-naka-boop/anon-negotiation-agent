"""依頼者のセッションクッキー(design.md §6.3): 署名・期限・クッキーの属性・鍵の扱い。

DV-01・DV-16 が確かめる「ID の発行の条件」「クッキーの延長」とは別に、ここでは、クッキーそのものの性質を
確かめる: 署名付き・HttpOnly・Secure・SameSite=Lax・寿命 29 日、署名の中にも期限を入れ、サーバ側でも
期限切れを受け付けない、署名の鍵はコードにも既定値にも持たず、なければ起動を拒否する。
"""

import datetime as dt
import re
import secrets
from pathlib import Path

import pytest

import web.app as web_app_module
from agents.wire import ROLES
from web.app import bind_agents_client, create_app, create_app_from_env
from web.config import DEFAULT_WEB_CONFIG, load_web_config
from web.session import (
    MIN_SESSION_KEY_BYTES,
    SESSION_COOKIE_NAME,
    SESSION_KEY_ENV,
    MissingSessionKeyError,
    SessionCodec,
    WeakSessionKeyError,
    load_session_key,
    validate_session_key,
)

_PID = "0123456789abcdef"
_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
_MAX_AGE = 29 * 24 * 3600
# 本番と同じ条件(base64url で 32 バイト以上。台帳 X-39)を満たす、テスト用の鍵(`secrets.token_urlsafe(32)` で作った値)。
_KEY = "oRXjuLrpBpmCPe_O9zLtE1ZhEXY7BAssKXRaJDPp1fg"
_OTHER_KEY = "4lNFNQsa4lwGod8A39IKlAQ2fCIdRPgGra7q8CQGuj0"
_KEY_RECIPE = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'


def _codec(key: str = _KEY) -> SessionCodec:
    return SessionCodec(key, max_age_seconds=_MAX_AGE)


def test_the_cookie_lifetime_is_29_days_and_shorter_than_the_retention_by_a_day():
    # §6.3: 寿命(Max-Age)は暫定 29 日。データの自動削除(30 日)より 1 日早く切れる。
    config = DEFAULT_WEB_CONFIG
    assert config.session.cookie_max_age_seconds == 29 * 24 * 3600
    assert config.principals.retention_seconds == 30 * 24 * 3600
    assert config.principals.touch_interval_seconds == 3600
    assert config.principal_sweeper.interval_seconds == 600
    assert config.retention.fictional_stage_ttl_seconds == 96 * 3600
    assert config.principals.retention_seconds - config.session.cookie_max_age_seconds >= 24 * 3600


def test_config_rejects_a_cookie_lifetime_that_is_not_shorter_than_the_retention(tmp_path: Path):
    # クッキーの寿命がデータの保持より短くなければ、まだ使えるクッキーを持つ人のデータを見回りが消し始めうる。
    # 設定を書き換えて、その組み合わせを読み込めないことを確かめる。
    source = Path(__file__).resolve().parents[1] / "config" / "params.toml"
    text = source.read_text(encoding="utf-8")
    broken = re.sub(r"cookie_max_age_seconds = \d+", "cookie_max_age_seconds = 2592000", text)  # 30 日(同じ)
    assert broken != text
    path = tmp_path / "params.toml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ValueError, match="cookie lifetime"):
        load_web_config(path)


def test_the_cookie_value_is_signed_and_carries_the_principal_id_and_an_expiry():
    # §6.3: 署名の中にも期限を入れる。同じ鍵・期限内なら、依頼者 ID が読める。
    codec = _codec()
    token = codec.issue(_PID, _NOW)

    assert codec.read(token, _NOW) == _PID
    assert codec.read(token, _NOW + dt.timedelta(seconds=_MAX_AGE - 1)) == _PID


def test_a_tampered_cookie_is_rejected():
    # 署名が合わないクッキー(値を書き換えた・別の鍵で署名した・でたらめ)は、無効として扱う。
    codec = _codec()
    token = codec.issue(_PID, _NOW)
    payload, signature = token.rsplit(".", 1)
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")

    assert codec.read(f"{payload}.{flipped}", _NOW) is None
    assert codec.read(_codec(_OTHER_KEY).issue(_PID, _NOW), _NOW) is None
    assert codec.read("not-a-cookie", _NOW) is None
    assert codec.read("", _NOW) is None
    assert codec.read(None, _NOW) is None


def test_the_server_rejects_an_expired_cookie_even_if_the_browser_still_sends_it():
    # §6.3: サーバ側でも期限切れのクッキーを受け付けない(署名の中の期限で判断する)。
    codec = _codec()
    token = codec.issue(_PID, _NOW)

    assert codec.read(token, _NOW + dt.timedelta(seconds=_MAX_AGE - 1)) == _PID
    assert codec.read(token, _NOW + dt.timedelta(seconds=_MAX_AGE)) is None
    assert codec.read(token, _NOW + dt.timedelta(days=30)) is None


def test_a_payload_that_is_not_a_principal_id_is_rejected():
    # 署名が正しくても、依頼者 ID の形(16 桁の 16 進数)でない値は受け付けない。
    codec = _codec()
    with pytest.raises(ValueError):
        codec.issue("not-a-hex-id", _NOW)
    forged = codec._serializer.dumps({"pid": "../etc", "exp": int(_NOW.timestamp()) + 100})
    assert codec.read(forged, _NOW) is None
    no_expiry = codec._serializer.dumps({"pid": _PID})
    assert codec.read(no_expiry, _NOW) is None
    bool_expiry = codec._serializer.dumps({"pid": _PID, "exp": True})
    assert codec.read(bool_expiry, _NOW) is None


@pytest.mark.anyio
async def test_the_start_page_sets_a_signed_httponly_secure_lax_cookie_with_a_29_day_max_age(web_app):
    # §6.3: クッキーは、署名付き・HttpOnly・Secure・SameSite=Lax で、寿命(Max-Age)は 29 日。
    browser = web_app.browser()

    response = await browser.get("/start")

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    attributes = {part.strip().lower() for part in set_cookie.split(";")[1:]}
    assert "httponly" in attributes
    assert "secure" in attributes
    assert "samesite=lax" in attributes
    assert f"max-age={_MAX_AGE}" in attributes
    assert "path=/" in attributes
    # 署名の中の依頼者 ID と期限が、サーバの時計で読める。
    token = browser.cookie
    assert web_app.services.codec.read(token, web_app.clock.now()) == browser.pid
    assert re.fullmatch(r"[0-9a-f]{16}", browser.pid)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_a_cookie_past_its_expiry_is_treated_as_no_session_by_the_api(web_app):
    # §6.3: サーバ側でも期限切れを受け付けない。ブラウザが送り続けても、期限を過ぎれば 401。
    browser = web_app.browser()
    pid = await browser.register()
    assert (await browser.get(f"/v1/principals/{pid}/negotiations")).status_code == 200

    web_app.clock.advance(dt.timedelta(seconds=_MAX_AGE + 1))

    assert (await browser.get(f"/v1/principals/{pid}/negotiations")).status_code == 401


def test_the_signing_key_is_read_from_the_environment_and_has_no_default():
    # §6.3: 署名の鍵は環境変数から読む。コードに鍵を書かず、既定値も持たない。
    assert load_session_key({SESSION_KEY_ENV: _KEY}) == _KEY
    assert load_session_key({SESSION_KEY_ENV: f"  {_KEY}\n"}) == _KEY  # 前後の空白(Secret Manager の値の末尾の改行など)は取り除く
    for environ in ({}, {SESSION_KEY_ENV: ""}, {SESSION_KEY_ENV: "   "}):
        with pytest.raises(MissingSessionKeyError):
            load_session_key(environ)


# --- 台帳 X-39: 署名の鍵の強さ(base64url で 32 バイト以上でなければ、起動を拒否する) ---

_KEY_31_BYTES = secrets.token_urlsafe(31)  # 42 文字。base64url として読めるが 31 バイト
_KEY_32_BYTES = secrets.token_urlsafe(32)  # 43 文字。ちょうど 32 バイト(境目)

STRONG_KEYS = {
    "token_urlsafe_32": _KEY_32_BYTES,  # 推奨の作り方(パディングなし)
    "exactly_the_minimum_with_padding": _KEY_32_BYTES + "=",  # 32 バイトをパディング付きで書いた形
    "token_urlsafe_48": secrets.token_urlsafe(48),
    "token_urlsafe_64": secrets.token_urlsafe(64),
}

WEAK_KEYS = {
    "one_character": "a",
    "readable_phrase": "a-key-for-this-test-only",  # base64url として読めるが 18 バイト
    "31_bytes": _KEY_31_BYTES,  # 境目の 1 つ下
    "32_characters": "k" * 32,  # 文字数は 32 でも、デコードすると 24 バイト
    "standard_base64_alphabet": _KEY_32_BYTES[:-2] + "+/",  # 標準の base64 の文字(base64url ではない)
    "space_inside": _KEY_32_BYTES[:20] + " " + _KEY_32_BYTES[21:],
    "trailing_newline": _KEY_32_BYTES + "\n",
    "non_ascii": "é" * 43,
    "symbols_only": "!" * 64,
    "impossible_length": "A" * 45,  # 4 の倍数 + 1 文字は、base64 として読めない
    "wrong_padding": _KEY_32_BYTES + "==",  # 43 文字に必要なパディングは「=」1 つ
    "padding_only": "=" * 44,
}


@pytest.mark.parametrize("key", list(STRONG_KEYS.values()), ids=list(STRONG_KEYS))
def test_a_base64url_key_of_at_least_32_bytes_is_accepted_everywhere(key, default_db):
    # X-39(対照): 条件を満たす鍵は、読み込みでも、署名の道具でも、アプリの組み立てでも通る。
    # 以降の拒否が、鍵の長さ・形のためであること(ほかのためではないこと)を示す。
    assert MIN_SESSION_KEY_BYTES == 32
    validate_session_key(key)
    assert load_session_key({SESSION_KEY_ENV: key}) == key
    assert _codec(key).read(_codec(key).issue(_PID, _NOW), _NOW) == _PID
    assert create_app(vault=object(), default_db=default_db, session_key=key) is not None


@pytest.mark.parametrize("key", list(WEAK_KEYS.values()), ids=list(WEAK_KEYS))
def test_a_weak_signing_key_is_refused_by_every_way_of_starting_the_service(key, default_db, monkeypatch):
    # X-39: 弱い鍵(base64url として読めない・32 バイト未満)では起動しない。環境変数から読む経路も、アプリを直接
    # 組み立てる経路も、署名の道具そのものも、同じ条件で拒否する(MissingSessionKeyError とは別の、鍵が弱いという例外)。
    with pytest.raises(WeakSessionKeyError):
        validate_session_key(key)
    with pytest.raises(WeakSessionKeyError):
        SessionCodec(key, max_age_seconds=_MAX_AGE)
    with pytest.raises(WeakSessionKeyError):
        create_app(vault=object(), default_db=default_db, session_key=key)
    if key == key.strip():  # 前後の空白は、環境変数から読むときに取り除く(その確認は別のテスト)ので、ここでは除く
        with pytest.raises(WeakSessionKeyError):
            load_session_key({SESSION_KEY_ENV: key})
        # 本番の起動口は、金庫のクライアントも Firestore のクライアントも作る前に拒否する。
        monkeypatch.setattr(
            web_app_module, "_create_default_db", lambda: pytest.fail("built a resource before the key check")
        )
        with pytest.raises(WeakSessionKeyError):
            create_app_from_env({SESSION_KEY_ENV: key, "VAULT_BASE_URL": "http://vault.test"})


def test_the_weak_key_error_explains_how_to_make_a_key_and_never_contains_the_key():
    # X-39: エラーの文に、鍵の作り方(32 バイトの乱数を base64url にする)を書く。鍵の値は書かない。
    weak = "weak-key-value-that-must-not-leak"
    for call in (
        lambda: validate_session_key(weak),
        lambda: load_session_key({SESSION_KEY_ENV: weak}),
        lambda: SessionCodec(weak, max_age_seconds=_MAX_AGE),
    ):
        with pytest.raises(WeakSessionKeyError) as excinfo:
            call()
        message = str(excinfo.value)
        assert _KEY_RECIPE in message
        assert "base64url" in message and "32 bytes" in message and SESSION_KEY_ENV in message
        assert weak not in message
    with pytest.raises(MissingSessionKeyError) as missing:  # 鍵がないときも、作り方を示す
        load_session_key({})
    assert _KEY_RECIPE in str(missing.value)


def test_the_key_boundary_is_32_bytes():
    # X-39: 31 バイトは拒否、32 バイトは受け付ける(境目)。
    assert len(_KEY_31_BYTES) == 42 and len(_KEY_32_BYTES) == 43
    with pytest.raises(WeakSessionKeyError):
        validate_session_key(_KEY_31_BYTES)
    validate_session_key(_KEY_32_BYTES)


def test_the_source_code_contains_no_signing_key():
    # 鍵をコードに書かない(ソースの中に、鍵らしい長い文字列の代入がない)。
    root = Path(__file__).resolve().parents[1] / "src" / "web"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert not re.search(r"(secret|signing)_?key\s*[:=]\s*[\"'][^\"']{8,}[\"']", text, flags=re.IGNORECASE)


def test_the_app_refuses_to_start_without_a_signing_key(default_db):
    # 鍵がなければ起動を拒否する(空・空白だけも同じ)。
    for key in ("", "   "):
        with pytest.raises(MissingSessionKeyError):
            create_app(vault=object(), default_db=default_db, session_key=key)
    with pytest.raises(MissingSessionKeyError):
        SessionCodec("", max_age_seconds=_MAX_AGE)


def test_the_production_entry_point_reads_the_key_and_the_vault_url_from_the_environment(
    default_db, monkeypatch
):
    # create_app_from_env は、鍵がなければ起動を拒否し、金庫の URL もなければ拒否する。両方あれば組み立てる。
    monkeypatch.setattr(web_app_module, "_create_default_db", lambda: default_db)

    with pytest.raises(MissingSessionKeyError):
        create_app_from_env({"VAULT_BASE_URL": "http://vault.test"})
    with pytest.raises(RuntimeError, match="VAULT_BASE_URL"):
        create_app_from_env({SESSION_KEY_ENV: _KEY})

    app = create_app_from_env({SESSION_KEY_ENV: _KEY, "VAULT_BASE_URL": "http://vault.test"})
    assert app.state.services.codec is not None


@pytest.mark.anyio
@pytest.mark.parametrize("role", ROLES)
async def test_the_agents_client_is_bound_to_the_configured_base_url(role, monkeypatch):
    # §4.1: agents.client.send_turn に、設定の base_url を束ねて、レフェリーに差し込む。
    # サービス間の認証を渡さなければ、認証は付かない(ローカル・テスト。台帳 X-37。付く場合は tests/test_service_auth.py)。
    calls = []

    async def recorder(base_url, role_, turn_input, *, nid, timeout_s, auth=None):
        calls.append((base_url, role_, nid, timeout_s, auth))
        return {"ok": True}

    monkeypatch.setattr(web_app_module, "agents_send_turn", recorder)
    send_turn = bind_agents_client("http://agents.example")

    result = await send_turn(role, object(), nid="0123456789abcdef", timeout_s=5)

    assert result == {"ok": True}
    assert calls == [("http://agents.example", role, "0123456789abcdef", 5, None)]


@pytest.mark.anyio
async def test_a_request_is_refused_when_the_usage_record_cannot_be_checked(web_app, monkeypatch):
    # §6.3: 削除中かどうかを確かめられない間は、依頼者の操作を通さない(閉じる側に倒す)。何も書かない。
    browser = web_app.browser()
    pid = await browser.register()

    async def broken_touch(principal_id):
        raise RuntimeError("firestore is down")

    monkeypatch.setattr(web_app.services.meta, "touch", broken_touch)
    response = await browser.post(f"/v1/principals/{pid}/blocklist", {"blocklist": ["company-x"]})

    assert (response.status_code, response.json()) == (503, {"detail": "temporarily_unavailable"})
    assert web_app.store._principal_ref(pid).get().to_dict().get("blocklist", []) == []
