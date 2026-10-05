"""web の金庫クライアント(design.md §3.3・§4.1)。

前半は httpx.MockTransport で、HTTP の失敗を例外に変換する規則とページ送りを確かめる
(金庫を動かさない)。v14 で足した金庫の口(費用の上限での停止 stop_cost_limit・冪等キーからの引き当て by-request。別の作業者が
金庫に作る口の、web 側の呼び方)も、ここで、送る形(パス・本文)を確かめる。後半は本物の金庫の app を ASGI のままつないで、
見回りの一覧に mode と candidate_principal_id が載ることを確かめる(1d-1 で金庫の一覧に足した項目)。
"""

import datetime as dt
import json

import httpx
import pytest
from vault.api_models import ControlRequest, MoveRequest
from vault_helpers import sample_package
from web.vault_client import (
    VaultClient,
    VaultClientError,
    VaultConflictError,
    VaultNotFoundError,
    VaultUnavailableError,
)
from web_helpers import create_demo_negotiation, create_live_negotiation


def _client(handler) -> VaultClient:
    return VaultClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://vault"))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (404, VaultNotFoundError),
        (409, VaultConflictError),
        (503, VaultUnavailableError),
        (500, VaultClientError),
        (422, VaultClientError),
    ],
)
async def test_http_failures_become_typed_client_errors(status, error_type):
    # 金庫の応答を、409(状態を読み直す)・503(あとで呼び直す)・404(交渉が消えた)に区別できる例外にする。
    client = _client(lambda request: httpx.Response(status, json={"detail": "reason from the vault"}))

    with pytest.raises(VaultClientError) as excinfo:
        await client.expire("0123456789abcdef")

    assert type(excinfo.value) is error_type
    assert excinfo.value.status_code == status
    assert "reason from the vault" in str(excinfo.value)


@pytest.mark.anyio
async def test_stop_cost_limit_posts_the_action_to_the_control_path_without_a_version():
    # §3.4・§8.2: 費用の上限での停止は、control の action=stop_cost_limit(冪等。expected_version は取らない)。side は、金庫が使わない。
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"version": 7, "status": "judged", "paused": False})

    response = await _client(handler).stop_cost_limit("0123456789abcdef")

    assert seen == [
        ("POST", "/v1/negotiations/0123456789abcdef/control", {"side": "candidate", "action": "stop_cost_limit"})
    ]
    assert (response.version, response.status, response.paused) == (7, "judged", False)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [409, 503, 500])
async def test_a_failed_stop_cost_limit_is_a_typed_error_like_the_other_calls(status):
    client = _client(lambda request: httpx.Response(status, json={"detail": "reason"}))

    with pytest.raises(VaultClientError) as excinfo:
        await client.stop_cost_limit("0123456789abcdef")

    assert excinfo.value.status_code == status


@pytest.mark.anyio
async def test_by_request_returns_the_nid_of_a_known_key_and_none_for_an_unknown_one():
    # §3.3・台帳 X-57: 冪等キーの正本は金庫。200 {"nid"} なら既知、404 なら未知(None)。
    paths = []

    def handler(request):
        paths.append(request.url.raw_path.decode())
        if request.url.path.endswith("known-key"):
            return httpx.Response(200, json={"nid": "0123456789abcdef"})
        return httpx.Response(404, json={"detail": "not found"})

    client = _client(handler)

    assert await client.get_negotiation_by_request("known-key") == "0123456789abcdef"
    assert await client.get_negotiation_by_request("missing-key") is None
    assert paths == ["/v1/negotiations/by-request/known-key", "/v1/negotiations/by-request/missing-key"]


@pytest.mark.anyio
async def test_by_request_encodes_the_key_into_one_path_segment():
    # web が作る request_id(「依頼者 ID:画面の乱数」など)は、どんな文字が入っても、1 つのパスの部分として送る
    # (「/」や「?」で別のパス・問い合わせに化けない)。
    paths = []

    def handler(request):
        paths.append(request.url.raw_path.decode())
        return httpx.Response(404, json={"detail": "not found"})

    await _client(handler).get_negotiation_by_request("0123456789abcdef:a/b?c#d e")

    assert paths == ["/v1/negotiations/by-request/0123456789abcdef%3Aa%2Fb%3Fc%23d%20e"]


@pytest.mark.anyio
async def test_by_request_does_not_hide_a_failure_that_is_not_a_404():
    # 金庫が応えない(503)ときに「未知のキー」と読むと、同じ request_id を二重に作ってしまう。404 以外は、そのまま例外にする。
    client = _client(lambda request: httpx.Response(503, json={"detail": "down"}))

    with pytest.raises(VaultUnavailableError):
        await client.get_negotiation_by_request("some-key")


@pytest.mark.anyio
async def test_a_transport_failure_is_reported_as_unavailable():
    # 通信そのものの失敗(接続できないなど)は、503 と同じ「あとで呼び直す」失敗として扱う。
    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(VaultUnavailableError):
        await client.get_view("0123456789abcdef", "candidate")


@pytest.mark.anyio
async def test_list_open_negotiations_follows_the_cursor_until_the_last_page():
    # 見回りの一覧(open=true)は、next_cursor がなくなるまで全ページを集める。
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).isoformat()

    def item(nid: str) -> dict:
        return {
            "nid": nid,
            "status": "active",
            "paused": False,
            "deadline": now,
            "expires_at": now,
            "mode": "demo",
            "candidate_principal_id": None,
        }

    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["open"] == "true"
        cursor = request.url.params.get("cursor")
        seen_cursors.append(cursor)
        if cursor is None:
            return httpx.Response(200, json={"items": [item("aaaa"), item("bbbb")], "next_cursor": "bbbb"})
        assert cursor == "bbbb"
        return httpx.Response(200, json={"items": [item("cccc")], "next_cursor": None})

    items = await _client(handler).list_open_negotiations()

    assert [i.nid for i in items] == ["aaaa", "bbbb", "cccc"]
    assert seen_cursors == [None, "bbbb"]


@pytest.mark.anyio
async def test_get_demo_events_calls_the_demo_endpoint_and_a_404_is_not_found():
    # 台帳 X-38: デモ用の読み出しは、金庫のデモ用の口(通常の events の口ではない)を呼ぶ。側と after_seq を渡す。
    # 金庫が本物の交渉・存在しない交渉を断る 404 は、VaultNotFoundError(web が 403 に写す)。
    seen: list[tuple[str, dict]] = []
    event = {"seq": 2, "kind": "check", "package": sample_package().model_dump(mode="json"), "own_evaluation": "acceptable"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if "0123456789abcdef" in request.url.path:
            return httpx.Response(200, json=[event])
        return httpx.Response(404, json={"detail": "fedcba9876543210"})

    client = _client(handler)

    events = await client.get_demo_events("0123456789abcdef", "employer", after_seq=1)
    with pytest.raises(VaultNotFoundError):
        await client.get_demo_events("fedcba9876543210", "candidate")

    assert [(e.seq, e.kind, e.own_evaluation) for e in events] == [(2, "check", "acceptable")]
    assert seen == [
        ("/v1/demo/negotiations/0123456789abcdef/events", {"side": "employer", "after_seq": "1"}),
        ("/v1/demo/negotiations/fedcba9876543210/events", {"side": "candidate", "after_seq": "0"}),
    ]


@pytest.mark.anyio
async def test_request_bodies_omit_unset_fields():
    # accept・end のように package を持たない手は、package・reason を null で送らず、項目ごと省く。
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"version": 1, "status": "active", "valid": True})

    client = _client(handler)
    await client.post_move("0123456789abcdef", MoveRequest(expected_version=0, side="employer", move="accept"))
    await client.post_move(
        "0123456789abcdef",
        MoveRequest(expected_version=1, side="candidate", move="propose", package=sample_package()),
    )

    assert bodies[0] == {"expected_version": 0, "side": "employer", "move": "accept"}
    assert bodies[1]["package"]["salary"] == 700
    assert "reason" not in bodies[1]


@pytest.mark.anyio
async def test_the_open_list_carries_mode_and_the_real_candidates_principal_id(store, vault_client):
    # 見回りがタスクと stages/{nid}(本物の候補者の依頼者 ID を持つ。§6.2)を作り直せるように、
    # 金庫の一覧に mode と candidate_principal_id が載る(候補者が架空人物なら None)。
    demo_nid = create_demo_negotiation(store, mode="attack")
    live_nid, pid = create_live_negotiation(store)

    items = {item.nid: item for item in await vault_client.list_open_negotiations()}

    assert items[demo_nid].mode == "attack"
    assert items[demo_nid].candidate_principal_id is None
    assert items[live_nid].mode == "live"
    assert items[live_nid].candidate_principal_id == pid
    assert items[live_nid].status == "active"


@pytest.mark.anyio
async def test_finished_negotiations_leave_the_open_list(store, vault_client):
    # 見回りの一覧は judged でない交渉だけを返す(取消の後は載らない)。
    nid = create_demo_negotiation(store)
    assert [i.nid for i in await vault_client.list_open_negotiations()] == [nid]

    await vault_client.control(nid, ControlRequest(side="candidate", action="cancel"))

    assert await vault_client.list_open_negotiations() == []
