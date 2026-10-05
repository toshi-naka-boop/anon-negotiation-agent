"""web の画面 API(1d-2): 面談の送信(§5 の手順 9 の部分)・ブロックリスト・交渉の作成・途中確認の回答(design.md §3.3・§4.4・§6.1)。

権限・セッション・削除は tests/test_authz.py・test_session.py・test_principal_deletion.py・test_inactive_deletion.py で
確かめる。ここでは、API そのものの振る舞い(丸める場所・入力の検証・金庫への橋渡し)を確かめる。
金庫は本物の vault の app を ASGI のままつなぎ、web の app へは Browser(クッキーを持つ httpx のクライアント)から入る。

開始ページ(GET /start)の入口の枠(web.limits の session_start。台帳 C-66)も、ここで確かめる。

面談の送信のロジック(丸め → 利用記録 → 金庫の PUT policy)は、内部の関数 InterviewService.submit_policy で、公開面(HTTP)には出ていない
(台帳 X-81。本番の経路は面談の API の /submit で、画面の流れは tests/test_interview_api.py)。ここでは、その関数をテストの補助
(web_app_helpers.submit_interview)から直接呼んで、丸める場所・入力の検証・書き込みの順を確かめる。
"""

import datetime as dt
import json
import logging

import pytest
from attack_helpers import make_env, small_limits  # noqa: F401  (make_env はフィクスチャ。入口の枠を絞った web を作る)
from fastapi import HTTPException
from negotiation_core import AXES, Verdict, evaluate
from pydantic import ValidationError
from vault.api_models import MoveRequest
from vault_helpers import put_candidate_and_employer_templates, sample_package
from web.vault_client import VaultUnavailableError
from web_app_helpers import CANARY, dump_documents, interview_body, submit_interview, wait_until


def _numeric_values(policy_dict: dict) -> list[tuple[str, object]]:
    """ポリシーのすべてのアンカーの数値軸の値(丸め済みかどうかの確認用)。"""
    values = []
    for key in ("accept_anchors", "reject_anchors"):
        for anchor in policy_dict[key]:
            values.extend((axis, anchor[axis]) for axis in ("salary", "remote_days", "night_duty", "review_months"))
    return values


@pytest.mark.anyio
async def test_the_interview_submit_rounds_on_the_web_side_and_the_vault_only_receives_grid_values(web_app, monkeypatch):
    # §2.5・§5 の手順 9: 丸めは web で行う。受けるアンカーは依頼者にとって良い側(年収 620 万 → 650 万)、
    # 受けないアンカーは悪い側(年収 410 万 → 400 万)のグリッド点に寄せる。金庫には、丸め済みの値しか届かない。
    sent = []
    original = web_app.vault.put_policy

    async def recording_put_policy(principal_id, request):
        sent.append(request.model_dump_json())
        return await original(principal_id, request)

    monkeypatch.setattr(web_app.vault, "put_policy", recording_put_policy)
    browser = web_app.browser()
    pid = await browser.open_start_page()

    await submit_interview(web_app.services, pid)

    stored = (await browser.get(f"/v1/principals/{pid}/policy")).json()
    assert stored["policy"]["accept_anchors"][0]["salary"] == 650
    assert stored["policy"]["reject_anchors"][0]["salary"] == 400
    assert stored["attribute_bands"] == interview_body()["attribute_bands"]  # 台帳 I-2: 属性帯は金庫に保存する
    for axis, value in _numeric_values(stored["policy"]):
        assert value in AXES[axis].grid, (axis, value)
    (payload,) = sent
    assert "620" not in payload and "410" not in payload  # 生の値は、金庫に届いていない
    # 丸めても判定は変わらない(グリッド上の組み合わせについて。§2.5): 650 万以上の組み合わせは「受けられる」。
    policy = web_app.store.get_policy(pid).policy
    assert evaluate(policy, sample_package(salary=650, remote_days=2, night_duty=4, review_months=12)) is Verdict.ACCEPTABLE
    assert evaluate(policy, sample_package(salary=600)) is not Verdict.ACCEPTABLE


@pytest.mark.anyio
async def test_the_interview_submit_stores_the_removed_axes_and_replaces_the_policy_when_sent_again(web_app):
    # 外した軸(離散軸)は、丸め済みポリシーと一緒に金庫に保存される。送り直すと、置き換わる。
    browser = web_app.browser()
    pid = await browser.register(removed_axes=["night_duty", "training"])

    first = (await browser.get(f"/v1/principals/{pid}/policy")).json()
    assert first["removed_axes"] == ["night_duty", "training"]

    await submit_interview(web_app.services, pid, accept_anchors=[], reject_anchors=[])
    second = (await browser.get(f"/v1/principals/{pid}/policy")).json()
    assert (second["removed_axes"], second["policy"]["accept_anchors"]) == ([], [])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        {"accept_anchors": [{**interview_body()["accept_anchors"][0], "salary": 5000}]},  # 範囲外
        {"accept_anchors": [{**interview_body()["accept_anchors"][0], "salary": "620"}]},  # 数値でない
        {"accept_anchors": [{**interview_body()["accept_anchors"][0], "training": "sometimes"}]},  # 列挙外
        {"accept_anchors": [{**interview_body()["accept_anchors"][0], "note": CANARY}]},  # 未定義の項目
        {"removed_axes": ["salary"]},  # 年収は離散軸ではない(外せない)
        {"removed_axes": ["no_such_axis"]},
        {"removed_axes": ["night_duty", "night_duty"]},  # 重複
        {"attribute_bands": {"experience_band": "3_to_5y", "region_block": "kanto"}},  # 帯が足りない
        {"attribute_bands": {**interview_body()["attribute_bands"], "job_category": CANARY}},  # 列挙外
        {"principal_id": "0123456789abcdef"},  # 依頼者 ID は、リクエストでは指定できない
    ],
)
async def test_invalid_interview_submissions_are_rejected_by_the_model_before_anything_is_written(web_app, mutation):
    # §5 の手順 9: 範囲外・型違い・列挙外・未定義の項目・外せない軸・帯の欠け・依頼者 ID の指定は、送信のモデル(InterviewSubmitRequest)の検証で拒否する
    # (HTTP の口はない。台帳 X-81。入力の値を返さない 422 の応答は、面談の API の側の確認: tests/test_interview_api.py)。
    # 利用記録も作らず、金庫にも書かない。
    browser = web_app.browser()
    pid = await browser.open_start_page()

    with pytest.raises(ValidationError):
        await submit_interview(web_app.services, pid, **mutation)

    assert not web_app.default_db.collection("principals_meta").document(pid).get().exists
    assert not web_app.store._principal_ref(pid).get().exists


@pytest.mark.anyio
async def test_a_contradictory_interview_is_rejected_by_the_policy_check(web_app):
    # 受ける条件より全軸で良い組み合わせを「受けない」と言う矛盾は、書き込む前に拒否する(§2.2)。
    browser = web_app.browser()
    pid = await browser.open_start_page()
    better = {"salary": 1000, "remote_days": 5, "night_duty": 0, "review_months": 6, "training": "*", "side_job": "*", "start": "*"}

    with pytest.raises(HTTPException) as refused:
        await submit_interview(web_app.services, pid, reject_anchors=[better])

    assert (refused.value.status_code, refused.value.detail) == (422, "policy_invalid")
    assert not web_app.default_db.collection("principals_meta").document(pid).get().exists
    assert not web_app.store._principal_ref(pid).get().exists


@pytest.mark.anyio
async def test_the_blocklist_is_replaced_and_a_blocked_company_cannot_be_negotiated_with(web_app):
    # §5 の手順 8・§6.1・AC-16: ブロック先(企業 ID)を置き換える。ブロック先の求人とは、交渉が作られない。
    browser = web_app.browser()
    pid = await browser.register()
    company = "company-blocked-1"
    blocked_template = web_app.put_employer_template(company_id=company)

    assert (await browser.post(f"/v1/principals/{pid}/blocklist", {"blocklist": [company]})).status_code == 200
    refused = await browser.post(
        f"/v1/principals/{pid}/negotiations", {"request_id": "request-0001", "employer_template_id": blocked_template}
    )

    assert (refused.status_code, refused.json()) == (409, {"detail": "blocked"})
    assert web_app.store.list_principal_negotiations(pid) == []
    assert web_app.store._principal_ref(pid).get().to_dict()["blocklist"] == [company]
    # 置き換え: 空にすれば、作れる。
    assert (await browser.post(f"/v1/principals/{pid}/blocklist", {"blocklist": []})).status_code == 200
    assert (await browser.create_negotiation(pid, blocked_template)) is not None


@pytest.mark.anyio
@pytest.mark.parametrize("blocklist", [[""], ["x" * 101], ["company"] * 201, "company-x", [1]])
async def test_invalid_blocklists_are_rejected(web_app, blocklist):
    browser = web_app.browser()
    pid = await browser.register()

    response = await browser.post(f"/v1/principals/{pid}/blocklist", {"blocklist": blocklist})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_creating_a_negotiation_is_refused_with_the_vaults_reason_and_is_idempotent_per_principal(web_app):
    # §3.5・§6.1: 断られた理由は 409 の detail で返す(already_active)。同じ request_id は、同じ交渉を返し、
    # 二重には作らない。request_id は依頼者ごとの名前空間で、他人の request_id と重なっても、他人の交渉は返らない。
    browser, other_browser = web_app.browser(), web_app.browser()
    pid = await browser.register()
    other_pid = await other_browser.register()
    template_id = web_app.put_employer_template()
    second_template_id = web_app.put_employer_template()

    nid = await browser.create_negotiation(pid, template_id, "shared-request-id")
    again = await browser.create_negotiation(pid, template_id, "shared-request-id")
    others = await other_browser.create_negotiation(other_pid, template_id, "shared-request-id")  # 同じ request_id
    refused = await browser.post(
        f"/v1/principals/{pid}/negotiations", {"request_id": "request-0002", "employer_template_id": second_template_id}
    )

    assert again == nid  # 応答が失われて再送されても、二重に作らない
    assert others != nid  # 他人の交渉の ID は返らない
    assert (refused.status_code, refused.json()) == (409, {"detail": "already_active"})
    assert [s.nid for s in web_app.store.list_principal_negotiations(pid)] == [nid]
    assert web_app.store._principal_ref(pid).get().to_dict()["evaluation_budget"]["used"] == 17  # 予約は 1 回だけ(側ごとの評価の上限)
    unknown = await browser.post(
        f"/v1/principals/{pid}/negotiations", {"request_id": "request-0003", "employer_template_id": "no-such-template"}
    )
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "not_found"})  # 存在しない求人(テンプレート)


@pytest.mark.anyio
async def test_creating_a_negotiation_registers_the_stage_and_starts_the_referee_task(web_app):
    # §6.2・§4.1: 交渉の作成直後に、段の状態(段 0。依頼者 ID つき)を作り、レフェリーのタスクを動かす。
    web_app.enable_referees()
    browser = web_app.browser()
    pid = await browser.register()

    nid = await browser.create_negotiation(pid, web_app.put_employer_template())

    stage = web_app.default_db.collection("stages").document(nid).get().to_dict()
    assert (stage["stage"], stage["candidate_principal_id"]) == (0, pid)
    assert web_app.services.referees.is_running(nid)
    await wait_until(lambda: web_app.agents.calls_for("candidate"))  # レフェリーは、候補者側のエージェントを呼んだ(応答待ち)
    (call,) = web_app.agents.calls_for("candidate")
    assert call.nid == nid


@pytest.mark.anyio
async def test_a_principal_answers_the_question_and_the_answer_is_appended_to_the_body(web_app):
    # §4.4: 途中確認への回答。本人が見た質問(組み合わせ)への回答だけを受け付け、金庫の principal-answer に橋渡しする。
    # 回答は本体のポリシーにも追記され(本人の閲覧に出る)、活動ログに「回答」として残る。version は画面に出さない。
    browser = web_app.browser()
    pid = await browser.register(accept_anchors=[], reject_anchors=[])  # 何も決めていない → どの組み合わせも「本人確認が必要」
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    package = sample_package()
    asked = web_app.store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert asked.status == "awaiting_principal"

    other_question = sample_package(salary=800)
    wrong = await browser.post(
        f"/v1/negotiations/{nid}/principal-answer", {"package": other_question.model_dump(), "answer": "accept"}
    )
    assert (wrong.status_code, wrong.json()) == (409, {"detail": "no_matching_question"})  # 別の質問への回答は受けない

    answered = await browser.post(
        f"/v1/negotiations/{nid}/principal-answer", {"package": package.model_dump(), "answer": "accept"}
    )

    assert (answered.status_code, answered.json()) == (200, {"status": "active"})
    events = (await browser.get(f"/v1/negotiations/{nid}/events")).json()
    assert [e["kind"] for e in events] == ["ask_principal", "principal_answer"]
    assert events[-1]["answer"] == "accept" and events[-1]["own_evaluation"] == "acceptable"
    body = (await browser.get(f"/v1/principals/{pid}/policy")).json()["policy"]
    assert len(body["accept_anchors"]) == 1  # 本体にも追記された(§4.4)
    assert "version" not in json.dumps(events) + answered.text
    repeated = await browser.post(
        f"/v1/negotiations/{nid}/principal-answer", {"package": package.model_dump(), "answer": "accept"}
    )
    assert repeated.status_code == 409  # 同じ回答の再送は、二重には効かない


@pytest.mark.anyio
async def test_vault_failures_are_mapped_to_http_statuses_without_the_vaults_detail(web_app, monkeypatch):
    # 金庫の応答を HTTP に直す: 見つからない → 404、応えない → 503(あとで呼び直してよい)。金庫の detail は返さない。
    browser = web_app.browser()
    pid = await browser.open_start_page()
    await web_app.services.meta.create_if_absent(pid)  # 利用記録だけがあり、金庫には何もない依頼者

    not_found = await browser.get(f"/v1/principals/{pid}/policy")
    assert (not_found.status_code, not_found.json()) == (404, {"detail": "not_found"})

    async def unavailable(principal_id):
        raise VaultUnavailableError("vault says: secret detail", 503)

    monkeypatch.setattr(web_app.vault, "get_policy", unavailable)
    down = await browser.get(f"/v1/principals/{pid}/policy")
    assert (down.status_code, down.json()) == (503, {"detail": "temporarily_unavailable"})
    assert "secret detail" not in down.text


@pytest.mark.anyio
async def test_the_demo_endpoint_creates_a_demo_negotiation_from_templates_only(web_app):
    # §3.7・§6.3: デモの交渉は、テンプレートからのコピーだけで作る。同じ request_id は同じ交渉を返す。
    # 2 つのデモは、互いに影響しない(交渉ごとの別のコピー)。
    candidate_template, employer_template = put_candidate_and_employer_templates(web_app.store._db)
    browser = web_app.browser()
    body = {
        "request_id": "request-demo1",
        "candidate_template_id": candidate_template.template_id,
        "employer_template_id": employer_template.template_id,
    }

    first = await browser.post("/v1/demo/negotiations", body)
    replay = await browser.post("/v1/demo/negotiations", body)
    second = await browser.post("/v1/demo/negotiations", {**body, "request_id": "request-demo2"})

    assert first.json() == replay.json()
    assert second.json()["nid"] != first.json()["nid"]
    document = web_app.store._negotiation_ref(first.json()["nid"]).get().to_dict()
    assert document["mode"] == "demo"
    assert document["participants"]["employer"]["template_id"] == employer_template.template_id
    assert document["participants"]["candidate"]["template_id"] == candidate_template.template_id


@pytest.mark.anyio
async def test_interview_values_and_the_cookie_never_appear_in_the_logs(web_app, caplog):
    # §7: ログに、組み合わせの値・クッキー・依頼者の入力を書かない。面談の送信(成功と、拒否されるもの。内部の関数で行う)と、クッキーを付けた
    # 読み出しの間に出たログ(すべてのレベル)に、生の値(丸める前の 623.5 万・417.25 万)も、クッキーの値も、入力に混ぜた文字列もない。
    # (面談の API の送信の側は、tests/test_interview_api.py の AC-18 の試験。)
    caplog.set_level(logging.DEBUG)
    browser = web_app.browser()
    pid = await browser.open_start_page()
    raw_accept = {**interview_body()["accept_anchors"][0], "salary": 623.5}
    raw_reject = {**interview_body()["reject_anchors"][0], "salary": 417.25}

    await submit_interview(web_app.services, pid, accept_anchors=[raw_accept], reject_anchors=[raw_reject])
    with pytest.raises(ValidationError):
        await submit_interview(
            web_app.services, pid, accept_anchors=[{**raw_accept, "note": CANARY}], reject_anchors=[raw_reject]
        )
    read = await browser.get(f"/v1/principals/{pid}/policy")  # クッキーを付けたリクエスト(ミドルウェア・ルートのログを出させる)

    assert read.status_code == 200
    assert caplog.records  # ログは出ている(空だから通るのではない)
    for secret in ("623.5", "417.25", CANARY, browser.cookie):
        assert secret not in caplog.text, secret


@pytest.mark.anyio
async def test_healthz_answers_without_a_session_and_without_touching_the_usage_record(web_app, monkeypatch):
    # AC-22: GET /health は、認証なしで 200 {"status":"ok"}。セッションを見ない: ID を発行せず、有効なクッキーがあっても利用記録
    # (Firestore の principals_meta)に触れない(死活確認が、Firestore の状態に左右されない)。
    stranger = web_app.browser()
    anonymous = await stranger.get("/health")
    assert (anonymous.status_code, anonymous.json()) == (200, {"status": "ok"})
    assert "set-cookie" not in anonymous.headers and stranger.cookie is None

    browser = web_app.browser()
    pid = await browser.register()

    async def broken(principal_id):
        raise RuntimeError("principals_meta is down")

    monkeypatch.setattr(web_app.services.meta, "touch", broken)

    again = await browser.get("/health")

    assert (again.status_code, again.json()) == (200, {"status": "ok"})
    # 対照: セッションを見るほかの経路は、利用記録を確かめられないので通さない(差し替えが効いている)
    assert (await browser.get(f"/v1/principals/{pid}/policy")).status_code == 503


# --- 開始ページの入口の枠(web.limits の session_start。クライアント IP ごと。台帳 C-66) ---


async def start_from(browser, ip: str):
    """クライアント IP(X-Forwarded-For の末尾。web.client_ip)を指定した GET /start。"""
    return await browser.client.get("/start", headers={"X-Forwarded-For": ip})


@pytest.mark.anyio
async def test_the_start_page_is_limited_per_client_and_a_refusal_issues_no_identity(make_env):
    # クッキーを持たないクライアントが、GET /start で新しい依頼者 ID を作り続けるのを抑える(上限は 3 回に絞って確かめる)。超えたら 429 で、ID を発行しない。
    env = make_env(rate_limits=small_limits(session_start=3))
    visitors = [env.browser() for _ in range(5)]

    allowed = [await start_from(visitor, "198.51.100.1") for visitor in visitors[:3]]  # 上限ちょうどまで通る
    refused = await start_from(visitors[3], "198.51.100.1")

    assert [response.status_code for response in allowed] == [200, 200, 200] and all(visitor.pid for visitor in visitors[:3])
    assert refused.status_code == 429
    assert refused.json()["detail"] == {
        "code": "rate_limited",
        "entrance": "session_start",
        "scope": "client",
        "limit": 3,
        "window_seconds": 600,
        "retry_after_seconds": 600,
    }
    assert refused.headers["Retry-After"] == "600"
    assert "set-cookie" not in refused.headers and visitors[3].cookie is None  # 新しい依頼者 ID を発行していない
    other = await start_from(visitors[4], "198.51.100.2")  # 別のクライアントは、別の枠
    assert other.status_code == 200 and visitors[4].pid is not None
    forged = await start_from(visitors[3], "203.0.113.9, 198.51.100.1")  # 利用者が書ける先頭側を変えても、末尾が同じなら同じ枠
    assert forged.status_code == 429

    env.clock.advance(dt.timedelta(seconds=600))  # 次の窓
    again = await start_from(visitors[3], "198.51.100.1")
    assert again.status_code == 200 and visitors[3].pid is not None


@pytest.mark.anyio
async def test_the_start_page_counts_only_its_own_entrance_and_not_the_overall_allowance(web_app):
    # 台帳 L19-7: LLM を呼ばない入口なので、全体の枠(300)には数えない。数える文書は、入口 session_start のクライアントの文書 1 つだけ。
    browser = web_app.browser()
    for _ in range(3):
        assert (await start_from(browser, "198.51.100.1")).status_code == 200

    documents = dump_documents(web_app.default_db)
    counters = {path: data["count"] for path, data in documents.items() if path.startswith("rate_limits/")}
    assert [path.split("/")[1].split(".")[0] for path in counters] == ["session_start"]
    assert list(counters.values()) == [3]  # 有効なクッキーがあっても、開いたぶんだけ数える(開始ページを開き直すたびに 1 回)


@pytest.mark.anyio
async def test_the_default_limit_of_the_start_page_is_20_per_client_in_10_minutes(web_app):
    # 設定ファイルの値(session_start = 20)が、本番の組み立ての GET /start に効いている: 20 回目まで通り、21 回目は 429。
    browser = web_app.browser()

    statuses = [(await start_from(browser, "198.51.100.1")).status_code for _ in range(21)]

    assert statuses == [200] * 20 + [429]
