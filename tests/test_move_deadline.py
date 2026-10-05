"""手番ごとの期限の付け直し(design.md §3.4「to_move や status が変わるたびに付け直す」。台帳 C-37・C-39 の (a))。

手番の期限は 5 分、途中確認の期限は 24 時間。手番(to_move)か status が変わる手(propose・reject・
ask_principal)と、途中確認の回答は、期限を今から付け直す(寿命 expires_at は超えない)。手番も status も変わらない手
(check・無効手)は、付け直さない。

時計は注入(FixedClock)で、テストは sleep せずに進める。以前のテストは、手の間で時計を進めなかったため、
「手番が変わっても期限を付け直さない」実装(作成から 5 分で必ず期限切れになる)でも、すべて通ってしまった。
"""

import dataclasses
import datetime as dt

import pytest

from vault.api_models import MoveRequest, PrincipalAnswerRequest
from vault.config import DEFAULT_VAULT_CONFIG
from vault.store import VaultStore
from vault_helpers import (
    demo_create_request,
    needs_confirmation_policy,
    put_candidate_and_employer_templates,
    sample_package,
)

_MINUTE = dt.timedelta(minutes=1)
_HOUR = dt.timedelta(hours=1)
_TURN_DEADLINE = 5 * _MINUTE
_QUESTION_DEADLINE = 24 * _HOUR


def _create(store, **kwargs) -> str:
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db, **kwargs)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def _deadline(store, nid):
    return store.get_view(nid, "candidate").deadline


def test_the_deadline_is_renewed_at_every_turn_change_so_4_minute_steps_never_expire(store, clock):
    # §3.4 / C-37: 手番が変わるたびに、期限(5 分)を今から付け直す。1 手ごとに 4 分ずつ進めても、
    # 手でも、見回りの expire でも、期限切れにならない(作成から 40 分たっても、交渉は続いている)。
    nid = _create(store)
    created_at = clock.now()
    version = 0

    for _ in range(5):
        for side, move in (("candidate", "propose"), ("employer", "reject")):
            clock.advance(4 * _MINUTE)
            assert store.expire(nid).expired is False  # 見回りの expire では、期限切れにならない
            request = MoveRequest(
                expected_version=version,
                side=side,
                move=move,
                package=sample_package() if move == "propose" else None,
            )
            response = store.process_move(nid, request)  # 手でも、期限切れにならない
            assert (response.valid, response.status, response.end_reason) == (True, "active", None)
            version = response.version
            assert _deadline(store, nid) == clock.now() + _TURN_DEADLINE  # 手番が変わったので、付け直された

    assert clock.now() - created_at == 40 * _MINUTE  # 作成時の期限(5 分)は、とうに過ぎている
    assert store.get_view(nid, "candidate").status == "active"
    assert store.expire(nid).expired is False


@pytest.mark.parametrize("how", ["expire", "move"])
def test_the_deadline_passes_when_the_turn_does_not_change(store, clock, how):
    # §3.4: 手番も status も変わらない手(check)では、期限を付け直さない(1 手番の中の確認は、すべて 5 分に入る)。
    # 手番が変わらないまま 5 分を過ぎたら、見回りの expire でも、次の手の操作でも、timeout で終わる。
    nid = _create(store)
    created_at = clock.now()
    clock.advance(4 * _MINUTE)
    checked = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="check", package=sample_package())
    )
    assert (checked.valid, checked.status) == (True, "active")
    assert _deadline(store, nid) == created_at + _TURN_DEADLINE  # check では、付け直されていない

    clock.advance(2 * _MINUTE)  # 作成から 6 分: 手番が変わらないまま、5 分を過ぎた
    if how == "expire":
        result = store.expire(nid)
        assert (result.expired, result.status) == (True, "judged")
    else:
        result = store.process_move(
            nid,
            MoveRequest(expected_version=checked.version, side="candidate", move="check", package=sample_package()),
        )
        assert (result.status, result.end_reason) == ("judged", "timeout")
    document = store._negotiation_ref(nid).get().to_dict()
    assert (document["status"], document["end_reason"]) == ("judged", "timeout")


def test_an_invalid_move_does_not_renew_the_deadline_either(store, clock):
    # 無効手(手番も status も変わらない)は、期限を付け直さない。無効手を重ねても、5 分を過ぎれば期限切れになる。
    nid = _create(store)
    created_at = clock.now()
    clock.advance(3 * _MINUTE)
    invalid = store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="accept"))
    assert (invalid.valid, invalid.error) == (False, "no_pending_offer")
    assert _deadline(store, nid) == created_at + _TURN_DEADLINE

    clock.advance(3 * _MINUTE)
    assert store.expire(nid).expired is True


def test_ask_principal_sets_the_24_hour_deadline_and_the_answer_goes_back_to_the_turn_deadline(store, clock):
    # §3.4: status が変わる手(ask_principal)は、途中確認の期限(24 時間)を付け直す。回答で active に戻ったら、
    # 手番の期限(5 分)を付け直す。途中確認中は、5 分を過ぎても期限切れにならない(24 時間まで待てる)。
    nid = _create(store, candidate_policy=needs_confirmation_policy("candidate"))
    package = sample_package()
    clock.advance(4 * _MINUTE)
    asked = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert asked.status == "awaiting_principal"
    assert _deadline(store, nid) == clock.now() + _QUESTION_DEADLINE

    clock.advance(23 * _HOUR)
    assert store.expire(nid).expired is False  # 24 時間の内は、待てる
    answered = store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=asked.version, side="candidate", package=package, answer="accept")
    )
    assert answered.status == "active"
    assert _deadline(store, nid) == clock.now() + _TURN_DEADLINE  # 回答の後は、手番の期限

    clock.advance(6 * _MINUTE)
    assert store.expire(nid).expired is True  # 手番のまま 5 分を過ぎた


def test_the_renewed_deadline_never_exceeds_the_negotiations_lifetime(firestore_client, clock):
    # §3.4: 期限は expires_at を超えない。寿命を 10 分にして、手番を変えながら進めると、付け直した期限は
    # 寿命で頭打ちになり、寿命に着いたら期限切れになる(手番を変え続けても、寿命で必ず終わる)。
    config = dataclasses.replace(
        DEFAULT_VAULT_CONFIG,
        deadlines=dataclasses.replace(DEFAULT_VAULT_CONFIG.deadlines, negotiation_lifetime_seconds=10 * 60),
    )
    store = VaultStore(db=firestore_client, clock=clock, config=config)
    nid = _create(store)
    created_at = clock.now()

    clock.advance(4 * _MINUTE)
    proposed = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=sample_package())
    )
    assert _deadline(store, nid) == created_at + 9 * _MINUTE  # 4 分 + 5 分。寿命(10 分)の内

    clock.advance(4 * _MINUTE)
    rejected = store.process_move(
        nid, MoveRequest(expected_version=proposed.version, side="employer", move="reject")
    )
    assert rejected.status == "active"
    assert _deadline(store, nid) == created_at + 10 * _MINUTE  # 8 分 + 5 分は寿命(10 分)を超えるので、寿命で頭打ち

    clock.advance(2 * _MINUTE)  # 寿命に着いた
    assert store.expire(nid).expired is True
