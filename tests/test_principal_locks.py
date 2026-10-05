"""台帳 I-4: 依頼者ごとのロック(design.md §6.3。web は 1 インスタンスなので、プロセスの中の asyncio のロック)。

本人の削除と、途中にある同じ依頼者の書き込みが競合すると、金庫に依頼者の文書が(または web の (default) に
段の状態が)作り直されて、削除の後にデータが残る。これを避けるため、同じ依頼者の操作と削除の流れを 1 つずつ
順に処理する。削除の流れは、印を立てる前にロックを取り、最後まで持つ。見回りの stages/{nid} の作成と、レフェリーの
金庫への操作(その依頼者の交渉のもの)も、同じロックの対象にする。

操作を途中で止める仕掛け(GatedVault・止めるイベント)を使い、タイミングに頼らずに確かめる: 止めた操作が
ロックを持ったまま止まっていること、削除がロックの順番待ちに入ったこと(lock_users)を確かめてから、止めた操作を
再開させる。ロックがなければ、削除は途中の操作を追い越して先に済み、その後に届く書き込みが、金庫または (default) に
データを作り直す(テストは、その場合に失敗する)。
"""

import asyncio
import datetime as dt

import pytest
from test_interview_api import Flow, ScriptedLlm
from vault_helpers import sample_package
from web.locks import PrincipalLocks
from web.referee import NegotiationContext, Referee, RefereeDeps, StepOutcome
from web.stages import StageStore
from web.sweeper import Sweeper
from web_app_helpers import (
    GatedVault,
    build_web_env,
    documents_mentioning,
    lock_users,
    wait_until,
)
from web_helpers import create_demo_negotiation, move_dict


def _meta_state(env, pid: str) -> str | None:
    document = env.default_db.collection("principals_meta").document(pid).get().to_dict()
    return document["deletion_state"] if document else None


def _stage_docs_of(env, pid: str) -> list:
    return [s for s in env.default_db.collection("stages").stream() if s.to_dict().get("candidate_principal_id") == pid]


@pytest.mark.anyio
async def test_locks_serialize_the_same_principal_in_order_and_do_not_block_other_principals():
    # 同じ依頼者は、取った順(FIFO)に 1 つずつ。別の依頼者は、待たされない。使い終わったロックは覚えておかない。
    locks = PrincipalLocks()
    log: list[str] = []
    release_first = asyncio.Event()

    async def worker(pid: str, name: str, gate: asyncio.Event | None = None):
        async with locks.lock(pid):
            log.append(f"{name}:start")
            if gate is not None:
                await gate.wait()
            log.append(f"{name}:end")

    first = asyncio.create_task(worker("a", "first", release_first))
    await wait_until(lambda: "first:start" in log)
    second = asyncio.create_task(worker("a", "second"))
    third = asyncio.create_task(worker("a", "third"))
    other = asyncio.create_task(worker("b", "other"))
    await asyncio.wait_for(other, 10)  # 別の依頼者は、a の順番待ちを待たない
    await wait_until(lambda: locks._entries["a"].users == 3)  # first が持ち、second・third が待っている
    assert log == ["first:start", "other:start", "other:end"]

    release_first.set()
    await asyncio.gather(first, second, third)

    assert log[3:] == ["first:end", "second:start", "second:end", "third:start", "third:end"]  # 追い越さない
    assert locks._entries == {}


async def _meta_state_under_lock(env, pid: str) -> str | None:
    """依頼者のロックを取ってから、利用記録の状態を読む(削除の流れの後に取れたことの確認用)。"""
    async with env.services.locks.lock(pid):
        return _meta_state(env, pid)


@pytest.mark.anyio
async def test_a_cancelled_waiter_leaves_the_lock_usable_and_forgotten():
    # 順番待ちの途中でキャンセルされても(クライアントの切断など)、ロックは壊れず、後の待ち手は進める。
    locks = PrincipalLocks()
    release = asyncio.Event()
    holder_started = asyncio.Event()

    async def holder():
        async with locks.lock("a"):
            holder_started.set()
            await release.wait()

    async def waiter():
        async with locks.lock("a"):
            pytest.fail("the cancelled waiter must not get the lock")

    holder_task = asyncio.create_task(holder())
    await holder_started.wait()
    waiter_task = asyncio.create_task(waiter())
    await wait_until(lambda: locks._entries["a"].users == 2)
    waiter_task.cancel()
    await asyncio.gather(waiter_task, return_exceptions=True)
    assert locks._entries["a"].users == 1

    release.set()
    await holder_task
    async with locks.lock("a"):  # 続けて取れる
        pass
    assert locks._entries == {}


@pytest.mark.anyio
async def test_deletion_waits_for_an_in_flight_interview_submit_and_leaves_no_data(
    store, clock, vault_client, default_db, session_key
):
    # I-4: 同じ依頼者の操作(面談の送信。金庫への PUT policy の途中)がある間に削除が求められても、削除はその
    # 操作の終わりを待ち、削除の後に金庫にも (default) にもデータが残らない。送信は、面談の API の /submit(本番の経路。台帳 X-81)。
    gated = GatedVault(vault_client, gated=("put_policy",))
    env = build_web_env(store=store, clock=clock, vault=gated, default_db=default_db, session_key=session_key)
    try:
        env.services.interview.agent.use_model(ScriptedLlm().stub)
        browser = env.browser()
        pid = await browser.open_start_page()
        flow = Flow(env, browser, pid)
        await flow.until_ready()  # 面談を、確認と「最悪ここまで」の承認まで進める(ここまでは、金庫を呼ばない)
        assert gated.events == []
        submit = asyncio.create_task(flow.post("submit"))
        await asyncio.wait_for(gated.entered.wait(), 10)  # 面談の送信が、金庫への書き込みの途中で止まった
        assert not store._principal_ref(pid).get().exists  # 金庫には、まだ何もない

        delete = asyncio.create_task(browser.post(f"/v1/principals/{pid}/delete"))
        await wait_until(lambda: lock_users(env, pid) == 2)  # 削除が、ロックの順番待ちに入った
        assert not delete.done()
        assert _meta_state(env, pid) == "active"  # 印はまだ立てていない(印を立てる前にロックを取る)
        assert "delete_principal:start" not in gated.events  # 金庫の削除も始まっていない

        gated.release.set()
        submitted, deleted = await asyncio.gather(submit, delete)
    finally:
        await env.aclose()

    assert (submitted.status_code, deleted.status_code) == (200, 200)
    relevant = [e for e in gated.events if e.startswith(("put_policy", "delete_principal"))]
    assert relevant == ["put_policy:start", "put_policy:end", "delete_principal:start", "delete_principal:end"]
    assert not store._principal_ref(pid).get().exists  # 途中の操作が書いた金庫の文書も、削除された
    assert documents_mentioning(default_db, pid) == {}
    assert documents_mentioning(store._db, pid) == {}


@pytest.mark.anyio
async def test_the_automatic_deletion_takes_the_lock_before_marking_and_holds_it_until_the_end(
    store, clock, vault_client, default_db, session_key
):
    # I-4: 30 日の自動削除(依頼者の見回り)も、同じロックを使う。削除の流れは、印を立てる前にロックを取り、最後まで持つ。
    gated = GatedVault(vault_client, gated=("delete_principal",))
    env = build_web_env(store=store, clock=clock, vault=gated, default_db=default_db, session_key=session_key)
    try:
        pid = await env.browser().register()
        clock.advance(dt.timedelta(days=31))  # delete_after を過ぎた

        async with env.services.locks.lock(pid):  # 別の操作が、この依頼者のロックを持っている
            sweep = asyncio.create_task(env.services.principal_sweeper.sweep_once())
            await wait_until(lambda: lock_users(env, pid) == 2)  # 見回りは、ロックの順番待ち
            assert _meta_state(env, pid) == "active"  # 印は、ロックを取る前には立てない
            assert "delete_principal:start" not in gated.events

        await asyncio.wait_for(gated.entered.wait(), 10)  # ロックが空くと、印を立てて、金庫の削除(止めてある)まで進んだ
        assert _meta_state(env, pid) == "deleting"
        assert lock_users(env, pid) == 1  # 削除の流れが、ロックを持っている
        next_operation = asyncio.create_task(_meta_state_under_lock(env, pid))
        await wait_until(lambda: lock_users(env, pid) == 2)
        assert not next_operation.done()  # 別の操作は、削除の流れが終わるまで待たされる

        gated.release.set()
        report = await sweep
        state_seen_by_the_next_operation = await next_operation
    finally:
        await env.aclose()

    assert report.completed == 1
    assert state_seen_by_the_next_operation is None  # 次の操作が取れたのは、利用記録(最後の段)まで消えてから
    assert documents_mentioning(default_db, pid) == {}
    assert documents_mentioning(store._db, pid) == {}


@pytest.mark.anyio
async def test_deletion_waits_for_an_in_flight_negotiation_creation_and_leaves_no_stage_or_negotiation(web_app):
    # I-4: 交渉の作成(金庫に作った後、stages/{nid} を作る途中)がある間に削除が求められても、削除はその終わりを待つ。
    # 削除の後に、段の状態も交渉も残らない(削除が先に済むと、後から作られた段の状態が、削除の対象から漏れる)。
    browser = web_app.browser()
    pid = await browser.register()
    template_id = web_app.put_employer_template()
    entered, release = asyncio.Event(), asyncio.Event()
    original_ensure = web_app.services.stages.ensure

    async def gated_ensure(nid, principal_id):
        entered.set()
        await release.wait()
        return await original_ensure(nid, principal_id)

    web_app.services.stages.ensure = gated_ensure

    create = asyncio.create_task(browser.create_negotiation(pid, template_id))
    await asyncio.wait_for(entered.wait(), 10)  # 交渉は金庫に作られた。段の状態を作る途中で止まった
    assert len(web_app.store.list_principal_negotiations(pid)) == 1
    assert _stage_docs_of(web_app, pid) == []
    delete = asyncio.create_task(browser.post(f"/v1/principals/{pid}/delete"))
    await wait_until(lambda: lock_users(web_app, pid) == 2)
    assert _meta_state(web_app, pid) == "active"

    release.set()
    nid, deleted = await asyncio.gather(create, delete)

    assert deleted.status_code == 200
    assert _stage_docs_of(web_app, pid) == []
    assert not web_app.store._negotiation_ref(nid).get().exists
    assert documents_mentioning(web_app.default_db, pid, nid) == {}
    assert documents_mentioning(web_app.store._db, pid) == {}


@pytest.mark.anyio
async def test_the_sweeper_does_not_recreate_a_stage_from_a_stale_list_after_the_principal_was_deleted(web_app):
    # I-4(1d-1 の申し送り): 交渉の見回りが金庫の一覧を読んだ後に、本人の削除が済んでも、古い一覧から段の状態を
    # 作り直さない(依頼者ごとのロックを取った後に、金庫に交渉が残っていることを確かめる)。
    # 対照として、ロックも確かめもない見回りは、同じ状況で、削除した依頼者の段の状態を作り直してしまう。
    locked_browser, unlocked_browser = web_app.browser(), web_app.browser()
    locked_pid = await locked_browser.register()
    locked_nid = await locked_browser.create_negotiation(locked_pid, web_app.put_employer_template())
    unlocked_pid = await unlocked_browser.register()
    unlocked_nid = await unlocked_browser.create_negotiation(unlocked_pid, web_app.put_employer_template(), "request-0002")
    for nid in (locked_nid, unlocked_nid):
        web_app.default_db.collection("stages").document(nid).delete()  # 作り損ねた状態(見回りが作る)
    services = web_app.services
    unlocked_sweeper = Sweeper(  # ロックを渡さない(1d-1 までの見回り)
        vault=services.vault,
        stages=StageStore(web_app.default_db, web_app.clock),
        referees=services.referees,
        clock=web_app.clock,
        sleep=web_app.sleep,
    )
    original_list = services.vault.list_open_negotiations
    delete_after_listing: list = []

    async def list_then_the_principal_is_deleted():
        items = await original_list()  # 見回りは、この一覧を読んだ
        for browser, pid, nid in delete_after_listing:
            assert nid in {item.nid for item in items}  # 削除される依頼者の交渉は、読んだ一覧に入っている
            assert (await browser.post(f"/v1/principals/{pid}/delete")).status_code == 200  # その後に削除が済む
        return items

    services.vault.list_open_negotiations = list_then_the_principal_is_deleted

    delete_after_listing[:] = [(locked_browser, locked_pid, locked_nid)]
    report = await services.sweeper.sweep_once()
    assert report.stages_created == 1  # 削除されていないほうの依頼者(unlocked)の段の状態だけを作った
    assert _stage_docs_of(web_app, locked_pid) == []
    assert documents_mentioning(web_app.default_db, locked_pid) == {}

    delete_after_listing[:] = [(unlocked_browser, unlocked_pid, unlocked_nid)]
    await unlocked_sweeper.sweep_once()
    assert len(_stage_docs_of(web_app, unlocked_pid)) == 1  # ロックがなければ、削除した依頼者の段の状態が残る(対照)


async def _open_negotiation_without_a_stage(web_app):
    """本物の候補者が交渉を持ち、段の状態だけがない(作り損ねた)状態を作る。(ブラウザ, 依頼者 ID, 交渉 ID)を返す。"""
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    web_app.default_db.collection("stages").document(nid).delete()
    return browser, pid, nid


@pytest.mark.anyio
async def test_the_sweeper_creates_the_stage_of_an_active_principal(web_app):
    # C-41(対照): 利用記録があり、削除中でない依頼者の交渉には、見回りが段の状態を作る(以降の「作らない」が、何でも断る
    # 実装のためではないことを示す)。
    _, pid, nid = await _open_negotiation_without_a_stage(web_app)

    report = await web_app.services.sweeper.sweep_once()

    assert (report.stages_created, report.errors) == (1, 0)
    assert [s.id for s in _stage_docs_of(web_app, pid)] == [nid]


@pytest.mark.anyio
async def test_the_sweeper_does_not_create_a_stage_for_a_principal_who_is_being_deleted(web_app):
    # C-41: 本物の候補者が削除中(利用記録の deletion_state=deleting。削除の流れが、金庫の削除より前で止まった)なら、
    # 金庫には交渉が残っていても、見回りは段の状態を作らない(削除の流れが、依頼者 ID で段の状態を消すので、作ると残る)。
    # 以前は、金庫に交渉があるかだけを見ていたので、作ってしまった。
    _, pid, nid = await _open_negotiation_without_a_stage(web_app)
    assert await web_app.services.meta.mark_deleting(pid) == "marked"
    assert web_app.store.get_view(nid, "candidate").status == "active"  # 金庫には、交渉も依頼者も残っている

    report = await web_app.services.sweeper.sweep_once()

    assert (report.stages_created, report.errors) == (0, 0)
    assert _stage_docs_of(web_app, pid) == []
    assert documents_mentioning(web_app.default_db, pid, nid) == {"principals_meta/" + pid: _meta_document(web_app, pid)}


def _meta_document(web_app, pid: str) -> dict:
    return web_app.default_db.collection("principals_meta").document(pid).get().to_dict()


@pytest.mark.anyio
async def test_the_sweeper_does_not_create_a_stage_for_a_principal_whose_usage_record_is_gone(web_app):
    # C-41: 利用記録がない(削除済み)依頼者の交渉にも、見回りは段の状態を作らない。金庫に交渉が残っていても(相手が本物の
    # 交渉で、削除した側の見え方だけを消して、交渉の文書は残るとき。§3.8 の手順 3)、「交渉がある」ことは、依頼者が
    # 使える状態であることを意味しない。
    _, pid, nid = await _open_negotiation_without_a_stage(web_app)
    web_app.default_db.collection("principals_meta").document(pid).delete()
    assert web_app.store._negotiation_ref(nid).get().exists  # 金庫には、交渉の文書が残っている

    report = await web_app.services.sweeper.sweep_once()

    assert (report.stages_created, report.errors) == (0, 0)
    assert _stage_docs_of(web_app, pid) == []
    assert documents_mentioning(web_app.default_db, pid, nid) == {}


@pytest.mark.anyio
async def test_the_sweeper_reads_the_usage_record_while_holding_the_principals_lock(web_app):
    # C-41・I-4: 利用記録は、依頼者のロックを取った後に読む。ロックを持つ削除の流れが、印を立てる(deleting)まで待たされた
    # 見回りは、ロックが空いた後に、印を見て、段の状態を作らない(ロックの外で先に読んでいれば、「使える」と読んで作る)。
    _, pid, nid = await _open_negotiation_without_a_stage(web_app)
    services = web_app.services

    async with services.locks.lock(pid):  # 削除の流れが、ロックを持っている
        sweep = asyncio.create_task(services.sweeper.sweep_once())
        await wait_until(lambda: lock_users(web_app, pid) == 2)  # 見回りは、ロックの順番待ちに入った
        assert await services.meta.mark_deleting(pid) == "marked"  # 削除の流れが、印を立てる
    report = await asyncio.wait_for(sweep, 10)

    assert (report.stages_created, report.errors) == (0, 0)
    assert _stage_docs_of(web_app, pid) == []


@pytest.mark.anyio
async def test_a_failure_to_read_the_usage_record_is_counted_and_the_next_sweep_retries(web_app, monkeypatch):
    # C-41: 利用記録を読めないときは、段の状態を作らず(確かめずに作らない)、失敗として数える。次の見回りでやり直す。
    _, pid, nid = await _open_negotiation_without_a_stage(web_app)
    meta = web_app.services.meta
    original = meta.get
    failures = [RuntimeError("firestore is down")]

    async def flaky(principal_id):
        if failures:
            raise failures.pop()
        return await original(principal_id)

    monkeypatch.setattr(meta, "get", flaky)

    first = await web_app.services.sweeper.sweep_once()
    second = await web_app.services.sweeper.sweep_once()

    assert (first.stages_created, first.errors) == (0, 1)
    assert (second.stages_created, second.errors) == (1, 0)
    assert [s.id for s in _stage_docs_of(web_app, pid)] == [nid]


@pytest.mark.anyio
async def test_the_sweeper_needs_the_locks_and_the_usage_records_together(web_app):
    # C-41: 片方だけでは、本物の候補者の段の状態を、確かめずに作る(または、ロックなしで確かめる)ことになる。組み立ての誤りとして拒否する。
    services = web_app.services
    common = dict(vault=services.vault, stages=services.stages, referees=services.referees)

    with pytest.raises(ValueError, match="together"):
        Sweeper(**common, locks=services.locks)
    with pytest.raises(ValueError, match="together"):
        Sweeper(**common, meta=services.meta)
    Sweeper(**common)  # どちらもなし(デモ・攻撃だけを見回る組み立て)
    Sweeper(**common, locks=services.locks, meta=services.meta)


@pytest.mark.anyio
async def test_the_referees_vault_calls_for_a_real_principal_wait_for_the_principals_lock(web_app):
    # I-4(1d-1 の申し送り): レフェリーの金庫への操作(その依頼者の交渉のもの)は、1 回ごとに依頼者のロックを取る。
    # ロックを別の操作が持っている間は、金庫に何も呼ばず、空いたら進む。架空人物の交渉は、依頼者がいないので取らない。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    demo_nid = create_demo_negotiation(web_app.store)
    recording = GatedVault(web_app.vault)  # 呼び出しを記録するだけ(止めない)
    deps = RefereeDeps(
        vault=recording,
        send_turn=web_app.agents,
        clock=web_app.clock,
        sleep=web_app.sleep,
        locks=web_app.services.locks,
        count_llm_calls=False,  # 台帳 X-60: 計上はこのテストの対象外
    )
    live_referee = Referee(NegotiationContext(nid, "live", pid), deps)
    demo_referee = Referee(NegotiationContext(demo_nid, "demo", None), deps)
    web_app.agents.script("candidate", move_dict("propose", sample_package()), move_dict("propose", sample_package()))

    async with web_app.services.locks.lock(pid):  # 別の操作が、この依頼者のロックを持っている
        live_step = asyncio.create_task(live_referee.step())
        await wait_until(lambda: lock_users(web_app, pid) == 2)  # レフェリーが、最初の金庫の操作で順番待ちに入った
        assert recording.events == []  # まだ、金庫に何も呼んでいない
        assert (await demo_referee.step()) is StepOutcome.MOVED  # 架空人物の交渉は、待たされない
        assert recording.events  # 架空人物の交渉のレフェリーは、金庫を呼んだ
        events_before_release = list(recording.events)

    outcome = await asyncio.wait_for(live_step, 10)  # ロックが空いたら、進む

    assert outcome is StepOutcome.MOVED
    assert recording.events[len(events_before_release):][0] == "get_view:start"
    assert "post_move:end" in recording.events[len(events_before_release):]


@pytest.mark.anyio
async def test_requests_of_the_same_principal_are_processed_one_at_a_time_and_others_are_not_blocked(
    store, clock, vault_client, default_db, session_key
):
    # I-4: 同じ依頼者の操作は 1 つずつ順に処理する(先の操作の終わりまで、次の操作は金庫を呼ばない)。
    # 別の依頼者の操作は、待たされない。
    gated = GatedVault(vault_client, gated=("list_principal_negotiations",))
    env = build_web_env(store=store, clock=clock, vault=gated, default_db=default_db, session_key=session_key)
    try:
        browser_a, browser_b = env.browser(), env.browser()
        pid_a = await browser_a.register()
        pid_b = await browser_b.register()

        first = asyncio.create_task(browser_a.get(f"/v1/principals/{pid_a}/negotiations"))
        await asyncio.wait_for(gated.entered.wait(), 10)  # 1 つ目が、金庫の呼び出しの途中で止まった
        second = asyncio.create_task(browser_a.get(f"/v1/principals/{pid_a}/negotiations"))
        await wait_until(lambda: lock_users(env, pid_a) == 2)  # 2 つ目は、ロックの順番待ち
        assert gated.events.count("list_principal_negotiations:start") == 1  # 2 つ目は、まだ金庫を呼んでいない
        other = await browser_b.get(f"/v1/principals/{pid_b}/policy")  # 別の依頼者は、待たされない
        assert other.status_code == 200

        gated.release.set()
        responses = await asyncio.gather(first, second)
    finally:
        await env.aclose()

    assert [r.status_code for r in responses] == [200, 200]
    listing = [e for e in gated.events if e.startswith("list_principal_negotiations")]
    assert listing == ["list_principal_negotiations:start", "list_principal_negotiations:end"] * 2  # 重ならない


@pytest.mark.anyio
async def test_the_deletion_does_not_wait_for_the_agent_and_the_referee_task_ends_when_the_negotiation_is_gone(web_app):
    # I-4 の帰結: ロックを持つのは金庫への 1 回ごとの操作の間だけで、エージェントの応答を待っている間は持たない。
    # そのため、本人の削除は、レフェリーが応答を待っていても、待たされずに済む。削除の後にエージェントが応答して
    # 手を登録しようとすると、金庫から交渉が消えている(404)ので、レフェリーのタスクは終わる。
    reply = asyncio.Event()

    async def slow_reply(call):
        await reply.wait()
        return move_dict("propose", sample_package())

    web_app.enable_referees()
    web_app.agents.script("candidate", slow_reply)
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    await wait_until(lambda: web_app.agents.calls_for("candidate"))  # レフェリーは、エージェントの応答を待っている
    task = web_app.services.referees.task(nid)
    assert not task.done()

    deleted = await asyncio.wait_for(browser.post(f"/v1/principals/{pid}/delete"), 10)  # 待たされない

    assert deleted.status_code == 200
    assert not web_app.store._negotiation_ref(nid).get().exists
    reply.set()  # 削除の後に、エージェントが応答する
    await asyncio.wait_for(task, 10)  # 手の登録は 404 になり、レフェリーのタスクは(例外なく)終わる
    assert task.exception() is None
