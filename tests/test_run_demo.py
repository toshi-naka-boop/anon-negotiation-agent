"""scripts/run_demo.py(design.md §12.2 の DV-15 のスクリプト。実装計画 ②-b)。

--live は本物の Gemini(費用がかかる)を呼ぶので、テストでは動かさない。ここで確かめるのは、本物の LLM・GCP に
接続しない部分。
- --scripted の流れ(金庫のテンプレート → 交渉の作成 → レフェリー → A2A → agents(台本の LLM)→ 金庫)が、ケース 1 で、
  側ごとの上限の中で、合意(agreed)まで進む。agents は、台本の LLM を差し込んだ本物の ADK(Gemini には接続しない)。
- 結果の表示と実行の記録(JSONL)が、必要な項目を持ち、ID(交渉 ID・プロジェクト ID)を含まない。
- --live の環境変数がそろっていなければ、何も呼ばずに(エミュレータも起動せずに)止まる。モデル名と場所は、最初に表示する。
- main は、--scripted のとき終了コード 0 を返し、実行の記録を書く。
- 終了コードは、合意なら 0、それ以外は 1。
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import socket
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from google import genai
from google.adk.models.llm_response import LlmResponse
from google.cloud import firestore
from google.genai import errors as genai_errors
from google.genai import types

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))
import run_demo as demo_script  # noqa: E402  (scripts/ を import できるようにしてから読む)

from vault.config import DEFAULT_VAULT_CONFIG  # noqa: E402
from vault.fixtures import load_case_fixture  # noqa: E402

LIVE_ENVIRONMENT = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")
ID_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")  # 16 桁の 16 進数(交渉 ID・依頼者 ID の形。§2.7)
PROJECT_ID_CANARY = "canary-project-1234"


def forbidden(*args, **kwargs):
    raise AssertionError("this must not be called")


REAL_CONNECT, REAL_GETADDRINFO = socket.socket.connect, socket.getaddrinfo
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def connect_to_loopback_only(sock, address):
    if isinstance(address, tuple) and address[0] not in LOOPBACK:
        raise AssertionError(f"the scripted run must not leave this machine (connect to {address[0]})")
    return REAL_CONNECT(sock, address)


def resolve_loopback_only(host, *args, **kwargs):
    if host not in (None, *LOOPBACK):
        raise AssertionError(f"the scripted run must not leave this machine (resolve {host})")
    return REAL_GETADDRINFO(host, *args, **kwargs)


def run_scripted_demo(firestore_emulator_host, **options):
    """--scripted のデモを、run_demo で 1 回動かす(オプションで、時間の上限などを替えられる)。

    本物の Gemini・GCP につながらないことの確認つき: google-genai のクライアントを作る・Python の socket が自分のマシン
    (ループバック)の外へ接続・名前解決しようとすると、失敗する(Firestore のエミュレータは、127.0.0.1)。
    """
    client = firestore.Client(project=f"demo-test-{uuid.uuid4().hex}", database="vault-db")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(genai, "Client", forbidden)
            patch.setattr(socket.socket, "connect", connect_to_loopback_only)
            patch.setattr(socket, "getaddrinfo", resolve_loopback_only)
            return asyncio.run(
                demo_script.run_demo(fixture=load_case_fixture(1), mode="scripted", vault_db=client, **options)
            )
    finally:
        client.close()


@pytest.fixture(scope="module")
def scripted_run(firestore_emulator_host):
    """--scripted の交渉を、ケース 1 で 1 回通した結果(このファイルの全テストで共有する。1 回に数秒かかる)。"""
    return run_scripted_demo(firestore_emulator_host)


def test_the_network_guard_of_the_scripted_run_blocks_everything_outside_this_machine():
    with pytest.raises(AssertionError, match="must not leave this machine"):
        connect_to_loopback_only(None, ("203.0.113.1", 443))
    with pytest.raises(AssertionError, match="must not leave this machine"):
        resolve_loopback_only("aiplatform.googleapis.com", 443)
    assert resolve_loopback_only("localhost", 80)  # 自分のマシンは通す


# ----------------------------------------------------------------------
# --scripted の流れ
# ----------------------------------------------------------------------


def test_scripted_run_reaches_an_agreement_on_case1_within_the_limits(scripted_run):
    # DV-15・AC-09 のスクリプトの流れ: 台本の LLM で、ケース 1 が、側ごとの上限の中で、合意(agreed)まで進む(中以上の見込み)。
    run, fixture, limits = scripted_run, load_case_fixture(1), DEFAULT_VAULT_CONFIG.limits
    assert not run.timed_out
    assert (run.status, run.end_reason) == ("judged", "agreed")
    assert run.result.likelihood in ("high", "medium")
    # 合意した組み合わせは、フィクスチャの両者(生の条件)が受けられるもの。
    assert fixture.candidate.raw.accepts(run.result.package)
    assert fixture.employer.rules[0].raw.accepts(run.result.package)
    for counters in run.counters.values():
        assert counters.moves_used <= limits.moves_budget_per_side
        assert counters.evaluations_used <= limits.evaluation_budget_per_side
        assert counters.principal_checks_used <= limits.principal_checks_per_side


def test_scripted_run_goes_through_a2a_and_adk_and_every_llm_output_is_a_valid_move(scripted_run):
    # 経路は agents の A2A の受信口と ADK を通る(LLM の呼び出しが、候補者側・求人側の両方に、記録される)。台本の LLM の出力は、
    # すべて Move の検証を通り(schema_invalid・agent_timeout がない)、LLM の呼び出しは、1 つも失敗していない。
    run = scripted_run
    assert {call.role for call in run.llm_calls} == {"candidate", "employer"}
    assert [call.n for call in run.llm_calls] == list(range(1, len(run.llm_calls) + 1))
    assert all(call.error is None and call.seconds >= 0 and call.output_text for call in run.llm_calls)
    invalid = run.invalid_moves_by_reason()
    assert invalid["schema_invalid"] == 0 and invalid["agent_timeout"] == 0


def test_the_agents_app_uses_the_scripted_llm_for_scripted_and_the_configured_model_for_live():
    # --scripted は台本の LLM(Gemini にはつながない)、--live は設定ファイルのモデル名のまま(最初の呼び出しまで接続しない)。
    scripted = demo_script.build_agents_app("scripted", 1, demo_script.LlmCallRecorder())
    assert all(isinstance(runner.agent.model, demo_script.ScriptedLlm) for runner in scripted.state.runners.values())
    live = demo_script.build_agents_app("live", 1, demo_script.LlmCallRecorder())
    assert {runner.agent.model for runner in live.state.runners.values()} == {"gemini-3.5-flash"}


def test_a_run_past_the_time_limit_stops_before_the_first_llm_call_and_still_reports(firestore_emulator_host):
    # 時間の上限を過ぎていたら、LLM を 1 回も呼ばずに止まる。その時点の状態(判定に届いていない)を、診断として返す。
    run = run_scripted_demo(firestore_emulator_host, timeout_seconds=0.0)

    assert run.timed_out and run.status == "active" and run.end_reason is None and run.result is None
    assert run.llm_calls == [] and all(items == [] for items in run.events.values())
    report = demo_script.format_report(run)
    assert "時間切れ" in report and "見込み・組み合わせ: なし(判定に届いていない)" in report
    assert "金庫の中の終了理由(診断用): (終了していない)" in report


def test_a_run_that_goes_over_the_time_limit_stops_at_the_next_llm_call_with_what_it_has(
    firestore_emulator_host, monkeypatch
):
    # 1 回の LLM の呼び出しに 0.4 秒かかるようにして、上限 1 秒で止める(合意までは 15 回ほど要るので、必ず途中で止まる)。
    # 進行中の呼び出しは終わるまで待ち、次の呼び出しは始めない。それまでの手の並びと LLM の呼び出しは、残る。
    original = demo_script.ScriptedLlm.generate_content_async

    async def slow(self, llm_request, stream=False):
        await asyncio.sleep(0.4)
        async for response in original(self, llm_request, stream):
            yield response

    monkeypatch.setattr(demo_script.ScriptedLlm, "generate_content_async", slow)
    run = run_scripted_demo(firestore_emulator_host, timeout_seconds=1.0)

    assert run.timed_out and run.status == "active" and run.end_reason is None
    assert 1 <= len(run.llm_calls) < 15 and all(call.error is None for call in run.llm_calls)
    assert run.events["candidate"]  # 止まるまでの手は、金庫の記録に残っている
    assert "時間切れ" in demo_script.format_report(run)


# ----------------------------------------------------------------------
# LLM の呼び出しの記録(--live の本物の応答・失敗の形でも、記録が取れる)
# ----------------------------------------------------------------------


def callback_context(invocation_id: str = "e-1", agent_name: str = "candidate_agent") -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id, agent_name=agent_name)


@pytest.mark.anyio
async def test_the_recorder_reads_time_tokens_and_finish_reason_from_a_gemini_shaped_response():
    # 本物の Gemini の応答(トークン数・終了理由・思考の部分つき)を、ADK の LlmResponse の形で渡す。
    seen = []
    recorder = demo_script.LlmCallRecorder(on_call=seen.append)
    context = callback_context()
    await recorder.before_model_callback(callback_context=context, llm_request=None)
    response = LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text="考え中", thought=True), types.Part(text='{"move": "end"}')]
        ),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1200, candidates_token_count=40, thoughts_token_count=300, total_token_count=1540
        ),
        finish_reason=types.FinishReason.STOP,
    )
    assert await recorder.after_model_callback(callback_context=context, llm_response=response) is None

    [call] = recorder.calls
    assert seen == [call]
    assert (call.n, call.role, call.error, call.seconds >= 0) == (1, "candidate", None, True)
    assert (call.input_tokens, call.output_tokens, call.thought_tokens) == (1200, 40, 300)
    assert (call.finish_reason, call.output_text) == ("STOP", '{"move": "end"}')  # 思考の部分は、出力に入れない


@pytest.mark.anyio
async def test_the_recorder_records_a_failed_call_by_type_and_status_and_never_the_message():
    recorder = demo_script.LlmCallRecorder()
    context = callback_context(agent_name="employer_agent")
    secret = {"error": {"code": 429, "message": "secret 623 in the message", "status": "RESOURCE_EXHAUSTED"}}
    await recorder.before_model_callback(callback_context=context, llm_request=None)
    await recorder.on_model_error_callback(
        callback_context=context, llm_request=None, error=genai_errors.ClientError(429, secret)
    )
    other = callback_context("e-2")
    await recorder.before_model_callback(callback_context=other, llm_request=None)
    await recorder.on_model_error_callback(callback_context=other, llm_request=None, error=ZeroDivisionError("secret 623"))
    blocked = callback_context("e-3")
    await recorder.before_model_callback(callback_context=blocked, llm_request=None)
    await recorder.after_model_callback(
        callback_context=blocked, llm_response=LlmResponse(error_code="MAX_TOKENS", error_message="secret 623")
    )

    assert [(call.n, call.role, call.error) for call in recorder.calls] == [
        (1, "employer", "ClientError 429 RESOURCE_EXHAUSTED"),
        (2, "candidate", "ZeroDivisionError"),
        (3, "candidate", "error_code MAX_TOKENS"),
    ]
    assert "623" not in repr(recorder.calls)


@pytest.mark.anyio
async def test_a_failure_of_the_recorder_does_not_break_the_llm_call(caplog):
    # ADK は、プラグインの例外を、モデルの呼び出しの失敗にする。記録の失敗は、警告だけにして、呼び出しを通す。
    recorder = demo_script.LlmCallRecorder()
    context = callback_context()
    assert await recorder.before_model_callback(callback_context=context, llm_request=None) is None
    assert await recorder.after_model_callback(callback_context=context, llm_response=object()) is None
    assert await recorder.before_model_callback(callback_context=None, llm_request=None) is None
    assert recorder.calls == []
    assert "LLM call recorder failed: AttributeError" in caplog.text


# ----------------------------------------------------------------------
# 表示と記録
# ----------------------------------------------------------------------


def test_the_report_shows_everything_the_caller_needs_and_no_id(scripted_run):
    report = demo_script.format_report(scripted_run)
    for expected in (
        "見込み:",  # 結果(見込みと組み合わせ)
        "組み合わせ:",
        "金庫の中の終了理由(診断用): agreed",
        "[候補者側]",  # 双方の手の並び(側ごとの見え方)
        "[求人側]",
        "final_result",
        f"計 {len(scripted_run.llm_calls)} 回",  # LLM の呼び出しの回数と、1 回ごとの所要時間
        "#1 ",
        " s  ok",
        "手数",  # 側ごとの手数と評価の使用量
        "評価",
        "途中確認",
        "schema_invalid: 0",  # 無効手の数(理由ごと)
        "agent_timeout: 0",
    ):
        assert expected in report
    assert scripted_run.nid not in report
    assert not ID_IN_TEXT.search(report)


def test_the_record_is_jsonl_with_the_move_sequence_and_the_llm_calls_and_no_id(scripted_run, tmp_path):
    path = demo_script.write_record(scripted_run, tmp_path)

    assert path.parent == tmp_path and re.fullmatch(r"case1_scripted_\d{8}T\d{6}_\d{6}\.jsonl", path.name)
    text = path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert [record["type"] for record in records][0] == "run" and records[-1]["type"] == "summary"
    assert {record["type"] for record in records} == {"run", "event", "llm_call", "summary"}
    assert {record["side"] for record in records if record["type"] == "event"} == {"candidate", "employer"}
    assert sum(record["type"] == "llm_call" for record in records) == len(scripted_run.llm_calls)
    header, summary = records[0], records[-1]
    assert (header["case"], header["mode"]) == (1, "scripted")
    assert (summary["end_reason"], summary["result"]["likelihood"]) == ("agreed", scripted_run.result.likelihood)
    assert scripted_run.nid not in text
    assert not ID_IN_TEXT.search(text)


# ----------------------------------------------------------------------
# --live の環境変数
# ----------------------------------------------------------------------


def live_environment(**changes) -> dict[str, str]:
    environment = {
        "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
        "GOOGLE_CLOUD_PROJECT": PROJECT_ID_CANARY,
        "GOOGLE_CLOUD_LOCATION": "global",
    }
    environment.update(changes)
    return {name: value for name, value in environment.items() if value is not None}


def test_live_environment_check_passes_when_all_three_variables_are_set():
    assert demo_script.check_live_environment(live_environment(), 1) is None
    assert demo_script.check_live_environment(live_environment(GOOGLE_GENAI_USE_VERTEXAI="1"), 1) is None


@pytest.mark.parametrize("missing", LIVE_ENVIRONMENT)
def test_live_environment_check_names_the_missing_variable_and_never_shows_a_value(missing):
    message = demo_script.check_live_environment(live_environment(**{missing: None}), 1)
    assert message is not None and missing in message
    assert "uv run python scripts/run_demo.py --case 1 --live" in message
    assert "何も呼ばずに止めました" in message
    assert PROJECT_ID_CANARY not in message


def test_live_environment_check_rejects_vertex_ai_that_is_not_enabled():
    message = demo_script.check_live_environment(live_environment(GOOGLE_GENAI_USE_VERTEXAI="false"), 1)
    assert message is not None and "GOOGLE_GENAI_USE_VERTEXAI" in message


def test_the_model_and_the_location_are_shown_and_the_project_is_not():
    assert demo_script.describe_backend("live", live_environment()) == ("gemini-3.5-flash", "global")
    scripted_model, scripted_location = demo_script.describe_backend("scripted", live_environment())
    assert "Gemini は呼ばない" in scripted_model and "ネットワークに出ない" in scripted_location


def test_live_run_without_the_environment_stops_before_calling_anything(monkeypatch, capsys):
    # 環境変数が足りなければ、モデル名と場所を表示したあと、エミュレータも起動せず、何も呼ばずに止まる(終了コード 1)。
    for name in LIVE_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(demo_script, "firestore_emulator", forbidden)
    monkeypatch.setattr(demo_script, "run_demo", forbidden)

    assert demo_script.main(["--case", "1", "--live"]) == 1

    captured = capsys.readouterr()
    assert captured.out.splitlines()[:3] == [
        "run_demo: ケース 1 / live",
        "使うモデル: gemini-3.5-flash",
        "場所: (環境変数 GOOGLE_CLOUD_LOCATION が未設定)",
    ]
    assert all(name in captured.err for name in LIVE_ENVIRONMENT)
    assert "何も呼ばずに止めました" in captured.err


# ----------------------------------------------------------------------
# コマンド(main)
# ----------------------------------------------------------------------


@pytest.fixture
def records_directory(monkeypatch, tmp_path, firestore_emulator_host):
    """main を、テストのセッションのエミュレータと、tmp_path の記録の置き場で動かす(本物の起動は、scripts の実行で確かめる)。"""

    @contextlib.contextmanager
    def session_emulator():
        yield firestore_emulator_host

    monkeypatch.setattr(demo_script, "firestore_emulator", session_emulator)
    monkeypatch.setattr(demo_script, "RUN_RECORD_DIRECTORY", tmp_path)
    return tmp_path


def test_main_runs_the_scripted_demo_and_returns_zero_and_writes_a_record(records_directory, capsys):
    # DV-15 のコマンド(--scripted 版): 終了コード 0(合意)・モデルと場所を最初に表示・実行の記録(JSONL)
    metrics_logger = logging.getLogger("google_adk.google.adk.telemetry._metrics")
    level = metrics_logger.level
    try:
        code = demo_script.main(["--case", "1", "--scripted"])
    finally:
        metrics_logger.setLevel(level)  # main が、このロガーの水準を変えるので、元に戻す

    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[:3] == [
        "run_demo: ケース 1 / scripted",
        "使うモデル: なし(台本のエージェント。Gemini は呼ばない)",
        "場所: なし(ネットワークに出ない)",
    ]
    assert "判定: 合意(agreed) → 終了コード 0" in out
    assert not ID_IN_TEXT.search(out)
    assert len(list(records_directory.glob("case1_scripted_*.jsonl"))) == 1


def test_main_returns_one_unless_the_negotiation_ends_in_an_agreement(monkeypatch, records_directory, capsys, scripted_run):
    # 合意以外(ここでは、時間切れで判定に届かなかった場合)は、終了コード 1。
    async def not_agreed(**kwargs):
        return dataclasses.replace(scripted_run, status="active", end_reason=None, result=None, timed_out=True)

    monkeypatch.setattr(demo_script, "run_demo", not_agreed)

    assert demo_script.main(["--case", "1", "--scripted"]) == 1
    out = capsys.readouterr().out
    assert "時間切れ" in out and "判定: 合意に届かなかった → 終了コード 1" in out


def test_main_returns_one_when_the_case_has_no_fixture(capsys):
    assert demo_script.main(["--case", "99", "--scripted"]) == 1
    assert "fixtures/case99.toml がありません" in capsys.readouterr().err
