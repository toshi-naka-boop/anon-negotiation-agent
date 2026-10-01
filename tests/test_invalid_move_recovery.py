"""DV-03: 無効手の扱いと回復(design.md §3.5・§4.1)。

金庫の部分: 無効手(invalid の登録と、金庫で分かる無効手)が記録されること、ガードで拒否された
提案も評価回数を消費すること、受け手としての評価・accept の確かめ直し・判定は評価回数を
消費しないこと、相手が提案を重ねても自分の評価上限を超えないこと、同じ側の無効手が
3 回続いたときだけ終わることを確かめる。

金庫の無効手の見え方(台帳 C-40): 無効手を打った側の見え方に、打とうとした手の種類(attempted_move)と、
自分側のポリシーで評価して無効と判断した手なら、その自分側の評価(own_evaluation)が入ること。相手には見えないこと。
レフェリーが登録した無効手(schema_invalid・agent_timeout)は、手の種類も分からないので入らないこと。

web(レフェリー)の部分(末尾。1d-1): 台本のエージェントのスキーマ違反が invalid として登録され、
次の TurnInput.last_error に理由が入り、直した手で交渉が続くこと。一時的なエラー
(ConnectionError・TimeoutError)は再試行されて無効手にならず、再試行を使い切ったら agent_timeout に
なること。ValueError(受信口が拒否した)は再試行せず schema_invalid にすること。
"""

from dataclasses import replace

import pytest
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
from web.config import DEFAULT_WEB_CONFIG
from web.referee import StepOutcome
from web_helpers import Blocker, FakeSleep, create_demo_negotiation, drive, move_dict


def _create(store, candidate_policy=None, employer_rules=None):
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=candidate_policy, employer_rules=employer_rules
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def test_referee_registered_invalid_move_is_recorded(store):
    # DV-03: レフェリーが見つけた無効手(schema_invalid・agent_timeout)の登録が記録される。
    nid = _create(store)
    response = store.process_move(
        nid,
        MoveRequest(expected_version=0, side="candidate", move="invalid", reason="schema_invalid"),
    )
    assert response.valid is False
    assert response.error == "schema_invalid"

    events = store.get_events(nid, "candidate")
    assert len(events) == 1
    assert events[0].kind == "invalid"
    assert events[0].reason == "schema_invalid"


def test_vault_detected_invalid_move_is_recorded(store):
    # DV-03: 金庫で分かる無効手(例: pending_offer がないのに accept)も記録される。
    nid = _create(store)
    response = store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="accept"))
    assert response.valid is False
    assert response.error == "no_pending_offer"

    events = store.get_events(nid, "candidate")
    assert events[-1].kind == "invalid"
    assert events[-1].reason == "no_pending_offer"


def test_guard_rejected_proposal_still_consumes_an_evaluation(store):
    # DV-03: ガードで拒否された提案(not_acceptable_to_own_principal)も評価回数を消費する。
    nid = _create(store, candidate_policy=reject_all_policy("candidate"))
    view_before = store.get_view(nid, "candidate")
    assert view_before.budget.remaining_evaluations == 16

    response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=sample_package())
    )
    assert response.valid is False
    assert response.error == "not_acceptable_to_own_principal"

    view_after = store.get_view(nid, "candidate")
    assert view_after.budget.remaining_evaluations == 15  # 1 回消費されている


def test_receiver_evaluation_accept_recheck_and_judgment_do_not_consume_evaluations(store):
    # DV-03: 受け手としての評価・accept の確かめ直し・判定は、評価回数を消費しない。
    nid = _create(store)
    package = sample_package()

    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    assert propose_response.valid is True

    # 提案を受けた時点(受け手としての評価が起きたはず)で、求人側の評価回数は 0 のまま。
    employer_view = store.get_view(nid, "employer")
    assert employer_view.budget.remaining_evaluations == 16

    accept_response = store.process_move(
        nid, MoveRequest(expected_version=propose_response.version, side="employer", move="accept")
    )
    assert accept_response.status == "judged"  # accept の確かめ直し・判定(§3.6)を経て合意

    # 交渉は judged になり snapshots は消えるが、counters 自体は消費されていないはず。
    # judged 後は get_view の budget 計算に使う counters がまだ Firestore に残っているので、
    # 直接 Firestore の文書を見て確認する。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["counters"]["employer"]["evaluations_used"] == 0


def test_counterparty_proposals_never_exceed_receivers_own_evaluation_budget(store):
    # DV-03: 相手が提案を重ねても、自分の評価上限を超えない。candidate が提案 → employer が
    # 断る、を手数の上限(6 回)いっぱいまで繰り返しても、employer 自身の評価回数
    # (evaluations_used)は 0 のまま(受け手としての評価は数えないため)。
    nid = _create(store, employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    package = sample_package()
    version = 0
    for _ in range(6):
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=package)
        )
        assert response.valid is True
        version = response.version
        response = store.process_move(nid, MoveRequest(expected_version=version, side="employer", move="reject"))
        assert response.valid is True
        version = response.version

    employer_view = store.get_view(nid, "employer")
    assert employer_view.budget.remaining_evaluations == 16  # 一度も自分の手で評価していない


def test_negotiation_ends_only_after_three_consecutive_invalid_moves_from_the_same_side(store):
    # DV-03: 同じ側の無効手が 3 回続いたときだけ終わる(2 回では終わらない)。
    nid = _create(store, candidate_policy=reject_all_policy("candidate"))
    version = 0
    for _ in range(2):
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
        )
        assert response.valid is False
        assert response.status == "active"  # まだ終わらない
        version = response.version

    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
    )
    assert response.valid is False
    assert response.status == "judged"
    assert response.end_reason == "stopped_invalid"


def test_a_valid_move_resets_the_consecutive_invalid_counter(store):
    # DV-03 の裏付け: 途中で有効な手を挟むと、連続無効手の数がリセットされ、3 回に届かない。
    nid = _create(store, employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    version = 0

    # 無効手を 2 回(no_pending_offer での accept)。
    for _ in range(2):
        response = store.process_move(nid, MoveRequest(expected_version=version, side="candidate", move="accept"))
        assert response.valid is False
        version = response.version

    # 有効な手を 1 回(check)。連続無効手はここでリセットされるはず。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="check", package=sample_package())
    )
    assert response.valid is True
    version = response.version

    # さらに無効手を 2 回続けても(合計では直前から数えて 2 回なので)、まだ終わらない。
    for _ in range(2):
        response = store.process_move(nid, MoveRequest(expected_version=version, side="candidate", move="accept"))
        assert response.valid is False
        assert response.status == "active"
        version = response.version


# --- 金庫の無効手の見え方(台帳 C-40) ---


def _last_invalid_event(store, nid, side="candidate"):
    events = store.get_events(nid, side)
    assert events[-1].kind == "invalid", events[-1]
    return events[-1]


def _summary(event):
    """無効手の見え方の、C-40 で決めた 4 つの項目(理由・打とうとした手・組み合わせ・自分側の評価)。"""
    return (event.reason, event.attempted_move, event.package, event.own_evaluation)


def _exhaust_evaluations(store, nid, package) -> int:
    """候補者が有効な check を 16 回打って、評価回数を使い切る。次の expected_version を返す。"""
    version = 0
    for _ in range(16):
        response = store.process_move(
            nid, MoveRequest(expected_version=version, side="candidate", move="check", package=package)
        )
        assert response.valid is True
        version = response.version
    return version


@pytest.mark.parametrize(
    ("candidate_policy", "expected_evaluation"),
    [(reject_all_policy("candidate"), "not_acceptable"), (needs_confirmation_policy("candidate"), "needs_confirmation")],
)
def test_a_guard_rejected_proposal_records_the_move_the_package_and_the_own_evaluation(
    store, candidate_policy, expected_evaluation
):
    # C-40: ガードで断られた提案の見え方は、打とうとした手(propose)・組み合わせ・自分側の評価(そのポリシーでの
    # 3 値)を持つ。次の TurnInput.last_invalid の元になる。相手(求人側)には、何も見えない。
    nid = _create(store, candidate_policy=candidate_policy)
    package = sample_package()
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package))

    event = _last_invalid_event(store, nid)
    assert _summary(event) == ("not_acceptable_to_own_principal", "propose", package, expected_evaluation)
    assert store.get_events(nid, "employer") == []


def test_a_question_that_does_not_apply_records_the_move_the_package_and_the_own_evaluation(store):
    # C-40: 「本人確認が必要」でない組み合わせへの途中確認(question_not_applicable。評価 1 消費)も、
    # 打とうとした手・組み合わせ・自分側の評価を持つ(どの P を聞いたか、次の入力から分かる)。
    nid = _create(store, candidate_policy=accept_all_policy("candidate"))
    package = sample_package()
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package))

    assert _summary(_last_invalid_event(store, nid)) == (
        "question_not_applicable",
        "ask_principal",
        package,
        "acceptable",
    )


def test_a_question_over_the_limit_records_the_move_the_package_and_the_own_evaluation(store):
    # C-40: 上限を超える 2 回目の途中確認(question_budget_exhausted。評価 1 消費)も、同じ 4 つの項目を持つ。
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    first, second = sample_package(salary=650), sample_package(salary=550, remote_days=1)
    asked = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=first)
    )
    answered = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=asked.version, side="candidate", package=first, answer="accept")
    )
    store.process_move(
        nid, MoveRequest(expected_version=answered.version, side="candidate", move="ask_principal", package=second)
    )

    assert _summary(_last_invalid_event(store, nid)) == (
        "question_budget_exhausted",
        "ask_principal",
        second,
        "needs_confirmation",
    )


def test_an_accept_recheck_failure_records_the_pending_package_and_the_own_evaluation(store):
    # C-40: accept の確かめ直しで「受けられる」でなかった無効手は、打とうとした手(accept)・受けようとした
    # 提案の組み合わせ・自分側の評価を持つ。
    nid = _create(store, employer_rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))])
    package = sample_package()
    proposed = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    store.process_move(nid, MoveRequest(expected_version=proposed.version, side="employer", move="accept"))

    assert _summary(_last_invalid_event(store, nid, "employer")) == (
        "not_acceptable_to_own_principal",
        "accept",
        package,
        "needs_confirmation",
    )
    assert [e.kind for e in store.get_events(nid, "candidate")] == ["propose"]  # 求人側の無効手は、候補者に見えない


@pytest.mark.parametrize("move", ["accept", "reject"])
def test_a_move_without_a_pending_offer_records_the_move_and_has_no_package_or_evaluation(store, move):
    # C-40: 提案がないのに accept・reject した無効手は、打とうとした手だけを持つ(組み合わせも評価もない)。
    nid = _create(store)
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move=move))

    assert _summary(_last_invalid_event(store, nid)) == ("no_pending_offer", move, None, None)


@pytest.mark.parametrize("move", ["check", "propose", "ask_principal"])
def test_a_move_after_the_evaluations_are_used_up_records_the_move_and_the_package_without_an_evaluation(store, move):
    # C-40: 評価回数が尽きた後の check・propose・ask_principal(evaluation_budget_exhausted)は、評価をしていない
    # ので、打とうとした手と組み合わせだけを持つ(自分側の評価はない)。
    nid = _create(store)
    package = sample_package()
    version = _exhaust_evaluations(store, nid, package)
    store.process_move(nid, MoveRequest(expected_version=version, side="candidate", move=move, package=package))

    assert _summary(_last_invalid_event(store, nid)) == ("evaluation_budget_exhausted", move, package, None)


@pytest.mark.parametrize("reason", ["schema_invalid", "agent_timeout"])
def test_an_invalid_move_registered_by_the_referee_has_no_move_package_or_evaluation(store, reason):
    # C-40: レフェリーが登録した無効手(schema_invalid・agent_timeout)は、金庫には打とうとした手も組み合わせも
    # 分からない。3 つとも入らない(null)。
    nid = _create(store)
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="invalid", reason=reason))

    assert _summary(_last_invalid_event(store, nid)) == (reason, None, None, None)


def test_the_attempted_move_is_exposed_by_the_events_api_and_only_for_invalid_moves(api_client, store):
    # C-40: 金庫の events API(web が読む口)の応答に、attempted_move が出る。無効手だけが持ち、ほかの手は null。
    nid = _create(store, candidate_policy=reject_all_policy("candidate"))
    package = sample_package()
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package))
    store.process_move(nid, MoveRequest(expected_version=1, side="candidate", move="check", package=package))

    events = api_client.get(f"/v1/negotiations/{nid}/events", params={"side": "candidate"}).json()

    assert [(e["kind"], e["attempted_move"], e["own_evaluation"]) for e in events] == [
        ("invalid", "propose", "not_acceptable"),
        ("check", None, "not_acceptable"),
    ]


# --- web(レフェリー)の部分(1d-1) ---


def _candidate_counters(store, nid) -> dict:
    return store._negotiation_ref(nid).get().to_dict()["counters"]["candidate"]


@pytest.mark.anyio
async def test_schema_violation_is_registered_as_invalid_then_last_error_reaches_the_next_turn_input(
    store, web_env
):
    # DV-03: 台本のエージェントのスキーマ違反が、invalid として登録される。次の TurnInput.last_error に
    # 理由が入り、直した手で交渉が続く。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)
    broken = {"schema": "move/v1", "move": "propose"}  # propose なのに package がない
    env.agents.script("candidate", broken, move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED  # 無効手として登録された
    events = store.get_events(nid, "candidate")
    assert [(e.kind, e.reason, e.package) for e in events] == [("invalid", "schema_invalid", None)]
    assert store.get_view(nid, "candidate").to_move == "candidate"  # 手番は変わらない
    assert store.get_events(nid, "employer") == []  # 相手には見えない
    counters = _candidate_counters(store, nid)
    assert (counters["moves_used"], counters["consecutive_invalid"]) == (1, 1)

    assert await referee.step() is StepOutcome.MOVED  # 直した手で続く
    first, second = env.agents.calls_for("candidate")
    assert first.turn_input.last_error is None
    assert second.turn_input.last_error == "schema_invalid"  # 理由が次の入力に入っている
    assert second.turn_input.own_move_number == 1  # 無効手も自分の手に数える
    assert _candidate_counters(store, nid)["consecutive_invalid"] == 0  # 有効な手で戻る

    assert await referee.step() is StepOutcome.FINISHED  # 求人側が受けて、合意
    assert env.agents.calls_for("employer")[0].turn_input.last_error is None  # 相手の無効手は伝わらない
    assert store.get_view(nid, "candidate").status == "judged"


@pytest.mark.anyio
async def test_vault_detected_invalid_move_also_reaches_the_next_turn_input(store, web_env):
    # DV-03: 金庫で分かる無効手(ガードで拒否された提案)も記録され、理由が次の TurnInput.last_error に入る。
    # ガードの評価は消費されるので、次の入力の残りの評価回数は 1 減っている。
    env = web_env
    nid = create_demo_negotiation(store, candidate_policy=reject_all_policy("candidate"))
    env.agents.script("candidate", move_dict("propose", sample_package()), move_dict("end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED
    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [
        ("invalid", "not_acceptable_to_own_principal")
    ]
    assert await referee.step() is StepOutcome.FINISHED  # 次の手(end)で終わる

    second = env.agents.calls_for("candidate")[1].turn_input
    assert second.last_error == "not_acceptable_to_own_principal"
    assert second.budget.remaining_evaluations == 15


@pytest.mark.anyio
@pytest.mark.parametrize("transient_error", [ConnectionError, TimeoutError])
async def test_transient_agent_errors_are_retried_and_never_become_invalid_moves(store, web_env, transient_error):
    # DV-03: 一時的なエラー(通信エラー・時間切れ)は、待ち時間を空けて再試行される。
    # 再試行の範囲で成功すれば、無効手にならない(連続無効手にも数えない)。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", transient_error("temporary"), transient_error("temporary"))
    env.agents.script("candidate", move_dict("propose", package))

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert [e.kind for e in store.get_events(nid, "candidate")] == ["propose"]  # 無効手の記録がない
    calls = env.agents.calls_for("candidate")
    assert len(calls) == 3  # 同じ手番で 3 回呼んだ
    assert len({str(c.turn_input.model_dump()) for c in calls}) == 1  # 3 回とも同じ入力
    assert all(c.timeout_s == DEFAULT_WEB_CONFIG.referee.agent_call_timeout_seconds for c in calls)  # 60 秒
    assert env.sleep.calls == [1.0, 2.0]  # 待ち時間を空けている(暫定 1・2・4 秒)
    assert _candidate_counters(store, nid)["consecutive_invalid"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("transient_error", [ConnectionError, TimeoutError])
async def test_retries_used_up_become_an_agent_timeout_invalid_move(store, web_env, transient_error):
    # DV-03: 再試行を使い切ったら agent_timeout の無効手として登録される(最初の 1 回 + 再試行 3 回)。
    # 次の TurnInput.last_error に agent_timeout が入り、続く手で交渉が続く。
    env = web_env
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", *[transient_error("down")] * 4)
    env.agents.script("candidate", move_dict("end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED

    assert len(env.agents.calls_for("candidate")) == 4
    assert env.sleep.calls == [1.0, 2.0, 4.0]
    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "agent_timeout")]

    assert await referee.step() is StepOutcome.FINISHED
    assert env.agents.calls_for("candidate")[4].turn_input.last_error == "agent_timeout"


@pytest.mark.anyio
async def test_value_error_from_the_receiver_is_not_retried_and_becomes_schema_invalid(store, web_env):
    # 受信口が拒否した(ValueError)のは、同じ入力を送り直しても直らないので、再試行せず schema_invalid にする。
    env = web_env
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", ValueError("receiver rejected the message"))

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert len(env.agents.calls_for("candidate")) == 1
    assert env.sleep.calls == []
    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "schema_invalid")]


@pytest.mark.anyio
async def test_the_agent_call_limit_covers_the_retry_waits(store, clock, web_env):
    # §4.1: 1 回の呼び出しの上限(60 秒)は、再試行の待ち時間を含む。待つと上限を超えるなら、あきらめる。
    # 待ち時間を 30 秒にして、2 回目の呼び出しには残りの 30 秒だけを渡し、その次の待ちは行わない。
    env = web_env
    env.configure(
        sleep=FakeSleep(clock, advance=True),
        config=replace(env.config, agent_retry_backoff_seconds=(30.0, 30.0, 30.0)),
    )
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", ConnectionError("down"), ConnectionError("down"), move_dict("end"))

    assert await env.referee(nid).step() is StepOutcome.MOVED

    calls = env.agents.calls_for("candidate")
    assert [c.timeout_s for c in calls] == [60, 30]  # 上限の残りを渡す
    assert env.sleep.calls == [30.0]
    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "agent_timeout")]


@pytest.mark.anyio
async def test_an_agent_that_never_answers_is_cut_off_at_the_limit_by_the_referee_itself(store, web_env):
    # §4.1: 1 回の呼び出しの上限は、差し込まれた関数が timeout_s を守ることに頼らず、レフェリー自身も強制する。
    # 応答しないエージェント(台本が止まったまま)でも、上限で打ち切って再試行し、使い切れば agent_timeout にする。
    # (上限と再試行の待ちだけを極端に短くする。時計・sleep は注入のまま)
    env = web_env
    env.configure(
        config=replace(env.config, agent_call_timeout_seconds=0.01, agent_retry_backoff_seconds=(0.0, 0.0, 0.0))
    )
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", Blocker(), Blocker(), Blocker(), Blocker(), move_dict("end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED

    assert len(env.agents.calls_for("candidate")) == 4  # 最初の 1 回 + 再試行 3 回。どれも打ち切られた
    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "agent_timeout")]


@pytest.mark.anyio
async def test_an_unexpected_agent_exception_is_an_agent_timeout_invalid_move_not_a_crash(store, web_env):
    # 約束にない例外(RuntimeError など)でタスクを落とさない。その手番を agent_timeout の無効手にする
    # (続けば金庫が 3 回で止める)。再試行はしない(原因が分からないため)。
    env = web_env
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", RuntimeError("bug in the gateway"))

    assert await env.referee(nid).step() is StepOutcome.MOVED

    assert len(env.agents.calls_for("candidate")) == 1
    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "agent_timeout")]


@pytest.mark.anyio
async def test_three_consecutive_invalid_agent_moves_end_the_negotiation(store, web_env):
    # DV-03: 同じ側の無効手が 3 回続いたときだけ終わる。終わりは金庫が決め、レフェリーのタスクも終わる。
    env = web_env
    nid = create_demo_negotiation(store)
    garbage = {"schema": "move/v1", "move": "withdraw"}  # 列挙外の手
    env.agents.script("candidate", garbage, garbage, garbage)

    outcomes = await drive(env.referee(nid))

    assert outcomes == [StepOutcome.MOVED, StepOutcome.MOVED, StepOutcome.FINISHED]
    doc = store._negotiation_ref(nid).get().to_dict()
    assert (doc["status"], doc["end_reason"]) == ("judged", "stopped_invalid")
    assert [e.kind for e in store.get_events(nid, "candidate")] == ["invalid", "invalid", "invalid", "final_result"]
