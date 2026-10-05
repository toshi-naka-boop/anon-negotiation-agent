"""scripts/serve_local.py(開発者が手元のブラウザで、画面を確かめるための開発用サーバ)の最小の確認。

サーバそのもの(Firestore エミュレータ・金庫・web を 1 プロセスで動かし、uvicorn で出す)は、ここでは起動しない。実ブラウザでの確かめ方は
tests/manual/ui_checklist.md。確かめるのは、次のこと。
- import できる・`--help` が出る(ホストは 127.0.0.1 に固定。ポートの既定は 8080)。
- `--live` の環境変数(scripts/run_demo.py と同じ)が足りなければ、何も起動せずに(エミュレータも起動せずに)止まる。説明に値は書かない。
- 面談のスタブの LLM が、面談エージェントが検証できる形(SalaryBasis・ConstraintList)を返す。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))
import run_demo  # noqa: E402  (scripts/ を import できるようにしてから読む)
import serve_local  # noqa: E402

from web.interview.salary import SalaryBasis  # noqa: E402
from web.interview.statements import ConstraintList  # noqa: E402

LIVE_VARIABLES = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


def test_the_help_is_shown_and_the_server_stays_on_the_loopback_address(capsys):
    with pytest.raises(SystemExit) as stopped:
        serve_local.main(["--help"])

    assert stopped.value.code == 0
    shown = capsys.readouterr().out
    for expected in ("--live", "--port", "127.0.0.1", "8080"):
        assert expected in shown
    assert serve_local.HOST == "127.0.0.1" and serve_local.DEFAULT_PORT == 8080  # 外には出さない


def test_a_live_start_without_the_environment_stops_before_anything_is_started(monkeypatch, capsys):
    for name in LIVE_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    def must_not_start(*args, **kwargs):
        raise AssertionError("the emulator must not be started")

    monkeypatch.setattr(run_demo, "firestore_emulator", must_not_start)

    assert serve_local.main(["--live"]) == 1

    shown = capsys.readouterr().err
    for name in LIVE_VARIABLES:
        assert name in shown  # 足りない環境変数の名前は書く
    assert "scripts/serve_local.py --live" in shown and "何も呼ばずに止めました" in shown


def test_the_live_environment_check_names_only_what_is_missing_and_never_a_value():
    environment = {"GOOGLE_GENAI_USE_VERTEXAI": "TRUE", "GOOGLE_CLOUD_PROJECT": "canary-project-1234", "GOOGLE_CLOUD_LOCATION": ""}

    problem = serve_local.check_live_environment(environment)

    assert problem is not None and "GOOGLE_CLOUD_LOCATION" in problem
    assert "GOOGLE_GENAI_USE_VERTEXAI、" not in problem and "canary-project-1234" not in problem
    assert serve_local.check_live_environment({**environment, "GOOGLE_CLOUD_LOCATION": "global"}) is None


def request_for(task: str) -> SimpleNamespace:
    """面談エージェントが LLM に渡す入力の形(最後の content の最初の part の文字が、task を持つ JSON)の、最小の偽物。"""
    part = SimpleNamespace(text=json.dumps({"task": task}))
    return SimpleNamespace(contents=[SimpleNamespace(parts=[part])])


def test_the_interview_stub_returns_what_the_interview_agent_validates():
    salary = serve_local.interview_stub_behavior(request_for("salary_basis"))
    comment = serve_local.interview_stub_behavior(request_for("free_comment"))
    reason = serve_local.interview_stub_behavior(request_for("reason_for_leaving"))

    assert SalaryBasis.model_validate_json(salary).amount_man_yen == 620  # 確認の手順の例(額面 620 万円)
    for statements in (comment, reason):
        assert ConstraintList.model_validate_json(statements).statements == []  # 何も読み取らない(条件にならない)
