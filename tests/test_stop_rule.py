"""AC-06: 停止の判定(design.md §3.5 FR-12)。

停止の判定関数がポリシーを引数に取らないこと(構造的な確認)。「手と金庫の応答」の並びが
同じなら、ポリシーを差し替えても同じ手番で止まること。交渉を作り直しても依頼者ごとの
予算は回復しないことを確かめる。
"""

import inspect

from negotiation_core import Anchor, Policy

from vault.api_models import ControlRequest, MoveRequest
from vault.stop_rule import determine_stop_reason
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    live_create_request,
    make_employer_template,
    new_id,
    put_candidate_and_employer_templates,
    put_candidate_policy,
    sample_package,
)
from vault.templates import put_template


def test_stop_rule_function_takes_no_policy_argument():
    # AC-06: 停止の判定関数がポリシーを引数に取らない(シグネチャそのものを確かめる)。
    signature = inspect.signature(determine_stop_reason)
    parameter_names = set(signature.parameters.keys())
    assert parameter_names == {
        "moves_used",
        "consecutive_invalid",
        "moves_budget",
        "consecutive_invalid_limit",
    }
    for name, parameter in signature.parameters.items():
        assert "policy" not in name.lower()
        assert parameter.annotation in (int, inspect.Parameter.empty) or "Policy" not in str(
            parameter.annotation
        )


def test_stop_rule_is_a_pure_function_of_counters():
    # AC-06 の言い換え: 同じ counters・上限なら、呼ぶたびに同じ結果になる(ポリシーはおろか、
    # どんな追加の文脈も参照しない)。
    assert determine_stop_reason(
        moves_used=6, consecutive_invalid=0, moves_budget=6, consecutive_invalid_limit=3
    ) == "stopped_budget"
    assert determine_stop_reason(
        moves_used=0, consecutive_invalid=3, moves_budget=6, consecutive_invalid_limit=3
    ) == "stopped_invalid"
    assert (
        determine_stop_reason(moves_used=5, consecutive_invalid=2, moves_budget=6, consecutive_invalid_limit=3)
        is None
    )


def test_same_move_sequence_stops_at_the_same_turn_regardless_of_policy(store):
    # AC-06: 「手と金庫の応答」の並びが同じなら、ポリシーを差し替えても同じ手番で止まる。
    # ここでは、2 つの異なるポリシー(アンカーの中身は違うが、テストで使う package は
    # どちらでも ACCEPTABLE になる)のもとで、同じ手の並び(6 回連続 propose/reject)を
    # 流し、どちらも候補者の 7 回目の手番(手数 6/6)で stopped_budget により止まることを
    # 確かめる。
    package = sample_package()

    policy_a = accept_all_policy("candidate")  # 何でも受ける
    anchor = Anchor(
        salary=package.salary,
        remote_days=package.remote_days,
        night_duty=package.night_duty,
        review_months=package.review_months,
        training="*",
        side_job="*",
        start="*",
    )
    policy_b = Policy(side="candidate", accept_anchors=[anchor], reject_anchors=[])  # この package だけ受ける

    results = []
    for candidate_policy in (policy_a, policy_b):
        candidate_template, employer_template = put_candidate_and_employer_templates(
            store._db, candidate_policy=candidate_policy
        )
        created = store.create_negotiation(
            demo_create_request(candidate_template.template_id, employer_template.template_id)
        )
        nid = created.nid
        version = 0
        last_response = None
        for _ in range(6):
            response = store.process_move(
                nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=package)
            )
            assert response.valid is True
            version = response.version
            response = store.process_move(
                nid, MoveRequest(expected_version=version, side="employer", move="reject")
            )
            assert response.valid is True
            version = response.version
        # 候補者の 7 回目: 手数を使い切っているので、手を処理する前に stopped_budget で終わる。
        last_response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=package)
        )
        results.append((last_response.status, last_response.end_reason, last_response.version))

    assert results[0] == results[1]  # 同じ手番(同じ version)・同じ理由で止まる
    assert results[0][0] == "judged"
    assert results[0][1] == "stopped_budget"


def test_recreating_a_negotiation_does_not_restore_the_daily_budget(store):
    # AC-06: 交渉を作り直しても依頼者ごとの予算は回復しない。
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    employer_template_1 = make_employer_template()
    put_template(store._db, employer_template_1)
    first = store.create_negotiation(live_create_request(pid, employer_template_1.template_id))
    assert first.status == "created"
    used_after_first = store._principal_ref(pid).get().to_dict()["evaluation_budget"]["used"]

    store.control(first.nid, ControlRequest(side="candidate", action="cancel"))

    employer_template_2 = make_employer_template()
    put_template(store._db, employer_template_2)
    second = store.create_negotiation(live_create_request(pid, employer_template_2.template_id))
    assert second.status == "created"
    used_after_second = store._principal_ref(pid).get().to_dict()["evaluation_budget"]["used"]

    assert used_after_second == used_after_first * 2  # 積み増されるだけで、戻らない
