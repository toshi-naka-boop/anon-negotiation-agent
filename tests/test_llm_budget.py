"""DV-18: LLM に実際に送る回数(物理の呼び出し数)の上限と、作成の入場の制限(design.md §4.1・§8.2・§12.2)。

送る前に、1 日の数と(交渉なら)交渉ごとの数を、`(default)` の 1 つのトランザクションで「上限に達していなければ 1 進める。達していれば
進めずに断る」(web.llm_budget)。エージェントは、送っている最中の状態を記録するスタブ(送っている最中に、永続のカウンタがすでに
進んでいることを見る。台帳 X-54)。金庫は本物(Firestore エミュレータ)だが、別の作業者が作る金庫の新しい口(control の
stop_cost_limit・by-request)は、偽の金庫(CostLimitVault)で代用する: stop_cost_limit は取消(cancel)で終わらせる(結果は双方に
「なし」。最終記録は双方に 1 件ずつ)、by-request は作成が成功した request_id から答える(契約: 200 {"nid"} か 404)。

設計書 §12.2 の DV-18 の項目と、このファイルのテスト:
- 計画・決定・面談・壁 1 の生メッセージのすべてで、送る前に数が 1 進む。再試行も 1 回と数え、429・5xx でも戻らない
  → test_the_counters_advance_before_each_send_of_the_plan_and_the_decision・
  test_a_retry_counts_once_more_and_a_429_or_a_5xx_never_rolls_the_count_back・
  test_the_interview_and_raw_message_paths_count_only_the_daily_number(それらの経路そのものは後の段。数える関数は共通)
- 金庫の作成の直後に web が落ちても、同じ request_id の再送は同じ交渉を返す
  → test_a_resend_after_the_vault_created_the_negotiation_but_web_fell_returns_the_same_negotiation
- 面談の入力が 32 KB を超えると拒否され、面談エージェントの要求に max_output_tokens が付く → 面談の経路がまだないので、ここでは確かめない
- 交渉ごとの上限(44)で、次の呼び出しは送られず、control{stop_cost_limit} で「なし」になり、ログに残る。直前の操作が 409 になった後・
  途中確認中・一時停止中に 1 日の上限に達した交渉(再開後の最初の送信の前)でも止まり、最終記録は 1 件
  → test_a_negotiation_stops_at_the_per_negotiation_limit_and_the_stop_is_logged・test_the_stop_works_after_a_409・
  test_the_stop_works_while_the_negotiation_is_paused・test_the_stop_works_for_a_negotiation_that_was_awaiting_a_question・
  test_a_paused_negotiation_stops_before_its_first_send_after_the_resume_when_the_day_was_used_up
- 出力が max_output_tokens で切れた呼び出しは output_truncated の無効手になり、次の TurnInput.last_error に入る
  → test_a_truncated_output_is_an_output_truncated_invalid_move_and_reaches_the_next_last_error
- 1 日の上限に達すると、面談と生メッセージは断られ、進行中の交渉は次の呼び出しの前で止まる
  → test_the_daily_limit_stops_a_running_negotiation_and_refuses_the_interview_and_raw_message_paths
- カウンタに書けない(Firestore の失敗の模擬)ときは送られず、回復すると続く
  → test_a_counter_that_cannot_be_written_blocks_the_send_and_the_send_goes_on_after_it_recovers
- 作成の入場の制限(「その日の物理の数 ＋ 進行中の未消化分 ＋ 44」が枠を超えるときだけ断られる。同じ request_id の再送は同じ交渉。金庫が断った作成・
  入場で断られた作成は何も影響しない。作成した交渉はその場で未消化分に入る。起動時の見回りが終わるまで 503)
  → test_creation_is_refused_only_when_*・test_a_resent_request_id_*・test_a_creation_refused_*・test_a_negotiation_created_just_now_*・
  test_creation_waits_for_the_first_sweep_*・test_a_failure_to_count_*
- アプリを作り直しても数えた値が残る → test_the_counted_values_survive_a_restart
- 日付が変わると 1 日の数は 0 に戻り、持ち越した交渉の呼び出しは翌日の数に入る
  → test_the_day_counter_starts_again_at_japan_midnight_and_a_carried_over_negotiation_counts_in_the_next_day

出力の切れ(output_truncated)のテストは、金庫が、その理由の無効手の登録を受け付けるまで、スキップする。
"""

import asyncio
import dataclasses
import datetime as dt
import logging
import re
import typing
from pathlib import Path

import pytest
from vault.api_models import ControlRequest
from vault.models import RegisteredInvalidReason
from vault_helpers import (
    needs_confirmation_policy,
    put_candidate_and_employer_templates,
    reject_all_policy,
    sample_package,
)
from web.config import DEFAULT_WEB_CONFIG, load_web_config
from web.llm_budget import LLM_COUNTERS_COLLECTION, LlmBudget, jst_date
from web.referee import StepOutcome
from web_app_helpers import build_web_env, wait_until
from web_helpers import ScriptedAnswerer, SimulatedCrash, create_demo_negotiation, move_dict, plan_dict

A = sample_package(salary=500)
NO_ID_IN_LOGS = "the logs must not carry the negotiation id"


class TruncatedOutputError(ValueError):
    """出力が max_output_tokens で切れた(agents.client.TruncatedOutputError の代わり。属性 usage を持つ ValueError。台帳 C-53)。"""

    def __init__(self, message: str, usage=None) -> None:
        super().__init__(message)
        self.usage = usage


class CostLimitVault:
    """金庫のクライアントを包む「偽の金庫」。金庫の新しい口の代用(契約は design.md §3.3・§3.4)。

    - stop_cost_limit(nid): 呼ばれた nid を記録し、取消(cancel)で終わらせる(金庫の stop_cost_limit と同じく、どの状態からでも効き、
      結果は双方に「なし」)。
    - get_negotiation_by_request(request_id): create_negotiation が成功した request_id → nid から答える(なければ None = 404)。
    - crash_after_create=True なら、create_negotiation を金庫に通したあと、web が落ちたことにする(障害の注入)。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.stop_calls: list[str] = []
        self.known_requests: dict[str, str] = {}
        self.crash_after_create = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def stop_cost_limit(self, nid):
        self.stop_calls.append(nid)
        return await self._inner.control(nid, ControlRequest(side="candidate", action="cancel"))

    async def get_negotiation_by_request(self, request_id):
        return self.known_requests.get(request_id)

    async def create_negotiation(self, request):
        response = await self._inner.create_negotiation(request)
        if response.status == "created" and response.nid is not None:
            self.known_requests[request.request_id] = response.nid
        if self.crash_after_create:
            raise SimulatedCrash
        return response


def _budget(env, **limits) -> LlmBudget:
    """env の (default)・時計で、上限だけを替えた物理の呼び出し数の計上。"""
    return LlmBudget(env.default_db, env.clock, dataclasses.replace(DEFAULT_WEB_CONFIG.llm_budget, **limits))


async def _negotiation(store, env, **kwargs) -> str:
    """金庫に架空人物どうしの交渉を作り、段の状態(交渉ごとの数を持つ)も作る。"""
    nid = create_demo_negotiation(store, **kwargs)
    await env.stages.ensure(nid, None)
    return nid


def _assert_stopped_with_one_final_record_per_side(store, nid: str) -> None:
    """交渉が終わっていて、最終記録が双方に 1 件ずつ(どちらも「なし」だけ)。"""
    assert store.get_view(nid, "candidate").status == "judged"
    for side in ("candidate", "employer"):
        finals = [e for e in store.get_events(nid, side) if e.kind == "final_result"]
        assert len(finals) == 1
        assert (finals[0].result.likelihood, finals[0].result.package) == ("none", None)


# ----------------------------------------------------------------------
# 設定([web.llm_budget])
# ----------------------------------------------------------------------


def test_the_config_holds_the_design_limits():
    # §4.1・§8.2: 1 日 1,500 回・交渉ごと 44 回(正常な上限 28 ＋ 余裕 16)・1 日のカウンタの TTL は 7 日・1 回の計画から確かめるのは 3 つまで。
    budget = DEFAULT_WEB_CONFIG.llm_budget
    assert (budget.daily_limit, budget.per_negotiation_limit) == (1500, 44)
    assert budget.daily_counter_ttl_seconds == 7 * 24 * 3600
    assert budget.max_checks_per_plan == 3


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [("max_checks_per_plan", 4), ("max_checks_per_plan", 0), ("daily_limit", 0), ("per_negotiation_limit", 0)],
)
def test_the_config_rejects_limits_that_do_not_make_sense(tmp_path: Path, key, bad_value):
    # 計画の確かめは 3 つまで(Plan のスキーマの上限。§2.7)。上限が 0 以下では、何も送れない(黙って止まる)ので、読み込みで拒否する。
    source = Path(__file__).resolve().parents[1] / "config" / "params.toml"
    text = source.read_text(encoding="utf-8")
    broken = re.sub(rf"^{key} = \d+", f"{key} = {bad_value}", text, flags=re.MULTILINE)
    assert broken != text
    path = tmp_path / "params.toml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ValueError, match="llm_budget"):
        load_web_config(path)


# ----------------------------------------------------------------------
# 送る前に数が進む。再試行も数え、429・5xx でも戻らない。面談・壁 1 は 1 日の数だけ
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_counters_advance_before_each_send_of_the_plan_and_the_decision(store, web_env):
    # 送っている最中(スタブが呼ばれている最中)に、永続のカウンタがすでに進んでいる。計画の送信で 1、決定の送信で 2。
    env = web_env
    budget = _budget(env)
    env.configure(llm_budget=budget)
    nid = await _negotiation(store, env)
    seen: list[tuple[str, int, int]] = []

    def at_send(payload):
        async def item(call):
            seen.append((call.turn_input.phase, await budget.daily_count(), await budget.negotiation_count(nid)))
            return payload

        return item

    env.agents.script("candidate", at_send(plan_dict(checks=[A])), at_send(move_dict("end")))

    await env.referee(nid).step()

    assert seen == [("plan", 1, 1), ("decide", 2, 2)]
    document = env.default_db.collection(LLM_COUNTERS_COLLECTION).document(jst_date(env.clock.now())).get().to_dict()
    assert document["count"] == 2 and document["ttl_at"] == env.clock.now() + dt.timedelta(days=7)  # 7 日の TTL


@pytest.mark.anyio
async def test_the_counters_are_already_advanced_while_a_send_is_held_in_flight(store, web_env):
    # 送信を止めて待つスタブ: 応答が返らない間に、外から永続のカウンタを読む。送る前に数えてあるので、応答を待っている間も、すでに進んでいる
    # (応答が返ってからの計上ではない)。止めた送信を放すと、手番は続き、決定の送信でも同じことが起きる。
    env = web_env
    budget = _budget(env)
    env.configure(llm_budget=budget)
    nid = await _negotiation(store, env)
    hold = asyncio.Event()

    async def held_plan(call):
        await hold.wait()
        return plan_dict(checks=[A])

    env.agents.script("candidate", held_plan, move_dict("end"))
    step = asyncio.create_task(env.referee(nid).step())
    try:
        await wait_until(lambda: len(env.agents.calls) == 1)  # 計画の送信が、応答を待っている
        assert (await budget.daily_count(), await budget.negotiation_count(nid)) == (1, 1)
        assert not step.done()
    finally:
        hold.set()
    assert await step is StepOutcome.FINISHED
    assert (await budget.daily_count(), await budget.negotiation_count(nid)) == (2, 2)  # 決定の送信の分も


@pytest.mark.anyio
async def test_a_retry_counts_once_more_and_a_429_or_a_5xx_never_rolls_the_count_back(store, web_env):
    # レフェリーの再試行も 1 回と数える。429・5xx(エージェントの一時的なエラー。ConnectionError)が返っても、計上は戻さない。
    env = web_env
    budget = _budget(env)
    env.configure(llm_budget=budget)
    nid = await _negotiation(store, env)
    seen: list[int] = []

    async def third_send(call):
        seen.append(await budget.negotiation_count(nid))
        return plan_dict(move="end")

    env.agents.script("candidate", ConnectionError("429"), TimeoutError("503"), third_send)

    assert await env.referee(nid).step() is StepOutcome.FINISHED

    assert len(env.agents.calls) == 3
    assert seen == [3]  # 3 回目を送っている最中には、失敗した 2 回もすでに数えてある
    assert await budget.negotiation_count(nid) == 3 and await budget.daily_count() == 3  # 失敗した送信も、戻っていない


@pytest.mark.anyio
async def test_the_interview_and_raw_message_paths_count_only_the_daily_number(store, web_env):
    # 面談の LLM 呼び出しと壁 1 の生メッセージは、交渉を持たない。経路そのものは後の段で作るが、数える関数(reserve(None))は共通で、
    # 1 日の数だけを進める(交渉ごとの数には触れない)。
    env = web_env
    budget = _budget(env, daily_limit=2)
    nid = await _negotiation(store, env)

    assert (await budget.reserve(None)).granted
    assert (await budget.reserve(None)).granted
    refused = await budget.reserve(None)

    assert (refused.granted, refused.refused_by) == (False, "daily")  # 3 回目は、進めずに断る
    assert await budget.daily_count() == 2  # 断った分は進んでいない
    assert await budget.negotiation_count(nid) == 0


@pytest.mark.anyio
async def test_concurrent_reservations_never_push_the_counters_past_the_limits(store, web_env):
    # 「上限に達していなければ 1 進める。達していれば進めずに断る」は、1 つのトランザクションで行う。並行して計上しても、通るのは
    # 上限までで、カウンタは通った分だけ進む(書き込みが競合して数えられなかったものは、LlmBudgetUnavailable で、送らない側)。
    env = web_env
    budget = _budget(env, per_negotiation_limit=2, daily_limit=1500)
    nid = await _negotiation(store, env)

    results = await asyncio.gather(*[budget.reserve(nid) for _ in range(4)], return_exceptions=True)

    granted = [r for r in results if not isinstance(r, BaseException) and r.granted]
    assert len(granted) <= 2
    assert await budget.negotiation_count(nid) == len(granted) == await budget.daily_count()
    for _ in range(3):  # 断られた・書けなかった分を、順に重ねても、上限(2)を超えない
        try:
            await budget.reserve(nid)
        except BaseException:  # noqa: BLE001  書けなかった分(LlmBudgetUnavailable)は、また次の回に任せる
            pass
    assert await budget.negotiation_count(nid) == 2 == await budget.daily_count()


# ----------------------------------------------------------------------
# 交渉ごとの上限で止まる(control{stop_cost_limit})。ログに残る(値は含まない)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_negotiation_stops_at_the_per_negotiation_limit_and_the_stop_is_logged(store, web_env, caplog):
    # 上限(ここでは 2)まで送った後の次の呼び出しは、送られない。金庫の control{stop_cost_limit} で「なし」になり、ログに残る。
    caplog.set_level(logging.INFO, logger="web")
    env = web_env
    fake = CostLimitVault(env.vault)
    await env.restart(vault=fake)
    budget = _budget(env, per_negotiation_limit=2)
    env.configure(llm_budget=budget)
    nid = await _negotiation(store, env, candidate_policy=reject_all_policy("candidate"))
    env.agents.script("candidate", *[move_dict("propose", sample_package())] * 3)  # どれも、ガードで断られる無効手
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED
    assert await referee.step() is StepOutcome.MOVED
    assert fake.stop_calls == []
    assert await referee.step() is StepOutcome.FINISHED  # 3 回目は、送らずに止める

    assert len(env.agents.calls) == 2  # 3 回目は送られていない
    assert fake.stop_calls == [nid]
    assert await budget.negotiation_count(nid) == 2  # 断った分は進んでいない
    _assert_stopped_with_one_final_record_per_side(store, nid)
    assert "llm call limit reached; stopping the negotiation limit=negotiation" in caplog.text
    assert nid not in caplog.text, NO_ID_IN_LOGS  # ログには、どちらの上限かだけ。交渉 ID も値も書かない


@pytest.mark.anyio
async def test_the_stop_works_after_a_409(store, web_env):
    # 直前の操作が 409 になった後: エージェントを呼んでいる間に一時停止と再開が入り、手の登録が 409 になる(RETRY)。
    # 次の読み直しの後の送信が、上限に達していて断られれば、そのまま止まる。
    env = web_env
    real = env.vault
    fake = CostLimitVault(real)
    await env.restart(vault=fake)
    env.configure(llm_budget=_budget(env, per_negotiation_limit=1))
    nid = await _negotiation(store, env)

    async def pause_and_resume_during_the_call(call):
        await real.control(nid, ControlRequest(side="candidate", action="pause"))
        await real.control(nid, ControlRequest(side="candidate", action="resume"))
        return move_dict("propose", sample_package())

    env.agents.script("candidate", pause_and_resume_during_the_call)
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.RETRY  # 登録は 409
    assert await referee.step() is StepOutcome.FINISHED  # 次の送信は、上限(1 回)に達していて断られ、止まる

    assert fake.stop_calls == [nid] and len(env.agents.calls) == 1
    _assert_stopped_with_one_final_record_per_side(store, nid)


@pytest.mark.anyio
async def test_the_stop_works_while_the_negotiation_is_paused(store, web_env):
    # レフェリーが状態を読んだ後・送る前に、一時停止が入った(その状態で、上限に達していて断られる)。止める操作は、一時停止中でも効く
    # (手の `end` では、一時停止中は 409 で止められない。台帳 X-52)。
    env = web_env
    real = env.vault
    fake = CostLimitVault(real)
    await env.restart(vault=fake)
    budget = _budget(env, daily_limit=1)
    assert (await budget.reserve(None)).granted  # 1 日の枠は、すでに使い切っている

    class PauseBeforeRefusal:
        def __getattr__(self, name):
            return getattr(budget, name)

        async def reserve(self, nid):
            await real.control(nid, ControlRequest(side="candidate", action="pause"))
            return await budget.reserve(nid)

    env.configure(llm_budget=PauseBeforeRefusal())
    nid = await _negotiation(store, env)

    assert await env.referee(nid).step() is StepOutcome.FINISHED

    assert fake.stop_calls == [nid] and env.agents.calls == []
    assert store._negotiation_ref(nid).get().to_dict()["paused"] is True  # 止めたとき、一時停止中だった
    _assert_stopped_with_one_final_record_per_side(store, nid)


@pytest.mark.anyio
async def test_the_stop_works_for_a_negotiation_that_was_awaiting_a_question(store, web_env):
    # 途中確認の間は、何も送らない。回答が届いた後の、最初の送信の前で、上限に達していれば止まる。
    env = web_env
    fake = CostLimitVault(env.vault)
    await env.restart(vault=fake)
    env.configure(llm_budget=_budget(env, per_negotiation_limit=1), answerer=ScriptedAnswerer("accept"))
    nid = await _negotiation(store, env, candidate_policy=needs_confirmation_policy("candidate"))
    env.agents.script("candidate", move_dict("ask_principal", sample_package()))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED  # 計画の 1 回で、途中確認を登録した(交渉ごとの上限の 1 回を使い切った)
    assert store.get_view(nid, "candidate").status == "awaiting_principal"
    assert await referee.step() is StepOutcome.ANSWERED  # 架空人物の自動回答。LLM は呼ばない
    assert await referee.step() is StepOutcome.FINISHED  # 回答の後の最初の送信の前で止まる

    assert fake.stop_calls == [nid] and len(env.agents.calls) == 1
    _assert_stopped_with_one_final_record_per_side(store, nid)


@pytest.mark.anyio
async def test_a_paused_negotiation_stops_before_its_first_send_after_the_resume_when_the_day_was_used_up(
    store, web_env
):
    # 一時停止中に(ほかの交渉などで)1 日の上限に達した交渉は、再開後の最初の送信の前で止まる。最終記録は 1 件。
    env = web_env
    fake = CostLimitVault(env.vault)
    await env.restart(vault=fake)
    budget = _budget(env, daily_limit=2)
    env.configure(llm_budget=budget)
    nid = await _negotiation(store, env)
    await env.vault.control(nid, ControlRequest(side="candidate", action="pause"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.WAITING  # 一時停止中は、送らずに待つ
    for _ in range(2):  # ほかの交渉が、1 日の枠を使い切る
        assert (await budget.reserve(None)).granted
    await env.vault.control(nid, ControlRequest(side="candidate", action="resume"))

    assert await referee.step() is StepOutcome.FINISHED  # 再開後の最初の送信の前で止まる

    assert env.agents.calls == []  # 一度も送っていない
    assert fake.stop_calls == [nid]
    _assert_stopped_with_one_final_record_per_side(store, nid)


@pytest.mark.anyio
async def test_the_referee_logs_the_physical_count_of_a_negotiation_when_it_finishes(store, web_env, caplog):
    # §8.2: レフェリーは、交渉ごとの物理の呼び出し数を、終了時にログに残す(値は含まない数だけ。交渉 ID は書かない)。
    caplog.set_level(logging.INFO, logger="web.referee")
    env = web_env
    env.configure(llm_budget=_budget(env))
    nid = await _negotiation(store, env)
    env.agents.script("candidate", plan_dict(checks=[A]), move_dict("end"))

    await env.referee(nid).run()

    assert "negotiation finished llm_calls=2" in caplog.text
    assert nid not in caplog.text, NO_ID_IN_LOGS


# ----------------------------------------------------------------------
# 出力が切れた呼び出しは output_truncated の無効手になり、次の TurnInput.last_error に入る
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.skipif(
    "output_truncated" not in typing.get_args(RegisteredInvalidReason),
    reason="the vault does not accept output_truncated as a registered invalid reason yet (a vault change)",
)
async def test_a_truncated_output_is_an_output_truncated_invalid_move_and_reaches_the_next_last_error(store, web_env):
    # agents の受信口が出力の切れを印を付けて返す(TruncatedOutputError。ValueError の一種)と、レフェリーは、schema_invalid ではなく
    # output_truncated の無効手として登録する(再試行はしない)。次の TurnInput.last_error に入り、短く答える手がかりになる。
    env = web_env
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", TruncatedOutputError("cut at max_output_tokens"), plan_dict(move="end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED

    assert [(e.kind, e.reason) for e in store.get_events(nid, "candidate")] == [("invalid", "output_truncated")]
    assert len(env.agents.calls) == 1 and env.sleep.calls == []  # 再試行しない
    assert await referee.step() is StepOutcome.FINISHED
    assert env.agents.calls[1].turn_input.last_error == "output_truncated"


# ----------------------------------------------------------------------
# 1 日の上限。カウンタに書けないときは送らない
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_daily_limit_stops_a_running_negotiation_and_refuses_the_interview_and_raw_message_paths(
    store, web_env
):
    env = web_env
    fake = CostLimitVault(env.vault)
    await env.restart(vault=fake)
    budget = _budget(env, daily_limit=2)
    env.configure(llm_budget=budget)
    nid = await _negotiation(store, env, candidate_policy=reject_all_policy("candidate"))
    env.agents.script("candidate", *[move_dict("propose", sample_package())] * 3)
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.MOVED
    assert await referee.step() is StepOutcome.MOVED  # 1 日の枠の 2 回を使い切った
    refused_path = await budget.reserve(None)  # 面談・壁 1 の生メッセージも、同じ枠で断られる
    assert (refused_path.granted, refused_path.refused_by) == (False, "daily")
    assert await referee.step() is StepOutcome.FINISHED  # 進行中の交渉は、次の呼び出しの前で止まる

    assert len(env.agents.calls) == 2 and fake.stop_calls == [nid]
    _assert_stopped_with_one_final_record_per_side(store, nid)


@pytest.mark.anyio
async def test_a_counter_that_cannot_be_written_blocks_the_send_and_the_send_goes_on_after_it_recovers(
    store, web_env, monkeypatch
):
    # Firestore の失敗の模擬: カウンタに書けないときは、送らず、金庫が応えないときと同じく待って読み直す(閉じる側)。回復すると続く。
    env = web_env
    budget = _budget(env)
    env.configure(llm_budget=budget)
    nid = await _negotiation(store, env)
    real_reserve = budget._reserve_sync
    failures = [RuntimeError("firestore is down"), RuntimeError("firestore is down")]

    def flaky(nid_):
        if failures:
            raise failures.pop()
        return real_reserve(nid_)

    monkeypatch.setattr(budget, "_reserve_sync", flaky)
    env.agents.script("candidate", plan_dict(move="end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.WAITING
    assert await referee.step() is StepOutcome.WAITING
    assert env.agents.calls == []  # 書けない間は、1 回も送っていない
    assert await budget.negotiation_count(nid) == 0

    assert await referee.step() is StepOutcome.FINISHED  # 回復した
    assert len(env.agents.calls) == 1 and await budget.negotiation_count(nid) == 1


@pytest.mark.anyio
async def test_a_stage_that_does_not_exist_yet_blocks_the_send_until_it_is_created(store, web_env):
    # 交渉ごとの数は、段の状態 stages/{nid} の項目に持つ。作り損ねている間(見回りが作る)は、数えられないので、送らずに待つ。
    env = web_env
    env.configure(llm_budget=_budget(env))
    nid = create_demo_negotiation(store)
    env.agents.script("candidate", plan_dict(move="end"))
    referee = env.referee(nid)

    assert await referee.step() is StepOutcome.WAITING
    assert env.agents.calls == []

    await env.stages.ensure(nid, None)
    assert await referee.step() is StepOutcome.FINISHED


# ----------------------------------------------------------------------
# アプリを作り直しても残る・日付が変わると 0 に戻る
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_counted_values_survive_a_restart(store, web_env, clock, vault_client, default_db, session_key):
    env = web_env
    env.configure(llm_budget=_budget(env))
    nid = await _negotiation(store, env)
    env.agents.script("candidate", plan_dict(checks=[A]), move_dict("end"))
    await env.referee(nid).step()

    # web を作り直す(新しいアプリ一式。同じ (default))。数えた値は、Firestore にあるので、残っている。
    restarted = _admission_env(store, clock, vault_client, default_db, session_key, daily_limit=1500)
    try:
        budget = restarted.services.llm_budget
        assert await budget.daily_count() == 2 and await budget.negotiation_count(nid) == 2
        assert (await budget.reserve(nid)).granted  # 続きから数える
        assert await budget.negotiation_count(nid) == 3 and await budget.daily_count() == 3
    finally:
        await restarted.aclose()


@pytest.mark.anyio
async def test_the_day_counter_starts_again_at_japan_midnight_and_a_carried_over_negotiation_counts_in_the_next_day(
    store, web_env
):
    env = web_env
    budget = _budget(env)
    nid = await _negotiation(store, env)
    env.clock.set(dt.datetime(2026, 1, 1, 14, 59, tzinfo=dt.timezone.utc))  # 日本時間の 1/1 23:59
    assert jst_date(env.clock.now()) == "2026-01-01"
    assert (await budget.reserve(nid)).granted

    env.clock.advance(dt.timedelta(minutes=2))  # 日本時間の 1/2 0:01
    assert jst_date(env.clock.now()) == "2026-01-02"
    assert await budget.daily_count() == 0  # 日付が変わると、1 日の数は 0 に戻る
    assert await budget.negotiation_count(nid) == 1  # 日付をまたいで動く交渉の数は、残る
    assert (await budget.reserve(nid)).granted  # 翌日の呼び出しは、翌日の数に入る

    counters = env.default_db.collection(LLM_COUNTERS_COLLECTION)
    assert counters.document("2026-01-01").get().to_dict()["count"] == 1
    assert counters.document("2026-01-02").get().to_dict()["count"] == 1
    assert await budget.negotiation_count(nid) == 2


# ----------------------------------------------------------------------
# 作成の入場の制限(画面 API)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_production_assembly_wires_the_budget_into_the_referee_with_the_configured_limits(web_app):
    # 本番の組み立て(web.services)は、物理の呼び出し数の計上を、レフェリーと入場の制限の両方に、設定ファイルの上限(1 日 1,500・
    # 交渉ごと 44)で渡す。渡し忘れると、費用の歯止めが黙って外れる。
    services = web_app.services
    assert services.referees._deps.llm_budget is services.llm_budget
    assert services.referees._deps.max_checks_per_plan == 3
    assert (services.config.llm_budget.daily_limit, services.config.llm_budget.per_negotiation_limit) == (1500, 44)
    assert services.llm_budget._config == services.config.llm_budget


def _admission_env(store, clock, vault, default_db, session_key, *, daily_limit, per_negotiation_limit=10, **kwargs):
    """上限を小さくした web の app 一式。レフェリーも動かす(エージェントは、台本がなければ応答しない IdleAgents)。"""
    llm_budget = dataclasses.replace(
        DEFAULT_WEB_CONFIG.llm_budget, daily_limit=daily_limit, per_negotiation_limit=per_negotiation_limit
    )
    return build_web_env(
        store=store,
        clock=clock,
        vault=vault,
        default_db=default_db,
        session_key=session_key,
        config=dataclasses.replace(DEFAULT_WEB_CONFIG, llm_budget=llm_budget),
        run_referees=True,
        **kwargs,
    )


def _demo_request(store, request_id: str) -> dict:
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    return {
        "request_id": request_id,
        "candidate_template_id": candidate_template.template_id,
        "employer_template_id": employer_template.template_id,
    }


async def _create_demo_and_wait_for_its_first_send(env, browser, body: dict) -> str:
    """デモの交渉を作り、レフェリーが最初の送信を数えて応答待ちに入る(エージェントの呼び出しが 1 つ増える)まで待つ。"""
    calls_before = len(env.agents.calls)
    response = await browser.post("/v1/demo/negotiations", body)
    assert response.status_code == 200, response.text
    await wait_until(lambda: len(env.agents.calls) == calls_before + 1)
    return response.json()["nid"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("daily_limit", "third_status"), [(30, 200), (29, 429)], ids=["exactly_at_the_limit", "over_the_limit"]
)
async def test_creation_is_refused_only_when_the_days_count_plus_the_unconsumed_part_plus_one_negotiation_exceed_the_limit(
    store, clock, vault_client, default_db, session_key, daily_limit, third_status
):
    # 「その日の物理の数 ＋ 進行中の交渉の未消化分(10 − その交渉の数)の合計 ＋ 10(新しい交渉 1 件ぶん)」が枠を超えるときだけ断る。
    # 1 件目: 0 + 0 + 10。2 件目: 1 + 9 + 10 = 20。3 件目: 2 + 9 + 9 + 10 = 30(枠 30 なら受け付け、29 なら断る)。
    env = _admission_env(store, clock, vault_client, default_db, session_key, daily_limit=daily_limit)
    try:
        browser = env.browser()
        await _create_demo_and_wait_for_its_first_send(env, browser, _demo_request(store, "request-demo1"))
        await _create_demo_and_wait_for_its_first_send(env, browser, _demo_request(store, "request-demo2"))

        third = await browser.post("/v1/demo/negotiations", _demo_request(store, "request-demo3"))

        assert third.status_code == third_status, third.text
        if third_status == 429:
            assert third.json() == {"detail": "daily_limit_reached"}  # 画面は「本日の上限に達しました」と出す
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_a_resent_request_id_returns_the_same_negotiation_even_when_the_day_is_full(
    store, clock, vault_client, default_db, session_key
):
    # 枠が埋まっていても、同じ request_id の再送は、入場の判定を通さずに、同じ交渉を返す(冪等キーの正本は金庫の by-request)。
    fake = CostLimitVault(vault_client)
    env = _admission_env(store, clock, fake, default_db, session_key, daily_limit=19)
    try:
        browser = env.browser()
        first_body = _demo_request(store, "request-demo1")
        nid = await _create_demo_and_wait_for_its_first_send(env, browser, first_body)
        assert (await browser.post("/v1/demo/negotiations", _demo_request(store, "request-demo2"))).status_code == 429

        again = await browser.post("/v1/demo/negotiations", first_body)

        assert (again.status_code, again.json()) == (200, {"nid": nid})
        assert [item.nid for item in store.list_open_negotiations().items] == [nid]  # 二重には作っていない
    finally:
        await env.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("refused_by", ["the_vault", "the_admission"])
async def test_a_creation_refused_by_the_vault_or_by_the_admission_changes_neither_the_counters_nor_the_next_decision(
    store, clock, vault_client, default_db, session_key, refused_by
):
    # 金庫が断った作成(already_active)と、入場で断られた作成(429)は、何も書かない: 1 日の数も、交渉ごとの数も変わらず、
    # 次の入場の判定にも影響しない(予約の記録を持たない。台帳 C-45・X-53)。
    fake = CostLimitVault(vault_client)
    # 金庫が断る場合は、入場を通る枠(1 + 9 + 10 ≤ 45)。入場で断る場合は、通らない枠(1 + 9 + 10 > 19)。
    env = _admission_env(store, clock, fake, default_db, session_key, daily_limit=45 if refused_by == "the_vault" else 19)
    try:
        browser = env.browser()
        pid = await browser.register()
        first_template, second_template = env.put_employer_template(), env.put_employer_template()
        first = await browser.post(
            f"/v1/principals/{pid}/negotiations", {"request_id": "request-0001", "employer_template_id": first_template}
        )
        assert first.status_code == 200
        await wait_until(lambda: len(env.agents.calls) == 1)
        nid = first.json()["nid"]
        budget = env.services.llm_budget
        running = env.services.referees.running_nids

        async def snapshot():
            return await budget.daily_count(), await budget.negotiation_count(nid), await budget.admits_new_negotiation(running())

        before = await snapshot()

        refused = await browser.post(
            f"/v1/principals/{pid}/negotiations", {"request_id": "request-0002", "employer_template_id": second_template}
        )

        expected = (409, {"detail": "already_active"}) if refused_by == "the_vault" else (429, {"detail": "daily_limit_reached"})
        assert (refused.status_code, refused.json()) == expected
        assert await snapshot() == before  # 数も、次の判定も、変わらない
        assert before[:2] == (1, 1)
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_a_negotiation_created_just_now_counts_as_unconsumed_before_the_next_sweep(
    store, clock, vault_client, default_db, session_key
):
    # 作成した交渉は、見回りを待たずに、その場で進行中の一覧(レフェリーのタスクの一覧)に入り、未消化分を数える。
    # 枠 19: 1 件目は 0 + 0 + 10 で入れる。作った直後の 2 件目は 0 + 10 + 10 = 20 > 19 で断られる(未消化分を数えなければ入ってしまう)。
    env = _admission_env(store, clock, vault_client, default_db, session_key, daily_limit=19)
    try:
        browser = env.browser()
        created = await browser.post("/v1/demo/negotiations", _demo_request(store, "request-demo1"))
        assert created.status_code == 200
        assert created.json()["nid"] in env.services.referees.running_nids()  # 見回り(sweep_once)は、まだ 1 回も動いていない

        second = await browser.post("/v1/demo/negotiations", _demo_request(store, "request-demo2"))

        assert (second.status_code, second.json()) == (429, {"detail": "daily_limit_reached"})
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_creation_waits_for_the_first_sweep_and_resends_are_answered_meanwhile(
    store, clock, vault_client, default_db, session_key
):
    # 起動時の見回りが終わるまで、新規の作成は 503(進行中の一覧が、まだそろっていない)。既知の request_id の再送は、その間も、
    # 同じ交渉を返す(by-request が、起動時の 503 より先)。見回りが終われば、新規も受け付ける。
    fake = CostLimitVault(vault_client)
    first_env = _admission_env(store, clock, fake, default_db, session_key, daily_limit=1500)
    try:
        known_body = _demo_request(store, "request-demo1")
        nid = (await first_env.browser().post("/v1/demo/negotiations", known_body)).json()["nid"]
    finally:
        await first_env.aclose()

    env = _admission_env(store, clock, fake, default_db, session_key, daily_limit=1500, startup_sweep_done=False)
    try:
        assert not env.services.sweeper.first_sweep_done
        browser = env.browser()

        new = await browser.post("/v1/demo/negotiations", _demo_request(store, "request-demo2"))
        resent = await browser.post("/v1/demo/negotiations", known_body)

        assert (new.status_code, new.json()) == (503, {"detail": "starting_up"})
        assert (resent.status_code, resent.json()) == (200, {"nid": nid})

        await env.services.sweeper.sweep_once()  # 起動時の見回りが終わる
        assert env.services.sweeper.first_sweep_done
        after = await browser.post("/v1/demo/negotiations", _demo_request(store, "request-demo3"))
        assert after.status_code == 200
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_a_failure_to_count_refuses_the_creation_with_a_503(
    store, clock, vault_client, default_db, session_key, monkeypatch
):
    # 入場の制限を数えられない(Firestore の失敗)ときは、断る(閉じる側)。何も作らない。
    env = _admission_env(store, clock, vault_client, default_db, session_key, daily_limit=1500)
    try:

        def broken():
            raise RuntimeError("firestore is down")

        monkeypatch.setattr(env.services.llm_budget, "_daily_count_sync", broken)

        response = await env.browser().post("/v1/demo/negotiations", _demo_request(store, "request-demo1"))

        assert (response.status_code, response.json()) == (503, {"detail": "temporarily_unavailable"})
        assert store.list_open_negotiations().items == []
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_a_resend_after_the_vault_created_the_negotiation_but_web_fell_returns_the_same_negotiation(
    store, clock, vault_client, default_db, session_key
):
    # X-57: 金庫の作成が成功した直後に web が落ちた(障害の注入)。段の状態もレフェリーのタスクもない。同じ request_id の再送は、
    # 金庫の by-request で同じ交渉を見つけて返す(二重には作らない)。
    fake = CostLimitVault(vault_client)
    body = _demo_request(store, "request-demo1")
    crashing = _admission_env(store, clock, fake, default_db, session_key, daily_limit=1500)
    try:
        fake.crash_after_create = True
        with pytest.raises(SimulatedCrash):
            await crashing.browser().post("/v1/demo/negotiations", body)
    finally:
        await crashing.aclose()
    (nid,) = fake.known_requests.values()  # 金庫には、作られている
    assert default_db.collection("stages").document(nid).get().exists is False  # web は、段の状態も作る前に落ちた

    fake.crash_after_create = False  # web が起動し直した
    restarted = _admission_env(store, clock, fake, default_db, session_key, daily_limit=1500)
    try:
        again = await restarted.browser().post("/v1/demo/negotiations", body)

        assert (again.status_code, again.json()) == (200, {"nid": nid})
        assert [item.nid for item in store.list_open_negotiations().items] == [nid]  # 二重に作っていない
        assert default_db.collection("stages").document(nid).get().exists  # 再送で、段の状態も整う(冪等)
    finally:
        await restarted.aclose()
