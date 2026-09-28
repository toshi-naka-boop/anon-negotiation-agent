"""DV-05: removing an axis from consideration (design.md §2.4, §12.2).

DV-05 の 1 つ目(外した軸があっても、全 18,000 通りで「受けられない」が「受けられる」に
変わらない)と 2 つ目(代表的な面談フィクスチャで離散軸を 1 つ外しても、受けるアンカーが
1 件以上残る)を確かめる。3 つ目(外した軸に触れる発言・中立でない途中確認の回答が本体に
保存されない)は、交渉の状態機械(vault)が要る後の段で足す。

sample_data.principal_decision は、negotiation_core の Anchor/Policy/evaluate を一切
使わない独立した「本人のモデル」。これを ground truth にすることで、単に
「アンカーを足して同じ evaluate() で確かめる」というトートロジーを避ける。
"""

import pytest

from negotiation_core.policy import Verdict, evaluate, iter_all_packages

from sample_data import (
    DISCRETE_AXES_TO_REMOVE,
    build_policy_from_interview,
    principal_decision,
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
