"""AC-17(次の 2 点。誰も操作しない交渉を見回りで期限切れにする部分は tests/test_referee_resume.py の DV-08): design.md §3.1・§3.8。

終わった交渉のコピーが、終了処理の後に残らない(合意・取消・期限切れ・上限での停止・
エージェントの終了・費用の上限での停止のどれでも)。デモ・攻撃の交渉の文書とイベントの各記録に TTL の項目が
付き、本物の利用者の交渉には付かない。
"""

import datetime as dt

from vault.api_models import ControlRequest, MoveRequest
from vault.models import EmployerRule
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    live_create_request,
    make_employer_template,
    new_id,
    put_candidate_and_employer_templates,
    put_candidate_policy,
    reject_all_policy,
    sample_package,
)


def _create_demo(store):
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def _snapshots_of(store, nid):
    return store._negotiation_ref(nid).get().to_dict()["snapshots"]


def test_snapshots_are_gone_after_agreement(store):
    # AC-17
    nid = _create_demo(store)
    package = sample_package()
    v = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    ).version
    response = store.process_move(nid, MoveRequest(expected_version=v, side="employer", move="accept"))
    assert response.end_reason == "agreed"
    assert _snapshots_of(store, nid) is None


def test_snapshots_are_gone_after_cancel(store):
    # AC-17
    nid = _create_demo(store)
    response = store.control(nid, ControlRequest(side="candidate", action="cancel"))
    assert response.status == "judged"
    assert _snapshots_of(store, nid) is None


def test_snapshots_are_gone_after_timeout(store, clock):
    # AC-17
    nid = _create_demo(store)
    clock.advance(dt.timedelta(days=10))  # expires_at (72 時間) を大きく超える
    response = store.expire(nid)
    assert response.expired is True
    assert response.status == "judged"
    assert _snapshots_of(store, nid) is None


def test_snapshots_are_gone_after_stopped_budget(store):
    # AC-17
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, employer_rules=[_wildcard_employer_rule()]
    )
    nid = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    ).nid
    v = 0
    for _ in range(6):
        response = store.process_move(
            nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=sample_package())
        )
        v = response.version
        if response.status == "judged":
            break
        response = store.process_move(nid, MoveRequest(expected_version=v, side="employer", move="reject"))
        v = response.version

    final = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=sample_package())
    )
    assert final.end_reason == "stopped_budget"
    assert _snapshots_of(store, nid) is None


def test_snapshots_are_gone_after_stopped_invalid(store):
    # AC-17
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=reject_all_policy("candidate")
    )
    nid = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    ).nid
    v = 0
    response = None
    for _ in range(3):
        response = store.process_move(
            nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=sample_package())
        )
        v = response.version
    assert response.end_reason == "stopped_invalid"
    assert _snapshots_of(store, nid) is None


def test_snapshots_are_gone_after_ended_by_agent(store):
    # AC-17
    nid = _create_demo(store)
    response = store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="end"))
    assert response.end_reason == "ended_by_agent"
    assert _snapshots_of(store, nid) is None


def test_snapshots_are_gone_after_stop_cost_limit(store):
    # AC-17: 費用の上限での停止(control の stop_cost_limit。台帳 X-52)でも、終了処理でコピーが消える。
    # 一時停止中・途中確認中からの停止は tests/test_stop_cost_limit.py。
    nid = _create_demo(store)
    response = store.control(nid, ControlRequest(side="candidate", action="stop_cost_limit"))
    assert response.status == "judged"
    assert store._negotiation_ref(nid).get().to_dict()["end_reason"] == "stopped_cost"
    assert _snapshots_of(store, nid) is None


def test_demo_negotiation_and_events_have_ttl(store):
    # AC-17
    nid = _create_demo(store)
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["ttl_at"] is not None

    store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )
    events = list(store._events(nid).stream())
    assert len(events) == 1
    assert events[0].to_dict()["ttl_at"] is not None


def test_live_negotiation_and_events_have_no_ttl(store):
    # AC-17
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)

    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"
    nid = result.nid

    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["ttl_at"] is None

    store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )
    events = list(store._events(nid).stream())
    assert len(events) == 1
    assert events[0].to_dict()["ttl_at"] is None


def _wildcard_employer_rule():
    return EmployerRule(when={}, policy=accept_all_policy("employer"))
