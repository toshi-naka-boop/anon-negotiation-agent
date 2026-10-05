"""FR-45: 二分探索の実演(POST /v1/demo/attack/bisection。design.md §8.3・§8.2。台帳 I-24・I-27)。

台本の攻撃者(src/web/attack/scripted.py。LLM を呼ばない)が、本物の金庫とレフェリー(この実行専用のもの)を通して、攻撃モードの交渉の中で
年収を二分探索する。候補者の台本は、受けられる提案を受けて交渉を終える。交渉が終わったら次の交渉を作って続け、区間が 1 マスになるか、
交渉が 3 件になったら止める。返すのは、交渉の ID の一覧と、区間(メーター API と同じ計算)。

確かめること
- 1 マスで止まる: 候補者の境目が見つかれば、3 件より前でも止まる。ケース 3 の候補者(境目 620 万)では、3 件で「600 万より上、650 万以下」の 1 マス。
- 3 件で止まる: 1 マスに届かなくても、4 件目は作らない(何でも受ける候補者では、3 件目の後も区間は広い)。
- 区間は、メーター API(POST /v1/demo/meter)に同じ交渉の ID を渡したときの区間と同じ。
- 本物の依頼者の交渉には触れない(セッションを見ない・作るのは設定の架空人物との攻撃の交渉だけ)。LLM は呼ばず、LLM の枠(1 日・交渉ごと)も減らさない。
- 見回りは、実行中の交渉に、本物のレフェリー(LLM を呼ぶ)を起こさない。失敗・時間切れのときは交渉を取り消す。
- 入口の枠 attack_create と入場の制限は、交渉 1 件ごとに通す(台帳 L19-8)。1 件目で断られたら 429(何も作らない)。2 件目以降で枠が尽きたら、そこまでの ID と区間に
  stopped_reason=rate_limited を付けて返す。同じ request_id で呼び直せば、できた交渉は作り直さずに、続きから進む。
- 同じ request_id の再送は、同じ結果を返し、何も作り直さない。同時の要求は 1 つずつ。
金庫は本物の vault の app を ASGI のままつなぎ、エージェントは台本(IdleAgents。呼ばれたら記録して止まる)。
"""

import asyncio
import dataclasses
import datetime as dt
import json

import pytest
from agents_helpers import valid_data
from attack_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    CREATE,
    create_body,
    make_env,
    message_of_size,
    post,
    put_attack_templates,
    small_limits,
    threshold_candidate_policy,
    vault_document,
)
from negotiation_core import Move, TurnInput, parse_plan
from negotiation_core.estimate_interval import Interval
from scripted_negotiators import SEARCH_LINE, is_on_search_line
from vault.api_models import CreateNegotiationResponse
from vault.fixtures import load_case_fixture, put_fixture_templates
from web.attack import bisection
from web.attack.scripted import (
    CANDIDATE_OPENING,
    SCRIPTED_USAGE,
    ScriptedAttacker,
    ScriptedBisectionAgents,
    candidate_move,
    probe_package,
)
from web.config import DEFAULT_WEB_CONFIG

pytestmark = pytest.mark.anyio

BISECTION = "/v1/demo/attack/bisection"
METER = "/v1/demo/meter"
CASE3 = load_case_fixture(3)
# 二分探索の 5 手(tests/test_meter.py と同じ。ケース 3 の候補者が、受けて終わる台本のとき)。900 万から始め、区間の真ん中を提案する。
CASE3_PROBES = [900, 550, 700, 600, 650]


def body(index: int = 1) -> dict:
    return {"request_id": f"bisect-{index:04d}"}


async def run(browser, index: int = 1, *, ip: str | None = None):
    """二分探索の実演を 1 回動かす(止まらずに待ち続けないよう、時間を区切る。台本の実行は、数秒で終わる)。"""
    return await asyncio.wait_for(post(browser, BISECTION, body(index), ip=ip), 90)


def end_reasons(store, nids: list[str]) -> list[str]:
    return [vault_document(store, nid).end_reason for nid in nids]


def open_nids(store) -> list[str]:
    """進行中の交渉の ID(見回りが一覧で拾うもの)。"""
    return [item.nid for item in store.list_open_negotiations().items]


def received_probes(store, nids: list[str]) -> list[int]:
    """候補者側が受け取った攻撃者の提案の年収(交渉を作った順)。"""
    return [
        event.package.salary
        for nid in nids
        for event in store.get_events(nid, "candidate")
        if event.kind == "offer_received"
    ]


def interval_of(response_json: dict) -> tuple[int | None, int | None, int]:
    interval = response_json["interval"]
    return interval["lower"], interval["upper"], interval["cells"]


# ----------------------------------------------------------------------
# 台本(LLM を使わない。エージェントの形で動く)
# ----------------------------------------------------------------------


def candidate_turn(pending: dict | None) -> TurnInput:
    """候補者の手番の TurnInput(攻撃者の提案とその評価が pending)。"""
    data = valid_data("candidate")
    data["pending_offer"] = pending
    return TurnInput.model_validate_json(json.dumps(data))  # 線の上と同じく JSON から作る(strict なので、文字列を列挙型に直す)


def test_the_scripted_candidate_proposes_first_accepts_what_it_can_accept_and_declines_the_rest():
    offer = {"package": probe_package(700).model_dump(mode="json")}

    assert candidate_move(candidate_turn(None)) == ("propose", CANDIDATE_OPENING)
    assert candidate_move(candidate_turn({**offer, "own_evaluation": "acceptable"})) == ("accept", None)
    for verdict in ("not_acceptable", "needs_confirmation"):  # 受けられない・決めきれないものは、断る(途中確認は使わない)
        assert candidate_move(candidate_turn({**offer, "own_evaluation": verdict})) == ("reject", None)


async def test_the_scripted_agents_answer_as_a_plan_and_as_a_move_for_the_two_roles_and_know_no_employer():
    async def nothing_seen(_nid):
        return []  # まだ金庫の答えが 1 つもない: 区間は何も分からないので、グリッドの真ん中(900 万)から尋ねる

    agents = ScriptedBisectionAgents(ScriptedAttacker(nothing_seen))
    plan_input = candidate_turn(None)
    decide_input = TurnInput.model_validate_json(json.dumps(valid_data("candidate", "decide")))

    plan_payload, usage = await agents("attacker", plan_input, nid="0" * 16, timeout_s=1.0)
    plan = parse_plan(plan_payload)  # レフェリーの実際の読み方(PlanEnvelope → Plan)を通る
    assert (plan.checks, plan.move, plan.package) == ([], "propose", probe_package(900))
    assert usage == SCRIPTED_USAGE and usage.prompt_tokens == usage.output_tokens == 0  # LLM は呼ばない
    move = Move.model_validate((await agents("attacker", decide_input, nid="0" * 16, timeout_s=1.0))[0])
    assert (move.move, move.package) == ("propose", probe_package(900))
    candidate_plan = parse_plan((await agents("candidate", plan_input, nid="0" * 16, timeout_s=1.0))[0])
    assert (candidate_plan.move, candidate_plan.package) == ("propose", CANDIDATE_OPENING)
    with pytest.raises(ValueError):
        await agents("employer", plan_input, nid="0" * 16, timeout_s=1.0)


# ----------------------------------------------------------------------
# 二分探索の実演(API)
# ----------------------------------------------------------------------


async def test_the_case3_candidate_is_narrowed_to_one_cell_in_three_negotiations(make_env, store):
    # AC-12 をこの口で: 本番の相手(ケース 3 の候補者 demo-candidate-1 と、何でも受ける求人)に、台本の攻撃者が 5 手で 1 マスに届く。
    # 候補者が受けるたびに交渉が終わるので、3 件の交渉をまたぐ。生の境目 620 万は、区間(600 万より上、650 万以下)の中。
    env = make_env()
    put_fixture_templates(store._db, CASE3)

    response = await run(env.browser())

    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"negotiation_ids", "interval", "stopped_reason"}
    assert result["stopped_reason"] is None  # 1 マスに届いて止まった(枠が尽きたのではない)
    nids = result["negotiation_ids"]
    assert len(nids) == 3 and len(set(nids)) == 3
    assert result["interval"] == {"lower": 600, "upper": 650, "cells": 1}  # 600 万より上、650 万以下。これ以上は絞れない
    assert Interval(600, 650).contains(620)  # 候補者の生の境目(ケース 3 の探索線の上で 620 万)を含む
    assert received_probes(store, nids) == CASE3_PROBES
    assert end_reasons(store, nids) == ["agreed", "agreed", "agreed"]  # 候補者が受けたので、どの交渉も合意で終わった


async def test_it_stops_before_the_third_negotiation_when_the_boundary_is_found_earlier(make_env, store):
    # 「900 万以上なら受ける」候補者: 1 件目は 900 万の提案を受けて終わる。2 件目は、550・700・800・850 万を断り続け、区間が 1 マス
    # (850 万より上、900 万以下)になったので、攻撃者が自分で終えて、3 件目は作らない。
    env = make_env()
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(900))

    result = (await run(env.browser())).json()

    nids = result["negotiation_ids"]
    assert len(nids) == 2
    assert interval_of(result) == (850, 900, 1)
    assert received_probes(store, nids) == [900, 550, 700, 800, 850]
    assert end_reasons(store, nids) == ["agreed", "ended_by_agent"]
    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:3") is None


async def test_it_stops_at_one_cell_in_the_third_negotiation_when_the_boundary_needs_all_five_probes(make_env, store):
    # 「700 万以上なら受ける」候補者: 900(受ける)・550(断る)・700(受ける)・600(断る)・650(断る)。3 件目で 1 マス(650 万より上、700 万以下)。
    env = make_env()
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))

    result = (await run(env.browser())).json()

    nids = result["negotiation_ids"]
    assert len(nids) == 3
    assert interval_of(result) == (650, 700, 1)
    assert received_probes(store, nids) == [900, 550, 700, 600, 650]
    assert end_reasons(store, nids) == ["agreed", "agreed", "ended_by_agent"]


async def test_it_stops_at_three_negotiations_even_when_the_interval_is_still_wide(make_env, store):
    # 何でも受ける候補者(攻撃の相手の既定): 900・550・400 万の提案を、それぞれ 1 件ずつの交渉で受ける。3 件目の後も区間は広い
    # (400 万以下の 3 マス)。それでも、4 件目は作らない(交渉 3 件で止める)。
    env = make_env()

    result = (await run(env.browser())).json()

    nids = result["negotiation_ids"]
    assert len(nids) == 3
    assert interval_of(result) == (None, 400, 3)
    assert received_probes(store, nids) == [900, 550, 400]
    assert end_reasons(store, nids) == ["agreed"] * 3
    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:4") is None


async def test_the_returned_interval_is_the_one_the_meter_api_computes_for_the_same_negotiations(make_env, store):
    env = make_env()
    put_fixture_templates(store._db, CASE3)
    browser = env.browser()

    result = (await run(browser)).json()
    metered = (await post(browser, METER, {"negotiation_ids": result["negotiation_ids"]})).json()

    assert metered["narrowest"]["interval"] == result["interval"]
    (group,) = metered["groups"]  # 攻撃者は探索線の上でしか提案しないので、組は 1 つ
    assert group["axes"] == SEARCH_LINE and group["observations"] == 5
    # 攻撃者が出した提案は、すべて探索線の上(年収だけが違う)
    for nid in result["negotiation_ids"]:
        offers = [event.package for event in store.get_events(nid, "candidate") if event.kind == "offer_received"]
        assert all(is_on_search_line(package) for package in offers)


# ----------------------------------------------------------------------
# 本物の依頼者・LLM の枠には触れない
# ----------------------------------------------------------------------


async def test_a_real_principals_negotiation_and_session_are_not_touched_and_only_attack_negotiations_are_created(
    make_env, store, default_db, vault_client
):
    # 本物の依頼者の交渉(ライブ)がある状態で、その依頼者のクッキーを持ったまま実演を動かす。この口はセッションを見ない(クッキーを
    # 発行も延長もしない)。作るのは、設定の架空の候補者との攻撃の交渉だけ(mode=attack・架空の候補者・本物の依頼者なし)。
    created_requests = []
    original = vault_client.create_negotiation

    async def spy(request):
        created_requests.append(request)
        return await original(request)

    vault_client.create_negotiation = spy
    env = make_env()
    owner = env.browser()
    pid = await owner.register()
    live = await owner.create_negotiation(pid, env.put_employer_template())
    created_requests.clear()  # ここから先が、実演が作ったもの
    snapshot = lambda: (  # noqa: E731
        store.get_view(live, "candidate"),
        store.get_events(live, "candidate"),
        store.get_events(live, "employer"),
        default_db.collection("principals_meta").document(pid).get().to_dict(),
        default_db.collection("stages").document(live).get().to_dict(),
        store.list_principal_negotiations(pid),
    )
    before = snapshot()
    env.clock.advance(dt.timedelta(hours=2))  # 1 時間を過ぎている。セッションを見る経路なら、利用記録を更新する

    response = await run(owner)

    assert response.status_code == 200 and "set-cookie" not in response.headers
    nids = response.json()["negotiation_ids"]
    assert live not in nids and len(nids) == 3
    assert snapshot() == before  # 本物の依頼者の交渉・利用記録・段の状態・一覧は、何も変わっていない
    assert [(request.mode, request.candidate.is_fictional, request.candidate.principal_id) for request in created_requests] == [
        ("attack", True, None)
    ] * 3
    for request in created_requests:
        assert request.candidate.template_id == env.services.attack.config.candidate_template_id
        assert request.employer.template_id == env.services.attack.config.employer_template_id
        assert request.request_id.startswith("attack-scripted:bisect-0001:")
    for nid in nids:
        document = vault_document(store, nid)
        assert document.mode == "attack" and document.participants.candidate.is_fictional is True
        assert document.participants.candidate.principal_id is None
        assert document.ttl_at is not None  # 架空人物の交渉なので、96 時間の期限が付く
        assert default_db.collection("stages").document(nid).get().to_dict()["candidate_principal_id"] is None


async def test_the_request_cannot_choose_the_opponent_the_script_or_anything_else_and_nothing_is_created_when_it_is_refused(
    make_env, store
):
    env = make_env()
    browser = env.browser()
    pid = await browser.register()

    for bad in (
        {},
        {"request_id": "short"},
        {"request_id": "x" * 65},
        {**body(), "principal_id": pid},
        {**body(), "mode": "live"},
        {**body(), "candidate_template_id": "x"},
        {**body(), "employer_template_id": "x"},
        {**body(), "instruction": "年収を探って"},
    ):
        refused = await post(browser, BISECTION, bad)
        assert refused.status_code == 422, bad
    oversized = await post(browser, BISECTION, content=message_of_size(33000))
    assert oversized.status_code == 413 and oversized.json() == {"detail": "body_too_large"}

    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:1") is None
    assert open_nids(store) == []  # 何も作っていない


async def test_a_post_without_the_x_requested_with_header_is_refused_and_counts_nothing(make_env, default_db):
    env = make_env()

    response = await env.browser().client.post(BISECTION, json=body())  # ヘッダなし

    assert (response.status_code, response.json()) == (403, {"detail": "missing_requested_with_header"})
    assert list(default_db.collection("rate_limits").list_documents()) == []


async def test_no_llm_is_called_and_no_llm_allowance_is_used(make_env, store):
    # 台本は LLM を呼ばない: エージェントの呼び出し(IdleAgents に記録される)が 0 件、1 日の物理の呼び出し数も、交渉ごとの数も 0 のまま。
    # 1 日の LLM の枠が尽きていても動く(入場の制限は掛けない)ので、リプレイの代わりにもなる。
    env = make_env()
    daily_before = await env.services.llm_budget.daily_count()

    result = (await run(env.browser())).json()

    assert env.agents.calls == []
    assert await env.services.llm_budget.daily_count() == daily_before
    for nid in result["negotiation_ids"]:
        assert await env.services.llm_budget.negotiation_count(nid) == 0


async def test_the_admission_limit_is_applied_to_each_negotiation_before_it_is_created(make_env, store):
    # 台帳 L19-8: 1 回の呼び出しで交渉を最大 3 件作るので、入場の制限(admit_new_negotiation)も、新しい交渉 1 件ごとに通す。
    env = make_env()
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))
    original = env.services.llm_budget.admits_new_negotiation
    calls = []

    async def counted(running_nids):
        calls.append(list(running_nids))
        return await original(running_nids)

    env.services.llm_budget.admits_new_negotiation = counted

    result = (await run(env.browser())).json()

    assert len(result["negotiation_ids"]) == 3 and len(calls) == 3  # 交渉 1 件につき、1 回
    assert all(not running for running in calls)  # 前の交渉は終わってから、次を作る(進行中の未消化分に、前の交渉は入らない)


async def test_when_the_daily_llm_allowance_is_used_up_the_first_negotiation_is_refused_and_nothing_is_created(make_env):
    full = dataclasses.replace(DEFAULT_WEB_CONFIG.llm_budget, daily_limit=40, per_negotiation_limit=44)  # 新しい交渉 1 件ぶん(44)が入らない
    env = make_env(config=dataclasses.replace(DEFAULT_WEB_CONFIG, llm_budget=full))

    response = await run(env.browser())

    assert (response.status_code, response.json()) == (429, {"detail": "daily_limit_reached"})  # ふつうの攻撃の作成と同じ断り方
    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:1") is None


async def test_when_the_admission_limit_refuses_the_second_negotiation_the_result_so_far_is_returned(make_env, store):
    env = make_env()
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))
    original = env.services.llm_budget.admits_new_negotiation
    calls = []

    async def refuse_from_the_second(running_nids):
        calls.append(1)
        return await original(running_nids) if len(calls) == 1 else False  # 1 件目は通し、2 件目から断る(1 日の枠が尽きた)

    env.services.llm_budget.admits_new_negotiation = refuse_from_the_second

    response = await run(env.browser())

    assert response.status_code == 200
    result = response.json()
    assert len(result["negotiation_ids"]) == 1 and result["stopped_reason"] == "rate_limited"
    assert interval_of(result) == (None, 900, 13)  # 1 件目の 900 万円の提案(受ける)だけが分かっている区間
    assert end_reasons(store, result["negotiation_ids"]) == ["agreed"]  # 作った交渉は、終わっている
    assert open_nids(store) == []
    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:2") is None  # 2 件目は作っていない


# ----------------------------------------------------------------------
# 入口の枠・冪等・並行
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("limit", "created"), [(1, 1), (2, 2), (3, 3)])
async def test_each_negotiation_uses_one_attack_create_allowance_and_a_run_stops_where_it_runs_out(make_env, store, limit, created):
    # 入口の枠は attack_create で、交渉 1 件ごとに 1 つ(1 件目は入口の依存が、2 件目以降は作る前に数える。台帳 L19-8)。枠が 1 つなら、2 件目で尽きる
    # (3 件のうち 2 件目で枠が尽きる): 1 件目だけ作って、stopped_reason=rate_limited で返す。3 つあれば、3 件作って、止める理由はない(1 マスか 3 件)。
    env = make_env(rate_limits=small_limits(attack_create=limit))
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))
    browser = env.browser()

    response = await run(browser, 1)

    assert response.status_code == 200
    result = response.json()
    assert len(result["negotiation_ids"]) == created
    assert result["stopped_reason"] == ("rate_limited" if created < 3 else None)
    assert end_reasons(store, result["negotiation_ids"])[:1] == ["agreed"]
    # 枠を使い切った: 別の要求は、入口の依存が 429 にする(何も作らない)
    refused = await run(browser, 2) if limit < 3 else None
    if refused is not None:
        assert refused.status_code == 429
        assert refused.headers["Retry-After"] == "600"
        assert refused.json() == {
            "detail": {
                "code": "rate_limited",
                "entrance": "attack_create",
                "scope": "client",
                "limit": limit,
                "window_seconds": 600,
                "retry_after_seconds": 600,
            }
        }
        assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0002:1") is None


async def test_when_the_allowance_runs_out_at_the_second_of_three_negotiations_the_result_so_far_is_returned(make_env, store):
    # 台帳 L19-8 の指定のケース: 枠が 1 つ(1 件目の分だけ)。2 件目の前に尽きるので、1 件目の ID と区間を、stopped_reason つきで返す。
    # 区間は、1 件目の提案(900 万円。何でも受ける候補者なので、受ける)だけで作った、広いもの。
    env = make_env(rate_limits=small_limits(attack_create=1))

    response = await run(env.browser())

    assert response.status_code == 200
    (nid,) = response.json()["negotiation_ids"]
    assert response.json() == {
        "negotiation_ids": [nid],
        "interval": {"lower": None, "upper": 900, "cells": 13},
        "stopped_reason": "rate_limited",
    }
    assert end_reasons(store, [nid]) == ["agreed"]
    assert open_nids(store) == []


async def test_the_allowance_is_shared_with_the_ordinary_attack_creation_and_a_refused_first_negotiation_creates_nothing(
    make_env, store
):
    env = make_env(rate_limits=small_limits(attack_create=2))
    browser = env.browser()
    assert (await post(browser, CREATE, create_body(1))).status_code == 200  # ふつうの攻撃の作成で、1 つ
    assert (await post(browser, CREATE, create_body(2))).status_code == 200  # もう 1 つ(枠は 2)

    refused = await run(browser)  # 1 件目の枠で断られる: 429(何も作らない)

    assert refused.status_code == 429 and refused.json()["detail"]["entrance"] == "attack_create"
    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:1") is None
    # 別の入口(攻撃の指示)の枠は使える(枠は入口ごとに別)。交渉がないので 404 だが、429 ではない
    unknown = await post(browser, f"{CREATE}/0123456789abcdef/instruction", {"instruction": "x"})
    assert unknown.status_code == 404


async def test_a_run_that_stopped_for_the_limit_continues_from_where_it_stopped_with_the_same_request_id(make_env, store, vault_client):
    created = []
    original = vault_client.create_negotiation

    async def spy(request):
        created.append(request.request_id)
        return await original(request)

    vault_client.create_negotiation = spy
    env = make_env(rate_limits=small_limits(attack_create=2))
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))
    browser = env.browser()

    first = (await run(browser)).json()  # 入口の依存で 1 つ、2 件目の前に 1 つ。3 件目の前で尽きる

    assert len(first["negotiation_ids"]) == 2 and first["stopped_reason"] == "rate_limited"
    assert interval_of(first) == (550, 700, 3)  # 3 件目までは分からない: まだ 3 マス
    env.clock.advance(dt.timedelta(minutes=11))  # 窓が変わって、枠が戻る
    again = (await run(browser)).json()  # 同じ request_id の呼び直し

    assert again["negotiation_ids"][:2] == first["negotiation_ids"]  # できた交渉は、作り直さずに使う
    assert len(again["negotiation_ids"]) == 3 and again["stopped_reason"] is None
    assert interval_of(again) == (650, 700, 1)
    assert len(created) == 3 and len(set(created)) == 3  # 作ったのは、全部で 3 件(続きの 1 件だけを、新しく作った)


async def test_a_resent_request_returns_the_same_result_and_creates_nothing_new(make_env, store, vault_client):
    created = []
    original = vault_client.create_negotiation

    async def spy(request):
        created.append(request.request_id)
        return await original(request)

    vault_client.create_negotiation = spy
    env = make_env()
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))
    browser = env.browser()

    first = await run(browser)
    created_by_the_first = list(created)
    again = await run(browser)

    assert again.status_code == 200 and again.json() == first.json()
    assert created == created_by_the_first and len(created) == 3  # 再送は、何も作り直さない
    assert len(set(created)) == 3  # 交渉ごとに別の request_id


async def test_requests_with_the_same_request_id_at_the_same_time_are_processed_one_at_a_time(make_env, store, vault_client):
    created = []
    original = vault_client.create_negotiation

    async def spy(request):
        created.append(request.request_id)
        return await original(request)

    vault_client.create_negotiation = spy
    env = make_env()
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))

    first, second = await asyncio.gather(run(env.browser()), run(env.browser()))

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(created) == 3  # 交渉は 1 回ずつしか作っていない(2 つ目の要求は、1 つ目が終わってから、同じ交渉を見つけて同じ結果を返した)
    assert interval_of(first.json()) == (650, 700, 1)


async def test_different_requests_run_independently_and_each_sees_only_its_own_negotiations(make_env, store):
    env = make_env()
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))

    first, second = await asyncio.gather(run(env.browser(), 1), run(env.browser(), 2))

    first_nids, second_nids = first.json()["negotiation_ids"], second.json()["negotiation_ids"]
    assert len(first_nids) == len(second_nids) == 3 and not set(first_nids) & set(second_nids)
    for result, nids in ((first.json(), first_nids), (second.json(), second_nids)):
        assert interval_of(result) == (650, 700, 1)
        assert received_probes(store, nids) == [900, 550, 700, 600, 650]  # 相手の実行の答えを混ぜずに、自分の交渉だけで二分探索した


async def test_a_refusal_by_the_vault_is_answered_with_its_reason(make_env, vault_client):
    async def refuse(_request):
        return CreateNegotiationResponse(status="refused", reason="already_active")

    vault_client.create_negotiation = refuse
    env = make_env()

    response = await run(env.browser())

    assert (response.status_code, response.json()) == (409, {"detail": "already_active"})


# ----------------------------------------------------------------------
# 見回りとの競合・失敗・時間切れ
# ----------------------------------------------------------------------


async def test_the_sweeper_does_not_start_a_real_referee_for_a_running_scripted_negotiation(make_env, store):
    # 見回りは、動いているタスクのない進行中の交渉に、本物のレフェリー(LLM を呼ぶ。攻撃の指示がなければ取消にする)を起こす。この実行の交渉は、
    # 動かしている間、動いているタスクとして登録されているので、起こされない。実行の途中(攻撃者の最初の手番)で、見回りを 1 回走らせて確かめる。
    env = make_env()
    env.enable_referees()  # 本番と同じく、見回りが本物のレフェリーを起こせる状態にする
    put_attack_templates(store, candidate_policy=threshold_candidate_policy(700))
    sweeps = []
    original = env.services.vault.get_demo_events

    async def sweeping(nid, side, after_seq=0):
        if not sweeps:
            sweeps.append(await env.services.sweeper.sweep_once())
        return await original(nid, side, after_seq)

    env.services.vault.get_demo_events = sweeping

    response = await run(env.browser())

    assert response.status_code == 200 and len(response.json()["negotiation_ids"]) == 3
    (report,) = sweeps
    assert report.listed >= 1 and report.tasks_started == 0  # 進行中の交渉を見つけたが、動いているタスクがあるので、起こさなかった
    assert env.agents.calls == []  # 本物のエージェント(LLM)は、1 度も呼ばれていない
    assert interval_of(response.json()) == (650, 700, 1)


async def test_a_failure_in_the_middle_cancels_the_negotiation_so_that_nothing_else_runs_it(make_env, store, monkeypatch):
    # 終わっていない攻撃の交渉を残すと、見回りが拾って、本物のレフェリーが LLM を呼ぶ。失敗したら、金庫の取消で終わらせる。
    env = make_env()

    async def fail(*_args, **_kwargs):
        raise RuntimeError("the stage store is down")

    monkeypatch.setattr(env.services.stages, "ensure", fail)

    with pytest.raises(RuntimeError):
        await run(env.browser())

    nid = await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:1")
    assert nid is not None
    document = vault_document(store, nid)
    assert (document.status, document.end_reason) == ("judged", "cancelled")  # 取消(結果は「なし」)で終わっている
    assert open_nids(store) == []  # 見回りが拾う、進行中の交渉は残っていない
    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:2") is None


async def test_a_negotiation_that_does_not_finish_in_time_is_cancelled_and_answered_with_504(make_env, store, monkeypatch):
    env = make_env()
    monkeypatch.setattr(bisection, "NEGOTIATION_TIMEOUT_SECONDS", 0.5)

    async def never_moves(self, _nid):
        await asyncio.Event().wait()

    monkeypatch.setattr(ScriptedAttacker, "next_move", never_moves)  # 攻撃者の最初の手番で止まる(候補者は先に最初の提案を出す)

    response = await run(env.browser())

    assert (response.status_code, response.json()) == (504, {"detail": "bisection_timeout"})
    nid = await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:1")
    assert end_reasons(store, [nid]) == ["cancelled"]
    assert open_nids(store) == []
    assert await env.vault.get_negotiation_by_request("attack-scripted:bisect-0001:2") is None
