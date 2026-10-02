"""DV-17: 1 手番の流れ(計画 → 確かめ → 決定)のレフェリーの部分(design.md §4.1・§2.7・§12.2)。

LLM は、受けた TurnInput を記録するスタブ(tests/web_helpers.py の ScriptedAgents)。金庫は本物(Firestore エミュレータ)。
設計書 §12.2 の DV-17 の行を、1 項目ずつ、下のテストに対応させた(テスト名がその項目)。

| 項目 | テスト |
|---|---|
| 1 手につきエージェントの呼び出しは最大 2 回・側ごとの手番は 7 回以下・交渉 1 回は 28 回以下(36 通り) | test_at_most_two_agent_calls_per_move_and_28_per_negotiation_over_the_36_starts |
| checks は並びの順に確かめ、「受けられる」が出たら残りは確かめない | test_checks_run_in_the_order_of_the_plan_and_stop_at_the_first_acceptable_one |
| 見え方にある「受けられる」「受けられない」は金庫を呼ばずに埋まる。「本人確認が必要」は回答の後だけ確かめ直す | test_known_not_acceptable_and_needs_confirmation_records_are_filled_without_calling_the_vault・test_a_needs_confirmation_record_is_checked_again_after_this_side_answered_a_question |
| 残りの評価が「残りの手数 ＋ 残りの途中確認数」以下なら確かめない。残りの手のすべてで提案のガードが通る | test_checks_stop_when_the_remaining_evaluations_only_cover_the_remaining_moves_and_the_question |
| checks が空の手はそのまま登録・checks と move の両方は checks を実行して move を無視・どちらもない/move=check は schema_invalid | test_a_plan_without_checks_registers_its_move_as_it_is・test_a_plan_with_both_checks_and_a_move_runs_the_checks_and_ignores_the_move・test_a_plan_with_neither_checks_nor_a_move_and_a_check_as_a_move_are_schema_invalid |
| 計画が無効手の手番では決定を呼ばない。決定の無効手が 3 回続くと、間に確かめがあっても stopped_invalid | test_an_invalid_plan_does_not_call_the_decision・test_three_invalid_decisions_in_a_row_stop_the_negotiation_even_with_checks_between_them |
| 決定の TurnInput は、確かめの後に読み直した view から作られ、checked が確かめた結果と順序どおりに一致する | test_the_decision_input_is_built_from_the_view_read_again_after_the_checks_and_carries_the_checked_results |
| 確かめの途中の 409・レフェリーの作り直しでも、同じ手番から続き、済んだ確かめは重ねて消費しない | test_a_409_during_the_checks_restarts_the_turn_without_spending_a_finished_check_again・test_a_recreated_referee_does_not_spend_a_finished_check_again |
| 無効な決定の後、確かめを挟んだ同じ手番の決定と次の手番の計画に last_error・last_invalid が残る | test_after_an_invalid_decision_the_next_plan_and_the_decision_after_a_check_both_keep_last_error |
| LLM の受けた要求の数が、レフェリーが数えた送信の数と一致する | test_the_agents_received_calls_equal_the_sends_the_referee_counted |
| 応答の封筒の検証の失敗は schema_invalid | test_a_failure_of_the_response_envelope_check_is_schema_invalid_and_is_not_retried |

次の 2 項目は、agents 側のテストで確かめる(web からは見えない): 本物の Gemini モデルの下の HTTP 層を偽の transport に差し替えて、
HTTP の要求が 1 回だけ出ること(`retry_options.attempts=1`。X-55)。計画・決定の両方で、LLM が受けた要求の system_instruction が前文と
ハッシュで一致し、contents が TurnInput の JSON 1 件だけで、thinking_config・temperature が設定の値と一致すること。
金庫の変更(有効な check が連続無効手を 0 に戻さない)に依る項目は、その金庫でないときは、スキップする。
"""

import asyncio
import logging
from collections import Counter

import pytest
from negotiation_core import Anchor, Budget, Policy, Verdict, best_value, worst_value
from scripted_negotiators import Negotiator, ScriptedNegotiators, Strategy
from test_fixtures import the_36_starts
from vault.api_models import ControlRequest, MoveRequest
from vault.fixtures import load_case_fixture, put_fixture_templates
from vault_helpers import demo_create_request, needs_confirmation_policy, sample_package
from web.fictional_answerer import FixtureAnswerer
from web.llm_budget import LlmBudget
from web.referee import NegotiationContext, Referee, RefereeDeps, StepOutcome
from web_helpers import (
    Blocker,
    ScriptedAnswerer,
    SpyVault,
    create_demo_negotiation,
    drive,
    make_usage,
    move_dict,
    plan_dict,
)

NOT, NEEDS, ACC = Verdict.NOT_ACCEPTABLE, Verdict.NEEDS_CONFIRMATION, Verdict.ACCEPTABLE


def threshold_policy(accept_from: int, reject_up_to: int) -> Policy:
    """候補者のポリシー: 年収が accept_from 以上なら「受けられる」、reject_up_to 以下なら「受けられない」、その間は「本人確認が必要」。

    年収以外の軸は気にしない(受けるアンカーは年収以外を最悪値、受けないアンカーは最良値にして、区分軸は * にする)。
    """
    accept = Anchor(
        salary=accept_from,
        remote_days=worst_value("remote_days", "candidate"),
        night_duty=worst_value("night_duty", "candidate"),
        review_months=worst_value("review_months", "candidate"),
        training="*",
        side_job="*",
        start="*",
    )
    reject = Anchor(
        salary=reject_up_to,
        remote_days=best_value("remote_days", "candidate"),
        night_duty=best_value("night_duty", "candidate"),
        review_months=best_value("review_months", "candidate"),
        training="*",
        side_job="*",
        start="*",
    )
    return Policy(side="candidate", accept_anchors=[accept], reject_anchors=[reject])


# 年収 500 万以下は「受けられない」(A)、600 万は「本人確認が必要」(B)、700 万以上は「受けられる」(C)
A, B, C = sample_package(salary=500), sample_package(salary=600), sample_package(salary=700)


def _threshold_negotiation(store) -> str:
    return create_demo_negotiation(store, candidate_policy=threshold_policy(accept_from=700, reject_up_to=500))


def vault_checks(spy: SpyVault, side: str = "candidate") -> list:
    """レフェリーが金庫に登録した確かめ(check)の組み合わせ(登録した順。409 になったものも含む)。"""
    return [r.package for r in spy.move_requests if r.side == side and r.move == "check"]


def _kinds(store, nid: str, side: str = "candidate") -> list[str]:
    return [e.kind for e in store.get_events(nid, side)]


class CountingSend:
    """SendTurn を包み、呼び出しごとの (側, 呼び出しの種類) を記録する。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, role, turn_input, *, nid, timeout_s):
        self.calls.append((turn_input.side, turn_input.phase))
        return await self._inner(role, turn_input, nid=nid, timeout_s=timeout_s)


# ----------------------------------------------------------------------
# 項目: 1 手(無効手を含む)につき、エージェントの呼び出しは最大 2 回。側ごとの正常な手番は 7 回以下、交渉 1 回は 28 回以下
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_at_most_two_agent_calls_per_move_and_28_per_negotiation_over_the_36_starts(store, web_env):
    # DV-17: 台本のエージェント(指示文どおりの探し方)でケース 1 の 36 通りを通して数える。レフェリーの 1 回の step()(1 手番)で、
    # エージェントの呼び出しは最大 2 回(計画と決定)。側ごとの手番は 7 回以下(手数 6 ＋ 有効な途中確認 1)で、交渉 1 回の呼び出しは
    # 2 × 7 × 2 = 28 回以下。
    env = web_env
    case1 = load_case_fixture(1)
    put_fixture_templates(store._db, case1)
    trade = Strategy("trade")
    most_calls = 0
    for candidate_opening, employer_opening in the_36_starts():
        created = store.create_negotiation(
            demo_create_request(case1.candidate.template_id, case1.employer.template_id)
        )
        assert created.status == "created"
        sender = CountingSend(
            ScriptedNegotiators(Negotiator(trade, candidate_opening), Negotiator(trade, employer_opening))
        )
        deps = RefereeDeps(
            vault=env.vault,
            send_turn=sender,
            clock=env.clock,
            sleep=env.sleep,
            config=env.config,
            answerer=FixtureAnswerer(case1),
        )
        referee = Referee(NegotiationContext(nid=created.nid, mode="demo", candidate_principal_id=None), deps)
        turns: Counter = Counter()
        for _ in range(100):
            before = len(sender.calls)
            outcome = await referee.step()
            made = sender.calls[before:]
            assert len(made) <= 2  # 1 手番(無効手を含む)につき、最大 2 回
            if made:
                assert len({side for side, _ in made}) == 1  # 1 手番の呼び出しは、手番の側だけ
                assert [phase for _, phase in made] == ["plan", "decide"][: len(made)]
                turns[made[0][0]] += 1
            if outcome is StepOutcome.FINISHED:
                break
        else:
            pytest.fail("the negotiation did not finish")
        assert all(count <= 7 for count in turns.values()), turns
        assert len(sender.calls) <= 28
        most_calls = max(most_calls, len(sender.calls))
    assert most_calls >= 2  # 数えている(何も呼ばずに通っているのではない)


# ----------------------------------------------------------------------
# 項目: checks は並びの順に確かめ、「受けられる」が出たら残りは確かめない(null になる)
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("planned", "executed", "expected"),
    [
        ([A, B, C], [A, B, C], [(A, NOT), (B, NEEDS), (C, ACC)]),  # 受けられるのは最後: 並びの順に、全部確かめる
        ([C, A, B], [C], [(C, ACC), (A, None), (B, None)]),  # 先頭が受けられる: 残りは確かめない
        ([A, C, B], [A, C], [(A, NOT), (C, ACC), (B, None)]),  # 途中で受けられる: そこから先は確かめない
    ],
    ids=["acceptable_last", "acceptable_first", "acceptable_in_the_middle"],
)
async def test_checks_run_in_the_order_of_the_plan_and_stop_at_the_first_acceptable_one(
    store, web_env, planned, executed, expected
):
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", plan_dict(checks=planned), move_dict("end"))

    await env.referee(nid).step()

    assert vault_checks(spy) == executed  # 並びの順。受けられるが出たら、そこで止まる
    decide = env.agents.calls_in_phase("decide", "candidate")[0].turn_input
    assert [(c.package, c.evaluation) for c in decide.checked] == expected  # 計画の順。確かめなかった案は null


@pytest.mark.anyio
async def test_a_package_listed_twice_in_a_plan_is_checked_only_once(store, web_env):
    # 同じ組み合わせが計画に 2 回あっても、1 回目の結果が見え方に入るので、2 回目は金庫を呼ばずに埋まる(評価は 1 回だけ消費)。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", plan_dict(checks=[A, A]), move_dict("end"))

    await env.referee(nid).step()

    assert vault_checks(spy) == [A]
    decide = env.agents.calls_in_phase("decide", "candidate")[0].turn_input
    assert [(c.package, c.evaluation) for c in decide.checked] == [(A, NOT), (A, NOT)]
    assert decide.budget.remaining_evaluations == 16


# ----------------------------------------------------------------------
# 項目: 見え方にすでにある「受けられる」「受けられない」の組み合わせは、金庫を呼ばずに埋まり、評価を消費しない。
#       「本人確認が必要」の記録は、その後に自分側の途中確認の回答があるときだけ確かめ直し、なければ埋まる(C-48)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_known_not_acceptable_and_needs_confirmation_records_are_filled_without_calling_the_vault(store, web_env):
    # 1 手番目で A(受けられない)・B(本人確認が必要)を確かめて、C を提案する。相手に断られた 2 手番目の計画が、同じ A・B を
    # 確かめたいと言っても、自分側の履歴にある評価で埋まり、金庫は呼ばれず、評価も減らない(途中確認の回答がないので、
    # 「本人確認が必要」の B も、記録から埋まる)。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    env.agents.script(
        "candidate",
        plan_dict(checks=[A, B]),
        move_dict("propose", C),
        plan_dict(checks=[A, B]),  # 2 手番目: 同じ案を、もう一度確かめたい
        move_dict("end"),
    )
    env.agents.script("employer", move_dict("reject"))

    await drive(env.referee(nid))

    assert vault_checks(spy) == [A, B]  # 金庫の check は、1 手番目の 2 回だけ
    second_plan, second_decide = [c.turn_input for c in env.agents.calls_for("candidate")][2:]
    assert [(c.package, c.evaluation) for c in second_decide.checked] == [(A, NOT), (B, NEEDS)]  # 履歴から埋まった
    assert second_decide.budget.remaining_evaluations == second_plan.budget.remaining_evaluations  # 評価を消費していない
    assert second_plan.budget.remaining_evaluations == 14  # 確かめ 2 回と、提案のガード 1 回(17 → 14)


@pytest.mark.anyio
async def test_a_needs_confirmation_record_is_checked_again_after_this_side_answered_a_question(store, web_env):
    # 「本人確認が必要」の記録(B)の後に、自分側の途中確認の回答(別の組み合わせ P への)がはさまれば、回答で受けるアンカーが
    # 広がり得るので、記録では埋めず、金庫で確かめ直す(その分の評価は消費する)。一方、「受けられない」の記録(A)は、
    # 回答の後でも変わらないので、金庫を呼ばずに埋まる。B は last_check ではない古い履歴の記録にしておく(last_check は、回答で
    # 評価し直されるので、履歴より優先して埋まる。台帳 L14-2)ために、確かめの順は B・A。
    env = web_env
    env.configure(answerer=ScriptedAnswerer("accept"))
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    asked = sample_package(salary=650)  # 本人確認が必要な組み合わせ
    env.agents.script(
        "candidate",
        plan_dict(checks=[B, A]),  # 1 手番目: B(本人確認が必要)・A(受けられない。last_check になる)
        move_dict("ask_principal", asked),  # 途中確認 → 自動回答(受ける)
        plan_dict(checks=[B, A]),  # 回答の後の計画: 同じ案を確かめたい
        move_dict("end"),
    )

    await drive(env.referee(nid))

    assert vault_checks(spy) == [B, A, B]  # 回答の後は、B だけが金庫で確かめ直された。A は埋まった
    plan_after, decide_after = [c.turn_input for c in env.agents.calls_for("candidate")][2:]
    assert plan_after.last_check.package == A  # B は、last_check ではない古い履歴の記録
    assert plan_after.budget.remaining_evaluations == 14  # 確かめ 2 回・途中確認 1 回(17 → 14)
    assert [(c.package, c.evaluation) for c in decide_after.checked] == [(B, NEEDS), (A, NOT)]
    assert decide_after.budget.remaining_evaluations == 13  # 確かめ直した B の分だけ減った


# ----------------------------------------------------------------------
# 項目: 残りの評価回数が「残りの手数 ＋ 残りの途中確認数」以下なら確かめず、途中確認(「受ける」の回答)の後の提案を含めて、
#       残りの手のすべてで提案のガードが通る(C-47)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_checks_stop_when_the_remaining_evaluations_only_cover_the_remaining_moves_and_the_question(store, web_env):
    env = web_env
    env.configure(answerer=ScriptedAnswerer("accept"))
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = create_demo_negotiation(store, candidate_policy=needs_confirmation_policy("candidate"))
    # 評価を 9 回使っておく(17 → 8)。残りの手数 6 ＋ 残りの途中確認 1 = 7 なので、確かめにはあと 1 回しか使えない。
    version = 0
    for k in range(9):
        used = store.process_move(
            nid,
            MoveRequest(
                expected_version=version,
                side="candidate",
                move="check",
                package=sample_package(salary=300 + 50 * k),
            ),
        )
        assert used.valid
        version = used.version
    assert store.get_view(nid, "candidate").budget == Budget(
        remaining_evaluations=8, remaining_moves=6, remaining_principal_checks=1
    )
    first, second = sample_package(remote_days=5), sample_package(remote_days=4)
    asked = sample_package(salary=650, remote_days=3)
    env.agents.script(
        "candidate",
        plan_dict(checks=[first, second]),  # 2 つ確かめたい: 1 つ目だけ確かめられる(8 > 7)。2 つ目は確かめない(7 ≤ 7)
        move_dict("ask_principal", asked),  # 途中確認(評価 1。8 → 6 で、残りの手数 6 と同じ)
        *[move_dict("propose", asked)] * 6,  # 「受ける」の答えの後、6 手のすべてで、提案のガードの評価が残っている
    )
    env.agents.script("employer", *[move_dict("reject")] * 6)

    outcomes = await drive(env.referee(nid), max_steps=40)

    assert outcomes[-1] is StepOutcome.FINISHED
    assert vault_checks(spy) == [first]  # 2 つ目は、金庫を呼ばずに null
    decide = env.agents.calls_in_phase("decide", "candidate")[0].turn_input
    assert [(c.package, c.evaluation) for c in decide.checked] == [(first, NEEDS), (second, None)]
    kinds = _kinds(store, nid)
    assert kinds.count("propose") == 6 and "invalid" not in kinds  # 6 回の提案がすべてガードを通った
    document = store._negotiation_ref(nid).get().to_dict()
    assert (document["status"], document["end_reason"]) == ("judged", "stopped_budget")
    assert len(env.agents.calls_for("candidate")) == 2 + 6  # 1 手番目の計画・決定と、提案 6 回(7 手番目は、呼ばずに終わる)


# ----------------------------------------------------------------------
# 項目: checks が空のときの手はそのまま登録され、checks と move の両方がある Plan は checks を実行して move を無視し、
#       どちらもない Plan と move=check を出した Plan・Move は schema_invalid になる(C-51)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_plan_without_checks_registers_its_move_as_it_is(store, web_env):
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", plan_dict(move="propose", package=C))

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert [(r.move, r.package) for r in spy.move_requests] == [("propose", C)]  # そのまま登録
    assert [c.turn_input.phase for c in env.agents.calls] == ["plan"]  # 決定は呼ばない(1 手番 1 回)


@pytest.mark.anyio
async def test_a_plan_with_both_checks_and_a_move_runs_the_checks_and_ignores_the_move(store, web_env):
    # 計画を優先する寛容な読み方(無効手にすると、手がかりのない last_invalid で同じ計画を繰り返すため)。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", plan_dict(checks=[A], move="propose", package=C), move_dict("end"))

    await drive(env.referee(nid))

    assert [(r.move, r.package) for r in spy.move_requests] == [("check", A), ("end", None)]  # 計画の propose は無視された
    assert [c.turn_input.phase for c in env.agents.calls] == ["plan", "decide"]
    assert _kinds(store, nid) == ["check", "final_result"]  # 提案は、一度も登録されていない


@pytest.mark.anyio
@pytest.mark.parametrize(
    "plan",
    [
        {"schema": "plan/v1", "checks": []},  # どちらもない(L12-3)
        {"schema": "plan/v1", "checks": [], "move": None, "package": None},
        {"schema": "plan/v1", "checks": [], "move": "check", "package": A.model_dump(mode="json")},  # check は手ではない
        {"schema": "plan/v1", "checks": [], "move": "propose"},  # propose なのに package がない
    ],
    ids=["neither_checks_nor_move", "neither_with_nulls", "check_as_a_move", "propose_without_a_package"],
)
async def test_a_plan_with_neither_checks_nor_a_move_and_a_check_as_a_move_are_schema_invalid(store, web_env, plan):
    env = web_env
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", plan)

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "schema_invalid")]
    assert [c.turn_input.phase for c in env.agents.calls] == ["plan"]  # 無効な計画の後に、決定は呼ばない


@pytest.mark.anyio
async def test_a_decision_that_is_a_check_is_schema_invalid(store, web_env):
    # 決定(Move)にも、check は出せない(エージェントが出せる手ではない。X-45)。確かめ(計画)の後でも、無効手として登録される。
    env = web_env
    nid = _threshold_negotiation(store)
    env.agents.script(
        "candidate", plan_dict(checks=[A]), {"schema": "move/v1", "move": "check", "package": B.model_dump(mode="json")}
    )

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [
        ("check", None),
        ("invalid", "schema_invalid"),
    ]


# ----------------------------------------------------------------------
# 項目: 計画が無効手になった手番では決定を呼ばず、決定の無効手が 3 回続くと、間に確かめがあっても stopped_invalid で止まる
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [{"schema": "plan/v1", "checks": []}, ValueError("rejected"), RuntimeError("bug")],
    ids=["schema_invalid", "value_error", "unexpected_exception"],
)
async def test_an_invalid_plan_does_not_call_the_decision(store, web_env, failure):
    env = web_env
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", failure)

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert [c.turn_input.phase for c in env.agents.calls] == ["plan"]
    assert _kinds(store, nid) == ["invalid"]


def _vault_keeps_the_invalid_streak_across_checks(store) -> bool:
    """金庫が、有効な check で連続無効手を 0 に戻さない(v14。台帳 X-48・C-46)か。使い捨ての交渉で確かめる。"""
    nid = create_demo_negotiation(store)
    version = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="invalid", reason="schema_invalid")
    ).version
    store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="check", package=sample_package())
    )
    return store._negotiation_ref(nid).get().to_dict()["counters"]["candidate"]["consecutive_invalid"] == 1


@pytest.mark.anyio
async def test_three_invalid_decisions_in_a_row_stop_the_negotiation_even_with_checks_between_them(store, web_env):
    # 計画ごとに新しい案を確かめる(有効な check が間に入る)。決定が 3 回続けて無効なら、金庫が stopped_invalid で止める
    # (有効な check は連続無効手を 0 に戻さない。レフェリーの確かめが間に入っても、止まる)。
    if not _vault_keeps_the_invalid_streak_across_checks(store):
        pytest.skip("this vault still resets the invalid streak at a valid check (the v14 vault change is not in yet)")
    env = web_env
    nid = _threshold_negotiation(store)
    garbage = {"schema": "move/v1", "move": "withdraw"}
    env.agents.script(
        "candidate",
        plan_dict(checks=[A]),
        garbage,
        plan_dict(checks=[B]),
        garbage,
        plan_dict(checks=[sample_package(salary=550)]),
        garbage,
    )

    outcomes = await drive(env.referee(nid))

    assert outcomes == [StepOutcome.MOVED, StepOutcome.MOVED, StepOutcome.FINISHED]
    document = store._negotiation_ref(nid).get().to_dict()
    assert (document["status"], document["end_reason"]) == ("judged", "stopped_invalid")
    assert _kinds(store, nid) == ["check", "invalid", "check", "invalid", "check", "invalid", "final_result"]


# ----------------------------------------------------------------------
# 項目: 決定の TurnInput は、確かめの後に読み直した view(残りの評価回数が確かめの分だけ減っている)から作られ、
#       checked が確かめた結果と順序どおりに一致する
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_decision_input_is_built_from_the_view_read_again_after_the_checks_and_carries_the_checked_results(
    store, web_env
):
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", plan_dict(checks=[A, B, C]), move_dict("propose", C))
    env.agents.script("employer", move_dict("accept"))

    await drive(env.referee(nid))

    plan, decide = [c.turn_input for c in env.agents.calls_for("candidate")]
    assert (plan.phase, plan.checked) == ("plan", [])
    assert decide.phase == "decide"
    assert plan.budget == Budget(remaining_evaluations=17, remaining_moves=6, remaining_principal_checks=1)
    assert decide.budget == Budget(remaining_evaluations=14, remaining_moves=6, remaining_principal_checks=1)  # 確かめ 3 回
    assert [(c.package, c.evaluation) for c in decide.checked] == [(A, NOT), (B, NEEDS), (C, ACC)]  # 順序どおり
    assert (decide.last_check.package, decide.last_check.own_evaluation) == (C, ACC)  # 読み直した view の last_check
    assert [(e.by, e.move, e.package, e.result) for e in decide.history] == [
        ("self", "check", A, NOT),
        ("self", "check", B, NEEDS),
        ("self", "check", C, ACC),
    ]  # 確かめは、イベント列にあるので、history に従来どおり self・check として入る
    # expected_version を付けて、view を読み直して version を追った: 確かめの version は 1 つずつ進む。
    versions = [r.expected_version for r in spy.move_requests if r.move == "check"]
    assert versions == [versions[0], versions[0] + 1, versions[0] + 2]


# ----------------------------------------------------------------------
# 項目: 確かめの途中で 409 が返る・レフェリーを止めて作り直す(再起動の模擬)のどちらでも、同じ手番から続き、済んだ確かめは
#       履歴から埋まって金庫の評価を重ねて消費しない(自分側の途中確認の回答がはさまった「本人確認が必要」だけは確かめ直す。
#       上の 2 項目)
# ----------------------------------------------------------------------


class InterruptBeforeCheck:
    """package の確かめ(金庫の check)が金庫に届く直前に、一時停止と再開を挟む(version だけが進み、その check は 409 になる)。"""

    def __init__(self, inner, real, package) -> None:
        self._inner = inner
        self._real = real
        self._package = package
        self.interrupted = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def post_move(self, nid, request):
        if request.move == "check" and request.package == self._package and not self.interrupted:
            self.interrupted = True
            await self._real.control(nid, ControlRequest(side="candidate", action="pause"))
            await self._real.control(nid, ControlRequest(side="candidate", action="resume"))
        return await self._inner.post_move(nid, request)


@pytest.mark.anyio
async def test_a_409_during_the_checks_restarts_the_turn_without_spending_a_finished_check_again(store, web_env):
    env = web_env
    real = env.vault
    spy = SpyVault(real)
    interrupting = InterruptBeforeCheck(spy, real, B)
    await env.restart(vault=interrupting)
    nid = _threshold_negotiation(store)
    env.agents.script(
        "candidate",
        plan_dict(checks=[A, B]),  # A は確かめられ、B の check が 409 になる
        plan_dict(checks=[A, B]),  # やり直した手番の計画: A は履歴から埋まり、B だけが確かめられる
        move_dict("end"),
    )
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.RETRY  # 手番をやめて、状態を読み直す
    assert interrupting.interrupted
    assert await referee.step() is StepOutcome.FINISHED  # 同じ手番を、計画から続ける

    assert vault_checks(spy) == [A, B, B]  # A は 1 回だけ。B は、409 になった分(消費なし)と、やり直した分
    assert _kinds(store, nid) == ["check", "pause", "resume", "check", "final_result"]  # 確かめの記録は、A・B 1 件ずつ
    decide = env.agents.calls_in_phase("decide", "candidate")[0].turn_input
    assert [(c.package, c.evaluation) for c in decide.checked] == [(A, NOT), (B, NEEDS)]
    assert decide.budget.remaining_evaluations == 15  # 評価は 2 回だけ消費(17 → 15)。A を重ねて消費していない


@pytest.mark.anyio
async def test_a_recreated_referee_does_not_spend_a_finished_check_again(store, web_env):
    # 決定の呼び出しの最中に web が落ちた(再起動の模擬)。作り直したレフェリーは、計画から始め、済んだ確かめを履歴から埋める。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    nid = _threshold_negotiation(store)
    blocker = Blocker()
    env.agents.script("candidate", plan_dict(checks=[A, B]), blocker)  # 計画 → 確かめ 2 回 → 決定(応答のないまま落ちる)
    task = asyncio.create_task(env.referee(nid).run())
    await asyncio.wait_for(blocker.started.wait(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert vault_checks(spy) == [A, B]
    assert store.get_view(nid, "candidate").budget.remaining_evaluations == 15

    await env.restart(vault=spy)  # 作り直す
    env.agents.script("candidate", plan_dict(checks=[A, B]), move_dict("end"))
    await drive(env.referee(nid))

    assert vault_checks(spy) == [A, B]  # 金庫の check は、増えていない
    decide = env.agents.calls_in_phase("decide", "candidate")[-1].turn_input
    assert [(c.package, c.evaluation) for c in decide.checked] == [(A, NOT), (B, NEEDS)]
    assert decide.budget.remaining_evaluations == 15  # 評価を重ねて消費していない


# ----------------------------------------------------------------------
# 項目: 無効な決定の後、確かめを挟んだ同じ手番の決定と次の手番の計画の TurnInput に、last_error・last_invalid が残る(X-51)
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ({"schema": "move/v1", "move": "withdraw"}, "schema_invalid"),
        (ConnectionError("down"), "agent_timeout"),  # 再試行を使い切った(最初の 1 回 ＋ 3 回)
    ],
    ids=["schema_invalid", "agent_timeout"],
)
async def test_after_an_invalid_decision_the_next_plan_and_the_decision_after_a_check_both_keep_last_error(
    store, web_env, failure, reason
):
    env = web_env
    nid = _threshold_negotiation(store)
    decision_failures = [failure] if reason == "schema_invalid" else [failure] * 4
    env.agents.script(
        "candidate",
        plan_dict(checks=[A]),
        *decision_failures,  # 手番 1 の決定: 無効
        plan_dict(checks=[B]),  # 手番 2 の計画(last_error あり): 確かめ
        move_dict("end"),  # 手番 2 の決定(last_error が残っている)
    )
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED  # 手番 1: 計画 → 確かめ → 無効な決定
    assert await referee.step() is StepOutcome.FINISHED  # 手番 2: 計画 → 確かめ → 決定

    inputs = [c.turn_input for c in env.agents.calls_for("candidate")]
    plan_two, decide_two = inputs[-2], inputs[-1]
    assert (plan_two.phase, decide_two.phase) == ("plan", "decide")
    for turn_input in (plan_two, decide_two):
        assert turn_input.last_error == reason
        assert turn_input.last_invalid is not None and turn_input.last_invalid.move is None
    assert _kinds(store, nid) == ["check", "invalid", "check", "final_result"]


# ----------------------------------------------------------------------
# 項目: LLM の受けた要求の数が、レフェリーが数えた送信の数と一致する(X-50)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_agents_received_calls_equal_the_sends_the_referee_counted(store, web_env):
    # 再試行(一時的なエラー)を含めて、スタブが受けた呼び出しの数 = レフェリーが送る前に数えた交渉ごとの物理の数 = 1 日の数。
    env = web_env
    budget = LlmBudget(env.default_db, env.clock)
    env.configure(llm_budget=budget)
    nid = _threshold_negotiation(store)
    await env.stages.ensure(nid, None)
    env.agents.script(
        "candidate",
        ConnectionError("temporary"),  # 計画 1 回目: 一時的なエラー(再試行される)
        plan_dict(checks=[A, B]),
        move_dict("propose", C),
        move_dict("end"),
    )
    env.agents.script("employer", move_dict("reject"))

    await drive(env.referee(nid))

    assert len(env.agents.calls) == 5  # 候補者 4 回(再試行 1 回を含む)＋求人側 1 回
    assert await budget.negotiation_count(nid) == 5
    assert await budget.daily_count() == 5


# ----------------------------------------------------------------------
# 項目: 応答の封筒の検証: usage の欠落・負の値・設定と違うモデル ID・余分な artifact や metadata は schema_invalid になる(X-58)
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "violation",
    [
        "usage_is_missing",
        "usage_has_a_negative_count",
        "usage_names_another_model",
        "response_has_an_extra_artifact",
        "artifact_metadata_has_an_extra_key",
    ],
)
async def test_a_failure_of_the_response_envelope_check_is_schema_invalid_and_is_not_retried(store, web_env, violation):
    # 封筒の検証は agents のクライアント(agents.client.send_turn)が行い、違反は ValueError(送り直しても直らない失敗)で伝える
    # 約束(SendTurn)。レフェリーは、それを再試行せずに、schema_invalid の無効手として登録する。
    env = web_env
    nid = _threshold_negotiation(store)
    env.agents.script("candidate", ValueError(violation))

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "schema_invalid")]
    assert len(env.agents.calls) == 1 and env.sleep.calls == []  # 再試行しない


# ----------------------------------------------------------------------
# 呼び出しごとの使用量のログ(§4.3): トークン数などの数だけ。交渉 ID・組み合わせの値は書かない
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_referee_logs_the_usage_of_each_call_as_numbers_only(store, web_env, caplog):
    caplog.set_level(logging.INFO, logger="web.referee")
    env = web_env
    nid = _threshold_negotiation(store)
    usage = make_usage(prompt_tokens=1234, cached_tokens=5, thoughts_tokens=67, output_tokens=89)
    env.agents.script("candidate", (plan_dict(checks=[A]), usage), (move_dict("end"), usage))

    await drive(env.referee(nid))

    assert (
        "agent call side=candidate phase=plan outcome=ok prompt_tokens=1234 cached_tokens=5 thoughts_tokens=67 "
        "output_tokens=89 requests=1"
    ) in caplog.text
    assert "agent call side=candidate phase=decide outcome=ok" in caplog.text
    assert nid not in caplog.text and "salary" not in caplog.text  # 交渉 ID も、組み合わせの値も書かない
