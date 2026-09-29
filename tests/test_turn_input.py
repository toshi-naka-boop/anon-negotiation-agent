"""TurnInput の組み立て(design.md §2.7・§4.1 の 1)。

前半は、金庫の view・イベント列の値を直接与えて、組み立ての規則(history・last_error・
own_move_number・version を含めないこと)だけを確かめる。後半は、本物の金庫(エミュレータ)を通して、
counterparty が側に応じて正しく入ること(台帳 I-2)と、history が相手側の確認手・途中確認・評価を
含まないことを確かめる。
"""

import datetime as dt

import pytest
from negotiation_core import (
    Budget,
    CandidateAttributeBands,
    EvaluatedPackage,
    JobCategoryInfo,
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
    sample_package,
)
from web.referee import StepOutcome
from web.turn_input import build_history, build_last_error, build_turn_input, count_own_moves
from web_helpers import (
    ScriptedAnswerer,
    create_demo_negotiation,
    create_live_negotiation,
    drive,
    move_dict,
)


def _event(seq, kind, package=None, own_evaluation=None, reason=None, answer=None) -> EventViewItem:
    return EventViewItem(
        seq=seq, kind=kind, package=package, own_evaluation=own_evaluation, reason=reason, answer=answer
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
        "budget",
    }
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
