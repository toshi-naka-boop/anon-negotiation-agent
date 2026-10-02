"""DV-10: イベント列の側ごとの見え方(design.md §3.2)。

金庫の部分: §3.2 の表のとおりに操作ごとの見え方が分かれていること、propose で相手の評価が
どちらの見え方にも入らないこと、相手に見えない操作(確認・途中確認・無効手・一時停止・再開)の後でも
相手の seq が飛ばずに 1 から連番で続くこと(一時停止・再開・無効手は、台帳 C-39 の (c) で足した確認)、
一時停止・再開の見え方が操作した側だけであること、見え方と view に相手の残り回数が現れず version は
view にだけ入ること、最終記録が双方に 1 件だけであること、principal-answer の行が答えた側の見え方にだけ
入り相手の seq を飛ばさないことを確かめる。

web の部分(末尾。1d-1): TurnInput に version と相手の残り回数が現れないこと、レフェリーを金庫の操作の
直後で止めて作り直しても、TurnInput.history に記録の欠けも重複もないこと。画面の部分は 1d-2 以降。
"""

import pytest
from negotiation_core import Verdict
from vault.api_models import ControlRequest, MoveRequest
from vault_helpers import demo_create_request, put_candidate_and_employer_templates, sample_package
from web_helpers import CrashAfterMove, SimulatedCrash, create_demo_negotiation, drive, move_dict


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


def test_pause_resume_and_invalid_records_are_visible_only_to_their_own_side_and_never_break_the_others_seq(store):
    # DV-10 / 台帳 C-39 の (c): 相手に見えない操作(一時停止・再開・無効手)の後でも、相手の seq は飛ばずに
    # 1 から連番で続く。一時停止・再開の見え方は、操作した側だけ(§3.2 の表)。
    # 候補者の一時停止・再開・無効手のそれぞれの後に、求人側に見える記録(候補者の提案 → 求人側の断り)を起こし、
    # 続けて求人側も同じ操作をして、候補者側に見える記録を起こす。どちらの側の seq も、1 から連番になる。
    nid = _create(store)
    package = sample_package()

    def seqs(side):
        return [event.seq for event in store.get_events(nid, side)]

    def kinds(side):
        return [event.kind for event in store.get_events(nid, side)]

    # --- 候補者の、求人側に見えない操作 ---
    paused = store.control(nid, ControlRequest(side="candidate", action="pause"))
    assert (kinds("candidate"), kinds("employer")) == (["pause"], [])  # 自分だけに見える
    resumed = store.control(nid, ControlRequest(side="candidate", action="resume"))
    assert (kinds("candidate"), kinds("employer")) == (["pause", "resume"], [])
    invalid = store.process_move(
        nid, MoveRequest(expected_version=resumed.version, side="candidate", move="invalid", reason="schema_invalid")
    )
    assert invalid.valid is False
    assert (kinds("candidate"), kinds("employer")) == (["pause", "resume", "invalid"], [])
    assert paused.version < resumed.version < invalid.version

    # --- 求人側に見える記録(1 件目)。求人側の seq は 1 から始まる(候補者の 3 件で飛ばない) ---
    proposed = store.process_move(
        nid, MoveRequest(expected_version=invalid.version, side="candidate", move="propose", package=package)
    )
    assert proposed.valid is True
    assert seqs("employer") == [1]
    assert seqs("candidate") == [1, 2, 3, 4]

    # --- 求人側の、候補者に見えない操作 ---
    store.control(nid, ControlRequest(side="employer", action="pause"))
    assert (kinds("employer"), seqs("candidate")) == (["offer_received", "pause"], [1, 2, 3, 4])  # 候補者の記録は増えない
    resumed = store.control(nid, ControlRequest(side="employer", action="resume"))
    assert (kinds("employer"), seqs("candidate")) == (["offer_received", "pause", "resume"], [1, 2, 3, 4])
    invalid = store.process_move(
        nid, MoveRequest(expected_version=resumed.version, side="employer", move="invalid", reason="agent_timeout")
    )
    assert invalid.valid is False
    assert (seqs("employer"), seqs("candidate")) == ([1, 2, 3, 4], [1, 2, 3, 4])

    # --- 候補者に見える記録(求人側の断り)。候補者の seq は 5 で続く(求人側の 3 件で飛ばない) ---
    store.process_move(nid, MoveRequest(expected_version=invalid.version, side="employer", move="reject"))
    assert seqs("candidate") == [1, 2, 3, 4, 5]
    assert seqs("employer") == [1, 2, 3, 4, 5]
    assert kinds("candidate") == ["pause", "resume", "invalid", "propose", "offer_rejected"]
    assert kinds("employer") == ["offer_received", "pause", "resume", "invalid", "reject"]


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
        "counterparty",  # TurnInput.counterparty の元(1d-1 で追加。相手の属性帯・公開求人の区分情報)
        "pending_offer",
        "last_check",
        "awaiting_principal_package",
        "budget",
        "deadline",
        "expires_at",
        "version",
        "result",
    }
    assert candidate_view.budget.remaining_evaluations == 16  # 自分(candidate)の残りだけ
    assert employer_view.budget.remaining_evaluations == 17  # 相手はまだ 1 回も使っていない

    # イベントの見え方(EventViewItem)には version も budget も、そもそも項目自体がない。
    events = store.get_events(nid, "candidate")
    for event in events:
        assert set(event.model_dump().keys()) == {
            "seq",
            "kind",
            "package",
            "own_evaluation",
            "reason",
            "answer",
            "result",
            "attempted_move",  # 無効手だけが持つ、打とうとした手の種類(台帳 C-40)
        }


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


def test_principal_answer_is_visible_only_to_the_answering_side_and_keeps_the_counterpartys_seq_unbroken(store):
    # DV-10: principal-answer の記録は、答えた側の見え方にだけ入り、相手には一切見えない
    # (§3.2 の表)。相手に見えない操作(ask_principal・principal-answer)を挟んでも、
    # 次に相手に見える操作の seq は飛ばずに続く。
    from negotiation_core import Anchor, Policy
    from vault.api_models import PrincipalAnswerRequest
    from vault.models import EmployerRule
    from vault_helpers import accept_all_policy

    package_a = sample_package(salary=700)
    package_b = sample_package(salary=650)  # ask_principal・principal-answer で使う組み合わせ
    # candidate 自身の propose(package_a)のガードは通しつつ、package_b は NEEDS_CONFIRMATION の
    # ままにしたいので、package_a とちょうど同じアンカー 1 件だけを持つポリシーにする
    # (salary が違う package_b は、このアンカーを満たさない)。
    candidate_policy = Policy(
        side="candidate", accept_anchors=[Anchor(**package_a.model_dump(mode="python"))], reject_anchors=[]
    )
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db,
        candidate_policy=candidate_policy,
        employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))],
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    nid = result.nid

    # 求人側にも見える操作を 1 往復させ、employer の seq を 2 にしておく(propose → reject)。
    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package_a)
    )
    v = propose_response.version
    reject_response = store.process_move(nid, MoveRequest(expected_version=v, side="employer", move="reject"))
    v = reject_response.version
    assert [e.seq for e in store.get_events(nid, "employer")] == [1, 2]

    # candidate が途中確認 → 回答(どちらも employer には一切見えない)。
    ask_response = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="ask_principal", package=package_b)
    )
    v = ask_response.version
    answer_response = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=v, side="candidate", package=package_b, answer="accept")
    )
    assert answer_response.status == "active"
    v = answer_response.version

    # 答えた側(candidate)には principal_answer の記録が見える。
    candidate_events = store.get_events(nid, "candidate")
    assert candidate_events[-1].kind == "principal_answer"
    assert candidate_events[-1].package == package_b
    assert candidate_events[-1].answer == "accept"

    # 相手(employer)には一切見えない: イベント数は変わらず、seq も飛ばずに 1・2 のまま。
    assert [e.seq for e in store.get_events(nid, "employer")] == [1, 2]

    # 次に employer に見える操作が起きれば、seq は 3 から続く(飛びがない)。
    final_propose = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=package_b)
    )
    assert final_propose.valid is True
    assert [e.seq for e in store.get_events(nid, "employer")] == [1, 2, 3]


# --- web(レフェリー)の部分(1d-1) ---


def _all_keys(value) -> set[str]:
    """JSON 相当の値に現れる、すべての辞書のキー。"""
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


@pytest.mark.anyio
async def test_turn_input_shows_neither_the_version_nor_the_counterpartys_remaining_budget(store, web_env):
    # DV-10: TurnInput に、version と相手の残り回数が現れない。budget は自分側の残りだけで、
    # 相手が評価・手数を使っても、自分の TurnInput の残りは減らない。
    env = web_env
    p1 = sample_package()
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", move_dict("check", p1), move_dict("check", p1), move_dict("propose", p1))
    env.agents.script("employer", move_dict("check", p1), move_dict("accept"))

    await drive(env.referee(nid))

    def budget(call):
        b = call.turn_input.budget
        return (b.remaining_evaluations, b.remaining_moves, b.remaining_principal_checks)

    candidate_calls = env.agents.calls_for("candidate")
    employer_calls = env.agents.calls_for("employer")
    # 候補者: check ×2(評価 2 回。有効な確認手は手数に数えない)→ propose のガード(評価 1 回)
    assert [budget(c) for c in candidate_calls] == [(16, 6, 1), (15, 6, 1), (14, 6, 1)]
    # 求人側: 候補者が評価を 3 回使った後でも、求人側は自分の分だけ(16)。check の後は 15。
    assert [budget(c) for c in employer_calls] == [(16, 6, 1), (15, 6, 1)]

    # 自分の手の数(own_move_number)は、金庫の通し番号(version)とは別物: 求人側の 1 回目は、候補者の
    # 記録が 3 件あって version は 3 だが、自分の手は 0 回。
    assert [c.turn_input.own_move_number for c in candidate_calls] == [0, 1, 2]
    assert [c.turn_input.own_move_number for c in employer_calls] == [0, 1]

    for call in env.agents.calls:
        dumped = call.turn_input.model_dump(mode="json", by_alias=True)
        keys = _all_keys(dumped)
        assert "version" not in keys and "nid" not in keys  # 金庫の通し番号も、ID も入っていない
        assert set(dumped["budget"]) == {"remaining_evaluations", "remaining_moves", "remaining_principal_checks"}


@pytest.mark.anyio
@pytest.mark.parametrize("crash_on_move_number", [1, 2])
async def test_recreated_referee_leaves_no_gap_or_duplicate_in_history_after_a_crash_right_after_a_vault_operation(
    store, web_env, vault_client, crash_on_move_number
):
    # DV-10: レフェリーを金庫の操作の直後で止めてから作り直しても、TurnInput.history に記録の欠けも重複もない。
    # 1 回目は候補者の確認手、2 回目は候補者の提案が、金庫にコミットされた直後に落ちる。web は記録を写さず、
    # 履歴を金庫のイベント列から読み直すので、どちらでも同じ履歴になる。
    env = web_env
    p1, p2 = sample_package(salary=700), sample_package(salary=650)
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", move_dict("check", p1), move_dict("propose", p1), move_dict("propose", p2))
    env.agents.script("employer", move_dict("reject"), move_dict("accept"))

    await env.restart(vault=CrashAfterMove(env.vault, crash_on_move_number=crash_on_move_number))
    with pytest.raises(SimulatedCrash):
        await drive(env.referee(nid))
    assert len(store.get_events(nid, "candidate")) == crash_on_move_number  # 落ちる前の操作は、金庫に残っている

    await env.restart(vault=vault_client)  # web を作り直す
    await drive(env.referee(nid))

    def history(call):
        return [(e.by, e.move, e.package.salary, e.result) for e in call.turn_input.history]

    candidate_calls = env.agents.calls_for("candidate")
    employer_calls = env.agents.calls_for("employer")
    assert len(candidate_calls) == 3 and len(employer_calls) == 2  # 呼び出しの数は、落ちた位置に依らない
    assert history(candidate_calls[0]) == []
    assert history(candidate_calls[1]) == [("self", "check", 700, Verdict.ACCEPTABLE)]  # 確認手は 1 件だけ
    assert history(employer_calls[0]) == [("counterparty", "propose", 700, Verdict.ACCEPTABLE)]  # 提案は 1 件だけ
    assert history(candidate_calls[2]) == [
        ("self", "check", 700, Verdict.ACCEPTABLE),
        ("self", "propose", 700, Verdict.ACCEPTABLE),
        ("counterparty", "reject", 700, Verdict.ACCEPTABLE),
    ]
    assert [e.kind for e in store.get_events(nid, "candidate")] == [
        "check",
        "propose",
        "offer_rejected",
        "propose",
        "final_result",
    ]
    assert store.get_view(nid, "candidate").status == "judged"
