"""scripts/verify_attestation.py(AC-23。design.md §12.1、契約 research/tee-spike-contract.md §12)と、scripts/tee_probe_client.py。

金庫には、本物の TLS で話す偽物(tests/attestation_helpers.py の FakeVaultServer)をつなぐ(--direct)。web は httpx.MockTransport の偽物(--web)。
Google の署名鍵の代わりに、手元の鍵の証明書を渡す。GCP にも Google にも GitHub にも接続しない。確かめること:

- 全部通れば終了コード 0 と claims の表・コミット。1 つでも違えば終了コード 1 と reason(署名を 1 バイト壊す・nonce 違い・証明書違い・
  debug・digest が表にない・期限切れ・プロジェクト違い・サービスアカウント違い・ハードウェア違い)。
- --web は、web の「verified」という申告を信用せず、トークンを自分で検証する。--direct は、控えた証明書の結び付きまで確かめる。
- --project と --service-account は必須(第三者の検証が、web の検証より弱くならないように)。
- --allow-debug は debug のトークンを通し、警告を出す。--print-token がなければ、標準出力にも標準エラーにも、トークンの値は出ない。
- --check-commit は、GITHUB_REPO_URL が要る。GitHub にコミットがなければ 1。
- tee_probe_client.py は、import でき、環境変数が欠けていれば、何も呼ばずに分かりやすく失敗する。偽の金庫に対しては、期待どおりの終了コードを返す。
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx
import pytest
from attestation_helpers import (
    BUILT_AT,
    COMMIT,
    DIGEST,
    PROJECT_ID,
    REJECTED_REASONS,
    SERVICE_ACCOUNT,
    attestation_payload,
    bad_token,
    certs_of,
    fake_vault,  # noqa: F401  (フィクスチャは import して使う)
    flip_signature_byte,
    mint_token,
    token_with,
)

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))
import tee_probe_client  # noqa: E402  (scripts/ を import できるようにしてから読む)
import verify_attestation as script  # noqa: E402

REPO = "https://github.com/acme/vault"
CERT_HASH = "e" * 64
NONCE_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")


@pytest.fixture
def releases_file(tmp_path) -> Path:
    path = tmp_path / "releases.json"
    path.write_text(
        json.dumps({"releases": [{"digest": DIGEST, "commit": COMMIT, "built_at": BUILT_AT, "status": "active"}]})
    )
    return path


@pytest.fixture
def common(releases_file) -> list[str]:
    return ["--project", PROJECT_ID, "--service-account", SERVICE_ACCOUNT, "--releases", str(releases_file)]


def run_direct(vault, common, *extra, environ=None, http_transport=None) -> int:
    return script.main(
        ["--direct", vault.url, *common, *extra], signer_certs=certs_of(), environ=environ or {}, http_transport=http_transport
    )


def fake_web(token_for_nonce, *, status: int = 200, seen: list | None = None) -> httpx.MockTransport:
    """web の GET /api/tee/attestation の偽物。要求の nonce から、トークンを作って返す(web の申告は verified: true と言い張る)。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"detail": "rate_limited"})
        nonce = request.url.params["nonce"]
        return httpx.Response(200, json={"verified": True, "reason": None, "nonce": nonce, "token": token_for_nonce(nonce)})

    return httpx.MockTransport(handler)


def good_web_token(nonce: str) -> str:
    return mint_token(attestation_payload([nonce, CERT_HASH], now=time.time()))


class TokenSpy:
    """偽の金庫の token_factory を包み、返したトークンを覚える(標準出力・標準エラーに出ていないことの確認用)。"""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.tokens: list[str] = []

    def __call__(self, nonce: str, cert: str) -> str:
        token = self.factory(nonce, cert)
        self.tokens.append(token)
        return token


# --- --direct -------------------------------------------------------------------------------------------------


def test_direct_passes_with_exit_code_zero_and_prints_the_claims_the_commit_and_the_certificate_binding(
    fake_vault, common, capsys
):
    spy = TokenSpy(token_with())
    fake_vault.token_factory = spy

    code = run_direct(fake_vault, common, environ={"GITHUB_REPO_URL": REPO})

    out, err = capsys.readouterr()
    assert code == 0
    assert "OK" in out
    for expected in (DIGEST, "GCP_AMD_SEV", "CONFIDENTIAL_SPACE", "disabled-since-boot", "STABLE", "asia-northeast1-b", COMMIT):
        assert expected in out
    assert f"{REPO}/commit/{COMMIT}" in out
    assert fake_vault.material.certificate_sha256 in out  # 今の TLS の相手と、トークンの結び付きまで確かめた
    (request,) = fake_vault.attestation_requests
    assert NONCE_FORMAT.fullmatch(request.query["nonce"][0]) and "authorization" not in request.headers
    (token,) = spy.tokens
    assert token not in out and token not in err  # トークンの値は、--print-token のときだけ


def test_direct_uses_a_fresh_nonce_every_run(fake_vault, common):
    assert run_direct(fake_vault, common) == 0
    assert run_direct(fake_vault, common) == 0

    first, second = (request.query["nonce"][0] for request in fake_vault.attestation_requests)
    assert first != second


@pytest.mark.parametrize("reason", REJECTED_REASONS)
def test_direct_fails_with_exit_code_one_and_the_reason(fake_vault, common, capsys, reason):
    spy = TokenSpy(bad_token(reason))
    fake_vault.token_factory = spy

    code = run_direct(fake_vault, common)

    out, err = capsys.readouterr()
    assert code == 1
    assert f"reason={reason}" in out and "OK" not in out
    (token,) = spy.tokens
    assert token not in out and token not in err


def test_direct_checks_the_certificate_the_script_itself_saw_not_the_one_the_vault_reports(fake_vault, common, capsys):
    # 金庫の応答の certificate_sha256(参考値)を正しく見せても、トークンの eat_nonce が別の証明書なら、拒否する。
    fake_vault.token_factory = bad_token("certificate")
    assert fake_vault.attestation_status == 200

    assert run_direct(fake_vault, common) == 1
    assert "reason=certificate" in capsys.readouterr().out


def test_direct_prints_the_unverified_claims_of_a_rejected_token_with_a_warning_not_to_trust_them(fake_vault, common, capsys):
    fake_vault.token_factory = bad_token("signature")

    assert run_direct(fake_vault, common) == 1

    out = capsys.readouterr().out
    assert "信用しないでください" in out and DIGEST in out  # 取れた範囲の claims(署名を確かめていない)


def test_control_characters_in_a_rejected_token_do_not_reach_the_terminal(fake_vault, common, capsys):
    def evil(nonce: str, cert: str) -> str:
        payload = attestation_payload([nonce, cert], now=time.time())
        payload["hwmodel"] = "\x1b[2J\x1b[31mpwned"
        return flip_signature_byte(mint_token(payload))

    fake_vault.token_factory = evil

    assert run_direct(fake_vault, common) == 1

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\\x1b" in out


@pytest.mark.parametrize("status", [429, 503])
def test_direct_fails_when_the_vault_refuses_to_attest(fake_vault, common, capsys, status):
    fake_vault.attestation_status = status

    assert run_direct(fake_vault, common) == 1
    assert str(status) in capsys.readouterr().err


def test_direct_fails_when_the_vault_cannot_be_reached(fake_vault, common, capsys):
    fake_vault.stop()

    assert run_direct(fake_vault, common) == 1
    assert "could not call the vault" in capsys.readouterr().err


def test_direct_needs_an_https_url(common, capsys):
    assert script.main(["--direct", "http://localhost:8443", *common], signer_certs=certs_of(), environ={}) == 1
    assert "https://" in capsys.readouterr().err


# --- 照合する値: プロジェクト・サービスアカウント・ハードウェア・digest ----------------------------------------------------


def test_the_project_and_the_service_account_are_required(releases_file):
    for argv in (
        ["--web", "https://web.test"],
        ["--web", "https://web.test", "--project", PROJECT_ID],
        ["--web", "https://web.test", "--service-account", SERVICE_ACCOUNT],
    ):
        with pytest.raises(SystemExit) as excinfo:
            script.main(argv, signer_certs=certs_of(), environ={})
        assert excinfo.value.code == 2  # 使い方の誤り


def test_exactly_one_of_web_and_direct_is_required(common):
    for argv in ([], ["--web", "https://a.test", "--direct", "https://b.test"]):
        with pytest.raises(SystemExit) as excinfo:
            script.main([*argv, *common], signer_certs=certs_of(), environ={})
        assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("option", "value", "reason"),
    [
        ("--project", "somebody-elses-project", "project"),
        ("--service-account", "attacker@somebody-elses-project.iam.gserviceaccount.com", "service_account"),
        ("--hwmodel", "GCP_INTEL_TDX", "hwmodel"),
    ],
)
def test_a_value_that_does_not_match_the_token_fails(fake_vault, releases_file, capsys, option, value, reason):
    argv = ["--project", PROJECT_ID, "--service-account", SERVICE_ACCOUNT, "--releases", str(releases_file)]
    if option == "--hwmodel":
        argv += ["--hwmodel", value]  # token は AMD SEV。TDX だけを許可する
    else:
        argv[argv.index(option) + 1] = value

    assert script.main(["--direct", fake_vault.url, *argv], signer_certs=certs_of(), environ={}) == 1
    assert f"reason={reason}" in capsys.readouterr().out


def test_hwmodel_can_be_repeated(fake_vault, common):
    assert run_direct(fake_vault, common, "--hwmodel", "GCP_INTEL_TDX", "--hwmodel", "GCP_AMD_SEV") == 0


def test_a_digest_that_is_not_in_the_releases_table_fails(fake_vault, tmp_path, capsys):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"releases": []}))
    argv = ["--project", PROJECT_ID, "--service-account", SERVICE_ACCOUNT, "--releases", str(empty)]

    assert script.main(["--direct", fake_vault.url, *argv], signer_certs=certs_of(), environ={}) == 1
    assert "reason=image_digest" in capsys.readouterr().out


def test_a_revoked_digest_fails_as_image_digest(fake_vault, tmp_path, capsys):
    revoked = tmp_path / "revoked.json"
    revoked.write_text(json.dumps({"releases": [{"digest": DIGEST, "commit": COMMIT, "status": "revoked"}]}))
    argv = ["--project", PROJECT_ID, "--service-account", SERVICE_ACCOUNT, "--releases", str(revoked)]

    assert script.main(["--direct", fake_vault.url, *argv], signer_certs=certs_of(), environ={}) == 1
    assert "reason=image_digest" in capsys.readouterr().out


def test_a_missing_releases_file_fails_without_calling_the_vault(fake_vault, tmp_path, capsys):
    argv = ["--project", PROJECT_ID, "--service-account", SERVICE_ACCOUNT, "--releases", str(tmp_path / "missing.json")]

    assert script.main(["--direct", fake_vault.url, *argv], signer_certs=certs_of(), environ={}) == 1
    assert "digest の表を読めません" in capsys.readouterr().err
    assert fake_vault.requests == []


# --- --allow-debug と --print-token --------------------------------------------------------------------------------


def debug_token(nonce: str, cert: str) -> str:
    payload = attestation_payload([nonce, cert], now=time.time())
    payload["dbgstat"] = "enabled"
    payload["submods"]["confidential_space"] = {}  # debug イメージには support_attributes が付かない
    return mint_token(payload)


def test_a_debug_token_is_refused_by_default_and_accepted_with_allow_debug_with_a_warning(fake_vault, common, capsys):
    fake_vault.token_factory = debug_token

    assert run_direct(fake_vault, common) == 1
    assert "reason=debug" in capsys.readouterr().out

    assert run_direct(fake_vault, common, "--allow-debug") == 0
    out, err = capsys.readouterr()
    assert "OK" in out and "enabled" in out
    assert err.count("警告") >= 2  # --allow-debug の警告と、「このトークンは本番イメージのものではありません」の警告


def test_allow_debug_does_not_relax_the_other_checks(fake_vault, common, capsys):
    fake_vault.token_factory = bad_token("image_digest")

    assert run_direct(fake_vault, common, "--allow-debug") == 1
    assert "reason=image_digest" in capsys.readouterr().out


def test_the_token_is_printed_only_with_print_token(fake_vault, common, capsys):
    spy = TokenSpy(token_with())
    fake_vault.token_factory = spy

    assert run_direct(fake_vault, common) == 0
    plain_out, plain_err = capsys.readouterr()
    assert run_direct(fake_vault, common, "--print-token") == 0
    out, _err = capsys.readouterr()

    first, second = spy.tokens
    assert first not in plain_out and first not in plain_err
    assert second in out.splitlines()  # 1 行で出る


def test_print_token_also_prints_the_token_of_a_rejected_attestation(fake_vault, common, capsys):
    spy = TokenSpy(bad_token("debug"))
    fake_vault.token_factory = spy

    assert run_direct(fake_vault, common, "--print-token") == 1
    out, _err = capsys.readouterr()
    assert spy.tokens[0] in out.splitlines()


# --- --web ----------------------------------------------------------------------------------------------------


def test_web_passes_and_says_that_the_certificate_binding_is_not_checked_from_here(common, capsys):
    seen: list[httpx.Request] = []
    token_spy = TokenSpy(lambda nonce, cert: good_web_token(nonce))

    code = script.main(
        ["--web", "https://web.test/", *common],
        signer_certs=certs_of(),
        environ={},
        http_transport=fake_web(lambda nonce: token_spy(nonce, CERT_HASH), seen=seen),
    )

    out, err = capsys.readouterr()
    assert code == 0 and "OK" in out and DIGEST in out and COMMIT in out
    assert "証明書の結び付き" in out  # web 経由では確かめていないことを、はっきり言う
    (request,) = seen
    assert request.url.path == "/api/tee/attestation" and NONCE_FORMAT.fullmatch(request.url.params["nonce"])
    assert token_spy.tokens[0] not in out and token_spy.tokens[0] not in err


def test_web_does_not_trust_the_verified_flag_of_the_web_app(common, capsys):
    # web が verified: true と言い張っても、トークンが改ざんされていれば拒否する。
    tampered = fake_web(lambda nonce: flip_signature_byte(good_web_token(nonce)))

    code = script.main(["--web", "https://web.test", *common], signer_certs=certs_of(), environ={}, http_transport=tampered)

    assert code == 1 and "reason=signature" in capsys.readouterr().out


def test_web_fails_when_the_token_was_made_for_another_nonce(common, capsys):
    replay = fake_web(lambda nonce: good_web_token("z" * 43))  # web が、古いトークンを使い回した

    code = script.main(["--web", "https://web.test", *common], signer_certs=certs_of(), environ={}, http_transport=replay)

    assert code == 1 and "reason=nonce" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (fake_web(good_web_token, status=429), "429"),
        (httpx.MockTransport(lambda request: httpx.Response(200, json={"verified": True})), "no token"),
        (httpx.MockTransport(lambda request: httpx.Response(200, text="<html>")), "not JSON"),
        (httpx.MockTransport(lambda request: httpx.Response(200, json={"token": 5})), "no token"),
        (httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))), "could not call the web app"),
    ],
    ids=["rate_limited", "no_token", "not_json", "token_not_a_string", "unreachable"],
)
def test_web_fails_when_it_does_not_return_a_token(common, capsys, transport, message):
    code = script.main(["--web", "https://web.test", *common], signer_certs=certs_of(), environ={}, http_transport=transport)

    assert code == 1 and message in capsys.readouterr().err


# --- --check-commit -------------------------------------------------------------------------------------------


def github(status: int, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={})

    return httpx.MockTransport(handler)


def test_check_commit_confirms_the_commit_exists_on_github(fake_vault, common, capsys):
    seen: list[httpx.Request] = []

    code = run_direct(fake_vault, common, "--check-commit", environ={"GITHUB_REPO_URL": REPO + ".git"}, http_transport=github(200, seen))

    assert code == 0 and "GitHub にそのコミットがある" in capsys.readouterr().out
    (request,) = seen
    assert str(request.url) == f"https://api.github.com/repos/acme/vault/commits/{COMMIT}"


@pytest.mark.parametrize("status", [404, 500])
def test_check_commit_fails_when_github_has_no_such_commit(fake_vault, common, capsys, status):
    code = run_direct(fake_vault, common, "--check-commit", environ={"GITHUB_REPO_URL": REPO}, http_transport=github(status))

    assert code == 1 and "見つかりません" in capsys.readouterr().out


def test_check_commit_fails_when_github_cannot_be_reached(fake_vault, common, capsys):
    def unreachable(request):
        raise httpx.ConnectError("refused")

    code = run_direct(
        fake_vault, common, "--check-commit", environ={"GITHUB_REPO_URL": REPO}, http_transport=httpx.MockTransport(unreachable)
    )

    assert code == 1 and "問い合わせられませんでした" in capsys.readouterr().out


def test_check_commit_needs_the_repository_url_and_stops_before_calling_anything(fake_vault, common, capsys):
    assert run_direct(fake_vault, common, "--check-commit", environ={}) == 1

    assert "GITHUB_REPO_URL" in capsys.readouterr().err
    assert fake_vault.requests == []


def test_the_commit_is_not_checked_on_github_without_the_flag(fake_vault, common):
    seen: list[httpx.Request] = []

    assert run_direct(fake_vault, common, environ={"GITHUB_REPO_URL": REPO}, http_transport=github(404, seen)) == 0
    assert seen == []


# --- tee_probe_client.py --------------------------------------------------------------------------------------


def test_the_probe_client_fails_with_a_clear_message_when_the_environment_is_incomplete(capsys):
    assert tee_probe_client.main({}) == 1
    err = capsys.readouterr().err
    assert "VAULT_BASE_URL" in err and "VAULT_SERVICE_ACCOUNT" in err

    assert tee_probe_client.main({"VAULT_BASE_URL": "https://10.10.0.10:8443", "VAULT_SERVICE_ACCOUNT": "  "}) == 1
    err = capsys.readouterr().err
    assert "VAULT_SERVICE_ACCOUNT" in err and "VAULT_BASE_URL" not in err


class StubIdTokenProvider:
    """メタデータサーバの代わり(claim を出力できる ID トークンを返す)。"""

    def __init__(self) -> None:
        self.audiences: list[str] = []

    async def token(self, audience: str) -> str:
        self.audiences.append(audience)
        claims = {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "azp": "1234567890",
            "email": SERVICE_ACCOUNT,
            "email_verified": True,
            "exp": int(time.time()) + 3600,
        }
        return mint_token(claims)

    def invalidate(self, audience, token=None) -> None:
        pass


@pytest.fixture
def probe(monkeypatch, fake_vault, releases_file):
    provider = StubIdTokenProvider()
    monkeypatch.setattr(tee_probe_client, "IdTokenProvider", lambda: provider)
    monkeypatch.setattr(tee_probe_client, "SignerCerts", lambda url: certs_of())
    environ = {
        "VAULT_BASE_URL": fake_vault.url,
        "VAULT_SERVICE_ACCOUNT": SERVICE_ACCOUNT,
        "GOOGLE_CLOUD_PROJECT": PROJECT_ID,
        "VAULT_RELEASES_FILE": str(releases_file),
    }
    return environ, provider


def test_the_probe_client_passes_against_a_vault_that_attests_and_authorizes_as_expected(probe, fake_vault, capsys):
    environ, provider = probe

    code = tee_probe_client.main(environ)

    out, err = capsys.readouterr()
    assert code == 0
    assert set(provider.audiences) == {"https://vault.anon-nego.internal"}  # 固定の audience(金庫の IP の URL からは作らない)
    for expected in ("[1/4]", "iss", "azp", "email", SERVICE_ACCOUNT, "email_verified", "[2/4] OK", DIGEST, "[3/4]", "404", "[4/4]", "401", "合格"):
        assert expected in out
    # ID トークンの値も、attestation トークンの値も出さない
    assert "eyJ" not in out and err == ""  # JWT は、ヘッダの base64url が eyJ で始まる
    # 順序: 検証(認証なし)→ Bearer あり → Bearer なし
    sent = [(request.path, "authorization" in request.headers) for request in fake_vault.requests]
    assert sent == [("/v1/attestation", False), ("/v1/principals/0000000000000000/policy", True), ("/v1/principals/0000000000000000/policy", False)]


def test_the_probe_client_fails_when_the_vault_does_not_reject_a_call_without_a_token(probe, fake_vault, capsys):
    environ, _provider = probe
    fake_vault.authorization_required = False  # 認可の壊れた金庫

    assert tee_probe_client.main(environ) == 1
    out = capsys.readouterr().out
    assert "[4/4] Bearer なし: 404" in out and "不合格" in out


def test_the_probe_client_fails_when_the_attestation_fails_and_still_reports_the_rest(probe, fake_vault, capsys):
    environ, _provider = probe
    fake_vault.token_factory = bad_token("debug")

    assert tee_probe_client.main(environ) == 1
    out = capsys.readouterr().out
    assert "[2/4] NG" in out and "reason=debug" in out and "不合格" in out
    assert "/v1/principals/0000000000000000/policy" not in [request.path for request in fake_vault.requests]  # 検証が通らない金庫には、要求を送らない


def test_the_probe_client_accepts_a_debug_image_only_when_told_to(probe, fake_vault, capsys):
    environ, _provider = probe
    fake_vault.token_factory = debug_token

    assert tee_probe_client.main(environ) == 1
    assert "reason=debug" in capsys.readouterr().out

    assert tee_probe_client.main({**environ, "TEE_PROBE_ALLOW_DEBUG": "true"}) == 0
