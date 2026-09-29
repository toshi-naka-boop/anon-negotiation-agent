"""AC-07(金庫の部分): design.md §3.5・§4.4・§12.1。

途中確認は側ごとの上限(合計で 2)を超えないこと、回答が追記先の規則どおりに追記される
こと(§4.4)、同じ組み合わせ(または支配される組み合わせ)には以後金庫が答えること、
次の交渉では前の交渉の回答(外した軸について中立でないものを除く。P-5)が使われる
ことを確かめる。架空人物への追記(交渉用コピーにだけ入る)は DV-09 (3)
(tests/test_statement_conversion.py)、外した軸で中立でない回答が本体に保存されない
ことの中心は DV-05 (3)(tests/test_remove_axis.py)で確かめる。
"""

from negotiation_core import Policy, Verdict, evaluate

from vault.api_models import ControlRequest, MoveRequest, PrincipalAnswerRequest
from vault.models import EmployerRule
from vault.serialization import model_from_firestore
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    live_create_request,
    make_employer_template,
    needs_confirmation_policy,
    new_id,
    put_candidate_policy,
    sample_package,
)


def _wildcard_employer_rule():
    return EmployerRule(when={}, policy=accept_all_policy("employer"))


def _create_live(store, pid, *, policy=None, removed_axes=None):
    employer_template = make_employer_template(rules=[_wildcard_employer_rule()])
    put_template(store._db, employer_template)
    put_candidate_policy(
        store,
        pid,
        policy=policy if policy is not None else needs_confirmation_policy("candidate"),
        removed_axes=removed_axes,
    )
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"
    return result.nid


def _ask(store, nid, version, package):
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="ask_principal", package=package)
    )
    assert response.status == "awaiting_principal", response
    return response.version


def test_principal_check_budget_is_capped_per_side_with_total_of_two(store):
    # AC-07: 途中確認は側ごとの上限(1)を超えない。合計では 2 まで(候補者 1 + 求人 1)。
    pid = new_id("principal")
    nid = _create_live(store, pid)
    p1 = sample_package(salary=650)
    v = _ask(store, nid, 0, p1)
    v = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="candidate", package=p1, answer="accept")
    ).version

    # 候補者側はすでに 1 回使っているので、別の(まだ NEEDS_CONFIRMATION の)組み合わせで
    # 2 回目を尋ねようとすると拒否される。
    p2 = sample_package(salary=550, remote_days=1)
    response = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="ask_principal", package=p2)
    )
    assert response.valid is False
    assert response.error == "question_budget_exhausted"

    assert store.get_view(nid, "candidate").budget.remaining_principal_checks == 0
    # 求人側はまだ 1 回分残っている(側ごとに独立した上限)。
    assert store.get_view(nid, "employer").budget.remaining_principal_checks == 1


def test_real_candidate_accept_answer_is_appended_to_both_body_and_copy(store):
    # AC-07: 回答が追記先の規則どおりに追記される(本物の候補者、軸を外していない場合)。
    pid = new_id("principal")
    nid = _create_live(store, pid)
    package = sample_package(salary=700)
    v = _ask(store, nid, 0, package)

    response = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="accept")
    )
    assert response.status == "active"

    principal_doc = store._principal_ref(pid).get().to_dict()
    body_policy = model_from_firestore(Policy, principal_doc["policy"])
    assert len(body_policy.accept_anchors) == 1
    assert evaluate(body_policy, package) is Verdict.ACCEPTABLE

    negotiation_doc = store._negotiation_ref(nid).get().to_dict()
    copy_policy = model_from_firestore(Policy, negotiation_doc["snapshots"]["candidate"])
    assert len(copy_policy.accept_anchors) == 1
    assert evaluate(copy_policy, package) is Verdict.ACCEPTABLE


def test_real_candidate_reject_answer_is_appended_to_both_body_and_copy(store):
    # AC-07: 「受けない」の場合も、本体・コピーの両方に受けないアンカーとして追記される。
    pid = new_id("principal")
    nid = _create_live(store, pid)
    package = sample_package(salary=350)
    v = _ask(store, nid, 0, package)

    response = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="reject")
    )
    assert response.status == "active"

    principal_doc = store._principal_ref(pid).get().to_dict()
    body_policy = model_from_firestore(Policy, principal_doc["policy"])
    assert len(body_policy.reject_anchors) == 1
    assert evaluate(body_policy, package) is Verdict.NOT_ACCEPTABLE

    negotiation_doc = store._negotiation_ref(nid).get().to_dict()
    copy_policy = model_from_firestore(Policy, negotiation_doc["snapshots"]["candidate"])
    assert len(copy_policy.reject_anchors) == 1
    assert evaluate(copy_policy, package) is Verdict.NOT_ACCEPTABLE


def test_vault_answers_a_dominated_combination_without_asking_again(store):
    # AC-07: 同じ組み合わせ(または支配される組み合わせ)には、以後、金庫が答える。
    pid = new_id("principal")
    nid = _create_live(store, pid)
    package = sample_package(salary=650, remote_days=2)
    v = _ask(store, nid, 0, package)
    v = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="accept")
    ).version

    # 同じ組み合わせは、もちろん ACCEPTABLE。
    same_check = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="check", package=package)
    )
    assert same_check.valid is True
    v = same_check.version
    assert store.get_view(nid, "candidate").last_check.own_evaluation is Verdict.ACCEPTABLE

    # 支配される(全軸で同等以上に良い)組み合わせも ACCEPTABLE。
    better_package = sample_package(salary=750, remote_days=3)
    better_check = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="check", package=better_package)
    )
    assert better_check.valid is True
    assert store.get_view(nid, "candidate").last_check.own_evaluation is Verdict.ACCEPTABLE


def test_next_negotiation_uses_the_previous_bodys_answer(store):
    # AC-07: 次の交渉では、前の交渉の回答(本体に保存されたもの)が使われる。
    pid = new_id("principal")
    nid_1 = _create_live(store, pid)
    package = sample_package(salary=650)
    v = _ask(store, nid_1, 0, package)
    store.process_principal_answer(
        nid_1, PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="accept")
    )
    store.control(nid_1, ControlRequest(side="candidate", action="cancel"))  # 1 件までの制約を外す

    # 同じ候補者で 2 つ目の交渉を作る(put_candidate_policy はもう呼ばない: 呼ぶと本体の
    # ポリシーが上書きされ、1 つ目の交渉で追記した分が消えてしまう)。
    employer_template_2 = make_employer_template(rules=[_wildcard_employer_rule()])
    put_template(store._db, employer_template_2)
    result_2 = store.create_negotiation(live_create_request(pid, employer_template_2.template_id))
    assert result_2.status == "created"
    nid_2 = result_2.nid

    # 新しい交渉のコピーには、前の交渉で答えた受けるアンカーがすでに入っているので、
    # 途中確認なしで check だけで ACCEPTABLE になる。
    check_response = store.process_move(
        nid_2, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
    )
    assert check_response.valid is True
    assert store.get_view(nid_2, "candidate").last_check.own_evaluation is Verdict.ACCEPTABLE


def test_next_negotiation_does_not_use_a_removed_axis_non_neutral_answer(store):
    # AC-07 / P-5: 外した軸について中立でない回答は、その交渉の中だけで使い、次の交渉では
    # 使われない(本体に保存されないため)。
    pid = new_id("principal")
    nid_1 = _create_live(store, pid, removed_axes=["night_duty"])
    package = sample_package(salary=650, night_duty=2)  # 2 は候補者の最悪値(8)ではない
    v = _ask(store, nid_1, 0, package)
    store.process_principal_answer(
        nid_1, PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="accept")
    )
    store.control(nid_1, ControlRequest(side="candidate", action="cancel"))

    employer_template_2 = make_employer_template(rules=[_wildcard_employer_rule()])
    put_template(store._db, employer_template_2)
    result_2 = store.create_negotiation(live_create_request(pid, employer_template_2.template_id))
    nid_2 = result_2.nid

    # 前の交渉のコピーにしか入らなかったので、新しい交渉ではまだ NEEDS_CONFIRMATION のまま。
    check_response = store.process_move(
        nid_2, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
    )
    assert check_response.valid is True
    assert store.get_view(nid_2, "candidate").last_check.own_evaluation is Verdict.NEEDS_CONFIRMATION


def test_append_anchor_if_consistent_skips_a_contradictory_anchor():
    # AC-07 / §4.4: 「どちらも矛盾検査を通す。矛盾したら追記しない」。
    #
    # ask_principal は評価が NEEDS_CONFIRMATION のときしか成功しない(§3.5)。
    # is_contradictory(anchor_from_package(P), 既存アンカー) は、まさに「P が既存アンカーを
    # 満たす/含まれるか」と同じ式になるため、ask_principal の前提(P はどの既存アンカーも
    # 満たさない・含まれない)がすでに非矛盾を保証してしまい、状態機械を通る自然な経路では
    # この分岐を再現できない。そのため、principal_answer.append_anchor_if_consistent を
    # 直接呼んで確かめる(vault.judgment・vault.stop_rule と同じ、純粋関数の直接テスト)。
    from negotiation_core import Anchor

    from vault import principal_answer as pa

    # 「年収 600 万以上なら(他は問わず)受ける」。他の数値軸は§2.2の中立(受けるアンカー
    # なので最悪値)、区分軸は * にして、salary だけを制約するアンカーにしてある。
    existing_accept = Anchor(
        salary=600, remote_days=0, night_duty=8, review_months=12, training="*", side_job="*", start="*"
    )
    policy = Policy(side="candidate", accept_anchors=[existing_accept], reject_anchors=[])

    # 「年収 650 万なら(他は問わず)受けない」。650 は 600 以上なので、既存の受けるアンカー
    # と重なり、矛盾する。
    contradicting_reject = Anchor(
        salary=650, remote_days=5, night_duty=0, review_months=6, training="*", side_job="*", start="*"
    )

    updated_policy, appended = pa.append_anchor_if_consistent(policy, contradicting_reject, "reject")

    assert appended is False
    assert updated_policy == policy  # 何も変わっていない
    assert updated_policy.reject_anchors == []
