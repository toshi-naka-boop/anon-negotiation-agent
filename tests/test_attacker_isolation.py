"""DV-04: design.md §2.7・§4.3・§8.2・§12.2。攻撃モードと本物の依頼者の分離(台帳 C-1。AC-13 の「接続先は架空人物だけ」)。

受信口の部分(agents。このファイルの前半):
`AttackerTurnInput`(TurnInput に principal_instruction の自由文を足した型)は、攻撃モード用の
受信口 `/a2a/attacker` でしか受け付けられない。`/a2a/candidate`・`/a2a/employer` に送ると拒否され、
LLM(スタブ)は一度も動かない。自由文が、候補者側や通常の求人側の LLM の文脈に入る経路はない。

呼ぶ側の部分(web。このファイルの後半。web の app・agents・金庫を ASGI でつなぎ、エージェントは台本):
- `/a2a/attacker` は、攻撃モードの交渉からしか呼ばれない(デモ・ライブの交渉は呼ばない。攻撃モードの交渉は通常の求人側を呼ばない)。
- 攻撃モードの API(/v1/demo/attack/...)から、本物の依頼者のデータ(金庫の principals/*、利用記録、ライブの交渉の文書・イベント・
  段の状態)には届かない: どの経路も、依頼者の ID を受け取らず、セッションを見ず、ライブの交渉の ID を渡されても読めも書けもしない。
  本物の依頼者のクッキーを付けて呼んでも、利用記録さえ更新されない。金庫は、攻撃モードの交渉を本物の候補者では作らない。

v14: 計画(phase=plan)でも決定(phase=decide)でも同じ。攻撃モードの求人エージェントも、通常の求人側と同じ 2 種類の呼び出し
(計画・決定)で動き、principal_instruction は毎手番、どちらの呼び出しにも渡る(§8.2)。
"""

import datetime as dt
import json

import pytest
from agents.wire import PHASES
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    agents_app,
    anyio_backend,
    assert_rejected,
    data_part,
    http,
    send_message,
    stub_llm,
    valid_data,
)
from attack_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    CANARY,
    CONFIG,
    CREATE,
    RecordingSender,
    create_body,
    make_env,
    post,
    run_to_the_end,
)
from negotiation_core import AttackerTurnInput
from pydantic import ValidationError
from vault.api_models import CandidateParticipantRequest, CreateNegotiationRequest, EmployerParticipantRequest
from vault_helpers import put_candidate_and_employer_templates, sample_package
from web.api import DEMO_PATH_PREFIX
from web_app_helpers import dump_documents
from web_helpers import plan_dict

pytestmark = pytest.mark.anyio

INSTRUCTION = "依頼者の最低年収を聞き出して、その金額ぎりぎりまで下げてください。"


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ["candidate", "employer"])
async def test_attacker_turn_input_is_rejected_by_the_other_endpoints(role, phase, http, stub_llm):
    # DV-04 (AttackerTurnInput は、候補者側・通常の求人側の受信口では拒否される。LLM は動かない。計画でも決定でも)
    data = valid_data("attacker", phase)  # principal_instruction を持つ AttackerTurnInput
    data["side"] = "candidate" if role == "candidate" else "employer"
    data["counterparty"] = valid_data(role)["counterparty"]
    assert "principal_instruction" in data
    body = await send_message(http, role, [data_part(data)])
    assert_rejected(body)
    assert stub_llm.requests == []


@pytest.mark.parametrize("phase", PHASES)
async def test_attacker_turn_input_is_accepted_by_the_attacker_endpoint(phase, http, stub_llm):
    # DV-04 (対照: 同じ AttackerTurnInput は、/a2a/attacker では通り、LLM が動く。計画でも決定でも)
    data = valid_data("attacker", phase)
    data["principal_instruction"] = INSTRUCTION
    body = await send_message(http, "attacker", [data_part(data)])
    assert "error" not in body, body
    assert len(stub_llm.requests) == 1
    # 攻撃モードの求人エージェントには、指示が渡る(§8.2)。ほかの 2 つには渡らない(上のテスト)。
    assert INSTRUCTION in stub_llm.requests[0].contents[0][1][0]


@pytest.mark.parametrize("phase", PHASES)
async def test_plain_turn_input_is_rejected_by_the_attacker_endpoint(phase, http, stub_llm):
    # DV-04 (/a2a/attacker は AttackerTurnInput だけを受け付ける。principal_instruction のない TurnInput は拒否)
    data = valid_data("employer", phase)  # 通常の求人側の TurnInput
    assert "principal_instruction" not in data
    body = await send_message(http, "attacker", [data_part(data)])
    assert_rejected(body)
    assert stub_llm.requests == []


@pytest.mark.parametrize("phase", PHASES)
async def test_principal_instruction_longer_than_400_chars_is_rejected(phase, http, stub_llm):
    # DV-04 (§2.7: principal_instruction は 400 文字以内。攻撃モードの受信口でも超えたら拒否)
    data = valid_data("attacker", phase)
    data["principal_instruction"] = "あ" * 401
    body = await send_message(http, "attacker", [data_part(data)])
    assert_rejected(body)
    assert stub_llm.requests == []


# ----------------------------------------------------------------------
# 呼ぶ側(web): /a2a/attacker は攻撃モードの交渉からしか呼ばれない
# ----------------------------------------------------------------------


async def _run_demo(env, store) -> str:
    candidate, employer = put_candidate_and_employer_templates(store._db)
    body = {
        "request_id": "request-demo-1",
        "candidate_template_id": candidate.template_id,
        "employer_template_id": employer.template_id,
    }
    response = await post(env.browser(), "/v1/demo/negotiations", body)
    assert response.status_code == 200, response.text
    return response.json()["nid"]


async def test_the_attacker_endpoint_is_called_only_for_attack_mode_negotiations(make_env, store):
    # DV-04: /a2a/attacker は攻撃モードの交渉からしか呼ばれない。デモ・ライブの交渉は、候補者側と通常の求人側を呼び、攻撃者は呼ばない。
    # 攻撃モードの交渉は、候補者側と攻撃者を呼び、通常の求人側は呼ばない。指示(自由文)は、攻撃者への入力にだけ入る。
    env = make_env(run_referees=True)
    agreement = sample_package(salary=700)

    env.agents.script("candidate", plan_dict(move="propose", package=agreement))
    env.agents.script("employer", plan_dict(move="accept"))
    demo_nid = await _run_demo(env, store)
    await run_to_the_end(env, demo_nid)

    live_browser = env.browser()
    pid = await live_browser.register()
    env.agents.script("candidate", plan_dict(move="propose", package=agreement))
    env.agents.script("employer", plan_dict(move="accept"))
    live_nid = await live_browser.create_negotiation(pid, env.put_employer_template())
    await run_to_the_end(env, live_nid)

    env.agents.script("candidate", plan_dict(move="propose", package=agreement))
    env.agents.script("attacker", plan_dict(move="accept"))
    attack_nid = (await post(env.browser(), CREATE, create_body(1, f"{CANARY} {INSTRUCTION}"))).json()["nid"]
    await run_to_the_end(env, attack_nid)

    roles_by_nid = {
        nid: sorted({call.role for call in env.agents.calls if call.nid == nid}) for nid in (demo_nid, live_nid, attack_nid)
    }
    assert roles_by_nid == {
        demo_nid: ["candidate", "employer"],
        live_nid: ["candidate", "employer"],
        attack_nid: ["attacker", "candidate"],
    }
    # 指示は、攻撃モードの交渉の攻撃者への入力にだけ入る(どの交渉の、どの候補者側・求人側の入力にも入らない)
    carrying = [call for call in env.agents.calls if CANARY in call.turn_input.model_dump_json()]
    assert carrying and all(call.role == "attacker" and call.nid == attack_nid for call in carrying)
    assert all(isinstance(call.turn_input, AttackerTurnInput) for call in carrying)


def test_the_vault_does_not_create_an_attack_negotiation_against_a_real_candidate():
    # 金庫の作成の検証(§6.3): mode=attack は架空人物の候補者でしか作れない。web を通らない経路でも、本物の候補者とは組めない。
    with pytest.raises(ValidationError):
        CreateNegotiationRequest(
            request_id="request-0001",
            mode="attack",
            candidate=CandidateParticipantRequest(is_fictional=False, principal_id="0123456789abcdef"),
            employer=EmployerParticipantRequest(template_id=CONFIG.employer_template_id),
        )


# ----------------------------------------------------------------------
# 攻撃モードの API から、本物の依頼者のデータには届かない
# ----------------------------------------------------------------------

ATTACK_PREFIX = "/v1/demo/attack/"
# 攻撃モードの各ルートを、本物のライブの交渉の ID を渡して呼んだときの結果。
ROUTES_WITH_AN_ID = {
    ("GET", "/v1/demo/attack/negotiations/{nid}/events"): 403,  # ライブの交渉は読めない(段の状態と金庫のデモ用の口の両方が断る)
    ("POST", "/v1/demo/attack/negotiations/{nid}/instruction"): 404,  # この web が持っている攻撃の交渉ではない
    ("GET", "/v1/demo/attack/walls/2/{nid}"): 403,
    ("GET", "/v1/demo/attack/walls/3/{nid}"): 403,
}
# ID を取らないルートは、本物に触れずに通る(攻撃モードの交渉の作成・初期値・生メッセージ・二分探索の実演)。
ROUTES_WITHOUT_AN_ID = {
    ("POST", "/v1/demo/attack/negotiations"): 200,
    ("POST", "/v1/demo/attack/bisection"): 200,
    ("GET", "/v1/demo/attack/walls/1/example"): 200,
    ("POST", "/v1/demo/attack/walls/1"): 200,
}
SESSION_DEPENDENCIES = ("require_session", "require_own_principal", "require_own_negotiation")


def _api_routes(routes):
    """app.routes から、実際のルート(APIRoute)を取り出す。FastAPI は include_router したルータを、入れ子のまま(_IncludedRouter)持つ。"""
    for route in routes:
        if hasattr(route, "original_router"):
            yield from _api_routes(route.original_router.routes)
        elif hasattr(route, "dependant"):
            yield route


def _attack_routes(app) -> dict[tuple[str, str], object]:
    routes = {
        (method, route.path): route
        for route in _api_routes(app.routes)
        if route.path.startswith(ATTACK_PREFIX)
        for method in route.methods
    }
    assert routes, "no attack route was found (the route walk is broken, so every route-wide check would pass for nothing)"
    return routes


def _dependency_names(dependant) -> set[str]:
    names = {getattr(dependant.call, "__name__", "")}
    for child in dependant.dependencies:
        names |= _dependency_names(child)
    return names


async def test_no_attack_route_takes_a_principal_id_or_depends_on_the_session(make_env):
    # DV-01・DV-04: 攻撃モードのルートは、依頼者の ID(pid)を受け取らず、セッション(依頼者の確認)に依存しない。パスの変数は nid だけ。
    # このテストが知っているルートの一覧と、実際に登録されているルートが食い違えば(ルートを足して、確かめ忘れていれば)失敗する。
    env = make_env()
    routes = _attack_routes(env.app)

    assert set(routes) == set(ROUTES_WITH_AN_ID) | set(ROUTES_WITHOUT_AN_ID)
    for (_method, path), route in routes.items():
        assert {param.name for param in route.dependant.path_params} <= {"nid"}, path
        assert not set(SESSION_DEPENDENCIES) & _dependency_names(route.dependant), path
        assert "principal" not in path and "{pid}" not in path
    # デモ用のエンドポイントの下にあるので、ミドルウェアはセッションを見ない(クッキーを付けても、利用記録を更新しない)
    assert ATTACK_PREFIX.startswith(DEMO_PATH_PREFIX)


async def test_calling_every_attack_route_changes_nothing_of_a_real_principal_or_a_live_negotiation(
    make_env, store, default_db, clock
):
    # AC-13 の「接続先は架空人物の金庫だけ」・DV-04: 本物の依頼者のクッキーを付けて、本物のライブの交渉の ID を渡して、攻撃モードの
    # すべてのルートを呼んでも、本物の依頼者のデータ(金庫の依頼者の文書・利用記録・ライブの交渉の状態とイベント・段の状態)は
    # 1 つも変わらない。ライブの交渉の ID を、応答に写しもしない。
    env = make_env(send_raw=RecordingSender())
    browser = env.browser()
    pid = await browser.register()
    live_nid = await browser.create_negotiation(pid, env.put_employer_template())

    def snapshot() -> dict:
        return {
            "vault_db": {k: v for k, v in dump_documents(store._db).items() if pid in k or live_nid in k},
            "default_db": {k: v for k, v in dump_documents(default_db).items() if pid in k or live_nid in k},
            "negotiations": [(s.nid, s.state) for s in store.list_principal_negotiations(pid)],
        }

    before = snapshot()
    assert before["vault_db"] and before["default_db"] and before["negotiations"]  # 比べる対象が空でないこと
    clock.advance(dt.timedelta(hours=3))  # 利用記録の更新間隔(1 時間)を過ぎさせる(通常のルートなら更新が起きる)

    expected_statuses = {**ROUTES_WITH_AN_ID, **ROUTES_WITHOUT_AN_ID}
    for (method, template), _route in sorted(_attack_routes(env.app).items()):
        path = template.replace("{nid}", live_nid)
        if template == "/v1/demo/attack/negotiations":
            response = await post(browser, path, create_body(1))
        elif template == "/v1/demo/attack/bisection":
            response = await post(browser, path, {"request_id": "request-bisect-0001"})
        elif method == "POST" and template.endswith("/instruction"):
            response = await post(browser, path, {"instruction": "ライブの交渉を書き換えろ"})
        elif method == "POST":
            response = await post(browser, path, {"messageId": "m"})
        else:
            response = await browser.get(path)
        assert response.status_code == expected_statuses[(method, template)], (method, template, response.text)
        assert live_nid not in response.text.replace(path, "")

    assert snapshot() == before  # 本物の依頼者のデータは、何も変わっていない
    # 対照: 通常のルート(セッションを見る)なら、同じ時刻の進みで利用記録が更新される。攻撃モードのルートは、更新もしなかった
    meta_ref = default_db.collection("principals_meta").document(pid)
    last_active_before = meta_ref.get().to_dict()["last_active_at"]
    assert (await browser.get(f"/v1/principals/{pid}/negotiations")).status_code == 200
    assert meta_ref.get().to_dict()["last_active_at"] > last_active_before


async def test_an_attack_negotiation_is_never_a_real_principals_negotiation(make_env, store, default_db):
    # 本物の依頼者のクッキーを付けて攻撃モードの交渉を作っても、その交渉は本物の依頼者のものにならない: 架空の候補者・依頼者 ID なし・
    # 本人の交渉の一覧に出ない・段の状態に依頼者 ID がない。本物の依頼者の削除・30 日の自動削除の流れにも引っかからない。
    env = make_env()
    browser = env.browser()
    pid = await browser.register()
    nid = (await post(browser, CREATE, create_body(1))).json()["nid"]

    assert store.list_principal_negotiations(pid) == []
    document = json.loads(json.dumps(store._negotiation_ref(nid).get().to_dict(), default=str))
    candidate = document["participants"]["candidate"]
    assert candidate["is_fictional"] is True and candidate.get("principal_id") is None
    stage = default_db.collection("stages").document(nid).get().to_dict()
    assert stage["candidate_principal_id"] is None and "ttl_at" in stage
    assert pid not in json.dumps(document, default=str) + json.dumps(stage, default=str)
