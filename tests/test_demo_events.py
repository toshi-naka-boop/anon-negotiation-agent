"""デモ用の読み出し(金庫の側。台帳 X-38。design.md §6.3「デモ用のエンドポイントは、本物の依頼者には触れない。web と vault の両方で確かめる」)。

GET /v1/demo/negotiations/{nid}/events?side=&after_seq= は、金庫が持つ交渉の文書(正本)の mode が demo か attack で、かつ候補者が
架空人物のときだけ、通常の events と同じ応答を返す。それ以外(本物の利用者の交渉・存在しない交渉・交渉 ID の形でない値・
mode と候補者が食い違う文書)は、交渉があるかどうかを知らせないよう、存在しない交渉と同じ 404 にする。
web の補助の文書(stages)には頼らない(stages の項目が欠けた・壊れた場合でも、本物の利用者の側の見え方を返さない)。
"""

import datetime as dt

import pytest

from vault.api_models import MoveRequest
from vault.ids import generate_id
from vault.models import NegotiationDocument, Participant, Participants, Snapshots
from vault.serialization import model_to_firestore
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
    sample_package,
)

_SIDES = ("candidate", "employer")


def _create_fictional(store, mode: str) -> str:
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id, mode=mode)
    )
    assert result.status == "created"
    return result.nid


def _write_document(store, clock, *, mode: str, candidate_is_fictional: bool) -> str:
    """mode と候補者の種類を指定して、交渉の文書を直接置く(作成の API は、食い違う組み合わせを断るため)。"""
    nid = generate_id()
    now = clock.now()
    candidate = (
        Participant(is_fictional=True, template_id="template-x", attribute_bands=default_attribute_bands())
        if candidate_is_fictional
        else Participant(is_fictional=False, principal_id=new_id("principal"), attribute_bands=default_attribute_bands())
    )
    document = NegotiationDocument(
        nid=nid,
        status="active",
        to_move="candidate",
        created_at=now,
        expires_at=now + dt.timedelta(hours=72),
        deadline=now + dt.timedelta(minutes=5),
        snapshots=Snapshots(candidate=accept_all_policy("candidate"), employer=accept_all_policy("employer")),
        participants=Participants(
            candidate=candidate,
            employer=Participant(is_fictional=True, template_id="template-y", job_id="job-y", company_id="company-y"),
        ),
        request_id=new_id("req"),
        mode=mode,
    )
    store._negotiation_ref(nid).set(model_to_firestore(document))
    return nid


@pytest.mark.parametrize("mode", ["demo", "attack"])
def test_demo_events_return_the_same_response_as_the_normal_events_for_demo_and_attack_negotiations(
    api_client, store, mode
):
    # X-38: mode が demo・attack で、候補者が架空人物の交渉は、通常の events と同じ応答を返す(両側。after_seq も同じ)。
    nid = _create_fictional(store, mode)
    package = sample_package()
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package))
    proposed = store.process_move(
        nid, MoveRequest(expected_version=1, side="candidate", move="propose", package=package)
    )
    store.process_move(nid, MoveRequest(expected_version=proposed.version, side="employer", move="reject"))

    for side in _SIDES:
        for after_seq in (0, 1):
            params = {"side": side, "after_seq": after_seq}
            normal = api_client.get(f"/v1/negotiations/{nid}/events", params=params)
            demo = api_client.get(f"/v1/demo/negotiations/{nid}/events", params=params)
            assert (normal.status_code, demo.status_code) == (200, 200)
            assert demo.json() == normal.json()
    demo_all = api_client.get(f"/v1/demo/negotiations/{nid}/events", params={"side": "employer"}).json()
    assert [event["kind"] for event in demo_all] == ["offer_received", "reject"]  # 中身が空でないことも確かめる
    assert [event["seq"] for event in api_client.get(
        f"/v1/demo/negotiations/{nid}/events", params={"side": "candidate", "after_seq": 1}
    ).json()] == [2, 3]


def test_demo_events_never_reveal_a_live_negotiation_or_whether_a_negotiation_exists(api_client, store):
    # X-38: 本物の利用者の交渉・存在しない交渉・交渉 ID の形でない値は、どれも同じ形の 404(交渉があるかどうかを
    # 知らせない)。本物の利用者の側の見え方(check の評価など)は、この口からは読めない。
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    live = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    store.process_move(
        live.nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )

    ids = [live.nid, "0123456789abcdef", "0123456789ABCDEF", "not-a-negotiation-id"]  # 本物・存在しない・形が違う
    for nid in ids:
        for side in _SIDES:
            response = api_client.get(f"/v1/demo/negotiations/{nid}/events", params={"side": side})
            assert response.status_code == 404, (nid, side)
            assert response.json() == {"detail": nid}, (nid, side)  # 存在しないときと同じ形(本物かどうかで変わらない)

    # 対照: 通常の口では、同じ交渉の候補者側のイベントが読める(demo 用の口が、本物の交渉を断っているだけ)。
    normal = api_client.get(f"/v1/negotiations/{live.nid}/events", params={"side": "candidate"})
    assert [event["kind"] for event in normal.json()] == ["check"]


@pytest.mark.parametrize(
    ("mode", "candidate_is_fictional", "readable"),
    [
        ("demo", True, True),
        ("attack", True, True),
        ("demo", False, False),  # mode は demo だが、候補者が本物
        ("attack", False, False),
        ("live", True, False),  # 候補者は架空人物だが、mode が live
        ("live", False, False),
    ],
)
def test_demo_events_check_both_the_mode_and_the_fictional_candidate_of_the_stored_document(
    api_client, store, clock, mode, candidate_is_fictional, readable
):
    # X-38: 保存された交渉の文書(正本)が、mode(demo・attack)と候補者(架空人物)の両方を満たすときだけ読める。
    # どちらか片方が外れる文書(不整合な文書)は、404。web の補助の文書には頼らない。
    nid = _write_document(store, clock, mode=mode, candidate_is_fictional=candidate_is_fictional)

    response = api_client.get(f"/v1/demo/negotiations/{nid}/events", params={"side": "candidate"})

    assert response.status_code == (200 if readable else 404)
