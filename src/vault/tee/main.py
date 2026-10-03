"""金庫の TEE(Confidential Space)版の起動口: `python -m vault.tee.main`(research/tee-spike-contract.md §4)。

環境変数は使わない(launch policy で `tee-env-*` を許さない)。設定は config/params.toml の [vault.tee]、プロジェクト ID・番号・ゾーン・
インスタンス名はメタデータサーバから取る。起動の順序(各段は関数に分けてあり、失敗したら理由の段と例外の型名をログに書いて、非 0 で終わる。
tee-restart-policy=OnFailure が再起動する):

1. ログ(INFO、標準出力)と、アクセスログから ID を伏せる設定(mask_ids_in_logs)。
2. メタデータサーバ(プロジェクト ID・番号・ゾーン・インスタンス名)。
3. 鍵の解放(STS と KMS。DEK を得る)。DEK は `vault-db` の `_tee/dek` に包んで保管するので、Firestore のクライアントはこの前に作る。
   保管された DEK は、包んだ鍵の版が KMS の鍵の primary と一致するときだけ解く(一致しなければ失敗。批評 C-56)。
4. VaultStore(Firestore の vault-db。サービスアカウントの既定の認証)。DEK の Sealer を渡し、本物の依頼者と live の交渉の機微な項目を封印して保存する
   (design.md §9 の 2。項目と仕組みは vault.seal_layer)。
5. 架空人物のテンプレートの投入(vault.seed。イメージに焼いた fixtures/case*.toml を、`templates/{template_id}` に冪等に書く。
   design.md §3.7・台帳 P-15)。テンプレートは公開フィクスチャなので、封印しない。失敗(ファイルの検証エラー・Firestore の失敗)したら起動しない。
6. 封印の自己試験(`_tee/selftest` は、なければ作り、あれば開封して確かめる。批評 X-70)。再起動をまたいで、既存の暗号文が同じ DEK で
   開くことを確かめる(鍵の版を切り替えたあとに、既存のデータが読めなくなっていないかを、ここで見つける)。
7. TLS の鍵と自己署名の証明書(メモリ上の tls_dir に 0600 で書く)。
8. create_app(呼び出し元の検証と attestation の口を付けた金庫の app)。
9. uvicorn(0.0.0.0:port、TLS は金庫の中で終端する)。SIGTERM は uvicorn に任せる。

失敗の理由には、トークン・鍵・ID が入りうるので、例外の文は書かず、型名だけを書く(詳細は、各段が自分でログに書く)。
"""

import hashlib
import logging
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import uvicorn
from fastapi import FastAPI
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from negotiation_core.log_privacy import mask_ids_in_logs

from vault.app import create_app
from vault.clock import SystemClock
from vault.config import DEFAULT_VAULT_CONFIG, VaultTeeConfig, load_vault_tee_config
from vault.firestore_client import create_client
from vault.fixtures import FIXTURES_DIRECTORY
from vault.seed import seed_templates
from vault.store import VaultStore
from vault.tee import tls
from vault.tee.attestation_api import AttestationService
from vault.tee.caller_auth import CallerCerts, CallerVerifier
from vault.tee.key_release import TEE_COLLECTION, release_dek
from vault.tee.launcher import LauncherClient
from vault.tee.metadata import InstanceMetadata, read_instance_metadata
from vault.tee.sealing import SealError, Sealer

logger = logging.getLogger(__name__)

SELFTEST_DOCUMENT = "selftest"
SELFTEST_FAILED = "sealing self-test failed: existing ciphertext does not open"
_T = TypeVar("_T")


class StartupError(Exception):
    """起動の段の 1 つが失敗した(理由はすでにログに書いてある)。"""


def configure_logging() -> None:
    """ログは INFO、標準出力(launcher が Cloud Logging へ転送する)。アクセスログの URL から ID を伏せる(台帳 X-40)。"""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s: %(message)s")
    mask_ids_in_logs()


def run_sealing_self_test(db: firestore.Client, sealer: Sealer) -> None:
    """封印の自己試験(批評 X-70)。`_tee/selftest` がなければ、封印した乱数 R(probe)・R の SHA-256(probe_sha256)・created_at で作る。
    そのあと(作った直後も)、保管されている probe を開封して、SHA-256 が probe_sha256 と一致することを確かめる。

    再起動をまたいで、既存の暗号文が同じ DEK で開くことの確認。鍵の版の切り替え(DEK の作り直し)のあとに、既存のデータが読めなくなって
    いないかを、ここで見つける(DEK を作り直すときは `_tee/selftest` も消す)。開かない・一致しない・項目がないときは、固定文をログに書いて
    SealError(値は出さない)。
    """
    path = f"{TEE_COLLECTION}/{SELFTEST_DOCUMENT}"
    ref = db.document(path)
    if not ref.get().exists:
        probe = secrets.token_bytes(32)
        try:
            ref.create(
                {
                    "probe": sealer.seal(path, "probe", probe),
                    "probe_sha256": hashlib.sha256(probe).hexdigest(),
                    "created_at": firestore.SERVER_TIMESTAMP,
                }
            )
            logger.info("sealing self-test: created the probe")
        except AlreadyExists:
            pass  # 別のインスタンスが先に作った。そちらを確かめる
    stored = ref.get().to_dict() or {}
    sealed, expected = stored.get("probe"), stored.get("probe_sha256")
    try:
        matches = hashlib.sha256(sealer.open(path, "probe", sealed)).hexdigest() == expected
    except (SealError, TypeError):  # 開けない・bytes でない
        matches = False
    if not matches:
        logger.error(SELFTEST_FAILED)
        raise SealError(SELFTEST_FAILED)
    logger.info("sealing self-test ok")


def prepare_tls(config: VaultTeeConfig) -> tuple[Path, Path, str]:
    """鍵と自己署名の証明書を作って tls_dir に書く。(鍵のパス, 証明書のパス, 証明書の SHA-256) を返す。"""
    material = tls.generate(config.tls_certificate_days)
    key_path, cert_path = tls.write(material, Path(config.tls_dir))
    return key_path, cert_path, material.certificate_sha256


def build_app(
    store: VaultStore, config: VaultTeeConfig, metadata: InstanceMetadata, certificate_sha256: str
) -> FastAPI:
    """呼び出し元の検証(web のサービスアカウントだけ)と attestation の口を付けた、金庫の app。"""
    certs = CallerCerts(config.caller_certs_url)
    if not certs.refresh():  # 起動時に 1 回取る。取れなくても起動する(最初の要求で取り直す。理由はログに書いてある)
        logger.warning("caller certs could not be fetched at startup; they will be fetched on the first request")
    verifier = CallerVerifier(
        audience=config.caller_audience,
        allowed_email=f"{config.caller_service_account}@{metadata.project_id}.iam.gserviceaccount.com",
        certs=certs,
    )
    attestation = AttestationService(
        audience=config.attestation_audience,
        certificate_sha256=certificate_sha256,
        launcher=LauncherClient(socket_path=config.launcher_socket),
        min_interval_seconds=config.min_attestation_interval_seconds,
    )
    return create_app(store, caller_verifier=verifier, attestation=attestation)


def serve(app: FastAPI, config: VaultTeeConfig, key_path: Path, cert_path: Path) -> None:
    """uvicorn で待ち受ける(TLS は金庫の中で終端する)。SIGTERM は uvicorn に任せる。"""
    uvicorn.run(app, host="0.0.0.0", port=config.port, ssl_keyfile=str(key_path), ssl_certfile=str(cert_path))


def _step(name: str, action: Callable[[], _T]) -> _T:
    try:
        return action()
    except Exception as exc:
        logger.error("startup failed at the step '%s' (%s)", name, type(exc).__name__)
        raise StartupError(name) from exc


def main() -> int:
    """起動の順序(上の docstring)を実行する。uvicorn が終わるまで戻らない。失敗は 1(非 0)を返す。"""
    configure_logging()
    try:
        config = _step("config", load_vault_tee_config)
        metadata = _step("metadata", read_instance_metadata)
        db = _step("firestore", lambda: create_client(project=metadata.project_id))
        dek = _step("key release", lambda: release_dek(db, metadata=metadata, config=config))
        sealer = Sealer(dek)  # release_dek が 32 バイトで返す。保存の封印と、起動時の自己試験の両方に使う
        store = _step(
            "store", lambda: VaultStore(db=db, clock=SystemClock(), config=DEFAULT_VAULT_CONFIG, sealer=sealer)
        )
        _step("seed templates", lambda: seed_templates(db, FIXTURES_DIRECTORY))
        _step("sealing self-test", lambda: run_sealing_self_test(db, sealer))
        key_path, cert_path, certificate_sha256 = _step("tls", lambda: prepare_tls(config))
        app = _step("app", lambda: build_app(store, config, metadata, certificate_sha256))
    except StartupError:
        return 1
    serve(app, config, key_path, cert_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
