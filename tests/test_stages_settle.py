"""段の決着処理を GET の外で行う(design.md §6.2・§6.3・§4.1。台帳 X-84・L19-14)。

`GET .../stage` は、段の状態を書かない純粋な読み出しにした(§6.3: 状態を変えるのは POST と独自ヘッダに限る。SameSite=Lax のクッキーは、外部サイトからの
トップレベルの GET に付くので、リンクを踏ませるだけで、決着処理と台帳の時刻を先行させられてしまう)。判定の検出(agreed_at)・架空人物の自動応答・台帳の
書き込み(決着処理。StageFlow.settle)は、次の 3 つで行う。
- (a) レフェリーの完了のフック(RefereeDeps.on_finished → StageSettler。FINISHED のとき。失敗してもレフェリーは落とさない)
- (b) 見回り(Sweeper。段の状態の settled_at が null で、金庫の進行中の一覧にないものを拾う)
- (c) 本人の「会う」「承認」(POST。tests/test_stages.py)

ここでは、GET が何も書かないこと・フックの呼ばれ方と失敗の扱い・見回りの拾い方(進行中は拾わない・決着済みは拾い直さない・フックが失敗しても拾う)・
本物の候補者の削除との競合(ロックの下で、利用記録が使える状態のときだけ行う)を確かめる。デモの画面の流れ(判定 → 段 0 → 自動応答)は、レフェリーを動かして、
GET も見回りも使わずに、フックだけで段 2 までそろうことで確かめる。

金庫は本物の vault の app を ASGI のままつなぐ。LLM・GCP には接続しない(エージェントはスタブ)。
"""

import asyncio
import dataclasses
import logging

import pytest
from test_stages import (  # noqa: F401  (stage_env はフィクスチャ)
    CANDIDATE_TEMPLATE_ID,
    EMPLOYER_JOB_ID,
    EMPLOYER_TEMPLATE_ID,
    JOB_SUMMARY,
    agree,
    demo_negotiation,
    ledger_docs,
    live_negotiation,
    make_case,
    settle,
    stage_doc,
    stage_env,
    stage_of,
)
from vault.templates import put_template
from vault_helpers import make_candidate_template, sample_package
from web.fictional_answerer import FixtureCatalog
from web.referee import NegotiationContext, Referee
from web.vault_client import VaultConflictError, VaultNotFoundError
from web_app_helpers import build_web_env, dump_documents, lock_users, wait_until
from web_helpers import CrashAfterMove, SimulatedCrash, create_demo_negotiation, move_dict


@pytest.fixture
async def referee_env(store, clock, vault_client, default_db, session_key):
    """レフェリーのタスクを動かす web 一式(架空人物のフィクスチャつき)。エージェントはスタブで、台本どおりに手を打つ。"""
    env = build_web_env(
        store=store,
        clock=clock,
        vault=vault_client,
        default_db=default_db,
        session_key=session_key,
        fixtures=FixtureCatalog([make_case()]),
        run_referees=True,
    )
    env.put_employer_template(template_id=EMPLOYER_TEMPLATE_ID, job_id=EMPLOYER_JOB_ID)
    put_template(store._db, make_candidate_template(template_id=CANDIDATE_TEMPLATE_ID))
    yield env
    await env.aclose()


def script_agreement(env) -> None:
    """候補者が提案し、求人側が受けて、合意で終わる台本(両者とも何でも受ける)。"""
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("employer", move_dict("accept"))


# ----------------------------------------------------------------------
# GET は純粋な読み出し(台帳 X-84)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_getting_the_stage_never_changes_the_stage_document_or_the_ledger(stage_env):
    # X-84・§6.3: GET .../stage は、状態を書かない。判定の直後で、決着処理がまだの段(合意・合意なし・デモ)を何度見ても、(default) の全文書が変わらない。
    # 見えるのは段 0 だけ(判定は出ているが、判定の検出も自動応答もまだ)。リンクを踏ませるだけの GET で、決着処理や台帳の時刻を先行させられない。
    env = stage_env()
    browser, visitor, other = env.browser(), env.browser(), env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)
    demo_nid = await demo_negotiation(env, visitor, settled=False)
    other_pid, other_nid = await live_negotiation(env, other, agreed=False, request_id="request-0002")
    assert (await other.post(f"/v1/negotiations/{other_nid}/control", dict(action="cancel"))).json()["status"] == "judged"
    before = dump_documents(env.default_db)

    for _ in range(3):
        own = await stage_of(browser, nid)
        demo = (await visitor.get(f"/v1/demo/negotiations/{demo_nid}/stage")).json()
        unagreed = await stage_of(other, other_nid)

    assert dump_documents(env.default_db) == before  # 3 回ずつ見ても、段の状態も台帳も、1 項目も変わらない
    assert (own["judged"], own["agreed"], own["stage"], own["meet"]) == (True, True, 0, dict(candidate=False, employer=False))
    assert (demo["agreed"], demo["stage"], demo["meet"]) == (True, 0, dict(candidate=False, employer=False))
    assert (unagreed["judged"], unagreed["agreed"]) == (True, False)
    assert ledger_docs(env, pid) == {} and ledger_docs(env, other_pid) == {}
    # 対照: 決着処理を行えば書かれる(上の等しさが、何も書けない環境のせいではない)
    assert await settle(env, nid, pid) and await settle(env, demo_nid) and await settle(env, other_nid, other_pid)
    assert dump_documents(env.default_db) != before
    assert stage_doc(env, nid)["meet"]["employer"] is True and stage_doc(env, demo_nid)["stage"] == 2
    assert len(ledger_docs(env, pid)) == 2 and len(ledger_docs(env, other_pid)) == 1


@pytest.mark.anyio
async def test_getting_the_stage_after_the_settlement_still_changes_nothing(stage_env):
    # 決着処理の後も、GET は何も書かない(見るたびに、同じ状態を返す)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    first = await stage_of(browser, nid)
    before = dump_documents(env.default_db)

    again = [await stage_of(browser, nid) for _ in range(3)]

    assert all(view == first for view in again)
    assert dump_documents(env.default_db) == before
    assert (first["stage"], first["meet"]) == (0, dict(candidate=False, employer=True))


# ----------------------------------------------------------------------
# (a) レフェリーの完了のフック
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_referee_calls_the_finish_hook_once_when_the_negotiation_ends_and_not_before(store, web_env):
    # X-84: FINISHED(判定が出た)のときに、交渉の性質(NegotiationContext)を渡して、フックを 1 回だけ呼ぶ。終わるまでは呼ばない。
    env = web_env
    nid = create_demo_negotiation(store)
    script_agreement(env)
    seen: list[NegotiationContext] = []

    async def hook(context: NegotiationContext) -> None:
        seen.append(context)

    context = NegotiationContext(nid=nid, mode="demo", candidate_principal_id=None)
    referee = Referee(context, dataclasses.replace(env.deps, on_finished=hook))

    await referee.step()  # 候補者の提案。交渉はまだ続いている
    assert seen == [] and store.get_view(nid, "candidate").status == "active"
    await referee.run()  # 求人側の受諾 → 判定 → 終わる

    assert store.get_view(nid, "candidate").status == "judged"
    assert seen == [context]


@pytest.mark.anyio
async def test_a_failing_finish_hook_does_not_stop_the_referee_and_only_the_error_type_is_logged(store, web_env, caplog):
    # X-84: フックが失敗しても、レフェリーは落とさない(判定は出ている。決着は、見回りが拾う)。ログには例外の型名だけを書く(交渉 ID・例外の文は書かない)。
    env = web_env
    nid = create_demo_negotiation(store)
    script_agreement(env)

    async def failing(context: NegotiationContext) -> None:
        raise RuntimeError(f"secret detail {context.nid}")

    referee = Referee(NegotiationContext(nid, "demo", None), dataclasses.replace(env.deps, on_finished=failing))
    with caplog.at_level(logging.INFO):
        await referee.run()  # 例外を出さずに終わる

    assert store.get_view(nid, "candidate").status == "judged"
    hook_logs = [record.getMessage() for record in caplog.records if record.name == "web.referee" and "finish hook" in record.getMessage()]
    assert hook_logs == ["finish hook failed error=RuntimeError"]  # 型名だけ(交渉 ID・例外の文は入らない)
    assert "secret detail" not in caplog.text


@pytest.mark.anyio
async def test_a_referee_without_the_hook_finishes_as_before(store, web_env):
    # フックは任意(渡さなければ何もしない)。既存の組み立て(スクリプト・テスト)は変わらない。
    env = web_env
    nid = create_demo_negotiation(store)
    script_agreement(env)
    assert env.deps.on_finished is None

    await Referee(NegotiationContext(nid, "demo", None), env.deps).run()

    assert store.get_view(nid, "candidate").status == "judged"


@pytest.mark.anyio
async def test_a_demo_negotiation_reaches_stage_two_by_the_hook_alone_without_any_get_or_sweep(referee_env):
    # デモの画面の流れ(判定 → 段 0 → 自動応答): レフェリーが交渉を終えると、完了のフックが決着させる。GET も見回りも使わずに、
    # 判定の検出(agreed_at)・求人側の自動応答・架空の候補者の自動応答(フィクスチャの職務要約)がそろい、段 2 まで進んでいる。台帳はない(持ち主がいない)。
    env = referee_env
    script_agreement(env)
    nid = await demo_negotiation(env, env.browser(), agreed=False)

    await asyncio.wait_for(env.services.referees.task(nid), 30)  # フックは、レフェリーのタスクの中で呼ばれる

    document = stage_doc(env, nid)
    assert (document["stage"], document["meet"], document["approve"]) == (
        2,
        dict(candidate=True, employer=True),
        dict(candidate=True, employer=True),
    )
    assert document["job_summary"] == JOB_SUMMARY
    assert document["agreed_at"] is not None and document["settled_at"] is not None
    assert [path for path in dump_documents(env.default_db) if "/ledger/" in path] == []
    view = (await env.browser().get(f"/v1/demo/negotiations/{nid}/stage")).json()  # 見るだけ。画面が受け取るのは、段 2
    assert (view["stage"], view["agreed"]) == (2, True)
    assert stage_doc(env, nid) == document


@pytest.mark.anyio
async def test_a_real_candidates_negotiation_is_settled_by_the_hook_with_the_ledger_and_the_fictional_employers_response(referee_env):
    # 本物の候補者の交渉も、レフェリーが終えた直後に、フックが決着させる: 判定の検出(agreed_at)・段 0 の台帳の 1 行・架空の求人の「会う」の自動応答
    # (台帳に fictional_employer の行)。候補者の操作(「会う」)はまだなので、段 0 のまま。GET は使わない。
    env = referee_env
    script_agreement(env)
    browser = env.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, EMPLOYER_TEMPLATE_ID)

    await asyncio.wait_for(env.services.referees.task(nid), 30)

    document = stage_doc(env, nid)
    assert (document["stage"], document["meet"], document["approve"]) == (
        0,
        dict(candidate=False, employer=True),
        dict(candidate=False, employer=False),
    )
    assert document["agreed_at"] is not None and document["settled_at"] is not None
    assert sorted((row["action"], row["stage"], row["operator"]) for row in ledger_docs(env, pid).values()) == [
        ("disclose", 0, "system"),
        ("meet", 0, "fictional_employer"),
    ]


@pytest.mark.anyio
async def test_a_crash_right_after_the_agreement_leaves_the_stage_for_the_sweeper_to_settle(
    store, clock, vault_client, default_db, session_key
):
    # DV-08・X-84: 合意の登録が金庫にコミットされた直後に web が落ちると(レフェリーのタスクが終わる前。完了のフックは呼ばれない)、判定は金庫にあるのに、
    # 段の状態は決着していない。判定の後なので、金庫の進行中の一覧にはもう載らないが、起動し直した見回りが、段の状態の settled_at から拾って決着する。
    env = build_web_env(
        store=store,
        clock=clock,
        vault=CrashAfterMove(vault_client, crash_on_move_number=2),  # 2 手目(求人側の受諾)がコミットされた直後に落ちる
        default_db=default_db,
        session_key=session_key,
        fixtures=FixtureCatalog([make_case()]),
        run_referees=True,
    )
    try:
        env.put_employer_template(template_id=EMPLOYER_TEMPLATE_ID, job_id=EMPLOYER_JOB_ID)
        put_template(store._db, make_candidate_template(template_id=CANDIDATE_TEMPLATE_ID))
        script_agreement(env)
        nid = await demo_negotiation(env, env.browser(), agreed=False)
        await asyncio.wait({env.services.referees.task(nid)}, timeout=30)
        assert isinstance(env.services.referees.task(nid).exception(), SimulatedCrash)  # 合意の直後に落ちた(フックは呼ばれていない)

        assert store.get_view(nid, "candidate").status == "judged"  # 合意は金庫に残っている
        document = stage_doc(env, nid)
        assert "agreed_at" not in document and document["settled_at"] is None and document["stage"] == 0
        assert nid not in [item.nid for item in await env.vault.list_open_negotiations()]  # 見回りの進行中の一覧には載らない

        report = await env.services.sweeper.sweep_once()  # 起動し直した見回り

        assert (report.listed, report.stages_settled, report.errors) == (0, 1, 0)
        settled = stage_doc(env, nid)
        assert (settled["stage"], settled["meet"], settled["approve"]) == (2, dict(candidate=True, employer=True), dict(candidate=True, employer=True))
        assert settled["agreed_at"] is not None and settled["settled_at"] is not None
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_a_negotiation_that_ends_without_agreement_is_settled_by_the_hook_with_one_ledger_row(referee_env):
    # 見込み「なし」で終わった交渉(求人側が打ち切る)も、完了のフックが決着させる(台帳 L19-14): 段 0 の開示の 1 行(items は result だけ)。agreed_at は付かない。
    env = referee_env
    env.agents.script("candidate", move_dict("propose", sample_package()))
    env.agents.script("employer", move_dict("end"))
    browser = env.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, EMPLOYER_TEMPLATE_ID)

    await asyncio.wait_for(env.services.referees.task(nid), 30)
    assert env.store.get_view(nid, "candidate").status == "judged"

    document = stage_doc(env, nid)
    assert "agreed_at" not in document and document["settled_at"] is not None
    assert [(row["action"], row["stage"], row["items"], row["to"]) for row in ledger_docs(env, pid).values()] == [
        ("disclose", 0, ["result"], "both")
    ]


# ----------------------------------------------------------------------
# (b) 見回り: 判定済みで、決着がまだの段を拾う
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_sweeper_settles_a_judged_stage_the_hook_did_not_settle_and_does_not_pick_it_up_again(stage_env):
    # X-84: フックを通らずに終わった・フックが失敗した交渉(終わっているので、金庫の進行中の一覧には現れない)を、見回りが、段の状態の settled_at から拾う。
    # 本物の候補者・デモ・見込み「なし」のどれも。1 回決着したら、拾い直さない(何も書かない)。
    env = stage_env()
    browser, visitor, other = env.browser(), env.browser(), env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)
    demo_nid = await demo_negotiation(env, visitor, settled=False)
    other_pid, other_nid = await live_negotiation(env, other, agreed=False, request_id="request-0002")
    await other.post(f"/v1/negotiations/{other_nid}/control", dict(action="cancel"))
    assert all(stage_doc(env, target)["settled_at"] is None for target in (nid, demo_nid, other_nid))  # 作成のときは null で付いている

    first = await env.services.sweeper.sweep_once()

    assert (first.stages_settled, first.errors) == (3, 0)
    assert all(stage_doc(env, target)["settled_at"] is not None for target in (nid, demo_nid, other_nid))
    live, demo = stage_doc(env, nid), stage_doc(env, demo_nid)
    assert (live["stage"], live["meet"]["employer"], live["agreed_at"] is not None) == (0, True, True)
    assert (demo["stage"], demo["approve"]) == (2, dict(candidate=True, employer=True))
    assert "agreed_at" not in stage_doc(env, other_nid)
    assert len(ledger_docs(env, pid)) == 2 and len(ledger_docs(env, other_pid)) == 1
    settled = dump_documents(env.default_db)

    second = await env.services.sweeper.sweep_once()

    assert (second.stages_settled, second.errors) == (0, 0)
    assert dump_documents(env.default_db) == settled


@pytest.mark.anyio
async def test_the_sweeper_leaves_a_negotiation_in_progress_to_the_referee(stage_env):
    # 進行中の交渉(金庫の進行中の一覧にある)の段は、見回りは決着させない(終わっていない。判定のあと、レフェリーの完了のフックが行う)。
    # 終わっても、その一覧に出ている間(同じ見回りの中)は拾わず、次の見回りで拾う。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)
    calls = []
    original = env.services.stage_settler.settle

    async def recording(target, principal_id):
        calls.append(target)
        return await original(target, principal_id)

    env.services.sweeper._settle = recording

    report = await env.services.sweeper.sweep_once()

    assert (report.listed, report.stages_settled, calls) == (1, 0, [])
    assert stage_doc(env, nid)["settled_at"] is None
    agree(env.store, nid)  # 終わった(進行中の一覧から消える)
    report = await env.services.sweeper.sweep_once()
    assert (report.listed, report.stages_settled, calls) == (0, 1, [nid])
    assert stage_doc(env, nid)["settled_at"] is not None


@pytest.mark.anyio
async def test_the_sweeper_settles_what_a_failed_hook_left_and_a_failing_stage_does_not_stop_the_others(referee_env, monkeypatch, caplog):
    # X-84: フックが失敗した(決着がない)交渉を、見回りが拾う。1 件の決着が失敗しても、ほかの段の決着は続け、失敗は数えて型名だけを記録する(次の見回りでやり直す)。
    env = referee_env
    calls = []

    async def failing_hook(target, principal_id):
        calls.append(target)
        raise RuntimeError(f"secret detail {target}")

    monkeypatch.setattr(env.services.stage_settler, "settle", failing_hook)  # フックだけを失敗させる(見回りは、組み立てのときの本物を持つ)
    script_agreement(env)
    first = await demo_negotiation(env, env.browser(), request_id="request-demo1", agreed=False)
    await asyncio.wait_for(env.services.referees.task(first), 30)
    script_agreement(env)
    second = await demo_negotiation(env, env.browser(), request_id="request-demo2", agreed=False)
    await asyncio.wait_for(env.services.referees.task(second), 30)
    assert calls == [first, second]  # フックは、どちらも失敗した
    assert all(stage_doc(env, target)["settled_at"] is None and "agreed_at" not in stage_doc(env, target) for target in (first, second))

    real_settle = env.services.sweeper._settle

    async def failing_for_the_first(target, principal_id):
        if target == first:
            raise RuntimeError(f"secret detail {target}")
        return await real_settle(target, principal_id)

    env.services.sweeper._settle = failing_for_the_first
    with caplog.at_level(logging.INFO):
        report = await env.services.sweeper.sweep_once()

    assert (report.stages_settled, report.errors) == (1, 1)
    assert stage_doc(env, second)["stage"] == 2 and stage_doc(env, first)["settled_at"] is None
    sweep_logs = [record.getMessage() for record in caplog.records if record.name == "web.sweeper" and record.levelno >= logging.ERROR]
    assert sweep_logs == ["sweep step failed step=settle_stage error=RuntimeError"]  # 型名だけ(交渉 ID・例外の文は入らない)
    assert "secret detail" not in caplog.text

    env.services.sweeper._settle = real_settle  # 直ったら、次の見回りが拾う
    report = await env.services.sweeper.sweep_once()

    assert (report.stages_settled, report.errors) == (1, 0)
    assert stage_doc(env, first)["stage"] == 2 and stage_doc(env, first)["settled_at"] is not None


@pytest.mark.anyio
async def test_a_sweeper_without_the_settle_part_changes_nothing_for_judged_stages(stage_env):
    # settle を渡さない組み立て(段階開示の部品を持たない)の見回りは、決着処理を行わない(以前と同じ)。段の状態の一覧も読まない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)
    env.services.sweeper._settle = None
    before = dump_documents(env.default_db)

    report = await env.services.sweeper.sweep_once()

    assert (report.stages_settled, report.errors) == (0, 0)
    assert dump_documents(env.default_db) == before


@pytest.mark.anyio
async def test_when_the_unsettled_stages_cannot_be_listed_the_sweep_goes_on_and_counts_the_error(stage_env, caplog):
    # 決着がまだの段の一覧を読めない(Firestore の失敗)ときも、見回りは落ちない(先に済ませた進行中の交渉の処理は残る)。失敗は数えて、型名だけを記録する。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)

    async def broken():
        raise RuntimeError("secret detail: firestore is down")

    env.services.stages.list_unsettled = broken
    with caplog.at_level(logging.INFO):
        report = await env.services.sweeper.sweep_once()

    assert (report.stages_settled, report.errors) == (0, 1)
    assert env.services.sweeper.first_sweep_done is True
    sweep_logs = [record.getMessage() for record in caplog.records if record.name == "web.sweeper" and record.levelno >= logging.ERROR]
    assert sweep_logs == ["sweep step failed step=list_unsettled error=RuntimeError"]
    assert "secret detail" not in caplog.text


# ----------------------------------------------------------------------
# 本物の候補者の削除との競合(台帳 I-4・C-41。DV-06)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_settlement_of_a_real_candidates_stage_waits_for_the_principals_lock(stage_env):
    # 決着処理は、本物の候補者の交渉では、その依頼者のロックの下で行う(本人の操作・削除の流れと、1 つずつ順に処理する)。ロックを持つ別の操作がある間は、始めない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)
    before = stage_doc(env, nid)

    async with env.services.locks.lock(pid):  # 別の操作(たとえば、削除の流れ)が、この依頼者のロックを持っている
        task = asyncio.create_task(settle(env, nid, pid))
        await wait_until(lambda: lock_users(env, pid) == 2)  # 決着処理は、ロックの順番待ち
        assert not task.done() and stage_doc(env, nid) == before and ledger_docs(env, pid) == {}

    assert await asyncio.wait_for(task, 10) is True
    assert stage_doc(env, nid)["settled_at"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["deleting", "deleted"])
async def test_a_principal_being_deleted_or_already_deleted_is_not_settled_so_nothing_is_recreated(stage_env, state):
    # 削除中(deletion_state=deleting)・削除済み(利用記録がない)の依頼者の交渉は、決着処理をしない(False を返し、何も書かない)。削除の流れが依頼者 ID で
    # 段の状態・台帳を消した後に、古い判定から作り直して、削除した依頼者のデータを残してしまわないため。フック・見回りのどちらも、同じ。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)
    if state == "deleting":
        await env.services.meta.mark_deleting(pid)
    else:
        await env.services.meta.delete(pid)
        env.default_db.collection("stages").document(nid).delete()  # 削除の流れが、段の状態を消した後
    before = dump_documents(env.default_db)

    assert await settle(env, nid, pid) is False
    report = await env.services.sweeper.sweep_once()

    assert report.stages_settled == 0
    assert dump_documents(env.default_db) == before
    assert ledger_docs(env, pid) == {}


@pytest.mark.anyio
@pytest.mark.parametrize("error", [VaultNotFoundError("gone", 404), VaultConflictError("deleting", 409)], ids=["vault_has_no_principal", "vault_is_deleting"])
async def test_a_principal_the_vault_no_longer_has_or_is_deleting_is_not_settled(stage_env, monkeypatch, error):
    # 金庫の側に依頼者がいない(404)・金庫の側でも削除中(409)のときも、決着処理はしない(False を返し、何も書かない)。見回りは、次の見回りで再び試す
    # (削除の流れが依頼者 ID で段の状態を消せば、もう対象にならない)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)

    async def raising(principal_id):
        raise error

    monkeypatch.setattr(env.services.vault, "list_principal_negotiations", raising)
    before = dump_documents(env.default_db)

    assert await settle(env, nid, pid) is False

    assert dump_documents(env.default_db) == before


@pytest.mark.anyio
async def test_the_fictional_path_does_not_settle_a_real_negotiation(stage_env):
    # 架空の候補者として決着させる経路(candidate_principal_id が None)は、金庫のデモ用の読み出しの口(正本)が認めた交渉だけ。本物の交渉の ID を渡しても
    # 何もしない(段の状態にも台帳にも、書かない)。存在しない交渉も同じ。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)
    before = dump_documents(env.default_db)

    assert await settle(env, nid, None) is False
    assert await settle(env, "0123456789abcdef", None) is False

    assert dump_documents(env.default_db) == before


@pytest.mark.anyio
async def test_settling_before_the_judgment_does_nothing_and_a_settled_stage_is_not_settled_twice(stage_env):
    # まだ判定が出ていない交渉には、何もしない(False)。決着した後に重ねて呼んでも、書き込みは起きない(冪等。完了のフックと見回りが重なっても、同じ)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)
    before = dump_documents(env.default_db)
    assert await settle(env, nid, pid) is False
    assert dump_documents(env.default_db) == before

    agree(env.store, nid)
    assert await settle(env, nid, pid) is True
    settled = dump_documents(env.default_db)
    stages = env.services.stages
    unsettled_before = await stages.list_unsettled()
    assert nid not in [stage.nid for stage in unsettled_before]  # 決着した段は、見回りの対象から外れる

    assert await settle(env, nid, pid) is True
    assert dump_documents(env.default_db) == settled
