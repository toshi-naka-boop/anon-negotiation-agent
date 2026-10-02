"""レフェリーの 1 手ごとの流れ(design.md §4.1)。

呼ぶエージェントの種類(candidate・employer・attacker)、登録する手の中身、409 で状態を読み直すこと、
待つ場面(一時停止・回答待ち・金庫が応えない・金庫の 500 などのエラー。台帳 L9-1)、交渉が消えたときの終わり方、
手数を使い切った側の手番ではエージェントを呼ばないこと(台帳 L9-3)、金庫が送り直しても直らないエラー(404・409 以外の 4xx)を
返し続けたときは、見回りの間隔まで待ってからエージェントを呼び直すこと(台帳 L10-1)を確かめる。
再試行・無効手の登録は tests/test_invalid_move_recovery.py、途中確認の回答は
tests/test_answer_reevaluation.py、再開・見回りは tests/test_referee_resume.py にある。
"""

import asyncio
import datetime as dt

import pytest
from negotiation_core import AttackerTurnInput, TurnInput
from vault.api_models import ControlRequest
from vault.config import DEFAULT_VAULT_CONFIG
from vault.models import EmployerRule
from vault_helpers import needs_confirmation_policy, sample_package
from web.config import DEFAULT_WEB_CONFIG
from web.referee import NegotiationContext, RefereeManager, StepOutcome
from web.vault_client import VaultClientError, VaultConflictError, VaultNotFoundError, VaultUnavailableError
from web_helpers import (
    FakeSleep,
    ScriptedAnswerer,
    create_demo_negotiation,
    create_live_negotiation,
    SpyVault,
    drive,
    move_dict,
    plan_dict,
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
async def test_the_attack_mode_employer_gets_the_instruction_in_both_the_plan_and_the_decision(store, web_env):
    # §4.1・§8.2: 攻撃モードの求人側は、1 手番 2 回の呼び出し(計画・決定)の、どちらにも AttackerTurnInput(principal_instruction つき)を受ける。
    env = web_env
    env.configure(attacker_instruction=lambda nid: "instruction")
    nid = create_demo_negotiation(store, mode="attack")
    package = sample_package()
    env.agents.script("candidate", move_dict("propose", package))
    env.agents.script("attacker", plan_dict(checks=[sample_package(salary=600)]), move_dict("accept"))

    await drive(env.referee(nid, mode="attack"))

    plan_call, decide_call = env.agents.calls_for("attacker")
    assert (plan_call.turn_input.phase, decide_call.turn_input.phase) == ("plan", "decide")
    for call in (plan_call, decide_call):
        assert isinstance(call.turn_input, AttackerTurnInput)
        assert call.turn_input.principal_instruction == "instruction"
    assert [c.package.salary for c in decide_call.turn_input.checked] == [600]  # 確かめの結果も、そのまま渡る
    assert type(env.agents.calls_for("candidate")[0].turn_input) is TurnInput  # 候補者側には、自由文が入る型を渡さない


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


class _FailingOnce:
    """金庫のクライアントを包み、method の最初の 1 回だけ、status の VaultClientError を投げる(金庫の 500 などの再現)。"""

    def __init__(self, inner, method: str, status: int) -> None:
        self._inner = inner
        self._method = method
        self._status = status
        self.failed = 0

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name != self._method:
            return attribute

        async def call(*args, **kwargs):
            if not self.failed:
                self.failed += 1
                raise VaultClientError(f"vault returned {self._status}: boom", self._status)
            return await attribute(*args, **kwargs)

        return call


@pytest.mark.anyio
@pytest.mark.parametrize("status", [500, 502, 422, 403])
@pytest.mark.parametrize("method", ["get_view", "get_events", "post_move"])
async def test_referee_waits_out_a_vault_error_other_than_404_409_and_503_instead_of_crashing(
    store, web_env, method, status
):
    # 台帳 L9-1: 金庫が 404・409・503 以外(500・502 など)を返しても、step() は例外を投げず、待って読み直す(WAITING)。
    # タスクを落とすと、見回りが作り直すまで(最長 60 秒)交渉が止まる。金庫の読み取りの中で出た Aborted が 500 になり得る。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)
    flaky = _FailingOnce(env.vault, method, status)
    await env.restart(vault=flaky)
    env.agents.script("candidate", move_dict("propose", package), move_dict("propose", package))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.WAITING  # 落とさずに、待つ
    assert flaky.failed == 1
    assert await referee.step() is StepOutcome.MOVED  # 読み直して、続ける
    assert [e.kind for e in store.get_events(nid, "candidate")] == ["propose"]  # 二重には登録されていない


@pytest.mark.anyio
async def test_the_run_loop_and_the_task_survive_a_vault_500_and_finish_the_negotiation(store, web_env):
    # 台帳 L9-1: run() も、タスクとして動かしたときも、金庫の 500 で落ちない。間隔を空けて読み直し、交渉を終わりまで進める。
    env = web_env
    nid = create_demo_negotiation(store)
    await env.restart(vault=_FailingOnce(env.vault, "get_view", 500))
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("employer", move_dict("accept"))
    manager = RefereeManager(env.deps)

    assert manager.start(NegotiationContext(nid=nid, mode="demo", candidate_principal_id=None))
    task = manager.task(nid)
    await asyncio.wait_for(task, 30)

    assert task.exception() is None  # 例外で終わっていない
    assert env.sleep.calls == [env.config.wait_poll_interval_seconds]  # 500 の後に 1 回だけ待った
    assert store.get_view(nid, "candidate").status == "judged"


class _AlwaysFailing:
    """金庫のクライアントを包み、method を呼ぶたびに error を投げる(金庫が同じエラーを返し続ける場面の再現)。"""

    def __init__(self, inner, method: str, error: VaultClientError) -> None:
        self._inner = inner
        self._method = method
        self._error = error

    def __getattr__(self, name: str):
        if name != self._method:
            return getattr(self._inner, name)

        async def call(*args, **kwargs):
            raise self._error

        return call


# 送り直しても直らないエラー(404・409 以外の 4xx)と、これまでどおりのもの(5xx・503)。待つ時間は、見回りの間隔と、読み直しの間隔。
_SWEEP_INTERVAL = DEFAULT_WEB_CONFIG.sweeper.interval_seconds
_POLL_INTERVAL = DEFAULT_WEB_CONFIG.referee.wait_poll_interval_seconds
_VAULT_ERRORS_AND_WAITS = [
    pytest.param(VaultClientError("vault returned 422: boom", 422), _SWEEP_INTERVAL, id="422"),
    pytest.param(VaultClientError("vault returned 400: boom", 400), _SWEEP_INTERVAL, id="400"),
    pytest.param(VaultClientError("vault returned 401: boom", 401), _SWEEP_INTERVAL, id="401"),
    pytest.param(VaultClientError("vault returned 403: boom", 403), _SWEEP_INTERVAL, id="403"),
    pytest.param(VaultClientError("vault returned 429: boom", 429), _SWEEP_INTERVAL, id="429"),
    pytest.param(VaultClientError("vault returned 500: boom", 500), _POLL_INTERVAL, id="500"),
    pytest.param(VaultClientError("vault returned 502: boom", 502), _POLL_INTERVAL, id="502"),
    pytest.param(VaultUnavailableError("vault returned 503: down", 503), _POLL_INTERVAL, id="503"),
]


def test_the_wait_after_a_vault_4xx_is_the_sweep_interval():
    # 台帳 L10-1: 送り直しても直らないエラーの後の待ちは、見回りの間隔(暫定 60 秒)。読み直しの間隔(暫定 2 秒)ではない。
    assert (_SWEEP_INTERVAL, _POLL_INTERVAL) == (60, 2)
    assert DEFAULT_WEB_CONFIG.referee.client_error_wait_seconds == DEFAULT_WEB_CONFIG.sweeper.interval_seconds


@pytest.mark.anyio
@pytest.mark.parametrize(("error", "expected_wait"), _VAULT_ERRORS_AND_WAITS)
async def test_the_agent_is_not_called_again_until_the_wait_after_a_vault_error_has_passed(
    store, web_env, error, expected_wait
):
    # 台帳 L10-1: 金庫が、手の登録(post_move)で、同じエラーを返し続ける。読み直すたびに、エージェント(LLM)を呼んでから登録するので、
    # 待ち時間が短いと、LLM を呼び直し続ける。404・409 以外の 4xx(422 など)の後は、見回りの間隔(60 秒)待つまで、エージェントを
    # 呼ばない。5xx・503 の後は、これまでどおり、読み直しの間隔(2 秒)。待ち時間は、止めた sleep(FakeSleep.blocking)で確かめる。
    env = web_env
    nid = create_demo_negotiation(store)
    await env.restart(vault=_AlwaysFailing(env.vault, "post_move", error))
    env.sleep.blocking = True
    env.agents.script("candidate", *[move_dict("propose", sample_package())] * 3)
    run = asyncio.create_task(env.referee(nid).run())
    try:
        await env.sleep.wait_for_calls(1)
        for _ in range(20):  # 待っている間は、エージェントを呼ばない(待ち時間が過ぎるまで、何も起きない)
            await asyncio.sleep(0)
        assert env.sleep.calls == [expected_wait]
        assert len(env.agents.calls) == 1  # 登録に失敗するまでに、エージェントを 1 回だけ呼んだ

        env.sleep.tick()  # 待ち時間が過ぎた
        await env.sleep.wait_for_calls(2)
        assert env.sleep.calls == [expected_wait, expected_wait]
        assert len(env.agents.calls) == 2  # 過ぎてから、次の 1 回を呼んだ
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)


class _StopTheTest(BaseException):
    """テストが、run() のループを止めるための合図(レフェリーの「約束にない例外は無効手にする」処理に飲み込まれないよう BaseException)。"""


@pytest.mark.anyio
async def test_a_vault_422_that_never_goes_away_costs_one_agent_call_per_sweep_interval(store, web_env):
    # 台帳 L10-1: 金庫が 422 を返し続けても、手番の期限(5 分)で終わるまでの間に、エージェント(LLM)を呼ぶのは、見回りの間隔(60 秒)
    # ごとに 1 回(5 回)。2 秒ごとに呼び直していたときは、同じ 5 分で約 150 回だった。時計は、待った秒数だけ進める。
    env = web_env
    nid = create_demo_negotiation(store)
    await env.restart(vault=_AlwaysFailing(env.vault, "post_move", VaultClientError("vault returned 422: boom", 422)))
    sleep = FakeSleep(env.clock, advance=True)
    env.configure(sleep=sleep)
    started = env.clock.now()
    deadline = dt.timedelta(seconds=DEFAULT_VAULT_CONFIG.deadlines.move_deadline_seconds)  # 手番の期限(暫定 5 分)

    def stop_when_the_move_deadline_has_passed(fake_sleep) -> None:
        if env.clock.now() - started >= deadline:
            raise _StopTheTest

    sleep.hook = stop_when_the_move_deadline_has_passed
    env.agents.script("candidate", *[move_dict("propose", sample_package())] * 200)

    with pytest.raises(_StopTheTest):
        await env.referee(nid).run()

    calls_within_the_deadline = int(deadline.total_seconds() // _SWEEP_INTERVAL)  # 300 秒 ÷ 60 秒 = 5 回
    assert sleep.calls == [_SWEEP_INTERVAL] * calls_within_the_deadline
    assert len(env.agents.calls) == calls_within_the_deadline == 5


def _vault_whose_view_fails_with(real_vault, error: VaultClientError):
    """get_view だけが error を投げる金庫のクライアント(ほかの口は、本物のまま)。"""

    class Failing:
        def __getattr__(self, name):
            return getattr(real_vault, name)

        async def get_view(self, nid, side):
            raise error

    return Failing()


@pytest.mark.anyio
async def test_a_404_a_409_and_a_503_keep_their_own_outcomes(store, web_env):
    # 台帳 L9-1 の対照: 404(交渉が消えた)は終わり、409(状態が変わった)は読み直し、503(一時的)は待つ。それ以外だけを
    # 新しく「待つ」にした(区別が崩れていないこと)。
    env = web_env
    nid = create_demo_negotiation(store)
    real_vault = env.vault
    cases = [
        (VaultNotFoundError("gone", 404), StepOutcome.FINISHED),
        (VaultConflictError("changed", 409), StepOutcome.RETRY),
        (VaultUnavailableError("down", 503), StepOutcome.WAITING),
    ]

    for error, expected in cases:
        await env.restart(vault=_vault_whose_view_fails_with(real_vault, error))
        assert await env.referee(nid).step() is expected

    assert env.agents.calls == []


@pytest.mark.anyio
async def test_the_referee_does_not_call_the_agent_when_the_candidates_moves_are_used_up(store, web_env):
    # 台帳 L9-3: 手番が回ってきた側の残りの手数が 0 なら、エージェント(LLM)を呼ばない(呼んでも、金庫は手を受け付けずに
    # stopped_budget で終わらせるので、最長 60 秒のむだになる)。呼ばずに、金庫に手(end)を登録して、停止の判定を効かせる。
    # 候補者が提案して求人側が断る、を 6 回(手数 6)。7 回目の候補者の手番では、エージェントを呼ばずに終わる。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    package = sample_package()
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", *[move_dict("propose", package)] * 6)  # 7 つ目はない: 呼ばれたらテストの誤りとして失敗する
    env.agents.script("employer", *[move_dict("reject")] * 6)

    outcomes = await drive(env.referee(nid), max_steps=20)

    assert outcomes == [StepOutcome.MOVED] * 12 + [StepOutcome.FINISHED]
    assert len(env.agents.calls_for("candidate")) == 6  # 7 回目は呼んでいない
    assert len(env.agents.calls_for("employer")) == 6
    assert store.get_view(nid, "candidate").budget.remaining_moves == 0
    assert (spy.move_requests[-1].side, spy.move_requests[-1].move) == ("candidate", "end")  # 金庫に登録した手
    document = store._negotiation_ref(nid).get().to_dict()
    assert (document["status"], document["end_reason"]) == ("judged", "stopped_budget")  # 金庫の停止の判定が効いた
    view = store.get_view(nid, "candidate")
    assert (view.result.likelihood, view.result.package) == ("none", None)


@pytest.mark.anyio
async def test_the_referee_does_not_call_the_agent_when_the_employers_moves_are_used_up(store, web_env):
    # 台帳 L9-3: 求人側でも同じ。求人側が、スキーマ違反の無効手(手数に数える)を挟みながら 6 手を使い切ると、次の求人側の
    # 手番では、エージェントを呼ばずに終わる。候補者は 3 回提案しただけ(手数は残っている)。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    package = sample_package()
    nid = create_demo_negotiation(store)
    garbage = {"schema": "move/v1", "move": "withdraw"}  # 無効手。有効な手を挟めば、連続無効手の上限(3)には届かない
    env.agents.script("candidate", *[move_dict("propose", package)] * 3)
    env.agents.script("employer", garbage, garbage, move_dict("reject"), garbage, garbage, move_dict("reject"))

    outcomes = await drive(env.referee(nid), max_steps=20)

    assert outcomes[-1] is StepOutcome.FINISHED
    assert len(env.agents.calls_for("candidate")) == 3
    assert len(env.agents.calls_for("employer")) == 6  # 7 回目は呼んでいない
    assert store.get_view(nid, "employer").budget.remaining_moves == 0
    assert (spy.move_requests[-1].side, spy.move_requests[-1].move) == ("employer", "end")
    document = store._negotiation_ref(nid).get().to_dict()
    assert (document["status"], document["end_reason"]) == ("judged", "stopped_budget")


@pytest.mark.anyio
async def test_the_agent_is_still_called_while_the_side_has_a_move_left(store, web_env):
    # 台帳 L9-3 の対照: 残りの手数が 1 つでもあれば、エージェントを呼ぶ(最後の 1 手も、エージェントが打つ)。
    # 候補者の 6 手目(残り 1)で、候補者のエージェントは呼ばれる。
    env = web_env
    package = sample_package()
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", *[move_dict("propose", package)] * 6)
    env.agents.script("employer", *[move_dict("reject")] * 5)
    referee = env.referee(nid)

    for _ in range(11):  # 候補者の提案 6 回と、求人側の断り 5 回
        assert await referee.step() is StepOutcome.MOVED

    assert store.get_view(nid, "candidate").budget.remaining_moves == 0  # 6 手目まで、エージェントが打った
    assert len(env.agents.calls_for("candidate")) == 6


@pytest.mark.anyio
async def test_only_the_moves_that_need_a_package_send_one_to_the_vault(store, web_env):
    # エージェントの手のうち、propose・ask_principal だけが package を伴って登録される(金庫の check は、レフェリーが計画の
    # checks を実行して登録する確かめで、package を伴う)。accept・reject・end は、エージェントが余計な package を付けてきても、
    # 付けずに登録する(合意する組み合わせは金庫の pending_offer で決まる)。
    env = web_env
    spy = SpyVault(env.vault)
    await env.restart(vault=spy)
    offered = sample_package(salary=700)
    other = sample_package(salary=500)
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", plan_dict(checks=[offered]), move_dict("propose", offered))
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
