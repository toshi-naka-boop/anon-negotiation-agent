"""pytest 共通フィクスチャ。Firestore エミュレータをセッション単位で起動する。

gcloud コマンドは使わない(~/.config/gcloud に触れないため)。JDK 21 で jar を直接起動する。
本物の GCP には絶対に接続しない: クライアントを作る前に FIRESTORE_EMULATOR_HOST を設定し、
プロジェクト ID は demo- で始まるものにする。認証情報は使わない。
"""

import datetime as dt
import os
import queue
import socket
import subprocess
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from google.cloud import firestore

from vault.app import create_app
from vault.clock import FixedClock
from vault.config import DEFAULT_VAULT_CONFIG
from vault.store import VaultStore

_JAVA_BIN = "/opt/homebrew/opt/openjdk@21/bin/java"
_EMULATOR_JAR = (
    "/opt/homebrew/share/google-cloud-sdk/platform/cloud-firestore-emulator/cloud-firestore-emulator.jar"
)
_READY_MARKER = "Dev App Server is now running"
_STARTUP_TIMEOUT_SECONDS = 30

PROJECT_ID = "demo-vault-test"
DATABASE_ID = "vault-db"


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


@pytest.fixture(scope="session")
def firestore_client(firestore_emulator_host: str) -> firestore.Client:
    """vault-db への Firestore クライアント(セッションで使い回す。データはテストごとに消す)。"""
    client = firestore.Client(project=PROJECT_ID, database=DATABASE_ID)
    yield client
    client.close()


@pytest.fixture(autouse=True)
def _clear_firestore_data(firestore_emulator_host: str) -> None:
    """テストごとに vault-db の全文書を消し、互いに影響しないようにする。"""
    url = (
        f"http://{firestore_emulator_host}/emulator/v1/projects/{PROJECT_ID}"
        f"/databases/{DATABASE_ID}/documents"
    )
    httpx.delete(url, timeout=10)


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
