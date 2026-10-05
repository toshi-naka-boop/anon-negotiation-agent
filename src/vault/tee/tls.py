"""TLS の自己署名の証明書(research/tee-spike-contract.md §11。research/tee-spike.md の 3-2)。

金庫は起動のたびに、メモリ上で P-256 の鍵と自己署名の葉の証明書を作り、`tls_dir`(/dev/shm の下。メモリ上)に書く
(uvicorn の ssl_keyfile・ssl_certfile がファイルのパスを要求するため)。金庫の再起動で証明書が変わるのは設計どおり
(web は、接続エラーのたびに attestation を検証し直して、新しい証明書に付け替える)。

certificate_sha256 は、証明書(DER)の SHA-256(小文字の 16 進 64 文字)。/v1/attestation が launcher の nonce に入れ、web が
`ssl.get_server_certificate` → `ssl.PEM_cert_to_DER_cert` から計算した値と照合する。

証明書: ECDSA P-256・SHA-256、subject/issuer は CN=vault、SAN は DNS vault、BasicConstraints は ca=False、拡張鍵用途は serverAuth
(付け忘れると、クライアントの検証が用途違いで通らないことがある)。notBefore は、金庫と web の時計のずれで「まだ有効でない」と
断られないよう、5 分前にする(notAfter は現在 + days 日)。
"""

import datetime as dt
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_BACKDATE = dt.timedelta(minutes=5)
KEY_FILE_NAME = "key.pem"
CERT_FILE_NAME = "cert.pem"


@dataclass(frozen=True)
class TlsMaterial:
    key_pem: bytes
    cert_pem: bytes
    certificate_sha256: str


def generate(days: int) -> TlsMaterial:
    """新しい鍵と自己署名の証明書(有効期間 days 日)を作る。"""
    if days < 1:
        raise ValueError("days must be at least 1")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vault")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("vault")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return TlsMaterial(
        key_pem=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
        certificate_sha256=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
    )


def _write_private_file(path: Path, data: bytes) -> None:
    # 0600 で作る(作ってから権限を絞ると、その間だけ他のユーザーに読まれうる)。すでにあるファイルの権限も 0600 に絞る。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)


def write(material: TlsMaterial, directory: Path) -> tuple[Path, Path]:
    """directory(0700)に key.pem・cert.pem(どちらも 0600)を書き、(鍵のパス, 証明書のパス)を返す。uvicorn の ssl_keyfile・ssl_certfile の順。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)  # すでにあったディレクトリ・umask で絞られた権限も、0700 にそろえる
    key_path, cert_path = directory / KEY_FILE_NAME, directory / CERT_FILE_NAME
    _write_private_file(key_path, material.key_pem)
    _write_private_file(cert_path, material.cert_pem)
    return key_path, cert_path
