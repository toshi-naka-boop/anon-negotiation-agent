"""DV-02(次の部分。「会う」・段 2 の承認は除く): design.md §3.3・§3.4・§3.5・§4.4。

同じ expected_version の手(check を含む)を並行して 10 本送ると 1 本だけが通り、残りは
何も消費せず 409 になること。上限を超える消費が起きないこと。control と expire は
何度呼んでも同じ結果で 409 にならないこと。accept の直後に取消を並行して送っても
結果が 2 通りにならないこと。手・一時停止・再開・期限切れを交互に起こしても記録が
上書きされないこと。途中確認の回答が並行・再送で 1 回しか効かず、再試行が尽きたら
409 になることを確かめる。

並行呼び出しは、TestClient/ASGI の都合を避けるため VaultStore を直接スレッドから叩く
(状態機械そのものの並行性を確かめるのが目的で、HTTP 層の並行性は対象外)。
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1.transaction import Transaction

from vault.api_models import ControlRequest, MoveRequest, PrincipalAnswerRequest
from vault.errors import MovePreconditionFailed, TransactionRetryExhausted
from vault.models import EmployerRule
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    needs_confirmation_policy,
    put_candidate_and_employer_templates,
    sample_package,
)

# store.py の _new_transaction のコメントのとおり、エミュレータの粗いロック実装は
# 同一文書への高い並行度で顕著になる。process_principal_answer では 10 本だと、まれに
# 全員が再試行を使い切って 0 勝(0 == 1 で失敗)になることが確認できたため、
# 「1 本だけ勝ち、残りは 409」という性質自体は変えずに、並行数だけ落とす。
_CONCURRENT_ANSWERS = 5


def _create(store, **kwargs):
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db, **kwargs)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def _try_move(store, nid, request):
    # store.process_move は、expected_version の不一致だけでなく、トランザクションの
    # 再試行を使い切った場合(競合)も MovePreconditionFailed(409)にそろえる
    # (vault/store.py の _run_transaction。差し戻し対応: ここでの読み替えは行わない)。
    try:
        return store.process_move(nid, request)
    except MovePreconditionFailed:
        return "409"


def test_ten_concurrent_moves_at_the_same_version_only_one_succeeds(store):
    # DV-02: 同じ expected_version の手(check を含む)を並行して 10 本送ると、1 本だけが
    # 通り、残りは何も消費せず 409 になる。
    nid = _create(store)
    package = sample_package()
    request = MoveRequest(expected_version=0, side="candidate", move="check", package=package)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: _try_move(store, nid, request), range(10)))

    ok_results = [r for r in results if r != "409"]
    conflict_results = [r for r in results if r == "409"]
    assert len(ok_results) == 1
    assert len(conflict_results) == 9

    # 何も消費していない: 評価回数はちょうど 1 回分だけ減っている。
    view = store.get_view(nid, "candidate")
    assert view.budget.remaining_evaluations == 15
    assert view.version == 1

    # イベントも 1 件だけ残る。
    assert len(store.get_events(nid, "candidate")) == 1


def _try_principal_answer(store, nid, request):
    # process_principal_answer も、expected_version の不一致・再試行を使い切った場合の
    # どちらも MovePreconditionFailed(409)にそろえる(手の操作と同じ扱い)。
    try:
        return store.process_principal_answer(nid, request)
    except MovePreconditionFailed:
        return "409"


def test_concurrent_principal_answers_at_the_same_version_only_one_succeeds(store):
    # DV-02: 途中確認の回答も、並行・再送で 1 回しか効かない。残りは何も消費せず 409 になる。
    # 並行度は、手の操作の同種テスト(10 本)より抑える(_CONCURRENT_ANSWERS を参照)。
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    package = sample_package()
    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert ask_response.status == "awaiting_principal"
    v = ask_response.version

    request = PrincipalAnswerRequest(expected_version=v, side="candidate", package=package, answer="accept")
    with ThreadPoolExecutor(max_workers=_CONCURRENT_ANSWERS) as pool:
        results = list(pool.map(lambda _: _try_principal_answer(store, nid, request), range(_CONCURRENT_ANSWERS)))

    ok_results = [r for r in results if r != "409"]
    conflict_results = [r for r in results if r == "409"]
    assert len(ok_results) == 1
    assert len(conflict_results) == _CONCURRENT_ANSWERS - 1

    # 再送(同じリクエストをもう一度、逐次で)しても、古い expected_version なので 409。
    assert _try_principal_answer(store, nid, request) == "409"

    # 追記が 1 回しか効いていない(コピーの受けるアンカーが 1 件だけ)。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["status"] == "active"
    assert len(doc["snapshots"]["candidate"]["accept_anchors"]) == 1

    # イベントも 1 件だけ残る(答えた側の見え方にだけ)。
    assert len(store.get_events(nid, "candidate")) == 2  # ask_principal 1 件 + principal_answer 1 件


def test_concurrent_bursts_never_let_the_evaluation_budget_go_negative(store):
    # DV-02: 上限を超える消費が起きない。評価回数の残りぶんを超える並行 check を
    # 繰り返し送っても、最終的な消費はちょうど上限(16)で止まり、それ以上は増えない。
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
        ok_results = [r for r in results if r != "409"]
        assert len(ok_results) <= 1  # この version では、通るのはたかだか 1 件
        if ok_results:
            version = ok_results[0].version
        rounds += 1
        assert rounds < 100  # 無限ループ防止(理論上 16 ラウンドで尽きるはず)

    final_view = store.get_view(nid, "candidate")
    assert final_view.budget.remaining_evaluations == 0
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["counters"]["candidate"]["evaluations_used"] == 16  # 16 を超えない


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


def test_concurrent_accept_and_cancel_do_not_produce_two_outcomes(store):
    # DV-02: accept の直後に取消を並行して送っても、結果が 2 通りにならない
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
        return store.control(nid, ControlRequest(side="candidate", action="cancel"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        accept_future = pool.submit(do_accept)
        cancel_future = pool.submit(do_cancel)
        accept_result = accept_future.result()
        cancel_result = cancel_future.result()

    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["status"] == "judged"
    assert doc["end_reason"] in ("agreed", "cancelled")  # ちょうど 1 つに定まる

    for side in ("candidate", "employer"):
        final_events = [e for e in store.get_events(nid, side) if e.kind == "final_result"]
        assert len(final_events) == 1  # 二重に記録されていない

    # accept と cancel のどちらが勝っても、負けた側は例外(409)か、すでに judged 後の
    # no-op のどちらかであり、勝者の結果を上書きしない。
    if doc["end_reason"] == "agreed":
        assert accept_result != "409"
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


def test_moves_return_409_when_transaction_retries_are_exhausted(api_client, store, monkeypatch):
    # 差し戻し対応 2: 再試行を使い切った場合の手の操作(moves)は、expected_version の
    # 不一致と同じ 409(MovePreconditionFailed)にする。何も消費していないことも保つ。
    # コミットを常に Aborted で失敗させ、再試行が尽きる場面を決定的に再現する
    # (実際の多重の競合を待つのではなく、確実に起こす)。
    nid = _create(store)
    package = sample_package()

    def _always_aborted(self):
        raise Aborted("simulated contention")

    monkeypatch.setattr(Transaction, "_commit", _always_aborted)

    with pytest.raises(MovePreconditionFailed):
        store.process_move(
            nid, MoveRequest(expected_version=0, side="candidate", move="check", package=package)
        )

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

    monkeypatch.undo()  # 以降の読み出しは、正常なコミット経路(get)で確かめる

    # 何も消費していない(version・評価回数のどちらも変わっていない)。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["version"] == 0
    assert doc["counters"]["candidate"]["evaluations_used"] == 0


def test_principal_answer_returns_409_when_transaction_retries_are_exhausted(api_client, store, monkeypatch):
    # DV-02: principal-answer も、手の操作と同じ扱いで、再試行を使い切ったときは 409 にする
    # (control・expire・作成とは違い、503 にはしない)。
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    package = sample_package()
    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    v = ask_response.version

    def _always_aborted(self):
        raise Aborted("simulated contention")

    monkeypatch.setattr(Transaction, "_commit", _always_aborted)

    with pytest.raises(MovePreconditionFailed):
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

    monkeypatch.undo()

    # 何も消費していない(status・version のどちらも変わっていない)。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert doc["status"] == "awaiting_principal"
    assert doc["version"] == v


def test_control_returns_503_when_transaction_retries_are_exhausted(api_client, store, monkeypatch):
    # 差し戻し対応 2: 冪等な操作(control)は、再試行を使い切っても 409 にしない
    # (DV-02「409 にならない」)。再試行してよいことが分かる 503 にする。
    nid = _create(store)

    def _always_aborted(self):
        raise Aborted("simulated contention")

    monkeypatch.setattr(Transaction, "_commit", _always_aborted)

    with pytest.raises(TransactionRetryExhausted):
        store.control(nid, ControlRequest(side="candidate", action="pause"))

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

    def _always_aborted(self):
        raise Aborted("simulated contention")

    monkeypatch.setattr(Transaction, "_commit", _always_aborted)

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


def _wildcard_employer_rule():
    from vault.models import EmployerRule

    return EmployerRule(when={}, policy=accept_all_policy("employer"))
