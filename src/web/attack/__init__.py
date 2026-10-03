"""攻撃モードと 3 枚の壁の実演(design.md §8.1・§8.2・§8.3 の冒頭。台帳 C-1・C-3・C-9・L7-3・P-17)。

- config: [web.attack] の設定(攻撃の相手のテンプレート ID など)。
- memory: web のメモリにだけ持つ状態。攻撃の指示(AttackContexts。永続化しない)と、壁 2 のための LLM 入力の記録(LlmContextRecorder)。
- raw_message: 壁 1(生のメッセージを /a2a/candidate へそのまま送る)。
- walls: 壁 2(LLM の文脈の全文)・壁 3(金庫の答え)の応答。
- router: API(/v1/demo/attack/...)。web.api.build_router が include する。この __init__ は router を import しない
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
