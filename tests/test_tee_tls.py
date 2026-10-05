"""TEE 版の金庫の TLS の自己署名の証明書(src/vault/tee/tls.py。research/tee-spike-contract.md §11)。

確かめること:
- generate: P-256(ECDSA)・SHA-256・CN=vault・SAN は DNS vault だけ・CA ではない・拡張鍵用途は serverAuth だけ・
  有効期間(notAfter は現在 + days 日)。certificate_sha256 は DER の SHA-256(小文字の 16 進 64 文字)。起動のたびに別の鍵・証明書。
- write: ディレクトリ 0700、key.pem・cert.pem は 0600(すでにあるディレクトリ・ファイルも絞る)。(鍵, 証明書)の順で返す。
- 本物の TLS の握手(127.0.0.1 の手元のサーバ): web が行う「その 1 枚だけを信用する」接続(check_hostname=False・CERT_REQUIRED)が通り、
  別の証明書は断られる。web が計算する値(ssl.get_server_certificate → PEM_cert_to_DER_cert の SHA-256)が certificate_sha256 と一致する。
GCP には接続しない。
"""

import contextlib
import datetime as dt
import hashlib
import socket
import ssl
import stat
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from vault.tee.tls import TlsMaterial, generate, write


def _load_cert(material: TlsMaterial) -> x509.Certificate:
    return x509.load_pem_x509_certificate(material.cert_pem)


def test_the_certificate_is_a_p256_ecdsa_sha256_leaf_for_vault():
    cert = _load_cert(generate(90))

    public_key = cert.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey) and public_key.curve.name == "secp256r1"
    assert isinstance(cert.signature_hash_algorithm, SHA256)
    assert cert.subject == cert.issuer == x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vault")])
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert list(san) == [x509.DNSName("vault")]
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.value.ca is False and basic.critical
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]


@pytest.mark.parametrize("days", [1, 90, 365])
def test_the_validity_period_ends_days_from_now_and_has_already_begun(days):
    before = dt.datetime.now(dt.timezone.utc)
    cert = _load_cert(generate(days))
    after = dt.datetime.now(dt.timezone.utc)

    assert cert.not_valid_before_utc <= before  # 時計のずれで「まだ有効でない」と断られない
    assert before + dt.timedelta(days=days) - dt.timedelta(seconds=2) <= cert.not_valid_after_utc
    assert cert.not_valid_after_utc <= after + dt.timedelta(days=days)


@pytest.mark.parametrize("days", [0, -1])
def test_a_validity_of_less_than_a_day_is_refused(days):
    with pytest.raises(ValueError):
        generate(days)


def test_certificate_sha256_is_the_sha256_of_the_der_in_lowercase_hex():
    material = generate(90)
    der = _load_cert(material).public_bytes(serialization.Encoding.DER)

    assert material.certificate_sha256 == hashlib.sha256(der).hexdigest()
    assert len(material.certificate_sha256) == 64
    assert material.certificate_sha256 == material.certificate_sha256.lower()
    # web が ssl の PEM → DER から計算する値と同じ(契約 §2)
    assert hashlib.sha256(ssl.PEM_cert_to_DER_cert(material.cert_pem.decode())).hexdigest() == material.certificate_sha256


def test_the_key_matches_the_certificate_and_every_start_makes_a_new_pair():
    first, second = generate(90), generate(90)

    key = serialization.load_pem_private_key(first.key_pem, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.public_key().public_numbers() == _load_cert(first).public_key().public_numbers()
    assert first.key_pem != second.key_pem
    assert first.cert_pem != second.cert_pem
    assert first.certificate_sha256 != second.certificate_sha256


def test_write_makes_a_0700_directory_and_0600_files(tmp_path):
    material = generate(90)
    directory = tmp_path / "vault-tls"

    key_path, cert_path = write(material, directory)

    assert (key_path, cert_path) == (directory / "key.pem", directory / "cert.pem")
    assert key_path.read_bytes() == material.key_pem
    assert cert_path.read_bytes() == material.cert_pem
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o600


def test_write_tightens_a_directory_and_files_that_already_exist(tmp_path):
    directory = tmp_path / "vault-tls"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    old_key = directory / "key.pem"
    old_key.write_text("old")
    old_key.chmod(0o644)
    material = generate(90)

    key_path, cert_path = write(material, directory)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o600
    assert key_path.read_bytes() == material.key_pem  # 古い内容は残らない


def test_write_creates_missing_parent_directories(tmp_path):
    key_path, cert_path = write(generate(90), tmp_path / "a" / "b" / "vault-tls")

    assert key_path.is_file() and cert_path.is_file()


# --- 本物の TLS の握手 ---


@contextlib.contextmanager
def _tls_server(key_path, cert_path):
    """127.0.0.1 の手元の TLS サーバ(握手だけして閉じる)。(ホスト, ポート) を返す。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                with context.wrap_socket(connection, server_side=True) as tls_connection:
                    tls_connection.recv(1)
            except (ssl.SSLError, OSError):
                pass  # 相手が証明書を断って切った

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()
    finally:
        stop.set()
        thread.join(timeout=5)
        listener.close()


def _pinning_context(cert_pem: bytes) -> ssl.SSLContext:
    """web の「検証してからピン留め」と同じ接続の条件: その 1 枚だけを信用し、ホスト名は見ない(契約 §8)。"""
    context = ssl.create_default_context(cadata=cert_pem.decode())
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _handshake(address, context: ssl.SSLContext) -> None:
    with socket.create_connection(address, timeout=5) as raw, context.wrap_socket(raw) as connection:
        connection.send(b"x")


def test_the_pinned_connection_that_web_makes_succeeds_with_this_certificate(tmp_path):
    material = generate(90)
    key_path, cert_path = write(material, tmp_path / "tls")

    with _tls_server(key_path, cert_path) as address:
        _handshake(address, _pinning_context(material.cert_pem))
        # X.509 の厳格な検査(Python 3.13 以降の既定)でも通る
        strict = _pinning_context(material.cert_pem)
        strict.verify_flags |= ssl.VERIFY_X509_STRICT
        _handshake(address, strict)


def test_web_computes_the_same_hash_from_the_certificate_the_server_presents(tmp_path):
    material = generate(90)
    key_path, cert_path = write(material, tmp_path / "tls")

    with _tls_server(key_path, cert_path) as address:
        presented_pem = ssl.get_server_certificate(address)

    der = ssl.PEM_cert_to_DER_cert(presented_pem)
    assert hashlib.sha256(der).hexdigest() == material.certificate_sha256


def test_another_certificate_is_refused_by_the_pinned_connection(tmp_path):
    served, other = generate(90), generate(90)
    key_path, cert_path = write(served, tmp_path / "tls")

    with _tls_server(key_path, cert_path) as address, pytest.raises(ssl.SSLCertVerificationError):
        _handshake(address, _pinning_context(other.cert_pem))
