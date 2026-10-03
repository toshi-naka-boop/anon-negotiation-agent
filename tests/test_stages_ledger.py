"""開示台帳 ④(design.md §6.2・§7・§6.3 の監査、FR-38): 段の遷移・開示のたびに、いつ・何を・誰に見せたかを台帳に書く。

- 段 0 の表示・「会う」「承認」・段 1・段 2 が開いたこと、が 1 行ずつ。行には、見せたものの種類(items)と相手(to)だけを書き、
  生の値(職務要約の本文・氏名・連絡先)は書かない。操作した依頼者 ID と時刻を残す(§6.3)。
- 段の状態と台帳は、同じトランザクションで書く(食い違わない)。同じ出来事は 2 行にならない。
- 本人が読めるのは自分の台帳だけ(GET /v1/principals/{pid}/ledger。時系列で全件)。段階開示が書いた形でない行は返さない。
- 削除の連鎖(§3.8・§6.3)に含まれることは tests/test_principal_deletion.py・tests/test_inactive_deletion.py(DV-06・DV-16)で確かめる。
"""

import datetime as dt
import json

import pytest
from test_stages import (  # noqa: F401  (stage_env はフィクスチャ)
    COMPANY_NAME,
    EMPLOYER_TEMPLATE_ID,
    JOB_SUMMARY,
    agree,
    approve,
    demo_negotiation,
    ledger_docs,
    live_negotiation,
    meet,
    stage_doc,
    stage_env,
    stage_of,
)
from vault.fixtures import load_case_fixture
from web_app_helpers import CANARY, documents_mentioning, plant_canaries

# 本物の候補者が、段 0 から段 2 まで進めたときの台帳(時系列)。(出来事, 段, 操作した主体, 見せたもの, 見せた相手, 模擬表示)
FULL_FLOW = [
    ("disclose", 0, "system", ["likelihood", "package"], "both", False),
    ("meet", 0, "fictional_employer", [], None, False),
    ("meet", 0, "principal", [], None, False),
    ("disclose", 1, "principal", ["job_summary"], "employer", False),
    ("approve", 1, "fictional_employer", [], None, False),
    ("approve", 1, "principal", [], None, False),
    ("disclose", 2, "principal", ["name", "email"], "employer", True),
]


def as_tuples(entries: list[dict]) -> list[tuple]:
    return [(e["action"], e["stage"], e["operator"], e["items"], e["to"], e["simulated"]) for e in entries]


async def run_full_flow(env, browser, nid: str, summary: str = CANARY) -> None:
    """段の状態を見る → 会う → 承認、を 1 秒ずつ空けて行う(台帳の並びを時刻で確かめるため)。"""
    env.clock.advance(dt.timedelta(seconds=1))
    await stage_of(browser, nid)
    env.clock.advance(dt.timedelta(seconds=1))
    assert (await meet(browser, nid, summary)).status_code == 200
    env.clock.advance(dt.timedelta(seconds=1))
    assert (await approve(browser, nid)).status_code == 200


@pytest.mark.anyio
async def test_the_ledger_records_when_what_and_to_whom_at_every_disclosure(stage_env):
    # FR-38・§7: 段の遷移・開示のたびに、いつ(at)・何を(items)・誰に(to)見せたかと、誰が操作したか(operator)を書く。
    # 段 2 は、実ユーザーでは模擬表示(simulated)。時系列の全件を、本人が API で読める。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    await run_full_flow(env, browser, nid)

    response = await browser.get(f"/v1/principals/{pid}/ledger")
    assert response.status_code == 200
    entries = response.json()
    assert as_tuples(entries) == FULL_FLOW
    assert all(entry["nid"] == nid for entry in entries)
    times = [dt.datetime.fromisoformat(entry["at"]) for entry in entries]
    assert times == sorted(times) and len(set(times)) == 3  # 3 回の操作の時刻(同じ操作の中の行は同じ時刻)
    assert all(set(entry) == set(["nid", "action", "stage", "operator", "items", "to", "simulated", "at"]) for entry in entries)


@pytest.mark.anyio
async def test_each_ledger_row_carries_the_principal_id_and_a_fixed_id_so_an_event_is_never_written_twice(stage_env):
    # §6.3 の監査: 各行に、台帳の持ち主(操作した依頼者)の ID と時刻を残す。行の ID は交渉 ID と出来事で決まる固定の文字列で、
    # 同じ出来事は 2 行にならない(何度見ても・再送しても、行は増えない)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    await run_full_flow(env, browser, nid)
    rows = ledger_docs(env, pid)

    for _ in range(3):  # 見直し・再送で増えない
        await stage_of(browser, nid)
        await meet(browser, nid)
        await approve(browser, nid)

    assert ledger_docs(env, pid) == rows
    assert set(rows) == set(
        f"{nid}-{event}"
        for event in ("stage0", "meet-employer", "meet-candidate", "stage1", "approve-employer", "approve-candidate", "stage2")
    )
    assert all(row["principal_id"] == pid and row["at"] is not None for row in rows.values())


@pytest.mark.anyio
async def test_no_raw_value_is_written_to_the_ledger(stage_env):
    # §7: 生の値は書かない。職務要約の本文(カナリア)は stages/{nid} にだけあり、台帳には種類(job_summary)しか書かない。
    # フィクスチャの架空の連絡先も、どこにも書かない(実ユーザーの段 2 は模擬表示)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    await run_full_flow(env, browser, nid, CANARY)

    assert stage_doc(env, nid)["job_summary"] == CANARY
    ledger_text = json.dumps(ledger_docs(env, pid), default=str, ensure_ascii=False)
    contact = load_case_fixture(1).candidate.contact
    for raw in (CANARY, JOB_SUMMARY, COMPANY_NAME, contact.name, contact.email, "650", "700"):
        assert raw not in ledger_text, raw
    api_text = (await browser.get(f"/v1/principals/{pid}/ledger")).text
    assert CANARY not in api_text and contact.email not in api_text and pid not in api_text
    found = documents_mentioning(env.default_db, CANARY)
    assert list(found) == [f"stages/{nid}"]  # 要約の本文があるのは、段の状態の 1 か所だけ


@pytest.mark.anyio
async def test_a_negotiation_that_did_not_agree_leaves_nothing_in_the_ledger(stage_env):
    # 合意で終わらなかった交渉は、段 1 以降に進めない(段 0 は「なし」だけの表示)。台帳には何も書かない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)
    await browser.post(f"/v1/negotiations/{nid}/control", dict(action="cancel"))

    await stage_of(browser, nid)
    await meet(browser, nid)

    assert (await browser.get(f"/v1/principals/{pid}/ledger")).json() == []
    assert ledger_docs(env, pid) == {}


@pytest.mark.anyio
async def test_a_principal_reads_only_their_own_ledger_in_order_across_negotiations(stage_env):
    # 本人が読めるのは自分の台帳だけ。複数の交渉の行は、時刻の順に並ぶ(終わった交渉の後に、次の交渉を始められる)。
    env = stage_env()
    browser, other = env.browser(), env.browser()
    pid, first = await live_negotiation(env, browser)
    other_pid, other_nid = await live_negotiation(env, other)
    await run_full_flow(env, browser, first)
    await run_full_flow(env, other, other_nid)
    env.clock.advance(dt.timedelta(seconds=1))
    second = await browser.create_negotiation(pid, EMPLOYER_TEMPLATE_ID, "request-0002")
    agree(env.store, second)
    await run_full_flow(env, browser, second)

    mine = (await browser.get(f"/v1/principals/{pid}/ledger")).json()
    theirs = (await other.get(f"/v1/principals/{other_pid}/ledger")).json()

    assert [entry["nid"] for entry in mine] == [first] * 7 + [second] * 7
    assert [entry["nid"] for entry in theirs] == [other_nid] * 7
    assert [entry["at"] for entry in mine] == sorted(entry["at"] for entry in mine)


@pytest.mark.anyio
async def test_rows_that_stage_disclosure_did_not_write_are_not_returned(stage_env):
    # 段階開示が書いた形でない行(古い形・壊れた行)は返さない。読み出しが、形の違う行で落ちることもない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    plant_canaries(env, pid, [nid], CANARY)  # {principal_id, stage, note, at} の行(action・nid がない)

    before = (await browser.get(f"/v1/principals/{pid}/ledger")).json()
    await run_full_flow(env, browser, nid)
    after = (await browser.get(f"/v1/principals/{pid}/ledger")).json()

    assert before == []
    assert as_tuples(after) == FULL_FLOW
    assert CANARY not in json.dumps(after)


@pytest.mark.anyio
async def test_a_press_and_its_ledger_rows_are_written_in_one_transaction(stage_env):
    # 段の状態と台帳は、同じトランザクションで書く。台帳の行を書けなければ、フラグも立たない(食い違わない)。直ったら、同じ操作が通る。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    await stage_of(browser, nid)  # 判定の記録と、求人側の自動応答まで済ませる
    stages = env.services.stages
    before = (stage_doc(env, nid), ledger_docs(env, pid))
    original = stages._ledger.row_ref

    def failing(principal_id, row_id):
        raise RuntimeError("ledger is down")

    stages._ledger.row_ref = failing
    with pytest.raises(RuntimeError):
        await stages.press(nid, "candidate", "meet", operator="principal", job_summary=CANARY)
    assert (stage_doc(env, nid), ledger_docs(env, pid)) == before  # フラグも要約も、書かれていない
    assert CANARY not in json.dumps(stage_doc(env, nid), default=str)

    stages._ledger.row_ref = original
    outcome = await stages.press(nid, "candidate", "meet", operator="principal", job_summary=CANARY)

    assert (outcome.changed, outcome.advanced_to) == (True, 1)
    assert stage_doc(env, nid)["stage"] == 1
    assert f"{nid}-stage1" in ledger_docs(env, pid) and f"{nid}-meet-candidate" in ledger_docs(env, pid)


@pytest.mark.anyio
async def test_the_ledger_is_empty_for_a_principal_with_no_disclosure(stage_env):
    env = stage_env()
    browser = env.browser()
    pid = await browser.register()

    response = await browser.get(f"/v1/principals/{pid}/ledger")

    assert (response.status_code, response.json()) == (200, [])


@pytest.mark.anyio
async def test_demo_negotiations_have_no_ledger_because_no_principal_owns_them(stage_env):
    # 台帳は依頼者ごと(principals/{pid}/ledger)。架空の候補者(デモ・攻撃)の交渉には持ち主がいないので、台帳は作らない。
    env = stage_env()
    visitor = env.browser()
    nid = await demo_negotiation(env, visitor)

    view = (await visitor.get(f"/v1/demo/negotiations/{nid}/stage")).json()

    assert view["stage"] == 2
    assert [path for path in documents_mentioning(env.default_db, nid) if "/ledger/" in path] == []
    assert list(env.default_db.collection("principals").stream()) == []
