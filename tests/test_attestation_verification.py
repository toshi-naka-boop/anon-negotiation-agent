"""attestation トークンの検証(negotiation_core.attestation。design.md §9・§12.1 AC-23、契約 research/tee-spike-contract.md §10)と、
[vault.tee] の設定の読み込み(negotiation_core.tee_settings)。

本物の Google にも GCP にも接続しない: トークンは、手元で作った RSA 鍵で署名する(google.auth.crypt.RSASigner + google.auth.jwt.encode)。
Google の署名鍵の代わりに、その鍵の自己署名の証明書を `{kid: PEM}` として渡す。payload は、本物の Confidential Space のトークンの形
(iss・aud・exp・iat・eat_nonce・swname・swversion・dbgstat・hwmodel・google_service_accounts・submods)。確かめること:

- 正常系: 全項目が通り、VerifiedAttestation の各項目が取り出せる。eat_nonce は、文字列でも配列でも通る。
- 契約 §10 の 17 の理由(16 の確認。16 番目は zone と instance)のそれぞれが、1 つの条件だけを壊したトークンで、その理由を返す。
  さらに、順番どおりに確かめること(複数の条件が壊れたトークンは、表の順で最初の理由を返す)。
- 個別の条件: 形(malformed の種類)・署名(alg・kid・別の鍵)・期限(境界の 60 秒・欠落・壊れた値。now の差し込み)・nonce・
  証明書の結び付き(None のときは飛ばす)・上書き・サービスアカウント・--allow-debug 相当(require_production=False)。
- SignerCerts: キャッシュ・TTL・未知の kid のときだけの取り直し(間隔の制限)・取れないときの扱い。
- 例外の文にもログにも、トークンの値が入らない。
- digest → コミットの表(load_releases・release_for_digest)と、証明書のピン留めの部品。
"""

import base64
import hashlib
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from attestation_helpers import (
    COMMIT,
    DIGEST,
    INSTANCE,
    NOW,
    OTHER_DIGEST,
    PROJECT_ID,
    SERVICE_ACCOUNT,
    ZONE,
    attestation_payload,
    certs_of,
    flip_signature_byte,
    make_policy,
    make_tls_material,
    mint_token,
    signing_key,
)
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from negotiation_core.attestation import (
    UNAVAILABLE,
    VERIFICATION_REASONS,
    AttestationError,
    SignerCerts,
    active_digests,
    certificate_sha256_of_pem,
    decode_claims_unverified,
    load_releases,
    pinned_ssl_context,
    release_for_digest,
    summarize_claims,
    verify_attestation_token,
)
from negotiation_core.tee_settings import load_tee_settings

NONCE = "n" * 43
OTHER_NONCE = "m" * 43
CERT_HASH = "c" * 64
OTHER_CERT_HASH = "d" * 64


def _b64(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


@dataclass
class Case:
    """検証の 1 回分の入力。壊す関数(BREAKERS)が、1 か所ずつ書き換える。"""

    payload: dict = field(default_factory=lambda: attestation_payload([NONCE, CERT_HASH], now=NOW))
    policy: dict = field(default_factory=dict)  # make_policy への上書き
    nonce: str = NONCE
    certificate_sha256: str | None = CERT_HASH
    now: float = NOW
    tamper_signature: bool = False  # 署名を 1 バイト壊す
    garbage: bool = False  # JWT ですらない文字列にする(ほかの壊し方より優先)

    def token(self) -> str:
        if self.garbage:
            return "this-is-not-a-jwt"
        token = mint_token(self.payload)
        return flip_signature_byte(token) if self.tamper_signature else token

    def verify(self, *, certs=None):
        return verify_attestation_token(
            self.token(),
            certs=certs if certs is not None else certs_of(),
            policy=make_policy(**self.policy),
            nonce=self.nonce,
            certificate_sha256=self.certificate_sha256,
            now=self.now,
        )


def _gce(case: Case) -> dict:
    return case.payload["submods"]["gce"]


def _container(case: Case) -> dict:
    return case.payload["submods"]["container"]


def _break_malformed(case: Case) -> None:
    case.garbage = True


def _break_signature(case: Case) -> None:
    case.tamper_signature = True


def _break_expired(case: Case) -> None:
    case.now = NOW + 3600 + 61  # exp の許容のずれ(60 秒)をこえて遅い


def _break_audience(case: Case) -> None:
    case.payload["aud"] = "https://sts.googleapis.com"


def _break_issuer(case: Case) -> None:
    case.payload["iss"] = "https://evil.example"


def _break_nonce(case: Case) -> None:
    case.nonce = OTHER_NONCE


def _break_certificate(case: Case) -> None:
    case.certificate_sha256 = OTHER_CERT_HASH


def _break_swname(case: Case) -> None:
    case.payload["swname"] = "GCE"


def _break_debug(case: Case) -> None:
    case.payload["dbgstat"] = "enabled"


def _break_support_attributes(case: Case) -> None:
    case.payload["submods"]["confidential_space"]["support_attributes"] = ["LATEST", "USABLE"]


def _break_hwmodel(case: Case) -> None:
    case.payload["hwmodel"] = "SOME_OTHER_TEE"


def _break_image_digest(case: Case) -> None:
    _container(case)["image_digest"] = OTHER_DIGEST


def _break_override(case: Case) -> None:
    _container(case)["cmd_override"] = ["/bin/sh"]


def _break_project(case: Case) -> None:
    _gce(case)["project_id"] = "somebody-elses-project"


def _break_service_account(case: Case) -> None:
    case.payload["google_service_accounts"] = ["attacker@somebody-elses-project.iam.gserviceaccount.com"]


def _break_zone(case: Case) -> None:
    case.policy["zone"] = ZONE
    _gce(case)["zone"] = "us-central1-a"


def _break_instance(case: Case) -> None:
    case.policy["instance_name"] = INSTANCE
    _gce(case)["instance_name"] = "another-instance"


# 契約 §10 の表の順。VERIFICATION_REASONS と同じ並び(下のテストが確かめる)。
BREAKERS = {
    "malformed": _break_malformed,
    "signature": _break_signature,
    "expired": _break_expired,
    "audience": _break_audience,
    "issuer": _break_issuer,
    "nonce": _break_nonce,
    "certificate": _break_certificate,
    "swname": _break_swname,
    "debug": _break_debug,
    "support_attributes": _break_support_attributes,
    "hwmodel": _break_hwmodel,
    "image_digest": _break_image_digest,
    "override": _break_override,
    "project": _break_project,
    "service_account": _break_service_account,
    "zone": _break_zone,
    "instance": _break_instance,
}


def reason_of(case: Case, **kwargs) -> str:
    with pytest.raises(AttestationError) as excinfo:
        case.verify(**kwargs)
    return excinfo.value.reason


# --- 正常系 ---------------------------------------------------------------------------------------------------


def test_the_reasons_are_the_seventeen_of_the_contract_in_the_order_of_its_table():
    # 契約 §10: 16 の確認(16 番目は zone と instance の 2 つ)。ここで数えているのは、理由の名前と並び。
    assert tuple(BREAKERS) == VERIFICATION_REASONS
    assert VERIFICATION_REASONS == (
        "malformed", "signature", "expired", "audience", "issuer", "nonce", "certificate", "swname", "debug",
        "support_attributes", "hwmodel", "image_digest", "override", "project", "service_account", "zone", "instance",
    )  # fmt: skip


def test_a_real_shaped_token_passes_every_check_and_the_verified_fields_are_extracted():
    case = Case(policy={"zone": ZONE, "instance_name": INSTANCE})  # 任意の照合(zone・instance)も入れて通す

    verified = case.verify()

    assert verified.image_digest == DIGEST
    assert (verified.hwmodel, verified.swname, verified.dbgstat) == ("GCP_AMD_SEV", "CONFIDENTIAL_SPACE", "disabled-since-boot")
    assert verified.swversion == ("250800",)  # 実トークンでは文字列の配列
    assert verified.support_attributes == ("LATEST", "STABLE", "USABLE")
    assert (verified.project_id, verified.zone, verified.instance_name) == (PROJECT_ID, ZONE, INSTANCE)
    assert verified.service_accounts == (SERVICE_ACCOUNT,)
    assert (verified.issued_at, verified.expires_at) == (NOW, NOW + 3600)
    assert verified.nonces == (NONCE, CERT_HASH)
    assert verified.claims["submods"]["container"]["image_digest"] == DIGEST  # 全文


@pytest.mark.parametrize(
    "eat_nonce", [[NONCE, CERT_HASH], [CERT_HASH, "extra", NONCE]], ids=["array", "array_with_other_values"]
)
def test_eat_nonce_as_an_array_is_accepted(eat_nonce):
    case = Case()
    case.payload["eat_nonce"] = eat_nonce

    assert case.verify().nonces == tuple(eat_nonce)


def test_eat_nonce_as_a_string_is_accepted():
    # launcher は、nonce が 1 個なら文字列で返す(複数なら配列)。どちらの形も受け付ける。
    case = Case(certificate_sha256=None)
    case.payload["eat_nonce"] = NONCE

    assert case.verify().nonces == (NONCE,)


def test_a_string_eat_nonce_is_also_checked_against_the_certificate_hash_when_one_is_given():
    case = Case(certificate_sha256=CERT_HASH)
    case.payload["eat_nonce"] = NONCE  # 証明書のハッシュが入っていない

    assert reason_of(case) == "certificate"


def test_the_certificate_check_is_skipped_when_no_certificate_hash_is_given():
    # スクリプトの --web 経由では、証明書のハッシュを知らない(None)。7(certificate)を飛ばす。
    case = Case(certificate_sha256=None)
    case.payload["eat_nonce"] = [NONCE]  # ハッシュが入っていなくても通る

    assert case.verify().nonces == (NONCE,)


def test_the_certificate_check_is_not_skipped_by_passing_a_hash_the_token_lacks():
    case = Case(certificate_sha256=OTHER_CERT_HASH)

    assert reason_of(case) == "certificate"


def test_the_verification_works_with_a_public_key_pem_as_well_as_a_certificate_pem():
    public_pem = (
        x509.load_pem_x509_certificate(signing_key().certificate_pem.encode())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    assert Case().verify(certs={"kid-1": public_pem}).image_digest == DIGEST


def test_the_current_time_is_used_when_now_is_not_given():
    payload = attestation_payload([NONCE, CERT_HASH], now=time.time())
    token = mint_token(payload)

    verified = verify_attestation_token(
        token, certs=certs_of(), policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH
    )

    assert verified.image_digest == DIGEST


# --- 17 の理由 ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", VERIFICATION_REASONS)
def test_each_check_reports_its_own_reason_when_only_its_condition_is_broken(reason):
    case = Case()
    BREAKERS[reason](case)

    assert reason_of(case) == reason


@pytest.mark.parametrize("index", range(len(VERIFICATION_REASONS)), ids=VERIFICATION_REASONS)
def test_the_checks_run_in_the_documented_order(index):
    # この理由の条件と、表でそれより後ろの条件をすべて壊す。最初に外れた条件(表の順で先のもの)の理由が返る。
    case = Case()
    for reason in VERIFICATION_REASONS[index:]:
        BREAKERS[reason](case)

    assert reason_of(case) == VERIFICATION_REASONS[index]


# --- 形(malformed) -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        "abc",
        "a.b",
        "a.b.c.d",
        "!!!.e30.sig",  # ヘッダが base64url でない
        "e30.!!!.sig",  # 本文が base64url でない
        "e30.e30.s=ig",  # 署名に、詰め物(=)が混ざっている
        f"{_b64([1])}.e30.sig",  # ヘッダが JSON の辞書でない
        f"e30.{_b64([1])}.sig",  # 本文が JSON の辞書でない
        f"e30.{_b64('text')}.sig",
        "e30.bm90LWpzb24.sig",  # 本文が JSON でない(not-json)
        f"{base64.urlsafe_b64encode(b'[' * 100000).decode()}.e30.sig",  # 深すぎる入れ子(RecursionError でも落ちない)
        None,
        12345,
        b"a.b.c",
        "a" * 70000,  # 大きすぎる(信用していない相手の入力の大きさを抑える)
    ],
    ids=lambda value: repr(value)[:30],
)
def test_a_token_that_is_not_a_jwt_is_malformed(token):
    with pytest.raises(AttestationError) as excinfo:
        verify_attestation_token(
            token, certs=certs_of(), policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW
        )

    assert excinfo.value.reason == "malformed"


# --- 署名 -----------------------------------------------------------------------------------------------------


def test_a_token_signed_by_another_key_with_the_expected_kid_is_a_bad_signature():
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW), signing_key("kid-other"), kid="kid-1")

    with pytest.raises(AttestationError) as excinfo:
        verify_attestation_token(
            token, certs=certs_of(), policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW
        )

    assert excinfo.value.reason == "signature"


def test_a_changed_payload_with_the_original_signature_is_a_bad_signature():
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW))
    header, _payload, signature = token.split(".")
    forged = attestation_payload([NONCE, CERT_HASH], now=NOW)
    forged["submods"]["container"]["image_digest"] = OTHER_DIGEST

    with pytest.raises(AttestationError) as excinfo:
        verify_attestation_token(
            f"{header}.{_b64(forged)}.{signature}",
            certs=certs_of(),
            policy=make_policy(allowed_digests=frozenset({DIGEST, OTHER_DIGEST})),
            nonce=NONCE,
            certificate_sha256=CERT_HASH,
            now=NOW,
        )

    assert excinfo.value.reason == "signature"


def test_a_kid_that_is_not_in_the_certs_is_a_bad_signature():
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW), signing_key("kid-2"))

    with pytest.raises(AttestationError) as excinfo:
        verify_attestation_token(
            token, certs=certs_of(), policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW
        )

    assert excinfo.value.reason == "signature"


@pytest.mark.parametrize("algorithm", ["none", "HS256", "ES256", "RS512"])
def test_only_rs256_is_accepted(algorithm):
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW), header={"alg": algorithm})

    with pytest.raises(AttestationError) as excinfo:
        verify_attestation_token(
            token, certs=certs_of(), policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW
        )

    assert excinfo.value.reason == "signature"


def test_a_token_without_a_kid_is_checked_against_every_cert():
    # google.auth.jwt.decode と同じ: ヘッダに kid がなければ、鍵のどれかで通ればよい。
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW), with_kid=False)

    verified = verify_attestation_token(
        token,
        certs=certs_of(signing_key("kid-2"), signing_key()),
        policy=make_policy(),
        nonce=NONCE,
        certificate_sha256=CERT_HASH,
        now=NOW,
    )

    assert verified.image_digest == DIGEST


def test_the_right_key_is_chosen_by_kid_among_several():
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW), signing_key("kid-2"))

    verified = verify_attestation_token(
        token,
        certs=certs_of(signing_key(), signing_key("kid-2")),
        policy=make_policy(),
        nonce=NONCE,
        certificate_sha256=CERT_HASH,
        now=NOW,
    )

    assert verified.image_digest == DIGEST


@pytest.mark.parametrize(
    "certs",
    [{}, {"kid-1": "not a pem"}, {"kid-1": ""}, {"kid-1": 12345}, {"kid-1": None}],
    ids=["no_certs", "garbage_pem", "empty_pem", "number", "none"],
)
def test_unreadable_or_missing_certs_are_a_bad_signature_not_a_crash(certs):
    with pytest.raises(AttestationError) as excinfo:
        Case().verify(certs=certs)

    assert excinfo.value.reason == "signature"


# --- 期限 -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now_offset", "passes"),
    [(3600, True), (3600 + 60, True), (3600 + 61, False)],
    ids=["at_expiry", "within_60s_after_expiry", "61s_after_expiry"],
)
def test_the_expiry_allows_sixty_seconds_of_clock_skew(now_offset, passes):
    case = Case(now=NOW + now_offset)

    if passes:
        assert case.verify().expires_at == NOW + 3600
    else:
        assert reason_of(case) == "expired"


@pytest.mark.parametrize(
    ("now_offset", "passes"), [(-60, True), (-61, False)], ids=["within_60s_before_issue", "61s_before_issue"]
)
def test_a_token_issued_in_the_future_beyond_the_skew_is_expired(now_offset, passes):
    case = Case(now=NOW + now_offset)

    if passes:
        assert case.verify().issued_at == NOW
    else:
        assert reason_of(case) == "expired"


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": None},  # exp がない(削除)
        {"iat": None},
        {"exp": "tomorrow"},
        {"iat": "now"},
        {"exp": True},
        {"exp": float("nan")},
        {"exp": float("inf")},
        {"iat": float("-inf")},
        {"exp": 10**400},  # float にできない
        {"exp": [NOW + 3600]},
    ],
    ids=lambda claims: ",".join(f"{k}={v!r}"[:24] for k, v in claims.items()),
)
def test_a_missing_or_broken_exp_or_iat_is_expired(claims):
    case = Case()
    for name, value in claims.items():
        if value is None:
            del case.payload[name]
        else:
            case.payload[name] = value

    assert reason_of(case) == "expired"


def test_an_expired_token_is_rejected_by_the_real_clock_when_now_is_not_given():
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=1_000_000.0))  # 1970 年の発行

    with pytest.raises(AttestationError) as excinfo:
        verify_attestation_token(token, certs=certs_of(), policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH)

    assert excinfo.value.reason == "expired"


# --- 宛先・発行者・nonce ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("audience", [["https://vault.anon-nego.internal/attestation"], None, "", 5])
def test_the_audience_must_be_exactly_the_expected_string(audience):
    case = Case()
    case.payload["aud"] = audience

    assert reason_of(case) == "audience"


@pytest.mark.parametrize("eat_nonce", [None, 5, [], [5, 6], {"n": NONCE}, [NONCE, 5]])
def test_an_eat_nonce_that_is_not_strings_does_not_contain_the_nonce(eat_nonce):
    case = Case(certificate_sha256=None)
    if eat_nonce is None:
        del case.payload["eat_nonce"]
    else:
        case.payload["eat_nonce"] = eat_nonce

    assert reason_of(case) == "nonce"


# --- ワークロード ---------------------------------------------------------------------------------------------


def test_an_override_is_detected_for_the_command_and_for_the_environment():
    for name, value in (("cmd_override", ["/bin/sh", "-c", "id"]), ("env_override", {"TEE_X": "1"})):
        case = Case()
        _container(case)[name] = value
        assert reason_of(case) == "override"


def test_an_empty_override_is_the_same_as_no_override():
    case = Case()
    _container(case).update({"cmd_override": [], "env_override": {}})

    assert case.verify().image_digest == DIGEST


def test_the_service_account_must_be_exactly_the_vault_one():
    for accounts in ([], [SERVICE_ACCOUNT, "another@x.iam.gserviceaccount.com"], "other@x.iam.gserviceaccount.com", None):
        case = Case()
        if accounts is None:
            del case.payload["google_service_accounts"]
        else:
            case.payload["google_service_accounts"] = accounts
        assert reason_of(case) == "service_account", accounts


def test_the_project_and_the_service_account_are_not_checked_when_the_policy_has_none():
    case = Case(policy={"project_id": None, "service_account": None})
    _gce(case)["project_id"] = "anything"
    case.payload["google_service_accounts"] = ["anyone@x.iam.gserviceaccount.com"]

    assert case.verify().project_id == "anything"


def test_the_hardware_must_be_in_the_allowed_set_and_a_non_string_does_not_crash():
    case = Case(policy={"allowed_hwmodels": frozenset({"GCP_INTEL_TDX"})})  # token は AMD SEV
    assert reason_of(case) == "hwmodel"

    case = Case()
    case.payload["hwmodel"] = ["GCP_AMD_SEV"]  # 配列(unhashable)でも落ちない
    assert reason_of(case) == "hwmodel"

    case = Case(policy={"allowed_hwmodels": frozenset({"GCP_INTEL_TDX"})})
    case.payload["hwmodel"] = "GCP_INTEL_TDX"
    assert case.verify().hwmodel == "GCP_INTEL_TDX"


def test_the_image_digest_must_be_in_the_allowed_set_and_a_missing_one_is_refused():
    case = Case(policy={"allowed_digests": frozenset()})  # 表が空なら、どのイメージも通らない
    assert reason_of(case) == "image_digest"

    case = Case()
    del _container(case)["image_digest"]
    assert reason_of(case) == "image_digest"

    case = Case()
    del case.payload["submods"]
    assert reason_of(case) == "support_attributes"  # 本番を要求していれば、STABLE がないほうが先に外れる

    case = Case(policy={"require_production": False})
    del case.payload["submods"]
    assert reason_of(case) == "image_digest"


@pytest.mark.parametrize("claim", [None, "dbgstat", "support_attributes"])
def test_a_debug_token_passes_when_production_is_not_required(claim):
    # --allow-debug 相当(require_production=False): dbgstat と STABLE を見ない。debug イメージには support_attributes が付かない。
    case = Case(policy={"require_production": False})
    case.payload["dbgstat"] = "enabled"
    case.payload["submods"]["confidential_space"] = {}
    if claim == "dbgstat":
        del case.payload["dbgstat"]
    if claim == "support_attributes":
        del case.payload["submods"]["confidential_space"]

    verified = case.verify()

    assert verified.support_attributes == ()
    assert verified.dbgstat == (None if claim == "dbgstat" else "enabled")


def test_the_other_checks_still_apply_when_production_is_not_required():
    case = Case(policy={"require_production": False})
    _break_swname(case)
    assert reason_of(case) == "swname"

    case = Case(policy={"require_production": False})
    _break_image_digest(case)
    assert reason_of(case) == "image_digest"


# --- トークンの値を出さない -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", VERIFICATION_REASONS)
def test_the_token_value_is_not_in_the_exception_message_for_any_reason(reason):
    case = Case()
    BREAKERS[reason](case)
    token = case.token()

    with pytest.raises(AttestationError) as excinfo:
        case.verify()

    error = excinfo.value
    assert str(error) == reason and repr(error) == f"AttestationError({reason!r})"
    payload_part = token.split(".")[1] if token.count(".") == 2 else token
    assert token not in str(error) and payload_part not in repr(error)
    assert error.token == token  # 表示用の属性(例外の文には入らない)


def test_the_token_value_is_not_logged_by_the_verification(caplog):
    caplog.set_level(logging.DEBUG)
    case = Case()
    _break_signature(case)
    token = case.token()

    with pytest.raises(AttestationError):
        case.verify()
    Case().verify()

    assert token not in caplog.text and token.split(".")[1] not in caplog.text


# --- 読み取り・要約 ---------------------------------------------------------------------------------------------


def test_the_claims_can_be_read_without_checking_the_signature_for_display():
    token = flip_signature_byte(mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW)))

    claims = decode_claims_unverified(token)

    assert claims["submods"]["container"]["image_digest"] == DIGEST
    with pytest.raises(AttestationError) as excinfo:
        decode_claims_unverified("nonsense")
    assert excinfo.value.reason == "malformed"


def test_the_summary_has_the_nine_items_of_the_contract_and_tolerates_missing_or_odd_claims():
    summary = summarize_claims(attestation_payload([NONCE], now=NOW))
    assert summary == {
        "image_digest": DIGEST,
        "hwmodel": "GCP_AMD_SEV",
        "swname": "CONFIDENTIAL_SPACE",
        "swversion": ["250800"],
        "dbgstat": "disabled-since-boot",
        "support_attributes": ["LATEST", "STABLE", "USABLE"],
        "project_id": PROJECT_ID,
        "zone": ZONE,
        "instance_name": INSTANCE,
    }

    empty = summarize_claims({"submods": "not a dict", "swversion": 5, "hwmodel": ["x"]})
    assert empty == {
        "image_digest": None,
        "hwmodel": None,
        "swname": None,
        "swversion": [],
        "dbgstat": None,
        "support_attributes": [],
        "project_id": None,
        "zone": None,
        "instance_name": None,
    }
    assert summarize_claims({"swversion": "250800"})["swversion"] == ["250800"]  # 文字列で来ても、配列にする


# --- SignerCerts ----------------------------------------------------------------------------------------------


class FakeFetch:
    """SignerCerts の fetch の差し替え。呼ばれた回数を数え、返す鍵を差し替えられる。"""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        response = self.responses[min(len(self.urls), len(self.responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return response


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


URL = "https://certs.test/x509"


def test_signer_certs_fetch_once_and_serve_the_cache_until_the_ttl():
    clock, fetch = FakeMonotonic(), FakeFetch(certs_of())
    certs = SignerCerts(URL, fetch=fetch, clock=clock)

    assert certs.get() == certs_of() and certs.get() == certs_of()
    assert fetch.urls == [URL]

    clock.value += 3599
    certs.get()
    assert len(fetch.urls) == 1

    clock.value += 2  # TTL(3600 秒)をすぎた
    certs.get()
    assert len(fetch.urls) == 2


def test_signer_certs_refresh_is_limited_to_once_per_interval():
    clock, fetch = FakeMonotonic(), FakeFetch(certs_of(), certs_of(signing_key("kid-2")), certs_of(signing_key("kid-3")))
    certs = SignerCerts(URL, fetch=fetch, clock=clock)
    certs.get()

    clock.value += 30
    assert set(certs.refresh()) == {"kid-1"}  # 前回の取得から 60 秒以内: 取らずにキャッシュを返す
    assert len(fetch.urls) == 1

    clock.value += 31
    assert set(certs.refresh()) == {"kid-2"}  # 60 秒をすぎた: 取り直す
    clock.value += 1
    assert set(certs.refresh()) == {"kid-2"}  # また 60 秒以内
    assert len(fetch.urls) == 2


def test_signer_certs_keep_serving_the_previous_keys_when_a_refetch_fails():
    clock = FakeMonotonic()
    fetch = FakeFetch(certs_of(), ConnectionError("down"))
    certs = SignerCerts(URL, fetch=fetch, clock=clock)
    certs.get()

    clock.value += 3601  # TTL 切れ。取り直しに失敗しても、前の鍵で続ける
    assert set(certs.get()) == {"kid-1"}
    clock.value += 61
    assert set(certs.refresh()) == {"kid-1"}


def test_signer_certs_raise_when_the_first_fetch_fails_and_try_again_next_time():
    fetch = FakeFetch(ConnectionError("down"), certs_of())
    certs = SignerCerts(URL, fetch=fetch, clock=FakeMonotonic())

    with pytest.raises(ConnectionError):
        certs.get()
    assert set(certs.get()) == {"kid-1"}


@pytest.mark.parametrize("bad", [{}, [], {"kid-1": 5}, {"kid-1": None}, "text"], ids=["empty", "list", "number", "none", "text"])
def test_signer_certs_refuse_an_unexpected_response_shape(bad):
    certs = SignerCerts(URL, fetch=FakeFetch(bad), clock=FakeMonotonic())

    with pytest.raises((ValueError, TypeError)):
        certs.get()


def test_verification_with_signer_certs_refetches_once_for_an_unknown_kid_and_then_passes():
    # 鍵は回転する。未知の kid のときだけ取り直し(1 回だけ)、新しい鍵で検証が通る。
    clock, fetch = FakeMonotonic(), FakeFetch(certs_of(), certs_of(signing_key(), signing_key("kid-2")))
    certs = SignerCerts(URL, fetch=fetch, clock=clock)
    certs.get()
    clock.value += 61  # 前回の取得から 1 分以上たった(取り直してよい)
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW), signing_key("kid-2"))

    verified = verify_attestation_token(
        token, certs=certs, policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW
    )

    assert verified.image_digest == DIGEST
    assert len(fetch.urls) == 2


def test_verification_with_signer_certs_does_not_refetch_for_a_known_kid():
    fetch = FakeFetch(certs_of())
    certs = SignerCerts(URL, fetch=fetch, clock=FakeMonotonic())
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW))

    for _ in range(3):
        verify_attestation_token(token, certs=certs, policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW)

    assert len(fetch.urls) == 1


def test_an_unknown_kid_is_a_bad_signature_and_the_refetch_is_rate_limited():
    clock = FakeMonotonic()
    fetch = FakeFetch(certs_of())  # 取り直しても kid-9 は出てこない
    certs = SignerCerts(URL, fetch=fetch, clock=clock)
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW), signing_key("kid-9"))

    for _ in range(3):  # 攻撃者が未知の kid のトークンを送り続けても、取得は 60 秒に 1 回まで
        with pytest.raises(AttestationError) as excinfo:
            verify_attestation_token(
                token, certs=certs, policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW
            )
        assert excinfo.value.reason == "signature"

    assert len(fetch.urls) == 1  # 最初の取得だけ(直後の取り直しは、間隔のうちなので取らない)


def test_verification_reports_unavailable_when_the_signer_certs_cannot_be_fetched_at_all():
    certs = SignerCerts(URL, fetch=FakeFetch(ConnectionError("down")), clock=FakeMonotonic())
    token = mint_token(attestation_payload([NONCE, CERT_HASH], now=NOW))

    with pytest.raises(AttestationError) as excinfo:
        verify_attestation_token(
            token, certs=certs, policy=make_policy(), nonce=NONCE, certificate_sha256=CERT_HASH, now=NOW
        )

    assert excinfo.value.reason == UNAVAILABLE == "unavailable"
    assert str(excinfo.value) == "unavailable"


# --- digest → コミットの表 ------------------------------------------------------------------------------------------


def _write_releases(path: Path, releases) -> Path:
    path.write_text(json.dumps({"releases": releases}), encoding="utf-8")
    return path


def test_the_releases_file_is_read_and_a_digest_is_looked_up(tmp_path):
    entry = {"digest": DIGEST, "commit": COMMIT, "built_at": "2026-10-03T09:00:00+00:00", "status": "active"}
    other = {"digest": OTHER_DIGEST, "commit": "f" * 40, "built_at": "2026-10-04T09:00:00+00:00", "status": "active"}
    releases = load_releases(_write_releases(tmp_path / "releases.json", [entry, other]))

    assert releases == [entry, other]
    assert release_for_digest(releases, OTHER_DIGEST) == other
    assert release_for_digest(releases, "sha256:" + "0" * 64) is None
    assert release_for_digest([], DIGEST) is None


def test_a_release_without_a_status_is_regarded_as_active(tmp_path):
    # 契約 §13 の追記: status は active か revoked。なければ active とみなして、返す要素に入れる。
    releases = load_releases(_write_releases(tmp_path / "releases.json", [{"digest": DIGEST, "commit": COMMIT}]))

    assert releases == [{"digest": DIGEST, "commit": COMMIT, "status": "active"}]
    assert active_digests(releases) == {DIGEST}


def test_a_revoked_release_is_returned_for_display_but_is_not_an_allowed_digest(tmp_path):
    revoked = {"digest": OTHER_DIGEST, "commit": "f" * 40, "built_at": "2026-10-04T09:00:00+00:00", "status": "revoked"}
    active = {"digest": DIGEST, "commit": COMMIT, "status": "active"}
    releases = load_releases(_write_releases(tmp_path / "releases.json", [active, revoked]))

    assert release_for_digest(releases, OTHER_DIGEST)["status"] == "revoked"  # 失効したことを表示するために引ける
    assert active_digests(releases) == {DIGEST}  # 検証では通さない

    # 検証の理由は、失効していても image_digest(revoked の区別は、API の release.status で分かる)
    case = Case(policy={"allowed_digests": active_digests(releases)})
    _break_image_digest(case)  # token の digest は OTHER_DIGEST(失効)
    assert reason_of(case) == "image_digest"


def test_an_empty_releases_table_is_valid_and_matches_nothing(tmp_path):
    assert load_releases(_write_releases(tmp_path / "releases.json", [])) == []


def test_the_real_releases_file_of_the_repository_has_the_expected_shape():
    path = Path(__file__).resolve().parents[1] / "deploy" / "vault-releases.json"

    assert isinstance(load_releases(path), list)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"releases": "none"}',
        '{"other": []}',
        json.dumps({"releases": [{"digest": "sha256:abc", "commit": COMMIT}]}),  # digest が 64 桁の 16 進でない
        json.dumps({"releases": [{"digest": DIGEST, "commit": "not-a-sha"}]}),
        json.dumps({"releases": [{"digest": DIGEST}]}),
        json.dumps({"releases": ["x"]}),
        json.dumps({"releases": [{"digest": DIGEST.upper(), "commit": COMMIT}]}),
        json.dumps({"releases": [{"digest": DIGEST, "commit": COMMIT, "status": "deleted"}]}),  # active・revoked 以外
        json.dumps({"releases": [{"digest": DIGEST, "commit": COMMIT, "status": None}]}),
    ],
)
def test_a_malformed_releases_file_is_refused(tmp_path, content):
    path = tmp_path / "releases.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        load_releases(path)


def test_a_missing_releases_file_is_a_file_not_found_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_releases(tmp_path / "missing.json")


# --- 証明書のピン留めの部品 ------------------------------------------------------------------------------------------


def test_the_certificate_hash_is_the_sha256_of_the_der_in_lowercase_hex():
    material = make_tls_material()
    der = ssl.PEM_cert_to_DER_cert(material.cert_pem)

    assert certificate_sha256_of_pem(material.cert_pem) == hashlib.sha256(der).hexdigest() == material.certificate_sha256
    assert len(material.certificate_sha256) == 64 and material.certificate_sha256 == material.certificate_sha256.lower()


def test_the_pinned_context_trusts_only_that_certificate_and_does_not_check_the_host_name():
    material = make_tls_material()

    context = pinned_ssl_context(material.cert_pem)

    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname is False
    assert context.cert_store_stats()["x509"] == 1  # システムの CA は信用しない(その 1 枚だけ)


# --- [vault.tee] の設定 ---------------------------------------------------------------------------------------


def test_the_tee_settings_are_read_from_the_params_file():
    settings = load_tee_settings()

    assert settings.port == 8443
    assert settings.attestation_audience == "https://vault.anon-nego.internal/attestation"
    assert settings.caller_audience == "https://vault.anon-nego.internal"
    assert settings.caller_service_account == "web-run"
    assert (settings.workload_identity_pool, settings.workload_identity_provider) == ("vault-tee-pool", "attestation-verifier")
    assert (settings.kms_key_ring, settings.kms_key) == ("vault-tee", "vault-kek")
    assert settings.launcher_socket == "/run/container_launcher/teeserver.sock"
    assert settings.claims_token_file == "/run/container_launcher/attestation_verifier_claims_token"
    assert settings.min_attestation_interval_seconds == 1.0
    assert (settings.tls_certificate_days, settings.tls_dir) == (90, "/dev/shm/vault-tls")
    assert settings.allowed_hwmodels == ("GCP_AMD_SEV", "GCP_INTEL_TDX")
    assert settings.attestation_issuer == "https://confidentialcomputing.googleapis.com"
    assert settings.attestation_signer_certs_url.endswith("/x509/signer@confidentialspace-sign.iam.gserviceaccount.com")
    assert settings.caller_certs_url == "https://www.googleapis.com/oauth2/v1/certs"


def _settings_file(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_params_file_without_the_tee_section_or_a_key_is_refused(tmp_path):
    real = (Path(__file__).resolve().parents[1] / "config" / "params.toml").read_text(encoding="utf-8")
    without_section = _settings_file(tmp_path, "without_section", "[vault.limits]\nx = 1\n")
    without_key = _settings_file(tmp_path, "without_key", real.replace('kms_key = "vault-kek"\n', ""))
    empty_hwmodels = _settings_file(
        tmp_path, "empty_hwmodels", real.replace('allowed_hwmodels = ["GCP_AMD_SEV", "GCP_INTEL_TDX"]', "allowed_hwmodels = []")
    )

    with pytest.raises(ValueError, match=r"\[vault\.tee\] section"):
        load_tee_settings(without_section)
    with pytest.raises(ValueError, match="kms_key"):
        load_tee_settings(without_key)
    with pytest.raises(ValueError, match="allowed_hwmodels"):
        load_tee_settings(empty_hwmodels)
