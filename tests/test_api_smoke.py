"""design.md §3.3 の API(1b-1 で作る分)が、実際に HTTP・JSON として動くことの確認。

状態機械そのものの詳細(AC/DV の各条件)は、store を直接呼ぶ他のテストファイルで
確かめている。ここでは、FastAPI の配線(ルーティング・リクエスト/レスポンスの
シリアライズ)がひととおり壊れていないことだけを、実際の HTTP 呼び出しで確かめる。
"""

from vault_helpers import accept_all_policy, new_id


def test_policy_put_and_get_round_trip(api_client):
    pid = new_id("principal")
    policy = accept_all_policy("candidate")
    body = {"policy": policy.model_dump(mode="json"), "removed_axes": []}

    put_response = api_client.put(f"/v1/principals/{pid}/policy", json=body)
    assert put_response.status_code == 204

    get_response = api_client.get(f"/v1/principals/{pid}/policy")
    assert get_response.status_code == 200
    data = get_response.json()
    assert data["removed_axes"] == []
    assert data["policy"]["side"] == "candidate"


def test_policy_get_is_404_for_unknown_principal(api_client):
    response = api_client.get(f"/v1/principals/{new_id('nobody')}/policy")
    assert response.status_code == 404


def test_blocklist_put(api_client):
    pid = new_id("principal")
    response = api_client.put(f"/v1/principals/{pid}/blocklist", json={"blocklist": ["company-1"]})
    assert response.status_code == 204


def test_full_negotiation_lifecycle_over_http(api_client, store):
    # 求人側・候補者側のテンプレートを直接 vault-db に置く(1b-1 ではテンプレート作成用の
    # API は作らない設計なので、店の下請け関数で直接置く)。
    from vault_helpers import put_candidate_and_employer_templates

    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)

    pid = new_id("principal")
    api_client.put(
        f"/v1/principals/{pid}/policy",
        json={
            "policy": accept_all_policy("candidate").model_dump(mode="json"),
            "removed_axes": [],
            # 台帳 I-2: 本物の候補者の属性帯はポリシーと一緒に保存する(作成の要求では
            # 渡さない。test_attribute_bands_in_create_request_is_rejected を参照)。
            "attribute_bands": {
                "experience_band": "3_to_5y",
                "region_block": "kanto",
                "job_category": "it_web",
            },
        },
    )

    create_response = api_client.post(
        "/v1/negotiations",
        json={
            "request_id": new_id("req"),
            "mode": "live",
            "candidate": {"is_fictional": False, "principal_id": pid},
            "employer": {"template_id": employer_template.template_id},
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["status"] == "created"
    nid = created["nid"]

    # 本人の交渉一覧に出る。
    list_response = api_client.get(f"/v1/principals/{pid}/negotiations")
    assert list_response.status_code == 200
    assert any(item["nid"] == nid for item in list_response.json())

    # 見回り用の一覧にも出る。
    open_response = api_client.get("/v1/negotiations", params={"open": "true"})
    assert open_response.status_code == 200
    assert any(item["nid"] == nid for item in open_response.json()["items"])

    view_response = api_client.get(f"/v1/negotiations/{nid}/view", params={"side": "candidate"})
    assert view_response.status_code == 200
    assert view_response.json()["version"] == 0

    package = {
        "salary": 700,
        "remote_days": 2,
        "night_duty": 2,
        "review_months": 6,
        "training": "available",
        "side_job": "allowed",
        "start": "within_1_month",
    }
    move_response = api_client.post(
        f"/v1/negotiations/{nid}/moves",
        json={"expected_version": 0, "side": "candidate", "move": "propose", "package": package},
    )
    assert move_response.status_code == 200
    move_data = move_response.json()
    assert move_data["valid"] is True

    events_response = api_client.get(
        f"/v1/negotiations/{nid}/events", params={"side": "employer", "after_seq": 0}
    )
    assert events_response.status_code == 200
    assert len(events_response.json()) == 1

    control_response = api_client.post(
        f"/v1/negotiations/{nid}/control", json={"side": "candidate", "action": "pause"}
    )
    assert control_response.status_code == 200
    assert control_response.json()["paused"] is True

    expire_response = api_client.post(f"/v1/negotiations/{nid}/expire")
    assert expire_response.status_code == 200
    assert expire_response.json()["expired"] is False


def test_attribute_bands_in_create_request_is_rejected(api_client, store):
    # 台帳 I-2: 本物の候補者の属性帯は principals/{pid} に保存済みのものを金庫が読む。
    # 作成の要求(candidate)に attribute_bands を含めて送ると、
    # CandidateParticipantRequest がこのフィールドを持たない(extra="forbid")ため 422 になる。
    from vault_helpers import put_candidate_and_employer_templates, put_candidate_policy

    _, employer_template = put_candidate_and_employer_templates(store._db)
    pid = new_id("principal")
    put_candidate_policy(store, pid)

    response = api_client.post(
        "/v1/negotiations",
        json={
            "request_id": new_id("req"),
            "mode": "live",
            "candidate": {
                "is_fictional": False,
                "principal_id": pid,
                "attribute_bands": {
                    "experience_band": "3_to_5y",
                    "region_block": "kanto",
                    "job_category": "it_web",
                },
            },
            "employer": {"template_id": employer_template.template_id},
        },
    )
    assert response.status_code == 422


def test_move_with_stale_version_is_409_over_http(api_client, store):
    from vault_helpers import demo_create_request, put_candidate_and_employer_templates

    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    created = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )

    response = api_client.post(
        f"/v1/negotiations/{created.nid}/moves",
        json={"expected_version": 99, "side": "candidate", "move": "end"},
    )
    assert response.status_code == 409
