"""DV-02(次の部分。「会う」・段 2 の承認は除く): design.md §3.3・§3.4・§3.5・§4.4。

同じ expected_version の手(check を含む)を並行して 10 本送ると 1 本だけが通り、残りは
何も消費せず 409 になること。上限を超える消費が起きないこと。control(費用の上限での停止
stop_cost_limit を含む。台帳 X-52)と expire は何度呼んでも同じ結果で 409 にならないこと。
accept の直後に取消(や費用の上限での停止)を並行して送っても結果が 2 通りにならないこと。
手・一時停止・再開・期限切れを交互に起こしても記録が上書きされないこと。途中確認の回答が並行・再送で 1 回しか効かず、再試行が尽きたら
409 になることを確かめる。

並行の 10 本の確認は、性質を 2 つに分け、どちらも条件なしで確かめる(台帳 I-5)。エミュレータは粗いロックで、
同じ文書を読んだ多数の呼び出しが一斉に中止され、一斉にやり直す、を繰り返して、10 本とも再試行を使い切る
(409)ことがある。「10 本のうち必ず 1 本通る」は、エミュレータの負荷に左右されるので、テストでは確かめない
(本番の Firestore での確認項目)。
- 安全: 通るのは 1 本以下で、失敗はすべて 409。
- 何も消費しない・詰まらない: 並行の後に、同じ要求を 1 本だけ逐次で送り直す。「並行で通った数 + 逐次で通った数 == 1」で、
  version・評価の残り・記録の数が 1 回ぶんだけ進んでいる。

競合で再試行を使い切った 409 は ContentionExhausted(MovePreconditionFailed の子。HTTP の 409 と detail は同じ)で、
再試行は「内側 5 回 × 外側 4 回」、外側の間に 10〜200 ms の乱数の待ちを入れる。トランザクションの中の読み取りで出た
Aborted も、同じ例外に変換される(L9-1)。

並行呼び出しは、TestClient/ASGI の都合を避けるため VaultStore を直接スレッドから叩く
(状態機械そのものの並行性を確かめるのが目的で、HTTP 層の並行性は対象外)。
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1.document import DocumentReference
from google.cloud.firestore_v1.transaction import Transaction

from vault import store as store_module
from vault.api_models import ControlRequest, MoveRequest, PrincipalAnswerRequest
from vault.errors import ContentionExhausted, MovePreconditionFailed, TransactionRetryExhausted
from vault.models import EmployerRule
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    needs_confirmation_policy,
    put_candidate_and_employer_templates,
    sample_package,
)

_CONCURRENT_REQUESTS = 10


def _create(store, **kwargs):
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db, **kwargs)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def _try_move(store, nid, request):
    # store.process_move は、expected_version の不一致だけでなく、トランザクションの
    # 再試行を使い切った場合(競合。ContentionExhausted)も MovePreconditionFailed(409)にそろえる
    # (vault/store.py の _run_transaction)。成功なら応答、409 ならその例外を返す(ここでの読み替えは行わない)。
    try:
        return store.process_move(nid, request)
    except MovePreconditionFailed as exc:
        return exc


def _is_conflict(result) -> bool:
    return isinstance(result, MovePreconditionFailed)


def test_ten_concurrent_moves_at_the_same_version_let_at_most_one_through_and_a_resend_settles_the_rest(store):
    # DV-02: 同じ expected_version の手(check を含む)を並行して 10 本送る。性質を 2 つに分けて確かめる(台帳 I-5)。
    nid = _create(store)
    request = MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())

    with ThreadPoolExecutor(max_workers=_CONCURRENT_REQUESTS) as pool:
        concurrent = list(pool.map(lambda _: _try_move(store, nid, request), range(_CONCURRENT_REQUESTS)))

    # (1) 安全: 通るのは 1 本以下で、失敗はすべて 409(_try_move は 409 以外の失敗を握りつぶさない)。
    #     エミュレータの競合で 10 本とも再試行を使い切っても(0 本)、この性質は成り立つ。
    assert len(concurrent) == _CONCURRENT_REQUESTS
    concurrent_wins = [r for r in concurrent if not _is_conflict(r)]
    assert len(concurrent_wins) <= 1

    # (2) 何も消費しない・詰まらない: 並行の後に、同じ要求を 1 本だけ、逐次で送り直す。並行で 1 本勝っていれば、
    #     送り直しは 409(古い expected_version)。全員が競合で落ちていれば、送り直しが通る。合わせて、ちょうど 1 回だけ効く。
    resent = _try_move(store, nid, request)
    resent_wins = 0 if _is_conflict(resent) else 1
    assert len(concurrent_wins) + resent_wins == 1

    # 失敗した 9 本(または 10 本)は、何も消費していない: version は 1、評価の残りは 16、記録は 1 件。
    view = store.get_view(nid, "candidate")
    assert view.version == 1
    assert view.budget.remaining_evaluations == 16
    assert len(store.get_events(nid, "candidate")) == 1


def _try_principal_answer(store, nid, request):
    # process_principal_answer も、expected_version の不一致・再試行を使い切った場合の
    # どちらも MovePreconditionFailed(409)にそろえる(手の操作と同じ扱い)。
    try:
        return store.process_principal_answer(nid, request)
    except MovePreconditionFailed as exc:
        return exc


def test_concurrent_principal_answers_at_the_same_version_let_at_most_one_through_and_a_resend_settles_the_rest(store):
    # DV-02: 途中確認の回答も、並行・再送で 1 回しか効かない。手の操作と同じ 2 つの性質に分けて、条件なしで確かめる(台帳 I-5)。
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    package = sample_package()
    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert ask_response.status == "awaiting_principal"
    v = ask_response.version
    request = PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="accept")

    with ThreadPoolExecutor(max_workers=_CONCURRENT_REQUESTS) as pool:
        concurrent = list(pool.map(lambda _: _try_principal_answer(store, nid, request), range(_CONCURRENT_REQUESTS)))

    # (1) 安全: 通るのは 1 本以下で、失敗はすべて 409。
    assert len(concurrent) == _CONCURRENT_REQUESTS
    concurrent_wins = [r for r in concurrent if not _is_conflict(r)]
    assert len(concurrent_wins) <= 1

    # (2) 並行の後に、同じ要求を 1 本だけ逐次で送り直す: 合わせてちょうど 1 回だけ効く(全員が競合で落ちていれば
    #     送り直しが通り、並行で 1 本勝っていれば、古い expected_version なので 409)。
    resent = _try_principal_answer(store, nid, request)
    assert len(concurrent_wins) + (0 if _is_conflict(resent) else 1) == 1

    # 追記が 1 回しか効いていない(コピーの受けるアンカーが 1 件だけ)。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["status"] == "active"
    assert len(doc["snapshots"]["candidate"]["accept_anchors"]) == 1

    # イベントも 1 件だけ残る(答えた側の見え方にだけ)。
    assert len(store.get_events(nid, "candidate")) == 2  # ask_principal 1 件 + principal_answer 1 件


def test_concurrent_bursts_never_let_the_evaluation_budget_go_negative(store):
    # DV-02: 上限を超える消費が起きない。評価回数の残りぶんを超える並行 check を
    # 繰り返し送っても、最終的な消費はちょうど上限(17)で止まり、それ以上は増えない。
    nid = _create(store)
    package = sample_package()

    version = 0
    rounds = 0
    while True:
        view = store.get_view(nid, "candidate")
        if view.budget.remaining_evaluations == 0:
            break
        request = MoveRequest(expected_version=version, side="candidate", move="check", package=package)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: _try_move(store, nid, request), range(3)))
        ok_results = [r for r in results if not _is_conflict(r)]
        assert len(ok_results) <= 1  # この version では、通るのはたかだか 1 件
        if ok_results:
            version = ok_results[0].version
        rounds += 1
        assert rounds < 100  # 無限ループ防止(理論上 17 ラウンドで尽きるはず)

    final_view = store.get_view(nid, "candidate")
    assert final_view.budget.remaining_evaluations == 0
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["counters"]["candidate"]["evaluations_used"] == 17  # 17 を超えない


def _retry_transient(fn, attempts=8):
    """control・expire・作成は、トランザクションの再試行を使い切ると
    TransactionRetryExhausted(503。再試行してよい)を投げる(vault/store.py の
    _run_transaction)。多重の競合下ではまれに起こり得るので、ここではその「再試行して
    よい」という約束どおりに呼び出し全体をもう一度試す。409 になるかどうか
    (確かめたい性質そのもの)は変えない: TransactionRetryExhausted 以外の例外は
    そのまま伝える。
    """
    last_exc = None
    for _ in range(attempts):
        try:
            return fn()
        except TransactionRetryExhausted as exc:
            last_exc = exc
            continue
    raise last_exc


def test_control_and_expire_are_idempotent_under_repetition_and_never_409(store):
    # DV-02: control と expire は何度呼んでも同じ結果で、交渉が進んでいる最中でも 409 に
    # ならない。「何度でも」を、並行呼び出し(4 本)と逐次の繰り返し呼び出しの両方で確かめる。
    nid = _create(store)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: _retry_transient(
                    lambda: store.control(nid, ControlRequest(side="candidate", action="pause"))
                ),
                range(4),
            )
        )
    assert all(r.paused is True for r in results)  # 例外(409)は一切起きない
    paused_version = results[0].version

    # 逐次で何度呼んでも、状態も version も変わらない(冪等)。
    for _ in range(5):
        response = store.control(nid, ControlRequest(side="candidate", action="pause"))
        assert response.paused is True
        assert response.version == paused_version

    for _ in range(5):
        expire_response = store.expire(nid)
        assert expire_response.expired is False  # まだ期限切れではない(一時停止中)
        assert expire_response.version == paused_version

    # resume も同様に何度呼んでも安全(並行 + 逐次)。
    with ThreadPoolExecutor(max_workers=4) as pool:
        resume_results = list(
            pool.map(
                lambda _: _retry_transient(
                    lambda: store.control(nid, ControlRequest(side="candidate", action="resume"))
                ),
                range(4),
            )
        )
    assert all(r.paused is False for r in resume_results)
    resumed_version = resume_results[0].version

    for _ in range(5):
        response = store.control(nid, ControlRequest(side="candidate", action="resume"))
        assert response.paused is False
        assert response.version == resumed_version


def test_stop_cost_limit_is_idempotent_under_repetition_and_never_409(store):
    # DV-02 / 台帳 X-52: stop_cost_limit は、交渉が進んでいる最中でも 409 にならず、何度呼んでも(並行・逐次とも)1 回しか効かない
    # (version が 1 しか進まず、終了の記録が 1 件だけ残る)。
    nid = _create(store)
    moved = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )
    stop = ControlRequest(side="candidate", action="stop_cost_limit")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _retry_transient(lambda: store.control(nid, stop)), range(4)))
    assert all(r.status == "judged" for r in results)  # 例外(409)は一切起きない
    assert {r.version for r in results} == {moved.version + 1}  # どの応答も同じ version: 効いたのは 1 回だけ

    for _ in range(5):  # 逐次で何度呼んでも、状態も version も変わらない(冪等)
        response = store.control(nid, stop)
        assert (response.status, response.version) == ("judged", moved.version + 1)

    doc = store._negotiation_ref(nid).get().to_dict()
    assert (doc["end_reason"], doc["version"]) == ("stopped_cost", moved.version + 1)
    assert len(list(store._events(nid).stream())) == 2  # check と終了の 2 件だけ(終了の記録が重ならない)
    for side in ("candidate", "employer"):
        assert [e.kind for e in store.get_events(nid, side)].count("final_result") == 1


@pytest.mark.parametrize(
    ("action", "stop_reason"), [("cancel", "cancelled"), ("stop_cost_limit", "stopped_cost")]
)
def test_concurrent_accept_and_cancel_or_cost_stop_do_not_produce_two_outcomes(store, action, stop_reason):
    # DV-02: accept の直後に取消(または費用の上限での停止。台帳 X-52)を並行して送っても、結果が 2 通りにならない
    # (judged の理由も、最終記録も、ちょうど 1 つに定まる)。
    nid = _create(store)
    package = sample_package()
    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    v = propose_response.version

    def do_accept():
        return _try_move(store, nid, MoveRequest(expected_version=v, side="employer", move="accept"))

    def do_cancel():
        return store.control(nid, ControlRequest(side="candidate", action=action))

    with ThreadPoolExecutor(max_workers=2) as pool:
        accept_future = pool.submit(do_accept)
        cancel_future = pool.submit(do_cancel)
        accept_result = accept_future.result()
        cancel_result = cancel_future.result()

    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["status"] == "judged"
    assert doc["end_reason"] in ("agreed", stop_reason)  # ちょうど 1 つに定まる

    for side in ("candidate", "employer"):
        final_events = [e for e in store.get_events(nid, side) if e.kind == "final_result"]
        assert len(final_events) == 1  # 二重に記録されていない

    # accept と cancel(または stop_cost_limit)のどちらが勝っても、負けた側は例外(409)か、
    # すでに judged 後の no-op のどちらかであり、勝者の結果を上書きしない。
    if doc["end_reason"] == "agreed":
        assert not _is_conflict(accept_result)
        assert accept_result.status == "judged"
    else:
        assert cancel_result.status == "judged"


def test_interleaved_moves_pause_resume_and_expire_never_overwrite_records(store, clock):
    # DV-02: 手・一時停止・再開・期限切れを交互に起こしても、イベント列の記録が
    # 上書きされず、1 件ずつ残る。
    nid = _create(store, employer_rules=[_wildcard_employer_rule()])
    package = sample_package()

    v = 0
    response = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="check", package=package)
    )
    v = response.version  # 1件目

    pause_response = store.control(nid, ControlRequest(side="candidate", action="pause"))  # 2件目
    v = pause_response.version

    expire_response = store.expire(nid)  # 一時停止中でまだ期限切れではないので no-op(記録なし)
    assert expire_response.expired is False

    resume_response = store.control(nid, ControlRequest(side="candidate", action="resume"))  # 3件目
    v = resume_response.version

    response = store.process_move(
        nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=package)
    )
    v = response.version  # 4件目(propose は双方に見えるが、記録は 1 件)

    response = store.process_move(nid, MoveRequest(expected_version=v, side="employer", move="reject"))
    v = response.version  # 5件目

    clock.advance(dt.timedelta(days=10))
    expire_response = store.expire(nid)  # 6件目(今度は期限切れになる)
    assert expire_response.expired is True

    events_col = list(store._events(nid).stream())
    versions = sorted(e.to_dict()["version"] for e in events_col)
    assert versions == list(range(1, 7))  # 6 件、重複も欠けもない
    assert len(versions) == len(set(versions))


def _record_backoff_waits(store, monkeypatch) -> list[float]:
    """外側の再試行の間の待ち(乱数の 10〜200 ms)を、実際には待たずに記録する(テストは sleep しない)。"""
    waits: list[float] = []
    monkeypatch.setattr(store, "_sleep", waits.append)
    return waits


def _abort_every_commit(monkeypatch) -> list[int]:
    """コミットを常に Aborted で失敗させ、再試行が尽きる場面を決定的に再現する(実際の多重の競合を待たずに、
    確実に起こす)。コミットを試みた回数を数える(戻り値の長さ)。
    """
    attempts: list[int] = []

    def _always_aborted(self):
        attempts.append(1)
        raise Aborted("simulated contention")

    monkeypatch.setattr(Transaction, "_commit", _always_aborted)
    return attempts


def test_moves_return_409_when_transaction_retries_are_exhausted(api_client, store, monkeypatch):
    # 差し戻し対応 2: 再試行を使い切った場合の手の操作(moves)は、expected_version の
    # 不一致と同じ 409(MovePreconditionFailed)にする。何も消費していないことも保つ。
    # 台帳 I-5: 使い切ったことは、子クラス ContentionExhausted で見分けられる。HTTP の 409 と detail は同じ。
    nid = _create(store)
    package = sample_package()
    _record_backoff_waits(store, monkeypatch)
    _abort_every_commit(monkeypatch)

    with pytest.raises(ContentionExhausted) as exhausted:
        store.process_move(
            nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
        )
    assert isinstance(exhausted.value, MovePreconditionFailed)  # 呼び出し側から見た扱いは、これまでどおり

    http_response = api_client.post(
        f"/v1/negotiations/{nid}/moves",
        json={
            "expected_version": 0,
            "side": "candidate",
            "move": "check",
            "package": package.model_dump(mode="json"),
        },
    )
    assert http_response.status_code == 409
    assert http_response.json() == {"detail": "transaction retries exhausted under contention"}

    monkeypatch.undo()  # 以降の読み出しは、正常なコミット経路(get)で確かめる

    # 何も消費していない(version・評価回数のどちらも変わっていない)。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["version"] == 0
    assert doc["counters"]["candidate"]["evaluations_used"] == 0


def test_a_version_mismatch_is_a_plain_409_and_not_a_contention_exhaustion(store):
    # 台帳 I-5: expected_version の不一致の 409 は、競合で使い切った 409(ContentionExhausted)ではない。
    # 二つはテストとログで区別できる(どちらも MovePreconditionFailed で、HTTP の 409 は同じ)。
    nid = _create(store)
    request = MoveRequest(expected_version=99, side="candidate", move="check", package=sample_package())

    with pytest.raises(MovePreconditionFailed) as mismatch:
        store.process_move(nid, request)

    assert not isinstance(mismatch.value, ContentionExhausted)


def test_contention_is_retried_in_four_rounds_of_five_attempts_with_a_random_wait_between_rounds(store, monkeypatch):
    # 台帳 I-5: 再試行は「内側 5 回 × 外側 4 回」(計 20 回)。外側の間(3 回)に、10〜200 ms の乱数の待ちを入れる
    # (エミュレータで、全員が一斉にやり直すのを崩すため)。待ちの前後で、同じ操作を新しいトランザクションでやり直す。
    nid = _create(store)
    waits = _record_backoff_waits(store, monkeypatch)
    attempts = _abort_every_commit(monkeypatch)

    with pytest.raises(ContentionExhausted):
        store.process_move(
            nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
        )

    assert len(attempts) == 5 * 4  # コミットを試みた回数(内側 5 回 × 外側 4 回)
    assert len(waits) == 3  # 外側の間(1 回目の前には待たない)
    assert all(0.010 <= wait <= 0.200 for wait in waits)


def test_the_backoff_wait_is_random(store, monkeypatch):
    # 台帳 I-5: 待ちは乱数(同じ長さで、一斉にやり直さない)。何度も引いて、すべて同じ値にはならない。
    nid = _create(store)
    waits = _record_backoff_waits(store, monkeypatch)
    _abort_every_commit(monkeypatch)

    for _ in range(4):
        with pytest.raises(ContentionExhausted):
            store.process_move(
                nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
            )

    assert len(waits) == 12
    assert len(set(waits)) > 1


def _fake_transactional(monkeypatch, failures: list):
    """firestore.transactional を、呼ぶたびに failures の先頭の例外を投げ、尽きたら txn_fn の結果を返すものに差し替える。

    競合(Aborted)が続く場面を、エミュレータの負荷に頼らず、決定的に作る(実際のトランザクションは開かない)。
    戻り値は、呼ばれた回数を数えるリスト。
    """
    calls: list[int] = []

    def transactional(txn_fn):
        def run(transaction):
            calls.append(1)
            if failures:
                raise failures.pop(0)
            return txn_fn(transaction)

        return run

    monkeypatch.setattr(store_module.firestore, "transactional", transactional)
    return calls


def _rounds_exhausted() -> ValueError:
    """google-cloud-firestore が、内側の再試行を使い切ったときに投げるもの(Aborted を包んだ ValueError)。"""
    error = ValueError("Failed to commit transaction in 5 attempts.")
    error.__cause__ = Aborted("simulated contention")
    return error


def test_a_later_round_recovers_after_earlier_rounds_were_exhausted(store, monkeypatch):
    # 台帳 I-5: 内側の 5 回(1 ラウンド)を使い切っても、乱数の待ちの後の次のラウンドで通れば、エラーにならない
    # (一斉のやり直しが続く場面の、外側の再試行の効き目)。待ちは、ラウンドの間の 2 回。
    waits = _record_backoff_waits(store, monkeypatch)
    calls = _fake_transactional(monkeypatch, [_rounds_exhausted(), _rounds_exhausted()])

    result = store._run_transaction(lambda txn: "committed", contention_error=ContentionExhausted)

    assert result == "committed"
    assert len(calls) == 3 and len(waits) == 2
    assert all(0.010 <= wait <= 0.200 for wait in waits)


def test_an_aborted_raised_directly_and_an_exhausted_round_are_both_retried_and_then_converted(store, monkeypatch):
    # L9-1 / 台帳 I-5: 競合の出方は 2 つ(内側を使い切った ValueError と、読み取りで出た Aborted)。どちらも外側の
    # 再試行の対象で、4 ラウンドとも尽きたら、指定された例外(ここでは 503 の系統)に変換する。元の例外が原因として残る。
    waits = _record_backoff_waits(store, monkeypatch)
    calls = _fake_transactional(
        monkeypatch, [Aborted("read"), _rounds_exhausted(), Aborted("read"), _rounds_exhausted()]
    )

    with pytest.raises(TransactionRetryExhausted) as exhausted:
        store._run_transaction(lambda txn: "committed", contention_error=TransactionRetryExhausted)

    assert len(calls) == 4 and len(waits) == 3
    assert isinstance(exhausted.value.__cause__, (Aborted, ValueError))


def test_an_unrelated_error_is_not_retried_or_converted(store, monkeypatch):
    # 競合ではない失敗(競合を表さない ValueError・そのほかの例外)は、再試行も変換もせず、そのまま上げる(バグを隠さない)。
    waits = _record_backoff_waits(store, monkeypatch)
    calls = _fake_transactional(monkeypatch, [ValueError("a plain bug, not an Aborted")])

    with pytest.raises(ValueError, match="a plain bug"):
        store._run_transaction(lambda txn: "committed", contention_error=ContentionExhausted)

    assert (len(calls), waits) == (1, [])

    calls = _fake_transactional(monkeypatch, [KeyError("another bug")])
    with pytest.raises(KeyError):
        store._run_transaction(lambda txn: "committed", contention_error=ContentionExhausted)
    assert (len(calls), waits) == (1, [])


def test_principal_answer_returns_409_when_transaction_retries_are_exhausted(api_client, store, monkeypatch):
    # DV-02: principal-answer も、手の操作と同じ扱いで、再試行を使い切ったときは 409 にする
    # (control・expire・作成とは違い、503 にはしない)。台帳 I-5: 子クラス ContentionExhausted。
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    package = sample_package()
    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    v = ask_response.version
    _record_backoff_waits(store, monkeypatch)
    _abort_every_commit(monkeypatch)

    with pytest.raises(ContentionExhausted):
        store.process_principal_answer(
            nid, PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="accept")
        )

    http_response = api_client.post(
        f"/v1/negotiations/{nid}/principal-answer",
        json={
            "expected_version": v,
            "side": "candidate",
            "package": package.model_dump(mode="json"),
            "answer": "accept",
        },
    )
    assert http_response.status_code == 409
    assert http_response.json() == {"detail": "transaction retries exhausted under contention"}

    monkeypatch.undo()

    # 何も消費していない(status・version のどちらも変わっていない)。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["status"] == "awaiting_principal"
    assert doc["version"] == v


def test_control_returns_503_when_transaction_retries_are_exhausted(api_client, store, monkeypatch):
    # 差し戻し対応 2: 冪等な操作(control)は、再試行を使い切っても 409 にしない
    # (DV-02「409 にならない」)。再試行してよいことが分かる 503 にする。
    nid = _create(store)
    _record_backoff_waits(store, monkeypatch)
    _abort_every_commit(monkeypatch)

    with pytest.raises(TransactionRetryExhausted) as exhausted:
        store.control(nid, ControlRequest(side="candidate", action="pause"))
    assert not isinstance(exhausted.value, MovePreconditionFailed)  # 409 の系統ではない

    http_response = api_client.post(
        f"/v1/negotiations/{nid}/control", json={"side": "candidate", "action": "pause"}
    )
    assert http_response.status_code == 503

    monkeypatch.undo()

    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["paused"] is False  # 何も適用されていない


def test_creation_returns_503_when_transaction_retries_are_exhausted(api_client, store, monkeypatch):
    # 差し戻し対応 2: 作成も、control・expire と同じく 409 にはせず 503 にする。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    _record_backoff_waits(store, monkeypatch)
    _abort_every_commit(monkeypatch)

    with pytest.raises(TransactionRetryExhausted):
        store.create_negotiation(
            demo_create_request(candidate_template.template_id, employer_template.template_id)
        )

    http_response = api_client.post(
        "/v1/negotiations",
        json={
            "request_id": "concurrency-503-test",
            "mode": "demo",
            "candidate": {"is_fictional": True, "template_id": candidate_template.template_id},
            "employer": {"template_id": employer_template.template_id},
        },
    )
    assert http_response.status_code == 503


# --- 台帳 L9-1: トランザクションの中の読み取りで出た Aborted ---
#
# google-cloud-firestore は、コミットの Aborted だけを再試行し、使い切ると ValueError に包む。トランザクションの中の
# 読み取り(get(transaction=...))で出た Aborted は、再試行も変換もされずにそのまま上がり、以前は 500 になり得た。
# 今は、再試行を使い切ったときと同じ例外に変換する(手の操作は 409、冪等な操作と作成は 503)。


def _abort_every_transactional_read(monkeypatch, failures: int | None = None) -> list[int]:
    """トランザクションの中の読み取りを、最初の failures 回だけ(None ならいつも)Aborted で失敗させる。読み取りの回数を返す。"""
    real_get = DocumentReference.get
    reads: list[int] = []

    def get(self, *args, **kwargs):
        if kwargs.get("transaction") is not None:
            reads.append(1)
            if failures is None or len(reads) <= failures:
                raise Aborted("simulated contention on a read")
        return real_get(self, *args, **kwargs)

    monkeypatch.setattr(DocumentReference, "get", get)
    return reads


@pytest.mark.parametrize(
    ("operation", "expected_error", "expected_status"),
    [
        ("move", ContentionExhausted, 409),
        ("principal_answer", ContentionExhausted, 409),
        ("control", TransactionRetryExhausted, 503),
        ("expire", TransactionRetryExhausted, 503),
        ("create", TransactionRetryExhausted, 503),
    ],
)
def test_an_aborted_read_inside_the_transaction_becomes_the_same_error_as_exhausted_retries(
    api_client, store, monkeypatch, operation, expected_error, expected_status
):
    # L9-1: 読み取りで出た Aborted も、再試行(外側 4 回)を使い切ったときと同じ例外・同じ HTTP ステータスになる。
    # 変換されないと、金庫は 500 を返し、web のレフェリーのタスクが落ちる。
    package = sample_package()
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=needs_confirmation_policy("candidate")
    )
    nid = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    ).nid
    version = 0
    if operation == "principal_answer":
        version = store.process_move(
            nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
        ).version

    def direct():
        if operation == "move":
            return store.process_move(
                nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
            )
        if operation == "principal_answer":
            return store.process_principal_answer(
                nid, PrincipalAnswerRequest(expected_version=version, side="candidate", package=package, answer="accept")
            )
        if operation == "control":
            return store.control(nid, ControlRequest(side="candidate", action="pause"))
        if operation == "expire":
            return store.expire(nid)
        return store.create_negotiation(
            demo_create_request(candidate_template.template_id, employer_template.template_id, request_id="l9-1")
        )

    def over_http():
        if operation == "move":
            return api_client.post(
                f"/v1/negotiations/{nid}/moves",
                json={"expected_version": 0, "side": "candidate", "move": "check", "package": package.model_dump(mode="json")},
            )
        if operation == "principal_answer":
            return api_client.post(
                f"/v1/negotiations/{nid}/principal-answer",
                json={
                    "expected_version": version,
                    "side": "candidate",
                    "package": package.model_dump(mode="json"),
                    "answer": "accept",
                },
            )
        if operation == "control":
            return api_client.post(f"/v1/negotiations/{nid}/control", json={"side": "candidate", "action": "pause"})
        if operation == "expire":
            return api_client.post(f"/v1/negotiations/{nid}/expire")
        return api_client.post(
            "/v1/negotiations",
            json={
                "request_id": "l9-1-http",
                "mode": "demo",
                "candidate": {"is_fictional": True, "template_id": candidate_template.template_id},
                "employer": {"template_id": employer_template.template_id},
            },
        )

    _record_backoff_waits(store, monkeypatch)
    reads = _abort_every_transactional_read(monkeypatch)

    with pytest.raises(expected_error):
        direct()
    assert len(reads) == 4  # 外側の 4 ラウンドで、読み取りは 1 ラウンドに 1 回(最初の読み取りで失敗する)
    assert over_http().status_code == expected_status

    monkeypatch.undo()
    doc = store._negotiation_ref(nid).get().to_dict()  # 何も適用されていない
    assert (doc["version"], doc["paused"]) == (version, False)


def test_a_transiently_aborted_read_is_retried_and_the_operation_takes_effect_once(store, monkeypatch):
    # L9-1: 読み取りの Aborted は、外側の再試行の対象でもある。最初の 3 回が Aborted でも、4 回目で通れば、
    # エラーにならず、1 回だけ効く。
    nid = _create(store)
    waits = _record_backoff_waits(store, monkeypatch)
    reads = _abort_every_transactional_read(monkeypatch, failures=3)

    response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )

    assert response.valid is True
    assert len(waits) == 3 and len(reads) >= 4
    monkeypatch.undo()
    assert store.get_view(nid, "candidate").version == 1
    assert len(store.get_events(nid, "candidate")) == 1


def _wildcard_employer_rule():
    from vault.models import EmployerRule

    return EmployerRule(when={}, policy=accept_all_policy("employer"))
