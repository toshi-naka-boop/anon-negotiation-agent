"""pytest 共通フィクスチャ。Firestore エミュレータをセッション単位で起動する。

gcloud コマンドは使わない(~/.config/gcloud に触れないため)。JDK 21 で jar を直接起動する。
本物の GCP には絶対に接続しない: クライアントを作る前に FIRESTORE_EMULATOR_HOST を設定し、
プロジェクト ID は demo- で始まるものにする。認証情報は使わない。

データの分け方: テストごとに別のプロジェクト ID(demo-test-{uuid})の Firestore クライアントを使う。
エミュレータはプロジェクトごとにデータを分けるので、テストの後始末でデータを消さずに済み、
遅れて届く書き込み(止まらない別スレッドの呼び出し)は、終わったテストのプロジェクトに落ちて、
次のテストに混ざらない(台帳 I-5)。vault-db と (default) は、同じテストの中では同じプロジェクトを使う。
"""

import datetime as dt
import logging
import os
import queue
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.testclient import TestClient
from google.cloud import firestore

from vault.app import create_app
from vault.clock import FixedClock
from vault.config import DEFAULT_VAULT_CONFIG
from vault.store import VaultStore
from web.vault_client import VaultClient

_JAVA_BIN = "/opt/homebrew/opt/openjdk@21/bin/java"
_EMULATOR_JAR = (
    "/opt/homebrew/share/google-cloud-sdk/platform/cloud-firestore-emulator/cloud-firestore-emulator.jar"
)
_READY_MARKER = "Dev App Server is now running"
_STARTUP_TIMEOUT_SECONDS = 30

DATABASE_ID = "vault-db"
DEFAULT_DATABASE_ID = "(default)"  # web 用(design.md §1.1)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _drain_output(pipe, sink: "queue.Queue[str | None]") -> None:
    for line in iter(pipe.readline, ""):
        sink.put(line)
    sink.put(None)


@pytest.fixture(scope="session")
def firestore_emulator_host() -> str:
    """Firestore エミュレータをセッション単位で起動し、終わったら止める(§テスト環境)。"""
    port = _find_free_port()
    host = f"127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            _JAVA_BIN,
            "-Duser.language=en",
            "-cp",
            _EMULATOR_JAR,
            "com.google.cloud.datastore.emulator.firestore.CloudFirestore",
            "start",
            "--host=127.0.0.1",
            f"--port={port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output_queue: "queue.Queue[str | None]" = queue.Queue()
    reader = threading.Thread(target=_drain_output, args=(process.stdout, output_queue), daemon=True)
    reader.start()

    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    ready = False
    captured: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = output_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break  # 標準出力が閉じた(起動に失敗した)
        captured.append(line)
        if _READY_MARKER in line:
            ready = True
            break

    if not ready:
        process.terminate()
        process.wait(timeout=10)
        raise RuntimeError(
            "Firestore エミュレータが時間内に起動しなかった:\n" + "".join(captured)
        )

    # 本物の GCP には絶対に接続しない: どの Firestore クライアントを作るより前に、
    # この環境変数をエミュレータへ向ける。
    os.environ["FIRESTORE_EMULATOR_HOST"] = host

    yield host

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@pytest.fixture
def firestore_project_id(firestore_emulator_host: str) -> str:
    """このテストだけが使う、一意なプロジェクト ID(demo- で始まる。エミュレータ専用の名前)。

    vault-db と (default) の両方のクライアントが、このテストの中では同じ ID を使う。
    """
    return f"demo-test-{uuid.uuid4().hex}"


@pytest.fixture
def firestore_client(firestore_project_id: str) -> firestore.Client:
    """vault-db への Firestore クライアント(テストごとに作る。プロジェクトごと分かれているので、データは消さない)。"""
    client = firestore.Client(project=firestore_project_id, database=DATABASE_ID)
    yield client
    client.close()


@pytest.fixture
def default_db(firestore_project_id: str) -> firestore.Client:
    """(default) への Firestore クライアント(web 用。テストごとに作る。vault-db と同じプロジェクト ID)。"""
    client = firestore.Client(project=firestore_project_id, database=DEFAULT_DATABASE_ID)
    yield client
    client.close()


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """本番の起動口(web.app.create_app_from_env)が変える、ログの設定を、テストごとに元に戻す(台帳 X-40)。

    uvicorn のアクセスログの ID を伏せるフィルタと、httpx のログの水準。残ると、あとのテストのログの確認が、
    実行の順番に左右される。
    """
    access_logger, httpx_logger = logging.getLogger("uvicorn.access"), logging.getLogger("httpx")
    filters, level = list(access_logger.filters), httpx_logger.level
    yield
    access_logger.filters[:] = filters
    httpx_logger.setLevel(level)


@pytest.fixture
def clock() -> FixedClock:
    """sleep せず進められるテスト用の時計。"""
    return FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))


@pytest.fixture
def store(firestore_client: firestore.Client, clock: FixedClock) -> VaultStore:
    return VaultStore(db=firestore_client, clock=clock, config=DEFAULT_VAULT_CONFIG)


@pytest.fixture
def api_client(store: VaultStore) -> TestClient:
    return TestClient(create_app(store))


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """非同期テスト(pytest.mark.anyio。anyio の pytest プラグインを使う)は asyncio だけで動かす。"""
    return "asyncio"


@pytest.fixture
async def vault_client(store: VaultStore) -> AsyncIterator[VaultClient]:
    """金庫の app を ASGI のまま(ネットワークを通さず)つないだ、web の金庫クライアント。"""
    transport = httpx.ASGITransport(app=create_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://vault") as http:
        yield VaultClient(http)


@pytest.fixture
async def web_env(store: VaultStore, clock: FixedClock, vault_client: VaultClient, default_db: firestore.Client):
    """web 一式(レフェリー・見回り・段階開示の状態)。終わったら、動いているタスクを止める。"""
    from web_helpers import make_web_env  # conftest の import 時に tests/ の部品を読み込まないよう、ここで読む

    env = make_web_env(store, clock, vault_client, default_db)
    yield env
    await env.manager.stop_all()


@pytest.fixture
def session_key() -> str:
    """セッションクッキーの署名の鍵(テスト用。コードにも既定値にも持たず、テストがここで与える。design.md §6.3)。

    本番と同じ条件(base64url で 32 バイト以上。台帳 X-39)を満たす。`secrets.token_urlsafe(32)` で作った値を、
    テストが毎回同じになるよう固定してある(テスト専用の値。本番の鍵には使わない)。
    """
    return "4lNFNQsa4lwGod8A39IKlAQ2fCIdRPgGra7q8CQGuj0"


@pytest.fixture
async def web_app(
    store: VaultStore, clock: FixedClock, vault_client: VaultClient, default_db: firestore.Client, session_key: str
):
    """web の app 一式(画面 API・セッション・削除。金庫は本物の app を ASGI のままつなぐ)。終わったらタスクを止める。"""
    from web_app_helpers import build_web_env  # conftest の import 時に tests/ の部品を読み込まないよう、ここで読む

    env = build_web_env(store=store, clock=clock, vault=vault_client, default_db=default_db, session_key=session_key)
    yield env
    await env.aclose()
