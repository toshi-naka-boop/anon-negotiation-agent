"""金庫の起動時のテンプレートの投入(src/vault/seed.py。design.md §3.7・台帳 P-15)。

Firestore は conftest が起動するエミュレータ。フィクスチャは、本物の fixtures/ と、tmp_path に作った小さなもの(変えたもの・壊したもの)。
確かめること:

- 初回に、fixtures/case*.toml のすべてのケースの候補者と求人が、vault-db の templates/{template_id} に入る。読み戻した中身は
  フィクスチャが作るテンプレートと同じで、封印していない(平文)。入ったテンプレートから、デモの交渉が作れる。
- 2 回目以降は書かない(Firestore への書き込みの呼び出しが 0 回。文書の更新時刻も変わらない)。件数は written と skipped に出る。
- フィクスチャの中身を変えると、変わったテンプレートだけが上書きされる。知らない項目が残っている文書も、置き換わる。
- 壊れたフィクスチャ(形・グリッド外の値・番号の食い違い・名前・同じ template_id で中身が違う・1 つも無い)は例外で、1 件も書かない。
  ほかのファイルが正しくても、同じ。ログにあるのは、ファイル名と例外の型名だけ。
- Firestore の失敗(読み・書き)は、握りつぶさずに例外になる。
- ログに出るのは件数だけで、テンプレートの値(企業名・連絡先など)は出ない。
- Cloud Run 版の起動口(vault.app.create_app_from_env)は、既定で投入し、VAULT_SEED_TEMPLATES=false では投入しない
  (true・false 以外は、Firestore に触る前に拒否する)。投入したテンプレートで、起動口の app から交渉が作れる。
TEE 版の起動口(vault.tee.main)の、起動の順序と、失敗したときの終了コードは、tests/test_tee_key_release.py にある。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import PermissionDenied, ServiceUnavailable
from google.cloud import firestore

import vault.app as vault_app_module
from vault.fixtures import FIXTURES_DIRECTORY, load_case_fixture
from vault.models import Template
from vault.seed import SeedResult, seed_templates
from vault.serialization import model_to_firestore
from vault.templates import TEMPLATES_COLLECTION, get_template
from vault_helpers import demo_create_request


def case_toml(
    case: int = 1,
    *,
    candidate_id: str | None = None,
    band: str = "5_to_10y",
    candidate_min: int = 600,
    employer_max: int = 650,
    employer_category: str = "it_web",
) -> str:
    """小さなケースのファイル(1 つのケースの候補者 1 人と求人 1 件)。引数で、テンプレートの中身を変えられる。

    候補者の band・candidate_min は候補者のテンプレート、employer_max・employer_category は求人のテンプレートに入る。
    """
    candidate_id = candidate_id or f"case{case}-candidate"
    return f"""
case = {case}

[candidate]
template_id = "{candidate_id}"
attribute_bands = {{ experience_band = "{band}", region_block = "kanto", job_category = "it_web" }}
job_summary = "テスト用の職務要約"

[candidate.contact]
name = "架空 太郎"
email = "taro.kako@example.com"

[[candidate.raw_conditions]]
night_duty = [0, 2]
min_salary = {candidate_min}

[employer]
template_id = "case{case}-employer"
company_id = "case{case}-company"
job_id = "case{case}-job"
company_name = "株式会社テスト商事(架空)"

[employer.public_job]
title = "テスト用の求人"
summary = "テスト用の求人の説明"
confidential = false
job_category = "{employer_category}"

[employer.auto_response]
meet = true
approve = true

[[employer.rules]]
when = {{}}

[[employer.rules.raw_conditions]]
max_salary = {employer_max}
"""


def write_case(directory: Path, case: int = 1, *, name: str | None = None, text: str | None = None, **changes) -> Path:
    """directory に case<N>.toml(name を渡せばその名前)を書く。text を渡せばその中身、なければ case_toml(case, **changes)。"""
    path = directory / (name or f"case{case}.toml")
    path.write_text(case_toml(case, **changes) if text is None else text, encoding="utf-8")
    return path


def real_case_numbers() -> list[int]:
    """本物の fixtures/ にある case<N>.toml の N。"""
    names = (path.name for path in FIXTURES_DIRECTORY.glob("case*.toml"))
    numbers = sorted(int(matched.group(1)) for name in names if (matched := re.fullmatch(r"case(\d+)\.toml", name)))
    assert numbers, "fixtures/ に case*.toml がない"
    return numbers


def real_templates() -> dict[str, Template]:
    """本物の fixtures/case*.toml が作るテンプレート(template_id → テンプレート)。投入の結果の期待値。"""
    return {t.template_id: t for number in real_case_numbers() for t in load_case_fixture(number).templates()}


def stored_ids(db: firestore.Client) -> set[str]:
    return {snapshot.id for snapshot in db.collection(TEMPLATES_COLLECTION).stream()}


def stored_dict(db: firestore.Client, template_id: str) -> dict | None:
    return db.collection(TEMPLATES_COLLECTION).document(template_id).get().to_dict()


@pytest.fixture
def writes(monkeypatch) -> list[str]:
    """Firestore への書き込み(DocumentReference.set)の呼び出しを、文書のパスで記録する。書き込みの回数を数える道具。"""
    paths: list[str] = []
    original = firestore.DocumentReference.set

    def recording_set(self, *args, **kwargs):
        paths.append(self.path)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(firestore.DocumentReference, "set", recording_set)
    return paths


# ----------------------------------------------------------------------
# 初回: すべてのテンプレートが入る
# ----------------------------------------------------------------------


def test_the_first_start_writes_the_templates_of_every_case_file_in_fixtures(firestore_client):
    expected = real_templates()

    result = seed_templates(firestore_client)

    assert result == SeedResult(written=len(expected), skipped=0)
    assert stored_ids(firestore_client) == set(expected)
    for template_id, template in expected.items():
        assert get_template(firestore_client, template_id) == template
        stored = stored_dict(firestore_client, template_id)
        assert stored == model_to_firestore(template)
        assert not any(isinstance(value, bytes) for value in stored.values())  # 公開フィクスチャなので封印しない(§3.8)


def test_a_demo_negotiation_can_be_created_from_the_seeded_templates_of_each_case(firestore_client, store):
    seed_templates(firestore_client)

    for number in real_case_numbers():
        fixture = load_case_fixture(number)
        created = store.create_negotiation(demo_create_request(fixture.candidate.template_id, fixture.employer.template_id))
        assert created.status == "created", f"case {number}: {created.reason}"


def test_every_case_file_is_read_whatever_its_number(tmp_path, firestore_client):
    for case in (1, 2, 10):
        write_case(tmp_path, case)

    result = seed_templates(firestore_client, tmp_path)

    assert result == SeedResult(written=6, skipped=0)
    assert stored_ids(firestore_client) == {f"case{case}-{who}" for case in (1, 2, 10) for who in ("candidate", "employer")}


# ----------------------------------------------------------------------
# 冪等: 同じ中身なら書かない
# ----------------------------------------------------------------------


def test_the_second_start_writes_nothing(firestore_client, writes):
    expected = real_templates()
    seed_templates(firestore_client)
    update_times = {tid: firestore_client.document(f"templates/{tid}").get().update_time for tid in expected}
    assert sorted(writes) == sorted(f"templates/{tid}" for tid in expected)  # 初回は、1 件 1 回ずつ書いた
    writes.clear()

    result = seed_templates(firestore_client)

    assert result == SeedResult(written=0, skipped=len(expected))
    assert writes == []
    assert {tid: firestore_client.document(f"templates/{tid}").get().update_time for tid in expected} == update_times


# ----------------------------------------------------------------------
# フィクスチャの中身を変えると、上書きされる
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "changed"),
    [
        pytest.param({"band": "10y_plus"}, ["case1-candidate"], id="candidate_attribute_band"),
        pytest.param({"candidate_min": 550}, ["case1-candidate"], id="candidate_raw_conditions"),
        pytest.param({"employer_max": 600}, ["case1-employer"], id="employer_raw_conditions"),
        pytest.param({"employer_category": "sales"}, ["case1-employer"], id="employer_job_category"),
        pytest.param({"band": "10y_plus", "employer_max": 600}, ["case1-candidate", "case1-employer"], id="both"),
    ],
)
def test_a_changed_fixture_overwrites_only_the_templates_that_changed(tmp_path, firestore_client, writes, change, changed):
    write_case(tmp_path)
    seed_templates(firestore_client, tmp_path)
    old = {tid: stored_dict(firestore_client, tid) for tid in ("case1-candidate", "case1-employer")}
    writes.clear()

    write_case(tmp_path, **change)
    result = seed_templates(firestore_client, tmp_path)

    new = {t.template_id: t for t in load_case_fixture(1, tmp_path).templates()}
    assert result == SeedResult(written=len(changed), skipped=2 - len(changed))
    assert writes == [f"templates/{tid}" for tid in changed]
    for template_id, template in new.items():
        stored = stored_dict(firestore_client, template_id)
        assert stored == model_to_firestore(template)  # 新しい中身になっている
        assert (stored != old[template_id]) == (template_id in changed)  # 変わったものだけが、前と違う
    assert seed_templates(firestore_client, tmp_path) == SeedResult(written=0, skipped=2)  # 置き換えたあとは、また書かない


def test_a_stored_document_with_a_leftover_field_is_replaced_as_a_whole(tmp_path, firestore_client, writes):
    write_case(tmp_path)
    expected = {t.template_id: t for t in load_case_fixture(1, tmp_path).templates()}
    stale = {**model_to_firestore(expected["case1-candidate"]), "legacy_field": "from an older schema"}
    firestore_client.document("templates/case1-candidate").set(stale)
    writes.clear()

    result = seed_templates(firestore_client, tmp_path)

    assert result == SeedResult(written=2, skipped=0)  # 求人は無かったので新規、候補者は知らない項目が残っていたので置き換え
    assert sorted(writes) == ["templates/case1-candidate", "templates/case1-employer"]
    assert stored_dict(firestore_client, "case1-candidate") == model_to_firestore(expected["case1-candidate"])  # legacy_field は消えた


# ----------------------------------------------------------------------
# 壊れたフィクスチャは例外。1 件も書かない
# ----------------------------------------------------------------------


def broken_cases() -> dict[str, tuple[dict[str, str], str]]:
    """壊れたフィクスチャの例: {名前: ({ファイル名: 中身}, 例外の文に含まれる語)}。正しい case1.toml と一緒に置いても、1 件も書かない。"""
    return {
        "off_grid_value": ({"case2.toml": case_toml(2).replace("night_duty = [0, 2]", "night_duty = [0, 3]")}, "not on the grid"),
        "unknown_key": ({"case2.toml": case_toml(2).replace("min_salary = 600", "min_salary = 600\nmax_salary = 700")}, "Extra inputs"),
        "missing_part": ({"case2.toml": "case = 2\n"}, "Field required"),
        "not_toml": ({"case2.toml": "case = = 2"}, "Invalid"),
        "declared_number_differs_from_the_name": ({"case2.toml": case_toml(3)}, "declares case"),
        "name_is_not_case_n": ({"case_notes.toml": case_toml(2)}, "must be named"),
        "name_has_a_leading_zero": ({"case02.toml": case_toml(2)}, "must be named"),
        "same_template_id_with_different_contents": (
            {"case2.toml": case_toml(2, candidate_id="case1-candidate", band="under_3y")},
            "different contents",
        ),
    }


@pytest.mark.parametrize("name", list(broken_cases()))
def test_a_broken_fixture_raises_and_nothing_is_written_even_when_the_other_files_are_fine(
    tmp_path, firestore_client, writes, caplog, name
):
    files, message = broken_cases()[name]
    write_case(tmp_path)  # 正しい case1.toml
    for file_name, text in files.items():
        write_case(tmp_path, name=file_name, text=text)
    caplog.set_level(logging.INFO)

    with pytest.raises(ValueError, match=message):
        seed_templates(firestore_client, tmp_path)

    assert writes == [] and stored_ids(firestore_client) == set()
    (broken_file,) = files
    assert f"the fixture file {broken_file} is not usable" in caplog.text  # どのファイルかが分かる
    assert "templates seeded" not in caplog.text


def test_a_directory_without_a_case_file_raises(tmp_path, firestore_client, writes):
    (tmp_path / "interview_templates.toml").write_text("title = 'not a case'\n", encoding="utf-8")  # case*.toml ではないものは数えない

    with pytest.raises(FileNotFoundError, match=r"case\*\.toml"):
        seed_templates(firestore_client, tmp_path)
    with pytest.raises(FileNotFoundError):
        seed_templates(firestore_client, tmp_path / "does-not-exist")

    assert writes == []


def test_the_same_template_in_two_files_with_the_same_contents_is_written_once(tmp_path, firestore_client, writes):
    write_case(tmp_path, 1)
    write_case(tmp_path, 2, candidate_id="case1-candidate")  # 候補者は case1 と同じ id・同じ中身。求人は別

    result = seed_templates(firestore_client, tmp_path)

    assert result == SeedResult(written=3, skipped=0)
    assert sorted(writes) == ["templates/case1-candidate", "templates/case1-employer", "templates/case2-employer"]


# ----------------------------------------------------------------------
# Firestore の失敗は例外
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "error"),
    [("get", PermissionDenied("denied")), ("set", ServiceUnavailable("unavailable"))],
    ids=["read_fails", "write_fails"],
)
def test_a_firestore_failure_is_raised_not_swallowed(tmp_path, firestore_client, monkeypatch, caplog, method, error):
    write_case(tmp_path)

    def fail(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(firestore.DocumentReference, method, fail)
    caplog.set_level(logging.INFO)

    with pytest.raises(type(error)):
        seed_templates(firestore_client, tmp_path)

    assert "templates seeded" not in caplog.text  # 成功したかのようなログを書かない


# ----------------------------------------------------------------------
# ログには件数だけ
# ----------------------------------------------------------------------


def test_the_log_has_only_the_two_counts_and_none_of_the_values(tmp_path, firestore_client, caplog):
    write_case(tmp_path)
    caplog.set_level(logging.INFO)

    seed_templates(firestore_client, tmp_path)
    seed_templates(firestore_client, tmp_path)

    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("vault.seed", logging.INFO, "templates seeded: written=2 skipped=0"),
        ("vault.seed", logging.INFO, "templates seeded: written=0 skipped=2"),
    ]
    values = ("case1-candidate", "case1-employer", "case1-company", "case1-job", "株式会社テスト商事", "taro.kako", "example.com", "テスト用の")
    for value in values:
        assert value not in caplog.text


# ----------------------------------------------------------------------
# Cloud Run 版の起動口
# ----------------------------------------------------------------------


@dataclass
class EntryPoint:
    """Cloud Run 版の起動口。Firestore のクライアントだけ、エミュレータのものに差し替えてある(作った回数と、投入の呼び出しを記録する)。"""

    db: firestore.Client
    db_created: int = 0
    seed_calls: list[tuple] = field(default_factory=list)

    def start(self, environ=None):
        return vault_app_module.create_app_from_env(environ)


@pytest.fixture
def entry_point(monkeypatch, firestore_client) -> EntryPoint:
    entry = EntryPoint(db=firestore_client)

    def create_db():
        entry.db_created += 1
        return firestore_client

    def recording_seed(db, directory):
        entry.seed_calls.append((db, directory))
        return seed_templates(db, directory)

    monkeypatch.setattr(vault_app_module, "_create_vault_db", create_db)
    monkeypatch.setattr(vault_app_module, "seed_templates", recording_seed)
    monkeypatch.delenv(vault_app_module.SEED_TEMPLATES_ENV, raising=False)
    return entry


def test_the_cloud_run_entry_point_seeds_the_templates_by_default(entry_point, firestore_client):
    entry_point.start({})

    assert entry_point.seed_calls == [(firestore_client, FIXTURES_DIRECTORY)]
    assert stored_ids(firestore_client) == set(real_templates())


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_the_cloud_run_entry_point_seeds_when_it_is_turned_on_in_any_case(entry_point, firestore_client, value):
    entry_point.start({"VAULT_SEED_TEMPLATES": value})

    assert len(entry_point.seed_calls) == 1
    assert stored_ids(firestore_client) == set(real_templates())


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_the_cloud_run_entry_point_does_not_seed_when_it_is_turned_off(entry_point, firestore_client, value):
    app = entry_point.start({"VAULT_SEED_TEMPLATES": value})

    assert entry_point.seed_calls == []
    assert stored_ids(firestore_client) == set()
    assert TestClient(app).get("/v1/principals/0123456789abcdef/policy").status_code == 404  # 金庫の app は動いている


def test_the_cloud_run_entry_point_reads_the_process_environment_when_no_environment_is_given(
    entry_point, firestore_client, monkeypatch
):
    monkeypatch.setenv("VAULT_SEED_TEMPLATES", "false")

    entry_point.start()

    assert entry_point.seed_calls == [] and stored_ids(firestore_client) == set()


@pytest.mark.parametrize("value", ["", "yes", "1", "flase", "on"])
def test_the_cloud_run_entry_point_refuses_a_value_that_is_not_true_or_false_before_touching_firestore(
    entry_point, firestore_client, value
):
    with pytest.raises(ValueError, match="VAULT_SEED_TEMPLATES must be true or false"):
        entry_point.start({"VAULT_SEED_TEMPLATES": value})

    assert entry_point.db_created == 0 and entry_point.seed_calls == []
    assert stored_ids(firestore_client) == set()


def test_the_cloud_run_entry_point_does_not_start_when_the_seeding_fails(entry_point, monkeypatch, tmp_path):
    monkeypatch.setattr(vault_app_module, "FIXTURES_DIRECTORY", tmp_path)  # case*.toml が 1 つも無い

    with pytest.raises(FileNotFoundError):
        entry_point.start({})


def test_the_app_from_the_cloud_run_entry_point_creates_a_negotiation_from_the_seeded_templates(entry_point):
    client = TestClient(entry_point.start({}))
    fixture = load_case_fixture(1)

    request = demo_create_request(fixture.candidate.template_id, fixture.employer.template_id)
    response = client.post("/v1/negotiations", json=request.model_dump(mode="json"))

    assert response.status_code == 200
    assert response.json()["status"] == "created"
