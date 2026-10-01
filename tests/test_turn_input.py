"""TurnInput の組み立て(design.md §2.7・§4.1 の 1)。

前半は、金庫の view・イベント列の値を直接与えて、組み立ての規則(history・last_error・last_invalid・
own_move_number・version を含めないこと)だけを確かめる。後半は、本物の金庫(エミュレータ)を通して、
counterparty が側に応じて正しく入ること(台帳 I-2)と、history が相手側の確認手・途中確認・評価を
含まないこと、無効手の次の TurnInput に last_invalid(打とうとした手・組み合わせ・自分側の評価。台帳 C-40)が
入り、有効な手の後は None になることを確かめる。
"""

import datetime as dt

import pytest
from negotiation_core import (
    AttackerTurnInput,
    Budget,
    CandidateAttributeBands,
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
from web.turn_input import build_history, build_last_error, build_last_invalid, build_turn_input, count_own_moves
from web_helpers import (
    ScriptedAnswerer,
    create_demo_negotiation,
    create_live_negotiation,
    drive,
    move_dict,
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
        budget=Budget(remaining_evaluations=16, remaining_moves=6, remaining_principal_checks=1),
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
        # 有効な手を打てば、直前の手は無効ではない。
        ([("invalid", "schema_invalid"), ("check", None)], None),
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
            [_invalid(1, "evaluation_budget_exhausted", "check", _P)],
            LastInvalid(move="check", package=_P, evaluation=None),
        ),
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
        # 有効な手を打てば、直前の手は無効ではない。
        (
            [_invalid(1, "not_acceptable_to_own_principal", "propose", _P, "not_acceptable"), _event(2, "check", _Q, "acceptable")],
            None,
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
        "evaluation_budget_exhausted",
        "accept_without_a_pending_offer",
        "referee_schema_invalid",
        "referee_agent_timeout",
        "skips_pause_and_resume",
        "valid_move_after_it",
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

    turn_input = build_turn_input(side="candidate", view=_view(), events=events)

    assert turn_input.last_error == "not_acceptable_to_own_principal"
    assert turn_input.last_invalid == LastInvalid(move="propose", package=_P, evaluation=Verdict.NOT_ACCEPTABLE)
    assert turn_input.model_dump(mode="json", by_alias=True)["last_invalid"] == {
        "move": "propose",
        "package": _P.model_dump(mode="json"),
        "evaluation": "not_acceptable",
    }


def test_own_move_number_counts_the_moves_this_side_made():
    # 自分が打った手(確認手・提案・断る・途中確認・無効手)の数。相手の手・回答・一時停止は数えない。
    package = sample_package()
    events = [
        _event(1, "check", package, "acceptable"),
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
    assert count_own_moves(events) == 5


def test_turn_input_carries_the_views_own_side_values_and_never_the_version():
    # DV-10(部品): TurnInput に view の version は現れない。相手の残り回数を表す項目も、そもそもない。
    package = sample_package()
    pending = EvaluatedPackage(package=package, own_evaluation=Verdict.NEEDS_CONFIRMATION)
    view = _view(pending_offer=pending, last_check=pending, version=12345)
    turn_input = build_turn_input(side="candidate", view=view, events=[_event(1, "check", package, "needs_confirmation")])

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
    }
    assert dumped["last_invalid"] is None
    assert "12345" not in str(dumped)  # version の値がどこにも入っていない
    assert set(dumped["budget"]) == {"remaining_evaluations", "remaining_moves", "remaining_principal_checks"}


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
    candidate_input = build_turn_input(side="candidate", view=candidate_view, events=[])
    employer_input = build_turn_input(side="employer", view=employer_view, events=[])

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
    turn_input = build_turn_input(side="employer", view=employer_view, events=[])
    assert turn_input.counterparty == bands


@pytest.mark.anyio
async def test_history_excludes_the_counterpartys_checks_principal_questions_and_evaluations(store, web_env):
    # §2.7 / DV-10: history は、相手側の確認手・途中確認・評価を含まない。
    # 候補者が確認して提案し、求人側が(候補者に見えない)確認・途中確認を経て断る。その後の
    # 候補者の TurnInput.history には、相手の提案の受領と断りだけが載り、相手の確認手・途中確認・
    # 回答・評価は現れない。求人側の TurnInput.history にも、候補者の確認手は現れない。
    env = web_env
    env.configure(answerer=ScriptedAnswerer("reject"))
    p1 = sample_package()
    nid = create_demo_negotiation(
        store, employer_rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))]
    )
    env.agents.script("candidate", move_dict("check", p1), move_dict("propose", p1), move_dict("end"))
    env.agents.script("employer", move_dict("check", p1), move_dict("ask_principal", p1), move_dict("reject"))

    outcomes = await drive(env.referee(nid))

    assert outcomes == [
        StepOutcome.MOVED,  # 候補者 check
        StepOutcome.MOVED,  # 候補者 propose
        StepOutcome.MOVED,  # 求人側 check
        StepOutcome.MOVED,  # 求人側 ask_principal
        StepOutcome.ANSWERED,  # 架空人物の自動回答(受けない)
        StepOutcome.MOVED,  # 求人側 reject
        StepOutcome.FINISHED,  # 候補者 end
    ]

    candidate_last = env.agents.calls_for("candidate")[2].turn_input
    assert _entries(candidate_last.history) == [
        ("self", "check", 700, Verdict.ACCEPTABLE),
        ("self", "propose", 700, Verdict.ACCEPTABLE),
        ("counterparty", "reject", 700, Verdict.ACCEPTABLE),
    ]
    # 相手の確認手・途中確認は、by=counterparty の手として現れない。
    assert {(e.by, e.move) for e in candidate_last.history if e.by == "counterparty"} == {("counterparty", "reject")}
    assert candidate_last.own_move_number == 2  # 自分の手は check と propose
    # 自分側の残りだけが見える: 求人側が途中確認を使っても、候補者の残りは減らない。
    assert candidate_last.budget.remaining_principal_checks == 1

    employer_last = env.agents.calls_for("employer")[2].turn_input
    assert _entries(employer_last.history) == [
        ("counterparty", "propose", 700, Verdict.NEEDS_CONFIRMATION),
        ("self", "check", 700, Verdict.NEEDS_CONFIRMATION),
        ("self", "ask_principal", 700, Verdict.NEEDS_CONFIRMATION),
    ]
    assert employer_last.own_move_number == 2  # 候補者の check(相手の手)は数えない
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
    # 台帳 C-40: ガードで断られた提案(無効手。評価 1 消費)の次の TurnInput に、その手(propose)・組み合わせ・自分側の
    # 評価が入る。状態を持たないエージェントが「同じ手を繰り返さない」ための情報。有効な手(確認手)の後は None に戻る。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store, candidate_policy=policy_factory("candidate"))
    env.agents.script("candidate", move_dict("propose", package), move_dict("check", package), move_dict("end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED  # propose: ガードで断られた(無効手)
    assert await referee.step() is StepOutcome.MOVED  # check: 有効な手
    assert await referee.step() is StepOutcome.FINISHED  # end

    first, second, third = [call.turn_input for call in env.agents.calls_for("candidate")]
    assert (first.last_error, first.last_invalid) == (None, None)
    assert second.last_error == "not_acceptable_to_own_principal"
    assert second.last_invalid == LastInvalid(move="propose", package=package, evaluation=expected_evaluation)
    assert second.budget.remaining_evaluations == 15  # ガードの評価は消費されている
    assert (third.last_error, third.last_invalid) == (None, None)  # 有効な手の後は null


@pytest.mark.anyio
async def test_the_turn_input_after_each_kind_of_invalid_move_carries_what_the_vault_knows_about_it(store, web_env):
    # 台帳 C-40: 無効手の種類ごとに、入るものが違う。金庫で分かる無効手は手の種類(と、あれば組み合わせ・評価)、
    # レフェリーが登録した無効手(スキーマ違反・応答なし)は、3 つとも None。どの場合も、続く有効な手で None に戻る。
    # (同じ側の無効手が 3 回続くと交渉が終わるので、有効な確認手を挟む)
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store, candidate_policy=accept_all_policy("candidate"))
    broken = {"schema": "move/v1", "move": "withdraw"}  # 列挙外の手(スキーマ違反)
    env.agents.script(
        "candidate",
        move_dict("ask_principal", package),  # 受けられる組み合わせへの途中確認(question_not_applicable)
        move_dict("accept"),  # 提案がないのに accept(no_pending_offer)
        move_dict("check", package),  # 有効な手
        broken,  # レフェリーが見つけた無効手(schema_invalid)
        move_dict("check", package),  # 有効な手
        move_dict("end"),
    )
    referee = env.referee(nid)

    for _ in range(6):
        await referee.step()

    turn_inputs = [call.turn_input for call in env.agents.calls_for("candidate")]
    assert [t.last_error for t in turn_inputs] == [
        None,
        "question_not_applicable",
        "no_pending_offer",
        None,
        "schema_invalid",
        None,
    ]
    assert [t.last_invalid for t in turn_inputs] == [
        None,
        LastInvalid(move="ask_principal", package=package, evaluation=Verdict.ACCEPTABLE),
        LastInvalid(move="accept", package=None, evaluation=None),
        None,
        LastInvalid(move=None, package=None, evaluation=None),
        None,
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
