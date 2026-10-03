"""攻撃モードのためのレフェリーの差し込み口と、web のメモリの状態(design.md §8.1・§8.2。台帳 P-17)。

- 攻撃の指示(AttackContexts)は web のメモリにだけ持つ。足す・置き換える・引く・古いものを捨てる。
- レフェリーの差し込み口 attacker_instruction: 指示を返せば攻撃者へ渡す。None(この web が持っていない = 再起動で消えた)を返したら、
  攻撃者を呼ばずに交渉を取消(「なし」)にして終える。差し込み口がなければ、従来どおり空文字を渡す。
- レフェリーの差し込み口 turn_recorder(壁 2): デモ・攻撃の交渉の、LLM に渡す入力を作るたびに呼ぶ。本物の利用者の交渉(live)では呼ばない。
"""

import dataclasses
import datetime as dt

import pytest
from vault.clock import FixedClock
from vault_helpers import sample_package
from web.attack.memory import AttackContexts
from web.referee import NegotiationContext, Referee
from web_helpers import create_demo_negotiation, create_live_negotiation, drive, plan_dict

pytestmark = pytest.mark.anyio

TTL_SECONDS = 600


# ----------------------------------------------------------------------
# 攻撃の指示(メモリ)
# ----------------------------------------------------------------------


def test_the_contexts_hold_the_instruction_per_negotiation_and_know_nothing_of_others():
    clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    contexts = AttackContexts(clock, TTL_SECONDS)

    assert contexts.instruction_for("a" * 16) is None  # 持っていない(再起動で消えた交渉も同じ)
    contexts.add("a" * 16, "指示 A")
    contexts.add("b" * 16, "指示 B")

    assert (contexts.instruction_for("a" * 16), contexts.instruction_for("b" * 16)) == ("指示 A", "指示 B")
    assert "a" * 16 in contexts and "c" * 16 not in contexts and len(contexts) == 2


def test_adding_again_keeps_the_first_instruction_and_an_update_replaces_it_only_for_known_negotiations():
    contexts = AttackContexts(FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)), TTL_SECONDS)
    contexts.add("a" * 16, "最初の指示")
    contexts.add("a" * 16, "再送された別の指示")  # 再送では変わらない

    assert contexts.instruction_for("a" * 16) == "最初の指示"
    assert contexts.set_instruction("a" * 16, "置き換えた指示") is True
    assert contexts.instruction_for("a" * 16) == "置き換えた指示"
    assert contexts.set_instruction("b" * 16, "知らない交渉") is False
    assert contexts.instruction_for("b" * 16) is None


def test_contexts_older_than_the_ttl_are_dropped_when_a_new_one_is_added():
    clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    contexts = AttackContexts(clock, TTL_SECONDS)
    contexts.add("a" * 16, "古い指示")
    clock.advance(dt.timedelta(seconds=TTL_SECONDS - 1))
    contexts.add("b" * 16, "新しい指示")
    assert contexts.instruction_for("a" * 16) == "古い指示"  # まだ寿命の中

    clock.advance(dt.timedelta(seconds=2))
    contexts.add("c" * 16, "さらに新しい指示")

    assert contexts.instruction_for("a" * 16) is None  # 寿命を過ぎたものは捨てる(メモリを使い続けない)
    assert contexts.instruction_for("b" * 16) == "新しい指示" and contexts.instruction_for("c" * 16) is not None


# ----------------------------------------------------------------------
# レフェリーの差し込み口
# ----------------------------------------------------------------------


async def test_an_attacker_turn_whose_instruction_this_process_does_not_have_cancels_the_negotiation(store, web_env):
    # 台帳 P-17: 指示を持っていない(None)交渉は、攻撃者の手番で、攻撃者を呼ばずに取消(「なし」)にして終える。
    # 候補者の手番は、そのまま進む(攻撃者の手番に着いて初めて気づく)。
    env = web_env
    nid = create_demo_negotiation(store, mode="attack")
    env.agents.script("candidate", plan_dict(move="propose", package=sample_package(salary=700)))
    env.configure(attacker_instruction=lambda _nid: None)

    await drive(env.referee(nid, mode="attack"))

    assert [call.role for call in env.agents.calls] == ["candidate"]  # 攻撃者は呼んでいない
    document = store._negotiation_ref(nid).get().to_dict()
    assert (document["status"], document["end_reason"]) == ("judged", "cancelled")
    for side in ("candidate", "employer"):
        finals = [event for event in store.get_events(nid, side) if event.kind == "final_result"]
        assert [(e.result.likelihood, e.result.package) for e in finals] == [("none", None)]  # 最終記録は双方に 1 件


async def test_an_instruction_the_process_has_is_passed_to_the_attacker_and_the_negotiation_goes_on(store, web_env):
    env = web_env
    nid = create_demo_negotiation(store, mode="attack")
    env.agents.script("candidate", plan_dict(move="propose", package=sample_package(salary=700)))
    env.agents.script("attacker", plan_dict(move="accept"))
    env.configure(attacker_instruction=lambda _nid: "持っている指示")

    await drive(env.referee(nid, mode="attack"))

    (attacker_call,) = env.agents.calls_for("attacker")
    assert attacker_call.turn_input.principal_instruction == "持っている指示"
    assert store._negotiation_ref(nid).get().to_dict()["end_reason"] == "agreed"


async def test_the_recorder_hook_is_called_for_demo_and_attack_turns_but_never_for_live_ones(store, web_env):
    # 壁 2: 本物の利用者の交渉(live)の入力は、記録の口に渡さない。デモ・攻撃は、LLM に渡す入力を作るたびに(計画・決定とも、両側)渡す。
    env = web_env
    seen: list[tuple[str, str, str, str]] = []

    def spy(context, side, turn_input):
        seen.append((context.mode, side, turn_input.phase, type(turn_input).__name__))

    deps = dataclasses.replace(env.deps, turn_recorder=spy, attacker_instruction=lambda _nid: "指示")
    package = sample_package(salary=700)

    live_nid, pid = create_live_negotiation(store)
    env.agents.script("candidate", plan_dict(move="propose", package=package))
    env.agents.script("employer", plan_dict(move="accept"))

    await drive(Referee(NegotiationContext(nid=live_nid, mode="live", candidate_principal_id=pid), deps))
    assert seen == []  # 本物の利用者の交渉は、1 回も渡していない

    attack_nid = create_demo_negotiation(store, mode="attack")
    env.agents.script("candidate", plan_dict(move="propose", package=package))
    env.agents.script("attacker", plan_dict(move="accept"))
    await drive(Referee(NegotiationContext(nid=attack_nid, mode="attack", candidate_principal_id=None), deps))
    assert seen == [
        ("attack", "candidate", "plan", "TurnInput"),
        ("attack", "employer", "plan", "AttackerTurnInput"),
    ]
