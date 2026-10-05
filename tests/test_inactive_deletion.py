"""DV-16: 使われなくなった依頼者の自動削除(design.md §6.3・§4.1。P-6)。

`delete_after` を過ぎた依頼者を、依頼者の見回り(PrincipalSweeper)が、進行中の交渉がなくても拾い、本人の
「データを消す」と同じ削除の流れで消す。有効なクッキーを持つリクエストのたびに、1 時間に 1 回まで利用記録を
更新し、クッキーの期限はその書き込みと同時にだけ延ばす(クッキーの寿命 29 日は、データの保持 30 日より 1 日
短いので、まだ使えるクッキーを持つ人のデータを見回りが消し始めることはない)。

時計は注入(FixedClock)で、テストは sleep せず、時計を進めて確かめる。金庫は本物の vault の app を ASGI の
まま(ネットワークを通さず)つなぐ。カナリアは、DV-06 と同じく、段の状態(段 1 の職務要約の項目)と開示台帳に
直接置く(面談と段 1 の画面は後の段)。
"""

import asyncio
import datetime as dt

import pytest
from fastapi import HTTPException
from vault.api_models import MoveRequest
from vault_helpers import sample_package
from web.vault_client import VaultUnavailableError
from web_app_helpers import (
    CANARY,
    DeletionProbe,
    documents_mentioning,
    plant_canaries,
    submit_interview,
)
from test_principal_deletion import (
    _principal_who_disclosed_through_the_stage_api,
    _write_live_negotiation_with_real_counterpart,
)
from test_stages import stage_env  # noqa: F401  (フィクスチャ)

_MINUTE = dt.timedelta(minutes=1)
_HOUR = dt.timedelta(hours=1)
_DAY = dt.timedelta(days=1)
_OTHER_CANARY = "CANARY-OTHER-PRINCIPAL-0002"


def _meta(env, pid: str) -> dict | None:
    return env.default_db.collection("principals_meta").document(pid).get().to_dict()


def _cookie_expiry(env, response) -> int:
    """応答の Set-Cookie の値を、署名を検証して読み、署名の中の期限(UNIX 秒)を返す。"""
    (header,) = response.headers.get_list("set-cookie")
    token = header.split(";")[0].split("=", 1)[1]
    return env.services.codec._serializer.loads(token)["exp"]


async def _principal_with_a_finished_negotiation(env, browser, canary: str = CANARY):
    """面談を送り、交渉を 1 件作って取消にした(進行中の交渉がない)依頼者。カナリアを置いて (pid, nid) を返す。"""
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, env.put_employer_template())
    await browser.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"})
    plant_canaries(env, pid, [nid], canary)
    return pid, nid


@pytest.mark.anyio
async def test_a_principal_past_delete_after_is_deleted_by_the_sweeper_even_without_any_negotiation_in_progress(web_app):
    # DV-16: delete_after を過ぎた依頼者は、進行中の交渉がなくても依頼者の見回りに拾われ、削除の流れで
    # vault-db と (default) にデータが残らない(DV-06 と同じカナリアで確かめる)。まだ過ぎていない依頼者は残る。
    browser, other_browser = web_app.browser(), web_app.browser()
    pid, nid = await _principal_with_a_finished_negotiation(web_app, browser)
    assert web_app.store.get_view(nid, "candidate").status == "judged"  # 進行中の交渉はない
    open_negotiations = await web_app.vault.list_open_negotiations()
    assert [item.nid for item in open_negotiations if item.candidate_principal_id == pid] == []  # 交渉の見回りの一覧には現れない
    assert documents_mentioning(web_app.default_db, pid, CANARY)  # カナリアは、削除の前は見つかる(確認の前提)
    assert documents_mentioning(web_app.store._db, CANARY)  # vault-db にも置いてある(置かなければ、「残らない」の確認は必ず通る)

    web_app.clock.advance(10 * _DAY)
    other_pid, _ = await _principal_with_a_finished_negotiation(web_app, other_browser, _OTHER_CANARY)
    web_app.clock.advance(20 * _DAY + _MINUTE)  # 最初の依頼者の最終利用から 30 日と 1 分。もう一方はまだ 20 日
    assert _meta(web_app, pid)["delete_after"] < web_app.clock.now() < _meta(web_app, other_pid)["delete_after"]

    report = await web_app.services.principal_sweeper.sweep_once()

    assert (report.due, report.completed, report.incomplete, report.skipped, report.errors) == (1, 1, 0, 0, 0)
    assert _meta(web_app, pid) is None
    assert documents_mentioning(web_app.default_db, pid, CANARY) == {}
    assert documents_mentioning(web_app.store._db, pid, CANARY) == {}
    assert not web_app.store._principal_ref(pid).get().exists
    assert not web_app.store._negotiation_ref(nid).get().exists
    # まだ期限が来ていない依頼者は、何も消えていない。
    assert _meta(web_app, other_pid)["deletion_state"] == "active"
    assert documents_mentioning(web_app.default_db, _OTHER_CANARY)
    assert documents_mentioning(web_app.store._db, _OTHER_CANARY)  # vault-db のほかの依頼者のカナリアは、消えていない
    assert web_app.store._principal_ref(other_pid).get().exists


@pytest.mark.anyio
async def test_the_automatic_deletion_keeps_the_real_counterparts_view_and_the_none_record(web_app):
    # DV-16: 相手が本物なら、相手側の見え方と「なし」の最終記録は残る(自分側の見え方は消える)。
    # 段の状態・開示台帳・利用記録は、本人の分がすべて消える。
    browser = web_app.browser()
    pid = await browser.register()
    counterpart_pid = "principal-counterpart-0001"  # 相手側の依頼者の文書そのものは作らない(消さないため)
    nid = _write_live_negotiation_with_real_counterpart(
        web_app.store, web_app.clock, pid, counterpart_pid, request_id=f"{pid}:{CANARY}"  # web と同じ「依頼者 ID:値」の形
    )
    package = sample_package()
    check = web_app.store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
    )
    web_app.store.process_move(
        nid, MoveRequest(expected_version=check.version, side="candidate", move="propose", package=package)
    )
    await web_app.services.sweeper.sweep_once()  # 交渉の見回りが、依頼者 ID つきの段の状態を作る(§6.2)
    plant_canaries(web_app, pid, [nid])
    assert documents_mentioning(web_app.default_db, pid, CANARY)
    assert documents_mentioning(web_app.store._db, pid, CANARY)  # vault-db にも、依頼者 ID とカナリアがある(削除の前)
    assert web_app.store.get_view(nid, "candidate").last_check is not None  # 削除する側の評価が読める(削除の前)

    web_app.clock.advance(31 * _DAY)
    report = await web_app.services.principal_sweeper.sweep_once()

    assert (report.due, report.completed) == (1, 1)
    assert _meta(web_app, pid) is None
    assert documents_mentioning(web_app.default_db, pid, CANARY) == {}
    assert not web_app.store._principal_ref(pid).get().exists
    assert documents_mentioning(web_app.store._db, pid, CANARY) == {}  # vault-db のどこにも、依頼者 ID もカナリアも残らない
    assert web_app.store.get_view(nid, "candidate").last_check is None  # 削除した側の評価は、もう読めない(台帳 C-41)
    negotiation = web_app.store._negotiation_ref(nid).get().to_dict()
    assert (negotiation["status"], negotiation["end_reason"]) == ("judged", "cancelled")
    assert web_app.store.get_events(nid, "candidate") == []  # 自分側の見え方は消えた
    employer_events = web_app.store.get_events(nid, "employer")  # 相手側の見え方と、「なし」の最終記録は残る
    assert [e.kind for e in employer_events] == ["offer_received", "final_result"]
    assert employer_events[-1].result.likelihood == "none"


@pytest.mark.anyio
async def test_the_usage_record_is_written_at_most_once_an_hour_and_the_cookie_is_extended_only_with_it(web_app):
    # DV-16: 有効なクッキーを持つリクエスト(閲覧だけの GET を含む)のたびに、1 時間に 1 回まで last_active_at と
    # delete_after が更新され、クッキーの期限はそれと同時にだけ延びる。
    browser = web_app.browser()
    pid = await browser.register()
    start = web_app.clock.now()
    assert _meta(web_app, pid)["last_active_at"] == start

    async def view_only():
        return await browser.get(f"/v1/principals/{pid}/negotiations")

    # 1 時間たつ前は、何度閲覧しても書かない(クッキーも延ばさない)。
    for elapsed in (_MINUTE, 30 * _MINUTE, _HOUR - dt.timedelta(seconds=1)):
        web_app.clock.set(start + elapsed)
        response = await view_only()
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
        assert _meta(web_app, pid)["last_active_at"] == start

    # 1 時間たった最初のリクエスト(閲覧だけの GET)で、書く。同じ応答で、クッキーの期限を今から 29 日に延ばす。
    web_app.clock.set(start + _HOUR)
    response = await view_only()
    written = _meta(web_app, pid)
    assert written["last_active_at"] == start + _HOUR
    assert written["delete_after"] == start + _HOUR + 30 * _DAY
    assert _cookie_expiry(web_app, response) == int((start + _HOUR).timestamp()) + 29 * 24 * 3600
    assert web_app.services.codec.read(browser.cookie, start + _HOUR + 29 * _DAY - _MINUTE) == pid

    # すぐ後の閲覧は、また書かない。次に書くのは、その 1 時間後。開始ページの GET も、有効なクッキーを持つリクエスト。
    response = await view_only()
    assert "set-cookie" not in response.headers
    assert _meta(web_app, pid) == written
    web_app.clock.set(start + 2 * _HOUR)
    response = await browser.get("/start")
    assert _meta(web_app, pid)["last_active_at"] == start + 2 * _HOUR
    assert len(response.headers.get_list("set-cookie")) == 1
    # POST(ブロックリスト)も同じ。1 時間の間隔を空ければ、書く。
    web_app.clock.set(start + 3 * _HOUR)
    response = await browser.post(f"/v1/principals/{pid}/blocklist", {"blocklist": []})
    assert response.status_code == 200
    assert _meta(web_app, pid)["last_active_at"] == start + 3 * _HOUR
    assert _cookie_expiry(web_app, response) == int((start + 3 * _HOUR).timestamp()) + 29 * 24 * 3600


@pytest.mark.anyio
async def test_a_principal_that_only_views_is_never_deleted_and_is_deleted_30_days_after_the_last_use(web_app):
    # DV-16: 閲覧だけで使い続けた依頼者は消えない。使うのをやめたら、最後に使ってから 30 日で消える
    # (クッキーは 29 日で切れて 401 になるが、データはそれまで残る)。
    browser = web_app.browser()
    pid = await browser.register()
    for _ in range(6):  # クッキーが切れる直前(29 日の 1 分前)に、閲覧だけをくり返す。合計で 170 日以上
        web_app.clock.advance(29 * _DAY - _MINUTE)
        assert (await browser.get(f"/v1/principals/{pid}/negotiations")).status_code == 200
        report = await web_app.services.principal_sweeper.sweep_once()
        assert (report.due, report.completed) == (0, 0)
        assert _meta(web_app, pid)["deletion_state"] == "active"
    last_use = _meta(web_app, pid)["last_active_at"]
    assert last_use == web_app.clock.now()

    web_app.clock.set(last_use + 29 * _DAY + 12 * _HOUR)  # クッキーは切れたが、データはまだ 30 日に届かない
    assert (await browser.get(f"/v1/principals/{pid}/negotiations")).status_code == 401
    report = await web_app.services.principal_sweeper.sweep_once()
    assert (report.due, report.completed) == (0, 0)
    assert _meta(web_app, pid) is not None and web_app.store._principal_ref(pid).get().exists

    web_app.clock.set(last_use + 30 * _DAY + 12 * _HOUR)
    report = await web_app.services.principal_sweeper.sweep_once()
    assert (report.due, report.completed) == (1, 1)
    assert _meta(web_app, pid) is None
    assert not web_app.store._principal_ref(pid).get().exists


@pytest.mark.anyio
async def test_delete_after_is_never_passed_while_the_cookie_is_valid(web_app):
    # DV-16: どの時点でも、有効なクッキーを持つ依頼者の delete_after は過ぎていない(境目の前後で消えない)。
    # クッキーの期限(書き込みから 29 日)は、delete_after(30 日)より 1 日早く切れる。
    browser = web_app.browser()
    pid = await browser.register()
    web_app.clock.advance(2 * _HOUR)
    await browser.get(f"/v1/principals/{pid}/negotiations")  # 書き込み。ここから数える
    written = _meta(web_app, pid)
    write_time = written["last_active_at"]
    assert written["delete_after"] == write_time + 30 * _DAY
    token = browser.cookie
    second = dt.timedelta(seconds=1)

    checkpoints = [
        (write_time + 29 * _DAY - second, True),  # クッキーは、まだ有効
        (write_time + 29 * _DAY, False),  # 期限の瞬間から、無効
        (write_time + 29 * _DAY + second, False),
        (write_time + 30 * _DAY - second, False),  # delete_after の前。データは残る
        (write_time + 30 * _DAY, False),  # ちょうど delete_after。まだ「過ぎて」いない
        (write_time + 30 * _DAY + second, False),  # 過ぎた。ここで初めて消える
    ]
    for moment, cookie_should_be_valid in checkpoints:
        web_app.clock.set(moment)
        cookie_is_valid = web_app.services.codec.read(token, moment) == pid
        assert cookie_is_valid is cookie_should_be_valid, moment
        meta = _meta(web_app, pid)
        if meta is not None:
            delete_after_passed = meta["delete_after"] < moment
            assert not (cookie_is_valid and delete_after_passed), moment  # 有効なクッキーの人は、消される対象ではない
        report = await web_app.services.principal_sweeper.sweep_once()
        if moment <= write_time + 30 * _DAY:
            assert (report.due, report.completed) == (0, 0), moment
            assert _meta(web_app, pid) is not None
        else:
            assert (report.due, report.completed) == (1, 1), moment
            assert _meta(web_app, pid) is None


@pytest.mark.anyio
async def test_a_sweep_does_not_mark_deleting_when_delete_after_was_extended_after_it_was_read(web_app):
    # DV-16: 見回りが読んだ後に delete_after が延びたら、削除中にしない(トランザクションで、読んだ値から変わって
    # いないことを確かめてから印を立てる)。削除の流れも進めない。
    browser = web_app.browser()
    pid, nid = await _principal_with_a_finished_negotiation(web_app, browser)
    web_app.clock.advance(30 * _DAY + _MINUTE)
    meta_store = web_app.services.meta
    read_delete_after = _meta(web_app, pid)["delete_after"]
    original_list_due = meta_store.list_due

    async def list_due_then_the_principal_comes_back(now):
        due = await original_list_due(now)
        assert [entry.principal_id for entry in due] == [pid]  # 見回りは、この依頼者を期限切れとして読んだ
        assert (await meta_store.touch(pid)).outcome == "updated"  # 読んだ後に、利用記録が更新された
        return due

    meta_store.list_due = list_due_then_the_principal_comes_back
    report = await web_app.services.principal_sweeper.sweep_once()

    assert (report.due, report.skipped, report.completed, report.incomplete) == (1, 1, 0, 0)
    meta = _meta(web_app, pid)
    assert meta["deletion_state"] == "active"  # 削除中にしていない
    assert meta["delete_after"] > read_delete_after
    assert documents_mentioning(web_app.default_db, pid, CANARY)  # 何も消えていない
    assert web_app.store._principal_ref(pid).get().exists

    meta_store.list_due = original_list_due  # 次の見回りでは、もう期限切れではない
    report = await web_app.services.principal_sweeper.sweep_once()
    assert (report.due, report.completed) == (0, 0)


@pytest.mark.anyio
async def test_the_usage_record_is_created_by_the_interview_submit_before_the_first_vault_write_and_not_by_the_start_page(
    web_app, monkeypatch
):
    # DV-16: principals_meta は、面談の送信で金庫に書く前に作られ、開始ページを開いただけでは作られない。
    # 送信が拒否された(不正な入力・利用記録がない依頼者のブロックリストや交渉の作成)場合も作られず、金庫に書かれない。
    # 送信は、内部の関数(submit_interview。面談の API の /submit が呼ぶものと同じ。台帳 X-81)で行う。
    browser = web_app.browser()
    pid = await browser.open_start_page()
    await browser.get("/start")
    await web_app.browser().open_start_page()  # 別の訪問者・クローラーも同じ
    for path in (f"/v1/principals/{pid}/negotiations", f"/v1/principals/{pid}/policy"):
        await browser.get(path)
    assert list(web_app.default_db.collection("principals_meta").stream()) == []  # 開始ページでは作られない
    assert not web_app.store._principal_ref(pid).get().exists

    # 面談の送信以外では作られない: 不正な送信(矛盾したアンカー)・利用記録のない依頼者の操作は、何も書かない。
    better_than_the_accepted = {  # 受ける条件より全軸で良い組み合わせを「受けない」と言う(矛盾)
        "salary": 1000, "remote_days": 5, "night_duty": 0, "review_months": 6,
        "training": "*", "side_job": "*", "start": "*",
    }  # fmt: skip
    with pytest.raises(HTTPException) as refused:
        await submit_interview(web_app.services, pid, reject_anchors=[better_than_the_accepted])
    assert (refused.value.status_code, refused.value.detail) == (422, "policy_invalid")
    blocked = await browser.post(f"/v1/principals/{pid}/blocklist", {"blocklist": ["company-x"]})
    created = await browser.post(
        f"/v1/principals/{pid}/negotiations",
        {"request_id": "request-0001", "employer_template_id": web_app.put_employer_template()},
    )
    assert [r.json() for r in (blocked, created)] == [{"detail": "interview_not_submitted"}] * 2
    assert [r.status_code for r in (blocked, created)] == [409, 409]
    assert list(web_app.default_db.collection("principals_meta").stream()) == []
    assert not web_app.store._principal_ref(pid).get().exists

    # 面談の送信: 金庫に初めて書く(PUT policy)ときには、利用記録がすでにある。
    seen = []
    original_put_policy = web_app.vault.put_policy

    async def put_policy_after_checking_the_record(principal_id, request):
        seen.append(_meta(web_app, principal_id))
        return await original_put_policy(principal_id, request)

    monkeypatch.setattr(web_app.vault, "put_policy", put_policy_after_checking_the_record)
    await submit_interview(web_app.services, pid)

    (at_first_vault_write,) = seen
    assert at_first_vault_write is not None  # 金庫に書く前に、利用記録がある
    now = web_app.clock.now()
    assert at_first_vault_write == {"last_active_at": now, "delete_after": now + 30 * _DAY, "deletion_state": "active"}
    assert web_app.store._principal_ref(pid).get().exists
    # 金庫への書き込みが失敗しても、利用記録は先に作ってある(金庫にデータがあって利用記録がない状態は生じない)。
    other = web_app.browser()
    other_pid = await other.open_start_page()

    async def failing_put_policy(principal_id, request):
        raise VaultUnavailableError("vault is down", 503)

    monkeypatch.setattr(web_app.vault, "put_policy", failing_put_policy)
    with pytest.raises(VaultUnavailableError):  # 面談の API なら 503(web.app の例外の写し)
        await submit_interview(web_app.services, other_pid)
    assert _meta(web_app, other_pid)["deletion_state"] == "active"
    assert not web_app.store._principal_ref(other_pid).get().exists


@pytest.mark.anyio
async def test_a_failure_after_the_vault_deletion_is_finished_by_the_next_sweep_and_the_usage_record_goes_last(web_app):
    # DV-16: 金庫の削除の後で失敗させても、次の見回りで段の状態(職務要約を含む)と開示台帳まで消し切り、
    # principals_meta は最後に消える。
    browser = web_app.browser()
    pid, nid = await _principal_with_a_finished_negotiation(web_app, browser)
    probe = DeletionProbe(web_app)
    probe.fail_once_at("ledger")  # 金庫の削除は済んだあと、開示台帳の段で失敗する
    web_app.clock.advance(31 * _DAY)

    first = await web_app.services.principal_sweeper.sweep_once()

    assert (first.due, first.completed, first.incomplete) == (1, 0, 1)
    assert not web_app.store._principal_ref(pid).get().exists  # 金庫の削除は済んでいる
    remaining = documents_mentioning(web_app.default_db, pid, CANARY)  # (default) には、まだ残っている
    assert any(path.startswith("stages/") for path in remaining)
    assert any("/ledger/" in path for path in remaining)
    assert _meta(web_app, pid)["deletion_state"] == "deleting"  # 印は残っている

    second = await web_app.services.principal_sweeper.sweep_once()

    assert (second.due, second.deleting, second.completed) == (0, 1, 1)
    assert documents_mentioning(web_app.default_db, pid, CANARY) == {}
    assert documents_mentioning(web_app.store._db, pid, CANARY) == {}
    assert _meta(web_app, pid) is None
    assert [step for step, _ in probe.steps] == ["vault", "ledger", "vault", "ledger", "stages", "meta"]
    assert all(state == "deleting" for _, state in probe.steps)  # どの段の直前にも、印は残っていた(最後に消える)


@pytest.mark.anyio
async def test_the_principal_sweeper_run_loop_sweeps_at_startup_and_then_every_interval(web_app):
    # DV-16(依頼者の見回りの動かし方。§4.1): 起動時に 1 回、その後は一定間隔(暫定 10 分)ごと。間隔は注入した sleep で数える
    # (実際には待たない)。
    browser = web_app.browser()
    pid, _ = await _principal_with_a_finished_negotiation(web_app, browser)
    web_app.clock.advance(31 * _DAY)
    web_app.sleep.blocking = True

    run_task = asyncio.create_task(web_app.services.principal_sweeper.run())
    await web_app.sleep.wait_for_calls(1)  # 起動時の見回りが済んで、間隔を待っている

    assert _meta(web_app, pid) is None  # 起動時の見回りで、期限切れの依頼者が消えた
    assert web_app.sleep.calls == [600]
    web_app.sleep.tick()
    await web_app.sleep.wait_for_calls(2)  # 次の見回り
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.anyio
async def test_a_summary_and_a_ledger_written_through_the_stage_api_are_deleted_after_30_days_even_after_a_failure(stage_env):
    # DV-16(段階開示 ④ の実経路): 最後に使ってから 30 日たった依頼者の、段 1 の「会う」の API で書いた職務要約(カナリア)と、段の遷移で書いた台帳が、
    # 依頼者の見回りで消える。段の状態の段で失敗させても、次の見回りが消し切り、principals_meta は最後に消える。まだ 30 日たたない依頼者は残る。
    env = stage_env()
    browser, other_browser = env.browser(), env.browser()
    pid, nid = await _principal_who_disclosed_through_the_stage_api(env, browser, CANARY)
    assert set(documents_mentioning(env.default_db, CANARY)) == {f"stages/{nid}"}  # 置いたカナリアが、削除の前は見つかる(確認の前提)
    env.clock.advance(10 * _DAY)
    other_pid, other_nid = await _principal_who_disclosed_through_the_stage_api(env, other_browser, _OTHER_CANARY, "request-0002")
    probe = DeletionProbe(env)
    probe.fail_once_at("stages")  # 金庫・開示台帳の削除は済み、段の状態の段で失敗する(要約が残る)
    env.clock.advance(20 * _DAY + _MINUTE)  # 最初の依頼者の最終利用から 30 日と 1 分。もう一方はまだ 20 日

    first = await env.services.principal_sweeper.sweep_once()

    assert (first.due, first.completed, first.incomplete) == (1, 0, 1)
    remaining = documents_mentioning(env.default_db, pid, CANARY)
    assert set(remaining) == {f"stages/{nid}", f"principals_meta/{pid}"}  # 台帳は消え、要約を持つ段の状態と、削除中の印が残っている
    assert _meta(env, pid)["deletion_state"] == "deleting"

    second = await env.services.principal_sweeper.sweep_once()

    assert (second.due, second.deleting, second.completed) == (0, 1, 1)
    assert documents_mentioning(env.default_db, pid, CANARY, nid) == {}
    assert documents_mentioning(env.store._db, pid, CANARY) == {}
    assert _meta(env, pid) is None
    assert [step for step, _ in probe.steps] == ["vault", "ledger", "stages", "vault", "ledger", "stages", "meta"]
    assert all(state == "deleting" for _, state in probe.steps)  # どの段の直前にも、印は残っていた(最後に消える)
    other_stage = env.default_db.collection("stages").document(other_nid).get().to_dict()
    assert (other_stage["job_summary"], other_stage["stage"]) == (_OTHER_CANARY, 2)  # まだ期限が来ていない依頼者は、何も消えていない
    assert len(list(env.default_db.collection("principals").document(other_pid).collection("ledger").stream())) == 7
    assert _meta(env, other_pid)["deletion_state"] == "active"
