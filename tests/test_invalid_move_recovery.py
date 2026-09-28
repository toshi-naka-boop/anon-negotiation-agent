"""DV-03(金庫の部分): design.md §3.5。TurnInput.last_error と Vertex AI の再試行は後の段。

無効手(invalid の登録と、金庫で分かる無効手)が記録されること、ガードで拒否された
提案も評価回数を消費すること、受け手としての評価・accept の確かめ直し・判定は評価回数を
消費しないこと、相手が提案を重ねても自分の評価上限を超えないこと、同じ側の無効手が
3 回続いたときだけ終わることを確かめる。
"""

from vault.api_models import MoveRequest
from vault.models import EmployerRule
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    put_candidate_and_employer_templates,
    reject_all_policy,
    sample_package,
)


def _create(store, candidate_policy=None, employer_rules=None):
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=candidate_policy, employer_rules=employer_rules
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def test_referee_registered_invalid_move_is_recorded(store):
    # DV-03: レフェリーが見つけた無効手(schema_invalid・agent_timeout)の登録が記録される。
    nid = _create(store)
    response = store.process_move(
        nid,
        MoveRequest(expected_version=0, side="candidate", move="invalid", reason="schema_invalid"),
    )
    assert response.valid is False
    assert response.error == "schema_invalid"

    events = store.get_events(nid, "candidate")
    assert len(events) == 1
    assert events[0].kind == "invalid"
    assert events[0].reason == "schema_invalid"


def test_vault_detected_invalid_move_is_recorded(store):
    # DV-03: 金庫で分かる無効手(例: pending_offer がないのに accept)も記録される。
    nid = _create(store)
    response = store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="accept"))
    assert response.valid is False
    assert response.error == "no_pending_offer"

    events = store.get_events(nid, "candidate")
    assert events[-1].kind == "invalid"
    assert events[-1].reason == "no_pending_offer"


def test_guard_rejected_proposal_still_consumes_an_evaluation(store):
    # DV-03: ガードで拒否された提案(not_acceptable_to_own_principal)も評価回数を消費する。
    nid = _create(store, candidate_policy=reject_all_policy("candidate"))
    view_before = store.get_view(nid, "candidate")
    assert view_before.budget.remaining_evaluations == 16

    response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=sample_package())
    )
    assert response.valid is False
    assert response.error == "not_acceptable_to_own_principal"

    view_after = store.get_view(nid, "candidate")
    assert view_after.budget.remaining_evaluations == 15  # 1 回消費されている


def test_receiver_evaluation_accept_recheck_and_judgment_do_not_consume_evaluations(store):
    # DV-03: 受け手としての評価・accept の確かめ直し・判定は、評価回数を消費しない。
    nid = _create(store)
    package = sample_package()

    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    assert propose_response.valid is True

    # 提案を受けた時点(受け手としての評価が起きたはず)で、求人側の評価回数は 0 のまま。
    employer_view = store.get_view(nid, "employer")
    assert employer_view.budget.remaining_evaluations == 16

    accept_response = store.process_move(
        nid, MoveRequest(expected_version=propose_response.version, side="employer", move="accept")
    )
    assert accept_response.status == "judged"  # accept の確かめ直し・判定(§3.6)を経て合意

    # 交渉は judged になり snapshots は消えるが、counters 自体は消費されていないはず。
    # judged 後は get_view の budget 計算に使う counters がまだ Firestore に残っているので、
    # 直接 Firestore の文書を見て確認する。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["counters"]["employer"]["evaluations_used"] == 0


def test_counterparty_proposals_never_exceed_receivers_own_evaluation_budget(store):
    # DV-03: 相手が提案を重ねても、自分の評価上限を超えない。candidate が提案 → employer が
    # 断る、を手数の上限(6 回)いっぱいまで繰り返しても、employer 自身の評価回数
    # (evaluations_used)は 0 のまま(受け手としての評価は数えないため)。
    nid = _create(store, employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    package = sample_package()
    version = 0
    for _ in range(6):
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=package)
        )
        assert response.valid is True
        version = response.version
        response = store.process_move(nid, MoveRequest(expected_version=version, side="employer", move="reject"))
        assert response.valid is True
        version = response.version

    employer_view = store.get_view(nid, "employer")
    assert employer_view.budget.remaining_evaluations == 16  # 一度も自分の手で評価していない


def test_negotiation_ends_only_after_three_consecutive_invalid_moves_from_the_same_side(store):
    # DV-03: 同じ側の無効手が 3 回続いたときだけ終わる(2 回では終わらない)。
    nid = _create(store, candidate_policy=reject_all_policy("candidate"))
    version = 0
    for _ in range(2):
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
        )
        assert response.valid is False
        assert response.status == "active"  # まだ終わらない
        version = response.version

    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
    )
    assert response.valid is False
    assert response.status == "judged"
    assert response.end_reason == "stopped_invalid"


def test_a_valid_move_resets_the_consecutive_invalid_counter(store):
    # DV-03 の裏付け: 途中で有効な手を挟むと、連続無効手の数がリセットされ、3 回に届かない。
    nid = _create(store, employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    version = 0

    # 無効手を 2 回(no_pending_offer での accept)。
    for _ in range(2):
        response = store.process_move(nid, MoveRequest(expected_version=version, side="candidate", move="accept"))
        assert response.valid is False
        version = response.version

    # 有効な手を 1 回(check)。連続無効手はここでリセットされるはず。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="check", package=sample_package())
    )
    assert response.valid is True
    version = response.version

    # さらに無効手を 2 回続けても(合計では直前から数えて 2 回なので)、まだ終わらない。
    for _ in range(2):
        response = store.process_move(nid, MoveRequest(expected_version=version, side="candidate", move="accept"))
        assert response.valid is False
        assert response.status == "active"
        version = response.version
