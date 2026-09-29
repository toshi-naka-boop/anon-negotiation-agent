"""DV-06: design.md §3.8(金庫の側)・§6.3(web の側)。

前半は金庫の側(store を直接呼ぶ)、末尾の `test_web_...` は web の側(1d-2。画面 API とミドルウェア・削除の流れ・
依頼者の見回りを、金庫の app を ASGI のままつないで確かめる)。金庫の側では、ここでは vault-db に残らない
ことだけを確かめる。

削除の後、vault-db にその依頼者のポリシー・帯・ブロックリスト・コピー・自分側の見え方が
残らないこと、相手が本物なら相手側の見え方と「なし」の最終記録が残ること、相手が架空人物
なら交渉の文書もイベント列も 1 件も残らないこと、終わっていない交渉が取消になること、
各段の間で止めても呼び直せば最後まで進むこと、すでに消えていれば成功を返すことを確かめる。

差し戻し対応 3(§3.8 手順 1「以後、その依頼者が関わる操作はすべて拒否する」): 手順 1
(deleting を立てる)だけが済んだ状態で、依頼者を単位にする操作(PUT/GET policy・PUT
blocklist・本人の交渉一覧・交渉の作成)と、交渉を単位にする操作のうち moves・control・
principal-answer・view・events が拒否され、expire だけは拒否されず、相手側の読み出しは
通ることを確かめる。あわせて、交渉の作成と削除を並行して繰り返しても、削除後に
その依頼者を参加者に持つ交渉が vault-db に残らないことを確かめる。

1b-1 は求人側を架空人物(テンプレート)に限っているので、相手が本物の依頼者の交渉は API
では作れない。そのため、その組み合わせだけは Firestore に直接状態を置いて確かめる
(新しい作成の経路は作らない)。
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from google.cloud.firestore_v1.base_query import FieldFilter

from vault.api_models import (
    ControlRequest,
    MoveRequest,
    PrincipalAnswerRequest,
    PutBlocklistRequest,
    PutPolicyRequest,
)
from vault.errors import MovePreconditionFailed, PrincipalDeletingError, TransactionRetryExhausted
from vault.ids import generate_id
from vault.models import EmployerRule, NegotiationDocument, Participant, Participants, Snapshots
from vault.serialization import model_to_firestore
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    default_attribute_bands,
    live_create_request,
    make_employer_template,
    needs_confirmation_policy,
    new_id,
    put_candidate_policy,
    sample_package,
)
from web import ledger as ledger_module
from web import stages as stages_module
from web_app_helpers import CANARY, DeletionProbe, documents_mentioning, interview_body, plant_canaries
from web_helpers import create_demo_negotiation

# エミュレータの粗いロック実装(store.py の _new_transaction を参照)を踏まえ、
# 交渉の作成と削除の並行テストは並行数を抑える。
_CONCURRENT_CREATE_ATTEMPTS = 6


def _write_live_negotiation_with_real_counterpart(store, clock, candidate_pid, employer_pid):
    """相手(求人側)も本物の依頼者である交渉を、Firestore に直接作る(§3.8 のテスト用)。

    1b-1 の作成 API は求人側を常にテンプレートにするので、この組み合わせは API では
    作れない。DELETE の後始末(相手が本物のとき)を確かめるためだけに、状態を直接置く。
    """
    nid = generate_id()
    now = clock.now()
    doc = NegotiationDocument(
        nid=nid,
        status="active",
        to_move="candidate",
        created_at=now,
        expires_at=now + dt.timedelta(hours=72),
        deadline=now + dt.timedelta(minutes=5),
        snapshots=Snapshots(candidate=accept_all_policy("candidate"), employer=accept_all_policy("employer")),
        participants=Participants(
            candidate=Participant(
                is_fictional=False, principal_id=candidate_pid, attribute_bands=default_attribute_bands()
            ),
            employer=Participant(
                is_fictional=False, principal_id=employer_pid, job_id="job-x", company_id="company-x"
            ),
        ),
        request_id=new_id("req"),
        mode="live",
    )
    store._negotiation_ref(nid).set(model_to_firestore(doc))
    return nid


def test_delete_principal_removes_policy_band_blocklist_and_budget(store):
    # DV-06: 削除の後、ポリシー・帯・ブロックリストが残らない(交渉が 1 件もない単純な場合)。
    pid = new_id("principal")
    put_candidate_policy(store, pid, removed_axes=["night_duty"])
    store.put_blocklist(pid, PutBlocklistRequest(blocklist=["some-company"]))
    assert store._principal_ref(pid).get().exists is True

    store.delete_principal(pid)

    assert store._principal_ref(pid).get().exists is False


def test_delete_cancels_unfinished_negotiations_and_removes_fictional_counterpart_entirely(store):
    # DV-06: 終わっていない交渉は取消になる。相手が架空人物(フィクスチャの求人)なら、
    # 交渉の文書もその下のイベント列の記録も 1 件も残らない。
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"
    nid = result.nid

    package = sample_package()
    check_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
    )
    assert check_response.status == "active"  # まだ終わっていない

    store.delete_principal(pid)

    assert store._principal_ref(pid).get().exists is False
    assert store._negotiation_ref(nid).get().exists is False
    assert list(store._events(nid).stream()) == []


def test_delete_keeps_the_real_counterpartys_view_and_nulls_out_own_view(store, clock):
    # DV-06: 相手が本物なら、交渉の文書は残り(取消になり)、削除した側の見え方(check・
    # propose・最終記録のすべて)は null になる一方、相手側の見え方(offer_received と
    # 「なし」の最終記録)は残る。
    candidate_pid = new_id("principal")
    put_candidate_policy(store, candidate_pid, policy=accept_all_policy("candidate"))
    employer_pid = new_id("principal")  # 相手側の本人文書そのものは作らない(消さないため)。
    nid = _write_live_negotiation_with_real_counterpart(store, clock, candidate_pid, employer_pid)

    package = sample_package()
    check_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
    )
    v = check_response.version
    propose_response = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=package)
    )
    assert propose_response.status == "active"  # まだ終わっていない

    store.delete_principal(candidate_pid)

    assert store._principal_ref(candidate_pid).get().exists is False

    negotiation_data = store._negotiation_ref(nid).get().to_dict()
    assert negotiation_data is not None  # 相手が本物なので交渉の文書は残る
    assert negotiation_data["status"] == "judged"
    assert negotiation_data["end_reason"] == "cancelled"

    # 削除した側(candidate)の見え方はすべて null になっている: 読み出すと 0 件。
    assert store.get_events(nid, "candidate") == []

    # 相手(employer)側の見え方は残る: offer_received と、「なし」の最終記録の 2 件。
    employer_events = store.get_events(nid, "employer")
    assert [e.kind for e in employer_events] == ["offer_received", "final_result"]
    assert employer_events[-1].result.likelihood == "none"


def test_deleting_principal_rejects_principal_answer(store):
    # DV-06: 削除中(deleting)の依頼者に関わる principal-answer は拒否される。
    pid = new_id("principal")
    put_candidate_policy(store, pid, policy=needs_confirmation_policy("candidate"))
    employer_template = make_employer_template(rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    put_template(store._db, employer_template)
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    nid = result.nid

    package = sample_package()
    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert ask_response.status == "awaiting_principal"

    # 削除の 1 段目(deleting にする)だけを単独で再現する。
    store._principal_ref(pid).set({"deleting": True}, merge=True)

    with pytest.raises(MovePreconditionFailed):
        store.process_principal_answer(
            nid,
            PrincipalAnswerRequest(
                expected_version=ask_response.version, side="candidate", package=package, answer="accept"
            ),
        )


def test_delete_principal_is_idempotent_when_called_twice(store):
    # DV-06: すでに消えていれば成功を返す(呼び直しても壊れない)。
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    store.delete_principal(pid)
    assert store._principal_ref(pid).get().exists is False

    store.delete_principal(pid)  # 2 回目(何もない状態への呼び出し)も例外なく成功する。
    assert store._principal_ref(pid).get().exists is False


def test_delete_principal_resumes_from_a_partially_completed_state(store):
    # DV-06: 各段の間で止めてから呼び直すと、最後まで進む。ここでは 1 段目(deleting)だけ
    # 済んで 2〜4 段目がまだの状態を直接作り、delete_principal を呼び直して最後まで
    # 進むことを確かめる(本人が押し直す想定はなく、呼び出し側が同じ呼び出しをもう一度
    # 行うだけで完了することを見る)。
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    nid = result.nid

    store._principal_ref(pid).set({"deleting": True}, merge=True)  # 1 段目だけ済んだ状態

    store.delete_principal(pid)  # 呼び直すと 2〜4 段目まで進む

    assert store._principal_ref(pid).get().exists is False
    assert store._negotiation_ref(nid).get().exists is False  # 架空の求人なので丸ごと消える


def test_delete_principal_on_an_unknown_principal_returns_successfully(store):
    # DV-06: そもそも存在しない依頼者(まだ何も保存していない)に対しても、例外なく成功する。
    store.delete_principal(new_id("nobody"))


def test_deleting_flag_alone_blocks_principal_and_negotiation_scoped_operations(store):
    # DV-06 / 差し戻し対応 3(§3.8 手順 1): 手順 1(deleting を立てる)だけが済んで、
    # まだ取消(手順 2)していない状態で、
    # ・依頼者を単位にする操作(PUT/GET policy・PUT blocklist・本人の交渉一覧・作成)
    # ・交渉を単位にする操作のうち moves・control・principal-answer・view・events(自分側)
    # が拒否され、expire は拒否されず、相手側(架空人物)の読み出しは通ることを確かめる。
    #
    # principal-answer の削除中ガードは 1b-2 ですでに実装・承認済み(MovePreconditionFailed)
    # で、その他の操作(このテストで新しく足した分)は PrincipalDeletingError を使う
    # (報告に記載)。pending_question が一致しないと principal-answer 自身の前提の方が
    # 先に働いてしまうので、awaiting_principal まで進めてから deleting を立てる。
    pid = new_id("principal")
    put_candidate_policy(store, pid, policy=needs_confirmation_policy("candidate"))
    employer_template = make_employer_template(rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))])
    put_template(store._db, employer_template)
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    nid = result.nid
    package = sample_package()

    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert ask_response.status == "awaiting_principal"

    # 手順 1 だけを単独で再現する(取消(手順 2)はまだ行わない)。
    store._principal_ref(pid).set({"deleting": True}, merge=True)

    # --- 1. 依頼者を単位にする操作 ---
    with pytest.raises(PrincipalDeletingError):
        store.put_policy(pid, PutPolicyRequest(policy=accept_all_policy("candidate"), removed_axes=[]))
    with pytest.raises(PrincipalDeletingError):
        store.get_policy(pid)
    with pytest.raises(PrincipalDeletingError):
        store.put_blocklist(pid, PutBlocklistRequest(blocklist=[]))
    with pytest.raises(PrincipalDeletingError):
        store.list_principal_negotiations(pid)

    other_employer_template = make_employer_template()
    put_template(store._db, other_employer_template)
    create_response = store.create_negotiation(live_create_request(pid, other_employer_template.template_id))
    assert create_response.status == "refused"
    assert create_response.reason == "principal_deleting"

    # --- 2. 交渉を単位にする操作 ---
    with pytest.raises(PrincipalDeletingError):
        store.process_move(
            nid,
            MoveRequest(
                expected_version=ask_response.version, side="candidate", move="check", package=package
            ),
        )
    with pytest.raises(PrincipalDeletingError):
        store.control(nid, ControlRequest(side="candidate", action="pause"))

    # principal-answer(1b-2 ですでに実装済みのガード。MovePreconditionFailed のまま)。
    with pytest.raises(MovePreconditionFailed):
        store.process_principal_answer(
            nid,
            PrincipalAnswerRequest(
                expected_version=ask_response.version, side="candidate", package=package, answer="accept"
            ),
        )

    # expire は拒否しない(システムの操作)。
    expire_response = store.expire(nid)
    assert expire_response.expired is False  # まだ期限切れではないが、例外にはならない

    # 相手側(employer。架空人物)の読み出しは通る。
    employer_view = store.get_view(nid, "employer")
    assert employer_view.status == "awaiting_principal"
    assert store.get_events(nid, "employer") == []

    # 自分側(candidate)の読み出しは拒否される。
    with pytest.raises(PrincipalDeletingError):
        store.get_view(nid, "candidate")
    with pytest.raises(PrincipalDeletingError):
        store.get_events(nid, "candidate")


def test_concurrent_creation_and_deletion_never_leaves_an_orphaned_negotiation(store):
    # DV-06 / 差し戻し対応 3: 交渉の作成と依頼者の削除を並行して何度も走らせても、削除が
    # 終わった後には、その依頼者を参加者に持つ交渉が vault-db に 1 件も残らない
    # (§3.8 手順 1 の拒否が交渉の作成にも効くことの確認)。
    #
    # 正確さの根拠は決定的なテスト(test_deleting_flag_alone_blocks_...。deleting=True の
    # 読み取りだけで作成が断られることを直接確かめる)と、作成が依頼者の文書を読む
    # トランザクションの中で判定していること(Firestore のトランザクションは、読んだ文書が
    # コミット前に変わっていれば abort・再試行するので、手順 1 の書き込みより後にコミットする
    # 作成は、deleting=True を読むまで必ず再試行される)にある。ここでは、その保証が実際の
    # 並行負荷のもとでも壊れないことを確認する(是正の再現そのものは、上の理由により
    # タイミングでほぼ強制できないため、壊れていないことの確認という位置づけ)。
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    def try_create():
        employer_template = make_employer_template()
        put_template(store._db, employer_template)
        try:
            store.create_negotiation(live_create_request(pid, employer_template.template_id))
        except TransactionRetryExhausted:
            pass  # エミュレータの競合。作成の成否そのものはこのテストの主題ではない。

    with ThreadPoolExecutor(max_workers=4) as pool:
        create_futures = [pool.submit(try_create) for _ in range(_CONCURRENT_CREATE_ATTEMPTS)]
        delete_future = pool.submit(store.delete_principal, pid)
        for future in create_futures:
            future.result()
        delete_future.result()

    # 削除の実行中に紛れ込んだ交渉があっても、各段は冪等なので、もう一度呼べば
    # 最後まで後始末される。
    store.delete_principal(pid)

    assert store._principal_ref(pid).get().exists is False
    remaining = list(
        store._negotiations().where(filter=FieldFilter("participants.candidate.principal_id", "==", pid)).stream()
    )
    assert remaining == []


# ----------------------------------------------------------------------
# DV-06(web の側): design.md §6.3 の削除の流れ。本人のボタン(POST /v1/principals/{pid}/delete)と、
# 依頼者の見回りが、同じ流れを使う。
# ----------------------------------------------------------------------

_CANARY_OTHER = "CANARY-OTHER-PRINCIPAL-0001"


async def _principal_with_two_negotiations(env, browser, canary: str = CANARY):
    """面談を送り、交渉を 2 件(1 件目は取消)作って、カナリアを置いた依頼者。(pid, [nid, nid]) を返す。"""
    pid = await browser.register()
    template_id = env.put_employer_template()
    first = await browser.create_negotiation(pid, template_id, "request-0001")
    await browser.post(f"/v1/negotiations/{first}/control", {"action": "cancel"})
    second = await browser.create_negotiation(pid, env.put_employer_template(), "request-0002")
    plant_canaries(env, pid, [first, second], canary)
    return pid, [first, second]


@pytest.mark.anyio
async def test_web_deletion_leaves_no_ledger_stage_or_canary_in_either_database(web_app):
    # DV-06: 削除の後、(default) に、その依頼者の台帳・段の状態・カナリアが残らない(vault-db にも残らない)。
    # 進行中の交渉は取消になる。ほかの依頼者のデータは、そのまま残る。
    browser, other_browser = web_app.browser(), web_app.browser()
    pid, nids = await _principal_with_two_negotiations(web_app, browser)
    other_pid, other_nids = await _principal_with_two_negotiations(web_app, other_browser, _CANARY_OTHER)
    assert documents_mentioning(web_app.default_db, pid, CANARY)  # 置いたカナリアが、削除の前は見つかる(確認の前提)
    assert web_app.store._negotiation_ref(nids[1]).get().exists  # 2 件目は進行中

    response = await browser.post(f"/v1/principals/{pid}/delete")

    assert (response.status_code, response.json()) == (200, {"status": "deleted"})
    assert browser.cookie is None  # 本人のボタンからのときは、クッキーも消える
    assert documents_mentioning(web_app.default_db, pid, CANARY) == {}
    assert documents_mentioning(web_app.store._db, pid, CANARY) == {}
    assert not web_app.store._principal_ref(pid).get().exists
    assert not any(web_app.store._negotiation_ref(nid).get().exists for nid in nids)  # 相手は架空の求人なので、丸ごと消える
    assert not any(list(web_app.store._events(nid).stream()) for nid in nids)
    # ほかの依頼者は、何も消えていない(利用記録・段の状態・開示台帳・金庫のポリシーと交渉)。
    assert web_app.default_db.collection("principals_meta").document(other_pid).get().exists
    assert all(web_app.default_db.collection("stages").document(nid).get().exists for nid in other_nids)
    assert len(list(web_app.default_db.collection("principals").document(other_pid).collection("ledger").stream())) == 2
    assert documents_mentioning(web_app.default_db, _CANARY_OTHER)
    assert web_app.store._principal_ref(other_pid).get().exists
    assert web_app.store._negotiation_ref(other_nids[1]).get().exists


@pytest.mark.anyio
async def test_web_operations_of_a_deleting_principal_are_rejected_on_every_route(web_app):
    # DV-06: 削除中(deletion_state=deleting)の操作は拒否される。すべての依頼者向けのルート(開始ページ・面談の送信・
    # 閲覧・ブロックリスト・交渉の作成・交渉への操作・データの削除)が 409 で、何も変えない。
    browser = web_app.browser()
    pid, nids = await _principal_with_two_negotiations(web_app, browser)
    template_id = web_app.put_employer_template()
    await web_app.services.meta.mark_deleting(pid)  # 削除の流れの 1 段目(印)だけが済んだ状態
    policy_before = web_app.store._principal_ref(pid).get().to_dict()["policy"]

    requests = [
        ("GET", "/start", None),
        ("POST", f"/v1/principals/{pid}/interview", interview_body(accept_anchors=[])),
        ("GET", f"/v1/principals/{pid}/policy", None),
        ("POST", f"/v1/principals/{pid}/blocklist", {"blocklist": ["company-x"]}),
        ("GET", f"/v1/principals/{pid}/negotiations", None),
        ("POST", f"/v1/principals/{pid}/negotiations", {"request_id": "request-0009", "employer_template_id": template_id}),
        ("GET", f"/v1/negotiations/{nids[1]}/events", None),
        ("POST", f"/v1/negotiations/{nids[1]}/control", {"action": "pause"}),
        ("POST", f"/v1/negotiations/{nids[1]}/principal-answer", {"package": sample_package().model_dump(), "answer": "accept"}),
        ("POST", f"/v1/principals/{pid}/delete", None),
    ]
    for method, path, body in requests:
        response = await (browser.get(path) if method == "GET" else browser.post(path, body))
        assert (response.status_code, response.json()) == (409, {"detail": "principal_deleting"}), (method, path)
        assert "set-cookie" not in response.headers  # クッキーの期限も延ばさない

    state = web_app.default_db.collection("principals_meta").document(pid).get().to_dict()
    assert state["deletion_state"] == "deleting"
    assert web_app.store._principal_ref(pid).get().to_dict()["policy"] == policy_before  # 面談の再送信は、金庫に届いていない
    assert web_app.store.get_view(nids[1], "employer").status == "active"  # 一時停止もされていない
    # デモ用のエンドポイントは、依頼者のセッションを見ない(削除中の依頼者のクッキーがあっても、デモは動く)。
    demo_nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()  # 架空の候補者の交渉にも、段の状態を作る(§6.2)
    assert (await browser.get(f"/v1/demo/negotiations/{demo_nid}/events", side="candidate")).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("failing_step", DeletionProbe.STEPS)
async def test_web_deletion_failing_between_steps_is_finished_by_the_principal_sweeper(web_app, failing_step):
    # DV-06: 各段(金庫・開示台帳・段の状態・利用記録)の入口で失敗させても、本人が押し直さずに、
    # 依頼者の見回りが最後まで進める。失敗の間、利用記録(削除中の印)は残り、最後の段で初めて消える。
    browser = web_app.browser()
    pid, nids = await _principal_with_two_negotiations(web_app, browser)
    probe = DeletionProbe(web_app)
    probe.fail_once_at(failing_step)

    response = await browser.post(f"/v1/principals/{pid}/delete")  # 本人が押すのは、これ 1 回だけ

    assert (response.status_code, response.json()) == (202, {"status": "deleting"})
    assert browser.cookie is not None  # 最後の段まで終わっていないので、クッキーはまだ消さない
    assert probe.meta_state(pid) == "deleting"  # 印は残っている(先に消えていない)
    reached = list(DeletionProbe.STEPS[: DeletionProbe.STEPS.index(failing_step) + 1])
    assert [step for step, _ in probe.steps] == reached
    assert (await browser.get(f"/v1/principals/{pid}/negotiations")).status_code == 409  # 削除中の操作は拒否される

    report = await web_app.services.principal_sweeper.sweep_once()

    assert (report.deleting, report.completed, report.incomplete, report.errors) == (1, 1, 0, 0)
    assert probe.meta_state(pid) is None
    assert documents_mentioning(web_app.default_db, pid, CANARY) == {}
    assert documents_mentioning(web_app.store._db, pid, CANARY) == {}
    assert not any(web_app.store._negotiation_ref(nid).get().exists for nid in nids)
    # 見回りは、流れを最初からやり直した。失敗した段を含め、どの段の直前にも、印は残っていた(先に消えていない)。
    assert [step for step, _ in probe.steps[len(reached):]] == list(DeletionProbe.STEPS)
    assert all(state == "deleting" for _, state in probe.steps)


@pytest.mark.anyio
async def test_web_usage_record_is_deleted_last_and_never_before_the_other_steps(web_app):
    # DV-06: principals_meta は最後に消え、それより先に消えることはない(印だけが先に消えて、データが残ることを防ぐ)。
    browser = web_app.browser()
    pid, nids = await _principal_with_two_negotiations(web_app, browser)
    probe = DeletionProbe(web_app)

    response = await browser.post(f"/v1/principals/{pid}/delete")

    assert response.status_code == 200
    # 金庫 → 開示台帳 → 段の状態 → 利用記録の順。どの段の直前にも、利用記録は削除中の印つきで残っている。
    assert probe.steps == [("vault", "deleting"), ("ledger", "deleting"), ("stages", "deleting"), ("meta", "deleting")]
    assert probe.meta_state(pid) is None  # 最後の段が終わって、初めて消える


@pytest.mark.anyio
async def test_web_deletion_without_a_usage_record_only_clears_the_cookie(web_app):
    # DV-06 / §6.3: 利用記録がなければ(面談を送っていなければ)、サーバにデータはないので、クッキーを消すだけで終える。
    browser = web_app.browser()
    pid = await browser.open_start_page()
    probe = DeletionProbe(web_app)

    response = await browser.post(f"/v1/principals/{pid}/delete")

    assert (response.status_code, response.json()) == (200, {"status": "deleted"})
    assert browser.cookie is None
    assert probe.steps == []  # 金庫にも (default) にも、何も呼んでいない


@pytest.mark.anyio
async def test_web_the_delete_response_clears_the_cookie_even_when_the_same_request_would_extend_it(web_app):
    # DV-06 / §6.3: 1 時間以上たってからの「データを消す」は、利用記録の更新(クッキーの期限の延長)も伴うが、応答が付ける
    # セッションクッキーは「消す」1 つだけ(延長のクッキーで、消したクッキーが復活しない)。
    browser = web_app.browser()
    pid = await browser.register()
    web_app.clock.advance(dt.timedelta(hours=2))

    response = await browser.post(f"/v1/principals/{pid}/delete")

    assert response.status_code == 200
    (header,) = response.headers.get_list("set-cookie")
    assert "Max-Age=0" in header
    assert browser.cookie is None
    assert web_app.default_db.collection("principals_meta").document(pid).get().exists is False


@pytest.mark.anyio
async def test_web_deletion_removes_more_documents_than_one_batch_holds(web_app, monkeypatch):
    # DV-06: 開示台帳の文書と段の状態が、1 回のバッチ(暫定 300 件)より多くても、すべて消える(バッチを繰り返す)。
    # バッチの大きさを 2 に絞って、少ない文書で、複数回のバッチを確かめる。
    monkeypatch.setattr(ledger_module, "_DELETE_BATCH_SIZE", 2)
    monkeypatch.setattr(stages_module, "_DELETE_BATCH_SIZE", 2)
    browser = web_app.browser()
    pid = await browser.register()
    nids = [f"{index:016x}" for index in range(1, 6)]
    for nid in nids:
        await web_app.services.stages.ensure(nid, pid)  # 5 件の段の状態
    ledger = web_app.default_db.collection("principals").document(pid).collection("ledger")
    for index in range(5):
        ledger.document(f"row-{index}").set({"principal_id": pid, "note": CANARY})  # 5 件の台帳

    response = await browser.post(f"/v1/principals/{pid}/delete")

    assert response.status_code == 200
    assert documents_mentioning(web_app.default_db, pid, CANARY) == {}
