"""DV-01: 権限と独自ヘッダ(design.md §6.3・§12.2)。

本人のデータと交渉への操作は、すべて「セッションの依頼者 ID が、その対象の当事者であること」を確かめる
(違えば 403、セッションがなければ 401)。状態を変えるリクエストは POST に限り、X-Requested-With を必須にする。
デモ用のエンドポイントは、本物の依頼者には触れない(web と vault の両方で確かめる)。依頼者 ID は開始ページの
GET でしか発行されない。

デモ用の読み出しは、web の段の状態(stages。補助)と金庫のデモ用の口(正本。台帳 X-38)の両方で確かめる。
段の状態が欠けている・壊れている本物の交渉も、読めない(段の状態だけに頼ると、欠けを「架空」と読んで、本物の
依頼者の側の見え方を返してしまう)。

「メーターの区間の API は、本物の利用者の交渉の ID を拒否する」は ③(推定区間メーター)なので、ここでは確かめない。

段階開示の経路(④。段の状態・「会う」・「承認」・開示台帳)も同じ権限で確かめる(末尾の試験)。デモ用の段の状態(/v1/demo/negotiations/{nid}/stage)
も、本物の交渉には触れない(金庫のデモ用の口が正本、web の段の状態が補助)。

金庫は本物の vault の app を ASGI のままつなぎ、web の app へは Browser(クッキーを持つ httpx のクライアント)
から入る。負の確認(403・401)だけだと、何でも断る実装でも通ってしまうので、本人の操作が通ることも確かめる。
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from vault.api_models import CandidateParticipantRequest, CreateNegotiationRequest, EmployerParticipantRequest, MoveRequest
from test_stages import agree
from vault_helpers import new_id, put_candidate_and_employer_templates, sample_package
from web.session import SESSION_COOKIE_NAME
from web.vault_client import VaultNotFoundError, VaultUnavailableError
from web_app_helpers import submit_interview
from web_helpers import create_demo_negotiation

_HOUR = dt.timedelta(hours=1)


def _answer_body() -> dict:
    return {"package": sample_package().model_dump(), "answer": "accept"}


async def _two_principals(web_app):
    """本人 A と他人 B(それぞれ別のブラウザ)。B は交渉を 1 件持つ。"""
    browser_a, browser_b = web_app.browser(), web_app.browser()
    pid_a = await browser_a.register()
    pid_b = await browser_b.register()
    template_id = web_app.put_employer_template()
    nid_b = await browser_b.create_negotiation(pid_b, template_id)
    return browser_a, pid_a, browser_b, pid_b, nid_b, template_id


def _principal_state(web_app, pid: str, nid: str | None = None) -> dict:
    """pid の、金庫と (default) の状態(他人の操作で変わっていないことの確認用)。"""
    principal_doc = web_app.store._principal_ref(pid).get().to_dict()
    meta_doc = web_app.default_db.collection("principals_meta").document(pid).get().to_dict()
    state = {
        "policy": principal_doc.get("policy"),
        "blocklist": principal_doc.get("blocklist", []),
        "meta_state": meta_doc["deletion_state"] if meta_doc else None,
        "negotiations": sorted(s.nid for s in web_app.store.list_principal_negotiations(pid)),
    }
    if nid is not None:
        view = web_app.store.get_view(nid, "candidate")
        state["negotiation"] = (view.status, view.paused)
    return state


@pytest.mark.anyio
async def test_other_principals_id_is_forbidden_on_every_principal_route_and_changes_nothing(web_app):
    # DV-01: 他人の依頼者 ID を指定した閲覧・操作がすべて 403(セッションの依頼者 ID と URL の依頼者 ID が違う)。
    # 他人の面談の送信・ブロックリスト・交渉の作成・データの削除が、実際に何も変えないことも確かめる。
    browser_a, pid_a, _, pid_b, nid_b, template_id = await _two_principals(web_app)
    before = _principal_state(web_app, pid_b, nid_b)

    routes = [
        ("GET", f"/v1/principals/{pid_b}/policy", None),
        ("POST", f"/v1/principals/{pid_b}/interview/submit", None),  # 面談の送信は、面談の API の /submit だけ(台帳 X-81)
        ("POST", f"/v1/principals/{pid_b}/blocklist", {"blocklist": ["company-x"]}),
        ("GET", f"/v1/principals/{pid_b}/negotiations", None),
        ("POST", f"/v1/principals/{pid_b}/negotiations", {"request_id": "request-0002", "employer_template_id": template_id}),
        ("POST", f"/v1/principals/{pid_b}/delete", None),
    ]
    for method, path, body in routes:
        response = await (browser_a.get(path) if method == "GET" else browser_a.post(path, body))
        assert response.status_code == 403, (method, path, response.text)
        assert response.json() == {"detail": "forbidden"}

    assert _principal_state(web_app, pid_b, nid_b) == before  # 他人のデータは、何も変わっていない
    assert web_app.store._principal_ref(pid_b).get().exists  # 消されてもいない


@pytest.mark.anyio
async def test_own_principal_routes_are_allowed(web_app):
    # DV-01(負の確認だけで通らないための、本人の操作の確認): 自分の依頼者 ID なら、閲覧・操作が通る。
    browser = web_app.browser()
    pid = await browser.register()
    template_id = web_app.put_employer_template()

    policy = await browser.get(f"/v1/principals/{pid}/policy")
    assert policy.status_code == 200
    assert policy.json()["policy"]["side"] == "candidate"
    assert (await browser.post(f"/v1/principals/{pid}/blocklist", {"blocklist": ["company-x"]})).status_code == 200
    assert (await browser.get(f"/v1/principals/{pid}/negotiations")).json() == []
    created = await browser.post(
        f"/v1/principals/{pid}/negotiations", {"request_id": "request-0001", "employer_template_id": template_id}
    )
    assert created.status_code == 200
    assert len((await browser.get(f"/v1/principals/{pid}/negotiations")).json()) == 1


@pytest.mark.anyio
async def test_other_principals_negotiation_id_is_forbidden_and_changes_nothing(web_app):
    # DV-01: 他人の交渉 ID(と、存在しない・デモの・形の違う交渉 ID)を指定した閲覧・操作がすべて 403。
    # 他人の交渉を一時停止・取消・回答できないことも確かめる。
    browser_a, pid_a, _, pid_b, nid_b, _ = await _two_principals(web_app)
    demo_nid = create_demo_negotiation(web_app.store)
    before = _principal_state(web_app, pid_b, nid_b)

    for nid in (nid_b, demo_nid, "0123456789abcdef", "not-a-negotiation-id"):
        responses = [
            await browser_a.get(f"/v1/negotiations/{nid}/events"),
            await browser_a.post(f"/v1/negotiations/{nid}/principal-answer", _answer_body()),
            await browser_a.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"}),
            await browser_a.post(f"/v1/negotiations/{nid}/control", {"action": "pause"}),
        ]
        assert [r.status_code for r in responses] == [403, 403, 403, 403], (nid, [r.text for r in responses])

    assert _principal_state(web_app, pid_b, nid_b) == before  # B の交渉は、進行中のまま(取消・一時停止されていない)
    assert before["negotiation"] == ("active", False)


@pytest.mark.anyio
async def test_own_negotiation_routes_are_allowed(web_app):
    # DV-01(本人の操作の確認): 自分の交渉なら、活動ログの閲覧・一時停止・再開・取消が通り、
    # 途中確認のない交渉への回答は「合う質問がない」(409)で、権限の 403 ではない。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())

    assert (await browser.get(f"/v1/negotiations/{nid}/events")).status_code == 200
    paused = await browser.post(f"/v1/negotiations/{nid}/control", {"action": "pause"})
    assert paused.json() == {"status": "active", "paused": True}
    resumed = await browser.post(f"/v1/negotiations/{nid}/control", {"action": "resume"})
    assert resumed.json() == {"status": "active", "paused": False}
    answer = await browser.post(f"/v1/negotiations/{nid}/principal-answer", _answer_body())
    assert (answer.status_code, answer.json()) == (409, {"detail": "no_matching_question"})
    cancelled = await browser.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"})
    assert cancelled.json() == {"status": "judged", "paused": False}
    assert (await browser.get(f"/v1/principals/{pid}/negotiations")).json()[0]["state"] == "ended"


@pytest.mark.anyio
async def test_state_changing_requests_without_x_requested_with_are_rejected_and_change_nothing(web_app):
    # DV-01: X-Requested-With のない状態変更(POST)は拒否する(クッキーがあっても、なくても)。何も変わらない。
    browser = web_app.browser()
    pid = await browser.register()
    template_id = web_app.put_employer_template()
    nid = await browser.create_negotiation(pid, template_id)
    demo_templates = put_candidate_and_employer_templates(web_app.store._db)
    before = _principal_state(web_app, pid, nid)
    negotiations_before = len(list(web_app.store._negotiations().stream()))

    posts = [
        (f"/v1/principals/{pid}/interview/submit", None),
        (f"/v1/principals/{pid}/blocklist", {"blocklist": ["company-x"]}),
        (f"/v1/principals/{pid}/negotiations", {"request_id": "request-0002", "employer_template_id": template_id}),
        (f"/v1/principals/{pid}/delete", None),
        (f"/v1/negotiations/{nid}/principal-answer", _answer_body()),
        (f"/v1/negotiations/{nid}/control", {"action": "cancel"}),
        (
            "/v1/demo/negotiations",
            {
                "request_id": "request-demo1",
                "candidate_template_id": demo_templates[0].template_id,
                "employer_template_id": demo_templates[1].template_id,
            },
        ),
        ("/no/such/route", None),
    ]
    stranger = web_app.browser()  # クッキーのないブラウザでも、同じ
    for path, body in posts:
        for who in (browser, stranger):
            response = await who.post(path, body, requested_with=False)
            assert response.status_code == 403, (path, response.text)
            assert response.json() == {"detail": "missing_requested_with_header"}
            assert "set-cookie" not in response.headers

    assert _principal_state(web_app, pid, nid) == before
    assert len(list(web_app.store._negotiations().stream())) == negotiations_before  # デモの交渉も作られていない
    # 状態を変える操作は POST だけ(PUT・DELETE・PATCH のルートはない)。
    for method in ("PUT", "DELETE", "PATCH"):
        response = await browser.client.request(
            method, f"/v1/principals/{pid}/delete", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        assert response.status_code == 405
    assert web_app.store._principal_ref(pid).get().exists
    # 値が空のヘッダは、ないのと同じ(付けたことにならない)。
    empty = await browser.client.post(
        f"/v1/principals/{pid}/blocklist", json={"blocklist": []}, headers={"X-Requested-With": ""}
    )
    assert empty.status_code == 403
    # 本人の操作は、ヘッダがあれば通る(何でも断っているのではない)。
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "pause"})).status_code == 200


@pytest.mark.anyio
async def test_demo_endpoints_cannot_touch_real_principals(web_app):
    # DV-01: デモ用エンドポイントから本物の依頼者に触れられない(web の側の確認)。
    browser, pid, _, pid_b, nid_b, _ = await _two_principals(web_app)
    demo_nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()  # 架空の候補者の交渉にも、段の状態を作る(§6.2)
    b_before = _principal_state(web_app, pid_b, nid_b)
    demo = web_app.browser()  # デモは、クッキーのない訪問者でも読める(セッションを見ない)

    # 読めるのは、候補者が架空人物と分かっている交渉だけ。本物の依頼者の交渉・存在しない交渉・形の違う値は 403。
    assert (await demo.get(f"/v1/demo/negotiations/{demo_nid}/events", side="candidate")).status_code == 200
    assert (await demo.get(f"/v1/demo/negotiations/{demo_nid}/events", side="employer")).status_code == 200
    for nid in (nid_b, "0123456789abcdef", "not-a-negotiation-id"):
        for side in ("candidate", "employer"):
            response = await demo.get(f"/v1/demo/negotiations/{nid}/events", side=side)
            assert response.status_code == 403, (nid, side, response.text)
    # 本人のクッキーがあっても、デモの入口は、本物の依頼者の交渉を読ませない。
    assert (await browser.get(f"/v1/demo/negotiations/{nid_b}/events", side="candidate")).status_code == 403

    # デモの交渉の作成は、依頼者・モード・本物かどうかを、リクエストで指定できない。
    candidate_template, employer_template = put_candidate_and_employer_templates(web_app.store._db)
    base = {
        "request_id": "request-demo1",
        "candidate_template_id": candidate_template.template_id,
        "employer_template_id": employer_template.template_id,
    }
    negotiations_before = len(list(web_app.store._negotiations().stream()))
    for extra in ({"principal_id": pid_b}, {"mode": "live"}, {"is_fictional": False}, {"mode": "attack"}):
        response = await demo.post("/v1/demo/negotiations", {**base, **extra})
        assert response.status_code == 422, (extra, response.text)
    # 本物の依頼者の ID を、テンプレートの ID として渡しても、金庫はテンプレートしか引かないので見つからない。
    response = await demo.post("/v1/demo/negotiations", {**base, "candidate_template_id": pid_b})
    assert response.status_code == 404
    assert len(list(web_app.store._negotiations().stream())) == negotiations_before
    assert _principal_state(web_app, pid_b, nid_b) == b_before

    # 正しい入力なら、テンプレートからの交渉が作られる(モードは demo。候補者は架空人物)。
    created = await demo.post("/v1/demo/negotiations", base)
    assert created.status_code == 200
    document = web_app.store._negotiation_ref(created.json()["nid"]).get().to_dict()
    assert document["mode"] == "demo"
    assert document["participants"]["candidate"]["is_fictional"] is True
    assert document["participants"]["candidate"]["principal_id"] is None


def _record_demo_reads(web_app, monkeypatch) -> list[tuple]:
    """web が金庫のデモ用の読み出しを呼ぶたびに、(交渉 ID, side, 結果または例外)を記録する(呼び出しそのものは、そのまま通す)。"""
    calls: list[tuple] = []
    original = web_app.vault.get_demo_events

    async def recording(nid, side, after_seq=0):
        try:
            result = await original(nid, side, after_seq)
        except Exception as exc:
            calls.append((nid, side, type(exc).__name__))
            raise
        calls.append((nid, side, "ok"))
        return result

    monkeypatch.setattr(web_app.vault, "get_demo_events", recording)
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["demo", "attack"])
async def test_the_demo_read_is_served_by_the_vaults_demo_endpoint(web_app, monkeypatch, mode):
    # X-38: デモ用の読み出しは、金庫のデモ用の口(GET /v1/demo/negotiations/{nid}/events)から返す(通常の events の口ではない)。
    # 応答は、通常の events と同じ(両側。after_seq も同じ)。
    nid = create_demo_negotiation(web_app.store, mode=mode)
    web_app.store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )
    await web_app.services.sweeper.sweep_once()  # 架空の候補者の交渉にも、段の状態を作る(§6.2)
    calls = _record_demo_reads(web_app, monkeypatch)
    demo = web_app.browser()

    for side in ("candidate", "employer"):
        response = await demo.get(f"/v1/demo/negotiations/{nid}/events", side=side)
        assert response.status_code == 200
        assert response.json() == [event.model_dump(mode="json") for event in web_app.store.get_events(nid, side)]
    after = await demo.get(f"/v1/demo/negotiations/{nid}/events", side="candidate", after_seq=1)
    assert after.json() == []  # after_seq も、金庫のデモ用の口に渡っている(check は seq 1)

    assert calls == [(nid, "candidate", "ok"), (nid, "employer", "ok"), (nid, "candidate", "ok")]


_STAGE_DOCUMENTS = {
    "document_missing": None,  # 段の状態がない(まだ作っていない・消えた)
    "field_missing": {"nid": "{nid}", "stage": 0},  # candidate_principal_id の項目が欠けている(移行・障害復旧・古い文書)
    "field_none_looks_fictional": {"nid": "{nid}", "candidate_principal_id": None, "stage": 0},  # 壊れて、架空と読める
    "someone_elses_principal": {"nid": "{nid}", "candidate_principal_id": "0123456789abcdef", "stage": 0},
    "wrong_type": {"nid": "{nid}", "candidate_principal_id": 12345, "stage": 0},
}


@pytest.mark.anyio
@pytest.mark.parametrize("variant", list(_STAGE_DOCUMENTS))
async def test_a_real_negotiation_cannot_be_read_from_the_demo_endpoint_whatever_its_stage_document_says(
    web_app, monkeypatch, variant
):
    # X-38 / DV-01: 本物の交渉の stages/{nid} が、欠けている・壊れている(項目がない・架空と読める・型が違う)ときも、
    # 未認証のデモの口から、本物の依頼者の側の見え方を読めない(403)。web の段の状態だけに頼ると、欠けや壊れた文書で、
    # 本物の交渉が「架空」と読まれて、金庫から返ってきてしまう。金庫が自分の文書(mode と、候補者が架空人物か)で断る。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    web_app.store.process_move(  # 本物の依頼者の側に、読まれてはならない記録(確認手。評価つき)を作る
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )
    stage_ref = web_app.default_db.collection("stages").document(nid)
    document = _STAGE_DOCUMENTS[variant]
    if document is None:
        stage_ref.delete()
    else:
        stage_ref.set({key: nid if value == "{nid}" else value for key, value in document.items()})
    calls = _record_demo_reads(web_app, monkeypatch)
    demo = web_app.browser()  # クッキーのない訪問者

    for side in ("candidate", "employer"):
        response = await demo.get(f"/v1/demo/negotiations/{nid}/events", side=side)
        assert response.status_code == 403, (variant, side, response.text)
        assert response.json() == {"detail": "forbidden"}

    # 対照: 本人は、同じ交渉の自分の側のイベントを、通常の口から読める(読まれてはならない記録は、実際にある)。
    own = await browser.get(f"/v1/negotiations/{nid}/events")
    assert [event["kind"] for event in own.json()] == ["check"]
    if variant == "field_none_looks_fictional":
        # web の確認(補助)は通るので、止めたのは金庫(正本): デモ用の口が、本物の交渉を 404 で断り、web が 403 に写した。
        assert calls == [(nid, "candidate", "VaultNotFoundError"), (nid, "employer", "VaultNotFoundError")]
    else:
        assert calls == []  # web の確認(補助)が先に断った。金庫のデモ用の口は呼んでいない


@pytest.mark.anyio
async def test_a_stage_document_without_the_candidate_principal_id_is_not_read_as_fictional(web_app):
    # X-38: 段の状態の判定(補助)。candidate_principal_id が null(架空の候補者)のときだけ「架空」。項目が欠けた文書は、
    # 「架空」と読まない(以前は .get(...) is None で、欠けも「架空」になっていた)。ない文書・本物・交渉 ID の形でない値も False。
    stages = web_app.services.stages
    collection = web_app.default_db.collection("stages")
    nids = {name: f"{index:016x}" for index, name in enumerate(["fictional", "missing_field", "real", "absent"], start=1)}
    collection.document(nids["fictional"]).set({"nid": nids["fictional"], "candidate_principal_id": None, "stage": 0})
    collection.document(nids["missing_field"]).set({"nid": nids["missing_field"], "stage": 0})
    collection.document(nids["real"]).set({"nid": nids["real"], "candidate_principal_id": "0123456789abcdef", "stage": 0})

    verdicts = {name: await stages.is_fictional_negotiation(nid) for name, nid in nids.items()}

    assert verdicts == {"fictional": True, "missing_field": False, "real": False, "absent": False}
    assert await stages.is_fictional_negotiation("not-a-negotiation-id") is False
    assert await stages.is_fictional_negotiation("../stages") is False


@pytest.mark.anyio
async def test_a_vault_404_on_the_demo_read_is_mapped_to_403_without_the_vaults_detail(web_app, monkeypatch):
    # X-38: 金庫のデモ用の口が 404(本物の交渉・存在しない交渉)を返したら、web はこれまでどおり 403 に写す。金庫の detail は返さない。
    # 金庫が応えないとき(503)は、これまでどおり 503(あとで呼び直してよい)。
    nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()
    demo = web_app.browser()

    async def not_found(nid, side, after_seq=0):
        raise VaultNotFoundError(f"vault returned 404: {nid}", 404)

    monkeypatch.setattr(web_app.vault, "get_demo_events", not_found)
    refused = await demo.get(f"/v1/demo/negotiations/{nid}/events", side="candidate")
    assert (refused.status_code, refused.json()) == (403, {"detail": "forbidden"})
    assert nid not in refused.text

    async def unavailable(nid, side, after_seq=0):
        raise VaultUnavailableError("vault says: secret detail", 503)

    monkeypatch.setattr(web_app.vault, "get_demo_events", unavailable)
    down = await demo.get(f"/v1/demo/negotiations/{nid}/events", side="candidate")
    assert (down.status_code, down.json()) == (503, {"detail": "temporarily_unavailable"})
    assert "secret detail" not in down.text


def test_the_vault_also_refuses_demo_or_attack_negotiations_that_involve_a_real_principal(api_client):
    # DV-01: デモ用エンドポイントは本物の依頼者に触れない(vault の側の確認)。金庫の作成の検証が、
    # demo・attack のモードに本物の依頼者を指定することも、live のモードに架空人物を指定することも拒否する。
    pid = new_id("principal")
    template = "template-1"
    for mode in ("demo", "attack"):
        with pytest.raises(ValidationError):
            CreateNegotiationRequest(
                request_id="request-x",
                mode=mode,
                candidate=CandidateParticipantRequest(is_fictional=False, principal_id=pid),
                employer=EmployerParticipantRequest(template_id=template),
            )
        response = api_client.post(
            "/v1/negotiations",
            json={
                "request_id": "request-x",
                "mode": mode,
                "candidate": {"is_fictional": False, "principal_id": pid},
                "employer": {"template_id": template},
            },
        )
        assert response.status_code == 422
    with pytest.raises(ValidationError):
        CreateNegotiationRequest(
            request_id="request-x",
            mode="live",
            candidate=CandidateParticipantRequest(is_fictional=True, template_id=template),
            employer=EmployerParticipantRequest(template_id=template),
        )
    # 本物の依頼者の ID を、テンプレートとして指定しても、テンプレートの置き場には依頼者がいないので見つからない。
    response = api_client.post(
        "/v1/negotiations",
        json={
            "request_id": "request-y",
            "mode": "demo",
            "candidate": {"is_fictional": True, "template_id": pid},
            "employer": {"template_id": template},
        },
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_a_real_principals_events_and_policy_are_readable_only_as_the_principals_own_side(web_app):
    # DV-01: 本物の利用者の交渉では、ポリシーとイベント列は本人の側としてしか読めない(相手の側は指定できない)。
    browser, pid, _, pid_b, nid_b, template_id = await _two_principals(web_app)
    nid = await browser.create_negotiation(pid, template_id)
    store = web_app.store
    package = sample_package()
    check = store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package))
    store.process_move(
        nid, MoveRequest(expected_version=check.version, side="candidate", move="propose", package=package)
    )
    candidate_kinds = [e.kind for e in store.get_events(nid, "candidate")]
    employer_kinds = [e.kind for e in store.get_events(nid, "employer")]
    assert candidate_kinds != employer_kinds  # 側によって見え方が違う(この確認の前提)

    plain = await browser.get(f"/v1/negotiations/{nid}/events")
    asked_for_the_other_side = await browser.get(f"/v1/negotiations/{nid}/events", side="employer")

    assert [e["kind"] for e in plain.json()] == candidate_kinds
    assert [e["kind"] for e in asked_for_the_other_side.json()] == candidate_kinds  # side を指定しても、本人の側のまま
    # 他人のポリシー・イベント列は、そもそも読めない。
    assert (await browser.get(f"/v1/principals/{pid_b}/policy")).status_code == 403
    assert (await browser.get(f"/v1/negotiations/{nid_b}/events")).status_code == 403
    # 自分のポリシーは、金庫が持つ丸め済みポリシーがそのまま読める(web は保存しない)。
    own_policy = (await browser.get(f"/v1/principals/{pid}/policy")).json()
    assert own_policy["policy"]["accept_anchors"][0]["salary"] == 650  # 620 万が、良い側の 650 に丸められている


@pytest.mark.anyio
async def test_the_principal_negotiation_list_hides_the_end_reason_counters_and_version(web_app):
    # DV-01: 本人向けの交渉一覧に、終了理由・相手の回数・version が現れない(§3.3: 返す項目は限られる)。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    await browser.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"})

    response = await browser.get(f"/v1/principals/{pid}/negotiations")
    events = await browser.get(f"/v1/negotiations/{nid}/events")

    (item,) = response.json()
    assert set(item) == {"nid", "job_id", "created_at", "state", "result"}
    assert item["state"] == "ended"
    assert item["result"] == {"likelihood": "none", "package": None}
    for text in (response.text, events.text):
        for hidden in ("end_reason", "cancelled", "version", "counters", "moves_used", "evaluations_used", "remaining"):
            assert hidden not in text, hidden


@pytest.mark.anyio
async def test_the_principal_id_is_issued_only_by_the_start_page_get_and_never_overwritten_by_a_post(web_app):
    # DV-01: 依頼者 ID は開始ページの GET でしか発行されず、POST の応答で上書きされない。
    stranger = web_app.browser()
    pid_x = "0123456789abcdef"
    posts = [
        (f"/v1/principals/{pid_x}/interview/begin", None, 401),
        (f"/v1/principals/{pid_x}/blocklist", {"blocklist": []}, 401),
        (f"/v1/principals/{pid_x}/negotiations", {"request_id": "request-0001", "employer_template_id": "t"}, 401),
        (f"/v1/principals/{pid_x}/delete", None, 401),
        (f"/v1/negotiations/{pid_x}/control", {"action": "cancel"}, 401),
        (f"/v1/negotiations/{pid_x}/principal-answer", _answer_body(), 401),
        ("/no/such/route", None, 404),  # ルートがなければ 404(セッションがあるかどうかは関係ない)
    ]
    # クッキーのない訪問者の POST・GET は、ID を発行しない(セッションがなければ 401)。クッキーも付かない。
    for path, body, expected_status in posts:
        response = await stranger.post(path, body)
        assert response.status_code == expected_status, (path, response.text)
        assert "set-cookie" not in response.headers
    for path in (f"/v1/principals/{pid_x}/policy", f"/v1/principals/{pid_x}/negotiations", f"/v1/negotiations/{pid_x}/events"):
        response = await stranger.get(path)
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
    assert stranger.cookie is None

    # 開始ページの GET で、初めて発行される。別のブラウザには、別の ID が発行される。
    pid_a = await stranger.open_start_page()
    other = web_app.browser()
    pid_b = await other.open_start_page()
    assert pid_a != pid_b

    # 有効なクッキーを持つ POST の応答は、クッキーを上書きしない(付いても、同じ依頼者 ID の期限の延長だけ)。
    begun = await stranger.post(f"/v1/principals/{pid_a}/interview/begin")
    assert begun.status_code == 200
    for header in begun.headers.get_list("set-cookie"):
        assert SESSION_COOKIE_NAME in header
    assert stranger.pid == pid_a
    await submit_interview(web_app.services, pid_a)  # 面談を送った依頼者にする(利用記録を作る。ブロックリストの登録に要る)
    web_app.clock.advance(2 * _HOUR)  # 1 時間たてば、利用記録の更新と一緒に、クッキーの期限が延びる
    extended = await stranger.post(f"/v1/principals/{pid_a}/blocklist", {"blocklist": ["company-x"]})
    assert extended.status_code == 200
    assert len(extended.headers.get_list("set-cookie")) == 1
    assert stranger.pid == pid_a

    # 署名の合わない(偽の)クッキーを付けた POST は、無効なクッキーとして扱われ、ID は発行されない。
    forged = web_app.browser()
    forged.set_cookie("forged-cookie-value")
    response = await forged.post(f"/v1/principals/{pid_a}/blocklist", {"blocklist": []})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert forged.cookie == "forged-cookie-value"


@pytest.mark.anyio
async def test_reopening_the_start_page_with_a_valid_cookie_keeps_the_same_id(web_app):
    # DV-01: 有効なクッキーがあるまま開始ページを開き直しても、ID が変わらない(新しい ID を発行しない)。
    browser = web_app.browser()
    first = await browser.open_start_page()

    second = await browser.get("/start")
    assert second.status_code == 200
    assert "set-cookie" not in second.headers  # 発行し直さない
    assert browser.pid == first

    # 面談を送った依頼者は、1 時間後に開き直すと、期限だけが延びる(同じ ID のまま)。
    await submit_interview(web_app.services, first)
    web_app.clock.advance(2 * _HOUR)
    third = await browser.get("/start")
    assert browser.pid == first
    (cookie_header,) = third.headers.get_list("set-cookie")
    assert cookie_header.startswith(f"{SESSION_COOKIE_NAME}=")
    assert web_app.services.codec.read(browser.cookie, web_app.clock.now()) == first

    # 署名の合わないクッキーは、有効なクッキーではないので、新しい ID が発行される。
    tampered = web_app.browser()
    tampered.set_cookie("tampered")
    fresh = await tampered.open_start_page()
    assert fresh != first


@pytest.mark.anyio
async def test_demo_endpoints_do_not_use_or_extend_the_principal_session(web_app):
    # DV-01 / §6.3: デモ用のエンドポイントは、本物の依頼者のセッションを見ない・触れない。有効なクッキーがあっても、
    # 利用記録を更新せず、クッキーの期限も延ばさない(本物の依頼者に触れないことの一部)。
    browser = web_app.browser()
    pid = await browser.register()
    demo_nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()
    candidate_template, employer_template = put_candidate_and_employer_templates(web_app.store._db)
    before = web_app.default_db.collection("principals_meta").document(pid).get().to_dict()
    web_app.clock.advance(2 * _HOUR)  # 1 時間を過ぎているので、セッションを見るリクエストなら、利用記録を更新する

    read = await browser.get(f"/v1/demo/negotiations/{demo_nid}/events", side="candidate")
    created = await browser.post(
        "/v1/demo/negotiations",
        {
            "request_id": "request-demo1",
            "candidate_template_id": candidate_template.template_id,
            "employer_template_id": employer_template.template_id,
        },
    )

    assert (read.status_code, created.status_code) == (200, 200)
    assert "set-cookie" not in read.headers and "set-cookie" not in created.headers
    assert web_app.default_db.collection("principals_meta").document(pid).get().to_dict() == before


# ----------------------------------------------------------------------
# 段階開示の経路(④。design.md §6.2・§6.3): 段の状態・「会う」・「承認」・開示台帳。デモ用の段の状態は、本物の依頼者に触れない
# ----------------------------------------------------------------------

_MEET_BODY = dict(job_summary="Web サービスの運用と開発に約 8 年従事。")


async def _agreed_negotiation_of(web_app, browser, request_id: str = "request-0001") -> tuple[str, str]:
    """面談を送った依頼者が、交渉を作って合意で終わらせる。(依頼者 ID, 交渉 ID)。"""
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template(), request_id)
    agree(web_app.store, nid)
    return pid, nid


@pytest.mark.anyio
async def test_stage_routes_need_a_session_and_the_custom_header_and_the_demo_route_does_not(web_app):
    # DV-01: セッションがなければ 401(依頼者 ID は発行しない)。状態を変える POST は、独自ヘッダがなければ 403(クッキーにも触れない)。
    # デモ用の段の状態は、セッションを見ない(クッキーのない訪問者が、デモの交渉を見られる)。
    browser = web_app.browser()
    pid, nid = await _agreed_negotiation_of(web_app, browser)
    visitor = web_app.browser()  # クッキーのない訪問者
    demo_nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()  # 架空の候補者の交渉にも、段の状態を作る(§6.2)

    responses = [
        await visitor.get(f"/v1/negotiations/{nid}/stage"),
        await visitor.post(f"/v1/negotiations/{nid}/stage/meet", _MEET_BODY),
        await visitor.post(f"/v1/negotiations/{nid}/stage/approve"),
        await visitor.get(f"/v1/principals/{pid}/ledger"),
    ]
    assert [(r.status_code, r.json()) for r in responses] == [(401, dict(detail="no_session"))] * 4
    assert visitor.cookie is None  # ID を発行していない
    no_header = [
        await browser.post(f"/v1/negotiations/{nid}/stage/meet", _MEET_BODY, requested_with=False),
        await browser.post(f"/v1/negotiations/{nid}/stage/approve", requested_with=False),
    ]
    assert [r.status_code for r in no_header] == [403, 403]
    assert web_app.default_db.collection("stages").document(nid).get().to_dict()["meet"] == dict(candidate=False, employer=False)
    assert (await visitor.get(f"/v1/demo/negotiations/{demo_nid}/stage")).status_code == 200


@pytest.mark.anyio
async def test_other_principals_negotiation_and_ledger_are_forbidden_on_the_stage_routes_and_change_nothing(web_app):
    # DV-01: 他人の交渉 ID(と、存在しない・デモの・形の違う交渉 ID)を指定した、段の状態・「会う」・「承認」がすべて 403。他人の台帳も 403。
    # 他人の段の状態にも台帳にも、何も書かない。デモ用の口に本物の交渉 ID を渡しても、同じ。
    browser_a, browser_b = web_app.browser(), web_app.browser()
    pid_a, _ = await _agreed_negotiation_of(web_app, browser_a)
    pid_b, nid_b = await _agreed_negotiation_of(web_app, browser_b, "request-0002")
    demo_nid = create_demo_negotiation(web_app.store)
    stages = web_app.default_db.collection("stages")
    assert (await browser_b.get(f"/v1/negotiations/{nid_b}/stage")).status_code == 200  # 本人なら通る(読み出しだけ。段の状態は書かない)
    before = (stages.document(nid_b).get().to_dict(), len(list(web_app.default_db.collection("principals").document(pid_b).collection("ledger").stream())))

    for nid in (nid_b, demo_nid, "0123456789abcdef", "not-a-negotiation-id"):
        responses = [
            await browser_a.get(f"/v1/negotiations/{nid}/stage"),
            await browser_a.post(f"/v1/negotiations/{nid}/stage/meet", _MEET_BODY),
            await browser_a.post(f"/v1/negotiations/{nid}/stage/approve"),
        ]
        assert [(r.status_code, r.json()) for r in responses] == [(403, dict(detail="forbidden"))] * 3, nid
    assert (await browser_a.get(f"/v1/principals/{pid_b}/ledger")).status_code == 403
    assert (await browser_a.get("/v1/principals/not-a-principal-id/ledger")).status_code == 403
    assert (await browser_a.get(f"/v1/demo/negotiations/{nid_b}/stage")).status_code == 403  # デモ用の口は、本物の交渉に触れない

    after = (stages.document(nid_b).get().to_dict(), len(list(web_app.default_db.collection("principals").document(pid_b).collection("ledger").stream())))
    assert after == before
    assert (await browser_a.get(f"/v1/principals/{pid_a}/ledger")).status_code == 200  # 自分の台帳は読める(負の確認だけで通らないように)


@pytest.mark.anyio
async def test_own_stage_routes_are_allowed(web_app):
    # DV-01(本人の操作の確認): 自分の交渉なら、段の状態の閲覧・「会う」・「承認」が通る(権限の 403 ではない)。フィクスチャを持たない組み立てでは、
    # 求人側の自動応答がないので、段は 0 のまま。「承認」は段 1 が開いてからなので、409(権限の 403 ではない)。
    browser = web_app.browser()
    pid, nid = await _agreed_negotiation_of(web_app, browser)

    view = await browser.get(f"/v1/negotiations/{nid}/stage")
    met = await browser.post(f"/v1/negotiations/{nid}/stage/meet", _MEET_BODY)
    approved = await browser.post(f"/v1/negotiations/{nid}/stage/approve")

    assert (view.status_code, view.json()["stage"]) == (200, 0)
    assert (met.status_code, met.json()["meet"]) == (200, dict(candidate=True, employer=False))
    assert (approved.status_code, approved.json()) == (409, dict(detail="stage_not_open"))
    assert (await browser.get(f"/v1/principals/{pid}/ledger")).status_code == 200
