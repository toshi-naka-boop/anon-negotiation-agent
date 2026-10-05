"""活動ログと、並べて見る画面(候補者側・求人側の 2 つのパネル)の API(design.md §7・§3.2・§6.3。FR-37。DV-01・DV-10)。

金庫は本物の vault の app を ASGI のままつなぎ、web の app へは Browser(クッキーを持つ httpx のクライアント)から入る。
確かめること:
- 金庫のイベント(その側の見え方)が、種類ごとに決めた項目だけの形に整う。金庫のイベントにない項目(相手側の評価・残り回数・version・
  終了理由・期限)は出ない。カナリア(求人側だけが持つ評価の値)が、本人向けの応答に出ない。
- 本人の経路は、セッションの依頼者が当事者の交渉だけ(他人の交渉も、存在しない交渉も、同じ 403。セッションがなければ 401)。
  本人の側(候補者側)しか読めない。
- デモの経路は、セッションを見ず、架空人物の交渉(デモ・攻撃)の両側が読める。本物の依頼者の交渉は、段の状態〔補助〕と金庫〔正本〕のどちらが
  断っても 403(台帳 X-38)。
- 並べて見る画面: デモ・攻撃は両側、本物の利用者の交渉は本人の側だけ。段の参照は段の番号だけで、段の中身を出さない。
- 本人向けの交渉一覧(§3.3)の「一時停止中」は、本人が止めたときだけ(L7-2)。
"""

import datetime as dt
import json
from typing import get_args

import pytest
from negotiation_core import Verdict
from vault.api_models import ControlRequest, EventViewItem, MoveRequest, PrincipalAnswerRequest
from vault.models import EmployerRule, EventKind
from vault_helpers import needs_confirmation_policy, reject_all_policy, sample_package
from web.activity_api import _SHAPE, to_entry
from web.api import DEMO_PATH_PREFIX
from web_app_helpers import CANARY
from web_helpers import create_demo_negotiation

# 画面に出てはならない項目名(金庫の中だけで使う値。§3.1・§3.2・X-25)。
_HIDDEN_KEYS = {
    "version",
    "budget",
    "remaining_evaluations",
    "remaining_moves",
    "remaining_principal_checks",
    "counters",
    "end_reason",
    "deadline",
    "expires_at",
    "paused_at",
    "receiver_evaluation",
    "pending_offer",
    "last_check",
    "snapshots",
}


def _keys(value) -> set[str]:
    """JSON の値に現れるすべての項目名(入れ子を含む)。"""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def _row(entry: dict) -> tuple:
    """活動ログの 1 件の、判定に使う項目(組み合わせ・最終結果は別に確かめる)。"""
    return (
        entry["seq"],
        entry["actor"],
        entry["action"],
        entry["own_evaluation"],
        entry["answer"],
        entry["reason"],
        entry["attempted_move"],
    )


def _move(store, nid: str, side: str, move: str, package=None, *, reason: str | None = None) -> int:
    """金庫に手を 1 つ登録し、応答の version を返す。"""
    expected_version = store.get_view(nid, side).version
    response = store.process_move(
        nid, MoveRequest(expected_version=expected_version, side=side, move=move, package=package, reason=reason)
    )
    return response.version


# ----------------------------------------------------------------------
# 形(金庫のイベント 1 件 → 活動ログの 1 件)
# ----------------------------------------------------------------------


def test_every_event_kind_of_the_vault_has_a_screen_shape():
    # 金庫のイベントの種類が増えたら、画面向けの形を決めるまで通らない(知らない種類を黙って通さない)。
    assert set(_SHAPE) == set(get_args(EventKind))


def test_only_the_fields_of_the_kind_are_copied_from_the_vaults_event():
    # 金庫のイベントに(種類に合わない)項目が入っていても、画面には出ない。propose は組み合わせだけ(提案した側の見え方に、評価はない。§3.2)。
    package = sample_package()
    event = EventViewItem(
        seq=3,
        kind="propose",
        package=package,
        own_evaluation="not_acceptable",
        reason="schema_invalid",
        answer="accept",
        attempted_move="propose",
        result={"likelihood": "high", "package": package.model_dump()},
    )

    entry = to_entry(event)

    assert entry.model_dump(mode="json") == {
        "seq": 3,
        "actor": "self",
        "action": "propose",
        "package": package.model_dump(),
        "own_evaluation": None,
        "answer": None,
        "reason": None,
        "attempted_move": None,
        "result": None,
    }


def test_an_offer_received_is_the_counterpartys_propose_with_the_sides_own_evaluation():
    package = sample_package()

    received = to_entry(EventViewItem(seq=1, kind="offer_received", package=package, own_evaluation="needs_confirmation"))
    rejected = to_entry(EventViewItem(seq=2, kind="offer_rejected", package=package))

    assert (received.actor, received.action, received.own_evaluation) == (
        "counterparty",
        "propose",
        Verdict.NEEDS_CONFIRMATION,
    )
    assert (rejected.actor, rejected.action, rejected.own_evaluation) == ("counterparty", "reject", None)


# ----------------------------------------------------------------------
# 本人の活動ログ(FR-37)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_live_activity_log_shows_only_the_principals_side_and_never_the_other_sides_evaluation(web_app):
    # §3.2・DV-10: 本人には本人の側の見え方だけ。求人側だけが持つ評価の値(カナリア)が、応答のどこにも出ない。
    # 求人側は、どの組み合わせも「本人確認が必要」と評価する。候補者側の評価は「受けられる」だけ(カナリアが混ざりようがない)。
    browser = web_app.browser()
    pid = await browser.register()
    template_id = web_app.put_employer_template(
        rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))]
    )
    nid = await browser.create_negotiation(pid, template_id)
    store, package = web_app.store, sample_package()
    _move(store, nid, "candidate", "check", package)
    _move(store, nid, "candidate", "propose", package)
    _move(store, nid, "employer", "reject")
    # 対照: カナリアは、求人側の見え方には実際にある(ないから出ないのではない)
    employer_side = store.get_events(nid, "employer")
    assert [(e.kind, e.own_evaluation) for e in employer_side] == [
        ("offer_received", "needs_confirmation"),
        ("reject", None),
    ]

    response = await browser.get(f"/v1/negotiations/{nid}/activity")

    assert response.status_code == 200
    body = response.json()
    assert body["side"] == "candidate"
    assert [_row(entry) for entry in body["entries"]] == [
        (1, "self", "check", "acceptable", None, None, None),
        (2, "self", "propose", None, None, None, None),
        (3, "counterparty", "reject", None, None, None, None),
    ]
    assert body["next_after_seq"] == 3
    assert [entry["package"] for entry in body["entries"]] == [package.model_dump()] * 3
    for canary in ("needs_confirmation", "offer_received", "offer_rejected"):
        assert canary not in response.text, canary
    assert _keys(body).isdisjoint(_HIDDEN_KEYS)
    assert set(body) == {"side", "entries", "next_after_seq"}


@pytest.mark.anyio
async def test_the_principals_question_and_answer_appear_in_the_activity_log(web_app):
    # FR-37・§4.4: 何を聞かれ、丸めた後で何と答えたか。途中確認の質問(組み合わせ)と、本人の回答、評価し直した結果が並ぶ。
    browser = web_app.browser()
    pid = await browser.register(accept_anchors=[], reject_anchors=[])  # 何も決めていない → どの組み合わせも「本人確認が必要」
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    package = sample_package()
    _move(web_app.store, nid, "candidate", "ask_principal", package)

    asked = (await browser.get(f"/v1/negotiations/{nid}/activity")).json()
    answer = await browser.post(
        f"/v1/negotiations/{nid}/principal-answer", {"package": package.model_dump(), "answer": "accept"}
    )
    answered = await browser.get(f"/v1/negotiations/{nid}/activity", after_seq=asked["next_after_seq"])

    assert answer.status_code == 200
    assert [(_row(e), e["package"]) for e in asked["entries"]] == [
        ((1, "self", "ask_principal", None, None, None, None), package.model_dump())
    ]
    assert [(_row(e), e["package"]) for e in answered.json()["entries"]] == [
        ((2, "self", "principal_answer", "acceptable", "accept", None, None), package.model_dump())
    ]
    assert _keys(answered.json()).isdisjoint(_HIDDEN_KEYS)


@pytest.mark.anyio
async def test_the_activity_log_is_read_from_after_seq_and_next_after_seq_continues_from_there(web_app):
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    store = web_app.store
    _move(store, nid, "candidate", "check", sample_package())
    _move(store, nid, "candidate", "check", sample_package(salary=800))

    everything = (await browser.get(f"/v1/negotiations/{nid}/activity")).json()
    later = (await browser.get(f"/v1/negotiations/{nid}/activity", after_seq=1)).json()
    nothing_new = (await browser.get(f"/v1/negotiations/{nid}/activity", after_seq=2)).json()
    _move(store, nid, "candidate", "check", sample_package(salary=900))
    continued = (await browser.get(f"/v1/negotiations/{nid}/activity", after_seq=nothing_new["next_after_seq"])).json()

    assert [e["seq"] for e in everything["entries"]] == [1, 2] and everything["next_after_seq"] == 2
    assert [e["seq"] for e in later["entries"]] == [2] and later["next_after_seq"] == 2
    assert nothing_new["entries"] == [] and nothing_new["next_after_seq"] == 2  # 新しい記録がなければ、位置は進まない
    assert [e["seq"] for e in continued["entries"]] == [3] and continued["next_after_seq"] == 3
    # 範囲外の after_seq は、金庫に届かず 422
    for bad in (-1, 2**31):
        assert (await browser.get(f"/v1/negotiations/{nid}/activity", after_seq=bad)).status_code == 422


@pytest.mark.anyio
async def test_the_live_activity_log_ends_with_one_final_result_that_has_no_reason(web_app):
    # 取消は結果「なし」。最終結果は 1 件だけで、終了理由(cancelled)も version も画面に出ない(§3.1)。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "pause"})).status_code == 200
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"})).status_code == 200

    response = await browser.get(f"/v1/negotiations/{nid}/activity")

    entries = response.json()["entries"]
    assert [(e["actor"], e["action"]) for e in entries] == [("self", "pause"), ("system", "final_result")]
    assert entries[-1]["result"] == {"likelihood": "none", "package": None}
    for hidden in ("cancelled", "end_reason", "version"):
        assert hidden not in response.text


@pytest.mark.anyio
async def test_reading_the_activity_log_changes_nothing(web_app):
    # 読み出しは何も書かない: 金庫の version もイベント列も、段の状態も変わらない(§3.3 の「読み出し」)。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    _move(web_app.store, nid, "candidate", "check", sample_package())
    before = (web_app.store.get_view(nid, "candidate").version, web_app.store.get_events(nid, "candidate"))
    stage_before = web_app.default_db.collection("stages").document(nid).get().to_dict()

    for path in (f"/v1/negotiations/{nid}/activity", f"/v1/negotiations/{nid}/panels"):
        assert (await browser.get(path)).status_code == 200

    assert (web_app.store.get_view(nid, "candidate").version, web_app.store.get_events(nid, "candidate")) == before
    assert web_app.default_db.collection("stages").document(nid).get().to_dict() == stage_before


# ----------------------------------------------------------------------
# 権限(§6.3・DV-01)
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["activity", "panels"])
async def test_only_a_party_of_the_negotiation_can_read_it_and_existence_is_not_revealed(web_app, route):
    # 他人の交渉・存在しない交渉・デモの交渉・形の違う ID は、どれも同じ 403(交渉があるかどうかを知らせない)。セッションがなければ 401。
    mine, others, stranger = web_app.browser(), web_app.browser(), web_app.browser()
    pid, other_pid = await mine.register(), await others.register()
    template_id = web_app.put_employer_template()
    my_nid = await mine.create_negotiation(pid, template_id)
    other_nid = await others.create_negotiation(other_pid, template_id)
    demo_nid = create_demo_negotiation(web_app.store)
    _move(web_app.store, other_nid, "candidate", "check", sample_package())  # 他人の記録(読まれてはならない)

    assert (await mine.get(f"/v1/negotiations/{my_nid}/{route}")).status_code == 200  # 本人は読める(何でも断っているのではない)
    refusals = []
    for nid in (other_nid, "0123456789abcdef", demo_nid, "not-a-negotiation-id"):
        response = await mine.get(f"/v1/negotiations/{nid}/{route}")
        refusals.append((response.status_code, response.json()))
    assert refusals == [(403, {"detail": "forbidden"})] * 4
    for nid in (my_nid, other_nid):
        response = await stranger.get(f"/v1/negotiations/{nid}/{route}")
        assert response.status_code == 401
        assert "set-cookie" not in response.headers  # ID を発行しない(発行は開始ページの GET だけ。§6.3)


@pytest.mark.anyio
async def test_the_principal_cannot_ask_for_the_other_side_of_their_own_negotiation(web_app):
    # DV-01: 本物の利用者の交渉では、イベント列は本人の側としてしか読めない。側を指定する引数はなく、指定しても本人の側のまま。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    _move(web_app.store, nid, "candidate", "propose", sample_package())
    candidate_kinds = [e.kind for e in web_app.store.get_events(nid, "candidate")]
    employer_kinds = [e.kind for e in web_app.store.get_events(nid, "employer")]
    assert candidate_kinds != employer_kinds

    plain = await browser.get(f"/v1/negotiations/{nid}/activity")
    asked = await browser.get(f"/v1/negotiations/{nid}/activity", side="employer")
    panels = await browser.get(f"/v1/negotiations/{nid}/panels", side="employer", employer_after_seq=0)

    for response in (plain, asked):
        assert [(e["actor"], e["action"]) for e in response.json()["entries"]] == [("self", "propose")]
    assert panels.json()["employer"] is None
    assert "offer_received" not in plain.text + asked.text + panels.text
    assert [(e["actor"], e["action"]) for e in panels.json()["candidate"]["entries"]] == [("self", "propose")]


# ----------------------------------------------------------------------
# デモ・攻撃の経路(セッションを見ない。DEMO_PATH_PREFIX の下)
# ----------------------------------------------------------------------


async def _three_demo_negotiations(web_app) -> tuple[str, str, str]:
    """金庫のイベントのすべての種類が出る、架空人物どうしの交渉を 3 つ作る(段の状態も作る)。

    A: 候補者は何も決めていない(確かめ → 途中確認 → 回答 → 提案 → 求人が受けて合意)。
    B: 提案 → 断られる → 無効手の登録 → 一時停止・再開 → 候補者が終える。
    C: 候補者は何も受けない(ガードで拒否された提案 → 確かめ)。攻撃モード。
    """
    store, package = web_app.store, sample_package()
    a = create_demo_negotiation(store, candidate_policy=needs_confirmation_policy("candidate"))
    b = create_demo_negotiation(store)
    c = create_demo_negotiation(store, candidate_policy=reject_all_policy("candidate"), mode="attack")
    await web_app.services.sweeper.sweep_once()  # 架空の候補者の交渉にも、段の状態を作る(§6.2)

    _move(store, a, "candidate", "check", package)
    _move(store, a, "candidate", "ask_principal", package)
    store.process_principal_answer(
        a,
        PrincipalAnswerRequest(
            expected_version=store.get_view(a, "candidate").version, side="candidate", package=package, answer="accept"
        ),
    )
    _move(store, a, "candidate", "propose", package)
    _move(store, a, "employer", "accept")

    _move(store, b, "candidate", "propose", package)
    _move(store, b, "employer", "reject")
    _move(store, b, "candidate", "invalid", reason="schema_invalid")
    store.control(b, ControlRequest(side="candidate", action="pause"))
    store.control(b, ControlRequest(side="candidate", action="resume"))
    _move(store, b, "candidate", "end")

    _move(store, c, "candidate", "propose", package)
    _move(store, c, "candidate", "check", package)
    return a, b, c


@pytest.mark.anyio
async def test_the_demo_panels_show_both_sides_each_with_only_its_own_view(web_app):
    # DV-10・§3.2: デモ・攻撃は両側のパネルを 1 回の呼び出しで返す。どのパネルも、その側自身の見え方(相手の評価は入らない)。
    a, b, c = await _three_demo_negotiations(web_app)
    store, package = web_app.store, sample_package().model_dump()
    demo = web_app.browser()  # クッキーのない訪問者

    panels = {}
    for name, nid in (("a", a), ("b", b), ("c", c)):
        response = await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{nid}/panels")
        assert response.status_code == 200, (name, response.text)
        panels[name] = response.json()
        assert set(panels[name]) == {"candidate", "employer", "stage"}
        assert panels[name]["stage"] == {"stage": 0}
        assert _keys(panels[name]).isdisjoint(_HIDDEN_KEYS)

    def rows(name: str, side: str) -> list[tuple]:
        return [_row(entry) for entry in panels[name][side]["entries"]]

    assert rows("a", "candidate") == [
        (1, "self", "check", "needs_confirmation", None, None, None),
        (2, "self", "ask_principal", None, None, None, None),
        (3, "self", "principal_answer", "acceptable", "accept", None, None),
        (4, "self", "propose", None, None, None, None),
        (5, "system", "final_result", None, None, None, None),
    ]
    assert rows("a", "employer") == [
        (1, "counterparty", "propose", "acceptable", None, None, None),
        (2, "system", "final_result", None, None, None, None),
    ]
    assert rows("b", "candidate") == [
        (1, "self", "propose", None, None, None, None),
        (2, "counterparty", "reject", None, None, None, None),
        (3, "self", "invalid", None, None, "schema_invalid", None),
        (4, "self", "pause", None, None, None, None),
        (5, "self", "resume", None, None, None, None),
        (6, "system", "final_result", None, None, None, None),
    ]
    assert rows("b", "employer") == [
        (1, "counterparty", "propose", "acceptable", None, None, None),
        (2, "self", "reject", None, None, None, None),
        (3, "system", "final_result", None, None, None, None),
    ]
    assert rows("c", "candidate") == [
        (1, "self", "invalid", "not_acceptable", None, "not_acceptable_to_own_principal", "propose"),
        (2, "self", "check", "not_acceptable", None, None, None),
    ]
    assert rows("c", "employer") == []
    # 組み合わせと最終結果は、金庫のその側の見え方と同じ
    for name, nid in (("a", a), ("b", b), ("c", c)):
        for side in ("candidate", "employer"):
            vault_events = store.get_events(nid, side)
            for entry, event in zip(panels[name][side]["entries"], vault_events, strict=True):
                assert entry["package"] == (event.package.model_dump() if event.package is not None else None)
                assert entry["result"] == (event.result.model_dump(mode="json") if event.result is not None else None)
    assert panels["a"]["candidate"]["entries"][0]["package"] == package
    assert panels["a"]["candidate"]["entries"][-1]["result"]["package"] == package  # 合意した組み合わせ
    assert panels["b"]["candidate"]["entries"][-1]["result"] == {"likelihood": "none", "package": None}
    # DV-10: 終わった交渉の最終記録は、双方に 1 件だけ(終わっていない C にはない)
    for name, finals in ("a", 1), ("b", 1), ("c", 0):
        for side in ("candidate", "employer"):
            actions = [entry["action"] for entry in panels[name][side]["entries"]]
            assert actions.count("final_result") == finals, (name, side)
    # このシナリオが、金庫のイベントの種類をすべて通っている(形の確認が全種類に及ぶ)
    seen = {event.kind for nid in (a, b, c) for side in ("candidate", "employer") for event in store.get_events(nid, side)}
    assert seen == set(get_args(EventKind))
    # 相手の評価は、どちらのパネルにも入らない: 候補者側だけが持つ「本人確認が必要」は、求人側のパネルにない
    assert "needs_confirmation" in json.dumps(panels["a"]["candidate"])
    assert "needs_confirmation" not in json.dumps(panels["a"]["employer"])


@pytest.mark.anyio
async def test_the_demo_activity_log_reads_either_side_from_after_seq_without_a_session(web_app):
    a, _, _ = await _three_demo_negotiations(web_app)
    demo = web_app.browser()

    candidate = await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/activity", side="candidate", after_seq=3)
    employer = await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/activity", side="employer")

    assert candidate.json()["side"] == "candidate"
    assert [e["seq"] for e in candidate.json()["entries"]] == [4, 5]
    assert candidate.json()["next_after_seq"] == 5
    assert employer.json()["side"] == "employer"
    assert [e["action"] for e in employer.json()["entries"]] == ["propose", "final_result"]
    assert (await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/activity", side="somebody")).status_code == 422
    assert (await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/activity")).status_code == 422  # side は必須


@pytest.mark.anyio
async def test_the_demo_panels_take_a_cursor_per_side(web_app):
    # 側ごとに seq の番号が別なので、読み始める位置も側ごとに指定する。
    a, _, _ = await _three_demo_negotiations(web_app)
    demo = web_app.browser()

    response = await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/panels", candidate_after_seq=4, employer_after_seq=1)

    body = response.json()
    assert [e["seq"] for e in body["candidate"]["entries"]] == [5] and body["candidate"]["next_after_seq"] == 5
    assert [e["seq"] for e in body["employer"]["entries"]] == [2] and body["employer"]["next_after_seq"] == 2


@pytest.mark.anyio
async def test_demo_routes_do_not_use_or_extend_the_principal_session(web_app):
    # 有効なクッキーがあっても、デモの経路は利用記録を更新せず、クッキーの期限も延ばさない(本物の依頼者に触れない。§6.3)。
    a, _, _ = await _three_demo_negotiations(web_app)
    browser = web_app.browser()
    pid = await browser.register()
    before = web_app.default_db.collection("principals_meta").document(pid).get().to_dict()
    web_app.clock.advance(dt.timedelta(hours=2))  # 1 時間を過ぎているので、セッションを見るリクエストなら利用記録を更新する

    reads = [
        await browser.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/activity", side="candidate"),
        await browser.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/panels"),
    ]

    assert [r.status_code for r in reads] == [200, 200]
    assert all("set-cookie" not in r.headers for r in reads)
    assert web_app.default_db.collection("principals_meta").document(pid).get().to_dict() == before
    # 対照: セッションを見る経路なら、同じ時刻の進みで利用記録が更新される(上の「変わらない」が、時刻の条件のせいではない)
    assert (await browser.get(f"/v1/principals/{pid}/policy")).status_code == 200
    assert web_app.default_db.collection("principals_meta").document(pid).get().to_dict() != before


_STAGE_DOCUMENTS = {
    "document_missing": None,  # 段の状態がない(まだ作っていない・消えた)
    "field_missing": {"nid": "{nid}", "stage": 0},  # candidate_principal_id の項目が欠けている
    "field_none_looks_fictional": {"nid": "{nid}", "candidate_principal_id": None, "stage": 0},  # 壊れて、架空と読める
    "someone_elses_principal": {"nid": "{nid}", "candidate_principal_id": "0123456789abcdef", "stage": 0},
    "wrong_type": {"nid": "{nid}", "candidate_principal_id": 12345, "stage": 0},
}


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["activity", "panels"])
@pytest.mark.parametrize("variant", list(_STAGE_DOCUMENTS))
async def test_a_real_negotiation_cannot_be_read_from_the_demo_routes_whatever_its_stage_document_says(
    web_app, monkeypatch, route, variant
):
    # DV-01・X-38: 本物の交渉の stages/{nid} が欠けている・壊れているときも、未認証のデモの経路から、本物の依頼者の側の見え方を読めない(403)。
    # field_none_looks_fictional では web の確認〔補助〕は通るので、止めるのは金庫〔正本〕(404 を 403 に写す)。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    _move(web_app.store, nid, "candidate", "check", sample_package())  # 読まれてはならない記録
    stage_ref = web_app.default_db.collection("stages").document(nid)
    document = _STAGE_DOCUMENTS[variant]
    if document is None:
        stage_ref.delete()
    else:
        stage_ref.set({key: nid if value == "{nid}" else value for key, value in document.items()})
    demo = web_app.browser()
    calls = []  # web が金庫のデモ用の口を呼んだ回数(2 段の確認のどちらが止めたかを見る)
    original = web_app.vault.get_demo_events

    async def recording(nid, side, after_seq=0):
        calls.append((nid, side))
        return await original(nid, side, after_seq)

    monkeypatch.setattr(web_app.vault, "get_demo_events", recording)

    params = {"side": "candidate"} if route == "activity" else {}
    response = await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{nid}/{route}", **params)

    assert (response.status_code, response.json()) == (403, {"detail": "forbidden"})
    # web の確認(補助)が先に断ったか(金庫は呼ばれない)、通って金庫(正本)が断ったか(404 を 403 に写した)
    assert len(calls) == (1 if variant == "field_none_looks_fictional" else 0)
    # 対照: 本人は、同じ交渉の自分の側を、本人の経路から読める(読まれてはならない記録は、実際にある)
    own = await browser.get(f"/v1/negotiations/{nid}/activity")
    assert [e["action"] for e in own.json()["entries"]] == ["check"]


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["activity", "panels"])
async def test_unknown_and_malformed_negotiations_are_forbidden_on_the_demo_routes(web_app, route):
    demo = web_app.browser()
    params = {"side": "employer"} if route == "activity" else {}

    for nid in ("0123456789abcdef", "not-a-negotiation-id"):
        response = await demo.get(f"{DEMO_PATH_PREFIX}negotiations/{nid}/{route}", **params)
        assert (response.status_code, response.json()) == (403, {"detail": "forbidden"}), nid


@pytest.mark.anyio
async def test_a_vault_outage_is_503_not_403_and_the_vaults_detail_is_not_returned(web_app, monkeypatch):
    # 金庫が応えないときは、あとで呼び直してよい(503)。権限の 403 に写さない。金庫の detail は返さない。
    from web.vault_client import VaultUnavailableError

    a, _, _ = await _three_demo_negotiations(web_app)
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())

    async def unavailable(*args, **kwargs):
        raise VaultUnavailableError("vault says: secret detail", 503)

    monkeypatch.setattr(web_app.vault, "get_demo_events", unavailable)
    monkeypatch.setattr(web_app.vault, "get_events", unavailable)
    responses = [
        await browser.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/panels"),
        await browser.get(f"{DEMO_PATH_PREFIX}negotiations/{a}/activity", side="candidate"),
        await browser.get(f"/v1/negotiations/{nid}/activity"),
        await browser.get(f"/v1/negotiations/{nid}/panels"),
    ]

    assert [(r.status_code, r.json()) for r in responses] == [(503, {"detail": "temporarily_unavailable"})] * 4
    assert all("secret detail" not in r.text for r in responses)


# ----------------------------------------------------------------------
# 並べて見る画面(本物の利用者の交渉)と、段の参照
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_live_panels_have_only_the_principals_side_and_a_reference_to_the_stage(web_app):
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    _move(web_app.store, nid, "candidate", "check", sample_package())

    response = await browser.get(f"/v1/negotiations/{nid}/panels")

    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"candidate", "employer", "stage"}
    assert body["employer"] is None  # 求人側は読めない(本人に見せるのは本人の側だけ)
    assert [e["action"] for e in body["candidate"]["entries"]] == ["check"]
    assert body["stage"] == {"stage": 0}  # 交渉の作成直後に作る段 0(§6.2)
    later = await browser.get(f"/v1/negotiations/{nid}/panels", candidate_after_seq=1)
    assert later.json()["candidate"] == {"side": "candidate", "entries": [], "next_after_seq": 1}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stage_document", "expected"),
    [
        ({"stage": 1}, {"stage": 1}),
        ({"stage": 2}, {"stage": 2}),
        (None, None),  # 段の状態がない
        ({"nid": "x"}, None),  # 段の番号がない
        ({"stage": "1"}, None),  # 番号が整数でない
        ({"stage": True}, None),
    ],
)
async def test_the_stage_reference_is_the_stage_number_or_null(web_app, stage_document, expected):
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    stage_ref = web_app.default_db.collection("stages").document(nid)
    if stage_document is None:
        stage_ref.delete()
    else:
        stage_ref.set(stage_document)

    response = await browser.get(f"/v1/negotiations/{nid}/panels")

    assert response.status_code == 200  # 段の状態がなくても、記録は読める
    assert response.json()["stage"] == expected


@pytest.mark.anyio
async def test_the_stage_reference_does_not_expose_the_contents_of_the_stage(web_app):
    # 段 1 の職務要約・依頼者 ID・呼び出し数などは、段の参照には出ない(中身は段階開示の段が決める。FR-33・§6.2)。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    web_app.default_db.collection("stages").document(nid).update({"stage": 1, "job_summary": CANARY, "llm_calls": 7})

    response = await browser.get(f"/v1/negotiations/{nid}/panels")

    assert response.json()["stage"] == {"stage": 1}
    for hidden in (CANARY, pid, "job_summary", "llm_calls", "candidate_principal_id"):
        assert hidden not in response.text, hidden


@pytest.mark.anyio
async def test_the_routes_are_in_the_openapi_schema_of_the_web_app(web_app):
    # スキーマの生成が壊れていないこと(/openapi.json・/docs が出せる)と、この API の経路の一覧。
    paths = web_app.app.openapi()["paths"]

    for path in (
        "/v1/negotiations/{nid}/activity",
        "/v1/negotiations/{nid}/panels",
        f"{DEMO_PATH_PREFIX}negotiations/{{nid}}/activity",
        f"{DEMO_PATH_PREFIX}negotiations/{{nid}}/panels",
    ):
        assert list(paths[path]) == ["get"], path


# ----------------------------------------------------------------------
# 本人向けの交渉一覧(§3.3・L7-2)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_principal_negotiation_list_shows_paused_only_for_the_principals_own_pause(web_app):
    # L7-2: 一覧の「一時停止中」は、本人が止めたときだけ出る。ハッカソンで一時停止できるのは本物の候補者だけ(求人側を止める入口はない)。
    browser = web_app.browser()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())

    async def listed() -> dict:
        response = await browser.get(f"/v1/principals/{pid}/negotiations")
        assert response.status_code == 200
        (item,) = response.json()
        assert set(item) == {"nid", "job_id", "created_at", "state", "result"}  # §3.3 の列挙だけ
        assert item["nid"] == nid
        return item

    assert (await listed())["state"] == "active"
    # 求人側を止める入口はない(側は指定できない)。止まらない。
    refused = await browser.post(f"/v1/negotiations/{nid}/control", {"action": "pause", "side": "employer"})
    assert refused.status_code == 422
    assert (await listed())["state"] == "active"
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "pause"})).status_code == 200
    assert (await listed())["state"] == "paused"
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "resume"})).status_code == 200
    assert (await listed())["state"] == "active"
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"})).status_code == 200
    ended = await listed()
    assert (ended["state"], ended["result"]) == ("ended", {"likelihood": "none", "package": None})
