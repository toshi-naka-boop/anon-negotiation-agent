"""DV-05: removing an axis from consideration (design.md §2.4, §12.2).

DV-05 の 1 つ目(外した軸があっても、全 18,000 通りで「受けられない」が「受けられる」に
変わらない)と 2 つ目(代表的な面談フィクスチャで離散軸を 1 つ外しても、受けるアンカーが
1 件以上残る)を確かめる。3 つ目のうち、外した軸に触れる発言が本体に保存されないことは
1a で済んでいる。ここでは、1b-2 で足す残り(外した軸で中立でない途中確認の回答が本体に
保存されない)を確かめる。

sample_data.principal_decision は、negotiation_core の Anchor/Policy/evaluate を一切
使わない独立した「本人のモデル」。これを ground truth にすることで、単に
「アンカーを足して同じ evaluate() で確かめる」というトートロジーを避ける。
"""

import pytest

from negotiation_core.policy import Policy, Verdict, evaluate, iter_all_packages
from negotiation_core.vocabulary import worst_value

from sample_data import (
    DISCRETE_AXES_TO_REMOVE,
    build_policy_from_interview,
    principal_decision,
)
from vault.api_models import MoveRequest, PrincipalAnswerRequest
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

ALL_PACKAGES = list(iter_all_packages())
_SIDE = "candidate"
_POLICY_WITHOUT_ANY_AXIS_REMOVED = build_policy_from_interview(_SIDE, removed_axis=None)


@pytest.mark.parametrize("axis", DISCRETE_AXES_TO_REMOVE)
def test_removing_a_discrete_axis_never_turns_not_acceptable_into_acceptable(axis):
    # DV-05 (1) 1点目: 軸を外していないポリシー(a)と比べ、axis を外して答えたポリシー(b)は、
    # 全 18,000 通りのどれについても「受けられない」→「受けられる」に変わらない(単調性)。
    policy_a = _POLICY_WITHOUT_ANY_AXIS_REMOVED
    policy_b = build_policy_from_interview(_SIDE, removed_axis=axis)

    for package in ALL_PACKAGES:
        verdict_a = evaluate(policy_a, package)
        verdict_b = evaluate(policy_b, package)
        if verdict_a is Verdict.NOT_ACCEPTABLE:
            assert verdict_b is not Verdict.ACCEPTABLE, (
                f"axis={axis} package={package!r}: 外す前は NOT_ACCEPTABLE だったのに、"
                f"{axis} を外した後に ACCEPTABLE になった"
            )


@pytest.mark.parametrize("axis", DISCRETE_AXES_TO_REMOVE)
def test_removing_a_discrete_axis_stays_sound_against_the_principal_model(axis):
    # DV-05 (1) 2点目(健全性): axis を外して答えたポリシー(b)が「受けられる」と言うときは、
    # negotiation_core とは独立な本人のモデル(principal_decision)でも必ず「行く」。
    policy_b = build_policy_from_interview(_SIDE, removed_axis=axis)

    for package in ALL_PACKAGES:
        if evaluate(policy_b, package) is Verdict.ACCEPTABLE:
            assert principal_decision(package), (
                f"axis={axis} package={package!r}: {axis} を外した後に ACCEPTABLE と "
                "判定されたが、本人のモデルでは「行かない」(健全性が破れている)"
            )


@pytest.mark.parametrize("axis", DISCRETE_AXES_TO_REMOVE)
def test_removing_a_discrete_axis_leaves_at_least_one_accept_anchor(axis):
    # DV-05 (2): 代表的な面談フィクスチャ(sample_data の本人・設問)で、axis を 1 つ外して
    # 答えても、変換後の受けるアンカーが 1 件以上残る。既存ポリシーにアンカーを足す形は
    # とらず、外した状態から直接組み立てる。テストを通すための後付けの調整はしていない。
    policy_b = build_policy_from_interview(_SIDE, removed_axis=axis)
    assert len(policy_b.accept_anchors) >= 1


def _create_live_with_removed_axis(store, removed_axis):
    pid = new_id("principal")
    employer_template = make_employer_template(rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    put_template(store._db, employer_template)
    put_candidate_policy(
        store, pid, policy=needs_confirmation_policy("candidate"), removed_axes=[removed_axis]
    )
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"
    return pid, result.nid


def test_principal_answer_touching_a_removed_numeric_axis_is_not_saved_to_the_body(store):
    # DV-05 (3): night_duty を外した候補者が、night_duty について中立でない(最悪値 8
    # ではない)途中確認の回答をしても、依頼者本体には保存されない(交渉用コピーにだけ入る)。
    pid, nid = _create_live_with_removed_axis(store, "night_duty")
    package = sample_package(salary=650, night_duty=2)

    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert ask_response.status == "awaiting_principal"

    answer_response = store.process_principal_answer(
        nid,
        PrincipalAnswerRequest(
            expected_version=ask_response.version, side="candidate", package=package, answer="accept"
        ),
    )
    assert answer_response.status == "active"

    body_policy = model_from_firestore(Policy, store._principal_ref(pid).get().to_dict()["policy"])
    assert body_policy.accept_anchors == []  # 本体には保存されない

    copy_policy = model_from_firestore(
        Policy, store._negotiation_ref(nid).get().to_dict()["snapshots"]["candidate"]
    )
    assert len(copy_policy.accept_anchors) == 1  # 交渉用コピーには入る


def test_principal_answer_neutral_on_removed_numeric_axis_is_saved_to_the_body(store):
    # DV-05 (3) の裏付け: 同じ night_duty を外した候補者でも、回答した組み合わせの
    # night_duty がその軸について中立(候補者にとっての最悪値 8)なら、本体にも保存される
    # (「外した軸について中立でない」場合だけが本体に保存されない、ことの確認)。
    pid, nid = _create_live_with_removed_axis(store, "night_duty")
    package = sample_package(salary=650, night_duty=worst_value("night_duty", "candidate"))
    assert package.night_duty == 8

    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    answer_response = store.process_principal_answer(
        nid,
        PrincipalAnswerRequest(
            expected_version=ask_response.version, side="candidate", package=package, answer="accept"
        ),
    )
    assert answer_response.status == "active"

    body_policy = model_from_firestore(Policy, store._principal_ref(pid).get().to_dict()["policy"])
    assert len(body_policy.accept_anchors) == 1


def test_principal_answer_touching_a_removed_categorical_axis_is_never_saved_to_the_body(store):
    # DV-05 (3): 区分軸(training)を外した場合、P から作るアンカーは常に具体的な値(*
    # ではない)になるので、どんな回答でも本体には保存されない(§2.2: 区分軸の中立は * のみ)。
    pid, nid = _create_live_with_removed_axis(store, "training")
    package = sample_package(salary=650, training="available")

    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    answer_response = store.process_principal_answer(
        nid,
        PrincipalAnswerRequest(
            expected_version=ask_response.version, side="candidate", package=package, answer="accept"
        ),
    )
    assert answer_response.status == "active"

    body_policy = model_from_firestore(Policy, store._principal_ref(pid).get().to_dict()["policy"])
    assert body_policy.accept_anchors == []

    copy_policy = model_from_firestore(
        Policy, store._negotiation_ref(nid).get().to_dict()["snapshots"]["candidate"]
    )
    assert len(copy_policy.accept_anchors) == 1
