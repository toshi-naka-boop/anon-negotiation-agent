"""web の部品の組み立て(design.md §1.1・§4.1・§6.3)。

金庫のクライアント・利用記録・段の状態・開示台帳・段階開示の流れ・LLM の物理の呼び出し数の計上・依頼者ごとのロック・削除の流れ・
レフェリー・2 つの見回りを、1 か所で組み立てる。ロックは 1 つを、ミドルウェア・レフェリー・交渉の見回り・削除の流れが
共有する(台帳 I-4: 同じ依頼者の操作と削除の流れを、1 つずつ順に処理するため)。
外部の部品(金庫のクライアント・Firestore・エージェントを呼ぶ関数・時計・sleep)は、すべて差し込める。

架空人物の自動応答: fixtures(FixtureCatalog。架空人物のフィクスチャの表)を渡すと、段階開示の自動応答(架空の求人の「会う」「承認」)と、
途中確認の自動回答(answerer を渡さなければ、フィクスチャで答える CatalogAnswerer)が動く。渡さなければ、どちらもない(以前と同じ)。

段の決着処理(判定の検出・架空人物の自動応答・台帳。StageSettler。台帳 X-84)は、GET では行わない。レフェリーの完了のフック(RefereeDeps.on_finished)と、
交渉の見回り(Sweeper の settle)が呼ぶ。本人の「会う」「承認」(POST)は、StageFlow がその場で行う。
"""

import asyncio
from dataclasses import dataclass

from google.cloud import firestore

from vault.clock import Clock, SystemClock

from web.attack import AttackServices, RawMessageSender, build_attack_services
from web.config import DEFAULT_WEB_CONFIG, WebConfig
from web.deletion import PrincipalDeletion
from web.interview import InterviewService, build_interview_service
from web.fictional_answerer import CatalogAnswerer, FixtureCatalog
from web.ledger import DisclosureLedger
from web.limits import (
    DEFAULT_RATE_LIMIT_CONFIG,
    AnonymousReadLimiter,
    RateLimitConfig,
    RateLimiter,
    SseConnectionLimiter,
    derive_limiter_key,
)
from web.llm_budget import LlmBudget
from web.locks import PrincipalLocks
from web.principal_sweeper import PrincipalSweeper
from web.principals_meta import PrincipalsMetaStore
from web.referee import FictionalAnswerer, NegotiationContext, RefereeDeps, RefereeManager, SendTurn, Sleep
from web.session import SessionCodec
from web.stages import StageFlow, StageSettler, StageStore
from web.sweeper import Sweeper
from web.vault_client import VaultClient


@dataclass
class WebServices:
    """組み立て済みの部品。ルート・ミドルウェア・起動の処理が、これを読む。"""

    vault: VaultClient
    clock: Clock
    config: WebConfig
    codec: SessionCodec
    locks: PrincipalLocks
    meta: PrincipalsMetaStore
    stages: StageStore
    ledger: DisclosureLedger
    stage_flow: StageFlow
    stage_settler: StageSettler
    llm_budget: LlmBudget
    deletion: PrincipalDeletion
    referees: RefereeManager
    sweeper: Sweeper
    principal_sweeper: PrincipalSweeper
    default_db: firestore.Client
    interview: InterviewService
    limiter: RateLimiter
    stream_limiter: SseConnectionLimiter
    read_limiter: AnonymousReadLimiter
    attack: AttackServices


def build_services(
    *,
    vault: VaultClient,
    default_db: firestore.Client,
    session_key: str,
    send_turn: SendTurn,
    clock: Clock | None = None,
    sleep: Sleep = asyncio.sleep,
    config: WebConfig = DEFAULT_WEB_CONFIG,
    answerer: FictionalAnswerer | None = None,
    send_raw: RawMessageSender | None = None,
    rate_limits: RateLimitConfig = DEFAULT_RATE_LIMIT_CONFIG,
    fixtures: FixtureCatalog | None = None,
) -> WebServices:
    """部品を組み立てる。session_key が空なら MissingSessionKeyError(起動を拒否する)。"""
    clock = clock if clock is not None else SystemClock()
    codec = SessionCodec(session_key, max_age_seconds=config.session.cookie_max_age_seconds)
    locks = PrincipalLocks()
    meta = PrincipalsMetaStore(default_db, clock, config.principals)
    stages = StageStore(default_db, clock, config.retention)
    ledger = DisclosureLedger(default_db)
    stage_flow = StageFlow(stages=stages, vault=vault, fixtures=fixtures if fixtures is not None else FixtureCatalog())
    stage_settler = StageSettler(flow=stage_flow, vault=vault, locks=locks, meta=meta)  # 段の決着処理(GET の外。台帳 X-84)
    if answerer is None and fixtures is not None:
        answerer = CatalogAnswerer(stage_flow)  # 架空人物の途中確認は、フィクスチャで自動回答する(§4.4)。渡さなければ、24 時間待つ
    llm_budget = LlmBudget(default_db, clock, config.llm_budget)
    # rate_limits の文書 ID の HMAC の鍵は、署名の鍵から派生させる(鍵なしのハッシュだと、IPv4 の全数を試して IP を戻せる。台帳 L19-12)
    limiter = RateLimiter(default_db, clock, rate_limits, key=derive_limiter_key(session_key))
    stream_limiter = SseConnectionLimiter()  # SSE の同時本数の上限(全体・クライアント IP ごと。メモリの中だけ。台帳 C-65)
    read_limiter = AnonymousReadLimiter(clock)  # 金庫か Firestore を読む GET(SSE の開始を含む)の、クライアントごとの 1 分あたりの枠(メモリの中だけ。台帳 C-68・C-72)
    attack = build_attack_services(clock=clock, send_raw=send_raw)
    interview = build_interview_service(
        vault=vault, meta=meta, llm_budget=llm_budget, clock=clock, sleep=sleep, web_config=config
    )
    deletion = PrincipalDeletion(
        vault=vault, meta=meta, stages=stages, ledger=ledger, interview_states=interview.store, locks=locks
    )  # 本人の削除と 30 日の自動削除は、面談の途中状態(メモリ)も消す(台帳 I-26)

    async def settle_finished(context: NegotiationContext) -> None:
        """レフェリーの完了のフック: 終わった交渉の段を決着させる(判定の検出・架空人物の自動応答・台帳)。失敗は、レフェリーが記録する(見回りが拾う)。"""
        await stage_settler.settle(context.nid, context.candidate_principal_id)

    referees = RefereeManager(
        RefereeDeps(
            vault=vault,
            send_turn=send_turn,
            clock=clock,
            sleep=sleep,
            config=config.referee,
            answerer=answerer,
            locks=locks,
            llm_budget=llm_budget,
            max_checks_per_plan=config.llm_budget.max_checks_per_plan,
            attacker_instruction=attack.contexts.instruction_for,
            turn_recorder=attack.llm_context.record,
            on_finished=settle_finished,
        )
    )
    sweeper = Sweeper(
        vault=vault,
        stages=stages,
        referees=referees,
        clock=clock,
        sleep=sleep,
        config=config.sweeper,
        locks=locks,
        meta=meta,
        settle=stage_settler.settle,
        evict_idle=interview.store.evict_idle,  # アイドルの面談の状態を、メモリから定期に消す(台帳 C-66・L19-6)
    )
    principal_sweeper = PrincipalSweeper(
        meta=meta, deletion=deletion, clock=clock, sleep=sleep, config=config.principal_sweeper
    )
    return WebServices(
        vault=vault,
        clock=clock,
        config=config,
        codec=codec,
        locks=locks,
        meta=meta,
        stages=stages,
        ledger=ledger,
        stage_flow=stage_flow,
        stage_settler=stage_settler,
        llm_budget=llm_budget,
        deletion=deletion,
        referees=referees,
        sweeper=sweeper,
        principal_sweeper=principal_sweeper,
        default_db=default_db,
        interview=interview,
        limiter=limiter,
        stream_limiter=stream_limiter,
        read_limiter=read_limiter,
        attack=attack,
    )
