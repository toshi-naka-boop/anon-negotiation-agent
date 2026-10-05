"""web のメモリに持つ、攻撃モードの状態(design.md §8.2・§8.1 の壁 2。台帳 P-17)。

- AttackContexts: 攻撃の交渉の文脈 = 攻撃の指示(自由文。400 文字まで)。**メモリにだけ持つ**: Firestore にも金庫にも書かず、ログにも
  出さない。再起動で消える。消えた交渉は「文脈がない」ので、レフェリーが攻撃者の手番で取消(「なし」)にして終える(web.referee)。
  web は 1 インスタンス(§1.1)なので、プロセスのメモリで足りる。古い文脈は、新しい文脈を足すときに捨てる(交渉の寿命より長い TTL)。
- LlmContextRecorder: 壁 2 のために、デモ・攻撃の交渉の候補者側の LLM に渡した入力(TurnInput の JSON。計画と決定の直近の手番ぶん)を
  覚える。本物の利用者の交渉(live)は、レフェリーが呼ばず、ここでも受け付けない。交渉の数に上限を置き、古いものから捨てる。
"""

import datetime as dt
import json
from collections import OrderedDict

from negotiation_core import AttackerTurnInput, Phase, Side, TurnInput

from vault.clock import Clock

from web.referee import NegotiationContext


def llm_input_text(turn_input: TurnInput | AttackerTurnInput) -> str:
    """検証済みの TurnInput を、LLM に渡す入力(JSON の文字列)にする。agents.executor.llm_input_text と同じ形(テストで一致を確かめる)。"""
    return json.dumps(turn_input.model_dump(mode="json", by_alias=True), ensure_ascii=False, separators=(",", ":"))


class AttackContexts:
    """攻撃の交渉の文脈(攻撃の指示)。メモリにだけ持つ。"""

    def __init__(self, clock: Clock, ttl_seconds: int) -> None:
        self._clock = clock
        self._ttl = dt.timedelta(seconds=ttl_seconds)
        self._contexts: dict[str, tuple[str, dt.datetime]] = {}  # nid → (指示, 作った時刻)

    def add(self, nid: str, instruction: str) -> None:
        """nid の文脈を足す(すでにあれば、置き換えずにそのまま。再送で指示が変わらないように)。古い文脈は捨てる。"""
        now = self._clock.now()
        self._contexts = {key: value for key, value in self._contexts.items() if now - value[1] < self._ttl}
        self._contexts.setdefault(nid, (instruction, now))

    def set_instruction(self, nid: str, instruction: str) -> bool:
        """nid の指示を置き換える(次の手番から効く)。文脈がなければ False。"""
        current = self._contexts.get(nid)
        if current is None:
            return False
        self._contexts[nid] = (instruction, current[1])
        return True

    def instruction_for(self, nid: str) -> str | None:
        """nid の攻撃の指示。文脈がなければ None(この web が持っていない = 再起動で消えた)。RefereeDeps.attacker_instruction に渡す。"""
        current = self._contexts.get(nid)
        return None if current is None else current[0]

    def __contains__(self, nid: object) -> bool:
        return nid in self._contexts

    def __len__(self) -> int:
        return len(self._contexts)


class LlmContextRecorder:
    """デモ・攻撃の交渉の、候補者側の LLM に渡した入力を、直近の手番ぶん覚える(壁 2)。"""

    def __init__(self, max_negotiations: int) -> None:
        self._max = max_negotiations
        self._by_nid: OrderedDict[str, dict[Phase, str]] = OrderedDict()

    def record(self, context: NegotiationContext, side: Side, turn_input: TurnInput | AttackerTurnInput) -> None:
        """レフェリーが LLM の入力を作るたびに呼ぶ(RefereeDeps.turn_recorder)。候補者側のデモ・攻撃の交渉だけを覚える。

        新しい手番の計画(phase=plan)が来たら、前の手番の決定は捨てる(計画と決定が同じ手番のものだけ残る)。
        """
        if side != "candidate" or context.mode == "live" or context.candidate_principal_id is not None:
            return
        entry = self._by_nid.setdefault(context.nid, {})
        self._by_nid.move_to_end(context.nid)
        if turn_input.phase == "plan":
            entry.clear()
        entry[turn_input.phase] = llm_input_text(turn_input)
        while len(self._by_nid) > self._max:
            self._by_nid.popitem(last=False)

    def latest(self, nid: str) -> dict[Phase, str] | None:
        """nid の直近の手番の LLM の入力(phase → JSON の文字列)。覚えていなければ None。"""
        entry = self._by_nid.get(nid)
        return None if entry is None else dict(entry)
