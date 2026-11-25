"""scripts/emulator_locator.py: java と jar の場所を、環境変数で上書きでき、見つからないときは直し方を伝える。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import emulator_locator  # noqa: E402


def test_env_vars_override_the_search(tmp_path, monkeypatch):
    java = tmp_path / "java"
    jar = tmp_path / "emulator.jar"
    java.write_text("")
    jar.write_text("")
    monkeypatch.setenv(emulator_locator.JAVA_ENV, str(java))
    monkeypatch.setenv(emulator_locator.JAR_ENV, str(jar))

    assert emulator_locator.find_emulator() == (java, jar)


def test_env_var_pointing_nowhere_names_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(emulator_locator.JAR_ENV, str(tmp_path / "missing.jar"))

    with pytest.raises(emulator_locator.EmulatorNotFound, match=emulator_locator.JAR_ENV):
        emulator_locator.find_jar()


def test_missing_jar_tells_how_to_install_it(monkeypatch):
    monkeypatch.delenv(emulator_locator.JAR_ENV, raising=False)
    monkeypatch.setattr(emulator_locator, "_SDK_ROOT_CANDIDATES", ())

    with pytest.raises(emulator_locator.EmulatorNotFound) as error:
        emulator_locator.find_jar()

    assert "gcloud components install cloud-firestore-emulator" in str(error.value)
    assert emulator_locator.JAR_ENV in str(error.value)


def test_missing_java_names_the_variable(monkeypatch):
    monkeypatch.delenv(emulator_locator.JAVA_ENV, raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(emulator_locator, "_HOMEBREW_JAVA", Path("/nonexistent/java"))

    with pytest.raises(emulator_locator.EmulatorNotFound, match=emulator_locator.JAVA_ENV):
        emulator_locator.find_java()
