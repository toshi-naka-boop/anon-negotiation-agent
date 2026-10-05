"""攻撃モードの部品の組み立て(web.services.build_services が呼ぶ)。

攻撃の指示(AttackContexts)・壁 2 の LLM 入力の記録(LlmContextRecorder)・壁 1 の生メッセージを送る関数(RawMessageSender)・設定。
レフェリーには、contexts.instruction_for(攻撃者へ毎手番渡す指示)と llm_context.record(壁 2 の記録)を差し込む(RefereeDeps)。
"""

from dataclasses import dataclass

from vault.clock import Clock

from web.attack.config import DEFAULT_ATTACK_CONFIG, AttackConfig
from web.attack.memory import AttackContexts, LlmContextRecorder
from web.attack.raw_message import RawMessageSender


@dataclass
class AttackServices:
    """組み立て済みの攻撃モードの部品。send_raw が None なら、壁 1 の生メッセージは 503 で断る(agents の URL が分からないとき)。"""

    config: AttackConfig
    contexts: AttackContexts
    llm_context: LlmContextRecorder
    send_raw: RawMessageSender | None


def build_attack_services(
    *, clock: Clock, send_raw: RawMessageSender | None = None, config: AttackConfig = DEFAULT_ATTACK_CONFIG
) -> AttackServices:
    return AttackServices(
        config=config,
        contexts=AttackContexts(clock, config.context_ttl_seconds),
        llm_context=LlmContextRecorder(config.llm_context_max_negotiations),
        send_raw=send_raw,
    )
