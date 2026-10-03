"""面談エージェント(web/interview/agent.py。design.md §5 の 2・4・5、§4.1、§8.2。DV-18 の面談の部分)。

本物の LLM には接続しない。ADK に差し込むスタブの LLM(tests/agents_helpers.py の StubLlm)が、LLM に渡った入力を記録する。
物理の呼び出し数は、Firestore エミュレータの (default) に数える(web.llm_budget)。時計は注入(FixedClock)、sleep はしない(FakeSleep)。

- JSON モード(応答スキーマなし)・max_output_tokens・固定の指示文・新しいセッション(会話を積まない。L14-3)・LLM の入力に ID が入らない
- 出力の検証(SalaryBasis・ConstraintList)・出力が切れたとき
- 送る前の計上(送っている最中にカウンタがすでに進んでいる。X-54)、再試行も 1 回と数え 429・5xx でも戻さない(X-56)、1 日の上限
- Vertex AI の一時的なエラーの再試行(§4.1 と同じ規則)。短い間の連打の枠(§8.2 の面談の枠)は、ここではなく HTTP の入口(web.limits の
  interview_llm)にある → tests/test_interview_api.py
- トレースのスパンに面談の中身が載らない(別のプロセスで OpenTelemetry のスパンを集めて確かめる。§5・§7)
"""

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agents_helpers import StubLlm, llm_response
from google.adk.models.google_llm import Gemini
from google.genai import errors as genai_errors
from google.genai import types

from agents.config import DEFAULT_AGENTS_CONFIG
from negotiation_core import AXES
from web.config import DEFAULT_WEB_CONFIG
from web.interview.agent import (
    DAILY_LIMIT_REACHED,
    LLM_FAILED,
    LLM_UNAVAILABLE,
    OUTPUT_INVALID,
    OUTPUT_TRUNCATED,
    InterviewAgent,
    InterviewLlmFailure,
    load_instruction,
)
from web.interview.config import DEFAULT_INTERVIEW_CONFIG
from web.interview.salary import SalaryBasis
from web.llm_budget import LlmBudget, LlmBudgetUnavailable, jst_date
from web_helpers import FakeSleep

OWNER = "0123456789abcdef"
QA = [("質問 1", "年収 600 万円です"), ("質問 2", "固定残業代なし"), ("質問 3", "賞与込みです")]
BASIS_JSON = json.dumps(
    {
        "amount_man_yen": 600,
        "amount_period": "annual",
        "amount_kind": "gross",
        "bonus_included": True,
        "bonus_months": 0,
        "fixed_overtime_man_yen_per_month": 0,
    }
)
STATEMENTS_JSON = json.dumps(
    {"statements": [{"polarity": "reject", "salary": None, "remote_days": None, "night_duty": 4, "review_months": None, "training": None, "side_job": None, "start": None}]}
)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def make_agent(default_db, clock, model, *, config=DEFAULT_INTERVIEW_CONFIG, retry=None, budget_config=None, sleep=None):
    budget = LlmBudget(default_db, clock, budget_config) if budget_config is not None else LlmBudget(default_db, clock)
    return InterviewAgent(
        budget=budget,
        clock=clock,
        sleep=sleep if sleep is not None else FakeSleep(clock),
        retry=retry if retry is not None else DEFAULT_WEB_CONFIG.referee,
        config=config,
        model=model,
    )


def daily_count(default_db, clock) -> int:
    snapshot = default_db.collection("llm_call_counters").document(jst_date(clock.now())).get()
    return snapshot.to_dict()["count"] if snapshot.exists else 0


def user_text(request) -> dict:
    """LLM(スタブ)に渡った入力(ユーザーの 1 通のメッセージ)の JSON。"""
    (role, texts), = request.contents
    assert role == "user" and len(texts) == 1
    return json.loads(texts[0])


def server_error(code=503):
    return genai_errors.ServerError(code, {"error": {"message": "unavailable"}})


def raising(error):
    """呼ばれたら error を投げる、スタブの LLM の振る舞い。"""

    def behavior(request):
        raise error

    return behavior


# ---------------------------------------------------------------------------
# LLM に渡るもの
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_salary_agent_asks_for_json_without_a_response_schema_and_with_the_output_limit(default_db, clock):
    stub = StubLlm(behavior=lambda request: BASIS_JSON)
    agent = make_agent(default_db, clock, stub)

    basis = await agent.extract_salary_basis(OWNER, QA)

    assert basis.amount_man_yen == 600 and basis.amount_kind == "gross"
    (request,) = stub.requests
    assert request.response_mime_type == "application/json"  # JSON モード(台帳 I-19)
    assert request.response_schema is None  # 応答スキーマは使わない(pydantic で検証)
    assert request.max_output_tokens == DEFAULT_INTERVIEW_CONFIG.max_output_tokens == 2048  # C-49
    assert request.temperature == DEFAULT_AGENTS_CONFIG.temperature
    assert request.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert not request.tools  # ツールは持たない
    assert request.system_instruction == load_instruction("salary")  # 固定の指示文そのもの(ADK が足す 1 文は外す)
    message = user_text(request)
    assert message["task"] == "salary_basis"
    assert [item["answer"] for item in message["qa"]] == [answer for _, answer in QA]
    assert OWNER not in request.dump  # LLM の入力に、依頼者の ID は入らない


@pytest.mark.anyio
async def test_the_constraint_agent_gets_its_own_instruction_and_the_kind_of_the_text(default_db, clock):
    stub = StubLlm(behavior=lambda request: STATEMENTS_JSON)
    agent = make_agent(default_db, clock, stub)

    parsed, dropped = await agent.extract_constraints(OWNER, "reason_for_leaving", "夜勤が多すぎた")

    assert [(s.polarity, s.night_duty) for s in parsed.statements] == [("reject", 4.0)] and dropped == 0
    (request,) = stub.requests
    assert request.system_instruction == load_instruction("constraints") != load_instruction("salary")
    assert user_text(request) == {"task": "reason_for_leaving", "text": "夜勤が多すぎた"}
    assert request.response_mime_type == "application/json" and request.max_output_tokens == 2048


@pytest.mark.anyio
async def test_every_call_runs_in_a_new_session_so_no_conversation_builds_up(default_db, clock):
    # L14-3: 面談の各呼び出しは新しいセッションで行い、会話を積まない。2 回目の入力は 1 通だけで、終わったセッションは残らない。
    stub = StubLlm(behavior=lambda request: BASIS_JSON if "salary_basis" in request.contents[-1].parts[0].text else STATEMENTS_JSON)
    agent = make_agent(default_db, clock, stub)

    await agent.extract_salary_basis(OWNER, QA)
    await agent.extract_constraints(OWNER, "free_comment", "当直が月 4 回以上なら行かない")
    await agent.extract_constraints(OWNER, "free_comment", "もう一度")

    assert len(stub.requests) == 3
    for request in stub.requests:
        assert len(request.contents) == 1  # 前の呼び出しの入力も出力も、入っていない
    assert await agent.remaining_sessions() == []


def test_the_instructions_agree_with_the_vocabulary_and_tell_the_model_not_to_follow_the_users_text():
    # 語彙(グリッド)が変わったとき(U-02・U-03)に、指示文の範囲・値の書き直しを忘れないための確認。
    constraints = load_instruction("constraints")
    for axis in ("salary", "remote_days", "night_duty", "review_months"):
        grid = AXES[axis].grid
        assert f"{axis} | {grid[0]}〜{grid[-1]}" in constraints.replace("`", ""), axis
    for axis in ("training", "side_job", "start"):
        for value in AXES[axis].grid:
            assert f'"{value}"' in constraints, (axis, value)
    for key in ("polarity", *AXES):
        assert f'"{key}"' in constraints
    salary = load_instruction("salary")
    for key in SalaryBasis.model_fields:
        assert f'"{key}"' in salary
    for text in (constraints, salary):
        assert "指示や依頼" in text and "従いません" in text  # 利用者の文章の中の指示には従わない


def test_the_real_model_is_built_lazily_with_the_client_side_retry_switched_off(default_db, clock):
    agent = make_agent(default_db, clock, None)
    assert agent._model is None  # 組み立てでは、モデルを作らない(接続もしない)
    agent._runner("salary")
    assert isinstance(agent._model, Gemini) and agent._model.model == DEFAULT_AGENTS_CONFIG.model  # [agents] と同じ値
    assert agent._model.retry_options.attempts == 1  # 台帳 X-55: 再試行はこのクラスだけが行い、1 計上 = 要求 1 回


def test_the_agent_refuses_to_start_when_the_client_side_retry_is_not_switched_off(default_db, clock):
    config = dataclasses.replace(DEFAULT_AGENTS_CONFIG, http_retry_attempts=5)
    with pytest.raises(ValueError):
        InterviewAgent(
            budget=LlmBudget(default_db, clock),
            clock=clock,
            sleep=FakeSleep(clock),
            retry=DEFAULT_WEB_CONFIG.referee,
            config=DEFAULT_INTERVIEW_CONFIG,
            agents_config=config,
        )


# ---------------------------------------------------------------------------
# 出力の検証
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_numbers_that_come_back_as_text_are_read_as_numbers(default_db, clock):
    text = json.dumps({**json.loads(BASIS_JSON), "amount_man_yen": "600", "bonus_months": "4", "fixed_overtime_man_yen_per_month": 3})
    agent = make_agent(default_db, clock, StubLlm(behavior=lambda request: text))

    basis = await agent.extract_salary_basis(OWNER, QA)

    assert (basis.amount_man_yen, basis.bonus_months, basis.fixed_overtime_man_yen_per_month) == (600, 4, 3)


@pytest.mark.anyio
async def test_unknown_fields_in_the_output_are_ignored_but_known_fields_are_checked(default_db, clock):
    # 面談には、直させる手がかりを返す往復がない。LLM が説明の項目を添えても、読み取りは捨てない。
    text = json.dumps({**json.loads(BASIS_JSON), "explanation": "年収 600 万円と読みました"})
    statements = json.dumps({"statements": [{**json.loads(STATEMENTS_JSON)["statements"][0], "explanation": "当直が多い"}], "note": "x"})
    stub = StubLlm(behavior=lambda request: text if "salary_basis" in request.contents[-1].parts[0].text else statements)
    agent = make_agent(default_db, clock, stub)

    basis = await agent.extract_salary_basis(OWNER, QA)
    parsed, dropped = await agent.extract_constraints(OWNER, "free_comment", "当直が月 4 回以上なら行かない")

    assert basis.amount_man_yen == 600 and "explanation" not in basis.model_dump()
    assert [s.night_duty for s in parsed.statements] == [4.0] and dropped == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "output",
    [
        "これは JSON ではありません",
        "[1, 2, 3]",
        "",
        json.dumps({**json.loads(BASIS_JSON), "amount_kind": "unknown"}),
        json.dumps({**json.loads(BASIS_JSON), "amount_man_yen": -5}),
        json.dumps({key: value for key, value in json.loads(BASIS_JSON).items() if key != "bonus_months"}),
    ],
)
async def test_an_output_that_is_not_a_valid_salary_basis_is_refused_without_a_retry(default_db, clock, output):
    stub = StubLlm(behavior=lambda request: output)
    agent = make_agent(default_db, clock, stub)

    with pytest.raises(InterviewLlmFailure) as excinfo:
        await agent.extract_salary_basis(OWNER, QA)

    assert excinfo.value.code == OUTPUT_INVALID
    assert len(stub.requests) == 1  # 同じ入力を送り直しても直らない(temperature 0)ので、再試行しない
    assert daily_count(default_db, clock) == 1


@pytest.mark.anyio
async def test_statements_that_fail_validation_are_dropped_and_counted_and_a_malformed_list_is_refused(default_db, clock):
    payload = {"statements": [json.loads(STATEMENTS_JSON)["statements"][0], {"polarity": "accept", "salary": 2000}, {"polarity": "reject"}]}
    stub = StubLlm(behavior=lambda request: json.dumps(payload))
    agent = make_agent(default_db, clock, stub)

    parsed, dropped = await agent.extract_constraints(OWNER, "free_comment", "text")
    assert len(parsed.statements) == 2 and dropped == 1  # 通らなかった 1 件だけを捨てる(もう 1 件は、軸に触れない発言。web.interview.service が扱う)

    broken = make_agent(default_db, clock, StubLlm(behavior=lambda request: json.dumps({"statements": "no"})))
    with pytest.raises(InterviewLlmFailure) as excinfo:
        await broken.extract_constraints(OWNER, "free_comment", "text")
    assert excinfo.value.code == OUTPUT_INVALID


@pytest.mark.anyio
async def test_an_output_cut_off_at_max_output_tokens_is_reported_and_not_retried(default_db, clock):
    stub = StubLlm(behavior=lambda request: llm_response(BASIS_JSON[:20], finish_reason=types.FinishReason.MAX_TOKENS))
    agent = make_agent(default_db, clock, stub)

    with pytest.raises(InterviewLlmFailure) as excinfo:
        await agent.extract_salary_basis(OWNER, QA)

    assert excinfo.value.code == OUTPUT_TRUNCATED
    assert len(stub.requests) == 1
    assert await agent.remaining_sessions() == []


# ---------------------------------------------------------------------------
# 物理の呼び出し数の計上(DV-18 の面談の部分)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_daily_count_is_already_advanced_while_the_request_is_in_flight(default_db, clock):
    # X-54: 送る前に数える。送っている最中(スタブが止めている間)に、永続のカウンタがすでに進んでいる。
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(request):
        entered.set()
        await release.wait()
        return BASIS_JSON

    agent = make_agent(default_db, clock, StubLlm(behavior=blocked))
    task = asyncio.create_task(agent.extract_salary_basis(OWNER, QA))
    await asyncio.wait_for(entered.wait(), timeout=10)

    assert daily_count(default_db, clock) == 1
    release.set()
    await task
    assert daily_count(default_db, clock) == 1


@pytest.mark.anyio
async def test_a_transient_error_is_retried_with_the_referees_rule_and_every_attempt_is_counted(default_db, clock):
    # §4.1 と同じ再試行: 一時的なエラー(5xx)を、待ち時間(1 秒・2 秒)を空けて再試行する。再試行も 1 回と数え、戻さない(X-56)。
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) < 3:
            raise server_error(503)
        return BASIS_JSON

    sleep = FakeSleep(clock)
    agent = make_agent(default_db, clock, StubLlm(behavior=flaky), sleep=sleep)

    basis = await agent.extract_salary_basis(OWNER, QA)

    assert basis.amount_man_yen == 600 and len(attempts) == 3
    assert sleep.calls == [1.0, 2.0]
    assert daily_count(default_db, clock) == 3
    assert await agent.remaining_sessions() == []


@pytest.mark.anyio
@pytest.mark.parametrize("code", [429, 500, 503])
async def test_when_the_retries_run_out_the_call_gives_up_and_nothing_is_given_back(default_db, clock, code):
    error = server_error(code) if code != 429 else genai_errors.ClientError(429, {"error": {}})
    stub = StubLlm(behavior=raising(error))
    agent = make_agent(default_db, clock, stub)

    with pytest.raises(InterviewLlmFailure) as excinfo:
        await agent.extract_salary_basis(OWNER, QA)

    assert excinfo.value.code == LLM_UNAVAILABLE
    retries = DEFAULT_WEB_CONFIG.referee.agent_max_retries
    assert len(stub.requests) == retries + 1
    assert daily_count(default_db, clock) == retries + 1  # 429・5xx でも、計上は戻さない


@pytest.mark.anyio
async def test_an_error_that_is_not_transient_is_not_retried(default_db, clock):
    stub = StubLlm(behavior=raising(genai_errors.ClientError(400, {"error": {"message": "bad"}})))
    agent = make_agent(default_db, clock, stub)

    with pytest.raises(InterviewLlmFailure) as excinfo:
        await agent.extract_salary_basis(OWNER, QA)

    assert excinfo.value.code == LLM_FAILED and len(stub.requests) == 1
    assert daily_count(default_db, clock) == 1


@pytest.mark.anyio
async def test_a_call_that_does_not_answer_in_time_is_retried_and_then_given_up(default_db, clock):
    async def never(request):
        await asyncio.Event().wait()

    retry = dataclasses.replace(
        DEFAULT_WEB_CONFIG.referee, agent_call_timeout_seconds=0.3, agent_max_retries=1, agent_retry_backoff_seconds=(0.001,)
    )
    stub = StubLlm(behavior=never)
    agent = make_agent(default_db, clock, stub, retry=retry)

    with pytest.raises(InterviewLlmFailure) as excinfo:
        await agent.extract_salary_basis(OWNER, QA)

    assert excinfo.value.code == LLM_UNAVAILABLE
    assert daily_count(default_db, clock) == 2  # 時間切れも一時的なエラー: 1 回再試行して、あきらめる。どちらも 1 回と数える
    assert 1 <= len(stub.requests) <= 2  # 負荷の高い環境では、最初の送信に届く前に時間切れになり得る
    assert await agent.remaining_sessions() == []


@pytest.mark.anyio
async def test_when_the_daily_limit_is_reached_nothing_is_sent(default_db, clock):
    budget_config = dataclasses.replace(DEFAULT_WEB_CONFIG.llm_budget, daily_limit=2)
    stub = StubLlm(behavior=lambda request: BASIS_JSON)
    agent = make_agent(default_db, clock, stub, budget_config=budget_config)

    await agent.extract_salary_basis(OWNER, QA)
    await agent.extract_salary_basis(OWNER, QA)
    with pytest.raises(InterviewLlmFailure) as excinfo:
        await agent.extract_salary_basis(OWNER, QA)

    assert excinfo.value.code == DAILY_LIMIT_REACHED
    assert len(stub.requests) == 2  # 3 回目は送っていない
    assert daily_count(default_db, clock) == 2  # 断った分は進めない


@pytest.mark.anyio
async def test_when_the_counter_cannot_be_written_nothing_is_sent(default_db, clock, monkeypatch):
    stub = StubLlm(behavior=lambda request: BASIS_JSON)
    agent = make_agent(default_db, clock, stub)

    async def unavailable(nid=None):
        raise LlmBudgetUnavailable("Aborted")

    monkeypatch.setattr(agent._budget, "reserve", unavailable)
    with pytest.raises(LlmBudgetUnavailable):
        await agent.extract_salary_basis(OWNER, QA)

    assert stub.requests == []  # カウンタに書けないときは送らない(閉じる側に倒す。X-50)


# ---------------------------------------------------------------------------
# トレースのスパンに面談の中身を載せない(§5・§7。AC-18 の面談の部分)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
SPAN_CANARY = "CANARY-7F3A-SPAN"
# 別のプロセスで、OpenTelemetry のスパンをメモリに集めながら面談の呼び出しを 1 回行い、スパンの中身を JSON で出す(プロセスで 1 つしか
# 設定できない TracerProvider を、テスト全体に残さないため)。環境変数の既定(web.interview.agent が決める)の呼び出しと、明示に
# true にした呼び出し(対照)の 2 回。
SPAN_PROBE = """
import asyncio, json, os, sys
os.environ.pop("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", None)
sys.path[:0] = ["src"]
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

from vault.clock import SystemClock
from web.config import DEFAULT_WEB_CONFIG
from web.interview.agent import InterviewAgent
from web.interview.config import DEFAULT_INTERVIEW_CONFIG
from web.llm_budget import Reservation


class Echo(BaseLlm):
    model: str = "stub"

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text='{"statements": []}')]))


class Budget:
    async def reserve(self, nid=None):
        return Reservation(True)


def collect():
    spans = exporter.get_finished_spans()
    exporter.clear()
    return json.dumps([{"name": s.name, "attributes": dict(s.attributes or {})} for s in spans], default=str, ensure_ascii=False)


async def main():
    agent = InterviewAgent(
        budget=Budget(), clock=SystemClock(), sleep=asyncio.sleep, retry=DEFAULT_WEB_CONFIG.referee,
        config=DEFAULT_INTERVIEW_CONFIG, model=Echo(),
    )
    dumps = {}
    await agent.extract_constraints("p", "free_comment", "__CANARY__ 当直が多い")
    dumps["default"] = collect()
    os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "true"
    await agent.extract_constraints("p", "free_comment", "__CANARY__ 当直が多い")
    dumps["explicit_true"] = collect()
    print(json.dumps(dumps))


asyncio.run(main())
"""


def test_trace_spans_do_not_carry_the_text_of_the_interview_unless_an_operator_switches_the_capture_on():
    # ADK は、既定でスパン(call_llm の gcp.vertex.agent.llm_request など)に LLM の入力の全文を載せる。面談のモジュールは、環境変数の
    # 既定を false にする。スパンは出ている(空だから通るのではない)。明示に true にしたときは載る(対照: 確認が見えている)。
    result = subprocess.run(
        [sys.executable, "-c", SPAN_PROBE.replace("__CANARY__", SPAN_CANARY)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    dumps = json.loads(result.stdout.strip().splitlines()[-1])

    assert "call_llm" in dumps["default"] and "invoke_agent interview_constraints_agent" in dumps["default"]
    assert SPAN_CANARY not in dumps["default"]
    assert SPAN_CANARY in dumps["explicit_true"]

