"""推定区間メーターの二分探索の実演(FR-45。design.md §8.3)。POST /v1/demo/attack/bisection の中身。台帳 I-24・I-27・L19-8。

台本の攻撃者(web.attack.scripted。LLM を呼ばない)が、攻撃モードの交渉で、探索線の上の年収を二分探索する。候補者の台本は、受けられる提案を受けて
交渉を終える。交渉が終わったら、次の交渉を作って続け(交渉をまたいで区間を積み上げる)、区間が 1 マスになるか、交渉が MAX_NEGOTIATIONS 件になったら
止める。返すのは、作った交渉の ID の一覧と、区間(メーター API と同じ計算。web.meter_api.build_meter の narrowest)。

- 相手: 設定 [web.attack] の架空人物のテンプレート(ふつうの攻撃モードの交渉と同じ。リクエストでは選べない)。交渉は mode=attack で、本物の依頼者には
  触れない。金庫の読み出しも、デモ用の口(get_demo_events。架空の候補者の交渉だけ)を使う。
- 入場の制限と入口の枠は、**交渉 1 件ごと**に通す(台帳 L19-8。1 回の呼び出しで交渉を最大 3 件作るので、1 要求 1 回では、件数の歯止めをすり抜ける)。
  新しく作る交渉の前に、入口の枠 attack_create を 1 つ数え(1 件目だけは、入口の依存〔web.attack.router〕がすでに数えている)、入場の制限
  (web.api の admit_new_negotiation。1 日の LLM の枠・起動時の見回りの待ち)を通す。すでにある交渉(同じ request_id の再送)は、作り直さないので、通さない。
  - 1 件目で断られたら、そのまま断る(429・503。何も作っていない)。
  - 2 件目以降で、枠が尽きて 429 になったら、そこまでの交渉の ID と区間を返し、stopped_reason に rate_limited を付ける(作った交渉は、終わっている)。
    429 以外(カウンタに書けない・起動時の待ち。503)は、そのまま断る。同じ request_id で呼び直せば、できた交渉は作り直さずに、続きから進める。
- 冪等: 交渉ごとの request_id は `attack-scripted:{request_id}:{番号}`(ふつうの攻撃モードの `attack:…`・デモの `demo:…`・本物の `{依頼者 ID}:…` の
  どれとも重ならない)。同じ request_id の再送は、すでにある交渉を作り直さず、終わっていなければ台本で終わらせて、同じ結果を返す。
  同じ request_id の同時の要求は、1 つずつ処理する(web は 1 インスタンス。プロセスの中のロックで足りる)。
- レフェリー: この実行の専用のもの(web.referee.Referee)を動かす。LLM を呼ばないので、物理の呼び出し数は数えない(count_llm_calls=False。
  web.llm_budget の 1 日の枠も、交渉ごとの枠も減らさない。入場の制限は、LLM の枠に余裕があるかを見るだけで、何も書かない)。
- 見回りとの競合: 見回り(web.sweeper)は、動いているタスクのない進行中の交渉に、本物のレフェリー(LLM を呼ぶ)を起こす。この実行の交渉に起こさせない
  ために、動かしている間は、レフェリーの管理(services.referees)に、動いているタスクとして登録する(RefereeManager.reserve)。
- 失敗・時間切れ・中断のときは、金庫の control{cancel} で交渉を取り消す(終わっていない交渉を残すと、見回りが拾って、本物のレフェリーが LLM を呼ぶため)。

ログには、例外の型名だけを書く(交渉 ID・組み合わせの値は書かない)。
"""

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException

from vault.api_models import (
    CandidateParticipantRequest,
    ControlRequest,
    CreateNegotiationRequest,
    EmployerParticipantRequest,
    EventViewItem,
)

from web.attack.scripted import ScriptedAttacker, ScriptedBisectionAgents
from web.meter_api import MeterInterval, build_meter
from web.referee import NegotiationContext, Referee, RefereeDeps

if TYPE_CHECKING:  # web.services が web.attack を import するので、型のためだけに読む(循環を避ける)
    from web.services import WebServices

_log = logging.getLogger(__name__)

# 1 回の実演で作る交渉の上限(暫定。§8.3「交渉 3 件」)。台本は、5 手で 1 マスに届く(候補者が受けて終わる台本なら、3 件)。
MAX_NEGOTIATIONS = 3
# 1 つの交渉を終えるまでの上限(秒)。台本は、金庫を呼ぶだけで、数秒で終わる。超えたら取り消す(金庫が応えないときなど)。
NEGOTIATION_TIMEOUT_SECONDS = 60.0

# 交渉ごとの request_id の前置き。ふつうの攻撃モード(`attack:`)・デモ(`demo:`)・本物の依頼者(16 桁の 16 進数の依頼者 ID)と重ならない。
REQUEST_ID_PREFIX = "attack-scripted"

# 途中で止まった理由。rate_limited は、2 件目以降を作る前に、入口の枠または入場の制限が 429 になったこと(台帳 L19-8)。
StoppedReason = Literal["rate_limited"]

# 新しい交渉 1 件ぶんの入口の枠(attack_create)を 1 つ数える口。使えなければ HTTPException(429・503)を投げる(web.limits の guard と同じ)。
CountCreation = Callable[[], Awaitable[None]]
# 入場の制限(web.api の admit_new_negotiation)。通せなければ HTTPException(429・503)を投げる。
AdmitNewNegotiation = Callable[[], Awaitable[None]]


class BisectionRefused(Exception):
    """金庫が交渉の作成を断った(例: 攻撃の相手のテンプレートがない)。reason は金庫の理由(列挙値)。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BisectionTimeout(Exception):
    """1 つの交渉を、時間内に終えられなかった(その交渉は取り消した)。"""


@dataclass(frozen=True)
class BisectionResult:
    """実演の結果。interval は、候補者側の金庫の答えから作った、年収の境目の区間(提案が 1 つも届かなかったときは None)。

    stopped_reason は、枠が尽きて、続きを作らずに止めたときだけ(rate_limited)。区間が 1 マスになった・交渉が 3 件になったときは None。
    """

    negotiation_ids: list[str]
    interval: MeterInterval | None
    stopped_reason: StoppedReason | None = None


class BisectionRunner:
    """二分探索の実演を動かす。要求ごとに、専用のレフェリーと台本を作る(要求どうしで状態を共有しない)。"""

    def __init__(self, services: "WebServices", admit_new_negotiation: AdmitNewNegotiation) -> None:
        self._services = services
        self._admit = admit_new_negotiation
        self._candidate_template_id = services.attack.config.candidate_template_id
        self._employer_template_id = services.attack.config.employer_template_id
        # 同じ request_id の同時の要求を、1 つずつにするロック(使い終わると、自然に消える)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def run(self, request_id: str, count_creation: CountCreation) -> BisectionResult:
        """request_id の実演を動かす(同じ request_id の再送は、同じ結果を返す)。count_creation は、2 件目以降を作る前に呼ぶ(枠を 1 つ数える)。"""
        lock = self._locks.get(request_id)
        if lock is None:
            lock = self._locks[request_id] = asyncio.Lock()
        async with lock:
            return await self._run(request_id, count_creation)

    async def _run(self, request_id: str, count_creation: CountCreation) -> BisectionResult:
        nids: list[str] = []
        deps = self._referee_deps(nids)
        interval: MeterInterval | None = None
        stopped_reason: StoppedReason | None = None
        for number in range(1, MAX_NEGOTIATIONS + 1):
            try:
                await self._negotiate(f"{REQUEST_ID_PREFIX}:{request_id}:{number}", nids, deps, count_creation, first=number == 1)
            except HTTPException as refused:
                if refused.status_code != 429 or not nids:
                    raise  # 1 件目で断られた(何も作っていない)・枠の尽き方ではない(503): そのまま断る
                stopped_reason = "rate_limited"  # 2 件目以降で枠が尽きた: そこまでの結果を返す
                break
            interval = await self._interval(nids)
            if interval is not None and interval.cells == 1:
                break  # これ以上は絞れない
        return BisectionResult(negotiation_ids=nids, interval=interval, stopped_reason=stopped_reason)

    def _referee_deps(self, nids: list[str]) -> RefereeDeps:
        """この実演のレフェリーの部品。send_turn は台本で、攻撃者は、いまの nids(これまでの交渉)の候補者側の答えを見る。"""
        services = self._services

        async def read_candidate_events(_nid: str) -> list[EventViewItem]:
            return await self._candidate_events(nids)

        return RefereeDeps(
            vault=services.vault,
            send_turn=ScriptedBisectionAgents(ScriptedAttacker(read_candidate_events)),
            clock=services.clock,
            config=services.config.referee,
            count_llm_calls=False,  # 台本は LLM を呼ばない。数えると、実際には呼んでいない分で LLM の枠を減らしてしまう
        )

    async def _candidate_events(self, nids: list[str]) -> list[EventViewItem]:
        """nids の候補者側の見え方のイベントを、つなげて返す(デモ用の口。架空の候補者の交渉だけ読める)。"""
        events: list[EventViewItem] = []
        for nid in list(nids):
            events += await self._services.vault.get_demo_events(nid, "candidate", 0)
        return events

    async def _interval(self, nids: list[str]) -> MeterInterval | None:
        """メーター API と同じ計算(web.meter_api.build_meter)で、最も狭い組の区間。"""
        meter = build_meter([await self._services.vault.get_demo_events(nid, "candidate", 0) for nid in nids])
        return None if meter.narrowest is None else meter.narrowest.interval

    async def _negotiate(
        self, key: str, nids: list[str], deps: RefereeDeps, count_creation: CountCreation, *, first: bool
    ) -> None:
        """攻撃の交渉を 1 つ(すでにあれば、それを)用意して、終わるまで台本で動かす。nid を nids に足す。

        新しく作るときだけ、作る前に、入口の枠(1 件目は、入口の依存が数えた)と入場の制限を通す(HTTPException 429・503 は、呼び出し側が扱う)。
        """
        services = self._services
        vault = services.vault
        nid = await vault.get_negotiation_by_request(key)
        if nid is None:
            if not first:
                await count_creation()  # 入口の枠 attack_create を、新しい交渉 1 件ごとに数える
            await self._admit()  # 入場の制限(1 日の LLM の枠に、新しい交渉 1 件ぶんの余裕があるか。起動時の見回りが終わっているか)
            created = await vault.create_negotiation(
                CreateNegotiationRequest(
                    request_id=key,
                    mode="attack",
                    candidate=CandidateParticipantRequest(is_fictional=True, template_id=self._candidate_template_id),
                    employer=EmployerParticipantRequest(template_id=self._employer_template_id),
                )
            )
            if created.status == "refused" or created.nid is None:
                raise BisectionRefused(created.reason or "refused")
            nid = created.nid
        nids.append(nid)
        # 作った直後に(待たずに)登録する。見回りが、先に本物のレフェリーを起こさないように。
        task = asyncio.create_task(
            Referee(NegotiationContext(nid=nid, mode="attack", candidate_principal_id=None), deps).run(), name="referee"
        )
        services.referees.reserve(nid, task)
        try:
            await services.stages.ensure(nid, None)  # 画面の読み出し口(活動ログ・メーター)が確かめる、web の段の状態
            await asyncio.wait_for(task, NEGOTIATION_TIMEOUT_SECONDS)
        except BaseException as exc:
            task.cancel()
            await self._cancel(nid)
            if isinstance(exc, TimeoutError):
                raise BisectionTimeout from None
            raise

    async def _cancel(self, nid: str) -> None:
        """交渉を取り消す(冪等。終わっていても何も変わらない)。失敗は、元の失敗を隠さないよう、ログだけに残す。"""
        try:
            await asyncio.shield(self._services.vault.control(nid, ControlRequest(side="employer", action="cancel")))
        except Exception as exc:
            _log.error("could not cancel a scripted attack negotiation error=%s", type(exc).__name__)
