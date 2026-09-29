"""DV-11(金庫の部分): design.md §4.1・§4.4。TurnInput を作る部分は後の段。

途中確認に答えた後の view で、pending_offer と last_check の評価が回答と合っていること、
§4.1 の図の流れ(求人側の途中確認 → 回答 → accept)が金庫の API で最後まで通ること、
評価のし直しで評価回数が減らない(消費しない)ことを確かめる。
"""

from negotiation_core import Verdict

from vault.api_models import MoveRequest, PrincipalAnswerRequest
from vault.models import EmployerRule
from vault_helpers import (
    demo_create_request,
    needs_confirmation_policy,
    put_candidate_and_employer_templates,
    sample_package,
)


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
    assert store.get_view(nid, "employer").budget.remaining_evaluations == 16 - 2
