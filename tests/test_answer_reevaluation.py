"""DV-11: 途中確認の回答と、評価のし直し(design.md §4.1・§4.4)。

金庫の部分: 途中確認に答えた後の view で、pending_offer と last_check の評価が回答と合っていること、
§4.1 の図の流れ(求人側の途中確認 → 回答 → accept)が金庫の API で最後まで通ること、
評価のし直しで評価回数が減らない(消費しない)ことを確かめる。

web(レフェリー)の部分(末尾。1d-1。v14 の計画・確かめ・決定に合わせた): 途中確認に答えた後の最初の TurnInput で、
pending_offer と last_check の評価が回答と合っていること。§4.1 の図の流れ(求人側の途中確認 → 自動回答 → accept)が、
レフェリーで最後まで通ること。回答の前に「本人確認が必要」だった組み合わせ(last_check ではない、古い履歴の記録)を、回答の後の
計画が確かめると、履歴からは埋まらず、金庫が 1 回呼ばれて評価回数が 1 減り、checked に金庫の新しい評価が入ること
(台帳 C-43・X-54)。
"""

import pytest
from negotiation_core import Verdict

from vault.api_models import MoveRequest, PrincipalAnswerRequest
from vault.models import EmployerRule
from vault_helpers import (
    demo_create_request,
    needs_confirmation_policy,
    put_candidate_and_employer_templates,
    sample_package,
)
from web.referee import StepOutcome
from web_helpers import ScriptedAnswerer, SpyVault, create_demo_negotiation, drive, move_dict, plan_dict


def _create(store):
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db,
        employer_rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))],
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def _drive_to_employer_awaiting_principal(store, nid, package):
    """candidate が propose(P) → employer が check(P) → ask_principal(P) まで進める。

    §4.1 の図と同じ経路(求人側の pending_offer に対する途中確認)。employer 側の
    last_check と pending_offer.receiver_evaluation の両方を、同じ P で NEEDS_CONFIRMATION に
    しておく(principal-answer がどちらも書き換えることを確かめるため)。
    """
    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    assert propose_response.valid is True
    v = propose_response.version

    check_response = store.process_move(
        nid, MoveRequest(expected_version=v, side="employer", move="check", package=package)
    )
    assert check_response.valid is True
    v = check_response.version

    ask_response = store.process_move(
        nid, MoveRequest(expected_version=v, side="employer", move="ask_principal", package=package)
    )
    assert ask_response.valid is True
    assert ask_response.status == "awaiting_principal"
    return ask_response.version


def test_principal_answer_updates_pending_offer_and_last_check_to_match_the_answer(store):
    # DV-11: 途中確認に答えた後の view で、pending_offer と last_check の評価が
    # 回答と合っている(「受ける」と答えたので、どちらも ACCEPTABLE になる)。
    package = sample_package()
    nid = _create(store)
    v = _drive_to_employer_awaiting_principal(store, nid, package)

    # 答える前は、両方とも NEEDS_CONFIRMATION(employer は空のポリシーで始めているため)。
    view_before = store.get_view(nid, "employer")
    assert view_before.pending_offer.own_evaluation is Verdict.NEEDS_CONFIRMATION
    assert view_before.last_check.own_evaluation is Verdict.NEEDS_CONFIRMATION

    answer_response = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="employer", package=package, answer="accept")
    )
    assert answer_response.status == "active"

    view_after = store.get_view(nid, "employer")
    assert view_after.pending_offer.package == package
    assert view_after.pending_offer.own_evaluation is Verdict.ACCEPTABLE
    assert view_after.last_check.package == package
    assert view_after.last_check.own_evaluation is Verdict.ACCEPTABLE


def test_employer_ask_principal_answer_accept_flow_completes_via_vault_api(store):
    # DV-11: §4.1 の図の流れ(求人側の途中確認 → 回答 → accept)が、金庫の API で
    # 最後まで通る(合意に至る)。
    package = sample_package()
    nid = _create(store)
    v = _drive_to_employer_awaiting_principal(store, nid, package)

    answer_response = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="employer", package=package, answer="accept")
    )
    v = answer_response.version
    assert answer_response.status == "active"

    accept_response = store.process_move(nid, MoveRequest(expected_version=v, side="employer", move="accept"))
    assert accept_response.valid is True
    assert accept_response.status == "judged"
    assert accept_response.end_reason == "agreed"


def test_reevaluation_after_answering_does_not_consume_evaluation_budget(store):
    # DV-11: 評価のし直し(pending_offer.receiver_evaluation と last_check の書き換え)で、
    # 評価回数(evaluations_used)が減らない(=消費しない)。
    package = sample_package()
    nid = _create(store)
    v = _drive_to_employer_awaiting_principal(store, nid, package)

    # ここまでの employer の評価回数: check 1 回 + ask_principal 1 回 = 2 回。
    counters_before = store._negotiation_ref(nid).get().to_dict()["counters"]["employer"]
    assert counters_before["evaluations_used"] == 2

    store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="employer", package=package, answer="accept")
    )

    # pending_offer と last_check の 2 件を評価し直しても、回数は 2 のまま増減しない。
    counters_after = store._negotiation_ref(nid).get().to_dict()["counters"]["employer"]
    assert counters_after["evaluations_used"] == 2
    assert store.get_view(nid, "employer").budget.remaining_evaluations == 17 - 2


# --- web(レフェリー)の部分(1d-1) ---


def _employer_needs_confirmation():
    return [EmployerRule(when={}, policy=needs_confirmation_policy("employer"))]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "expected", "checked_salary"),
    [
        # 求人側は、年収が低いほど良い。「受ける」と答えた組み合わせ(700 万)より良い 600 万は、受けるアンカーに広がって「受けられる」。
        ("accept", Verdict.ACCEPTABLE, 600),
        # 「受けない」と答えた組み合わせ(700 万)より悪い 800 万は、受けないアンカーに含まれて「受けられない」。
        ("reject", Verdict.NOT_ACCEPTABLE, 800),
    ],
)
async def test_first_turn_input_after_the_answer_carries_the_reevaluated_verdicts(
    store, web_env, answer, expected, checked_salary
):
    # DV-11: 途中確認に答えた後の最初の TurnInput で、pending_offer と last_check の評価が回答と合っている。
    # 答える前は、どちらも「本人確認が必要」。評価のし直しは、評価回数を消費しない。
    # 求人側は、提案(700 万)を受けた手番で、別の組み合わせ(last_check になる)を確かめてから、提案について途中確認する。
    env = web_env
    env.configure(answerer=ScriptedAnswerer(answer))
    package = sample_package()
    checked = sample_package(salary=checked_salary)
    nid = create_demo_negotiation(store, employer_rules=_employer_needs_confirmation())
    env.agents.script("candidate", move_dict("propose", package), move_dict("end"))
    env.agents.script(
        "employer",
        plan_dict(checks=[checked]),  # 計画: 別の組み合わせを確かめる(金庫の check。last_check になる)
        move_dict("ask_principal", package),  # 決定: 提案について途中確認
        move_dict(answer),  # 回答の後の計画: 受けると答えたら accept、受けないと答えたら reject
    )

    outcomes = await drive(env.referee(nid))

    assert outcomes[:4] == [
        StepOutcome.MOVED,  # 候補者 propose
        StepOutcome.MOVED,  # 求人側 計画(確かめ)→ 決定(ask_principal)
        StepOutcome.ANSWERED,  # 自動回答
        StepOutcome.FINISHED if answer == "accept" else StepOutcome.MOVED,  # 求人側 accept / reject
    ]
    plan_turn, decide_turn, after_answer = [c.turn_input for c in env.agents.calls_for("employer")]
    assert (plan_turn.phase, decide_turn.phase, after_answer.phase) == ("plan", "decide", "plan")
    assert plan_turn.pending_offer.own_evaluation is Verdict.NEEDS_CONFIRMATION
    assert decide_turn.last_check.package == checked
    assert decide_turn.last_check.own_evaluation is Verdict.NEEDS_CONFIRMATION
    assert after_answer.pending_offer.package == package
    assert after_answer.pending_offer.own_evaluation is expected  # 回答に合わせて評価し直されている
    assert after_answer.last_check.package == checked
    assert after_answer.last_check.own_evaluation is expected
    # 自分の評価は確かめと ask_principal の 2 回だけ(17 → 16 → 15)。評価し直しでは減らない。
    assert decide_turn.budget.remaining_evaluations == 16
    assert after_answer.budget.remaining_evaluations == 15
    assert after_answer.budget.remaining_principal_checks == 0  # 途中確認は 1 回使った


@pytest.mark.anyio
async def test_employer_question_flow_runs_through_the_referee_to_agreement(store, web_env):
    # DV-11: §4.1 の図の流れ(求人側の途中確認 → 自動回答 → accept)が、レフェリーで最後まで通る。
    # 図のとおり、求人側の計画は checks が空で、手(ask_principal)をそのまま出す(確かめは要らない。受けた提案の評価は、
    # 自分側の見え方にすでにある)。回答の後の計画も、checks が空で accept。
    env = web_env
    answerer = ScriptedAnswerer("accept")
    env.configure(answerer=answerer)
    package = sample_package()
    nid = create_demo_negotiation(store, employer_rules=_employer_needs_confirmation())
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("ask_principal", package), move_dict("accept"))

    outcomes = await drive(env.referee(nid))

    assert outcomes == [StepOutcome.MOVED, StepOutcome.MOVED, StepOutcome.ANSWERED, StepOutcome.FINISHED]
    assert answerer.calls == [(nid, "employer", package)]
    assert [c.turn_input.phase for c in env.agents.calls_for("employer")] == ["plan", "plan"]  # 決定は呼ばない(1 手番 1 回)
    doc = store._negotiation_ref(nid).get().to_dict()
    assert (doc["status"], doc["end_reason"]) == ("judged", "agreed")
    for side in ("candidate", "employer"):  # 最終記録は双方に 1 件だけ。同じ内容
        finals = [e for e in store.get_events(nid, side) if e.kind == "final_result"]
        assert len(finals) == 1
        assert finals[0].result.package == package
        assert finals[0].result.likelihood in ("high", "medium")
    # 回答は求人側の見え方にだけ残り、候補者には見えない。
    assert [e.kind for e in store.get_events(nid, "employer")] == [
        "offer_received",
        "ask_principal",
        "principal_answer",
        "final_result",
    ]
    assert "principal_answer" not in [e.kind for e in store.get_events(nid, "candidate")]


@pytest.mark.anyio
async def test_a_fictional_candidates_question_is_answered_automatically_too(store, web_env):
    # 架空の候補者(デモ)の途中確認にも、自動で答える。答えの後の TurnInput では、確認した組み合わせが
    # 「受けられる」になっていて、そのまま提案できる(ガードを通る)。
    env = web_env
    env.configure(answerer=ScriptedAnswerer("accept"))
    package = sample_package()
    nid = create_demo_negotiation(store, candidate_policy=needs_confirmation_policy("candidate"))
    env.agents.script(
        "candidate", plan_dict(checks=[package]), move_dict("ask_principal", package), move_dict("propose", package)
    )
    env.agents.script("employer", move_dict("accept"))

    outcomes = await drive(env.referee(nid))

    assert outcomes == [StepOutcome.MOVED, StepOutcome.ANSWERED, StepOutcome.MOVED, StepOutcome.FINISHED]
    after_answer = env.agents.calls_for("candidate")[2].turn_input  # 回答の後の計画
    assert after_answer.phase == "plan"
    assert after_answer.last_check.own_evaluation is Verdict.ACCEPTABLE
    assert store.get_view(nid, "candidate").status == "judged"


@pytest.mark.anyio
async def test_a_package_that_needed_confirmation_before_the_answer_is_checked_again_by_the_plan_after_it(
    store, web_env
):
    # DV-11・台帳 C-43・X-54: 回答の前に「本人確認が必要」だった組み合わせ(last_check ではなく、それより古い履歴の記録)を、
    # 回答の後の計画が確かめると、履歴からは埋まらず、金庫が 1 回呼ばれて評価回数が 1 減り、checked に金庫の新しい評価
    # (回答で広がった「受けられる」)が入る。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    env.configure(answerer=ScriptedAnswerer("accept"))
    package = sample_package(salary=700)
    older, newer = sample_package(salary=600), sample_package(salary=550)  # どちらも、求人側には 700 万より良い
    nid = create_demo_negotiation(store, employer_rules=_employer_needs_confirmation())
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script(
        "employer",
        plan_dict(checks=[older, newer]),  # 計画: 2 つ確かめる(どちらも「本人確認が必要」。last_check は後の newer)
        move_dict("ask_principal", package),  # 決定: 提案について途中確認 → 「受ける」(older・newer も受けられるに広がる)
        plan_dict(checks=[older]),  # 回答の後の計画: older を確かめ直す(履歴の記録は、回答より前の「本人確認が必要」)
        move_dict("accept"),  # 決定
    )

    await drive(env.referee(nid))

    after_answer_plan, after_answer_decide = [c.turn_input for c in env.agents.calls_for("employer")][2:]
    assert after_answer_plan.last_check.package == newer  # last_check は older ではない
    assert after_answer_plan.budget.remaining_evaluations == 14  # 確かめ 2 回・途中確認 1 回を使った(17 → 14)
    employer_checks = [r for r in spy.move_requests if r.side == "employer" and r.move == "check"]
    assert [r.package for r in employer_checks] == [older, newer, older]  # 回答の後に、older が 1 回、金庫で確かめられた
    assert after_answer_decide.budget.remaining_evaluations == 13  # 評価回数が 1 減った
    assert [(c.package, c.evaluation) for c in after_answer_decide.checked] == [(older, Verdict.ACCEPTABLE)]
    assert store.get_view(nid, "candidate").status == "judged"
