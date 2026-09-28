"""DV-10: イベント列の側ごとの見え方(design.md §3.2)。principal-answer の行と、
TurnInput・画面・レフェリーの作り直しは後の段(1b-2 以降)。

§3.2 の表のとおりに操作ごとの見え方が分かれていること、propose で相手の評価が
どちらの見え方にも入らないこと、相手に見えない操作の後でも相手の seq が飛ばずに
続くこと、見え方と view に相手の残り回数が現れず version は view にだけ入ること、
最終記録が双方に 1 件だけであることを確かめる。
"""

from vault.api_models import MoveRequest
from vault_helpers import demo_create_request, put_candidate_and_employer_templates, sample_package


def _create(store):
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def test_check_is_visible_only_to_the_mover(store):
    # §3.2 の表: check(P) は操作した側にだけ見える(相手の見え方は「なし」)。
    nid = _create(store)
    response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )
    assert response.valid is True

    candidate_events = store.get_events(nid, "candidate")
    employer_events = store.get_events(nid, "employer")
    assert len(candidate_events) == 1
    assert candidate_events[0].kind == "check"
    assert len(employer_events) == 0  # 相手には何も見えない


def test_ask_principal_is_visible_only_to_the_mover(store):
    # §3.2 の表: ask_principal(P) も操作した側にだけ見える。
    from vault.models import EmployerRule
    from vault_helpers import accept_all_policy, needs_confirmation_policy

    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db,
        candidate_policy=needs_confirmation_policy("candidate"),
        employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))],
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    nid = result.nid

    response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=sample_package())
    )
    assert response.valid is True
    assert response.status == "awaiting_principal"

    candidate_events = store.get_events(nid, "candidate")
    employer_events = store.get_events(nid, "employer")
    assert len(candidate_events) == 1
    assert candidate_events[0].kind == "ask_principal"
    assert len(employer_events) == 0


def test_invalid_move_is_visible_only_to_the_mover(store):
    # §3.2 の表: 無効手(ガードで拒否された propose を含む)は操作した側にだけ見える。
    from vault_helpers import reject_all_policy

    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=reject_all_policy("candidate")
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    nid = result.nid

    response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=sample_package())
    )
    assert response.valid is False

    candidate_events = store.get_events(nid, "candidate")
    employer_events = store.get_events(nid, "employer")
    assert len(candidate_events) == 1
    assert candidate_events[0].kind == "invalid"
    assert len(employer_events) == 0


def test_propose_is_visible_to_both_sides_but_receiver_evaluation_never_reaches_the_proposer(store):
    # DV-10: propose で、相手の評価はどちらの見え方にも入らない。
    # 提案した側(candidate)の見え方には own_evaluation が一切現れない(自分の提案 P だけ)。
    # 受け手(employer)の見え方には、受け手「自身」の評価(自分側の評価)が入るが、
    # これは提案した側には一切届かない。
    nid = _create(store)
    package = sample_package()
    response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    assert response.valid is True

    candidate_events = store.get_events(nid, "candidate")
    employer_events = store.get_events(nid, "employer")

    assert len(candidate_events) == 1
    assert candidate_events[0].kind == "propose"
    assert candidate_events[0].package == package
    assert candidate_events[0].own_evaluation is None  # 相手(受け手)の評価は提案者に見えない

    assert len(employer_events) == 1
    assert employer_events[0].kind == "offer_received"
    assert employer_events[0].package == package
    assert employer_events[0].own_evaluation is not None  # 受け手「自身」の評価は自分には見える


def test_reject_is_visible_to_both_sides_with_different_payloads(store):
    # §3.2 の表: reject は双方に見えるが、内容が異なる(断った側/断られた側)。
    nid = _create(store)
    package = sample_package()
    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    reject_response = store.process_move(
        nid, MoveRequest(expected_version=propose_response.version, side="employer", move="reject")
    )
    assert reject_response.valid is True

    candidate_events = store.get_events(nid, "candidate")
    employer_events = store.get_events(nid, "employer")

    assert candidate_events[-1].kind == "offer_rejected"  # 提案した側:断られたことが見える
    assert employer_events[-1].kind == "reject"  # 断った側:断ったことが見える
    assert candidate_events[-1].package == package
    assert employer_events[-1].package == package


def test_counterparty_seq_does_not_skip_across_invisible_operations(store):
    # DV-10: 相手に見えない操作(check など)の後でも、相手の seq は飛ばずに続く。
    nid = _create(store)
    package = sample_package()

    # candidate が check を 2 回(employer には一切見えない)。
    v = 0
    for _ in range(2):
        response = store.process_move(
            nid, MoveRequest(expected_version=v, side="candidate", move="check", package=package)
        )
        v = response.version

    # candidate が propose(employer にも見える最初の記録)。
    response = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=package)
    )
    v = response.version

    employer_events = store.get_events(nid, "employer")
    assert len(employer_events) == 1
    assert employer_events[0].seq == 1  # 2 と 3 ではなく、1 から始まる(飛びがない)

    # employer が reject。次に見える employer 自身の記録の seq が 2 になる(連番)。
    response = store.process_move(nid, MoveRequest(expected_version=v, side="employer", move="reject"))
    v = response.version
    employer_events = store.get_events(nid, "employer")
    assert [e.seq for e in employer_events] == [1, 2]


def test_view_and_events_never_expose_the_counterparty_remaining_budget(store):
    # DV-10: 見え方と view に、相手の残り回数が現れない(version は view にだけ入る)。
    nid = _create(store)
    package = sample_package()
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package))

    candidate_view = store.get_view(nid, "candidate")
    employer_view = store.get_view(nid, "employer")

    # view には自分側の budget しかなく、相手の残り回数を表す項目は存在しない。
    assert set(candidate_view.model_dump().keys()) == {
        "status",
        "to_move",
        "paused",
        "pending_offer",
        "last_check",
        "awaiting_principal_package",
        "budget",
        "deadline",
        "expires_at",
        "version",
        "result",
    }
    assert candidate_view.budget.remaining_evaluations == 15  # 自分(candidate)の残りだけ
    assert employer_view.budget.remaining_evaluations == 16  # 相手はまだ 1 回も使っていない

    # イベントの見え方(EventViewItem)には version も budget も、そもそも項目自体がない。
    events = store.get_events(nid, "candidate")
    for event in events:
        assert set(event.model_dump().keys()) == {"seq", "kind", "package", "own_evaluation", "reason", "result"}


def test_exactly_one_final_result_record_per_side(store):
    # DV-10: 最終記録は双方に 1 件だけ。
    nid = _create(store)
    package = sample_package()
    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    accept_response = store.process_move(
        nid, MoveRequest(expected_version=propose_response.version, side="employer", move="accept")
    )
    assert accept_response.status == "judged"

    for side in ("candidate", "employer"):
        events = store.get_events(nid, side)
        final_events = [e for e in events if e.kind == "final_result"]
        assert len(final_events) == 1
