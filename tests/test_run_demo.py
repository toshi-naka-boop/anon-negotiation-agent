"""scripts/run_demo.py(design.md §12.2 の DV-15 のスクリプト。実装計画 ②-b・③-0)。

--live は本物の Gemini(費用がかかる)を呼ぶので、テストでは動かさない。ここで確かめるのは、本物の LLM・GCP に接続しない部分。
- --scripted の流れ(金庫のテンプレート → 交渉の作成 → レフェリー(計画・確かめ・決定)→ 台本のエージェント → 金庫)が、ケース 1 で、
  側ごとの上限の中で、合意(agreed)まで進む。台本のエージェントは、agents を通さず、レフェリーの send_turn の差し込み口に直接入る。
- エージェントへの物理の送信が、1 回ごとに、役割・呼び出しの種類・結果・`usage` つきで記録され、レフェリーが数えた物理の数と一致する。
- DV-15 の判定(`--judge`)をスクリプトが行う: 合意・200 応答の数・費用(設定ファイルの単価)・思考トークンの平均・`usage` の欠落・出力の切れ。
  基準を 1 つでも外した実行は、その項目だけが不合格になる(判定が、必ず通るものになっていない)。
- 結果の表示と実行の記録(JSONL)が、必要な項目(モデル ID・設定のハッシュ・単価の版・送信ごとのトークン数・物理の数・判定)を持ち、
  ID(交渉 ID・プロジェクト ID)を含まない。
- --live の環境変数がそろっていなければ、何も呼ばずに(エミュレータも起動せずに)止まる。モデル名と場所は、最初に表示する。
- main は、--scripted のとき終了コード 0 を返し、実行の記録を書く。--runs N --judge は、N 回とも合格なら 0、1 回でも不合格なら 1。
"""

import asyncio
import contextlib
import dataclasses
import json
import re
import socket
import sys
import uuid
from pathlib import Path

import pytest
from google import genai
from google.cloud import firestore
from negotiation_core import Usage

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))
import run_demo as demo_script  # noqa: E402  (scripts/ を import できるようにしてから読む)

from vault.config import DEFAULT_VAULT_CONFIG  # noqa: E402
from vault.fixtures import load_case_fixture  # noqa: E402

LIVE_ENVIRONMENT = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")
ID_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")  # 16 桁の 16 進数(交渉 ID・依頼者 ID の形。§2.7)
PROJECT_ID_CANARY = "canary-project-1234"
TARGETS = demo_script.load_cost_targets()


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
    return run_scripted_demo_as(firestore_emulator_host, "scripted", **options)


def run_scripted_demo_as(firestore_emulator_host, mode, **options):
    """run_scripted_demo の本体(mode は、配線の確かめのときだけ live にする。同じネットワークの確認つき)。"""
    project = f"demo-test-{uuid.uuid4().hex}"
    vault_db = firestore.Client(project=project, database="vault-db")
    default_db = firestore.Client(project=project, database="(default)")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(genai, "Client", forbidden)
            patch.setattr(socket.socket, "connect", connect_to_loopback_only)
            patch.setattr(socket, "getaddrinfo", resolve_loopback_only)
            return asyncio.run(
                demo_script.run_demo(
                    fixture=load_case_fixture(1),
                    mode=mode,
                    vault_db=vault_db,
                    default_db=default_db,
                    **options,
                )
            )
    finally:
        vault_db.close()
        default_db.close()


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
    # DV-15・AC-09 のスクリプトの流れ: 台本のエージェントで、ケース 1 が、側ごとの上限の中で、合意(agreed)まで進む(中以上の見込み)。
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


def test_every_send_of_the_scripted_run_is_recorded_with_its_role_its_phase_and_its_usage(scripted_run):
    # エージェントへの物理の送信が、1 回ごとに、記録される(両側の、計画と決定)。台本のエージェントの出力は、すべて Plan・Move の
    # 検証を通り(schema_invalid・agent_timeout・output_truncated がない)、送信は 1 つも失敗していない。
    run = scripted_run
    assert [call.n for call in run.calls] == list(range(1, len(run.calls) + 1))
    assert {call.role for call in run.calls} == {"candidate", "employer"}
    assert {call.phase for call in run.calls} == {"plan", "decide"}  # 確かめを挟んだ手番は、決定も呼ぶ
    assert all(call.kind == "ok" and call.usage == demo_script.SCRIPTED_USAGE and call.output for call in run.calls)
    assert run.successful_calls == len(run.calls)
    invalid = run.invalid_moves_by_reason()
    assert invalid["schema_invalid"] == 0 and invalid["agent_timeout"] == 0 and invalid["output_truncated"] == 0


def test_the_referees_physical_count_equals_the_sends_the_recorder_saw(scripted_run):
    # DV-17: レフェリーが送る前に数えた物理の数(交渉ごとのカウンタ)が、実際の送信の数と一致する。
    assert scripted_run.referee_counted_calls == len(scripted_run.calls) > 0


def test_the_live_mode_wires_the_agents_client_and_the_agents_app_through_the_same_recorder(
    firestore_emulator_host, monkeypatch
):
    # --live の配線(本物の Gemini は呼ばない): web.app.bind_agents_client が返す関数が、そのまま、記録の包みを通って、レフェリーの send_turn
    # になる。agents の app は、build_agents_app が作り、A2A の HTTP の口を、その app につなぐ間だけ差し替える(終わったら元に戻す)。
    # ここでは、agents を台本のエージェントと、空の app に差し替えて、配線だけを確かめる。
    import agents.client as agents_client
    import web.app as web_app_module
    from starlette.applications import Starlette

    original_http_client = agents_client._open_http_client
    seen = {}
    monkeypatch.setattr(web_app_module, "bind_agents_client", lambda base_url: demo_script.scripted_sender(1))
    monkeypatch.setattr(demo_script, "build_agents_app", lambda: seen.setdefault("app", Starlette()))

    run = run_scripted_demo_as(firestore_emulator_host, "live")

    assert run.mode == "live" and run.model_id == "gemini-3.5-flash"
    assert (run.status, run.end_reason) == ("judged", "agreed")
    assert len(run.calls) > 0 and all(call.kind == "ok" for call in run.calls)  # 台本の送信が、記録された
    assert run.referee_counted_calls == len(run.calls)
    assert "app" in seen  # agents の app を作った(--live だけ)
    assert agents_client._open_http_client is original_http_client  # A2A の HTTP の口は、元に戻っている


def test_a_run_past_the_time_limit_stops_before_the_first_send_and_still_reports(firestore_emulator_host):
    # 時間の上限を過ぎていたら、エージェントに 1 回も送らずに止まる。その時点の状態(判定に届いていない)を、診断として返す。
    run = run_scripted_demo(firestore_emulator_host, timeout_seconds=0.0)

    assert run.timed_out and run.status == "active" and run.end_reason is None and run.result is None
    assert run.calls == [] and all(items == [] for items in run.events.values())
    report = demo_script.format_report(run)
    assert "時間切れ" in report and "見込み・組み合わせ: なし(判定に届いていない)" in report
    assert "金庫の中の終了理由(診断用): (終了していない)" in report


def test_a_run_that_goes_over_the_time_limit_stops_at_the_next_send_with_what_it_has(
    firestore_emulator_host, monkeypatch
):
    # 1 回の送信に 0.4 秒かかるようにして、上限 1 秒で止める(合意までは 20 回ほど要るので、必ず途中で止まる)。
    # 進行中の送信は終わるまで待ち、次の送信は始めない。それまでの手の並びと送信の記録は、残る。
    original = demo_script.scripted_sender

    def slow_sender(case):
        inner = original(case)

        async def send(role, turn_input, *, nid, timeout_s):
            await asyncio.sleep(0.4)
            return await inner(role, turn_input, nid=nid, timeout_s=timeout_s)

        return send

    monkeypatch.setattr(demo_script, "scripted_sender", slow_sender)
    run = run_scripted_demo(firestore_emulator_host, timeout_seconds=1.0)

    assert run.timed_out and run.status == "active" and run.end_reason is None
    assert 1 <= len(run.calls) < 15 and all(call.kind == "ok" for call in run.calls)
    assert run.events["candidate"]  # 止まるまでの手は、金庫の記録に残っている
    assert "時間切れ" in demo_script.format_report(run)
    assert not demo_script.judge(run, TARGETS).passed  # 時間切れは、不合格


# ----------------------------------------------------------------------
# エージェントへの送信の記録(SendRecorder)
# ----------------------------------------------------------------------


class TruncatedOutputError(ValueError):
    """出力が max_output_tokens で切れた(agents.client.TruncatedOutputError の代わり。属性 usage を持つ ValueError)。"""

    def __init__(self, message: str, usage=None) -> None:
        super().__init__(message)
        self.usage = usage


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "kind", "error", "has_usage"),
    [
        (None, "ok", None, True),
        (TruncatedOutputError("cut", usage=demo_script.SCRIPTED_USAGE), "truncated", "TruncatedOutputError", True),
        (TruncatedOutputError("cut"), "truncated", "TruncatedOutputError", False),
        (ValueError("envelope"), "unusable", "ValueError", False),
        (ConnectionError("429"), "transient", "ConnectionError", False),
        (TimeoutError("503"), "transient", "TimeoutError", False),
        (RuntimeError("bug"), "error", "RuntimeError", False),
    ],
    ids=["ok", "truncated_with_usage", "truncated_without_usage", "unusable", "connection_error", "timeout", "error"],
)
async def test_the_recorder_classifies_each_send_and_never_changes_its_outcome(outcome, kind, error, has_usage):
    # 物理の送信 1 回ごとに 1 件の記録(再試行も 1 回ずつ)。結果は、そのまま返す・そのまま投げる。記録は、型名だけ(メッセージは持たない)。
    from web_helpers import move_dict

    async def inner(role, turn_input, *, nid, timeout_s):
        if outcome is not None:
            raise outcome
        return move_dict("end"), demo_script.SCRIPTED_USAGE

    class Input:
        phase = "decide"

    seen = []
    recorder = demo_script.SendRecorder(inner, seen.append)

    if outcome is None:
        assert await recorder("candidate", Input(), nid="0123456789abcdef", timeout_s=5) == (
            move_dict("end"),
            demo_script.SCRIPTED_USAGE,
        )
    else:
        with pytest.raises(type(outcome)):
            await recorder("candidate", Input(), nid="0123456789abcdef", timeout_s=5)

    [record] = recorder.records
    assert seen == [record]
    assert (record.n, record.role, record.phase, record.kind, record.error) == (1, "candidate", "decide", kind, error)
    assert record.has_usage is has_usage
    assert record.usage_missing is (kind == "unusable" or (kind == "truncated" and not has_usage))
    assert "cut" not in repr(record) and "envelope" not in repr(record)  # 例外のメッセージは持たない


# ----------------------------------------------------------------------
# DV-15 の判定
# ----------------------------------------------------------------------


def test_the_cost_targets_are_read_from_the_config_file():
    # 基準の値の正本は設定ファイル([agents.cost_targets])。スクリプトに値を持たない。
    assert dataclasses.astuple(TARGETS) == (0.25, 2, 28, 820, 1.0, "2026-10-02", 1.5, 0.15, 9.0)


def test_the_cost_of_a_call_is_computed_from_the_versioned_unit_prices():
    # 入力は、キャッシュ済みを含む prompt_tokens から、キャッシュ済みを引いた分に入力単価、キャッシュ済みにキャッシュ単価。
    # 出力と思考に出力単価(100 万トークンあたり)。
    usage = Usage(
        model="gemini-3.5-flash",
        prompt_tokens=1_000_000,
        cached_tokens=200_000,
        thoughts_tokens=100_000,
        output_tokens=50_000,
        requests=1,
    )
    assert demo_script.usage_cost_usd(usage, TARGETS) == pytest.approx(800_000 * 1.5 / 1e6 + 200_000 * 0.15 / 1e6 + 150_000 * 9.0 / 1e6)


def test_the_settings_hash_changes_with_the_model_or_any_setting():
    settings = demo_script.load_agents_settings()
    base = demo_script.settings_hash("gemini-3.5-flash", settings)
    assert re.fullmatch(r"[0-9a-f]{12}", base)
    assert demo_script.settings_hash("gemini-3.5-flash", settings) == base  # 同じなら同じ
    assert demo_script.settings_hash("gemini-3.6-flash", settings) != base
    for key in ("plan_thinking_level", "decide_thinking_level", "temperature", "max_output_tokens"):
        assert key in settings
        assert demo_script.settings_hash("gemini-3.5-flash", {**settings, key: "changed"}) != base


def _passed(judgement) -> dict[str, bool]:
    return {check.name: check.passed for check in judgement.checks}


def test_the_scripted_run_passes_every_item_of_the_judgement(scripted_run):
    judgement = demo_script.judge(scripted_run, TARGETS)

    assert judgement.passed
    assert _passed(judgement) == {
        "agreed": True,
        "successful_calls": True,
        "cost": True,
        "thinking": True,
        "usage": True,
        "no_truncation": True,
    }
    assert "上限" in next(c for c in judgement.checks if c.name == "successful_calls").detail


def _with_usage(run, **changes):
    """run の、すべての送信の usage の項目を替えたもの。"""
    return dataclasses.replace(
        run, calls=[dataclasses.replace(c, usage=c.usage.model_copy(update=changes)) for c in run.calls]
    )


def _extra_call(run, **changes):
    """run に、送信の記録を 1 件足したもの。"""
    template = run.calls[0]
    return dataclasses.replace(
        run, calls=[*run.calls, dataclasses.replace(template, n=len(run.calls) + 1, **changes)]
    )


@pytest.mark.parametrize(
    ("name", "broken"),
    [
        pytest.param("agreed", lambda run: dataclasses.replace(run, end_reason="stopped_budget"), id="not_agreed"),
        pytest.param(
            "successful_calls",
            lambda run: dataclasses.replace(run, calls=[*run.calls, *run.calls, *run.calls]),  # 200 応答が、上限(28 回)を超える
            id="too_many_successful_calls",
        ),
        pytest.param("cost", lambda run: _with_usage(run, output_tokens=200_000), id="over_the_cost_limit"),
        pytest.param("thinking", lambda run: _with_usage(run, thoughts_tokens=1_000), id="over_the_thinking_average"),
        pytest.param(
            "usage",
            lambda run: _extra_call(run, kind="unusable", error="ValueError", usage=None, output=None),
            id="usage_is_missing",
        ),
        pytest.param(
            "no_truncation",
            lambda run: _extra_call(run, kind="truncated", error="TruncatedOutputError", output=None),
            id="the_output_was_cut",
        ),
    ],
)
def test_the_judgement_fails_only_the_item_whose_criterion_is_not_met(scripted_run, name, broken):
    judgement = demo_script.judge(broken(scripted_run), TARGETS)

    results = _passed(judgement)
    assert results[name] is False
    assert not judgement.passed
    if name not in ("successful_calls", "usage", "no_truncation"):  # 送信を足す・増やす例は、費用なども動くので、項目だけ確かめる
        assert [n for n, ok in results.items() if not ok] == [name]


def test_physical_sends_that_got_no_response_are_not_part_of_the_pass_or_fail(scripted_run):
    # 物理の送信の数(429・5xx の分を含む)は、別に記録するだけで、合否に入れない(台帳 C-50)。応答のなかった送信が増えても、
    # 200 応答の数・使用量の欠落には、数えない。
    noisy = dataclasses.replace(
        scripted_run,
        calls=[
            *scripted_run.calls,
            *[
                dataclasses.replace(scripted_run.calls[0], n=100 + i, kind="transient", error="ConnectionError", usage=None, output=None)
                for i in range(10)
            ],
        ],
    )

    assert noisy.successful_calls == scripted_run.successful_calls
    assert len(noisy.calls) == len(scripted_run.calls) + 10
    assert demo_script.judge(noisy, TARGETS).passed


# ----------------------------------------------------------------------
# 表示と記録
# ----------------------------------------------------------------------


def test_the_report_shows_everything_the_caller_needs_and_no_id(scripted_run):
    report = demo_script.format_report(scripted_run, TARGETS)
    for expected in (
        "見込み:",  # 結果(見込みと組み合わせ)
        "組み合わせ:",
        "金庫の中の終了理由(診断用): agreed",
        "[候補者側]",  # 双方の手の並び(側ごとの見え方)
        "[求人側]",
        "final_result",
        f"物理の送信 計 {len(scripted_run.calls)} 回",  # 送信の回数と、1 回ごとの所要時間・使用量
        f"レフェリーが数えた物理の数 {scripted_run.referee_counted_calls} 回",
        "#1 ",
        " s  ok",
        "トークン合計",
        "費用 $",
        "台本の合成値",  # 台本の費用は、実際の費用ではないことを、書く
        "手数",  # 側ごとの手数と評価の使用量
        "評価",
        "途中確認",
        "schema_invalid: 0",  # 無効手の数(理由ごと)
        "agent_timeout: 0",
        "output_truncated: 0",
    ):
        assert expected in report
    assert scripted_run.nid not in report
    assert not ID_IN_TEXT.search(report)


def test_the_judgement_is_shown_item_by_item(scripted_run):
    text = demo_script.format_judgement(demo_script.judge(_with_usage(scripted_run, thoughts_tokens=1_000), TARGETS))

    assert "[合格] agreed" in text and "[不合格] thinking" in text and "→ 不合格" in text


def test_the_record_is_jsonl_with_everything_the_judgement_needs_and_no_id(scripted_run, tmp_path):
    judgement = demo_script.judge(scripted_run, TARGETS)
    path = demo_script.write_record(scripted_run, tmp_path, targets=TARGETS, judgement=judgement)

    assert path.parent == tmp_path and re.fullmatch(r"case1_scripted_\d{8}T\d{6}_\d{6}\.jsonl", path.name)
    text = path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert [record["type"] for record in records][0] == "run"
    assert [record["type"] for record in records][-2:] == ["summary", "judgement"]
    assert {record["type"] for record in records} == {"run", "event", "call", "summary", "judgement"}
    assert {record["side"] for record in records if record["type"] == "event"} == {"candidate", "employer"}
    header, summary, judged = records[0], records[-2], records[-1]
    # モデル ID・設定(思考の量・temperature・max_output_tokens・キャッシュの有無)のハッシュ・単価の版
    assert (header["case"], header["mode"], header["model_id"]) == (1, "scripted", "scripted")
    assert header["settings_hash"] == demo_script.settings_hash("scripted", demo_script.load_agents_settings())
    assert {"plan_thinking_level", "decide_thinking_level", "temperature", "max_output_tokens", "explicit_cache"} <= set(
        header["settings"]
    )
    assert header["price_version"] == "2026-10-02"
    assert header["prices_usd_per_million"] == {"input": 1.5, "cached_input": 0.15, "output": 9.0}
    # 送信ごとのトークン数と、物理の数(レフェリーが数えた数を含む)
    calls = [record for record in records if record["type"] == "call"]
    assert len(calls) == len(scripted_run.calls)
    assert all(
        {"prompt_tokens", "cached_tokens", "thoughts_tokens", "output_tokens", "requests"} <= set(call["usage"])
        and call["phase"] in ("plan", "decide")
        for call in calls
    )
    assert summary["physical_sends"] == len(calls) == summary["referee_counted_calls"]
    assert summary["successful_calls"] == len(calls) and summary["cost_usd"] > 0
    assert (summary["end_reason"], summary["result"]["likelihood"]) == ("agreed", scripted_run.result.likelihood)
    # 判定の結果
    assert judged["passed"] is True
    assert [check["name"] for check in judged["checks"]] == [c.name for c in judgement.checks]
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
    code = demo_script.main(["--case", "1", "--scripted"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[:3] == [
        "run_demo: ケース 1 / scripted",
        "使うモデル: なし(台本のエージェント。Gemini は呼ばない)",
        "場所: なし(ネットワークに出ない)",
    ]
    assert "判定: 合意(agreed) → 終了コード 0" in out
    assert "DV-15 の判定" not in out  # --judge を付けなければ、判定の表示はない
    assert not ID_IN_TEXT.search(out)
    assert len(list(records_directory.glob("case1_scripted_*.jsonl"))) == 1


def test_main_with_runs_and_judge_judges_each_run_and_returns_zero_when_all_pass(records_directory, capsys):
    # DV-15 の 1 コマンド(--runs 2 --judge。--scripted 版): 2 回続けて判定し、2 回とも合格なら終了コード 0。記録は 2 件。
    code = demo_script.main(["--case", "1", "--scripted", "--runs", "2", "--judge"])

    out = capsys.readouterr().out
    assert code == 0
    assert "##### 実行 1/2 #####" in out and "##### 実行 2/2 #####" in out
    assert out.count("=== DV-15 の判定") == 2 and out.count("→ 合格") == 2
    assert "DV-15 の判定: 2 回中 2 回が合格 → 終了コード 0" in out
    assert not ID_IN_TEXT.search(out)
    records = sorted(records_directory.glob("case1_scripted_*.jsonl"))
    assert len(records) == 2
    for path in records:
        last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert (last["type"], last["passed"]) == ("judgement", True)


def test_main_returns_one_when_a_run_does_not_meet_the_criteria(monkeypatch, records_directory, capsys):
    # 基準を 1 つ外した(費用の上限を、実測より小さくした)実行がある → 終了コード 1。判定の結果は、記録にも残る。
    strict = dataclasses.replace(TARGETS, max_cost_usd_per_negotiation=0.0001)
    monkeypatch.setattr(demo_script, "load_cost_targets", lambda: strict)

    code = demo_script.main(["--case", "1", "--scripted", "--judge"])

    out = capsys.readouterr().out
    assert code == 1
    assert "[不合格] cost" in out and "DV-15 の判定: 1 回中 0 回が合格 → 終了コード 1" in out
    (path,) = records_directory.glob("case1_scripted_*.jsonl")
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["passed"] is False and [c["name"] for c in last["checks"] if not c["passed"]] == ["cost"]


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


def test_main_rejects_a_run_count_below_one():
    with pytest.raises(SystemExit):
        demo_script.main(["--case", "1", "--scripted", "--runs", "0"])
