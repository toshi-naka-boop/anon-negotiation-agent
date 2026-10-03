"""AC-15・FR-33: 段 1 以降の自由文(匿名職務要約)は、人間にだけ見せ、LLM の入力には入れない(design.md §6.2)。

本物の経路(web のレフェリー → A2A → agents → スタブの LLM → 金庫)を通して、段 1 の要約を書いた「後」に動いた LLM の呼び出しを
含めて、記録した LLM の入力(LlmRequest 全体の JSON)のどこにも、要約のカナリアが出てこないことを確かめる。LLM の入力は
`llm_gateway` の型付き関数(TurnInput・AttackerTurnInput)を通り、これらの型に段 1 以降の内容を入れる項目はない(FR-33)ので、
要約が書かれた後の交渉(同じ依頼者の次の交渉)でも、入力に混ざらない。

LLM だけがスタブ(tests/agents_helpers.py の StubLlm。受けた入力を requests に記録する)。GCP には接続しない。
"""

import asyncio

import pytest
from agents_helpers import agents_app, plan_json, stub_llm  # noqa: F401  (フィクスチャは import して使う)
from test_stages import (  # noqa: F401  (stage_env はフィクスチャ)
    CANDIDATE_TEMPLATE_ID,
    EMPLOYER_JOB_ID,
    EMPLOYER_TEMPLATE_ID,
    JOB_SUMMARY,
    approve,
    demo_negotiation,
    make_case,
    meet,
    stage_of,
)
from test_web_integration import _AGENTS_URL, _scripted_llm, agents_transport  # noqa: F401
from vault.fixtures import load_case_fixture
from vault.templates import put_template
from vault_helpers import make_candidate_template, sample_package
from web.fictional_answerer import FixtureCatalog
from web_app_helpers import CANARY, build_web_env


@pytest.fixture
async def wired(store, clock, vault_client, default_db, session_key, agents_transport):
    """本物の agents.client.send_turn(base_url を束ねたもの)で agents につないだ web。レフェリーも動かす。フィクスチャ(架空人物の自動応答)つき。"""
    env = build_web_env(
        store=store,
        clock=clock,
        vault=vault_client,
        default_db=default_db,
        session_key=session_key,
        agents_base_url=_AGENTS_URL,
        use_stub_agents=False,
        run_referees=True,
        fixtures=FixtureCatalog([make_case()]),
    )
    env.put_employer_template(template_id=EMPLOYER_TEMPLATE_ID, job_id=EMPLOYER_JOB_ID)
    put_template(store._db, make_candidate_template(template_id=CANDIDATE_TEMPLATE_ID))
    yield env
    await env.aclose()


def assert_no_llm_input_contains(stub_llm, *needles: str) -> None:
    """記録したすべての LLM の入力(前文・文脈・設定・ツールを含む LlmRequest 全体)に、needles が出てこない。"""
    assert stub_llm.requests
    for request in stub_llm.requests:
        for needle in needles:
            assert needle not in request.dump
            assert needle not in request.system_instruction
            assert all(needle not in text for _, texts in request.contents for text in texts)


@pytest.mark.anyio
async def test_a_summary_written_at_stage_one_never_reaches_an_llm_input(wired, stub_llm):
    # AC-15: 本物の候補者が、1 件目の交渉の段 1 で要約(カナリア)を書く。その後、同じ依頼者の 2 件目の交渉で LLM が動く。
    # 要約を書く前後を通して、記録した LLM の入力のどこにも、カナリアは出てこない。
    browser = wired.browser()
    package = sample_package()
    pid = await browser.register()
    _scripted_llm(stub_llm, plan_json("propose", package.model_dump()), plan_json("accept"))
    first = await browser.create_negotiation(pid, EMPLOYER_TEMPLATE_ID, "request-0001")
    await asyncio.wait_for(wired.services.referees.task(first), 30)
    before_summary = len(stub_llm.requests)
    assert before_summary == 2  # 1 件目の LLM の呼び出し(候補者の提案・求人側の受諾)

    assert (await meet(browser, first, CANARY)).json()["stage"] == 1  # 段 1: 要約を書く(求人側は自動で会う)
    assert (await approve(browser, first)).json()["stage"] == 2  # 段 2: 模擬表示
    _scripted_llm(stub_llm, plan_json("propose", package.model_dump()), plan_json("accept"))
    second = await browser.create_negotiation(pid, EMPLOYER_TEMPLATE_ID, "request-0002")
    await asyncio.wait_for(wired.services.referees.task(second), 30)

    assert len(stub_llm.requests) - before_summary == 2  # 要約を書いた後にも、LLM は動いた(確認が空振りでない)
    assert (await stage_of(browser, second))["agreed"] is True
    assert_no_llm_input_contains(stub_llm, CANARY)
    with pytest.raises(AssertionError):  # 対照: 実際に入力に入っている文字列(TurnInput の schema)なら、この確認は検出する
        assert_no_llm_input_contains(stub_llm, "turn-input/v1")


@pytest.mark.anyio
async def test_the_fictional_candidates_summary_and_contact_never_reach_an_llm_input_either(wired, stub_llm):
    # FR-33: デモの架空の候補者の職務要約・連絡先(フィクスチャ)も、段 1・段 2 でサーバが開示した後に動く LLM の入力に、出てこない。
    visitor = wired.browser()
    package = sample_package()
    _scripted_llm(stub_llm, plan_json("propose", package.model_dump()), plan_json("accept"))
    first = await demo_negotiation(wired, visitor, request_id="request-demo1", agreed=False)
    await asyncio.wait_for(wired.services.referees.task(first), 30)
    before_stages = len(stub_llm.requests)

    view = (await visitor.get(f"/v1/demo/negotiations/{first}/stage")).json()
    assert view["stage"] == 2 and view["disclosed_to_employer"]["job_summary"] == JOB_SUMMARY
    _scripted_llm(stub_llm, plan_json("propose", package.model_dump()), plan_json("accept"))
    second = await demo_negotiation(wired, visitor, request_id="request-demo2", agreed=False)
    await asyncio.wait_for(wired.services.referees.task(second), 30)

    assert len(stub_llm.requests) - before_stages == 2
    contact = load_case_fixture(1).candidate.contact
    assert_no_llm_input_contains(stub_llm, JOB_SUMMARY, contact.name, contact.email)
