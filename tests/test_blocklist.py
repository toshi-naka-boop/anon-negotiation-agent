"""AC-16(金庫の側): ブロック先の企業には、交渉もイベントも回数も生まれない。

design.md §3.5・§6.1。設計書にブロックの根拠となる具体的な HTTP ステータス等の指定が
見当たらないため、最も保守的な形(構造化された拒否応答。CreateNegotiationResponse の
status="refused", reason="blocked")で実装した(報告の 4 に記載)。
"""

from vault.api_models import PutBlocklistRequest
from vault.templates import put_template
from vault_helpers import (
    live_create_request,
    make_employer_template,
    new_id,
    put_candidate_and_employer_templates,
    put_candidate_policy,
)


def test_blocked_employer_creates_no_negotiation_no_events_no_counters(store):
    # AC-16
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    store.put_blocklist(pid, PutBlocklistRequest(blocklist=[employer_template.company_id]))

    negotiations_before = list(store._negotiations().stream())

    request_id = new_id("req")
    response = store.create_negotiation(
        live_create_request(pid, employer_template.template_id, request_id=request_id)
    )

    assert response.status == "refused"
    assert response.reason == "blocked"
    assert response.nid is None

    # 交渉が 1 件も作られていない。
    negotiations_after = list(store._negotiations().stream())
    assert len(negotiations_after) == len(negotiations_before)

    # 冪等キー(request_id)にも何も対応付けられていない(このリクエストでは何も
    # 「作っていない」ことの裏付け)。
    assert not store._idempotency_ref(request_id).get().exists

    # 評価予算が 1 円も予約されていない(回数が生まれていない)。
    principal_doc = store._principal_ref(pid).get().to_dict()
    assert principal_doc.get("evaluation_budget") is None


def test_non_blocked_employer_is_unaffected_by_an_unrelated_block_entry(store):
    # AC-16: ブロックリストに無関係な企業 ID が入っていても、ブロックされていない求人は
    # 通常どおり交渉を作れる(誤検出しないことの確認)。
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    store.put_blocklist(pid, PutBlocklistRequest(blocklist=[new_id("other-company")]))

    _, employer_template = put_candidate_and_employer_templates(store._db)
    response = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert response.status == "created"


def test_blocklist_is_checked_per_candidate_not_globally(store):
    # AC-16: ブロックリストは候補者ごと。別の候補者(ブロックしていない)は、同じ求人と
    # 通常どおり交渉できる。
    employer_template = make_employer_template()
    put_template(store._db, employer_template)

    blocking_pid = new_id("principal")
    put_candidate_policy(store, blocking_pid)
    store.put_blocklist(blocking_pid, PutBlocklistRequest(blocklist=[employer_template.company_id]))

    other_pid = new_id("principal")
    put_candidate_policy(store, other_pid)

    blocked_response = store.create_negotiation(live_create_request(blocking_pid, employer_template.template_id))
    assert blocked_response.status == "refused"
    assert blocked_response.reason == "blocked"

    allowed_response = store.create_negotiation(live_create_request(other_pid, employer_template.template_id))
    assert allowed_response.status == "created"
