"""DV-13: 手数の上限(design.md §3.5・§12.2)。

片方が最後の手で提案しても、残りがある相手は答えられること。手番が回ってきた側の
残りが 0 のときだけ終わること。3 回目の連続無効手で、無効手と終了が別々の version の
2 件になることを確かめる。
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


def _wildcard_rule(side):
    return EmployerRule(when={}, policy=accept_all_policy(side))


def _create(store, candidate_policy=None, employer_rules=None):
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=candidate_policy, employer_rules=employer_rules
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def test_valid_check_does_not_count_toward_moves_but_invalid_check_does(store):
    # DV-13 / 差し戻し対応 1(§3.5): 「手数(check を除く。無効手を含む)」の「check を
    # 除く」は有効な check のことと読む。無効な check(evaluation_budget_exhausted)は、
    # 他の無効手と同じく手数を 1 進める。
    nid = _create(store)
    package = sample_package()
    version = 0

    # 評価回数(16)を使い切るまで、有効な check を繰り返す。手数は 1 つも減らない。
    for _ in range(16):
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="check", package=package)
        )
        assert response.valid is True
        version = response.version

    view = store.get_view(nid, "candidate")
    assert view.budget.remaining_evaluations == 0
    assert view.budget.remaining_moves == 6  # 有効な check はここまで 1 つも数えていない

    # 17 回目の check は無効(evaluation_budget_exhausted)。これは手数を 1 進める。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="check", package=package)
    )
    assert response.valid is False
    assert response.error == "evaluation_budget_exhausted"
    assert response.status == "active"
    version = response.version

    view_after_invalid_check = store.get_view(nid, "candidate")
    assert view_after_invalid_check.budget.remaining_moves == 5  # 無効な check は数える

    # もう 1 回無効な check を送っても、連続無効手はまだ上限(3)に届かないので終わらない。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="check", package=package)
    )
    assert response.valid is False
    assert response.status == "active"
    view_after_second_invalid_check = store.get_view(nid, "candidate")
    assert view_after_second_invalid_check.budget.remaining_moves == 4


def test_responder_with_remaining_moves_can_answer_the_last_proposal(store):
    # DV-13: 片方の側(candidate)が最後の手(6 手目)で提案したとき、相手(employer)は
    # 自分の残りがあれば accept で答えられる(手数を使い切った側の直後でも終わらない)。
    nid = _create(store, employer_rules=[_wildcard_rule("employer")])
    version = 0

    # 5 回、候補者が提案して求人側が断る、を繰り返す(双方の手数を 5 まで使う)。
    for _ in range(5):
        response = store.process_move(
            nid,
            MoveRequest(
                expected_version=version, side="candidate", move="propose", package=sample_package()
            ),
        )
        assert response.valid is True
        version = response.version
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="employer", move="reject")
        )
        assert response.valid is True
        version = response.version

    # 候補者の 6 回目(最後)の提案。これで候補者の手数は 6/6 になる。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
    )
    assert response.valid is True
    assert response.status == "active"  # まだ終わらない(求人側の手番)
    version = response.version

    # 求人側はまだ手数が残っている(5/6)ので、accept で答えられる。
    response = store.process_move(nid, MoveRequest(expected_version=version, side="employer", move="accept"))
    assert response.valid is True
    assert response.status == "judged"
    assert response.end_reason == "agreed"


def test_negotiation_ends_only_when_the_zero_remaining_side_is_given_the_turn(store):
    # DV-13: 手番が回ってきた側の残りが 0 のときだけ終わる(候補者が使い切った直後の
    # 求人側の手番では終わらず、手番が候補者に戻ってきたところで初めて終わる)。
    nid = _create(store, employer_rules=[_wildcard_rule("employer")])
    version = 0

    for _ in range(6):
        response = store.process_move(
            nid,
            MoveRequest(
                expected_version=version, side="candidate", move="propose", package=sample_package()
            ),
        )
        assert response.valid is True
        version = response.version
        if response.status == "judged":
            break
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="employer", move="reject")
        )
        assert response.valid is True
        version = response.version

    # ここまでで、候補者は 6 回とも使い切っている。手番は候補者に戻っているはず。
    view = store.get_view(nid, "candidate")
    assert view.status == "active"
    assert view.to_move == "candidate"
    assert view.budget.remaining_moves == 0

    # 候補者に手番が回ってきた時点で、手を出す前に終了処理(stopped_budget)が働く。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
    )
    assert response.status == "judged"
    assert response.end_reason == "stopped_budget"


def test_third_consecutive_invalid_move_writes_two_separate_version_records(store):
    # DV-13: 3 回目の連続無効手で終わるとき、無効手の記録と終了の記録が別々の version で
    # 2 件残る。
    nid = _create(store, candidate_policy=reject_all_policy("candidate"))
    version = 0

    for _ in range(2):
        response = store.process_move(
            nid,
            MoveRequest(
                expected_version=version, side="candidate", move="propose", package=sample_package()
            ),
        )
        assert response.valid is False
        assert response.error == "not_acceptable_to_own_principal"
        assert response.status == "active"
        assert response.version == version + 1  # 無効手 1 件だけ
        version = response.version

    version_before_third = version
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
    )
    assert response.valid is False
    assert response.status == "judged"
    assert response.end_reason == "stopped_invalid"
    # 無効手の記録(version+1)と終了の記録(version+2)の 2 件、別々の version で残る。
    assert response.version == version_before_third + 2

    events = list(store._events(nid).order_by("version").stream())
    versions = [e.to_dict()["version"] for e in events]
    assert versions == sorted(set(versions))  # 重複がない
    assert version_before_third + 1 in versions
    assert version_before_third + 2 in versions

    last_two = [e.to_dict() for e in events[-2:]]
    assert last_two[0]["views"]["candidate"]["kind"] == "invalid"
    assert last_two[1]["views"]["candidate"]["kind"] == "final_result"
