"""攻撃モードと 3 枚の壁の実演(design.md §8.1・§8.2・§8.3 の冒頭。台帳 C-1・C-3・C-9・L7-3・P-17)。

- config: [web.attack] の設定(攻撃の相手のテンプレート ID など)。
- memory: web のメモリにだけ持つ状態。攻撃の指示(AttackContexts。永続化しない)と、壁 2 のための LLM 入力の記録(LlmContextRecorder)。
- raw_message: 壁 1(生のメッセージを /a2a/candidate へそのまま送る)。
- walls: 壁 2(LLM の文脈の全文)・壁 3(金庫の答え)の応答。
- scripted: 台本の攻撃者・候補者(LLM を使わない)。二分探索の実演が使う。
- bisection: 二分探索の実演(FR-45。§8.3)。台本の攻撃者で、攻撃の交渉を作り、交渉をまたいで年収を二分探索する。
- router: API(/v1/demo/attack/...)。web.api.build_router が include する。この __init__ は router・bisection を import しない
  (web.services が AttackServices を import するため。循環を避ける)。
- services: 部品の組み立て(AttackServices)。

入口ごとのレート制限は web.limits(攻撃の入口もそこで数える)。
"""

from web.attack.config import DEFAULT_ATTACK_CONFIG, AttackConfig, load_attack_config
from web.attack.memory import AttackContexts, LlmContextRecorder
from web.attack.raw_message import RawMessageSender, bind_raw_sender
from web.attack.services import AttackServices, build_attack_services

__all__ = [
    "DEFAULT_ATTACK_CONFIG",
    "AttackConfig",
    "AttackContexts",
    "AttackServices",
    "LlmContextRecorder",
    "RawMessageSender",
    "bind_raw_sender",
    "build_attack_services",
    "load_attack_config",
]
