"""結合(1d-2): web のレフェリー → A2A → agents(スタブの LLM)→ 金庫 の経路(design.md §4.1・§4.3・§6.3)。

web の app・agents の app・金庫の app を、すべて本物のまま ASGI でつなぐ(httpx の ASGITransport)。LLM だけが
スタブ(tests/agents_helpers.py の StubLlm)。web はエージェントを、agents.client.send_turn に設定の base_url を
束ねた関数で呼ぶ(スタブの send_turn は差し込まない)。時計は注入(FixedClock)で、テストは sleep しない(FakeSleep)。

- デモの交渉が、web の API で作られ、判定(judged)まで進む。
- 本物の候補者が、面談の送信 → 交渉の作成の API を通って、判定まで進む(段の状態にも期限の項目は付かない)。
- 起動(lifespan)で、交渉の見回りと依頼者の見回りが動き、見回りがレフェリーのタスクを作る。
"""

import asyncio
import datetime as dt

import httpx
import pytest
from agents_helpers import PACKAGE, agents_app, move_json, stub_llm  # noqa: F401  (フィクスチャは import して使う)

import agents.client as agents_client_module
from vault_helpers import put_candidate_and_employer_templates, sample_package
from web_app_helpers import build_web_env
from web_helpers import create_demo_negotiation

_AGENTS_URL = "http://agents.test"


class RecordingTransport(httpx.AsyncBaseTransport):
    """web から agents への通信を記録して、agents の app に ASGI のままつなぐ。"""

    def __init__(self, app) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.urls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.urls.append(str(request.url))
        return await self._inner.handle_async_request(request)


@pytest.fixture
def agents_transport(monkeypatch, agents_app) -> RecordingTransport:
    """agents.client.send_turn が使う HTTP の通信路を、本物の agents の app(スタブの LLM)につなぐ。"""
    transport = RecordingTransport(agents_app)
    monkeypatch.setattr(
        agents_client_module,
        "_open_http_client",
        lambda timeout_s: httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout_s)),
    )
    return transport


@pytest.fixture
async def wired(store, clock, vault_client, default_db, session_key, agents_transport):
    """本物の agents.client.send_turn(base_url を束ねたもの)で agents につないだ web。レフェリーも動かす。"""
    env = build_web_env(
        store=store,
        clock=clock,
        vault=vault_client,
        default_db=default_db,
        session_key=session_key,
        agents_base_url=_AGENTS_URL,
        use_stub_agents=False,
        run_referees=True,
    )
    yield env
    await env.aclose()


def _scripted_llm(stub_llm, *moves: str) -> None:
    """スタブの LLM が、呼ばれた順に、moves(Move の JSON)を返すようにする。"""
    remaining = list(moves)
    stub_llm.behavior = lambda _request: remaining.pop(0)


@pytest.mark.anyio
async def test_a_demo_negotiation_runs_from_the_web_api_through_a2a_and_the_agents_to_the_vault_until_judged(
    store, wired, stub_llm, agents_transport
):
    # 結合: デモの交渉が、web のレフェリー → A2A → agents(スタブの LLM)→ 金庫 の経路で、終了(judged)まで進む。
    # 候補者側の LLM が提案し、求人側の LLM が受ける。金庫が合意と判定を 1 つのトランザクションで行い、双方に
    # 同じ最終結果を記録する。
    _scripted_llm(stub_llm, move_json("propose", PACKAGE), move_json("accept"))
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    browser = wired.browser()

    created = await browser.post(
        "/v1/demo/negotiations",
        {
            "request_id": "request-demo1",
            "candidate_template_id": candidate_template.template_id,
            "employer_template_id": employer_template.template_id,
        },
    )
    assert created.status_code == 200
    nid = created.json()["nid"]
    await asyncio.wait_for(wired.services.referees.task(nid), 30)  # レフェリーのタスクが、終わりまで進めた

    assert store.get_view(nid, "candidate").status == "judged"
    assert store._negotiation_ref(nid).get().to_dict()["end_reason"] == "agreed"
    candidate_events = (await browser.get(f"/v1/demo/negotiations/{nid}/events", side="candidate")).json()
    employer_events = (await browser.get(f"/v1/demo/negotiations/{nid}/events", side="employer")).json()
    assert [e["kind"] for e in candidate_events] == ["propose", "final_result"]
    assert [e["kind"] for e in employer_events] == ["offer_received", "final_result"]
    final = candidate_events[-1]["result"]
    assert final["likelihood"] == "high"  # どちらも何でも受けるので、両者が受けられる組み合わせは十分に広い
    assert final["package"] == PACKAGE
    assert employer_events[-1]["result"] == final  # 双方に同じ最終結果
    # 経路: 設定の base_url を束ねた A2A の受信口(候補者側 → 求人側の順)を、LLM まで通った。
    assert agents_transport.urls == [f"{_AGENTS_URL}/a2a/candidate", f"{_AGENTS_URL}/a2a/employer"]
    assert len(stub_llm.requests) == 2
    # 段の状態も作られ、架空の候補者なので期限の項目がある(台帳 I-6)。
    stage = wired.default_db.collection("stages").document(nid).get().to_dict()
    assert stage["candidate_principal_id"] is None and "ttl_at" in stage


@pytest.mark.anyio
async def test_a_real_principals_negotiation_runs_from_the_interview_through_the_agents_until_judged(
    store, wired, stub_llm
):
    # 結合: 本物の候補者が、開始ページ → 面談の送信 → 交渉の作成の API を通り、レフェリー → A2A → agents → 金庫
    # の経路で判定まで進む。本人の一覧・活動ログに結果が出て、本人の側の見え方しか出ない。段の状態に期限は付かない。
    package = sample_package()  # 面談で丸めた受ける条件(年収 650 万以上・リモート 2 日以上・当直 4 回以下)を満たす
    _scripted_llm(stub_llm, move_json("propose", package.model_dump()), move_json("accept"))
    browser = wired.browser()
    pid = await browser.register()
    template_id = wired.put_employer_template()

    nid = await browser.create_negotiation(pid, template_id)
    await asyncio.wait_for(wired.services.referees.task(nid), 30)

    listing = (await browser.get(f"/v1/principals/{pid}/negotiations")).json()
    assert [(item["nid"], item["state"]) for item in listing] == [(nid, "ended")]
    assert listing[0]["result"]["likelihood"] == "high"
    assert listing[0]["result"]["package"] == package.model_dump()
    events = (await browser.get(f"/v1/negotiations/{nid}/events")).json()
    assert [e["kind"] for e in events] == ["propose", "final_result"]  # 本人(候補者)の側の見え方だけ
    stage = wired.default_db.collection("stages").document(nid).get().to_dict()
    assert stage["candidate_principal_id"] == pid
    assert "ttl_at" not in stage
    # 終わった後は、新しい交渉を作れる(本物の候補者は、進行中の交渉を同時に 1 件までしか持てない)。
    again = await browser.post(
        f"/v1/principals/{pid}/negotiations",
        {"request_id": "request-0002", "employer_template_id": wired.put_employer_template()},
    )
    assert again.status_code == 200 and again.json()["nid"] != nid


@pytest.mark.anyio
async def test_the_startup_runs_both_sweepers_and_the_sweeper_starts_the_referee(store, wired, stub_llm):
    # 起動(lifespan)で、交渉の見回り(60 秒ごと)と依頼者の見回り(10 分ごと)が動く。起動時の見回りが、金庫の一覧から
    # レフェリーのタスクを作り(交渉は、A2A → agents → 金庫 の経路で判定まで進む)、期限切れの依頼者を消す。
    # 止めるときは、見回りをすべて止める。
    _scripted_llm(stub_llm, move_json("propose", PACKAGE), move_json("accept"))
    browser = wired.browser()
    pid = await browser.register()
    wired.clock.advance(dt.timedelta(days=31))  # 依頼者の最終利用から 30 日を過ぎた
    nid = create_demo_negotiation(store)  # 起動の前から金庫にある、進行中の交渉
    wired.sleep.blocking = True  # 見回りのループは、間隔を待つところで止めておく(テストが 1 回ずつ進める)
    services = wired.services
    assert services.referees.task(nid) is None  # 起動の前は、レフェリーのタスクはない

    async with wired.app.router.lifespan_context(wired.app):
        await wired.sleep.wait_for_calls(2)  # 2 つの見回りが、起動時の見回りを済ませて、間隔を待っている
        assert sorted(wired.sleep.calls) == [60, 600]
        assert not wired.default_db.collection("principals_meta").document(pid).get().exists  # 期限切れの依頼者が消えた
        assert not store._principal_ref(pid).get().exists
        await asyncio.wait_for(services.referees.task(nid), 30)  # 交渉の見回りが作ったタスクが、終わりまで進めた
        assert store.get_view(nid, "candidate").status == "judged"
        assert wired.default_db.collection("stages").document(nid).get().exists  # 段の状態も作られた

    # 止めた後は、見回りのループが動いていない(間隔が来ても、次の見回りをしない)。
    calls_at_shutdown = len(wired.sleep.calls)
    wired.sleep.tick()
    await asyncio.sleep(0)
    assert len(wired.sleep.calls) == calls_at_shutdown
