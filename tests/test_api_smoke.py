"""design.md §3.3 の API(1b-1・1b-2 で作る分)が、実際に HTTP・JSON として動くことの確認。

状態機械そのものの詳細(AC/DV の各条件)は、store を直接呼ぶ他のテストファイルで
確かめている。ここでは、FastAPI の配線(ルーティング・リクエスト/レスポンスの
シリアライズ)がひととおり壊れていないことだけを、実際の HTTP 呼び出しで確かめる。
"""

from vault_helpers import accept_all_policy, new_id


def test_delete_principal_is_idempotent_over_http(api_client):
    # そもそも何もなくても成功し(204)、実在するものを消しても成功する。
    empty_response = api_client.delete(f"/v1/principals/{new_id('nobody')}")
    assert empty_response.status_code == 204

    pid = new_id("principal")
    put_response = api_client.put(
        f"/v1/principals/{pid}/policy",
        json={"policy": accept_all_policy("candidate").model_dump(mode="json"), "removed_axes": []},
    )
    assert put_response.status_code == 204

    delete_response = api_client.delete(f"/v1/principals/{pid}")
    assert delete_response.status_code == 204

    get_response = api_client.get(f"/v1/principals/{pid}/policy")
    assert get_response.status_code == 404


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


def test_principal_answer_over_http(api_client, store):
    from vault.models import EmployerRule
    from vault_helpers import needs_confirmation_policy, put_candidate_and_employer_templates

    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db,
        candidate_policy=needs_confirmation_policy("candidate"),
        employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))],
    )
    create_response = api_client.post(
        "/v1/negotiations",
        json={
            "request_id": new_id("req"),
            "mode": "demo",
            "candidate": {"is_fictional": True, "template_id": candidate_template.template_id},
            "employer": {"template_id": employer_template.template_id},
        },
    )
    nid = create_response.json()["nid"]

    package = {
        "salary": 700,
        "remote_days": 2,
        "night_duty": 2,
        "review_months": 6,
        "training": "available",
        "side_job": "allowed",
        "start": "within_1_month",
    }
    ask_response = api_client.post(
        f"/v1/negotiations/{nid}/moves",
        json={"expected_version": 0, "side": "candidate", "move": "ask_principal", "package": package},
    )
    assert ask_response.status_code == 200
    ask_data = ask_response.json()
    assert ask_data["status"] == "awaiting_principal"

    answer_response = api_client.post(
        f"/v1/negotiations/{nid}/principal-answer",
        json={
            "expected_version": ask_data["version"],
            "side": "candidate",
            "package": package,
            "answer": "accept",
        },
    )
    assert answer_response.status_code == 200
    assert answer_response.json()["status"] == "active"

    events_response = api_client.get(
        f"/v1/negotiations/{nid}/events", params={"side": "candidate", "after_seq": 0}
    )
    assert events_response.status_code == 200
    kinds = [e["kind"] for e in events_response.json()]
    assert "principal_answer" in kinds


def test_stop_cost_limit_over_http(api_client, store):
    # §3.3・§3.4(台帳 X-52): control の action に stop_cost_limit が通り、双方に「なし」だけの最終記録が 1 件残る。
    from vault_helpers import demo_create_request, put_candidate_and_employer_templates

    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    created = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )

    response = api_client.post(
        f"/v1/negotiations/{created.nid}/control", json={"side": "candidate", "action": "stop_cost_limit"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "judged"
    for side in ("candidate", "employer"):
        events = api_client.get(f"/v1/negotiations/{created.nid}/events", params={"side": side}).json()
        assert [(e["kind"], e["result"]) for e in events] == [("final_result", {"likelihood": "none", "package": None})]


def test_negotiation_by_request_over_http(api_client, store):
    # §3.3(台帳 X-57): 作成の冪等キーから nid を引ける。未知のキーは 404。
    from urllib.parse import quote

    from vault_helpers import demo_create_request, put_candidate_and_employer_templates

    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    # web が付ける request_id は「依頼者 ID:画面の値」の形。画面の値に "/" が入っても、別のパスとして 404 にならずに引ける。
    request_id = f"{new_id('principal')}:request/0001"
    created = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id, request_id=request_id)
    )

    for in_path in (request_id, quote(request_id, safe="")):  # そのままの形と、パーセントエンコードした形
        response = api_client.get(f"/v1/negotiations/by-request/{in_path}")
        assert (response.status_code, response.json()) == (200, {"nid": created.nid})

    assert api_client.get("/v1/negotiations/by-request/never-created").status_code == 404
    # request_id が "view"・"events" でも、{nid}/view・{nid}/events と取り違えない(取り違えると、side がなくて 422 になる)。
    for word in ("view", "events"):
        assert api_client.get(f"/v1/negotiations/by-request/{word}").status_code == 404


def test_healthz_over_http_does_not_touch_the_store():
    # AC-22: GET /health は、認証なしで 200 {"status":"ok"}。ストレージ(Firestore)には触れない(死活確認が、ストレージの状態に左右されない)。
    from fastapi.testclient import TestClient

    from vault.app import create_app

    class UntouchableStore:
        def __getattr__(self, name):
            raise AssertionError(f"/health touched the store ({name})")

    client = TestClient(create_app(UntouchableStore()))

    response = client.get("/health")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert client.post("/health").status_code == 405  # GET だけ


def test_healthz_is_open_while_every_other_route_needs_authentication(store):
    # TEE 版(caller_verifier あり)でも、/health は認証なしで通る(attestation と同じく、依存の外の素の経路)。ほかの経路は 401 のまま。
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from vault.app import create_app

    def deny_everyone() -> None:
        raise HTTPException(status_code=401, detail="unauthenticated")

    client = TestClient(create_app(store, caller_verifier=deny_everyone))

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/v1/negotiations").status_code == 401
    assert client.get(f"/v1/principals/{new_id('nobody')}/policy").status_code == 401
