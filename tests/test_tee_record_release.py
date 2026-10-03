"""scripts/tee_record_release.py と scripts/tee_reset_dek.py(TEE スパイクの補助スクリプト。契約 §13)。

tee_record_release(digest↔コミットの対応表 deploy/vault-releases.json への追記と、失効)
- 空の表にも既存の表にも、末尾に追記する。追記する項目は status が "active"。JSON は indent=2・末尾に改行(既存のファイルと同じ形)。既存の項目は変えない。
- 同じ digest は(active でも revoked でも)上書きも復活もせず(ファイルが変わらない)、終了コード 0 で already recorded。
- --revoke <digest>: 表にある項目の status を "revoked" にして revoked_at をつける。項目は消さず、順序も変えない。
  表に無い digest は終了コード 2。すでに revoked なら、何も書かずに終了コード 0(already revoked)。--digest・--commit・--built-at とは一緒に使えない(2)。
- 形の違い(digest・commit・--built-at・表のファイル)は、何も書かずに終了コード 2。
- --built-at を省くと、現在の UTC の ISO 8601 が入る。

tee_reset_dek(debug の VM の間に作った DEK の削除。research R22)
- import できる。--yes が無ければ、Firestore のクライアントも作らずに、説明だけ出す。
- --yes なら、_tee/dek と _tee/selftest だけが消え、ほかの文書は残る。文書の中身は表示しない。接続先(本物かエミュレータか)を、削除の前に表示する。
  失敗したら終了コード 1。

GCP には接続しない: Firestore はエミュレータだけ(--yes の試験は、エミュレータが設定されていることを確かめてから動かす)。
"""

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))
import tee_record_release as record_script  # noqa: E402  (scripts/ を import できるようにしてから読む)
import tee_reset_dek as reset_script  # noqa: E402

DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "cd" * 32
THIRD_DIGEST = "sha256:" + "ef" * 32
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "f" * 40
BUILT_AT = "2026-10-03T12:00:00Z"
REVOKED_AT = "2026-10-05T00:00:00Z"


def entry(digest: str = DIGEST, commit: str = COMMIT, built_at: str = BUILT_AT) -> dict:
    return {"digest": digest, "commit": commit, "built_at": built_at, "status": "active"}


def revoked_entry(digest: str = DIGEST, commit: str = COMMIT, revoked_at: str = REVOKED_AT) -> dict:
    return {**entry(digest, commit), "status": "revoked", "revoked_at": revoked_at}


def write_table(path: Path, releases: list[dict]) -> Path:
    """リポジトリの deploy/vault-releases.json と同じ形(indent=2・末尾に改行)で、表を書く。"""
    path.write_text(json.dumps({"releases": releases}, indent=2) + "\n", encoding="utf-8")
    return path


def read_releases(table: Path) -> list[dict]:
    return json.loads(table.read_text(encoding="utf-8"))["releases"]


def run(*argv: str) -> int:
    """record_script.main を動かして、終了コードを返す(argparse の SystemExit も、終了コードとして返す)。"""
    try:
        return record_script.main(list(argv))
    except SystemExit as exc:
        return exc.code


def record(table: Path, *, digest: str = DIGEST, commit: str = COMMIT, built_at: str | None = BUILT_AT) -> int:
    argv = ["--releases", str(table), "--digest", digest, "--commit", commit]
    if built_at is not None:
        argv += ["--built-at", built_at]
    return run(*argv)


def revoke(table: Path, digest: str = DIGEST, *extra: str) -> int:
    return run("--releases", str(table), "--revoke", digest, *extra)


# ----------------------------------------------------------------------
# tee_record_release: 追記
# ----------------------------------------------------------------------


def test_appends_to_an_empty_table_in_the_agreed_format(tmp_path):
    table = write_table(tmp_path / "vault-releases.json", [])

    assert record(table) == 0

    assert table.read_text(encoding="utf-8") == json.dumps({"releases": [entry()]}, indent=2) + "\n"


def test_appends_at_the_end_and_keeps_existing_entries(tmp_path):
    existing = [entry("sha256:" + "11" * 32, "1" * 40, "2026-10-01T00:00:00Z"), revoked_entry("sha256:" + "22" * 32, "2" * 40)]
    table = write_table(tmp_path / "vault-releases.json", existing)

    assert record(table) == 0

    assert read_releases(table) == [*existing, entry()]


def test_the_new_entry_has_exactly_the_agreed_keys_and_is_active(tmp_path):
    table = write_table(tmp_path / "vault-releases.json", [])
    record(table)

    (recorded,) = read_releases(table)
    assert list(recorded) == ["digest", "commit", "built_at", "status"]
    assert recorded["status"] == "active"


def test_the_message_says_what_was_recorded(tmp_path, capsys):
    table = write_table(tmp_path / "vault-releases.json", [])
    record(table)

    assert DIGEST in capsys.readouterr().out


# ----------------------------------------------------------------------
# tee_record_release: 重複
# ----------------------------------------------------------------------


def test_the_same_digest_is_not_overwritten(tmp_path, capsys):
    table = write_table(tmp_path / "vault-releases.json", [entry(built_at="2026-10-01T00:00:00Z")])
    before = table.read_bytes()

    assert record(table, commit=OTHER_COMMIT, built_at="2026-10-09T00:00:00Z") == 0

    assert table.read_bytes() == before
    captured = capsys.readouterr()
    assert "already recorded" in captured.out
    assert COMMIT in captured.out  # すでに記録されているコミットが分かる
    assert "注意" in captured.err and OTHER_COMMIT in captured.err  # 渡されたコミットが違うことは、黙らない


def test_a_revoked_digest_is_not_reactivated_by_recording_it_again(tmp_path, capsys):
    table = write_table(tmp_path / "vault-releases.json", [revoked_entry()])
    before = table.read_bytes()

    assert record(table) == 0

    assert table.read_bytes() == before
    assert read_releases(table)[0]["status"] == "revoked"
    out = capsys.readouterr().out
    assert "already recorded" in out and "revoked" in out


def test_running_twice_records_once_and_the_second_run_is_quiet_on_stderr(tmp_path, capsys):
    table = write_table(tmp_path / "vault-releases.json", [])

    assert record(table) == 0
    first = table.read_bytes()
    capsys.readouterr()
    assert record(table) == 0

    assert table.read_bytes() == first
    captured = capsys.readouterr()
    assert "already recorded" in captured.out
    assert captured.err == ""


def test_a_different_digest_is_recorded_next_to_the_existing_one(tmp_path):
    table = write_table(tmp_path / "vault-releases.json", [entry()])

    assert record(table, digest=OTHER_DIGEST, commit=OTHER_COMMIT) == 0

    assert [item["digest"] for item in read_releases(table)] == [DIGEST, OTHER_DIGEST]


# ----------------------------------------------------------------------
# tee_record_release: 失効(--revoke)
# ----------------------------------------------------------------------


def test_revoke_marks_the_entry_revoked_and_stamps_revoked_at(tmp_path, capsys):
    untouched = entry(OTHER_DIGEST, OTHER_COMMIT)
    table = write_table(tmp_path / "vault-releases.json", [entry(), untouched])
    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    assert revoke(table) == 0

    revoked, other = read_releases(table)
    assert list(revoked) == ["digest", "commit", "built_at", "status", "revoked_at"]
    assert (revoked["digest"], revoked["commit"], revoked["built_at"], revoked["status"]) == (DIGEST, COMMIT, BUILT_AT, "revoked")
    assert other == untouched
    revoked_at = dt.datetime.fromisoformat(revoked["revoked_at"])
    assert revoked_at.utcoffset() == dt.timedelta(0)
    assert before <= revoked_at <= dt.datetime.now(dt.timezone.utc)
    assert table.read_text(encoding="utf-8") == json.dumps({"releases": [revoked, other]}, indent=2) + "\n"
    assert "revoked:" in capsys.readouterr().out


def test_revoke_keeps_every_entry_in_its_place(tmp_path):
    entries = [entry(DIGEST), entry(OTHER_DIGEST, OTHER_COMMIT), entry(THIRD_DIGEST, "e" * 40)]
    table = write_table(tmp_path / "vault-releases.json", entries)

    assert revoke(table, OTHER_DIGEST) == 0

    releases = read_releases(table)
    assert [item["digest"] for item in releases] == [DIGEST, OTHER_DIGEST, THIRD_DIGEST]
    assert [item["status"] for item in releases] == ["active", "revoked", "active"]


def test_revoking_an_unknown_digest_exits_with_2_and_leaves_the_table(tmp_path, capsys):
    table = write_table(tmp_path / "vault-releases.json", [entry(OTHER_DIGEST, OTHER_COMMIT)])
    before = table.read_bytes()

    assert revoke(table, DIGEST) == 2

    assert table.read_bytes() == before
    err = capsys.readouterr().err
    assert DIGEST in err and "表にありません" in err


def test_revoking_on_an_empty_table_exits_with_2(tmp_path):
    table = write_table(tmp_path / "vault-releases.json", [])
    before = table.read_bytes()

    assert revoke(table) == 2

    assert table.read_bytes() == before


def test_revoking_twice_keeps_the_first_revoked_at(tmp_path, capsys):
    table = write_table(tmp_path / "vault-releases.json", [revoked_entry()])
    before = table.read_bytes()

    assert revoke(table) == 0

    assert table.read_bytes() == before
    assert read_releases(table)[0]["revoked_at"] == REVOKED_AT
    assert "already revoked" in capsys.readouterr().out


@pytest.mark.parametrize("digest", ["", "sha256:", "sha256:" + "a" * 63, "sha256:" + "A" * 64, "a" * 64, "sha256:" + "a" * 64 + "\n"])
def test_revoke_with_a_malformed_digest_exits_with_2(tmp_path, digest):
    table = write_table(tmp_path / "vault-releases.json", [entry()])
    before = table.read_bytes()

    assert revoke(table, digest) == 2

    assert table.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [("--digest", DIGEST), ("--commit", COMMIT), ("--built-at", BUILT_AT)],
    ids=["digest", "commit", "built-at"],
)
def test_revoke_cannot_be_combined_with_the_recording_options(tmp_path, extra):
    table = write_table(tmp_path / "vault-releases.json", [entry()])
    before = table.read_bytes()

    assert revoke(table, DIGEST, *extra) == 2

    assert table.read_bytes() == before


def test_revoke_on_a_table_in_the_wrong_shape_exits_with_2(tmp_path):
    table = tmp_path / "vault-releases.json"
    table.write_text('{"releases": [{"digest": "x"}]}', encoding="utf-8")

    assert revoke(table) == 2

    assert table.read_text(encoding="utf-8") == '{"releases": [{"digest": "x"}]}'


# ----------------------------------------------------------------------
# tee_record_release: 形の違いは、何も書かずに終了コード 2
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "sha256:",
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "A" * 64,  # 大文字
        "sha256:" + "g" * 64,  # 16 進でない
        "a" * 64,  # sha256: が無い
        "sha1:" + "a" * 40,
        " sha256:" + "a" * 64,
        "sha256:" + "a" * 64 + "\n",
    ],
)
def test_a_malformed_digest_is_rejected_and_nothing_is_written(tmp_path, digest):
    table = write_table(tmp_path / "vault-releases.json", [])
    before = table.read_bytes()

    assert record(table, digest=digest) == 2

    assert table.read_bytes() == before


@pytest.mark.parametrize(
    "commit",
    ["", "a" * 39, "a" * 41, "A" * 40, "abc1234", "g" * 40, "a" * 40 + "\n"],
)
def test_a_malformed_commit_is_rejected_and_nothing_is_written(tmp_path, commit):
    table = write_table(tmp_path / "vault-releases.json", [])
    before = table.read_bytes()

    assert record(table, commit=commit) == 2

    assert table.read_bytes() == before


@pytest.mark.parametrize("built_at", ["", "yesterday", "12:00", "2026-10-03", "2026-10-03T12:00:00"])
def test_a_built_at_that_is_not_iso_8601_with_a_timezone_is_rejected(tmp_path, built_at):
    table = write_table(tmp_path / "vault-releases.json", [])
    before = table.read_bytes()

    assert record(table, built_at=built_at) == 2

    assert table.read_bytes() == before


@pytest.mark.parametrize("built_at", ["2026-10-03T12:00:00Z", "2026-10-03T21:00:00+09:00", "2026-10-03T12:00:00+00:00"])
def test_a_built_at_with_a_timezone_is_stored_as_given(tmp_path, built_at):
    table = write_table(tmp_path / "vault-releases.json", [])

    assert record(table, built_at=built_at) == 0

    assert read_releases(table)[0]["built_at"] == built_at


@pytest.mark.parametrize(
    "argv",
    [[], ["--digest", DIGEST], ["--commit", COMMIT], ["--built-at", BUILT_AT]],
    ids=["nothing", "digest-only", "commit-only", "built-at-only"],
)
def test_recording_needs_both_digest_and_commit(tmp_path, argv):
    table = write_table(tmp_path / "vault-releases.json", [])
    before = table.read_bytes()

    assert run("--releases", str(table), *argv) == 2

    assert table.read_bytes() == before


def test_built_at_defaults_to_the_current_utc_time(tmp_path):
    table = write_table(tmp_path / "vault-releases.json", [])
    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    assert record(table, built_at=None) == 0

    built_at = dt.datetime.fromisoformat(read_releases(table)[0]["built_at"])
    assert built_at.utcoffset() == dt.timedelta(0)
    assert before <= built_at <= dt.datetime.now(dt.timezone.utc)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "",
        "[]",
        '{"other": []}',
        '{"releases": {}}',
        '{"releases": [1]}',
        '{"releases": [{"commit": "x", "status": "active"}]}',
        '{"releases": [{"digest": 1, "status": "active"}]}',
        '{"releases": [{"digest": "sha256:aa"}]}',
        '{"releases": [{"digest": "sha256:aa", "status": "pending"}]}',
    ],
    ids=[
        "not-json", "empty", "list", "no-releases", "releases-not-a-list", "entry-not-an-object",
        "no-digest", "digest-not-a-string", "no-status", "unknown-status",
    ],
)
def test_a_table_in_the_wrong_shape_is_rejected_and_left_untouched(tmp_path, content, capsys):
    table = tmp_path / "vault-releases.json"
    table.write_text(content, encoding="utf-8")

    assert record(table) == 2

    assert table.read_text(encoding="utf-8") == content
    assert "エラー" in capsys.readouterr().err


def test_a_missing_table_is_rejected_and_not_created(tmp_path):
    table = tmp_path / "no-such-file.json"

    assert record(table) == 2

    assert not table.exists()


def test_the_default_table_is_the_one_in_deploy():
    assert record_script.DEFAULT_RELEASES_PATH == Path(__file__).resolve().parents[1] / "deploy" / "vault-releases.json"


def test_the_script_runs_as_a_command_and_reports_the_exit_code(tmp_path):
    table = write_table(tmp_path / "vault-releases.json", [])
    command = [sys.executable, str(SCRIPTS_DIRECTORY / "tee_record_release.py"), "--releases", str(table)]

    ok = subprocess.run([*command, "--digest", DIGEST, "--commit", COMMIT], capture_output=True, text=True)
    bad = subprocess.run([*command, "--digest", "sha256:xyz", "--commit", COMMIT], capture_output=True, text=True)
    revoked = subprocess.run([*command, "--revoke", DIGEST], capture_output=True, text=True)
    unknown = subprocess.run([*command, "--revoke", OTHER_DIGEST], capture_output=True, text=True)

    assert (ok.returncode, bad.returncode, revoked.returncode, unknown.returncode) == (0, 2, 0, 2)
    assert [(item["digest"], item["status"]) for item in read_releases(table)] == [(DIGEST, "revoked")]


# ----------------------------------------------------------------------
# tee_reset_dek
# ----------------------------------------------------------------------


def forbidden(*args, **kwargs):
    raise AssertionError("Firestore に触れてはいけない")


@pytest.mark.parametrize("argv", [[], ["--project", "some-project"]])
def test_reset_without_yes_only_explains_and_never_touches_firestore(monkeypatch, capsys, argv):
    from google.cloud import firestore

    monkeypatch.setattr(reset_script, "create_client", forbidden)
    monkeypatch.setattr(firestore, "Client", forbidden)

    assert reset_script.main(argv) == 1

    out = capsys.readouterr().out
    assert "--yes" in out
    assert all(path in out for path in ("_tee/dek", "_tee/selftest", "vault-db"))


def test_reset_targets_the_documents_the_vault_uses():
    assert reset_script.DOCUMENT_PATHS == ("_tee/dek", "_tee/selftest")
    assert reset_script.VAULT_DATABASE == "vault-db"


def test_reset_with_yes_deletes_only_the_two_tee_documents(firestore_client, firestore_project_id, capsys):
    assert os.environ.get("FIRESTORE_EMULATOR_HOST"), "本物の GCP に接続しないよう、エミュレータが設定されているときだけ動かす"
    firestore_client.document("_tee/dek").set({"wrapped_dek": b"CANARY-WRAPPED-DEK", "kek": "key"})
    firestore_client.document("_tee/selftest").set({"probe": b"CANARY-SEALED-PROBE"})
    firestore_client.document("_tee/other").set({"keep": True})
    firestore_client.document("principals/p1").set({"keep": True})

    assert reset_script.main(["--yes", "--project", firestore_project_id]) == 0

    assert not firestore_client.document("_tee/dek").get().exists
    assert not firestore_client.document("_tee/selftest").get().exists
    assert firestore_client.document("_tee/other").get().exists
    assert firestore_client.document("principals/p1").get().exists
    captured = capsys.readouterr()
    assert "削除した: _tee/dek" in captured.out and "削除した: _tee/selftest" in captured.out
    assert "接続先: エミュレータ" in captured.out and firestore_project_id in captured.out
    assert "CANARY" not in captured.out + captured.err  # 文書の中身は表示しない


def test_reset_with_yes_succeeds_when_the_documents_are_already_gone(firestore_project_id, firestore_client, capsys):
    assert os.environ.get("FIRESTORE_EMULATOR_HOST"), "本物の GCP に接続しないよう、エミュレータが設定されているときだけ動かす"

    assert reset_script.main(["--yes", "--project", firestore_project_id]) == 0

    assert capsys.readouterr().out.count("もともと無かった") == 2


class FakeDocument:
    def __init__(self, deleted: list[str], path: str):
        self.deleted, self.path = deleted, path

    def get(self):
        return type("Snapshot", (), {"exists": True})()

    def delete(self):
        self.deleted.append(self.path)


class FakeClient:
    project = "some-project"

    def __init__(self):
        self.deleted: list[str] = []

    def document(self, path: str) -> FakeDocument:
        return FakeDocument(self.deleted, path)

    def close(self):
        pass


def test_reset_says_it_is_talking_to_the_real_firestore_when_no_emulator_is_configured(monkeypatch, capsys):
    """エミュレータの設定が残っていると、本物の DEK を消したつもりで消していない、ということが起きる。どちらに向けたかを表示する。"""
    client = FakeClient()
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.setattr(reset_script, "create_client", lambda project=None: client)

    assert reset_script.main(["--yes", "--project", "some-project"]) == 0

    assert "接続先: 本物の Firestore / プロジェクト some-project / データベース vault-db" in capsys.readouterr().out
    assert client.deleted == ["_tee/dek", "_tee/selftest"]


def test_reset_reports_a_failure_with_exit_code_1(monkeypatch, capsys):
    def fail(project=None):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(reset_script, "create_client", fail)

    assert reset_script.main(["--yes"]) == 1

    err = capsys.readouterr().err
    assert "RuntimeError" in err and "gcloud auth application-default login" in err
