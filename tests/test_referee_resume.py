"""DV-08: レフェリーの再開・期限切れ・見回り(design.md §3.4・§4.1・§6.2)。

金庫は本物の vault の app を ASGI のままつなぎ、エージェントはスタブ、時計は注入(FixedClock)。
テストは sleep しない: 見回りは sweep_once()、レフェリーは step() で 1 回ずつ進め、背景のタスクが
待つ場面は FakeSleep(blocking)を tick() で進める。
"""

import asyncio
import datetime as dt

import pytest
from vault.api_models import ControlRequest, PrincipalAnswerRequest
from vault.config import DEFAULT_VAULT_CONFIG
from vault.models import EmployerRule
from vault_helpers import needs_confirmation_policy, sample_package
from web.config import DEFAULT_WEB_CONFIG, load_web_config
from web.referee import StepOutcome
from web.vault_client import VaultUnavailableError
from web_helpers import (
    Blocker,
    CrashAfterMove,
    ScriptedAnswerer,
    SimulatedCrash,
    create_demo_negotiation,
    create_live_negotiation,
    move_dict,
    plan_dict,
)

_HOUR = dt.timedelta(hours=1)
_MINUTE = dt.timedelta(minutes=1)


def _negotiation_doc(store, nid) -> dict:
    return store._negotiation_ref(nid).get().to_dict()


def _stage_doc(default_db, nid):
    return default_db.collection("stages").document(nid).get()


async def _finish(env, nid, timeout: float = 30) -> None:
    await asyncio.wait_for(env.manager.task(nid), timeout=timeout)


@pytest.mark.anyio
async def test_recreated_referee_task_continues_from_the_same_turn(store, web_env):
    # DV-08: レフェリーのタスクを途中で止めて作り直すと、見回りが一覧から拾い、同じ手番から続く。
    # 候補者の手番の、確かめの後の決定の呼び出しの最中に web が落ちる → 起動し直した見回りがタスクを作り直し、
    # 同じ手番を計画から始める。済んだ確かめは履歴から埋まるので、金庫の check は重複せず、評価も重ねて消費しない。
    # 決定の入力は、落ちる前と同じになる(同じ手番・同じ history・同じ checked・同じ残り)。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)
    blocker = Blocker()
    env.agents.script("candidate", plan_dict(checks=[package]), blocker)  # 計画 → 確かめ → 決定(応答のないまま落ちる)

    first_report = await env.sweeper.sweep_once()
    assert (first_report.listed, first_report.tasks_started) == (1, 1)
    await asyncio.wait_for(blocker.started.wait(), timeout=10)
    interrupted = env.agents.calls[-1]
    assert interrupted.turn_input.phase == "decide"
    assert store.get_view(nid, "candidate").budget.remaining_evaluations == 16  # 確かめで評価を 1 使った

    await env.restart()  # web が落ちて、タスクが消えた
    assert not env.manager.is_running(nid)

    env.agents.script("candidate", plan_dict(checks=[package]), move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))
    second_report = await env.sweeper.sweep_once()
    assert (second_report.listed, second_report.tasks_started) == (1, 1)
    assert second_report.stages_created == 0  # 段階開示の状態は、すでにある
    await _finish(env, nid)

    replanned, resumed = env.agents.calls[2], env.agents.calls[3]
    assert (replanned.role, replanned.nid) == (interrupted.role, interrupted.nid)
    assert replanned.turn_input.phase == "plan" and replanned.turn_input.own_move_number == 1  # 確かめ 1 回は登録済み
    assert resumed.turn_input.phase == "decide"
    assert resumed.turn_input.model_dump() == interrupted.turn_input.model_dump()  # 同じ手番・同じ入力
    assert [e.kind for e in store.get_events(nid, "candidate")] == ["check", "propose", "final_result"]  # 確かめは重ならない
    assert store.get_view(nid, "candidate").status == "judged"


@pytest.mark.anyio
async def test_recreated_task_waits_on_a_pending_principal_question_and_resumes_after_the_answer(store, web_env):
    # DV-08: 途中確認(awaiting_principal)の最中に落ちても、見回りが作り直したタスクは待ち続け、
    # 本物の依頼者の回答が金庫に届けば、同じ手番から続く。本物の依頼者の途中確認には、
    # 自動回答の口(差し込んであっても)を使わない。
    env = web_env
    env.sleep.blocking = True
    answerer = ScriptedAnswerer("accept")
    env.configure(answerer=answerer)
    package = sample_package()
    nid, pid = create_live_negotiation(store, candidate_policy=needs_confirmation_policy("candidate"))
    env.agents.script("candidate", move_dict("ask_principal", package))

    await env.sweeper.sweep_once()
    await env.sleep.wait_for_calls(1)  # 質問を登録して、回答待ちに入った
    assert store.get_view(nid, "candidate").status == "awaiting_principal"

    await env.restart()  # 回答待ちのまま web が落ちた
    env.sleep.blocking = True
    await env.sweeper.sweep_once()
    await env.sleep.wait_for_calls(2)  # 作り直したタスクも、同じ質問を待つ
    assert len(env.agents.calls) == 1  # 待つだけで、エージェントは呼ばない
    assert answerer.calls == []  # 本物の依頼者には、自動では答えない

    # 本物の依頼者の回答が届く(画面の API が金庫に直接送る。1d-2)。
    view = store.get_view(nid, "candidate")
    answer_view = await env.vault.post_principal_answer(
        nid,
        PrincipalAnswerRequest(
            expected_version=view.version, side="candidate", package=package, answer="accept"
        ),
    )
    assert answer_view.status == "active"
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))
    env.sleep.tick()
    await _finish(env, nid)

    assert store.get_view(nid, "candidate").status == "judged"
    resumed_input = env.agents.calls[1].turn_input  # 回答の後の最初の TurnInput
    assert resumed_input.last_error is None
    assert [e.kind for e in store.get_events(nid, "candidate")] == [
        "ask_principal",
        "principal_answer",
        "propose",
        "final_result",
    ]
    assert (_stage_doc(env.default_db, nid).to_dict())["candidate_principal_id"] == pid


@pytest.mark.anyio
async def test_pause_freezes_the_turn_deadline_and_resume_does_not_expire_the_negotiation(store, clock, web_env):
    # DV-08: 一時停止中は手番の期限が進まず、再開しても期限切れにならない。
    env = web_env
    env.sleep.blocking = True
    package = sample_package()
    nid = create_demo_negotiation(store)
    await env.vault.control(nid, ControlRequest(side="candidate", action="pause"))

    await env.sweeper.sweep_once()  # 一時停止中の交渉にもタスクを作る。待つだけで、エージェントは呼ばない
    await env.sleep.wait_for_calls(1)
    assert env.agents.calls == []

    clock.advance(2 * _HOUR)  # 手番の期限(5 分)を大きく過ぎる
    report = await env.sweeper.sweep_once()
    assert (report.expire_calls, report.expired) == (1, 0)  # 一時停止中は金庫に判断させるが、期限切れにはならない
    frozen = await env.vault.get_view(nid, "candidate")
    assert (frozen.status, frozen.paused, frozen.deadline) == ("active", True, None)

    await env.vault.control(nid, ControlRequest(side="candidate", action="resume"))
    resumed = await env.vault.get_view(nid, "candidate")
    move_deadline = dt.timedelta(seconds=DEFAULT_VAULT_CONFIG.deadlines.move_deadline_seconds)
    assert resumed.deadline == clock.now() + move_deadline  # 再開の時点から付け直す
    report = await env.sweeper.sweep_once()
    assert (report.expire_calls, report.expired) == (0, 0)  # 期限内なので、expire も呼ばない

    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))
    env.sleep.tick()
    await _finish(env, nid)
    assert store.get_view(nid, "candidate").status == "judged"
    assert _negotiation_doc(store, nid)["end_reason"] == "agreed"  # 期限切れではなく、合意で終わった


@pytest.mark.anyio
@pytest.mark.parametrize("final_state", ["paused", "active"])
async def test_repeated_pause_and_resume_still_ends_at_expires_at(store, clock, web_env, final_state):
    # DV-08: 一時停止と再開を繰り返しても、expires_at(作成から 72 時間)で必ず終わる。
    # 停止は最長の停止時間(24 時間)未満で再開し、再開のたびに手番の期限が付け直されるので、
    # 期限・最長停止では終わらない。それでも、寿命を過ぎれば見回りの expire で終わる。
    env = web_env
    env.agents.script("candidate", Blocker())  # 誰も手を打たない(エージェントは応答しない)
    nid = create_demo_negotiation(store)
    await env.sweeper.sweep_once()

    for _ in range(3):  # 1 周 = 23 時間の一時停止 + 4 分。3 周で 69 時間 12 分
        await env.vault.control(nid, ControlRequest(side="candidate", action="pause"))
        clock.advance(23 * _HOUR)
        report = await env.sweeper.sweep_once()
        assert (report.expire_calls, report.expired) == (1, 0)
        await env.vault.control(nid, ControlRequest(side="candidate", action="resume"))
        view = await env.vault.get_view(nid, "candidate")
        assert view.deadline is not None and view.deadline <= view.expires_at  # 寿命を超える期限は付かない
        clock.advance(4 * _MINUTE)
        report = await env.sweeper.sweep_once()
        assert (report.expire_calls, report.expired) == (0, 0)

    # 最後の一時停止から、寿命の 2 分前に再開する(または再開せず停止のまま待つ)。
    view = await env.vault.get_view(nid, "candidate")
    await env.vault.control(nid, ControlRequest(side="candidate", action="pause"))
    clock.advance(view.expires_at - clock.now() - 2 * _MINUTE)
    if final_state == "active":
        await env.vault.control(nid, ControlRequest(side="candidate", action="resume"))
        view = await env.vault.get_view(nid, "candidate")
        assert view.deadline == view.expires_at  # 手番の期限(5 分)は、寿命で頭打ちになる
    report = await env.sweeper.sweep_once()
    assert (report.expire_calls, report.expired) == (0 if final_state == "active" else 1, 0)  # まだ寿命の前

    clock.advance(3 * _MINUTE)  # 寿命(作成から 72 時間)を過ぎた
    report = await env.sweeper.sweep_once()

    assert (report.expire_calls, report.expired) == (1, 1)
    doc = _negotiation_doc(store, nid)
    assert (doc["status"], doc["end_reason"]) == ("judged", "timeout")
    assert doc["snapshots"] is None  # コピーが残らない(FR-14)
    assert (await env.vault.get_view(nid, "candidate")).result.likelihood == "none"


@pytest.mark.anyio
async def test_sweeper_expires_a_negotiation_nobody_operates(store, clock, web_env):
    # DV-08: 誰も操作しなくても、見回りの expire で期限切れになる(手番の期限 5 分)。
    env = web_env
    env.agents.script("candidate", Blocker())  # エージェントが応答しない = 誰も手を打たない
    nid = create_demo_negotiation(store)
    first = await env.sweeper.sweep_once()
    assert (first.tasks_started, first.expire_calls) == (1, 0)  # 期限内。expire は呼ばない

    await env.restart()  # web が落ちて、タスクが消えた(起動し直した見回りが最初に拾う)
    clock.advance(6 * _MINUTE)
    second = await env.sweeper.sweep_once()

    assert (second.expire_calls, second.expired) == (1, 1)
    assert second.tasks_started == 0 and env.manager.task(nid) is None  # 終わった交渉には、タスクを作らない
    doc = _negotiation_doc(store, nid)
    assert (doc["status"], doc["end_reason"]) == ("judged", "timeout")
    assert doc["snapshots"] is None
    assert await env.vault.list_open_negotiations() == []  # 一覧からも消える


@pytest.mark.anyio
async def test_cancel_is_effective_while_awaiting_principal(store, web_env):
    # DV-08: 取消は awaiting_principal 中でも効く。回答待ちのレフェリーは、取消を読んでタスクを終える。
    env = web_env
    env.configure(answerer=ScriptedAnswerer("accept"))
    package = sample_package()
    nid, pid = create_live_negotiation(store, candidate_policy=needs_confirmation_policy("candidate"))
    env.agents.script("candidate", move_dict("ask_principal", package))

    async def cancel_while_waiting(sleep) -> None:
        assert store.get_view(nid, "candidate").status == "awaiting_principal"
        await env.vault.control(nid, ControlRequest(side="candidate", action="cancel"))

    env.sleep.hook = cancel_while_waiting
    referee = env.referee(nid, mode="live", candidate_principal_id=pid)

    await asyncio.wait_for(referee.run(), timeout=30)  # 回答待ち → 取消 → 終了

    doc = _negotiation_doc(store, nid)
    assert (doc["status"], doc["end_reason"]) == ("judged", "cancelled")
    assert env.sleep.calls == [DEFAULT_WEB_CONFIG.referee.wait_poll_interval_seconds]  # 1 回待っただけ
    assert (await env.vault.get_view(nid, "candidate")).result.likelihood == "none"


@pytest.mark.anyio
async def test_stage_zero_is_recreated_after_web_crashes_right_after_agreement(
    store, web_env, default_db, vault_client
):
    # DV-08: 合意の直後に web を落としても、段階開示の状態が作り直され、段 0 になっている。
    # 金庫に交渉を作った直後に落ちて stages/{nid} がない → 起動し直した見回りが作る(本物の候補者の
    # 依頼者 ID つき)→ 合意の登録が金庫にコミットされた直後にもう一度落ちる → 起動し直しても、
    # 段階開示の状態は段 0 のまま残っている(判定の後で、一覧にはもう載らない)。
    env = web_env
    package = sample_package()
    nid, pid = create_live_negotiation(store)
    assert _stage_doc(default_db, nid).exists is False  # 作り損ねた

    await env.restart(vault=CrashAfterMove(env.vault, crash_on_move_number=2))
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))
    report = await env.sweeper.sweep_once()
    assert (report.stages_created, report.tasks_started) == (1, 1)
    await asyncio.wait({env.manager.task(nid)}, timeout=30)
    assert isinstance(env.manager.task(nid).exception(), SimulatedCrash)  # 合意の直後に落ちた

    assert store.get_view(nid, "candidate").status == "judged"  # 合意は金庫に残っている
    assert _negotiation_doc(store, nid)["end_reason"] == "agreed"

    await env.restart(vault=vault_client)  # 起動し直す(金庫の状態と (default) の文書はそのまま)
    report = await env.sweeper.sweep_once()

    assert report.listed == 0  # 判定の後なので、見回りの一覧には載らない
    stage = _stage_doc(default_db, nid).to_dict()
    assert (stage["stage"], stage["candidate_principal_id"]) == (0, pid)  # 段 0・依頼者 ID つき


@pytest.mark.anyio
async def test_sweeper_creates_missing_stage_once_and_never_overwrites_it(store, web_env, default_db):
    # §6.2: stages/{nid} は、なければ作る(冪等)。すでにあれば、段が進んでいても上書きしない。
    env = web_env
    env.agents.script("candidate", Blocker())
    nid, pid = create_live_negotiation(store)

    first = await env.sweeper.sweep_once()
    assert (first.stages_created, first.tasks_started, first.errors) == (1, 1, 0)
    stage = _stage_doc(default_db, nid).to_dict()
    assert (stage["stage"], stage["candidate_principal_id"]) == (0, pid)

    default_db.collection("stages").document(nid).update({"stage": 1})  # 段が進んだ(後の段の遷移の再現)
    second = await env.sweeper.sweep_once()

    assert (second.stages_created, second.tasks_started, second.errors) == (0, 0, 0)  # 何度拾っても同じ
    assert _stage_doc(default_db, nid).to_dict()["stage"] == 1  # 段 0 で上書きされていない


@pytest.mark.anyio
async def test_sweeper_creates_stage_for_a_fictional_candidate_without_a_principal_id(store, web_env, default_db):
    # 架空の候補者(デモ・攻撃)の交渉にも、段階開示の状態を段 0 で作る(依頼者 ID は持たない)。
    env = web_env
    env.agents.script("candidate", Blocker())
    nid = create_demo_negotiation(store)

    await env.sweeper.sweep_once()

    stage = _stage_doc(default_db, nid).to_dict()
    assert (stage["stage"], stage["candidate_principal_id"]) == (0, None)


@pytest.mark.anyio
async def test_one_failing_negotiation_does_not_stop_the_rest_of_the_sweep(store, web_env, default_db):
    # 見回りは、1 件の処理が失敗しても続ける(次の見回りでやり直す)。段階開示の状態を作れなくても、
    # 期限切れとタスクの作り直しは行う。
    env = web_env
    env.agents.script("candidate", Blocker(), Blocker())
    nid_a = create_demo_negotiation(store)
    nid_b = create_demo_negotiation(store)

    real_ensure = env.stages.ensure

    async def failing_ensure(nid, candidate_principal_id):
        if nid == nid_a:
            raise RuntimeError("stage store is down")
        return await real_ensure(nid, candidate_principal_id)

    env.stages.ensure = failing_ensure
    report = await env.sweeper.sweep_once()

    assert report.errors == 1
    assert report.tasks_started == 2  # 失敗した交渉にも、タスクは作る
    assert _stage_doc(default_db, nid_a).exists is False
    assert _stage_doc(default_db, nid_b).exists is True

    env.stages.ensure = real_ensure  # 直ったら、次の見回りが作る
    report = await env.sweeper.sweep_once()
    assert (report.errors, report.stages_created) == (0, 1)
    assert _stage_doc(default_db, nid_a).exists is True


@pytest.mark.anyio
async def test_a_referee_task_that_ended_is_recreated_by_the_next_sweep(store, web_env):
    # 「タスクがなければ作り直す」の「ない」には、落ちて終わったタスクも含む(動いているタスクだけを数える)。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", SimulatedCrash)  # 呼び出しの途中でタスクが落ちる

    await env.sweeper.sweep_once()
    await asyncio.wait({env.manager.task(nid)}, timeout=30)
    assert not env.manager.is_running(nid)

    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))
    report = await env.sweeper.sweep_once()
    assert report.tasks_started == 1
    await _finish(env, nid)
    assert store.get_view(nid, "candidate").status == "judged"


def test_web_config_holds_the_design_values():
    # §4.1 の暫定値: 1 回の呼び出しの上限 60 秒・再試行 3 回・見回り 60 秒ごと(設定ファイルにある)。
    config = load_web_config()
    assert config.referee.agent_call_timeout_seconds == 60
    assert config.referee.agent_max_retries == 3
    assert config.sweeper.interval_seconds == 60
    assert config == DEFAULT_WEB_CONFIG
    assert len(config.referee.agent_retry_backoff_seconds) >= config.referee.agent_max_retries


@pytest.mark.anyio
async def test_the_sweeper_run_loop_sweeps_at_startup_and_then_every_interval(store, web_env):
    # §4.1: 起動時に 1 回、その後は一定間隔ごと。間隔は注入した sleep で数える(実際には待たない)。
    env = web_env
    env.sleep.blocking = True
    env.agents.script("candidate", Blocker())
    nid = create_demo_negotiation(store)

    run_task = asyncio.create_task(env.sweeper.run())
    await env.sleep.wait_for_calls(1)
    assert env.manager.is_running(nid)  # 起動時の見回りでタスクが作られた
    assert env.sleep.calls == [DEFAULT_WEB_CONFIG.sweeper.interval_seconds]

    env.sleep.tick()  # 次の見回り
    await env.sleep.wait_for_calls(2)
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.anyio
async def test_the_sweeper_run_loop_survives_a_failing_list_call(store, web_env):
    # 金庫の一覧を読めなかった見回りは、そのループを止めず、次の間隔でやり直す(起動時の見回りを含む)。
    env = web_env
    env.agents.script("candidate", Blocker())
    nid = create_demo_negotiation(store)
    real_vault = env.vault
    failures = [VaultUnavailableError("vault is down", 503)]

    class FlakyVault:
        def __getattr__(self, name):
            return getattr(real_vault, name)

        async def list_open_negotiations(self):
            if failures:
                raise failures.pop()
            return await real_vault.list_open_negotiations()

    await env.restart(vault=FlakyVault())
    env.sleep.blocking = True

    run_task = asyncio.create_task(env.sweeper.run())
    await env.sleep.wait_for_calls(1)  # 起動時の見回りは失敗して、間隔を待っている
    assert not env.manager.is_running(nid)

    env.sleep.tick()  # 次の見回りで、タスクが作られる
    await env.sleep.wait_for_calls(2)
    assert env.manager.is_running(nid)
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.anyio
async def test_concurrent_stage_creation_creates_the_document_exactly_once(web_env, default_db):
    # §6.2: 交渉の作成直後の作成と、見回り・画面を開いたときの作成が重なっても、文書は 1 つで、作れたのは 1 回だけ。
    env = web_env
    nid = "0123456789abcdef"

    results = await asyncio.gather(*(env.stages.ensure(nid, "principal-1") for _ in range(4)))

    assert sorted(results) == [False, False, False, True]
    assert _stage_doc(default_db, nid).to_dict()["candidate_principal_id"] == "principal-1"


@pytest.mark.anyio
async def test_step_finishes_when_the_negotiation_is_already_judged(store, web_env):
    # 交渉が終わっていれば、step() は何もせず FINISHED(タスクを終える)。
    env = web_env
    nid = create_demo_negotiation(store)
    await env.vault.control(nid, ControlRequest(side="candidate", action="cancel"))

    assert await env.referee(nid).step() is StepOutcome.FINISHED
    assert env.agents.calls == []


@pytest.mark.anyio
async def test_employer_question_of_a_fictional_principal_is_answered_after_a_restart_too(store, web_env):
    # 架空の求人の途中確認(自動回答)の最中に落ちても、作り直したタスクが自動で答えて続ける。
    env = web_env
    env.configure(answerer=ScriptedAnswerer("accept"))
    package = sample_package()
    nid = create_demo_negotiation(
        store, employer_rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))]
    )
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("ask_principal", package))

    referee = env.referee(nid)
    assert await referee.step() is StepOutcome.MOVED  # 候補者の提案
    assert await referee.step() is StepOutcome.MOVED  # 求人側が途中確認を登録した
    assert store.get_view(nid, "employer").status == "awaiting_principal"

    await env.restart()  # 回答の前に落ちた
    env.agents.script("employer", move_dict("accept"))
    await env.sweeper.sweep_once()
    await _finish(env, nid)

    assert store.get_view(nid, "employer").status == "judged"
    assert _negotiation_doc(store, nid)["end_reason"] == "agreed"
