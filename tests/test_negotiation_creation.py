"""DV-12: 交渉の作成(design.md §3.5・§12.2)。

同じ request_id での冪等性、本物の候補者の同時 1 件制限、予算の予約と枯渇、
交渉の途中で 1 日の予算が尽きないこと、属性帯(台帳 I-2)を保存済みでなければ
作れないことを確かめる。
"""

from negotiation_core import CandidateAttributeBands, Policy, Verdict, evaluate

from vault.api_models import ControlRequest, MoveRequest
from vault.models import EmployerRule
from vault.serialization import model_from_firestore
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    default_attribute_bands,
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
    assert principal_doc["evaluation_budget"]["used"] == 16  # 16 が 1 回だけ引かれている


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
    # DV-12: 予約できないとき(1 日の評価予算 160 に対し、交渉ごとに 16 を予約。
    # 160 // 16 = 10 件で使い切る)は、作成が断られる。
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
    assert principal_doc["evaluation_budget"]["used"] == 160  # 交渉を作り直しても回復しない


def test_daily_budget_never_runs_out_mid_negotiation(store):
    # DV-12: 交渉の途中で本人の 1 日の予算が尽きることはない(予約済みのため)。
    # 1 つの交渉の中で、評価上限(16)いっぱいまで check を送っても、日次予算の枯渇による
    # 拒否(evaluation_budget_exhausted 以外の理由)が起きないことを確かめる。
    _, employer_template = put_candidate_and_employer_templates(store._db)
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"
    nid = result.nid

    version = 0
    for _ in range(16):
        response = store.process_move(
            nid,
            MoveRequest(expected_version=version, side="candidate", move="check", package=sample_package()),
        )
        assert response.valid is True
        version = response.version

    # 16 回使い切った後の 17 回目は、交渉ごとの評価上限(evaluation_budget_exhausted)で
    # 無効になる。日次予算が別途尽きて拒否されるわけではないことが、ここまでの 16 回が
    # すべて成功したことで確かめられている。
    response = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="check", package=sample_package())
    )
    assert response.valid is False
    assert response.error == "evaluation_budget_exhausted"


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

