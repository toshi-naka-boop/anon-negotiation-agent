"""DV-02(「会う」・段 2 の承認の部分): 並行・再送で、1 回しか効かない(design.md §6.2・§12.2)。

「会う」と「承認」は、側ごとのフラグとして冪等に立て、両方のフラグがそろったトランザクションの中でだけ、次の段へ進める。
同じ操作を並行して送っても、再送しても、フラグは 1 回しか立たず、段は 1 回しか進まない。

並行の確認は、test_concurrency.py と同じく、性質を 2 つに分け、どちらも条件なしで確かめる(台帳 I-5)。エミュレータは粗いロックで、
同じ文書を読んだ多数の呼び出しが一斉に中止され、一斉にやり直す、を繰り返して、再試行を使い切る(StageBusy。API では 503)ことがある。
「並行の何本かは必ず通る」は、エミュレータの負荷に左右されるので確かめない(本番の Firestore での確認項目)。
- 安全: 効いた(changed・advanced_to)のは 1 本以下で、失敗はすべて StageBusy(競合で書けなかっただけ)。
- 詰まらない・一度だけ: 並行の後に、同じ操作を逐次で送り直す。「並行で効いた数 + 逐次で効いた数 == 1」で、状態がそろう。
本人の API(HTTP)は、ミドルウェアが同じ依頼者の操作を 1 つずつ順に処理する(台帳 I-4)ので、並行に送っても全部が通る。
"""

import asyncio

import pytest
from test_stages import (  # noqa: F401  (stage_env はフィクスチャ)
    JOB_SUMMARY,
    approve,
    ledger_docs,
    live_negotiation,
    meet,
    stage_doc,
    stage_env,
)
from web.stages import StageBusy

# エミュレータは、同じ文書を競合する多数のトランザクションを数秒単位で待たせる(10 本で 10 秒以上。本番の Firestore ではない)ので、並行の数は少なくする。
CONCURRENT = 3


async def run_concurrently(calls) -> tuple[list, list]:
    """calls(引数なしのコルーチン関数)を並行に走らせ、(成功した結果, 失敗した例外) を返す。"""
    results = await asyncio.gather(*(call() for call in calls), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    return [result for result in results if not isinstance(result, BaseException)], failures


async def agreed_with_manual_employer(stage_env, **case_options):
    """合意で終わった本物の候補者の交渉。求人側の自動応答は切ってあり、テストが求人側を押す。判定の記録まで済ませる。"""
    env = stage_env(**case_options)
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)  # 判定の記録まで済ませてある(決着処理。GET は書かない)
    return env, browser, pid, nid


@pytest.mark.anyio
async def test_the_same_meet_sent_in_parallel_takes_effect_once(stage_env):
    # DV-02: 同じ「会う」を並行して送っても、フラグを立てた(changed)のは 1 本以下で、失敗は競合だけ。逐次で送り直すと、
    # ちょうど 1 回ぶんの効果がそろう(要約は最初の 1 回のものを残す)。
    env, browser, pid, nid = await agreed_with_manual_employer(stage_env, meet=False, approve=False)
    stages = env.services.stages
    summaries = [f"{JOB_SUMMARY}({index})" for index in range(CONCURRENT)]

    def press(index):
        return lambda: stages.press(nid, "candidate", "meet", operator="principal", job_summary=summaries[index])

    results, failures = await run_concurrently([press(index) for index in range(CONCURRENT)])

    effective = [result for result in results if result.changed]
    assert len(effective) <= 1
    assert all(isinstance(failure, StageBusy) for failure in failures), failures
    resend = await stages.press(nid, "candidate", "meet", operator="principal", job_summary="再送")
    assert len(effective) + int(resend.changed) == 1
    document = stage_doc(env, nid)
    assert document["meet"]["candidate"] is True
    assert document["job_summary"] in summaries  # 最初に効いた 1 本のもの。再送の要約には書き換わっていない
    assert f"{nid}-meet-candidate" in ledger_docs(env, pid)


@pytest.mark.anyio
async def test_meets_from_both_sides_in_parallel_open_stage_one_exactly_once(stage_env):
    # DV-02: 候補者と求人側が同時に「会う」を押し、両方のフラグがそろったトランザクションで、段 1 がちょうど 1 回だけ開く。
    env, browser, pid, nid = await agreed_with_manual_employer(stage_env, meet=False, approve=False)
    stages = env.services.stages

    def press(side, operator):
        return lambda: stages.press(nid, side, "meet", operator=operator, job_summary=JOB_SUMMARY if side == "candidate" else None)

    results, failures = await run_concurrently([press("candidate", "principal"), press("employer", "fictional_employer")])

    opened = [result for result in results if result.advanced_to == 1]
    assert len(opened) <= 1
    assert all(isinstance(failure, StageBusy) for failure in failures), failures
    resend = [
        await stages.press(nid, "candidate", "meet", operator="principal", job_summary=JOB_SUMMARY),
        await stages.press(nid, "employer", "meet", operator="fictional_employer"),
    ]
    assert len(opened) + sum(result.advanced_to == 1 for result in resend) == 1
    document = stage_doc(env, nid)
    assert (document["stage"], document["meet"]) == (1, dict(candidate=True, employer=True))
    assert document["approve"] == dict(candidate=False, employer=False)  # 承認は触れていない


@pytest.mark.anyio
async def test_approvals_from_both_sides_in_parallel_open_stage_two_exactly_once(stage_env):
    # DV-02: 段 2 の承認も同じ。双方が同時に押しても、段 2 がちょうど 1 回だけ開く(段 3 のようなものには進まない)。
    env, browser, pid, nid = await agreed_with_manual_employer(stage_env, meet=True, approve=False)
    stages = env.services.stages
    assert (await meet(browser, nid)).json()["stage"] == 1

    def press(side, operator):
        return lambda: stages.press(nid, side, "approve", operator=operator)

    results, failures = await run_concurrently([press("candidate", "principal"), press("employer", "fictional_employer")])

    opened = [result for result in results if result.advanced_to == 2]
    assert len(opened) <= 1
    assert all(isinstance(failure, StageBusy) for failure in failures), failures
    resend = [
        await stages.press(nid, "candidate", "approve", operator="principal"),
        await stages.press(nid, "employer", "approve", operator="fictional_employer"),
    ]
    assert len(opened) + sum(result.advanced_to == 2 for result in resend) == 1
    document = stage_doc(env, nid)
    assert (document["stage"], document["approve"]) == (2, dict(candidate=True, employer=True))
    assert f"{nid}-stage2" in ledger_docs(env, pid)


@pytest.mark.anyio
async def test_recording_the_agreement_in_parallel_writes_the_stage_zero_row_once(stage_env):
    # 判定の記録(段 0 の台帳)も、並行で 1 回しか効かない。
    env = stage_env(meet=False, approve=False)
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)  # 合意で終わったが、判定はまだ記録していない
    await env.services.stages.ensure(nid, pid)
    stages = env.services.stages

    results, failures = await run_concurrently([lambda: stages.record_agreement(nid) for _ in range(2)])

    assert results.count("recorded") <= 1
    assert all(isinstance(failure, StageBusy) for failure in failures), failures
    resend = await stages.record_agreement(nid)
    assert results.count("recorded") + int(resend == "recorded") == 1
    assert list(ledger_docs(env, pid)) == [f"{nid}-stage0"]


@pytest.mark.anyio
async def test_settling_without_agreement_in_parallel_writes_the_stage_zero_row_once(stage_env):
    # 見込み「なし」の決着(段 0 の開示の台帳の行。台帳 L19-14)も、並行・再送で 1 回しか効かない: 完了のフックと見回りが重なっても、行は 1 つ。
    # 決着の印(settled_at)を、台帳の行と同じトランザクションで立てるので、2 本目以降は何も書かない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)
    assert (await browser.post(f"/v1/negotiations/{nid}/control", dict(action="cancel"))).json()["status"] == "judged"
    stages = env.services.stages

    results, failures = await run_concurrently([lambda: stages.settle_without_agreement(nid) for _ in range(2)])

    assert results.count("recorded") <= 1
    assert all(isinstance(failure, StageBusy) for failure in failures), failures
    resend = await stages.settle_without_agreement(nid)
    assert results.count("recorded") + int(resend == "recorded") == 1
    assert list(ledger_docs(env, pid)) == [f"{nid}-stage0"]
    assert stage_doc(env, nid)["settled_at"] is not None and "agreed_at" not in stage_doc(env, nid)


@pytest.mark.anyio
async def test_meet_and_approve_sent_in_parallel_through_the_api_all_succeed_and_take_effect_once(stage_env):
    # DV-02(API): 本人の「会う」を並行して送っても、同じ依頼者の操作は 1 つずつ順に処理される(台帳 I-4)ので、全部が 200 で、効くのは 1 回だけ。
    # 承認も同じ。再送(同じ本文の送り直し)も、同じ状態を返す。
    env, browser, pid, nid = await agreed_with_manual_employer(stage_env, meet=True, approve=True)

    meets = await asyncio.gather(*(meet(browser, nid) for _ in range(CONCURRENT)))
    approves = await asyncio.gather(*(approve(browser, nid) for _ in range(CONCURRENT)))

    assert [response.status_code for response in meets + approves] == [200] * (2 * CONCURRENT)
    assert len(set(response.text for response in meets)) <= 2  # 最初の 1 本だけが段を進めた。残りは同じ状態の返事
    assert meets[0].json()["stage"] == 1 and approves[-1].json()["stage"] == 2
    principal_rows = [row for row in ledger_docs(env, pid).values() if row["operator"] == "principal"]
    assert sorted((row["action"], row["stage"]) for row in principal_rows) == [
        ("approve", 1),
        ("disclose", 1),
        ("disclose", 2),
        ("meet", 0),
    ]
    assert stage_doc(env, nid)["job_summary"] == JOB_SUMMARY
