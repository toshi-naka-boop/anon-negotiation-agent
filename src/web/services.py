"""web の部品の組み立て(design.md §1.1・§4.1・§6.3)。

金庫のクライアント・利用記録・段の状態・開示台帳・依頼者ごとのロック・削除の流れ・レフェリー・
2 つの見回りを、1 か所で組み立てる。ロックは 1 つを、ミドルウェア・レフェリー・交渉の見回り・削除の流れが
共有する(台帳 I-4: 同じ依頼者の操作と削除の流れを、1 つずつ順に処理するため)。
外部の部品(金庫のクライアント・Firestore・エージェントを呼ぶ関数・時計・sleep)は、すべて差し込める。
"""

import asyncio
from dataclasses import dataclass

from google.cloud import firestore

from vault.clock import Clock, SystemClock

from web.config import DEFAULT_WEB_CONFIG, WebConfig
from web.deletion import PrincipalDeletion
from web.ledger import DisclosureLedger
from web.locks import PrincipalLocks
from web.principal_sweeper import PrincipalSweeper
from web.principals_meta import PrincipalsMetaStore
from web.referee import FictionalAnswerer, RefereeDeps, RefereeManager, SendTurn, Sleep
from web.session import SessionCodec
from web.stages import StageStore
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
    deletion: PrincipalDeletion
    referees: RefereeManager
    sweeper: Sweeper
    principal_sweeper: PrincipalSweeper


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
) -> WebServices:
    """部品を組み立てる。session_key が空なら MissingSessionKeyError(起動を拒否する)。"""
    clock = clock if clock is not None else SystemClock()
    codec = SessionCodec(session_key, max_age_seconds=config.session.cookie_max_age_seconds)
    locks = PrincipalLocks()
    meta = PrincipalsMetaStore(default_db, clock, config.principals)
    stages = StageStore(default_db, clock, config.retention)
    ledger = DisclosureLedger(default_db)
    deletion = PrincipalDeletion(vault=vault, meta=meta, stages=stages, ledger=ledger, locks=locks)
    referees = RefereeManager(
        RefereeDeps(
            vault=vault,
            send_turn=send_turn,
            clock=clock,
            sleep=sleep,
            config=config.referee,
            answerer=answerer,
            locks=locks,
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
        deletion=deletion,
        referees=referees,
        sweeper=sweeper,
        principal_sweeper=principal_sweeper,
    )
