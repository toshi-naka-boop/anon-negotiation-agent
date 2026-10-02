"""§3.4 control の stop_cost_limit(台帳 X-52): DV-02(control の冪等性)・AC-08・AC-17 の金庫の側。

web の費用の歯止め(§8.2。LLM の物理の呼び出し数の、1 日・交渉ごとの上限)に達した交渉を、何も決まらなかった結果(「なし」)で
終わらせる、金庫の冪等な操作。手の end を流用すると、一時停止中・途中確認中・409 の最中には効かないので、取消・期限切れと同じ
種類(expected_version を取らない)の操作にした。FR-12・AC-06 の停止の判定(回数・手数・連続無効手。tests/test_stop_rule.py)とは
別で、秘密の値には依存しない。

確かめること: active・paused・awaiting_principal のどこからでも「なし」で終わること(コピー(snapshots)が消え、最終記録が双方に
同じ内容で 1 件だけ残り、version は 1 進む)、2 回目以降の呼び出しは何も変えないこと、すでに judged なら何もしない(合意の結果を
上書きしない)こと、終わった後にレフェリーが登録しようとした手は 409 になること、本物の候補者の交渉でも同じに終わること。
並行・再送(tests/test_concurrency.py)と HTTP の口(tests/test_api_smoke.py)は、それぞれのファイルで確かめる。
"""

import datetime as dt

import pytest

from vault.api_models import ControlRequest, MoveRequest
from vault.errors import MovePreconditionFailed
from vault.models import NegotiationResult
from vault.templates import put_template
from vault_helpers import (
    demo_create_request,
    live_create_request,
    make_employer_template,
    needs_confirmation_policy,
    new_id,
    put_candidate_and_employer_templates,
    put_candidate_policy,
    sample_package,
)

_STOP = ControlRequest(side="candidate", action="stop_cost_limit")
_NO_RESULT = NegotiationResult(likelihood="none", package=None)


def _create(store, candidate_policy=None):
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=candidate_policy
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def _events_of_both_sides(store, nid):
    return {side: store.get_events(nid, side) for side in ("candidate", "employer")}


# --- 終わっていない交渉の、3 つの状態(§3.1: active・paused(active のまま立つ)・awaiting_principal) ---


def _active(store):
    """交渉が進んでいる最中(候補者が check を 1 回。version は 1)。"""
    nid = _create(store)
    store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package()))
    return nid


def _paused(store):
    nid = _create(store)
    store.control(nid, ControlRequest(side="candidate", action="pause"))
    return nid


def _awaiting_principal(store):
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=sample_package())
    )
    return nid


@pytest.mark.parametrize(
    ("make_negotiation", "expected_state", "candidate_kinds"),
    [
        (_active, ("active", False), ["check", "final_result"]),
        (_paused, ("active", True), ["pause", "final_result"]),
        (_awaiting_principal, ("awaiting_principal", False), ["ask_principal", "final_result"]),
    ],
    ids=["active", "paused", "awaiting_principal"],
)
def test_stop_cost_limit_ends_the_negotiation_with_no_result_from_every_open_state(
    store, make_negotiation, expected_state, candidate_kinds
):
    # §3.4: active・paused・awaiting_principal のどれからでも、終了処理(stopped_cost)で「なし」になる。
    nid = make_negotiation(store)
    before = store.get_view(nid, "candidate")
    assert (before.status, before.paused) == expected_state  # 想定した状態から始めている

    response = store.control(nid, _STOP)

    assert (response.status, response.version) == ("judged", before.version + 1)  # 終了の記録は 1 件なので、version は 1 進む
    doc = store._negotiation_ref(nid).get().to_dict()
    assert (doc["status"], doc["end_reason"], doc["version"]) == ("judged", "stopped_cost", before.version + 1)
    assert doc["result"] == {"likelihood": "none", "package": None}
    assert doc["snapshots"] is None  # FR-14・AC-17: 終了処理で、ポリシーの交渉用コピーが消える
    assert doc["deadline"] is None

    # AC-08: 最終記録は双方に 1 件だけで、中身は同じ「なし」だけ(理由を含まない)。相手には、終了のほかに何も見えない。
    events = _events_of_both_sides(store, nid)
    assert [e.kind for e in events["candidate"]] == candidate_kinds
    assert [e.kind for e in events["employer"]] == ["final_result"]
    assert events["candidate"][-1].result == events["employer"][-1].result == _NO_RESULT
    assert (events["candidate"][-1].reason, events["employer"][-1].reason) == (None, None)
    assert [e.seq for e in events["candidate"]] == [1, 2]
    assert [e.seq for e in events["employer"]] == [1]  # 相手に見えない操作(確認・一時停止・途中確認)の分は、番号が飛ばない


def test_stop_cost_limit_does_not_depend_on_the_side_given(store):
    # cancel と同じく、side は使わない(§3.4。画面から呼べないようにするのは web の責務で、金庫は区別しない)。
    nid = _active(store)

    response = store.control(nid, ControlRequest(side="employer", action="stop_cost_limit"))

    assert response.status == "judged"
    assert store._negotiation_ref(nid).get().to_dict()["end_reason"] == "stopped_cost"
    events = _events_of_both_sides(store, nid)
    assert [e.kind for e in events["employer"]] == ["final_result"]
    assert [e.kind for e in events["candidate"]] == ["check", "final_result"]


# --- 2 回目以降・終わった後 ---


def test_a_repeated_stop_cost_limit_changes_nothing(store):
    # §3.3・§3.4: 冪等な操作。2 回目以降は、version も文書も記録も変えない(最終記録は 1 件のまま)。
    nid = _active(store)
    first = store.control(nid, _STOP)
    doc_after_first = store._negotiation_ref(nid).get().to_dict()
    record_count_after_first = len(list(store._events(nid).stream()))
    assert record_count_after_first == 2  # check と終了

    for _ in range(3):
        again = store.control(nid, _STOP)
        assert (again.status, again.version) == (first.status, first.version)  # version は進まない

    assert store._negotiation_ref(nid).get().to_dict() == doc_after_first
    assert len(list(store._events(nid).stream())) == record_count_after_first
    for side, events in _events_of_both_sides(store, nid).items():
        assert [e.kind for e in events].count("final_result") == 1, side


def _agreed(store, clock):
    nid = _create(store)
    proposed = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=sample_package())
    )
    store.process_move(nid, MoveRequest(expected_version=proposed.version, side="employer", move="accept"))
    return nid


def _cancelled(store, clock):
    nid = _create(store)
    store.control(nid, ControlRequest(side="candidate", action="cancel"))
    return nid


def _timed_out(store, clock):
    nid = _create(store)
    clock.advance(dt.timedelta(days=10))  # 寿命(72 時間)を大きく超える
    store.expire(nid)
    return nid


@pytest.mark.parametrize(
    ("end", "end_reason", "likelihood"),
    [(_agreed, "agreed", "high"), (_cancelled, "cancelled", "none"), (_timed_out, "timeout", "none")],
    ids=["agreed", "cancelled", "timeout"],
)
def test_stop_cost_limit_after_the_negotiation_has_ended_does_nothing(store, clock, end, end_reason, likelihood):
    # §3.4「judged でなければ」: すでに終わっていれば何もしない。とくに、合意の結果を「なし」で上書きしない。
    nid = end(store, clock)
    doc_before = store._negotiation_ref(nid).get().to_dict()
    assert (doc_before["end_reason"], doc_before["result"]["likelihood"]) == (end_reason, likelihood)  # 想定の終わり方から始めている
    events_before = _events_of_both_sides(store, nid)

    response = store.control(nid, _STOP)

    assert (response.status, response.version) == ("judged", doc_before["version"])  # version は進まない
    assert store._negotiation_ref(nid).get().to_dict() == doc_before  # 終了の理由も結果も、変わらない
    assert _events_of_both_sides(store, nid) == events_before


def test_a_move_the_referee_registers_after_the_stop_is_refused_with_409_and_records_nothing(store):
    # §3.4: control は expected_version を取らないので、進行中でも 409 にならない。状態を変えたときは version を進めるので、
    # その直後にレフェリーが(古い version のまま)登録しようとした手は 409 になり、何も記録されない。
    nid = _active(store)  # version 1。レフェリーは、ここまで読んで手を作っている
    store.control(nid, _STOP)
    record_count = len(list(store._events(nid).stream()))

    with pytest.raises(MovePreconditionFailed):
        store.process_move(
            nid, MoveRequest(expected_version=1, side="candidate", move="check", package=sample_package())
        )

    assert len(list(store._events(nid).stream())) == record_count
    assert store._negotiation_ref(nid).get().to_dict()["end_reason"] == "stopped_cost"


# --- 本物の候補者の交渉 ---


def test_stop_cost_limit_on_a_live_negotiation_ends_it_and_keeps_the_reserved_budget(store):
    # 本物の候補者の交渉でも、同じに終わる(本物の参加者の削除中ガードを通る)。本人の一覧には、終わったことと「なし」だけが
    # 見える(終了の理由は返らない。§3.3)。作成で予約した 1 日の評価予算は、戻らない(AC-06: 交渉を終えても回復しない)。
    pid = new_id("principal")
    put_candidate_policy(store, pid)
    employer_template = make_employer_template()
    put_template(store._db, employer_template)
    created = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert created.status == "created"
    reserved = store._principal_ref(pid).get().to_dict()["evaluation_budget"]["used"]

    response = store.control(created.nid, _STOP)

    assert response.status == "judged"
    (summary,) = store.list_principal_negotiations(pid)
    assert (summary.state, summary.result) == ("ended", _NO_RESULT)
    assert store._principal_ref(pid).get().to_dict()["evaluation_budget"]["used"] == reserved
