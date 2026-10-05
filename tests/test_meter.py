"""AC-12・DV-01: 推定区間メーターの API(design.md §8.3・§12.1 AC-12・§12.2 DV-01。台帳 L8-2・L7-3・P-16)。

防御あり(POST /v1/demo/meter)
- 台本の攻撃者(tests/scripted_negotiators.py の ScriptedAttacker。最悪の場合: 候補者側の金庫の答えをすべて見て、探索線の上で年収を二分探索する)
  で、ケース 3 の候補者に、本物の金庫とレフェリーを通して攻撃の交渉を動かし、メーター API が返す区間を確かめる。
  - 候補者が受けて交渉が終わる台本(区間が交渉をまたいで積み上がる)でも、対案を返し続ける台本(1 つの交渉の中で進む)でも、区間は狭まる一方で、
    常に候補者の生の境目(620 万)を含み、最後はグリッド 1 マス(600 万より上、650 万以下)で止まる。
  - 本物の利用者の交渉の ID が 1 件でも混ざると全体が 403(存在しない交渉・形の違う ID も同じ)。web の段の状態〔補助〕が「架空」と言っていても、
    金庫〔正本〕が断る。0 件と 21 件は 422(金庫に触れる前)。
  - 「本人確認が必要」の答えは区間を変えない。候補者側エージェントの手・候補者自身の確かめは使わない。
  - 別々の候補者の交渉を混ぜて、同じ線の上の答えが食い違ったときは、500 にせず 422 で知らせる。
  - 何も覚えず、何も書かず(入口の枠の回数を rate_limits に数える以外は)、セッションを見ない。
  - 入口の枠(web.limits の meter。1 回で最大 20 件を読むため): 超えたら 429 で、金庫にも段の状態にも触れない。クライアント IP ごと。窓が変われば開く。
    LLM を呼ばず金庫を読むだけなので、全体の枠(rate_overall_limit)には数えない(台帳 L19-7)。
防御なし(GET /v1/demo/meter/simulation): 300〜1500 万を 10 万刻みで二分探索すると、どの値も 7 手以内で特定される(620 万は 7 手。純粋な計算で、金庫を呼ばない)。
入口の枠は掛けない(メーターの枠を使い切っていても呼べ、回数も数えない)。
"""

import dataclasses
import datetime as dt
from typing import get_args

import pytest

from attack_helpers import make_env, small_limits  # noqa: F401  (make_env はフィクスチャ。入口の枠を絞った web を作る)
from negotiation_core import AXES, Anchor, Package, Policy, Verdict, evaluate
from negotiation_core.estimate_interval import Interval, estimate_interval
from scripted_negotiators import (
    SALARY_GRID,
    SEARCH_LINE,
    Negotiator,
    ScriptedAttackNegotiators,
    ScriptedAttacker,
    Strategy,
    is_on_search_line,
    probe_package,
)
from vault.api_models import EventViewItem, MoveRequest
from vault.fixtures import RawConditions, build_rounded_policy, load_case_fixture, put_fixture_templates
from vault.models import EventKind
from vault_helpers import demo_create_request, put_candidate_and_employer_templates, sample_package
from web.fictional_answerer import FixtureAnswerer
from web.meter_api import MAX_NEGOTIATION_IDS, NOTE, SIMULATION_CANDIDATES, build_meter, simulate_bisection
from web.referee import NegotiationContext, Referee, RefereeDeps, StepOutcome
from web_app_helpers import REQUESTED_WITH, dump_documents
from web_helpers import create_demo_negotiation

METER = "/v1/demo/meter"
SIMULATION = "/v1/demo/meter/simulation"

ACC, NOT, NC = Verdict.ACCEPTABLE, Verdict.NOT_ACCEPTABLE, Verdict.NEEDS_CONFIRMATION

CASE3 = load_case_fixture(3)
# ケース 3 の候補者の、探索線の上の生の境目(620 万。グリッドの上にない値)。メーターの区間は、これを含み続ける。
RAW_BOUNDARY = CASE3.candidate.raw.bounds[(SEARCH_LINE["remote_days"], SEARCH_LINE["night_duty"], SEARCH_LINE["review_months"])]
# 候補者の最初の提案。攻撃者の台本は、これに答えずに、探索線の上で提案を出す。
CANDIDATE_OPENING = Package(
    salary=900, remote_days=3, night_duty=0, review_months=6, training="available", side_job="allowed", start="within_3_months"
)
# 二分探索の 5 手(tests/test_fixtures.py・test_run_demo.py と同じ。候補者が受けても対案を返しても、同じ手になる)と、区間の移り変わり。
EXPECTED_PROBES = [900, 550, 700, 600, 650]
EXPECTED_HISTORY = [(None, 900, 13), (550, 900, 7), (550, 700, 3), (600, 700, 2), (600, 650, 1)]


def test_the_raw_boundary_of_case3_is_off_the_grid():
    # 前提: 生の境目はグリッドの上にない。だから「含む」とは、丸めた後のマスの中にある、という意味になる。
    assert RAW_BOUNDARY == 620 and RAW_BOUNDARY not in SALARY_GRID


# ----------------------------------------------------------------------
# 純粋な計算(build_meter): 組にまとめて、区間を作る
# ----------------------------------------------------------------------


def _offer(seq: int, salary: int, verdict: Verdict, **axes) -> EventViewItem:
    """候補者側の見え方の、相手の提案を受け取った記録(offer_received)。axes は、年収以外の軸の値(既定は sample_package)。"""
    return EventViewItem(
        seq=seq, kind="offer_received", package=sample_package(salary=salary, **axes), own_evaluation=verdict.value
    )


def _interval(group) -> tuple[int | None, int | None, int]:
    return group.interval.lower, group.interval.upper, group.interval.cells


def test_offers_are_grouped_by_the_axes_other_than_salary_and_the_most_observed_group_comes_first():
    events = [
        _offer(1, 900, ACC, remote_days=3),
        _offer(2, 550, NOT),
        _offer(3, 700, ACC),
        _offer(4, 600, NOT),
        _offer(5, 800, ACC, remote_days=3),
    ]

    response = build_meter([events])

    assert response.note == NOTE == "金庫の答えをすべて見られたとしても、ここまで"
    first, second = response.groups
    assert (first.axes, first.observations, _interval(first)) == (
        {
            "remote_days": 2,
            "night_duty": 2,
            "review_months": 6,
            "training": "available",
            "side_job": "allowed",
            "start": "within_1_month",
        },
        3,
        (600, 700, 2),
    )
    assert (second.axes["remote_days"], second.observations, _interval(second)) == (3, 2, (None, 800, 11))
    assert list(first.axes) == ["remote_days", "night_duty", "review_months", "training", "side_job", "start"]  # 年収は入らない
    assert response.narrowest == first  # 最も狭い組(2 マス。もう一方は 11 マス)


def test_groups_with_the_same_number_of_observations_are_ordered_by_their_axes_and_narrowest_prefers_more_observations():
    narrow_few = [_offer(1, 650, ACC, remote_days=1), _offer(2, 600, NOT, remote_days=1)]  # 1 マス・観測 2
    narrow_many = [
        _offer(3, 650, ACC, remote_days=4),
        _offer(4, 600, NOT, remote_days=4),
        _offer(5, 700, ACC, remote_days=4),
    ]  # 1 マス・観測 3
    wide_few = [_offer(6, 650, ACC, remote_days=0), _offer(7, 700, ACC, remote_days=0)]  # 広い・観測 2

    response = build_meter([narrow_few + wide_few, narrow_many])
    reversed_response = build_meter([narrow_many, wide_few + narrow_few])  # 渡す順に関わらず、並びは決まる

    assert [(g.axes["remote_days"], g.observations) for g in response.groups] == [(4, 3), (0, 2), (1, 2)]
    assert response == reversed_response
    assert _interval(response.narrowest) == (600, 650, 1) and response.narrowest.axes["remote_days"] == 4


def test_a_needs_confirmation_answer_adds_an_observation_but_does_not_change_the_interval():
    # §8.3: 「本人確認が必要」は情報なし。
    known = [_offer(1, 700, ACC), _offer(2, 600, NOT)]
    with_gap_answer = [*known, _offer(3, 650, NC)]

    before, after = build_meter([known]), build_meter([with_gap_answer])

    assert _interval(before.groups[0]) == _interval(after.groups[0]) == (600, 700, 2)
    assert (before.groups[0].observations, after.groups[0].observations) == (2, 3)
    only = build_meter([[_offer(1, 650, NC)]])  # 「本人確認が必要」だけなら、何も分からない(グリッドの全体と、その外の 1 マス)
    assert _interval(only.groups[0]) == (None, None, len(SALARY_GRID) + 1) and only.narrowest == only.groups[0]


def test_nothing_observed_gives_no_group_and_no_narrowest():
    assert build_meter([]).model_dump() == {"groups": [], "narrowest": None, "note": NOTE}
    assert build_meter([[], []]).groups == []


def test_only_the_offers_the_candidate_received_are_used_not_what_the_candidate_did():
    # 候補者側のエージェントが受けたか(LLM の手)・自分で確かめた組み合わせは使わない。受けなかったことは「受けられない」を意味しないため。
    package = sample_package(salary=650)
    others = [
        EventViewItem(seq=index, kind=kind, package=package, own_evaluation=Verdict.ACCEPTABLE.value)
        for index, kind in enumerate(get_args(EventKind), start=1)
        if kind != "offer_received"
    ]
    assert len(others) == len(get_args(EventKind)) - 1

    assert build_meter([others]).groups == []
    # 対照: 受け取った提案が 1 件あれば、組ができる(使っていないのは、種類のせい)
    assert [g.observations for g in build_meter([[*others, _offer(99, 650, ACC)]]).groups] == [1]


@pytest.mark.parametrize("raw_boundary", [300, 301, 349, 351, 600, 601, 620, 650, 651, 1000, 1499, 1500])
def test_the_interval_is_exactly_one_cell_and_contains_the_raw_boundary_whatever_it_is(raw_boundary):
    # 丸め(§2.5)は、フィクスチャのポリシーの作り方(build_rounded_policy)と同じ。候補者の生の境目が 300〜1500 万のどこにあっても、
    # 探索線の全グリッド点の答えから作った区間は 1 マスで、生の境目を含む(AC-12 の「最後はグリッド 1 マス」)。
    columns = [
        (remote, night, review)
        for remote in AXES["remote_days"].grid
        for night in AXES["night_duty"].grid
        for review in AXES["review_months"].grid
    ]
    policy = build_rounded_policy(RawConditions("candidate", {column: raw_boundary for column in columns}))
    events = [
        EventViewItem(
            seq=seq,
            kind="offer_received",
            package=probe_package(salary),
            own_evaluation=evaluate(policy, probe_package(salary)).value,
        )
        for seq, salary in enumerate(SALARY_GRID, start=1)
    ]

    (group,) = build_meter([events]).groups

    assert group.interval.cells == 1
    assert Interval(group.interval.lower, group.interval.upper).contains(raw_boundary)


# ----------------------------------------------------------------------
# 防御なしのシミュレーション(純粋な計算)
# ----------------------------------------------------------------------


def test_the_simulation_finds_620_in_seven_steps():
    # §8.3: 300〜1500 万を 10 万刻みで二分探索すると、7 手で特定される。
    steps = simulate_bisection(620)

    assert len(steps) == 7
    assert [(s.ask, s.at_least) for s in steps] == [
        (900, False),
        (600, True),
        (750, False),
        (670, False),
        (630, False),
        (610, True),
        (620, True),
    ]
    assert (steps[-1].low, steps[-1].high) == (620, 620)


def test_the_simulation_finds_every_value_of_the_range_within_seven_steps_with_truthful_answers():
    assert len(SIMULATION_CANDIDATES) == 121 and (SIMULATION_CANDIDATES[0], SIMULATION_CANDIDATES[-1]) == (300, 1500)
    for value in SIMULATION_CANDIDATES:
        steps = simulate_bisection(value)
        assert 1 <= len(steps) <= 7, value  # 121 < 2^7
        low, high = SIMULATION_CANDIDATES[0], SIMULATION_CANDIDATES[-1]
        for step in steps:
            assert step.at_least == (value >= step.ask)  # 防御がないので、答えは正確
            assert low <= step.low <= step.high <= high and (step.low, step.high) != (low, high)  # 候補は狭まる一方
            assert step.low <= value <= step.high
            low, high = step.low, step.high
        assert (low, high) == (value, value)  # 最後は 1 つに絞れている
    assert max(len(simulate_bisection(value)) for value in SIMULATION_CANDIDATES) == 7


# ----------------------------------------------------------------------
# 台本の攻撃者で、ケース 3 の候補者に、本物の金庫とレフェリーを通して攻撃する(AC-12)
# ----------------------------------------------------------------------


def _move(store, nid: str, side: str, move: str, package: Package | None = None) -> int:
    """金庫に手を 1 つ登録し、応答の version を返す。"""
    expected_version = store.get_view(nid, side).version
    request = MoveRequest(expected_version=expected_version, side=side, move=move, package=package)
    return store.process_move(nid, request).version


class _Attack:
    """ケース 3 の攻撃の交渉を、本物の金庫とレフェリーで動かす(候補者は台本。攻撃者は、これまでの交渉の候補者側の見え方から区間を作る)。

    メーター API は、画面と同じ呼び方(交渉 ID の一覧を渡すだけ)で、レフェリーが 1 手番進むたびに呼ぶ。区間の移り変わりを history に残す。
    """

    def __init__(self, web_app, *, candidate_accepts: bool) -> None:
        self.web_app = web_app
        self.browser = web_app.browser()  # クッキーのない訪問者(攻撃画面)
        self.nids: list[str] = []
        self.history: list[tuple[int | None, int | None, int]] = []
        self.last_body: dict | None = None
        put_fixture_templates(web_app.store._db, CASE3)
        sender = ScriptedAttackNegotiators(
            Negotiator(Strategy("hybrid", accepts=candidate_accepts), CANDIDATE_OPENING),
            ScriptedAttacker(self.read_every_negotiation),
        )
        self.deps = RefereeDeps(
            vault=web_app.vault,
            send_turn=sender,
            clock=web_app.clock,
            sleep=web_app.sleep,
            config=web_app.services.config.referee,
            answerer=FixtureAnswerer(CASE3),
            count_llm_calls=False,  # 台帳 X-60: 計上はこのテストの対象外
        )

    async def read_every_negotiation(self, _nid: str) -> list[EventViewItem]:
        """最悪の場合の攻撃者が見る、これまでのすべての交渉の候補者側の見え方。"""
        return [item for nid in self.nids for item in await self.web_app.vault.get_events(nid, "candidate")]

    async def _observe(self) -> None:
        """メーターを呼び、区間が真の境目を含み続け、狭まる一方であることを確かめる。"""
        response = await self.browser.post(METER, {"negotiation_ids": self.nids})
        assert response.status_code == 200, response.text
        body = self.last_body = response.json()
        if not body["groups"]:
            return  # まだ提案が 1 つも届いていない
        (group,) = body["groups"]  # 攻撃者は探索線の上でしか提案しないので、組は 1 つ
        assert group["axes"] == SEARCH_LINE and body["narrowest"] == group
        interval = group["interval"]
        assert Interval(interval["lower"], interval["upper"]).contains(RAW_BOUNDARY)  # 常に真の値を含む
        current = (interval["lower"], interval["upper"], interval["cells"])
        if self.history and current == self.history[-1]:
            return
        if self.history:
            previous_lower, previous_upper, previous_cells = self.history[-1]
            assert current[2] < previous_cells  # 変わるときは、必ず狭まる
            assert previous_lower is None or (current[0] is not None and current[0] >= previous_lower)
            assert previous_upper is None or (current[1] is not None and current[1] <= previous_upper)
        self.history.append(current)

    async def negotiate(self) -> str:
        """攻撃の交渉を 1 件作り、終わるまで動かす(レフェリーが 1 手番進むたびに、メーターを呼ぶ)。交渉 ID を返す。"""
        created = self.web_app.store.create_negotiation(
            demo_create_request(CASE3.candidate.template_id, CASE3.employer.template_id, mode="attack")
        )
        assert created.status == "created"
        self.nids.append(created.nid)
        await self.web_app.services.stages.ensure(created.nid, None)  # 画面 API の確認(web の段の状態)が見る文書
        referee = Referee(NegotiationContext(nid=created.nid, mode="attack", candidate_principal_id=None), self.deps)
        for _ in range(100):
            outcome = await referee.step()
            await self._observe()
            if outcome is StepOutcome.FINISHED:
                return created.nid
        raise AssertionError("the referee did not finish the negotiation")

    def end_reason(self, nid: str) -> str:
        return self.web_app.store._negotiation_ref(nid).get().to_dict()["end_reason"]


def _observed_answers(body: dict) -> int:
    return sum(group["observations"] for group in body["groups"])


@pytest.mark.anyio
async def test_the_interval_accumulates_across_negotiations_and_stops_at_one_cell_when_the_candidate_accepts(web_app):
    # AC-12 の「受けて終わる台本」: 候補者が受けられる提案を受けるたびに交渉が終わる。区間は、交渉をまたいで積み上がる
    # (画面は自分が作った交渉の ID の一覧を渡すだけ。web は覚えない)。
    attack = _Attack(web_app, candidate_accepts=True)
    end_reasons = []
    for _ in range(5):  # 1 マスになるまで。上限を置くのは、止まらない場合に失敗させるため
        end_reasons.append(attack.end_reason(await attack.negotiate()))
        if attack.history[-1][2] == 1:
            break

    assert end_reasons == ["agreed", "agreed", "agreed"]  # 3 つの交渉で 5 手
    assert attack.history == EXPECTED_HISTORY  # 狭まる一方で、5 手のどれもが区間を変えた
    final = attack.last_body
    assert _observed_answers(final) == len(EXPECTED_PROBES) == 5 and final["note"] == NOTE
    (group,) = final["groups"]
    assert group["interval"] == {"lower": 600, "upper": 650, "cells": 1}  # 600 万より上、650 万以下の 1 マス
    assert Interval(600, 650).contains(RAW_BOUNDARY)

    # 独立な確かめ: 攻撃者が見た候補者側の答え(金庫のイベント)から、探索線の上の提案だけを集めて estimate_interval で作った区間と同じ
    probes = [e for e in await attack.read_every_negotiation("") if e.kind == "offer_received"]
    assert all(is_on_search_line(e.package) for e in probes) and [e.package.salary for e in probes] == EXPECTED_PROBES
    independent = estimate_interval([(e.package.salary, e.own_evaluation) for e in probes])
    assert (independent.lower, independent.upper, independent.cells) == (600, 650, 1)

    # 1 マスになった後は、攻撃者が続けても、それ以上は絞れない(次の交渉は、提案せずに終える)
    again = await attack.negotiate()
    assert attack.end_reason(again) == "ended_by_agent"
    assert attack.history == EXPECTED_HISTORY and _observed_answers(attack.last_body) == 5
    assert attack.last_body["groups"][0]["interval"] == {"lower": 600, "upper": 650, "cells": 1}


@pytest.mark.anyio
async def test_the_interval_narrows_within_one_negotiation_and_stops_at_one_cell_when_the_candidate_counters(web_app):
    # AC-12 の「候補者側が対案を返す台本」: 受けられる提案が来ても受けずに対案を出し続けるので、1 つの交渉の中で二分探索が最後まで進む。
    attack = _Attack(web_app, candidate_accepts=False)

    nid = await attack.negotiate()

    assert attack.end_reason(nid) == "ended_by_agent"  # 1 マスになったので、攻撃者が自分で終えた
    assert attack.history == EXPECTED_HISTORY
    (group,) = attack.last_body["groups"]
    assert group["interval"] == {"lower": 600, "upper": 650, "cells": 1} and group["observations"] == 5
    assert Interval(group["interval"]["lower"], group["interval"]["upper"]).contains(RAW_BOUNDARY)
    # 候補者が出した提案(求人側が受け取った提案)は使わない: 候補者側の見え方の offer_received の 5 件だけ
    candidate_events = web_app.store.get_events(nid, "candidate")
    employer_events = web_app.store.get_events(nid, "employer")
    assert sum(e.kind == "offer_received" for e in employer_events) >= 1
    assert sum(e.kind == "offer_received" for e in candidate_events) == 5


# ----------------------------------------------------------------------
# 手作りの交渉で、メーター API の形・組・「本人確認が必要」を確かめる
# ----------------------------------------------------------------------


def _policy_with_a_gap() -> Policy:
    """年収 700 万以上なら受ける、600 万以下は受けない(ほかの軸は中立)。650 万は「本人確認が必要」の隙間。"""
    accept = Anchor(salary=700, remote_days=0, night_duty=8, review_months=12, training="*", side_job="*", start="*")
    reject = Anchor(salary=600, remote_days=5, night_duty=0, review_months=6, training="*", side_job="*", start="*")
    return Policy(side="candidate", accept_anchors=[accept], reject_anchors=[reject])


def _line(salary: int, **axes) -> Package:
    """探索線(SEARCH_LINE)の上の提案。axes で、年収以外の軸を変えると別の線になる。"""
    return Package(salary=salary, **{**SEARCH_LINE, **axes})


def _propose_in_turn(store, nid: str, probes, opening: Package | None = None) -> None:
    """候補者が最初の提案を出し、求人側(攻撃者)が probes を順に提案する(候補者は、そのたびに提案を断って手番を戻す)。

    候補者の最初の提案(opening)は、候補者のポリシーで「受けられる」組み合わせでなければならない(既定の sample_package(salary=900) は、
    ケース 3 のポリシーでも、隙間のあるポリシーでも、既定の「何でも受ける」ポリシーでも受けられる)。
    """
    _move(store, nid, "candidate", "propose", opening if opening is not None else sample_package(salary=900))
    for index, probe in enumerate(probes):
        if index:
            _move(store, nid, "candidate", "reject")
        _move(store, nid, "employer", "propose", probe)


async def _fictional_negotiation(
    web_app, probes=(), *, policy: Policy | None = None, mode: str = "attack", opening: Package | None = None
) -> str:
    """架空人物の交渉を作り、probes を提案させ、web の段の状態も作る。"""
    nid = create_demo_negotiation(web_app.store, candidate_policy=policy, mode=mode)
    _propose_in_turn(web_app.store, nid, probes, opening)
    await web_app.services.stages.ensure(nid, None)
    return nid


@pytest.mark.anyio
async def test_the_meter_response_has_one_group_per_line_with_the_vaults_answers_of_the_candidate(web_app):
    # ケース 3 の候補者に、2 本の線(リモート 1 日と 4 日)の上で提案する。金庫が候補者側として返した 3 値の答えから、線ごとの区間ができる。
    put_fixture_templates(web_app.store._db, CASE3)
    store = web_app.store
    created = store.create_negotiation(
        demo_create_request(CASE3.candidate.template_id, CASE3.employer.template_id, mode="attack")
    )
    nid = created.nid
    _propose_in_turn(store, nid, [_line(900), _line(550), _line(700), _line(600, remote_days=4), _line(550, remote_days=4)])
    await web_app.services.stages.ensure(nid, None)

    response = await web_app.browser().post(METER, {"negotiation_ids": [nid]})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"groups", "narrowest", "note"} and body["note"] == NOTE
    line_one, line_four = body["groups"]  # 観測の多い順
    assert (line_one["axes"], line_one["observations"], line_one["interval"]) == (
        SEARCH_LINE,
        3,
        {"lower": 550, "upper": 700, "cells": 3},
    )
    assert (line_four["axes"]["remote_days"], line_four["observations"], line_four["interval"]) == (
        4,
        2,
        {"lower": 550, "upper": 600, "cells": 1},  # リモート 4 日なら、600 万で受ける・550 万で受けない(生の境目 570 万)
    )
    assert body["narrowest"] == line_four  # 最も狭い組
    # 答えは金庫のもの: 候補者側の見え方の評価をそのまま使っている
    answers = {
        (e.package.salary, e.package.remote_days): e.own_evaluation
        for e in store.get_events(nid, "candidate")
        if e.kind == "offer_received"
    }
    assert answers == {
        (900, 1): "acceptable",
        (550, 1): "not_acceptable",
        (700, 1): "acceptable",
        (600, 4): "acceptable",
        (550, 4): "not_acceptable",
    }


@pytest.mark.anyio
async def test_a_needs_confirmation_answer_from_the_vault_does_not_change_the_interval(web_app):
    # 隙間のある候補者(650 万だけ「本人確認が必要」)。650 万の提案が加わっても、区間は 2 マスのまま、広がりも狭まりもしない。
    policy = _policy_with_a_gap()
    with_gap = await _fictional_negotiation(web_app, [_line(700), _line(600), _line(650)], policy=policy)
    without_gap = await _fictional_negotiation(web_app, [_line(700), _line(600)], policy=policy)
    only_gap = await _fictional_negotiation(web_app, [_line(650)], policy=policy)
    answers = [e.own_evaluation for e in web_app.store.get_events(with_gap, "candidate") if e.kind == "offer_received"]
    assert answers == ["acceptable", "not_acceptable", "needs_confirmation"]
    browser = web_app.browser()

    wide = (await browser.post(METER, {"negotiation_ids": [with_gap]})).json()["groups"][0]
    narrow = (await browser.post(METER, {"negotiation_ids": [without_gap]})).json()["groups"][0]
    nothing = (await browser.post(METER, {"negotiation_ids": [only_gap]})).json()["groups"][0]

    assert wide["interval"] == narrow["interval"] == {"lower": 600, "upper": 700, "cells": 2}
    assert (wide["observations"], narrow["observations"]) == (3, 2)
    assert nothing["interval"] == {"lower": None, "upper": None, "cells": len(SALARY_GRID) + 1}  # 何も分からない


def _strict_policy() -> Policy:
    """年収 800 万以上なら受ける、750 万以下は受けない(ほかの軸は中立)。700 万は「受けられない」。"""
    accept = Anchor(salary=800, remote_days=0, night_duty=8, review_months=12, training="*", side_job="*", start="*")
    reject = Anchor(salary=750, remote_days=5, night_duty=0, review_months=6, training="*", side_job="*", start="*")
    return Policy(side="candidate", accept_anchors=[accept], reject_anchors=[reject])


@pytest.mark.anyio
async def test_negotiations_of_different_candidates_that_contradict_each_other_are_refused_not_a_server_error(web_app):
    # 別々の候補者の交渉を混ぜると、同じ線の上で答えが食い違い得る(一方は 700 万を受け、他方は受けない)。同じ候補者のポリシーは年収について
    # 単調なので、食い違いは起きない。食い違ったときは 500 にせず、422 で知らせる。
    lenient = await _fictional_negotiation(web_app, [_line(700)], policy=_policy_with_a_gap())
    strict = await _fictional_negotiation(web_app, [_line(700)], policy=_strict_policy(), opening=sample_package(salary=900))
    browser = web_app.browser()
    answers = {
        nid: [e.own_evaluation for e in web_app.store.get_events(nid, "candidate") if e.kind == "offer_received"]
        for nid in (lenient, strict)
    }
    assert answers == {lenient: ["acceptable"], strict: ["not_acceptable"]}

    for ids in ([lenient, strict], [strict, lenient]):
        response = await browser.post(METER, {"negotiation_ids": ids})
        assert (response.status_code, response.json()) == (422, {"detail": "inconsistent_answers"})
    # 一方だけなら通る(それぞれは単調)
    assert (await browser.post(METER, {"negotiation_ids": [lenient]})).status_code == 200
    assert (await browser.post(METER, {"negotiation_ids": [strict]})).status_code == 200


@pytest.mark.anyio
async def test_what_the_candidate_agent_did_with_the_offers_is_not_used(web_app):
    # 候補者が提案を断っても、自分で確かめても、区間は変わらない(受けなかったことは「受けられない」を意味しない)。
    store = web_app.store
    nid = create_demo_negotiation(store, mode="attack")  # 候補者は何でも受ける
    _move(store, nid, "candidate", "propose", sample_package(salary=900))
    _move(store, nid, "employer", "propose", _line(300))  # 何でも受ける候補者は「受けられる」(300 万でも)
    _move(store, nid, "candidate", "check", _line(1500))  # 自分で確かめた組み合わせ(攻撃者は見られない)
    _move(store, nid, "candidate", "reject")  # 断った(受けられるのに)
    await web_app.services.stages.ensure(nid, None)
    assert [e.kind for e in store.get_events(nid, "candidate")] == ["propose", "offer_received", "check", "reject"]

    (group,) = (await web_app.browser().post(METER, {"negotiation_ids": [nid]})).json()["groups"]

    assert (group["observations"], group["interval"]) == (1, {"lower": None, "upper": 300, "cells": 1})


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["demo", "attack"])
async def test_both_demo_and_attack_negotiations_are_accepted(web_app, mode):
    nid = await _fictional_negotiation(web_app, [_line(650)], mode=mode)

    response = await web_app.browser().post(METER, {"negotiation_ids": [nid]})

    assert response.status_code == 200 and response.json()["groups"][0]["observations"] == 1


@pytest.mark.anyio
async def test_a_negotiation_without_offers_gives_an_empty_answer(web_app):
    nid = await _fictional_negotiation(web_app)  # 候補者の最初の提案だけ(攻撃者は、まだ何も提案していない)

    response = await web_app.browser().post(METER, {"negotiation_ids": [nid]})

    assert (response.status_code, response.json()) == (200, {"groups": [], "narrowest": None, "note": NOTE})


@pytest.mark.anyio
async def test_the_same_id_twice_is_read_once_and_the_order_of_ids_does_not_matter(web_app):
    first = await _fictional_negotiation(web_app, [_line(900), _line(550)])
    second = await _fictional_negotiation(web_app, [_line(700)])
    browser = web_app.browser()

    once = (await browser.post(METER, {"negotiation_ids": [first, second]})).json()
    twice = (await browser.post(METER, {"negotiation_ids": [second, first, first, second]})).json()

    assert once == twice and once["groups"][0]["observations"] == 3


@pytest.mark.anyio
async def test_twenty_ids_are_accepted_and_twenty_one_or_none_are_refused_before_the_vault_is_read(web_app, monkeypatch):
    assert MAX_NEGOTIATION_IDS == 20
    candidate_template, employer_template = put_candidate_and_employer_templates(web_app.store._db)
    nids = []
    for _ in range(MAX_NEGOTIATION_IDS):
        request = demo_create_request(candidate_template.template_id, employer_template.template_id, mode="attack")
        created = web_app.store.create_negotiation(request)
        await web_app.services.stages.ensure(created.nid, None)
        nids.append(created.nid)
    browser = web_app.browser()
    assert (await browser.post(METER, {"negotiation_ids": nids})).status_code == 200  # ちょうど 20 件

    reads = []
    original = web_app.vault.get_demo_events

    async def recording(nid, side, after_seq=0):
        reads.append(nid)
        return await original(nid, side, after_seq)

    monkeypatch.setattr(web_app.vault, "get_demo_events", recording)
    bad_bodies = [
        {"negotiation_ids": [*nids, nids[0]]},  # 21 件(同じ ID でも、件数で数える)
        {"negotiation_ids": ["0123456789abcdef"] * 21},
        {"negotiation_ids": []},
        {},
        {"negotiation_ids": nids[0]},  # 一覧でない
        {"negotiation_ids": [123]},  # 文字列でない
        {"negotiation_ids": ["x" * 65]},  # 1 件の長さの上限(64)を超える
        {"negotiation_ids": nids[:1], "side": "employer"},  # 余計な項目
    ]
    for body in bad_bodies:
        response = await browser.post(METER, body)
        assert response.status_code == 422, body
        detail = response.json()["detail"]  # 入力の値は返さない(場所と理由の種類だけ)
        assert set(response.json()) == {"detail"} and all(set(error) == {"loc", "msg", "type"} for error in detail)
    assert reads == []  # 金庫には 1 回も触れていない


@pytest.mark.anyio
async def test_one_real_principals_negotiation_among_the_ids_refuses_the_whole_request(web_app):
    # DV-01: メーターの区間の API は、本物の利用者の交渉の ID を拒否する。存在しない交渉・形の違う ID も同じ 403。
    own, demo = web_app.browser(), web_app.browser()
    pid = await own.register()
    real_nid = await own.create_negotiation(pid, web_app.put_employer_template())
    attack_nid = await _fictional_negotiation(web_app, [_line(650)])
    # 本物の交渉にも、候補者側の見え方の offer_received がある(読まれてはならない記録)
    _move(web_app.store, real_nid, "candidate", "propose", sample_package())
    _move(web_app.store, real_nid, "employer", "propose", sample_package(salary=800))
    assert "offer_received" in [e.kind for e in web_app.store.get_events(real_nid, "candidate")]
    # 対照: 架空人物の交渉だけなら通る(何でも断っているのではない)
    assert (await demo.post(METER, {"negotiation_ids": [attack_nid]})).json()["groups"]

    refusals = []
    for ids in (
        [real_nid],
        [attack_nid, real_nid],
        [real_nid, attack_nid],
        [attack_nid, "0123456789abcdef"],  # 存在しない交渉
        [attack_nid, "not-a-negotiation-id"],  # 形の違う ID
        [attack_nid, ""],
    ):
        response = await demo.post(METER, {"negotiation_ids": ids})
        refusals.append((response.status_code, response.json()))
    assert refusals == [(403, {"detail": "forbidden"})] * 6  # 交渉があるかどうか・一覧のどれが断られたかを知らせない


@pytest.mark.anyio
async def test_a_real_negotiation_is_refused_by_the_vault_even_when_its_stage_document_says_it_is_fictional(
    web_app, monkeypatch
):
    # 台帳 X-38: web の段の状態〔補助〕が壊れて「架空」と読めても、金庫〔正本〕が本物の交渉を断る(404 を 403 に写す)。
    own = web_app.browser()
    pid = await own.register()
    real_nid = await own.create_negotiation(pid, web_app.put_employer_template())
    stage = {"nid": real_nid, "candidate_principal_id": None, "stage": 0}
    web_app.default_db.collection("stages").document(real_nid).set(stage)
    assert await web_app.services.stages.is_fictional_negotiation(real_nid)  # web の確認は通ってしまう
    calls = []
    original = web_app.vault.get_demo_events

    async def recording(nid, side, after_seq=0):
        calls.append((nid, side))
        return await original(nid, side, after_seq)

    monkeypatch.setattr(web_app.vault, "get_demo_events", recording)

    response = await web_app.browser().post(METER, {"negotiation_ids": [real_nid]})

    assert (response.status_code, response.json()) == (403, {"detail": "forbidden"})
    assert calls == [(real_nid, "candidate")]  # 金庫に聞いて、金庫が断った


@pytest.mark.anyio
async def test_a_fictional_negotiation_without_a_stage_document_is_refused_until_the_stage_exists(web_app):
    # web の確認は拒否する側に倒す(段の状態がまだない交渉は 403)。作られれば通る。
    nid = create_demo_negotiation(web_app.store, mode="attack")
    browser = web_app.browser()

    assert (await browser.post(METER, {"negotiation_ids": [nid]})).status_code == 403
    await web_app.services.stages.ensure(nid, None)
    assert (await browser.post(METER, {"negotiation_ids": [nid]})).status_code == 200


@pytest.mark.anyio
async def test_the_meter_reads_the_candidate_side_only(web_app, monkeypatch):
    nid = await _fictional_negotiation(web_app, [_line(650)])
    sides = []
    original = web_app.vault.get_demo_events

    async def recording(nid, side, after_seq=0):
        sides.append((side, after_seq))
        return await original(nid, side, after_seq)

    monkeypatch.setattr(web_app.vault, "get_demo_events", recording)

    assert (await web_app.browser().post(METER, {"negotiation_ids": [nid]})).status_code == 200
    assert sides == [("candidate", 0)]  # 求人側(攻撃者)の見え方は読まない


@pytest.mark.anyio
async def test_the_meter_writes_nothing_and_remembers_nothing(web_app):
    # §8.3: web は一覧を覚えない。読むだけで、金庫にも (default) にも書かない。(default) に増えるのは、入口の枠(meter)の回数 rate_limits だけで、
    # 交渉 ID も IP も入らない。
    nid = await _fictional_negotiation(web_app, [_line(900), _line(550)])
    browser = web_app.browser()
    vault_before, default_before = dump_documents(web_app.store._db), dump_documents(web_app.default_db)
    version_before = web_app.store.get_view(nid, "candidate").version

    first = await browser.post(METER, {"negotiation_ids": [nid]})
    second = await browser.post(METER, {"negotiation_ids": [nid]})

    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    default_after = dump_documents(web_app.default_db)
    counters = {path: data for path, data in default_after.items() if path.startswith("rate_limits/")}
    assert dump_documents(web_app.store._db) == vault_before
    assert {path: data for path, data in default_after.items() if path not in counters} == default_before  # 回数のほかは、何も書いていない
    assert sorted(path.split("/")[1].split(".")[0] for path in counters) == ["meter"]  # 入口 meter のクライアントの文書だけ(全体の枠には数えない。台帳 L19-7)
    assert [data["count"] for data in counters.values()] == [2]  # 2 回呼んだ分だけ
    assert nid not in str(counters)
    assert web_app.store.get_view(nid, "candidate").version == version_before


@pytest.mark.anyio
async def test_the_meter_needs_the_custom_header_but_not_a_session_and_leaves_the_session_alone(web_app):
    # DV-01: X-Requested-With のない POST は拒否する。メーターはデモ用の経路なので、セッションは見ない(クッキーがあっても、利用記録を更新せず、
    # クッキーの期限も延ばさない。§6.3)。
    nid = await _fictional_negotiation(web_app, [_line(650)])
    stranger, member = web_app.browser(), web_app.browser()
    pid = await member.register()
    before = web_app.default_db.collection("principals_meta").document(pid).get().to_dict()
    web_app.clock.advance(dt.timedelta(hours=2))  # 1 時間を過ぎているので、セッションを見る経路なら利用記録を更新する

    for browser in (stranger, member):
        refused = await browser.post(METER, {"negotiation_ids": [nid]}, requested_with=False)
        assert (refused.status_code, refused.json()) == (403, {"detail": "missing_requested_with_header"})
        answered = await browser.post(METER, {"negotiation_ids": [nid]})
        assert answered.status_code == 200 and "set-cookie" not in answered.headers

    assert web_app.default_db.collection("principals_meta").document(pid).get().to_dict() == before
    assert (await member.get(f"/v1/principals/{pid}/policy")).status_code == 200  # 対照: セッションを見る経路なら更新される
    assert web_app.default_db.collection("principals_meta").document(pid).get().to_dict() != before


# ----------------------------------------------------------------------
# 防御なしのシミュレーション(API)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_simulation_api_finds_620_in_seven_steps_and_says_it_is_a_simulation(web_app, monkeypatch):
    # 金庫を呼ばない純粋な計算: 金庫の呼び出しが 1 回でもあれば失敗するようにしておく。
    async def boom(*args, **kwargs):
        raise AssertionError("the simulation must not call the vault")

    monkeypatch.setattr(type(web_app.vault), "_send", boom)

    response = await web_app.browser().get(SIMULATION, value=620)

    assert response.status_code == 200
    body = response.json()
    assert body["simulation"] is True and "シミュレーション" in body["note"]
    assert (body["value"], body["found"], body["count"]) == (620, 620, 7) and len(body["steps"]) == 7
    assert body["candidates"] == {"low": 300, "high": 1500, "step": 10}
    assert [(s["ask"], s["at_least"]) for s in body["steps"]] == [
        (900, False),
        (600, True),
        (750, False),
        (670, False),
        (630, False),
        (610, True),
        (620, True),
    ]
    assert (body["steps"][-1]["low"], body["steps"][-1]["high"]) == (620, 620)
    assert "set-cookie" not in response.headers


@pytest.mark.anyio
@pytest.mark.parametrize("value", [300, 1000, 1500])
async def test_the_simulation_api_accepts_the_ends_of_the_range(web_app, value):
    response = await web_app.browser().get(SIMULATION, value=value)

    assert response.status_code == 200 and response.json()["found"] == value and response.json()["count"] <= 7


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["290", "1510", "305", "625", "0", "-10", "abc", "620.5", ""])
async def test_the_simulation_api_refuses_values_that_are_not_on_the_grid(web_app, value):
    response = await web_app.browser().client.get(SIMULATION, params={"value": value})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_the_simulation_api_needs_a_value(web_app):
    assert (await web_app.browser().client.get(SIMULATION)).status_code == 422


@pytest.mark.anyio
async def test_the_meter_routes_are_in_the_openapi_schema_of_the_web_app(web_app):
    paths = web_app.app.openapi()["paths"]

    assert list(paths[METER]) == ["post"] and list(paths[SIMULATION]) == ["get"]


# ----------------------------------------------------------------------
# 入口の枠(web.limits の meter): POST にだけ掛け、シミュレーション(GET。純粋な計算)には掛けない
# ----------------------------------------------------------------------


def _as_client(ip: str) -> dict[str, str]:
    """X-Requested-With と、X-Forwarded-For の末尾(クライアント IP。web.client_ip)。"""
    return {**REQUESTED_WITH, "X-Forwarded-For": ip}


def _rate_limit_documents(env) -> dict[str, dict]:
    return {path: data for path, data in dump_documents(env.default_db).items() if path.startswith("rate_limits/")}


@pytest.mark.anyio
async def test_the_meter_answers_429_over_its_limit_without_reading_anything_and_opens_again_in_the_next_window(make_env, monkeypatch):
    # 1 回で最大 20 件の交渉について金庫と Firestore を読むので、読む前に数える。超えたら 429(Retry-After と、画面が理由を出せる本文)で、
    # 段の状態にも金庫にも触れない。窓が変われば、また通る。上限は 3 回に下げて確かめる(設定ファイルの値は test_the_default_limit_of_the_meter...)。
    env = make_env(rate_limits=small_limits(meter=3))
    nid = await _fictional_negotiation(env, [_line(900), _line(550)])
    reads: list[str] = []
    original_events, original_fictional = env.vault.get_demo_events, env.services.stages.is_fictional_negotiation

    async def recording_events(nid_, side, after_seq=0):
        reads.append("vault")
        return await original_events(nid_, side, after_seq)

    async def recording_fictional(nid_):
        reads.append("stages")
        return await original_fictional(nid_)

    monkeypatch.setattr(env.vault, "get_demo_events", recording_events)
    monkeypatch.setattr(env.services.stages, "is_fictional_negotiation", recording_fictional)
    browser = env.browser()
    body = {"negotiation_ids": [nid]}

    assert [(await browser.post(METER, body)).status_code for _ in range(3)] == [200, 200, 200]
    reads_before = len(reads)
    refused = await browser.post(METER, body)

    assert reads_before >= 3  # 通った分は、読んでいる(確認の前提)
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert {key: detail[key] for key in ("code", "entrance", "scope", "limit", "window_seconds")} == {
        "code": "rate_limited",
        "entrance": "meter",
        "scope": "client",
        "limit": 3,
        "window_seconds": 600,
    }
    assert refused.headers["Retry-After"] == str(detail["retry_after_seconds"]) and 1 <= detail["retry_after_seconds"] <= 600
    assert len(reads) == reads_before  # 断った要求は、段の状態にも金庫にも触れていない

    env.clock.advance(dt.timedelta(seconds=detail["retry_after_seconds"]))  # 次の窓
    assert (await browser.post(METER, body)).status_code == 200


@pytest.mark.anyio
async def test_the_meter_does_not_count_in_the_overall_allowance_so_it_cannot_use_up_the_llm_entrances(make_env):
    # 台帳 L19-7: メーター(LLM を呼ばず、金庫を読むだけ)は、全体の枠に数えない。全体の枠(ここでは 2 回に絞る)より多く呼んでも、断らず、全体の文書も作らない
    # (v20 までは、メーターも全体を消費し、別々の IP で全体の枠が埋まって、本物の利用者の面談・ライブ交渉が 429 になった)。
    env = make_env(rate_limits=dataclasses.replace(small_limits(meter=10), overall_limit=2))
    nid = await _fictional_negotiation(env, [_line(900)])
    browsers = [env.browser() for _ in range(5)]

    statuses = [
        (await browser.client.post(METER, json={"negotiation_ids": [nid]}, headers=_as_client(f"198.51.100.{index}"))).status_code
        for index, browser in enumerate(browsers)  # 別々のクライアントが 1 回ずつ(クライアントごとの枠には当たらない)
    ]

    assert statuses == [200] * 5
    assert not any(path.startswith("rate_limits/overall.") for path in _rate_limit_documents(env))


@pytest.mark.anyio
async def test_the_meter_limit_is_per_client_and_the_simulation_is_neither_limited_nor_counted(make_env):
    env = make_env(rate_limits=small_limits(meter=2))
    nid = await _fictional_negotiation(env, [_line(900), _line(550)])
    browser = env.browser()

    async def post_as(ip: str):
        return await browser.client.post(METER, json={"negotiation_ids": [nid]}, headers=_as_client(ip))

    assert [(await post_as("198.51.100.1")).status_code for _ in range(2)] == [200, 200]
    assert (await post_as("198.51.100.1")).status_code == 429
    assert (await post_as("198.51.100.2")).status_code == 200  # 別のクライアントは、別の枠
    # クライアントは X-Forwarded-For の末尾。利用者が書ける先頭側を変えても、別の枠にならない(台帳 C-3)
    assert (await post_as("203.0.113.9, 198.51.100.1")).status_code == 429

    # シミュレーション(純粋な計算)は、メーターの枠を使い切っていても呼べて、回数も数えない
    counters = _rate_limit_documents(env)
    assert counters  # 数えた文書がある(確認の前提)
    for _ in range(5):
        simulation = await browser.client.get(SIMULATION, params={"value": 620}, headers={"X-Forwarded-For": "198.51.100.1"})
        assert simulation.status_code == 200 and simulation.json()["found"] == 620
    assert _rate_limit_documents(env) == counters


@pytest.mark.anyio
async def test_the_default_limit_of_the_meter_is_60_per_client_in_10_minutes(web_app):
    # 設定ファイルの値(meter = 60)が、本番の組み立ての POST に効いている: 60 回目まで通り、61 回目は 429。
    ip = "198.51.100.1"
    for _ in range(59):  # HTTP を通さずに数える
        await web_app.services.limiter.admit("meter", ip)
    browser = web_app.browser()
    body = {"negotiation_ids": ["0123456789abcdef"]}  # 存在しない交渉(403)。ここでは、枠を通ったかどうかだけを見る

    sixtieth = await browser.client.post(METER, json=body, headers=_as_client(ip))
    sixty_first = await browser.client.post(METER, json=body, headers=_as_client(ip))

    assert sixtieth.status_code == 403  # 枠は通った(中身は、存在しない交渉を断る 403)
    assert sixty_first.status_code == 429
    assert (sixty_first.json()["detail"]["entrance"], sixty_first.json()["detail"]["limit"]) == ("meter", 60)
