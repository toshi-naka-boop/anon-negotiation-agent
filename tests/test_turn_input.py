"""TurnInput の組み立て(design.md §2.7・§4.1 の 2・4)。

前半は、金庫の view・イベント列の値を直接与えて、組み立ての規則(history・last_error・last_invalid・
own_move_number・version を含めないこと、phase・checked)と、その側の見え方にすでにある評価の読み出し
(known_evaluations。§4.1 の 3)を確かめる。last_error・last_invalid は、エージェントの手(check 以外)についてだけ作り、
レフェリーの確かめ(有効・無効)は飛ばす(台帳 X-51)。後半は、本物の金庫(エミュレータ)を通して、
counterparty が側に応じて正しく入ること(台帳 I-2)と、history が相手側の確認手・途中確認・評価を
含まないこと、無効手の次の TurnInput に last_invalid(打とうとした手・組み合わせ・自分側の評価。台帳 C-40)が
入り、有効なエージェントの手の後は None になることを確かめる。
"""

import datetime as dt

import pytest
from negotiation_core import (
    AttackerTurnInput,
    Budget,
    CandidateAttributeBands,
    CheckedPackage,
    EvaluatedPackage,
    JobCategoryInfo,
    LastInvalid,
    TurnInput,
    Verdict,
)
from vault.api_models import EventViewItem, NegotiationViewResponse
from vault.models import EmployerRule
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    live_create_request,
    make_candidate_template,
    make_employer_template,
    needs_confirmation_policy,
    new_id,
    put_candidate_policy,
    reject_all_policy,
    sample_package,
)
from web.referee import StepOutcome
from web.turn_input import (
    build_history,
    build_last_error,
    build_last_invalid,
    build_turn_input,
    count_own_moves,
    known_evaluations,
    package_key,
)
from web_helpers import (
    ScriptedAnswerer,
    create_demo_negotiation,
    create_live_negotiation,
    drive,
    move_dict,
    plan_dict,
)


def _event(
    seq, kind, package=None, own_evaluation=None, reason=None, answer=None, attempted_move=None
) -> EventViewItem:
    return EventViewItem(
        seq=seq,
        kind=kind,
        package=package,
        own_evaluation=own_evaluation,
        reason=reason,
        answer=answer,
        attempted_move=attempted_move,
    )


def _view(**overrides) -> NegotiationViewResponse:
    values = dict(
        status="active",
        to_move="candidate",
        paused=False,
        counterparty=JobCategoryInfo(job_category="it_web"),
        pending_offer=None,
        last_check=None,
        awaiting_principal_package=None,
        budget=Budget(remaining_evaluations=17, remaining_moves=6, remaining_principal_checks=1),
        deadline=None,
        expires_at=dt.datetime(2026, 1, 4, tzinfo=dt.timezone.utc),
        version=7,
        result=None,
    )
    values.update(overrides)
    return NegotiationViewResponse(**values)


def _entries(history):
    """history を (by, move, salary, result) の並びにして比べやすくする(salary で組み合わせを区別)。"""
    return [(e.by, e.move, e.package.salary, e.result) for e in history]


# --- 組み立ての規則(金庫を通さない) ---


def test_history_holds_only_moves_visible_to_this_side_and_never_the_non_moves():
    # §2.7: history はイベント列のその側の見え方から作る。手だけを入れ、無効手・途中確認の回答・
    # 一時停止・再開・最終記録は入れない。result は「その組み合わせについての自分側の評価」。
    p1, p2, p3 = sample_package(salary=700), sample_package(salary=650), sample_package(salary=600)
    events = [
        _event(1, "check", p1, "needs_confirmation"),
        _event(2, "invalid", p2, reason="not_acceptable_to_own_principal"),
        _event(3, "pause"),
        _event(4, "resume"),
        _event(5, "propose", p1),
        _event(6, "offer_rejected", p1),
        _event(7, "offer_received", p3, "needs_confirmation"),
        _event(8, "ask_principal", p3),
        _event(9, "principal_answer", p3, "acceptable", answer="accept"),
        _event(10, "reject", p3),
    ]

    assert _entries(build_history(events)) == [
        ("self", "check", 700, Verdict.NEEDS_CONFIRMATION),
        ("self", "propose", 700, Verdict.ACCEPTABLE),  # ガードを通った提案は「受けられる」
        ("counterparty", "reject", 700, Verdict.ACCEPTABLE),  # 断られた自分の提案の評価
        ("counterparty", "propose", 600, Verdict.NEEDS_CONFIRMATION),  # 受け手としての自分側の評価
        ("self", "ask_principal", 600, Verdict.NEEDS_CONFIRMATION),
        ("self", "reject", 600, Verdict.NEEDS_CONFIRMATION),  # 断った提案を受け取ったときの評価
    ]


def test_history_of_a_fresh_negotiation_is_empty():
    # §2.7: まだ何も起きていない交渉の history は空(最初の手番の TurnInput)。
    assert build_history([]) == []


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ([], None),
        ([("check", None)], None),
        ([("check", None), ("invalid", "schema_invalid")], "schema_invalid"),
        # 一時停止・再開は手ではないので飛ばす(直前の手はまだ無効手のまま)。
        ([("invalid", "evaluation_budget_exhausted"), ("pause", None), ("resume", None)], "evaluation_budget_exhausted"),
        # 有効なエージェントの手を打てば、直前の手は無効ではない。
        ([("invalid", "schema_invalid"), ("propose", None)], None),
        # レフェリーの確かめ(check)は、エージェントの手ではないので飛ばす(台帳 X-51): 確かめが間に入っても、直前の無効手は残る。
        ([("invalid", "schema_invalid"), ("check", None)], "schema_invalid"),
        ([("invalid", "output_truncated"), ("check", None), ("check", None)], "output_truncated"),
        # 相手の手(提案の受領)は「直前の手」に数える: その前の自分の無効手は、もう直前ではない。
        ([("invalid", "agent_timeout"), ("propose", None), ("offer_rejected", None)], None),
    ],
)
def test_last_error_is_the_reason_of_the_most_recent_move_when_it_was_invalid(kinds, expected):
    # §2.7: last_error は「直前の自分の手が無効だった理由」。なければ None。
    package = sample_package()
    events = [
        _event(i + 1, kind, package if kind not in ("pause", "resume") else None, reason=reason)
        for i, (kind, reason) in enumerate(kinds)
    ]
    assert build_last_error(events) == expected


_P = sample_package(salary=650)
_Q = sample_package(salary=550, remote_days=1)


def _invalid(seq, reason, attempted_move=None, package=None, evaluation=None) -> EventViewItem:
    return _event(seq, "invalid", package, evaluation, reason=reason, attempted_move=attempted_move)


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([], None),
        ([_event(1, "check", _P, "needs_confirmation")], None),
        # ガードで断られた提案: 打とうとした手(propose)・組み合わせ・自分側の評価が入る。
        (
            [_invalid(1, "not_acceptable_to_own_principal", "propose", _P, "not_acceptable")],
            LastInvalid(move="propose", package=_P, evaluation=Verdict.NOT_ACCEPTABLE),
        ),
        (
            [_invalid(1, "not_acceptable_to_own_principal", "propose", _P, "needs_confirmation")],
            LastInvalid(move="propose", package=_P, evaluation=Verdict.NEEDS_CONFIRMATION),
        ),
        # 評価を使った、途中確認の無効手。
        (
            [_invalid(1, "question_not_applicable", "ask_principal", _P, "acceptable")],
            LastInvalid(move="ask_principal", package=_P, evaluation=Verdict.ACCEPTABLE),
        ),
        # 評価をしていない無効手(評価回数の尽き)は、手と組み合わせだけ。評価は None。
        (
            [_invalid(1, "evaluation_budget_exhausted", "propose", _P)],
            LastInvalid(move="propose", package=_P, evaluation=None),
        ),
        # レフェリーの確かめが、評価回数が尽きて無効になった(attempted_move=check)ものは、エージェントの手ではないので飛ばす。
        ([_invalid(1, "evaluation_budget_exhausted", "check", _P)], None),
        # 提案がないのに accept: 手だけ。組み合わせも評価もない。
        ([_invalid(1, "no_pending_offer", "accept")], LastInvalid(move="accept", package=None, evaluation=None)),
        # レフェリーが登録した無効手: 何を打とうとしたか金庫には分からないので、3 つとも None(last_invalid 自体は入る)。
        ([_invalid(1, "schema_invalid")], LastInvalid(move=None, package=None, evaluation=None)),
        ([_invalid(1, "agent_timeout")], LastInvalid(move=None, package=None, evaluation=None)),
        # 一時停止・再開・途中確認の回答は、手ではないので飛ばして探す(直前の手はまだ無効手のまま)。
        (
            [
                _invalid(1, "not_acceptable_to_own_principal", "propose", _P, "not_acceptable"),
                _event(2, "pause"),
                _event(3, "resume"),
            ],
            LastInvalid(move="propose", package=_P, evaluation=Verdict.NOT_ACCEPTABLE),
        ),
        # 有効なエージェントの手を打てば、直前の手は無効ではない。
        (
            [_invalid(1, "not_acceptable_to_own_principal", "propose", _P, "not_acceptable"), _event(2, "propose", _Q)],
            None,
        ),
        # レフェリーの確かめ(有効な check)は、エージェントの手ではないので飛ばす(台帳 X-51): 直前の無効手が残る。
        (
            [_invalid(1, "not_acceptable_to_own_principal", "propose", _P, "not_acceptable"), _event(2, "check", _Q, "acceptable")],
            LastInvalid(move="propose", package=_P, evaluation=Verdict.NOT_ACCEPTABLE),
        ),
        # 無効な確かめ(評価回数の尽き)と有効な確かめが混じっていても同じ。新しい無効手があれば、それが優先される。
        (
            [
                _invalid(1, "no_pending_offer", "accept"),
                _event(2, "check", _Q, "acceptable"),
                _invalid(3, "evaluation_budget_exhausted", "check", _Q),
            ],
            LastInvalid(move="accept", package=None, evaluation=None),
        ),
        # 相手の手(提案の受領)は「直前の手」に数える(last_error と同じ規則): その前の自分の無効手は、もう直前ではない。
        (
            [
                _invalid(1, "agent_timeout"),
                _event(2, "propose", _P),
                _event(3, "offer_rejected", _P),
            ],
            None,
        ),
        # 古い無効手より、新しい無効手が優先される。
        (
            [
                _invalid(1, "not_acceptable_to_own_principal", "propose", _P, "not_acceptable"),
                _invalid(2, "no_pending_offer", "reject"),
            ],
            LastInvalid(move="reject", package=None, evaluation=None),
        ),
    ],
    ids=[
        "no_events",
        "valid_check_only",
        "guard_rejected_proposal_not_acceptable",
        "guard_rejected_proposal_needs_confirmation",
        "question_not_applicable",
        "evaluation_budget_exhausted_proposal",
        "evaluation_budget_exhausted_referee_check_is_skipped",
        "accept_without_a_pending_offer",
        "referee_schema_invalid",
        "referee_agent_timeout",
        "skips_pause_and_resume",
        "valid_agent_move_after_it",
        "valid_referee_check_after_it_is_skipped",
        "invalid_and_valid_referee_checks_are_skipped",
        "counterparty_move_after_it",
        "newest_invalid_wins",
    ],
)
def test_last_invalid_is_the_content_of_the_most_recent_move_when_it_was_invalid(events, expected):
    # §2.7・台帳 C-40: last_invalid は「直前の自分の手が無効だったときの、打とうとした手の中身」。なければ None。
    # 「直前の手」の決め方は last_error と同じ。
    assert build_last_invalid(events) == expected
    assert (build_last_error(events) is not None) == (expected is not None)  # last_error と、あるなしがいつも揃う


def test_the_turn_input_carries_last_invalid_next_to_last_error():
    # C-40: build_turn_input が、last_invalid を TurnInput に詰める。線の上の形(JSON)にも、そのまま出る。
    events = [_invalid(1, "not_acceptable_to_own_principal", "propose", _P, "not_acceptable")]

    turn_input = build_turn_input(side="candidate", view=_view(), events=events, phase="plan")

    assert turn_input.last_error == "not_acceptable_to_own_principal"
    assert turn_input.last_invalid == LastInvalid(move="propose", package=_P, evaluation=Verdict.NOT_ACCEPTABLE)
    assert turn_input.model_dump(mode="json", by_alias=True)["last_invalid"] == {
        "move": "propose",
        "package": _P.model_dump(mode="json"),
        "evaluation": "not_acceptable",
    }


def test_own_move_number_counts_the_moves_the_agent_made_and_not_the_referees_checks():
    # 台帳 L15-2: エージェント自身が出した手(提案・断る・途中確認・無効手)の数。相手の手・回答・一時停止は数えない。
    # レフェリーが計画の中で登録した確かめ(check の記録)は、エージェントの手ではないので数えない。
    package = sample_package()
    events = [
        _event(1, "check", package, "acceptable"),  # レフェリーの確かめ
        _event(2, "invalid", package, reason="schema_invalid"),
        _event(3, "propose", package),
        _event(4, "offer_rejected", package),  # 相手の手
        _event(5, "offer_received", package, "acceptable"),  # 相手の手
        _event(6, "ask_principal", package),
        _event(7, "principal_answer", package, "acceptable", answer="accept"),  # 依頼者の回答
        _event(8, "pause"),
        _event(9, "resume"),
        _event(10, "reject", package),
    ]
    assert count_own_moves(events) == 4


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ([], 0),
        # 確かめだけの履歴は、エージェントの手が 0。何回入っても数えない。
        ([("check", None, None)], 0),
        ([("check", None, None)] * 3, 0),
        # 確かめが、エージェントの手の前・後・間のどこに混ざっても、数は変わらない。
        ([("propose", None, None)], 1),
        ([("check", None, None), ("propose", None, None)], 1),
        ([("propose", None, None), ("check", None, None)], 1),
        (
            [
                ("check", None, None),
                ("propose", None, None),
                ("offer_rejected", None, None),  # 相手の手
                ("check", None, None),
                ("check", None, None),
                ("reject", None, None),
            ],
            2,
        ),
        # 評価回数が尽きて無効になった確かめ(attempted_move が check の無効手)も、レフェリーの確かめ。
        ([("invalid", "evaluation_budget_exhausted", "check")], 0),
        ([("check", None, None), ("invalid", "evaluation_budget_exhausted", "check"), ("propose", None, None)], 1),
        # エージェントの無効手は、数える(打とうとした手が check でないとき。レフェリーが登録した無効手は、何を打とうとしたか None)。
        ([("invalid", "schema_invalid", None)], 1),
        ([("invalid", "evaluation_budget_exhausted", "propose")], 1),
        ([("invalid", "schema_invalid", None), ("invalid", "evaluation_budget_exhausted", "check")], 1),
    ],
    ids=[
        "no_events",
        "a_check_only",
        "three_checks_only",
        "a_proposal",
        "check_before_a_proposal",
        "check_after_a_proposal",
        "checks_between_the_agents_moves",
        "an_invalid_check_only",
        "valid_and_invalid_checks_around_a_proposal",
        "an_invalid_move_registered_by_the_referee",
        "an_invalid_proposal",
        "an_invalid_move_and_an_invalid_check",
    ],
)
def test_the_referees_checks_never_change_own_move_number(kinds, expected):
    # 台帳 L15-2: 確かめが混ざった履歴で、数が変わらない(有効な check も、評価回数が尽きて無効になった check も)。
    # TurnInput の own_move_number も同じ数になる。
    package = sample_package()
    events = [
        _event(
            seq,
            kind,
            package,
            "acceptable" if kind in ("check", "offer_received") else None,
            reason=reason,
            attempted_move=attempted_move,
        )
        for seq, (kind, reason, attempted_move) in enumerate(kinds, start=1)
    ]

    assert count_own_moves(events) == expected
    assert build_turn_input(side="candidate", view=_view(), events=events, phase="plan").own_move_number == expected


def test_turn_input_carries_the_views_own_side_values_and_never_the_version():
    # DV-10(部品): TurnInput に view の version は現れない。相手の残り回数を表す項目も、そもそもない。
    package = sample_package()
    pending = EvaluatedPackage(package=package, own_evaluation=Verdict.NEEDS_CONFIRMATION)
    view = _view(pending_offer=pending, last_check=pending, version=12345)
    turn_input = build_turn_input(
        side="candidate", view=view, events=[_event(1, "check", package, "needs_confirmation")], phase="plan"
    )

    assert isinstance(turn_input, TurnInput)
    assert turn_input.pending_offer == pending
    assert turn_input.last_check == pending
    assert turn_input.budget == view.budget

    dumped = turn_input.model_dump(mode="json", by_alias=True)
    assert set(dumped) == {
        "schema",
        "side",
        "own_move_number",
        "counterparty",
        "history",
        "pending_offer",
        "last_check",
        "last_error",
        "last_invalid",  # 直前の無効手の中身(台帳 C-40)。この入力の直前の手は有効な確認手なので None
        "budget",
        "phase",  # 呼び出しの種類(plan・decide。§4.1)
        "checked",  # その手番で確かめた結果(plan では空)
    }
    assert (dumped["phase"], dumped["checked"]) == ("plan", [])
    assert dumped["last_invalid"] is None
    assert "12345" not in str(dumped)  # version の値がどこにも入っていない
    assert set(dumped["budget"]) == {"remaining_evaluations", "remaining_moves", "remaining_principal_checks"}


def test_the_decide_turn_input_carries_the_checked_results_in_the_order_of_the_plan():
    # §2.7: phase=decide の TurnInput は、その手番で確かめた結果 checked(計画の順。確かめなかった案は evaluation が null)を持つ。
    # plan では checked は空。線の上の形(JSON)にも、そのまま出る。
    p, q, r = sample_package(salary=700), sample_package(salary=650), sample_package(salary=600)
    checked = [
        CheckedPackage(package=p, evaluation=Verdict.NOT_ACCEPTABLE),
        CheckedPackage(package=q, evaluation=Verdict.ACCEPTABLE),
        CheckedPackage(package=r, evaluation=None),
    ]

    decide = build_turn_input(side="candidate", view=_view(), events=[], phase="decide", checked=checked)
    plan = build_turn_input(side="candidate", view=_view(), events=[], phase="plan")

    assert decide.phase == "decide" and decide.checked == checked
    assert plan.phase == "plan" and plan.checked == []
    dumped = decide.model_dump(mode="json", by_alias=True)["checked"]
    assert [(item["package"]["salary"], item["evaluation"]) for item in dumped] == [
        (700, "not_acceptable"),
        (650, "acceptable"),
        (600, None),
    ]


def test_known_evaluations_come_from_the_history_records_of_this_side():
    # §4.1 の 3: その側の見え方(イベント列)にすでにある評価。確かめ・相手の提案の受領は記録された評価、自分の提案は「受けられる」
    # (ガードを通った)、自分の途中確認は「本人確認が必要」。断る手・無効手・相手の断りは、組み合わせの評価を持たない。
    p1, p2, p3, p4, p5 = (sample_package(salary=s) for s in (900, 800, 700, 600, 500))
    events = [
        _event(1, "check", p1, "not_acceptable"),
        _event(2, "offer_received", p2, "needs_confirmation"),
        _event(3, "propose", p3),
        _event(4, "ask_principal", p4),
        _event(5, "reject", p2),
        _event(6, "invalid", p5, "not_acceptable", reason="not_acceptable_to_own_principal", attempted_move="propose"),
        _event(7, "offer_rejected", p3),
    ]

    known = known_evaluations(view=_view(), events=events)

    assert known == {
        package_key(p1): Verdict.NOT_ACCEPTABLE,
        package_key(p2): Verdict.NEEDS_CONFIRMATION,  # 後に自分側の途中確認の回答がないので、記録のまま
        package_key(p3): Verdict.ACCEPTABLE,
        package_key(p4): Verdict.NEEDS_CONFIRMATION,
    }


def test_a_needs_confirmation_record_is_dropped_only_when_this_side_answered_a_question_after_it():
    # §4.1 の 3・台帳 C-43・C-48: 「受けられる」「受けられない」は変わらないので、回答の後でも埋まる。「本人確認が必要」の記録は、
    # その後に自分側の途中確認の回答(principal_answer)があるときだけ、含めない(確かめ直す。回答で広がり得るため)。
    # 回答より後の記録は残る。
    p1, p2, p3, p4 = (sample_package(salary=s) for s in (900, 800, 700, 600))
    events = [
        _event(1, "check", p1, "needs_confirmation"),
        _event(2, "check", p2, "acceptable"),
        _event(3, "check", p3, "not_acceptable"),
        _event(4, "ask_principal", p4),
        _event(5, "principal_answer", p4, "acceptable", answer="accept"),
        _event(6, "check", p1, "acceptable"),  # 回答の後に確かめ直した結果は、新しい記録
    ]
    assert known_evaluations(view=_view(), events=events[:4]) == {
        package_key(p1): Verdict.NEEDS_CONFIRMATION,
        package_key(p2): Verdict.ACCEPTABLE,
        package_key(p3): Verdict.NOT_ACCEPTABLE,
        package_key(p4): Verdict.NEEDS_CONFIRMATION,
    }
    # 回答の直後(5 まで): 「本人確認が必要」の p1・p4 は含めない。ほかは残る。
    assert known_evaluations(view=_view(), events=events[:5]) == {
        package_key(p2): Verdict.ACCEPTABLE,
        package_key(p3): Verdict.NOT_ACCEPTABLE,
    }
    # 回答の後の確かめ(6)は、回答より後の記録なので残る。
    assert known_evaluations(view=_view(), events=events) == {
        package_key(p1): Verdict.ACCEPTABLE,
        package_key(p2): Verdict.ACCEPTABLE,
        package_key(p3): Verdict.NOT_ACCEPTABLE,
    }


def test_the_views_reevaluated_values_take_priority_over_the_history():
    # §4.1 の 3・台帳 L14-2: view の pending_offer・last_check は、途中確認の回答のたびに評価し直されているので、履歴の古い記録より
    # 優先する(回答で「本人確認が必要」から「受けられる」に変わった組み合わせを、古い記録で埋めない)。
    p1, p2 = sample_package(salary=700), sample_package(salary=650)
    events = [
        _event(1, "check", p1, "needs_confirmation"),
        _event(2, "offer_received", p2, "needs_confirmation"),
        _event(3, "ask_principal", p2),
        _event(4, "principal_answer", p2, "acceptable", answer="accept"),
    ]
    view = _view(
        last_check=EvaluatedPackage(package=p1, own_evaluation=Verdict.ACCEPTABLE),
        pending_offer=EvaluatedPackage(package=p2, own_evaluation=Verdict.ACCEPTABLE),
    )

    assert known_evaluations(view=view, events=events) == {
        package_key(p1): Verdict.ACCEPTABLE,
        package_key(p2): Verdict.ACCEPTABLE,
    }

    # 履歴に回答より後の「本人確認が必要」の記録が残っていても(含められる記録)、view の値が優先される(見え方どうしが食い違うときの、
    # 優先順位そのものを固定する)。
    p3 = sample_package(salary=600)
    later = [*events, _event(5, "check", p3, "needs_confirmation")]
    view = _view(last_check=EvaluatedPackage(package=p3, own_evaluation=Verdict.ACCEPTABLE))
    assert known_evaluations(view=view, events=later)[package_key(p3)] is Verdict.ACCEPTABLE


# --- 本物の金庫を通す ---


@pytest.mark.anyio
async def test_counterparty_is_job_category_info_for_the_candidate_and_bands_for_the_employer(
    store, vault_client
):
    # TurnInput.counterparty(§2.7): 候補者側には公開求人の区分情報、求人側には候補者の属性帯。
    bands = CandidateAttributeBands(experience_band="5_to_10y", region_block="kinki", job_category="sales")
    nid = create_demo_negotiation(store, attribute_bands=bands)
    # 求人テンプレートの区分情報は、既定(その他)のまま: 金庫が返す値がそのまま入る。
    candidate_view = await vault_client.get_view(nid, "candidate")
    employer_view = await vault_client.get_view(nid, "employer")
    candidate_input = build_turn_input(side="candidate", view=candidate_view, events=[], phase="plan")
    employer_input = build_turn_input(side="employer", view=employer_view, events=[], phase="plan")

    assert candidate_input.counterparty == JobCategoryInfo(job_category="other")
    assert employer_input.counterparty == bands
    assert candidate_input.side == "candidate"
    assert employer_input.side == "employer"


@pytest.mark.anyio
async def test_job_category_info_comes_from_the_employer_template(store, vault_client):
    # 公開求人の区分情報は、求人テンプレートに置いた値が交渉の作成時に写され、候補者側の view に出る。
    candidate_template = make_candidate_template()
    employer_template = make_employer_template()
    employer_template.job_category_info = JobCategoryInfo(job_category="medical_welfare")
    put_template(store._db, candidate_template)
    put_template(store._db, employer_template)
    created = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )

    candidate_view = await vault_client.get_view(created.nid, "candidate")
    assert candidate_view.counterparty == JobCategoryInfo(job_category="medical_welfare")


@pytest.mark.anyio
async def test_real_candidates_bands_reach_the_employer_from_the_vault(store, vault_client):
    # 台帳 I-2: 本物の候補者の属性帯は、ポリシーと一緒に金庫に保存したものを金庫が読む。求人側の
    # TurnInput.counterparty は、金庫が返したその帯になる(web は帯を持たず、渡さない)。
    bands = CandidateAttributeBands(
        experience_band="10y_plus", region_block="kyushu_okinawa", job_category="administration"
    )
    pid = new_id("principal")
    put_candidate_policy(store, pid, attribute_bands=bands)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    created = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert created.status == "created"

    employer_view = await vault_client.get_view(created.nid, "employer")
    turn_input = build_turn_input(side="employer", view=employer_view, events=[], phase="plan")
    assert turn_input.counterparty == bands


@pytest.mark.anyio
async def test_history_excludes_the_counterpartys_checks_principal_questions_and_evaluations(store, web_env):
    # §2.7 / DV-10: history は、相手側の確認手・途中確認・評価を含まない。
    # 候補者が確かめて提案し、求人側が(候補者に見えない)確かめ・途中確認を経て断る。その後の候補者の TurnInput.history には、
    # 相手の断りだけが載り、相手の確かめ・途中確認・回答・評価は現れない。求人側の TurnInput.history にも、候補者の確かめは
    # 現れない。確かめ(金庫の check)は、レフェリーが計画の checks を実行して登録する(history には self・check として入る)。
    env = web_env
    env.configure(answerer=ScriptedAnswerer("reject"))
    p1 = sample_package()
    p2 = sample_package(salary=600)
    nid = create_demo_negotiation(
        store, employer_rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))]
    )
    env.agents.script("candidate", plan_dict(checks=[p1]), move_dict("propose", p1), move_dict("end"))
    env.agents.script("employer", plan_dict(checks=[p2]), move_dict("ask_principal", p1), move_dict("reject"))

    outcomes = await drive(env.referee(nid))

    assert outcomes == [
        StepOutcome.MOVED,  # 候補者 確かめ(p1)→ propose
        StepOutcome.MOVED,  # 求人側 確かめ(p2)→ ask_principal
        StepOutcome.ANSWERED,  # 架空人物の自動回答(受けない)
        StepOutcome.MOVED,  # 求人側 reject
        StepOutcome.FINISHED,  # 候補者 end
    ]

    candidate_last = env.agents.calls_for("candidate")[2].turn_input  # 2 手番目の計画
    assert _entries(candidate_last.history) == [
        ("self", "check", 700, Verdict.ACCEPTABLE),
        ("self", "propose", 700, Verdict.ACCEPTABLE),
        ("counterparty", "reject", 700, Verdict.ACCEPTABLE),
    ]
    # 相手の確かめ・途中確認は、by=counterparty の手として現れない。
    assert {(e.by, e.move) for e in candidate_last.history if e.by == "counterparty"} == {("counterparty", "reject")}
    assert candidate_last.own_move_number == 1  # 自分の手は propose だけ(check はレフェリーの確かめ。台帳 L15-2)
    # 自分側の残りだけが見える: 求人側が途中確認を使っても、候補者の残りは減らない。
    assert candidate_last.budget.remaining_principal_checks == 1

    employer_last = env.agents.calls_for("employer")[2].turn_input  # 2 手番目の計画
    assert _entries(employer_last.history) == [
        ("counterparty", "propose", 700, Verdict.NEEDS_CONFIRMATION),
        ("self", "check", 600, Verdict.NEEDS_CONFIRMATION),
        ("self", "ask_principal", 700, Verdict.NEEDS_CONFIRMATION),
    ]
    assert employer_last.own_move_number == 1  # 自分の手は ask_principal だけ(自分の check はレフェリーの確かめ。相手の手も数えない)
    assert employer_last.budget.remaining_principal_checks == 0


@pytest.mark.anyio
async def test_live_negotiation_turn_inputs_use_the_saved_bands_and_the_default_job_info(store, web_env):
    # 本物の候補者の交渉でも、両側の TurnInput に counterparty が入る(候補者側: 区分情報、求人側: 帯)。
    env = web_env
    nid, pid = create_live_negotiation(store, candidate_policy=accept_all_policy("candidate"))
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("employer", move_dict("accept"))

    await drive(env.referee(nid, mode="live", candidate_principal_id=pid))

    candidate_call = env.agents.calls_for("candidate")[0]
    employer_call = env.agents.calls_for("employer")[0]
    assert isinstance(candidate_call.turn_input.counterparty, JobCategoryInfo)
    assert isinstance(employer_call.turn_input.counterparty, CandidateAttributeBands)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("policy_factory", "expected_evaluation"),
    [(reject_all_policy, Verdict.NOT_ACCEPTABLE), (needs_confirmation_policy, Verdict.NEEDS_CONFIRMATION)],
    ids=["not_acceptable", "needs_confirmation"],
)
async def test_the_turn_input_after_a_guard_rejected_proposal_carries_the_move_the_package_and_the_evaluation(
    store, web_env, policy_factory, expected_evaluation
):
    # 台帳 C-40・X-51: ガードで断られた提案(無効手。評価 1 消費)の次の TurnInput に、その手(propose)・組み合わせ・自分側の
    # 評価が入る。状態を持たないエージェントが「同じ手を繰り返さない」ための情報。次の手番の計画にも、そこで確かめを挟んだ
    # 同じ手番の決定にも入る(レフェリーの確かめは、エージェントの手ではないので、これを消さない)。
    env = web_env
    package = sample_package()
    other = sample_package(salary=600)
    nid = create_demo_negotiation(store, candidate_policy=policy_factory("candidate"))
    env.agents.script("candidate", move_dict("propose", package), plan_dict(checks=[other]), move_dict("end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED  # propose: ガードで断られた(無効手)
    assert await referee.step() is StepOutcome.FINISHED  # 計画(確かめ)→ 決定(end)

    first, second_plan, second_decide = [call.turn_input for call in env.agents.calls_for("candidate")]
    assert (first.phase, second_plan.phase, second_decide.phase) == ("plan", "plan", "decide")
    assert (first.last_error, first.last_invalid) == (None, None)
    expected_invalid = LastInvalid(move="propose", package=package, evaluation=expected_evaluation)
    assert second_plan.last_error == "not_acceptable_to_own_principal"
    assert second_plan.last_invalid == expected_invalid
    assert second_plan.budget.remaining_evaluations == 16  # ガードの評価は消費されている(17 → 16)
    # 確かめを挟んだ同じ手番の決定にも、残っている。確かめで評価を 1 使い、決定の入力は読み直した view から作られる(16 → 15)。
    assert second_decide.last_error == "not_acceptable_to_own_principal"
    assert second_decide.last_invalid == expected_invalid
    assert second_decide.budget.remaining_evaluations == 15
    assert [e.kind for e in store.get_events(nid, "candidate")][:2] == ["invalid", "check"]


@pytest.mark.anyio
async def test_the_turn_input_after_each_kind_of_invalid_move_carries_what_the_vault_knows_about_it(store, web_env):
    # 台帳 C-40: 無効手の種類ごとに、入るものが違う。金庫で分かる無効手は手の種類(と、あれば組み合わせ・評価)、
    # レフェリーが登録した無効手(スキーマ違反・応答なし)は、3 つとも None。どの場合も、続く有効なエージェントの手
    # (提案。相手が断って手番が戻る)で None に戻る。(同じ側の無効手が 3 回続くと交渉が終わるので、有効な手を挟む)
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store, candidate_policy=accept_all_policy("candidate"))
    broken = {"schema": "move/v1", "move": "withdraw"}  # 列挙外の手(スキーマ違反)
    env.agents.script(
        "candidate",
        move_dict("ask_principal", package),  # 受けられる組み合わせへの途中確認(question_not_applicable)
        move_dict("accept"),  # 提案がないのに accept(no_pending_offer)
        move_dict("propose", package),  # 有効な手(相手が断る)
        broken,  # レフェリーが見つけた無効手(schema_invalid)
        move_dict("end"),
    )
    env.agents.script("employer", move_dict("reject"))
    referee = env.referee(nid)

    for _ in range(6):
        await referee.step()

    turn_inputs = [call.turn_input for call in env.agents.calls_for("candidate")]
    assert [t.last_error for t in turn_inputs] == [
        None,
        "question_not_applicable",
        "no_pending_offer",
        None,  # 提案(有効)と相手の断りの後
        "schema_invalid",
    ]
    assert [t.last_invalid for t in turn_inputs] == [
        None,
        LastInvalid(move="ask_principal", package=package, evaluation=Verdict.ACCEPTABLE),
        LastInvalid(move="accept", package=None, evaluation=None),
        None,
        LastInvalid(move=None, package=None, evaluation=None),
    ]


@pytest.mark.anyio
async def test_the_other_side_never_sees_an_invalid_move_in_its_turn_input(store, web_env):
    # 台帳 C-40: last_invalid は、自分側だけの情報。候補者の無効手は、次の求人側の TurnInput に出ない(相手への漏れは増えない)。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store, candidate_policy=accept_all_policy("candidate"))
    env.agents.script("candidate", {"schema": "move/v1", "move": "withdraw"}, move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))

    await drive(env.referee(nid))

    (employer_input,) = [call.turn_input for call in env.agents.calls_for("employer")]
    assert (employer_input.last_error, employer_input.last_invalid) == (None, None)
    assert [e.kind for e in store.get_events(nid, "employer")][:1] == ["offer_received"]  # 相手には、無効手の記録も見えない


@pytest.mark.anyio
async def test_the_attack_mode_employer_also_receives_last_invalid(store, web_env):
    # 台帳 C-40: 攻撃モードの求人エージェント(AttackerTurnInput)にも、TurnInput と同じ last_invalid が渡る
    # (principal_instruction を足すときに、落とさない)。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store, mode="attack", employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("attacker", move_dict("ask_principal", package), move_dict("accept"))  # 受けられる組み合わせへの途中確認 → 無効手
    referee = env.referee(nid, mode="attack")

    await drive(referee)

    first, second = [call.turn_input for call in env.agents.calls_for("attacker")]
    assert isinstance(second, AttackerTurnInput)
    assert first.last_invalid is None
    assert second.last_invalid == LastInvalid(move="ask_principal", package=package, evaluation=Verdict.ACCEPTABLE)
