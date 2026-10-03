"""推定区間メーターの API(design.md §8.3。FR-45・AC-12。台帳 L8-2・L7-3・P-16)。画面は含まない。

| メソッド・パス | 呼べる人 | 返すもの |
|---|---|---|
| POST /v1/demo/meter | 誰でも(セッションを見ない。X-Requested-With は要る) | 交渉 ID の一覧から作った、年収の境目の推定区間(防御あり) |
| GET /v1/demo/meter/simulation?value= | 誰でも(セッションを見ない) | 防御なしのシミュレーション: 二分探索の手の列と手数 |

防御あり(POST /v1/demo/meter。本文は {"negotiation_ids": [...]} の 1〜20 件)
- 画面は交渉 ID の一覧を渡すだけ。web は一覧を覚えず、その場で金庫を読んで計算し、何も書かない(訪問者を見分ける ID も作らない。L7-3)。
- 渡された ID は、すべて架空人物の交渉(デモ・攻撃)でなければならない。確かめ方は activity_api と同じ 2 段(web の段の状態〔補助〕と、
  金庫のデモ用の読み出しの口〔正本〕。台帳 X-38)。1 件でも本物の利用者の交渉(存在しない交渉・交渉 ID の形でない値も同じ)が混ざっていれば、
  何も返さずに全体を 403 にする(交渉があるかどうかは知らせない)。21 件以上・0 件は、金庫に触れる前に 422。
- 同じ組の答えどうしが食い違うとき(別々の候補者の交渉を混ぜたときだけ起きる。同じ候補者のポリシーは年収について単調)は 422(inconsistent_answers)。
- 使うのは、候補者側の見え方の offer_received だけ(攻撃者の提案と、候補者側の受け手としての評価。§3.2)。候補者側のエージェントが
  受けたかどうか(LLM の手)・候補者自身の確かめや提案は使わない。受けなかったことは「受けられない」を意味しないため。
- 提案を「年収以外の軸の組」でまとめ(他の軸を固定して年収だけを変えた提案が、同じ組になる)、組ごとに estimate_interval で区間を計算する。
  「受けられる」なら境目は提案値以下、「受けられない」なら提案値より上、「本人確認が必要」は情報なし(区間を変えない。ただし観測の数には入る)。
- 組は観測の多い順(同数なら軸の値の順)。narrowest は最も狭い組(同じ幅なら観測の多い方)。観測が 1 件もなければ、groups は空で narrowest は null。
- 区間は (lower, upper]: 境目は lower より上、upper 以下。None は、その側の情報がない(グリッドの外まで広がっている)。
  cells が 1 なら、これ以上は狭まらない(グリッド 1 マス)。区間は常に真の境目を含む。

防御なし(GET /v1/demo/meter/simulation)
- 架空人物の生の値(300〜1500 万、10 万刻み)に対し、「x 万円以上か」を正確に答えてもらえるとして二分探索する。どの値も 7 手以内で特定される(620 万は 7 手)。
- 純粋な計算だけ。「防御なしの金庫」に当たるものはコードになく、金庫も Firestore も呼ばない。応答には simulation: true と注記を付ける
  (画面は「シミュレーション」と明示する)。

ログには何も書かない(組み合わせの値・評価・交渉 ID を、ここから出さない)。
"""

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from negotiation_core import AXES, AXIS_KEYS
from negotiation_core.estimate_interval import estimate_interval
from negotiation_core.policy import StrictModel

from vault.api_models import EventViewItem

from web.services import WebServices
from web.vault_client import VaultNotFoundError

# 1 回に渡せる交渉 ID の数(暫定。§8.3「暫定 20 件まで」)。
MAX_NEGOTIATION_IDS = 20

NOTE = "金庫の答えをすべて見られたとしても、ここまで"
SIMULATION_NOTE = "シミュレーション。防御なしの金庫を仮定した計算で、実システムの金庫の答えではない"

# 組にまとめるときの軸(年収以外のすべて。語彙の順)。
OTHER_AXES: tuple[str, ...] = tuple(axis for axis in AXIS_KEYS if axis != "salary")

# 防御なしのシミュレーション(§8.3): 300〜1500 万を 10 万刻み。
SIMULATION_LOW, SIMULATION_HIGH, SIMULATION_STEP = 300, 1500, 10
SIMULATION_CANDIDATES: tuple[int, ...] = tuple(range(SIMULATION_LOW, SIMULATION_HIGH + 1, SIMULATION_STEP))


class InconsistentAnswers(ValueError):
    """同じ組(他の軸が同じ)の上で、金庫の答えどうしが食い違った(「受けられる」より高い年収が「受けられない」、など)。"""


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeterRequest(StrictModel):
    """POST /v1/demo/meter の本文。交渉 ID の一覧(1〜20 件)だけ。

    1 件ごとの長さは抑える(形の違う値は 403 にするので、ここでは形を見ない。交渉 ID は 16 桁の 16 進数)。
    """

    negotiation_ids: list[Annotated[str, StringConstraints(max_length=64)]] = Field(
        min_length=1, max_length=MAX_NEGOTIATION_IDS
    )


class MeterInterval(_ResponseModel):
    """候補者の年収の境目が入る区間 (lower, upper](万円)。negotiation_core.estimate_interval.Interval をそのまま写す。"""

    lower: int | None  # 境目はこの値より上(この値自身は含まない)。None は、下側の情報がない
    upper: int | None  # 境目はこの値以下(この値自身を含む)。None は、上側の情報がない
    cells: int  # 区間がまたぐグリッドのマスの数。1 なら、これ以上は狭まらない


class MeterGroup(_ResponseModel):
    """年収以外の軸の組が同じ提案の集まりと、そこから作った区間。"""

    axes: dict[str, int | str]  # 年収以外の軸の値(他の軸を固定した探索線)
    observations: int  # この組の提案に対する、候補者側の金庫の答えの数(「本人確認が必要」を含む)
    interval: MeterInterval


class MeterResponse(_ResponseModel):
    groups: list[MeterGroup]  # 観測の多い順
    narrowest: MeterGroup | None  # 最も狭い組。観測がなければ null
    note: str


class SimulationStep(_ResponseModel):
    """二分探索の 1 手: 「ask 万円以上ですか」と尋ね、防御がないので正確に答えてもらう。"""

    ask: int
    at_least: bool
    low: int  # この手の後に残る候補の下端(万円。含む)
    high: int  # 同じく上端(含む)


class SimulationResponse(_ResponseModel):
    simulation: Literal[True]  # 実システムの答えではない(画面は「シミュレーション」と明示する)
    value: int  # 架空人物の生の値(万円)
    candidates: dict[str, int]  # 探索する範囲 {low, high, step}(万円)
    steps: list[SimulationStep]
    count: int  # 手数
    found: int  # 特定した値(万円)
    note: str


def build_meter(event_lists: Iterable[Sequence[EventViewItem]]) -> MeterResponse:
    """候補者側の見え方のイベント(交渉ごとの並び)から、組ごとの区間を作る。純粋な計算で、何も読まず、何も書かない。

    offer_received 以外(候補者自身の確かめ・提案・断り・途中確認など)は使わない。
    答えどうしが食い違うと InconsistentAnswers。同じ候補者のポリシーは年収について単調なので、同じ組の上では起きない
    (別々の候補者の交渉を混ぜたときだけ起きる)。
    """
    answers: dict[tuple, list[tuple[int, str]]] = defaultdict(list)
    for events in event_lists:
        for event in events:
            if event.kind != "offer_received" or event.package is None or event.own_evaluation is None:
                continue
            key = tuple(getattr(event.package, axis) for axis in OTHER_AXES)
            answers[key].append((event.package.salary, event.own_evaluation))

    def to_group(key: tuple, pairs: list[tuple[int, str]]) -> MeterGroup:
        try:
            interval = estimate_interval(pairs)
        except ValueError as exc:
            raise InconsistentAnswers from exc
        return MeterGroup(
            axes=dict(zip(OTHER_AXES, key, strict=True)),
            observations=len(pairs),
            interval=MeterInterval(lower=interval.lower, upper=interval.upper, cells=interval.cells),
        )

    groups = [to_group(key, pairs) for key, pairs in answers.items()]
    groups.sort(key=lambda g: (-g.observations, [AXES[axis].grid.index(g.axes[axis]) for axis in OTHER_AXES]))
    # min は、同じ幅なら先に並んだ組(観測の多い方)を返す
    return MeterResponse(groups=groups, narrowest=min(groups, key=lambda g: g.interval.cells, default=None), note=NOTE)


def simulate_bisection(value: int) -> list[SimulationStep]:
    """防御なしのシミュレーション(§8.3): 300〜1500 万を 10 万刻みで二分探索して value を特定する手の列。

    「x 万円以上か」を正確に答えてもらえるとして、候補の真ん中(偶数個のときは上側)を尋ねる。value は SIMULATION_CANDIDATES のどれか
    (そうでなければ、最も近い下の候補に着く)。121 個の候補は、どれも 7 手以内で特定される。
    """
    low, high = 0, len(SIMULATION_CANDIDATES) - 1
    steps: list[SimulationStep] = []
    while low < high:
        middle = (low + high + 1) // 2  # 上側の真ん中。「以上か」で尋ねるので、これで必ず狭まる
        at_least = value >= SIMULATION_CANDIDATES[middle]
        if at_least:
            low = middle
        else:
            high = middle - 1
        steps.append(
            SimulationStep(
                ask=SIMULATION_CANDIDATES[middle],
                at_least=at_least,
                low=SIMULATION_CANDIDATES[low],
                high=SIMULATION_CANDIDATES[high],
            )
        )
    return steps


def build_meter_router(services: WebServices) -> APIRouter:
    """メーターのルートを作る。api.py の build_router が include する。"""
    router = APIRouter()
    vault = services.vault

    async def read_candidate_view(nid: str) -> list[EventViewItem]:
        """架空人物の交渉(デモ・攻撃)の、候補者側の見え方。activity_api の read_demo_events と同じ 2 段の確認(台帳 X-38)。"""
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await vault.get_demo_events(nid, "candidate", 0)
        except VaultNotFoundError:
            raise HTTPException(status_code=403, detail="forbidden") from None

    @router.post("/v1/demo/meter", response_model=MeterResponse)
    async def meter(body: MeterRequest) -> MeterResponse:
        """推定区間メーター(防御あり): 渡された交渉の、候補者側の金庫の答えから、組ごとの区間を返す。"""
        negotiation_ids = list(dict.fromkeys(body.negotiation_ids))  # 同じ ID が重なっても、1 回だけ読む
        # 待ち時間を短くするため並行して読む。1 件でも断られたら全体を断る(最初に断られた ID の順に決まる)。
        reads = await asyncio.gather(*(read_candidate_view(nid) for nid in negotiation_ids), return_exceptions=True)
        for read in reads:
            if isinstance(read, BaseException):
                raise read
        try:
            return build_meter(reads)
        except InconsistentAnswers:
            raise HTTPException(status_code=422, detail="inconsistent_answers") from None

    @router.get("/v1/demo/meter/simulation", response_model=SimulationResponse)
    async def meter_simulation(value: int) -> SimulationResponse:
        """推定区間メーター(防御なし。シミュレーション): 架空人物の生の値 value(万円)を、二分探索で特定する手の列。"""
        if value not in SIMULATION_CANDIDATES:
            raise HTTPException(status_code=422, detail="value_not_on_the_grid")
        steps = simulate_bisection(value)
        return SimulationResponse(
            simulation=True,
            value=value,
            candidates={"low": SIMULATION_LOW, "high": SIMULATION_HIGH, "step": SIMULATION_STEP},
            steps=steps,
            count=len(steps),
            found=steps[-1].low,
            note=SIMULATION_NOTE,
        )

    return router
