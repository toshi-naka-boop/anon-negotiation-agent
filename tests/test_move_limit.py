"""DV-13: 手数の上限(design.md §3.5・§12.2)。

片方が最後の手で提案しても、残りがある相手は答えられること。手番が回ってきた側の
残りが 0 のときだけ終わること。3 回目の連続無効手で、無効手と終了が別々の version の
2 件になることを確かめる。

手数に数えないのは、有効な check と有効な ask_principal(どちらも手番を渡さない。台帳 C-38 の決定)。
無効な check・無効な ask_principal は、ほかの無効手と同じく数える。最後の 1 手で途中確認を打っても、
「受ける」の回答の後に accept できることを確かめる。
"""

from negotiation_core import Anchor, Policy

from vault.api_models import MoveRequest, PrincipalAnswerRequest
from vault.models import EmployerRule
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    needs_confirmation_policy,
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

    # 評価回数(17)を使い切るまで、有効な check を繰り返す。手数は 1 つも減らない。
    for _ in range(17):
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="check", package=package)
        )
        assert response.valid is True
        version = response.version

    view = store.get_view(nid, "candidate")
    assert view.budget.remaining_evaluations == 0
    assert view.budget.remaining_moves == 6  # 有効な check はここまで 1 つも数えていない

    # 18 回目の check は無効(evaluation_budget_exhausted)。これは手数を 1 進める。
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


def test_valid_ask_principal_does_not_count_toward_moves_but_invalid_ones_do(store):
    # DV-13 / 台帳 C-38(決定: 案 1): 有効な ask_principal は、有効な check と同じく手数に数えない
    # (どちらも手番を渡さない)。無効な ask_principal は、ほかの無効手と同じく数える。
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    asked_package = sample_package(salary=650)
    other_package = sample_package(salary=550, remote_days=1)

    asked = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=asked_package)
    )
    assert (asked.valid, asked.status) == (True, "awaiting_principal")
    assert store.get_view(nid, "candidate").budget.remaining_moves == 6  # 有効な ask_principal は数えない

    answered = store.process_principal_answer(
        nid,
        PrincipalAnswerRequest(
            expected_version=asked.version, side="candidate", package=asked_package, answer="accept"
        ),
    )
    assert store.get_view(nid, "candidate").budget.remaining_moves == 6  # 回答も数えない

    # 上限(側ごとに 1)を超える 2 回目の途中確認は無効(question_budget_exhausted)。数える。
    exhausted = store.process_move(
        nid,
        MoveRequest(expected_version=answered.version, side="candidate", move="ask_principal", package=other_package),
    )
    assert (exhausted.valid, exhausted.error) == (False, "question_budget_exhausted")
    assert store.get_view(nid, "candidate").budget.remaining_moves == 5

    # すでに「受けられる」組み合わせへの途中確認も無効(question_not_applicable)。数える。
    not_applicable = store.process_move(
        nid,
        MoveRequest(expected_version=exhausted.version, side="candidate", move="ask_principal", package=asked_package),
    )
    assert (not_applicable.valid, not_applicable.error) == (False, "question_not_applicable")
    assert store.get_view(nid, "candidate").budget.remaining_moves == 4


def test_a_question_asked_with_the_last_move_can_still_be_answered_and_accepted(store):
    # 台帳 C-38 の破綻シナリオ: 手数を 5 使った後に、相手の提案について途中確認 → 「受ける」の回答 → accept。
    # 以前は、途中確認が 6 手目に数えられ、回答の後の accept が、処理の前の停止の判定(stopped_budget)で
    # 「なし」になっていた。今は、途中確認が手数に数えられないので、6 手目の accept で合意になる。
    # 候補者は、年収 900 万以上なら受ける。求人側は何でも受ける。
    candidate_policy = Policy(
        side="candidate",
        accept_anchors=[
            Anchor(salary=900, remote_days=0, night_duty=8, review_months=12, training="*", side_job="*", start="*")
        ],
        reject_anchors=[],
    )
    nid = _create(store, candidate_policy=candidate_policy, employer_rules=[_wildcard_rule("employer")])
    own_offer = sample_package(salary=900)  # 候補者が受けられる提案
    offer_to_candidate = sample_package(salary=600)  # 候補者には「本人確認が必要」な、求人側の提案
    version = 0

    for _ in range(4):  # 候補者の提案 → 求人側の断り、を 4 回
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=own_offer)
        )
        version = response.version
        response = store.process_move(nid, MoveRequest(expected_version=version, side="employer", move="reject"))
        version = response.version
    response = store.process_move(  # 候補者の 5 手目
        nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=own_offer)
    )
    version = response.version
    response = store.process_move(  # 求人側が、自分の案を出し直す
        nid, MoveRequest(expected_version=version, side="employer", move="propose", package=offer_to_candidate)
    )
    version = response.version
    view = store.get_view(nid, "candidate")
    assert view.budget.remaining_moves == 1  # 候補者は 5 手使った
    assert view.pending_offer.own_evaluation.value == "needs_confirmation"

    asked = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="ask_principal", package=offer_to_candidate)
    )
    assert (asked.valid, asked.status) == (True, "awaiting_principal")

    answered = store.process_principal_answer(
        nid,
        PrincipalAnswerRequest(
            expected_version=asked.version, side="candidate", package=offer_to_candidate, answer="accept"
        ),
    )
    assert store.get_view(nid, "candidate").pending_offer.own_evaluation.value == "acceptable"

    accepted = store.process_move(nid, MoveRequest(expected_version=answered.version, side="candidate", move="accept"))

    assert (accepted.valid, accepted.status, accepted.end_reason) == (True, "judged", "agreed")
    for side in ("candidate", "employer"):
        (final,) = [e for e in store.get_events(nid, side) if e.kind == "final_result"]
        assert final.result.likelihood in ("high", "medium")
        assert final.result.package == offer_to_candidate


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
