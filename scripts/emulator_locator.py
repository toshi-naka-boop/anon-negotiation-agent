"""Firestore エミュレータの java と jar の場所を決める(tests/conftest.py と scripts/run_demo.py が共有する)。

決め方(上から順。最初に見つかったものを使う):

- java: 環境変数 FIRESTORE_EMULATOR_JAVA → JAVA_HOME/bin/java → PATH の java → macOS の Homebrew の openjdk@21
- jar:  環境変数 FIRESTORE_EMULATOR_JAR → Google Cloud SDK のよくある場所(Homebrew・apt・手で展開した ~/google-cloud-sdk など。gcloud コマンドは呼ばない)

jar の入れ方: `gcloud components install cloud-firestore-emulator`(Google Cloud SDK が要る)。JDK は 21 を使う。
標準ライブラリだけで書く(pytest の前にも、scripts/ の単体起動でも読めるように)。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

JAVA_ENV = "FIRESTORE_EMULATOR_JAVA"
JAR_ENV = "FIRESTORE_EMULATOR_JAR"

_JAR_RELATIVE = Path("platform") / "cloud-firestore-emulator" / "cloud-firestore-emulator.jar"
_HOMEBREW_JAVA = Path("/opt/homebrew/opt/openjdk@21/bin/java")
_SDK_ROOT_CANDIDATES = (
    Path("/opt/homebrew/share/google-cloud-sdk"),
    Path("/usr/local/share/google-cloud-sdk"),
    Path("/usr/local/gcloud/google-cloud-sdk"),
    Path("/usr/lib/google-cloud-sdk"),
    Path("/usr/share/google-cloud-sdk"),
    Path("/opt/google-cloud-sdk"),
    Path.home() / "google-cloud-sdk",
)


class EmulatorNotFound(RuntimeError):
    """java か jar が見つからない。メッセージに、見た場所と直し方を書く。"""


def _from_env(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def find_java() -> Path:
    """エミュレータを動かす java の実行ファイル。見つからなければ EmulatorNotFound。"""
    explicit = _from_env(JAVA_ENV)
    if explicit is not None:
        if explicit.is_file():
            return explicit
        raise EmulatorNotFound(f"{JAVA_ENV} が指す java がありません: {explicit}")
    candidates: list[Path] = []
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        candidates.append(Path(java_home) / "bin" / "java")
    on_path = shutil.which("java")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(_HOMEBREW_JAVA)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise EmulatorNotFound(
        "Firestore エミュレータを動かす java(JDK 21)が見つかりません。JDK 21 を入れて PATH に通すか、"
        f"環境変数 {JAVA_ENV} に java の実行ファイルのパスを指定してください。"
    )


def find_jar() -> Path:
    """cloud-firestore-emulator.jar。見つからなければ EmulatorNotFound。"""
    explicit = _from_env(JAR_ENV)
    if explicit is not None:
        if explicit.is_file():
            return explicit
        raise EmulatorNotFound(f"{JAR_ENV} が指す jar がありません: {explicit}")
    for root in _SDK_ROOT_CANDIDATES:
        candidate = root / _JAR_RELATIVE
        if candidate.is_file():
            return candidate
    raise EmulatorNotFound(
        "Firestore エミュレータの jar(cloud-firestore-emulator.jar)が見つかりません。"
        "Google Cloud SDK を入れて `gcloud components install cloud-firestore-emulator` を実行するか、"
        f"環境変数 {JAR_ENV} に jar のパスを指定してください。"
    )


def find_emulator() -> tuple[Path, Path]:
    """(java, jar)。どちらかが無ければ EmulatorNotFound。"""
    return find_java(), find_jar()
