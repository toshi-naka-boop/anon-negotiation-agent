"""DV-12: 交渉の作成(design.md §3.5・§12.2)。

同じ request_id での冪等性、本物の候補者の同時 1 件制限、予算の予約と枯渇、
交渉の途中で 1 日の予算が尽きないこと、属性帯(台帳 I-2)を保存済みでなければ
作れないことを確かめる。

冪等キー(idempotency/{hash}。台帳 I-8): 交渉が先に消えて、キーだけが残っていても(TTL・本人の削除の途中)、
古いキーとして上書きして作り直す(500 にしない)。デモ・攻撃のキーには、交渉と同じ TTL(ttl_at)が付き、
本物の依頼者のキーには付かない(本人の削除で消える。tests/test_principal_deletion.py)。

冪等キーからの引き直し(GET /v1/negotiations/by-request/{request_id}。台帳 X-57): 作ったキーで nid が返る。未知のキー・金庫が
断った作成のキー・古いキー(交渉が先に消えた)は 404(NotFoundError)。読み出しだけで、何も書かない。
"""

import datetime as dt

import pytest
from negotiation_core import CandidateAttributeBands, Policy, Verdict, evaluate

from vault.api_models import ControlRequest, MoveRequest
from vault.errors import NotFoundError
from vault.models import EmployerRule
from vault.serialization import model_from_firestore
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    default_attribute_bands,
    demo_create_request,
    live_create_request,
    make_employer_template,
    new_id,
    put_candidate_and_employer_templates,
    put_candidate_policy,
    put_candidate_policy_without_bands,
    reject_all_policy,
    sample_package,
)


def test_same_request_id_returns_same_negotiation_and_reserves_budget_once(store):
    # DV-12: 同じ request_id で作成を 2 回送ると、同じ交渉が返り、予算の予約も 1 回だけ。
    _, employer_template = put_candidate_and_employer_templates(store._db)
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    request_id = new_id("req")
    req = live_create_request(pid, employer_template.template_id, request_id=request_id)

    first = store.create_negotiation(req)
    second = store.create_negotiation(req)

    assert first.status == "created"
    assert second.status == "created"
    assert first.nid == second.nid

    principal_doc = store._principal_ref(pid).get().to_dict()
    assert principal_doc["evaluation_budget"]["used"] == 17  # 17 が 1 回だけ引かれている


def test_real_candidate_can_have_only_one_active_negotiation(store):
    # DV-12: 本物の候補者は、進行中(judged でない)の交渉を同時に 1 件までしか持てない。
    _, employer_template_1 = put_candidate_and_employer_templates(store._db)
    employer_template_2 = make_employer_template()
    put_template(store._db, employer_template_2)

    pid = new_id("principal")
    put_candidate_policy(store, pid)

    first = store.create_negotiation(live_create_request(pid, employer_template_1.template_id))
    assert first.status == "created"

    second = store.create_negotiation(live_create_request(pid, employer_template_2.template_id))
    assert second.status == "refused"
    assert second.reason == "already_active"

    # 最初の交渉が終われば、また新しく作れる。
    store.control(first.nid, ControlRequest(side="candidate", action="cancel"))
    third = store.create_negotiation(live_create_request(pid, employer_template_2.template_id))
    assert third.status == "created"
    assert third.nid != first.nid


def test_creation_is_refused_once_daily_budget_is_exhausted(store):
    # DV-12: 予約できないとき(1 日の評価予算 170 に対し、交渉ごとに 17 を予約。
    # 170 // 17 = 10 件で使い切る)は、作成が断られる。
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    created_nids = []
    for _ in range(10):
        employer_template = make_employer_template()
        put_template(store._db, employer_template)
        result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
        assert result.status == "created"
        created_nids.append(result.nid)
        # 進行中 1 件までの制限に引っかからないよう、都度終わらせる。
        store.control(result.nid, ControlRequest(side="candidate", action="cancel"))

    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    refused = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert refused.status == "refused"
    assert refused.reason == "budget_exhausted"

    principal_doc = store._principal_ref(pid).get().to_dict()
    assert principal_doc["evaluation_budget"]["used"] == 170  # 交渉を作り直しても回復しない


def _evaluation_budget(store, pid) -> dict:
    """依頼者の 1 日の評価予算の窓(window_started_at と used)。"""
    return store._principal_ref(pid).get().to_dict()["evaluation_budget"]


def test_daily_budget_never_runs_out_mid_negotiation(store):
    # DV-12: 交渉の途中で本人の 1 日の予算が尽きることはない(予約済みのため)。
    # 1 日の窓の残りを、ちょうど 1 つの交渉の予約分(17)にしてから作成する(9 件作って取り消した後。
    # 使用済みは 153)。作成の予約で、窓の残りは 0 になる。その状態で、交渉の評価上限(17)いっぱいまで
    # check を送っても全部通り、1 日の予算(used)は check の前後で変わらない。
    # 評価のたびに 1 日の予算も引く実装なら、残り 0 の窓では拒否されるか、used が増える(台帳 C-39 の (b))。
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    for _ in range(9):
        employer_template = make_employer_template()
        put_template(store._db, employer_template)
        earlier = store.create_negotiation(live_create_request(pid, employer_template.template_id))
        assert earlier.status == "created"
        store.control(earlier.nid, ControlRequest(side="candidate", action="cancel"))  # 進行中 1 件までの制限を避ける
    assert _evaluation_budget(store, pid)["used"] == 153  # 1 日の窓の残りは、ちょうど 17

    _, employer_template = put_candidate_and_employer_templates(store._db)
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"
    nid = result.nid
    window_after_reservation = _evaluation_budget(store, pid)
    assert window_after_reservation["used"] == 170  # 予約で、1 日の窓の残りは 0

    version = 0
    for _ in range(17):
        response = store.process_move(
            nid,
            MoveRequest(expected_version=version, side="candidate", move="check", package=sample_package()),
        )
        assert response.valid is True
        version = response.version
        assert _evaluation_budget(store, pid) == window_after_reservation  # check のたびに、1 日の予算は変わらない

    # 17 回使い切った後の 18 回目は、交渉ごとの評価上限(evaluation_budget_exhausted)で
    # 無効になる。日次予算が別途尽きて拒否されるわけではないことが、ここまでの 17 回が
    # すべて成功したことで確かめられている。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="check", package=sample_package())
    )
    assert response.valid is False
    assert response.error == "evaluation_budget_exhausted"
    assert _evaluation_budget(store, pid) == window_after_reservation


def test_candidate_without_stored_attribute_bands_cannot_create_a_negotiation(store):
    # DV-12 / 台帳 I-2: 属性帯のないポリシー(policy だけ PUT 済みで、帯は未保存)の
    # 候補者は、交渉を作れない(理由が返る。交渉もイベントも回数も作らない)。
    pid = new_id("principal")
    put_candidate_policy_without_bands(store, pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)

    negotiations_before = list(store._negotiations().stream())
    response = store.create_negotiation(live_create_request(pid, employer_template.template_id))

    assert response.status == "refused"
    assert response.reason == "attribute_bands_missing"
    assert response.nid is None
    assert list(store._negotiations().stream()) == negotiations_before

    principal_doc = store._principal_ref(pid).get().to_dict()
    assert principal_doc.get("evaluation_budget") is None  # 予算も予約されていない


def test_employer_rule_matching_uses_the_stored_attribute_bands(store):
    # DV-12 / 台帳 I-2: 求人側のルールの当てはめ(snapshots)は、保存済みの属性帯で
    # 行われる。5〜10 年の帯にだけ合う狭いルールを、ワイルドカードより前に置き、
    # 保存した帯がそのルールに一致する場合に限って、そのルールの求人ポリシーが
    # snapshots.employer に使われることを確かめる。
    narrow_rule = EmployerRule(
        when={"experience_band": "5_to_10y"}, policy=accept_all_policy("employer")
    )
    wildcard_rule = EmployerRule(when={}, policy=reject_all_policy("employer"))
    employer_template = make_employer_template(rules=[narrow_rule, wildcard_rule])
    put_template(store._db, employer_template)

    pid = new_id("principal")
    matching_bands = CandidateAttributeBands(
        experience_band="5_to_10y", region_block="kanto", job_category="it_web"
    )
    put_candidate_policy(store, pid, attribute_bands=matching_bands)

    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"

    doc = store._negotiation_ref(result.nid).get().to_dict()
    employer_policy_side = doc["snapshots"]["employer"]["side"]
    assert employer_policy_side == "employer"
    # narrow_rule(accept_all)が当たっていれば、どんな package でも ACCEPTABLE になる。
    # wildcard_rule(reject_all)が誤って当たっていれば NOT_ACCEPTABLE になってしまう。
    employer_policy = model_from_firestore(Policy, doc["snapshots"]["employer"])
    assert evaluate(employer_policy, sample_package()) is Verdict.ACCEPTABLE

    # 比較のため、帯が一致しない候補者では wildcard_rule(reject_all)が使われる。
    other_pid = new_id("principal")
    non_matching_bands = default_attribute_bands()  # experience_band="3_to_5y"
    put_candidate_policy(store, other_pid, attribute_bands=non_matching_bands)
    other_result = store.create_negotiation(live_create_request(other_pid, employer_template.template_id))
    other_doc = store._negotiation_ref(other_result.nid).get().to_dict()
    other_employer_policy = model_from_firestore(Policy, other_doc["snapshots"]["employer"])
    assert evaluate(other_employer_policy, sample_package()) is Verdict.NOT_ACCEPTABLE


def test_a_leftover_idempotency_key_without_its_negotiation_is_replaced_and_the_negotiation_is_created_again(store):
    # DV-12 / 台帳 I-8: 冪等キーが残っていて、指す交渉がない(交渉が TTL で先に消えた)とき、古いキーとして扱い、
    # 上書きして作り直す。以前は、存在しない文書を読み戻そうとして、500 になった。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    request = demo_create_request(candidate_template.template_id, employer_template.template_id, request_id="req-stale")
    first = store.create_negotiation(request)
    store._negotiation_ref(first.nid).delete()  # 交渉だけが先に消えた(冪等キーは残っている)
    assert store._idempotency_ref("req-stale").get().exists

    second = store.create_negotiation(request)

    assert (second.status, second.version) == ("created", 0)
    assert second.nid != first.nid
    assert store._idempotency_ref("req-stale").get().to_dict()["nid"] == second.nid  # キーは、新しい交渉を指す
    assert store._negotiation_ref(second.nid).get().exists
    # 交渉がある間は、これまでどおり、同じ request_id に同じ交渉を返す(二重に作らない。§3.5)。
    assert store.create_negotiation(request).nid == second.nid


def test_a_stale_idempotency_key_of_a_live_negotiation_is_recreated_for_an_existing_principal_and_refused_for_a_deleted_one(
    store,
):
    # DV-12 / 台帳 I-8: 本物の依頼者でも、キーだけが残っていれば作り直す(予約もやり直す)。依頼者の文書がなければ、
    # 作り直しの中の確認で断られる(500 にならない)。
    _, employer_template = put_candidate_and_employer_templates(store._db)
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    request = live_create_request(pid, employer_template.template_id, request_id="req-live-stale")
    first = store.create_negotiation(request)
    store._negotiation_ref(first.nid).delete()

    second = store.create_negotiation(request)

    assert second.status == "created" and second.nid != first.nid
    assert _evaluation_budget(store, pid)["used"] == 34  # 作り直しで、予約もやり直された(17 × 2)

    store._negotiation_ref(second.nid).delete()
    store._principal_ref(pid).delete()  # 依頼者の文書もない
    with pytest.raises(NotFoundError):
        store.create_negotiation(request)


@pytest.mark.parametrize("mode", ["demo", "attack"])
def test_demo_and_attack_idempotency_keys_carry_the_negotiations_ttl(store, clock, mode):
    # 台帳 I-8: デモ・攻撃の冪等キーには、交渉と同じ ttl_at(暫定 96 時間後)を付ける(Firestore の TTL ポリシーで消える)。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    created = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id, mode=mode, request_id="req-ttl")
    )

    key = store._idempotency_ref("req-ttl").get().to_dict()
    negotiation = store._negotiation_ref(created.nid).get().to_dict()

    assert key["ttl_at"] == negotiation["ttl_at"] == clock.now() + dt.timedelta(hours=96)


def test_a_live_negotiations_idempotency_key_carries_no_ttl(store):
    # 台帳 I-8: 本物の依頼者のキーには TTL を付けない(交渉と同じ扱い。本人の削除で消える)。
    _, employer_template = put_candidate_and_employer_templates(store._db)
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    store.create_negotiation(live_create_request(pid, employer_template.template_id, request_id="req-live-ttl"))

    key = store._idempotency_ref("req-live-ttl").get().to_dict()

    assert "ttl_at" not in key


# --- 冪等キーからの引き直し(GET /v1/negotiations/by-request/{request_id}。台帳 X-57) ---


def test_a_request_id_resolves_to_the_negotiation_created_with_it_and_reading_writes_nothing(store):
    # DV-12 / 台帳 X-57: 作成のキー(request_id)から、その交渉の nid を引ける(本物の候補者・デモの両方)。同じキーでの作成の
    # 再送が返す交渉と同じ(二重に作られない)。読み出しだけで、交渉の文書も冪等キーも変えない(version も進めない)。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    live_request = live_create_request(pid, employer_template.template_id, request_id=f"{pid}:request-0001")
    demo_request = demo_create_request(
        candidate_template.template_id, employer_template.template_id, request_id="demo:request-0001"
    )
    live, demo = store.create_negotiation(live_request), store.create_negotiation(demo_request)
    assert live.nid != demo.nid

    def stored_documents():
        return [
            (store._negotiation_ref(created.nid).get().to_dict(), store._idempotency_ref(request.request_id).get().to_dict())
            for created, request in ((live, live_request), (demo, demo_request))
        ]

    before = stored_documents()

    assert store.get_negotiation_by_request(live_request.request_id).nid == live.nid
    assert store.get_negotiation_by_request(demo_request.request_id).nid == demo.nid
    assert store.create_negotiation(live_request).nid == live.nid  # 同じキーでの作成の再送も、同じ交渉
    assert store.create_negotiation(demo_request).nid == demo.nid
    assert stored_documents() == before


def test_an_unknown_request_id_is_not_found(store):
    # DV-12 / 台帳 X-57: 一度も作っていないキーは 404(NotFoundError)。web は、これを受けて入場の判定と作成に進む。
    # キーは完全に一致したものだけが引ける(似たキー・前の部分が同じキーでは引けない)。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id, request_id="demo:request-0001")
    )

    for unknown in ("never-created", "demo:request-0002", "demo:request-000", "demo:request-00011"):
        with pytest.raises(NotFoundError):
            store.get_negotiation_by_request(unknown)


def test_a_creation_the_vault_refused_leaves_no_key_to_find(store):
    # DV-12 / 台帳 X-57・C-45: 金庫が断った作成(ここでは already_active)は、何も書かない。そのキーでは、あとから引けない
    # (web は、断られた作成の再送を同じ交渉としては扱わず、もう一度入場の判定と作成を通す)。
    _, first_employer_template = put_candidate_and_employer_templates(store._db)
    second_employer_template = make_employer_template()
    put_template(store._db, second_employer_template)
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    first = store.create_negotiation(
        live_create_request(pid, first_employer_template.template_id, request_id="req-accepted")
    )
    refused = store.create_negotiation(
        live_create_request(pid, second_employer_template.template_id, request_id="req-refused")
    )
    assert (refused.status, refused.reason) == ("refused", "already_active")

    assert store.get_negotiation_by_request("req-accepted").nid == first.nid
    with pytest.raises(NotFoundError):
        store.get_negotiation_by_request("req-refused")


def test_a_stale_key_without_its_negotiation_is_not_found_until_creation_replaces_it(store):
    # DV-12 / 台帳 X-57・I-8: キーだけが残っていて、指す交渉がない(交渉が TTL や本人の削除で先に消えた)とき、404。nid を返すと、
    # web は作成に進めず、存在しない交渉を返し続けてしまう。作成は古いキーを上書きして作り直し、その後は新しい nid が引ける。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    request = demo_create_request(
        candidate_template.template_id, employer_template.template_id, request_id="req-stale-by-request"
    )
    first = store.create_negotiation(request)
    store._negotiation_ref(first.nid).delete()  # 交渉だけが先に消えた(冪等キーは残っている)
    assert store._idempotency_ref("req-stale-by-request").get().exists

    with pytest.raises(NotFoundError):
        store.get_negotiation_by_request("req-stale-by-request")

    second = store.create_negotiation(request)
    assert second.nid != first.nid
    assert store.get_negotiation_by_request("req-stale-by-request").nid == second.nid
