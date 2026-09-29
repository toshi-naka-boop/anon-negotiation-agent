"""レフェリーの 1 手ごとの流れ(design.md §4.1)。

呼ぶエージェントの種類(candidate・employer・attacker)、登録する手の中身、409 で状態を読み直すこと、
待つ場面(一時停止・回答待ち・金庫が応えない)、交渉が消えたときの終わり方を確かめる。
再試行・無効手の登録は tests/test_invalid_move_recovery.py、途中確認の回答は
tests/test_answer_reevaluation.py、再開・見回りは tests/test_referee_resume.py にある。
"""

import asyncio

import pytest
from negotiation_core import AttackerTurnInput, TurnInput
from vault.api_models import ControlRequest
from vault.models import EmployerRule
from vault_helpers import needs_confirmation_policy, sample_package
from web.referee import NegotiationContext, RefereeManager, StepOutcome
from web.vault_client import VaultUnavailableError
from web_helpers import (
    ScriptedAnswerer,
    create_demo_negotiation,
    create_live_negotiation,
    SpyVault,
    drive,
    move_dict,
)


@pytest.mark.parametrize(
    ("mode", "side", "expected_role"),
    [
        ("live", "candidate", "candidate"),
        ("demo", "candidate", "candidate"),
        ("attack", "candidate", "candidate"),  # 攻撃モードでも、候補者側は通常の受信口
        ("live", "employer", "employer"),
        ("demo", "employer", "employer"),
        ("attack", "employer", "attacker"),  # 攻撃モードの交渉の求人側だけが attacker(§4.1・§4.3)
    ],
)
def test_agent_role_is_attacker_only_for_the_employer_side_of_an_attack_negotiation(mode, side, expected_role):
    # §4.1・§4.3: 呼ぶ受信口は、候補者側なら candidate。求人側は、攻撃モード(mode=attack)の交渉のときだけ
    # attacker、それ以外は employer。
    context = NegotiationContext(nid="0123456789abcdef", mode=mode, candidate_principal_id=None)
    assert context.agent_role(side) == expected_role


def test_only_a_candidate_with_a_principal_id_is_real_and_hackathon_employers_are_always_fictional():
    # 架空人物の判定(途中確認に自動で答えてよいか)。求人側は、ハッカソンではいつもフィクスチャ(§3.7)。
    fictional_candidate = NegotiationContext("0123456789abcdef", "demo", None)
    real_candidate = NegotiationContext("0123456789abcdef", "live", "principal-1")
    assert fictional_candidate.is_fictional("candidate") is True
    assert real_candidate.is_fictional("candidate") is False
    assert fictional_candidate.is_fictional("employer") is True
    assert real_candidate.is_fictional("employer") is True


@pytest.mark.anyio
async def test_attack_mode_employer_gets_an_attacker_turn_input_carrying_the_instruction(store, web_env):
    # §4.1・§8.2: 攻撃モードの求人側には role=attacker で AttackerTurnInput(principal_instruction つき)を渡す。
    # 指示文は差し込み口(交渉 ID から引く)から取る。候補者側には、自由文が入らない TurnInput しか渡さない。
    env = web_env
    env.configure(attacker_instruction=lambda nid: f"instruction for {nid[:4]}")
    nid = create_demo_negotiation(store, mode="attack")
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("attacker", move_dict("accept"))

    referee = env.referee(nid, mode="attack")
    await drive(referee)

    candidate_call = env.agents.calls_for("candidate")[0]
    attacker_call = env.agents.calls_for("attacker")[0]
    assert type(candidate_call.turn_input) is TurnInput
    assert isinstance(attacker_call.turn_input, AttackerTurnInput)
    assert attacker_call.turn_input.principal_instruction == f"instruction for {nid[:4]}"
    assert env.agents.calls_for("employer") == []  # 通常の求人側の受信口は使わない


@pytest.mark.anyio
async def test_a_task_recreated_by_the_sweeper_restores_the_agent_role_from_the_vaults_open_list(store, web_env):
    # §4.1: 見回りが作り直したタスクも、交渉の性質(mode)を金庫の一覧から取り戻して、正しい種類の
    # エージェントを呼ぶ(web は mode を覚えていない)。攻撃モードの求人側は attacker、候補者側は candidate。
    env = web_env
    nid = create_demo_negotiation(store, mode="attack")
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("attacker", move_dict("accept"))

    report = await env.sweeper.sweep_once()
    await asyncio.wait_for(env.manager.task(nid), timeout=30)

    assert report.tasks_started == 1
    assert [c.role for c in env.agents.calls] == ["candidate", "attacker"]
    assert store.get_view(nid, "candidate").status == "judged"


@pytest.mark.anyio
async def test_attack_mode_without_an_instruction_source_passes_an_empty_instruction(store, web_env):
    # 攻撃モードの指示の受け付け(③)ができるまでは、指示文の差し込み口がなくても、空の指示文で動く。
    env = web_env
    nid = create_demo_negotiation(store, mode="attack")
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("attacker", move_dict("accept"))

    await drive(env.referee(nid, mode="attack"))

    assert env.agents.calls_for("attacker")[0].turn_input.principal_instruction == ""


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["demo", "live"])
async def test_employer_of_a_normal_negotiation_never_receives_an_attacker_turn_input(store, web_env, mode):
    # 通常の求人側(demo・live)には、TurnInput だけを渡す(自由文が入る経路を作らない。§4.3)。
    env = web_env
    if mode == "live":
        nid, pid = create_live_negotiation(store)
        referee = env.referee(nid, mode="live", candidate_principal_id=pid)
    else:
        nid = create_demo_negotiation(store)
        referee = env.referee(nid, mode="demo")
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("employer", move_dict("accept"))

    await drive(referee)

    employer_call = env.agents.calls_for("employer")[0]
    assert type(employer_call.turn_input) is TurnInput
    assert env.agents.calls_for("attacker") == []


@pytest.mark.anyio
async def test_step_waits_while_paused_and_does_not_call_the_agent(store, web_env):
    # §4.1: 一時停止中はタスクを待たせる。再開すれば、同じ手番から続く。
    env = web_env
    nid = create_demo_negotiation(store)
    await env.vault.control(nid, ControlRequest(side="candidate", action="pause"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.WAITING
    assert env.agents.calls == []

    await env.vault.control(nid, ControlRequest(side="candidate", action="resume"))
    env.agents.script("candidate", move_dict("propose", sample_package()))
    assert await referee.step() is StepOutcome.MOVED
    assert len(env.agents.calls_for("candidate")) == 1


@pytest.mark.anyio
async def test_a_409_on_registration_makes_the_referee_reread_the_state(store, web_env):
    # §4.1 の 3: 409 が返ったら、状態を読み直してから進める。エージェントを呼んでいる間に一時停止が
    # 入ると、その手の登録は 409 になる(記録も消費もない)。次の step() は一時停止を読んで待ち、
    # 再開の後は、新しい状態でエージェントを呼び直す。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)

    async def pause_during_the_call(call):
        await env.vault.control(nid, ControlRequest(side="candidate", action="pause"))
        return move_dict("propose", package)

    env.agents.script("candidate", pause_during_the_call, move_dict("propose", package))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.RETRY  # 登録は 409
    assert [e.kind for e in store.get_events(nid, "candidate")] == ["pause"]  # 手は記録されていない
    assert await referee.step() is StepOutcome.WAITING  # 読み直して、一時停止を知る

    await env.vault.control(nid, ControlRequest(side="candidate", action="resume"))
    assert await referee.step() is StepOutcome.MOVED  # 新しい version で登録できた
    kinds = [e.kind for e in store.get_events(nid, "candidate")]
    assert kinds == ["pause", "resume", "propose"]
    assert len(env.agents.calls_for("candidate")) == 2  # 無駄になった呼び出しと、呼び直し


@pytest.mark.anyio
async def test_the_run_loop_sleeps_between_reads_after_a_409(store, web_env):
    # 409 の後は、待たずに読み直すのではなく、間を空けて(暫定 2 秒)読み直す
    # (依頼者の削除中など、409 が続く場面で金庫を叩き続けないため)。
    # エージェントを呼んでいる間に、一時停止と再開が入って version だけが進む。状態は動ける(active)のままなので、
    # 待ちが起きるのは 409 の後の 1 回だけ(一時停止を読んで待つ経路とは区別できる)。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)

    async def bump_the_version_during_the_call(call):
        await env.vault.control(nid, ControlRequest(side="candidate", action="pause"))
        await env.vault.control(nid, ControlRequest(side="candidate", action="resume"))
        return move_dict("propose", package)

    env.agents.script("candidate", bump_the_version_during_the_call, move_dict("propose", package))
    env.agents.script("employer", move_dict("accept"))

    await env.referee(nid).run()

    assert env.sleep.calls == [env.config.wait_poll_interval_seconds]  # 409 の後に 1 回だけ待った
    assert len(env.agents.calls_for("candidate")) == 2  # 登録できなかった手は、読み直した状態で呼び直した
    assert store.get_view(nid, "candidate").status == "judged"


@pytest.mark.anyio
async def test_fictional_principal_question_waits_when_no_answerer_is_plugged_in(store, web_env):
    # 自動回答の口が差し込まれていなければ、架空人物の途中確認も、回答が届くまで待つ(期限は金庫が見る)。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(
        store, employer_rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))]
    )
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("ask_principal", package))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED
    assert await referee.step() is StepOutcome.MOVED
    assert await referee.step() is StepOutcome.WAITING
    assert store.get_view(nid, "employer").status == "awaiting_principal"


@pytest.mark.anyio
async def test_the_answerer_receives_the_asking_side_and_the_asked_package(store, web_env):
    # 自動回答の口には、質問した側・確認された組み合わせ・交渉 ID を渡す(答え方はフィクスチャで決まる)。
    env = web_env
    answerer = ScriptedAnswerer("reject")
    env.configure(answerer=answerer)
    package = sample_package()
    nid = create_demo_negotiation(
        store, employer_rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))]
    )
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("employer", move_dict("ask_principal", package))
    referee = env.referee(nid)

    outcomes = [await referee.step() for _ in range(3)]

    assert outcomes == [StepOutcome.MOVED, StepOutcome.MOVED, StepOutcome.ANSWERED]
    assert answerer.calls == [(nid, "employer", package)]


@pytest.mark.anyio
async def test_referee_finishes_when_the_negotiation_has_been_deleted(store, web_env):
    # 依頼者の削除で交渉が消えた(404)ときは、待たずにタスクを終える。エージェントは呼ばない。
    env = web_env
    nid, pid = create_live_negotiation(store)
    store.delete_principal(pid)
    assert store._negotiation_ref(nid).get().exists is False

    assert await env.referee(nid, mode="live", candidate_principal_id=pid).step() is StepOutcome.FINISHED
    assert env.agents.calls == []


@pytest.mark.anyio
async def test_referee_waits_out_a_vault_that_is_temporarily_unavailable(store, web_env):
    # 金庫が一時的に応えない(503・通信の失敗)ときは、タスクを落とさず、待って読み直す。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)
    real_vault = env.vault
    failures = [VaultUnavailableError("vault is down", 503)]

    class FlakyVault:
        def __getattr__(self, name):
            return getattr(real_vault, name)

        async def get_view(self, nid, side):
            if failures:
                raise failures.pop()
            return await real_vault.get_view(nid, side)

    await env.restart(vault=FlakyVault())
    env.agents.script("candidate", move_dict("propose", package))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.WAITING
    assert env.agents.calls == []
    assert await referee.step() is StepOutcome.MOVED


@pytest.mark.anyio
async def test_only_the_moves_that_need_a_package_send_one_to_the_vault(store, web_env):
    # propose・check・ask_principal だけが package を伴って登録される。accept・reject・end は、
    # エージェントが余計な package を付けてきても、付けずに登録する(合意する組み合わせは金庫の
    # pending_offer で決まる)。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    offered = sample_package(salary=700)
    other = sample_package(salary=500)
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", move_dict("check", offered), move_dict("propose", offered))
    env.agents.script("employer", move_dict("accept", other))  # accept に別の package を付けてきた

    await drive(env.referee(nid))

    assert [(r.move, r.package) for r in spy.move_requests] == [
        ("check", offered),
        ("propose", offered),
        ("accept", None),
    ]
    assert store.get_view(nid, "candidate").result.package == offered


@pytest.mark.anyio
async def test_an_end_move_finishes_the_negotiation_without_a_result(store, web_env):
    # end(エージェントが終了を選ぶ)は、金庫が終了処理(ended_by_agent)を行い、結果は「なし」だけ。
    env = web_env
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", move_dict("end"))

    outcomes = await drive(env.referee(nid))

    assert outcomes == [StepOutcome.FINISHED]
    view = store.get_view(nid, "candidate")
    assert (view.status, view.result.likelihood, view.result.package) == ("judged", "none", None)
    assert store._negotiation_ref(nid).get().to_dict()["end_reason"] == "ended_by_agent"


@pytest.mark.anyio
async def test_a_manager_starts_one_task_per_negotiation(store, web_env):
    # 交渉ごとに、動いているタスクは 1 つだけ(何度 start しても増えない)。終わったら作り直せる。
    env = web_env
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", move_dict("end"))
    manager = RefereeManager(env.deps)
    context = NegotiationContext(nid=nid, mode="demo", candidate_principal_id=None)

    assert manager.start(context) is True
    assert manager.start(context) is False  # すでに動いている
    await manager.task(nid)
    assert manager.is_running(nid) is False
    assert manager.start(context) is True  # 終わったタスクは数えない(見回りが作り直せる)
    await manager.stop_all()
