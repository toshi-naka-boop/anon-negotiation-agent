"""DV-06: design.md §3.8(金庫の側)・§6.3(web の側)。

前半は金庫の側(store を直接呼ぶ)、末尾の `test_web_...` は web の側(1d-2。画面 API とミドルウェア・削除の流れ・
依頼者の見回りを、金庫の app を ASGI のままつないで確かめる)。金庫の側では、ここでは vault-db に残らない
ことだけを確かめる。

削除の後、vault-db にその依頼者のポリシー・帯・ブロックリスト・コピー・自分側の見え方が
残らないこと、相手が本物なら相手側の見え方と「なし」の最終記録が残ること、相手が架空人物
なら交渉の文書もイベント列も 1 件も残らないこと、終わっていない交渉が取消になること、
各段の間で止めても呼び直せば最後まで進むこと、すでに消えていれば成功を返すことを確かめる。

相手が本物の交渉では、イベント列の見え方を消すだけでなく、交渉の文書に残る削除した側の評価(last_check)・
未決の提案と質問・属性帯・依頼者 ID(と、依頼者 ID を含む request_id)も消えること(台帳 C-41)、交渉の作成の
冪等キー(idempotency/{hash})が、相手が架空人物でも本物でも消えること(台帳 I-8)を確かめる。

差し戻し対応 3(§3.8 手順 1「以後、その依頼者が関わる操作はすべて拒否する」): 手順 1
(deleting を立てる)だけが済んだ状態で、依頼者を単位にする操作(PUT/GET policy・PUT
blocklist・本人の交渉一覧・交渉の作成)と、交渉を単位にする操作のうち moves・control・
principal-answer・view・events が拒否され、expire だけは拒否されず、相手側の読み出しは
通ることを確かめる。あわせて、削除の途中(手順 1 と 2 の間、手順 2 と 3 の間)に割り込んだ交渉の作成が
principal_deleting で断られ、削除後にその依頼者を参加者に持つ交渉が vault-db に残らないことを、
割り込みを決定的に起こして確かめる(並行して走らせる方法は、割り込みが起きないまま通り得るため、使わない)。

1b-1 は求人側を架空人物(テンプレート)に限っているので、相手が本物の依頼者の交渉は API
では作れない。そのため、その組み合わせだけは Firestore に直接状態を置いて確かめる
(新しい作成の経路は作らない)。
"""

import datetime as dt

import pytest

from vault.api_models import (
    ControlRequest,
    MoveRequest,
    PrincipalAnswerRequest,
    PutBlocklistRequest,
    PutPolicyRequest,
)
from vault.errors import MovePreconditionFailed, NotFoundError, PrincipalDeletingError
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
from test_stages import EMPLOYER_TEMPLATE_ID, agree, approve, meet, stage_env  # noqa: F401  (stage_env はフィクスチャ)
from web import ledger as ledger_module
from web import stages as stages_module
from web_app_helpers import CANARY, DeletionProbe, documents_mentioning, interview_body, plant_canaries
from web_helpers import create_demo_negotiation

def _write_live_negotiation_with_real_counterpart(
    store, clock, candidate_pid, employer_pid, *, request_id=None, candidate_policy=None, employer_policy=None
):
    """相手(求人側)も本物の依頼者である交渉を、Firestore に直接作る(§3.8 のテスト用)。

    1b-1 の作成 API は求人側を常にテンプレートにするので、この組み合わせは API では
    作れない。DELETE の後始末(相手が本物のとき)を確かめるためだけに、状態を直接置く。
    request_id を渡すと、その値と、それを指す冪等キー(idempotency/{hash})も置く(web は request_id を
    「依頼者 ID:画面の値」の形で付ける)。交渉のコピー(snapshots)のポリシーは、既定では両側とも何でも受ける。
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
        snapshots=Snapshots(
            candidate=candidate_policy if candidate_policy is not None else accept_all_policy("candidate"),
            employer=employer_policy if employer_policy is not None else accept_all_policy("employer"),
        ),
        participants=Participants(
            candidate=Participant(
                is_fictional=False, principal_id=candidate_pid, attribute_bands=default_attribute_bands()
            ),
            employer=Participant(
                is_fictional=False, principal_id=employer_pid, job_id="job-x", company_id="company-x"
            ),
        ),
        request_id=request_id if request_id is not None else new_id("req"),
        mode="live",
    )
    store._negotiation_ref(nid).set(model_to_firestore(doc))
    if request_id is not None:
        store._idempotency_ref(request_id).set({"nid": nid, "created_at": now})
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


def test_delete_erases_the_deleted_sides_evaluation_band_and_id_from_a_real_counterparts_negotiation(store, clock):
    # DV-06 / 台帳 C-41: 相手が本物の交渉では、イベント列の見え方を null にするだけでなく、交渉の文書に残る
    # 削除した側の評価(last_check)・未決の提案・属性帯・依頼者 ID も消える。get_view(nid, 削除した側) に評価が出ず、
    # vault-db のどこにも、その依頼者 ID(と、web が request_id に含める形の値)が残らない。
    # 相手(求人側)の確認結果と見え方は、残る。
    candidate_pid = new_id("principal")
    put_candidate_policy(store, candidate_pid, policy=accept_all_policy("candidate"))
    employer_pid = new_id("principal")
    request_id = f"{candidate_pid}:{CANARY}"  # web は「依頼者 ID:画面の値」の形で付ける(§6.3)
    nid = _write_live_negotiation_with_real_counterpart(
        store, clock, candidate_pid, employer_pid, request_id=request_id
    )
    package = sample_package()
    checked = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
    )
    proposed = store.process_move(
        nid, MoveRequest(expected_version=checked.version, side="candidate", move="propose", package=package)
    )
    store.process_move(
        nid, MoveRequest(expected_version=proposed.version, side="employer", move="check", package=package)
    )
    # 削除の前は、削除する側の評価が読め、依頼者 ID が交渉の文書に入っている(確認の前提)。
    before = store.get_view(nid, "candidate")
    assert before.last_check.own_evaluation.value == "acceptable"
    assert documents_mentioning(store._db, candidate_pid, CANARY)
    assert store._idempotency_ref(request_id).get().exists

    store.delete_principal(candidate_pid)

    after = store.get_view(nid, "candidate")
    assert (after.last_check, after.pending_offer, after.awaiting_principal_package) == (None, None, None)
    assert documents_mentioning(store._db, candidate_pid, CANARY) == {}  # vault-db のどこにも残らない
    document = store._negotiation_ref(nid).get().to_dict()
    assert document["participants"]["candidate"]["principal_id"] is None
    assert document["participants"]["candidate"]["attribute_bands"] is None
    assert document["last_check"]["candidate"] is None and document["pending_offer"] is None
    assert document["request_id"] == ""
    assert not store._idempotency_ref(request_id).get().exists  # 冪等キーも消えている
    # 相手(求人側)の情報は、そのまま残る。
    assert document["participants"]["employer"]["principal_id"] == employer_pid
    employer_view = store.get_view(nid, "employer")
    assert employer_view.last_check.package == package
    assert employer_view.last_check.own_evaluation.value == "acceptable"
    assert employer_view.counterparty is None  # 相手(候補者)の属性帯は消えたので、求人側の view に相手の帯は出ない
    assert [e.kind for e in store.get_events(nid, "employer")] == ["offer_received", "check", "final_result"]
    assert store.get_events(nid, "candidate") == []


@pytest.mark.parametrize("asker", ["candidate", "employer"])
def test_delete_erases_only_the_deleted_sides_pending_question(store, clock, asker):
    # DV-06 / 台帳 C-41: 削除した側(候補者)の途中確認(pending_question)は消える。相手(求人側)の途中確認は、
    # 相手のものなので残る。どちらの場合も、交渉は取消になっている。
    candidate_pid, employer_pid = new_id("principal"), new_id("principal")
    put_candidate_policy(store, candidate_pid, policy=needs_confirmation_policy("candidate"))
    nid = _write_live_negotiation_with_real_counterpart(
        store,
        clock,
        candidate_pid,
        employer_pid,
        candidate_policy=needs_confirmation_policy("candidate") if asker == "candidate" else None,
        employer_policy=needs_confirmation_policy("employer") if asker == "employer" else None,
    )
    package = sample_package()
    version = 0
    if asker == "employer":  # 手番を求人側に渡す
        version = store.process_move(
            nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
        ).version
    asked = store.process_move(
        nid, MoveRequest(expected_version=version, side=asker, move="ask_principal", package=package)
    )
    assert asked.status == "awaiting_principal"

    store.delete_principal(candidate_pid)

    document = store._negotiation_ref(nid).get().to_dict()
    assert (document["status"], document["end_reason"]) == ("judged", "cancelled")
    if asker == "candidate":
        assert document["pending_question"] is None
        assert store.get_view(nid, "candidate").awaiting_principal_package is None
    else:
        assert document["pending_question"]["side"] == "employer"  # 相手の質問は、相手のものとして残る
        assert store.get_view(nid, "employer").awaiting_principal_package == package
    assert document["pending_offer"] is None


def test_delete_stopped_before_the_last_step_of_a_real_counterparts_negotiation_is_finished_by_calling_again(
    store, clock, monkeypatch
):
    # DV-06 / 台帳 C-41: 依頼者 ID は最後に消す。交渉の文書を書き換える段(トランザクション)の前で止まっても、
    # 交渉には依頼者 ID が残っているので、呼び直せば、その交渉を引いて最後まで進む(削除した側の情報が残らない)。
    candidate_pid, employer_pid = new_id("principal"), new_id("principal")
    put_candidate_policy(store, candidate_pid)
    nid = _write_live_negotiation_with_real_counterpart(
        store, clock, candidate_pid, employer_pid, request_id=f"{candidate_pid}:request-0001"
    )
    package = sample_package()
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package))

    real_erase = store._erase_side_from_negotiation
    calls = []

    def failing_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("stopped before the last step")
        return real_erase(*args, **kwargs)

    monkeypatch.setattr(store, "_erase_side_from_negotiation", failing_once)
    with pytest.raises(RuntimeError):
        store.delete_principal(candidate_pid)

    # 止まった状態: 依頼者は deleting のまま残り、見え方は null になったが、依頼者 ID と評価は交渉に残っている。
    assert store._principal_ref(candidate_pid).get().to_dict()["deleting"] is True
    stopped = store._negotiation_ref(nid).get().to_dict()
    assert stopped["participants"]["candidate"]["principal_id"] == candidate_pid
    assert stopped["last_check"]["candidate"] is not None
    assert store._events(nid).get() and all(
        event.to_dict()["views"]["candidate"] is None for event in store._events(nid).stream()
    )

    store.delete_principal(candidate_pid)  # 呼び直す

    assert len(calls) == 2
    assert store._principal_ref(candidate_pid).get().exists is False
    assert documents_mentioning(store._db, candidate_pid) == {}
    assert store._negotiation_ref(nid).get().to_dict()["last_check"]["candidate"] is None


def test_delete_removes_the_creation_idempotency_key_of_a_fictional_counterparts_negotiation(store):
    # DV-06 / 台帳 I-8: 相手が架空人物の交渉を消すとき、交渉の文書の request_id から冪等キー(idempotency/{hash})も消える。
    # 別の依頼者・デモの交渉の冪等キーは、消えない。消えたキーは、by-request(台帳 X-57)でも 404 になる。
    pid, other_pid = new_id("principal"), new_id("principal")
    put_candidate_policy(store, pid)
    put_candidate_policy(store, other_pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    request_id, other_request_id = f"{pid}:request-0001", f"{other_pid}:request-0001"
    mine = store.create_negotiation(live_create_request(pid, employer_template.template_id, request_id=request_id))
    theirs = store.create_negotiation(
        live_create_request(other_pid, employer_template.template_id, request_id=other_request_id)
    )
    assert store._idempotency_ref(request_id).get().exists
    assert store.get_negotiation_by_request(request_id).nid == mine.nid  # 削除の前は、キーから交渉を引ける

    store.delete_principal(pid)

    assert not store._idempotency_ref(request_id).get().exists
    assert not store._negotiation_ref(mine.nid).get().exists
    with pytest.raises(NotFoundError):  # 本人の削除で消えたキーは、by-request でも 404
        store.get_negotiation_by_request(request_id)
    assert store._idempotency_ref(other_request_id).get().exists  # ほかの依頼者のキーは、そのまま
    assert store._negotiation_ref(theirs.nid).get().exists
    assert store.get_negotiation_by_request(other_request_id).nid == theirs.nid
    assert documents_mentioning(store._db, pid) == {}


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


@pytest.mark.parametrize("interrupted_listing", [0, 1], ids=["between_steps_1_and_2", "between_steps_2_and_3"])
def test_a_creation_that_lands_in_the_middle_of_a_deletion_is_refused_and_leaves_nothing_behind(
    store, monkeypatch, interrupted_listing
):
    # DV-06 / 差し戻し対応 3 / 台帳 C-39 の (d): 削除の途中(手順 1 で deleting を立てた後)に割り込んだ同じ依頼者の
    # 交渉の作成は、principal_deleting で断られる。削除の後には、その依頼者を参加者に持つ交渉も、冪等キーも残らない。
    # 割り込みは、決定的に起こす: 削除は、関わる交渉の一覧を 2 回引く(手順 2 の前・手順 3 の前)。その
    # interrupted_listing 回目の呼び出しの中で、同じ依頼者の作成を 1 本流す。
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    listings: list[str] = []
    creations = []
    original_listing = store._negotiation_ids_for_principal

    def listing_with_a_creation_in_the_middle(principal_id):
        if len(listings) == interrupted_listing:
            assert store._principal_ref(principal_id).get().to_dict()["deleting"] is True  # 手順 1 は済んでいる
            creations.append(store.create_negotiation(live_create_request(principal_id, employer_template.template_id)))
        listings.append(principal_id)
        return original_listing(principal_id)

    monkeypatch.setattr(store, "_negotiation_ids_for_principal", listing_with_a_creation_in_the_middle)

    store.delete_principal(pid)

    (created,) = creations  # 割り込みは、ちょうど 1 回起きた
    assert (created.status, created.reason, created.nid) == ("refused", "principal_deleting", None)
    assert len(listings) == 2  # 削除は、最後の段まで進んだ
    assert store._principal_ref(pid).get().exists is False
    assert list(store._negotiations().stream()) == []  # 交渉の文書が、1 件も残っていない
    assert list(store._db.collection("idempotency").stream()) == []  # 冪等キーも作られていない


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
    assert documents_mentioning(web_app.store._db, CANARY)  # vault-db にも置いてある(置かなければ、確認は必ず通る)
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
    assert documents_mentioning(web_app.store._db, _CANARY_OTHER)  # vault-db のほかの依頼者のカナリアは、消えていない
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
        ("GET", f"/v1/negotiations/{nids[1]}/stage", None),  # 段階開示(④)の経路も、削除中は拒否する
        ("POST", f"/v1/negotiations/{nids[1]}/stage/meet", {"job_summary": CANARY}),
        ("POST", f"/v1/negotiations/{nids[1]}/stage/approve", None),
        ("GET", f"/v1/principals/{pid}/ledger", None),
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


# ----------------------------------------------------------------------
# DV-06(段階開示 ④ の実経路): カナリアは、段 1 の職務要約と、面談の入力の両方に入れる。ここは、段 1 の「会う」の API で書いた要約と、
# 段の遷移で書いた台帳(plant_canaries が直接置く文書ではない)が、削除の連鎖で残らないことを確かめる。
# ----------------------------------------------------------------------


async def _principal_who_disclosed_through_the_stage_api(env, browser, canary: str, request_id: str = "request-0001"):
    """面談を送り、合意で終わった交渉の段 1 で、要約(カナリア)を書いて、段 2 まで進めた依頼者。(pid, nid) を返す。"""
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, EMPLOYER_TEMPLATE_ID, request_id)
    agree(env.store, nid)
    assert (await meet(browser, nid, canary)).json()["stage"] == 1
    assert (await approve(browser, nid)).json()["stage"] == 2
    return pid, nid


@pytest.mark.anyio
async def test_web_deletion_leaves_no_summary_or_ledger_that_was_written_through_the_stage_api(stage_env):
    # DV-06: 削除の後、(default) に、その依頼者の段の状態(段 1 の職務要約を含む)・台帳・カナリアが残らない(vault-db にも残らない)。
    # ほかの依頼者の段の状態・台帳は、そのまま残る。要約の本文があるのは、削除の前は段の状態だけ(台帳には書かない)。
    env = stage_env()
    browser, other_browser = env.browser(), env.browser()
    pid, nid = await _principal_who_disclosed_through_the_stage_api(env, browser, CANARY)
    other_pid, other_nid = await _principal_who_disclosed_through_the_stage_api(env, other_browser, _CANARY_OTHER)
    assert set(documents_mentioning(env.default_db, CANARY)) == {f"stages/{nid}"}  # 置いたカナリアが、削除の前は見つかる(確認の前提)
    ledger_paths = {path for path in documents_mentioning(env.default_db, pid) if "/ledger/" in path}
    assert len(ledger_paths) == 7  # 段 0〜2 の出来事(段の遷移で書いた台帳)

    response = await browser.post(f"/v1/principals/{pid}/delete")

    assert (response.status_code, response.json()) == (200, {"status": "deleted"})
    assert documents_mentioning(env.default_db, pid, CANARY, nid) == {}
    assert documents_mentioning(env.store._db, pid, CANARY) == {}
    assert not env.default_db.collection("stages").document(nid).get().exists
    # ほかの依頼者は、何も消えていない。
    other_stage = env.default_db.collection("stages").document(other_nid).get().to_dict()
    assert (other_stage["candidate_principal_id"], other_stage["job_summary"], other_stage["stage"]) == (other_pid, _CANARY_OTHER, 2)
    assert len(list(env.default_db.collection("principals").document(other_pid).collection("ledger").stream())) == 7
    assert (await other_browser.get(f"/v1/principals/{other_pid}/ledger")).status_code == 200
