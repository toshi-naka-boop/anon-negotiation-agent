"""入口ごとのレート制限(design.md §8.2「レート制限」。台帳 L4-2・C-3・X-10・X-30)の、時間窓カウンタ(web.limits)。

Firestore エミュレータ(`(default)` の代わり)と、注入した時計で確かめる。AC-13 のうち、回数の数え方そのもの:
- 入口ごとに別の枠(ある入口の枠を使い切っても、別の入口は使える)。クライアントごとに別の枠。
- 全入口・全クライアントの合計の枠(301 回目は断る)。断った要求は、どの枠にも数えない。
- 窓の境目で数え直す。Retry-After は窓の終わりまでの秒数。
- 再起動(新しいリミッター)しても、数えた回数が残る。文書には TTL を付け、IP をそのまま入れない。
- カウンタに書けない・読めない・壊れているときは、通さない(閉じる側)。回復すれば続く。
- FastAPI の依存(guard)は、超えたら 429(Retry-After と、理由の本文)、数えられなければ 503。クライアントは X-Forwarded-For の末尾の IP。
HTTP の入口(攻撃モード・デモ・ライブ)で枠が効くことは tests/test_attack_mode.py で確かめる。
"""

import asyncio
import dataclasses
import datetime as dt
import re
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from web.limits import (
    DEFAULT_RATE_LIMIT_CONFIG,
    ENTRANCES,
    RATE_LIMITS_COLLECTION,
    RateLimitConfig,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    load_rate_limit_config,
)

pytestmark = pytest.mark.anyio

WINDOW = DEFAULT_RATE_LIMIT_CONFIG.window_seconds  # 600
PARAMS_TOML = Path(__file__).resolve().parents[1] / "config" / "params.toml"


def _config(**changes) -> RateLimitConfig:
    """設定ファイルの値から、一部だけ替えた設定。per_client の一部を替えるときは per_client={...} を丸ごと渡す。"""
    return dataclasses.replace(DEFAULT_RATE_LIMIT_CONFIG, **changes)


def _documents(db) -> dict[str, dict]:
    return {ref.id: ref.get().to_dict() for ref in db.collection(RATE_LIMITS_COLLECTION).list_documents()}


async def _admit_many(limiter: RateLimiter, entrance, client: str, count: int) -> None:
    for _ in range(count):
        await limiter.admit(entrance, client)


# ----------------------------------------------------------------------
# 設定([web.limits])
# ----------------------------------------------------------------------


def test_the_config_holds_the_design_limits():
    # §8.2 の表(暫定): 面談 30・デモの実行 10・ライブ交渉の作成 10・攻撃モードの指示 30・壁 1 の生メッセージ 20(10 分)と、全体 300。
    # 設計書の表にない攻撃モードの交渉の作成は、デモの実行と同じ 10。
    config = DEFAULT_RATE_LIMIT_CONFIG
    assert config.window_seconds == 600
    assert config.overall_limit == 300
    assert dict(config.per_client) == {
        "interview_llm": 30,
        "demo_run": 10,
        "live_negotiation_create": 10,
        "attack_create": 10,
        "attack_instruction": 30,
        "raw_message": 20,
    }
    assert set(config.per_client) == set(ENTRANCES)


def _broken_config(tmp_path: Path, edit) -> Path:
    path = tmp_path / "params.toml"
    path.write_text(edit(PARAMS_TOML.read_text(encoding="utf-8")), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: re.sub(r"^rate_overall_limit = \d+", "rate_overall_limit = 0", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^rate_window_seconds = \d+", "rate_window_seconds = 0", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^demo_run = \d+", "demo_run = 0", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^demo_run = \d+", "demo_run = 1.5", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^raw_message = \d+\n", "", text, flags=re.MULTILINE),  # 足りない入口
        lambda text: text.replace("raw_message = 20\n", "raw_message = 20\nraw_mesage = 5\n"),  # 打ち間違いの入口
        lambda text: re.sub(r"^rate_counter_ttl_seconds = \d+\n", "", text, flags=re.MULTILINE),  # 足りない項目
    ],
    ids=["overall_zero", "window_zero", "entrance_zero", "entrance_float", "missing_entrance", "unknown_entrance", "missing_key"],
)
def test_the_config_rejects_values_that_do_not_make_sense(tmp_path, edit):
    # 枠が 0 だと入口が黙って閉じる・足りない入口が枠なしで通る・打ち間違いが黙って無視される。読み込みで拒否する。
    path = _broken_config(tmp_path, edit)
    with pytest.raises(ValueError, match="web.limits"):
        load_rate_limit_config(path)


# ----------------------------------------------------------------------
# 入口ごと・クライアントごとの枠
# ----------------------------------------------------------------------


async def test_a_client_is_admitted_up_to_the_entrance_limit_and_the_next_request_is_refused_with_the_time_left(
    default_db, clock
):
    # §8.2: クライアントごと・入口ごとの上限(デモの実行は 10 回/10 分)まで通り、次は断る。理由は client の枠で、窓の終わりまでの秒数を持つ。
    limiter = RateLimiter(default_db, clock)
    await _admit_many(limiter, "demo_run", "203.0.113.5", 10)
    clock.advance(dt.timedelta(seconds=100))

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "203.0.113.5")

    exceeded = raised.value
    assert (exceeded.entrance, exceeded.scope, exceeded.limit) == ("demo_run", "client", 10)
    assert exceeded.window_seconds == WINDOW
    assert exceeded.retry_after_seconds == WINDOW - 100


async def test_each_entrance_has_its_own_allowance_for_the_same_client(default_db, clock):
    # 台帳 L4-2: ある入口の枠を使い切っても、別の入口は使える(枠は入口ごとに別)。
    limiter = RateLimiter(default_db, clock)
    await _admit_many(limiter, "demo_run", "203.0.113.5", 10)
    with pytest.raises(RateLimitExceeded):
        await limiter.admit("demo_run", "203.0.113.5")

    for entrance in ENTRANCES:
        if entrance != "demo_run":
            await limiter.admit(entrance, "203.0.113.5")  # どれも通る
    # 入口ごとの上限は、設定の値のとおり(攻撃モードの指示は 30 回まで通り、31 回目で断る)
    await _admit_many(limiter, "attack_instruction", "203.0.113.5", 29)  # 上の 1 回と合わせて 30 回
    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("attack_instruction", "203.0.113.5")
    assert raised.value.limit == 30


async def test_each_client_has_its_own_allowance_on_the_same_entrance(default_db, clock):
    # クライアントごとの枠: 別の IP は、同じ入口でも別に数える。
    limiter = RateLimiter(default_db, clock)
    await _admit_many(limiter, "raw_message", "203.0.113.5", 20)
    with pytest.raises(RateLimitExceeded):
        await limiter.admit("raw_message", "203.0.113.5")

    await limiter.admit("raw_message", "203.0.113.6")  # 別のクライアントは通る


# ----------------------------------------------------------------------
# 全入口の合計の枠
# ----------------------------------------------------------------------


async def test_the_overall_allowance_counts_every_entrance_and_every_client(default_db, clock):
    # §8.2: 全入口の合計で、全体として窓あたりの上限まで。IP の取り方が崩れても(クライアントごとの枠に当たらなくても)効く。
    limiter = RateLimiter(default_db, clock, _config(overall_limit=5))
    for index in range(5):
        await limiter.admit(ENTRANCES[index % len(ENTRANCES)], f"203.0.113.{index}")

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "203.0.113.200")  # どのクライアント・入口の枠にも余りがあるのに、全体で断る

    assert (raised.value.scope, raised.value.limit) == ("overall", 5)
    assert raised.value.retry_after_seconds == WINDOW


async def test_the_301st_request_in_a_window_is_refused_by_the_default_overall_limit(default_db, clock):
    # AC-13: 全体で 301 回目は 429(設定ファイルの 300)。300 の別々のクライアントが、入口をめぐらせて 1 回ずつ。
    limiter = RateLimiter(default_db, clock)
    for index in range(300):
        await limiter.admit(ENTRANCES[index % len(ENTRANCES)], f"client-{index}")

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "client-new")

    assert (raised.value.scope, raised.value.limit) == ("overall", 300)


async def test_the_client_allowance_is_reported_before_the_overall_one(default_db, clock):
    # 両方に当たるときは、より具体的なクライアントの枠を理由にする。
    limiter = RateLimiter(default_db, clock, _config(overall_limit=2, per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
    await _admit_many(limiter, "demo_run", "203.0.113.5", 2)

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "203.0.113.5")

    assert raised.value.scope == "client"


async def test_a_refused_request_is_not_counted_in_any_allowance(default_db, clock):
    # 拒否した要求は、どの枠にも数えない(web.llm_budget と同じ): 断られ続けても、クライアントの数は上限のまま、全体の数も増えない。
    limiter = RateLimiter(default_db, clock, _config(overall_limit=50))
    await _admit_many(limiter, "demo_run", "203.0.113.5", 10)
    for _ in range(5):
        with pytest.raises(RateLimitExceeded):
            await limiter.admit("demo_run", "203.0.113.5")

    counts = {doc_id: data["count"] for doc_id, data in _documents(default_db).items()}
    assert sorted(counts.values()) == [10, 10]  # クライアントの文書と全体の文書。どちらも通った 10 回だけ
    await limiter.admit("raw_message", "203.0.113.6")  # 全体の枠は、通った分(11 回)しか使っていない
    assert sorted(data["count"] for data in _documents(default_db).values()) == [1, 10, 11]


# ----------------------------------------------------------------------
# 窓の境目・再起動・文書の中身
# ----------------------------------------------------------------------


async def test_the_count_starts_again_at_the_window_boundary(default_db, clock):
    # 固定の窓: 窓の最後の 1 秒までは同じ窓(断る。Retry-After は 1)、窓の始まりの瞬間に数え直す。
    limiter = RateLimiter(default_db, clock)
    await _admit_many(limiter, "demo_run", "203.0.113.5", 10)

    clock.set(dt.datetime(2026, 1, 1, 0, 9, 59, 500000, tzinfo=dt.timezone.utc))
    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "203.0.113.5")
    assert raised.value.retry_after_seconds == 1  # 0.5 秒残っていても、1 秒未満には丸めない

    clock.set(dt.datetime(2026, 1, 1, 0, 10, 0, tzinfo=dt.timezone.utc))  # 次の窓の始まり
    await _admit_many(limiter, "demo_run", "203.0.113.5", 10)  # また 10 回通る
    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "203.0.113.5")
    assert raised.value.retry_after_seconds == WINDOW  # 窓の始まりでは、窓の長さぶん残っている


async def test_the_counted_values_survive_a_restart(default_db, clock):
    # 台帳 X-10: 回数は Firestore にあるので、リミッターを作り直しても(再起動・新しいリビジョン)消えない。
    await _admit_many(RateLimiter(default_db, clock), "demo_run", "203.0.113.5", 7)

    restarted = RateLimiter(default_db, clock)
    await _admit_many(restarted, "demo_run", "203.0.113.5", 3)  # 7 + 3 = 10

    with pytest.raises(RateLimitExceeded):
        await restarted.admit("demo_run", "203.0.113.5")
    with pytest.raises(RateLimitExceeded):
        await RateLimiter(default_db, clock).admit("demo_run", "203.0.113.5")  # もう 1 回作り直しても同じ


async def test_the_documents_carry_a_ttl_and_do_not_hold_the_client_address(default_db, clock):
    # 文書には TTL 用の ttl_at(窓の終わりから設定の秒数)を付ける。IP は文書 ID にも内容にも、そのまま入れない。
    await RateLimiter(default_db, clock).admit("raw_message", "203.0.113.77")

    documents = _documents(default_db)
    assert len(documents) == 2
    window_end = dt.datetime(2026, 1, 1, 0, 10, tzinfo=dt.timezone.utc)
    expected_ttl = window_end + dt.timedelta(seconds=DEFAULT_RATE_LIMIT_CONFIG.counter_ttl_seconds)
    for doc_id, data in documents.items():
        assert data["ttl_at"] == expected_ttl
        assert data["window_start"] == dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        assert "203.0.113.77" not in doc_id and "203.0.113.77" not in str(data)
    client_doc = next(data for doc_id, data in documents.items() if doc_id.startswith("raw_message."))
    assert (client_doc["count"], client_doc["entrance"]) == (1, "raw_message")
    assert any(doc_id.startswith("overall.") for doc_id in documents)


async def test_a_client_value_that_is_not_a_valid_document_id_still_works(default_db, clock):
    # クライアントは X-Forwarded-For の値(テストや直接公開では何でも入り得る)。スラッシュなどが入っても、文書 ID を壊さない。
    limiter = RateLimiter(default_db, clock)
    for weird in ("a/b/c", "..", "__x__", "", "日本語", "x" * 5000):
        await limiter.admit("demo_run", weird)


# ----------------------------------------------------------------------
# 数えられないとき(閉じる側)・並行
# ----------------------------------------------------------------------


class _FlakyDb:
    """Firestore のクライアントを包み、failing の間は、どの操作も失敗させる(Firestore の失敗の模擬)。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.failing = False

    def __getattr__(self, name):
        if self.failing:
            raise RuntimeError("firestore is down")
        return getattr(self._inner, name)


async def test_a_counter_that_cannot_be_written_closes_the_door_and_recovery_opens_it_again(default_db, clock, caplog):
    # 台帳 X-50 と同じ考え方: カウンタに書けないときは通さない(閉じる側)。回復すれば、続きから数える。ログには例外の型名だけ。
    flaky = _FlakyDb(default_db)
    limiter = RateLimiter(flaky, clock)
    await limiter.admit("demo_run", "203.0.113.5")

    flaky.failing = True
    with pytest.raises(RateLimiterUnavailable, match="RuntimeError"):
        await limiter.admit("demo_run", "203.0.113.5")
    assert "RuntimeError" in caplog.text and "203.0.113.5" not in caplog.text

    flaky.failing = False
    await _admit_many(limiter, "demo_run", "203.0.113.5", 9)  # 1 + 9 = 10。失敗した 1 回は数えていない
    with pytest.raises(RateLimitExceeded):
        await limiter.admit("demo_run", "203.0.113.5")


@pytest.mark.parametrize("bad_count", ["many", -1, None, True, 1.5], ids=["text", "negative", "null", "bool", "float"])
async def test_a_corrupt_counter_document_closes_the_door_instead_of_counting_from_zero(default_db, clock, bad_count):
    # 数の項目が壊れた文書を 0 とみなすと、枠が黙って開く。壊れていれば通さない(台帳 X-62 と同じ)。
    limiter = RateLimiter(default_db, clock)
    await limiter.admit("demo_run", "203.0.113.5")
    client_ref = next(ref for ref in default_db.collection(RATE_LIMITS_COLLECTION).list_documents() if ref.id.startswith("demo_run."))
    client_ref.update({"count": bad_count})

    with pytest.raises(RateLimiterUnavailable):
        await limiter.admit("demo_run", "203.0.113.5")


async def test_concurrent_requests_never_push_the_counters_past_the_limit(default_db, clock):
    # 「上限に達していなければ 1 進める。達していれば進めずに断る」は 1 つのトランザクション。並行して送っても、通るのは上限までで、
    # カウンタは通った分だけ進む(競合で数えられなかった分は RateLimiterUnavailable で通さない側)。エミュレータは、同じ文書への
    # 並行の書き込みが重いので、並行は 4 本(web.llm_budget の同じ試験と同じ数)。
    limiter = RateLimiter(default_db, clock, _config(per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
    results = await asyncio.gather(*[limiter.admit("demo_run", "203.0.113.5") for _ in range(4)], return_exceptions=True)

    admitted = [r for r in results if r is None]
    assert len(admitted) <= 2
    assert all(isinstance(r, RateLimitExceeded | RateLimiterUnavailable) for r in results if r is not None)
    client_count = next(d["count"] for i, d in _documents(default_db).items() if i.startswith("demo_run."))
    overall_count = next(d["count"] for i, d in _documents(default_db).items() if i.startswith("overall."))
    assert client_count == overall_count == len(admitted)
    for _ in range(3):  # 断られた・数えられなかった分を重ねても、上限(2)を超えない
        try:
            await limiter.admit("demo_run", "203.0.113.5")
        except (RateLimitExceeded, RateLimiterUnavailable):
            pass
    assert next(d["count"] for i, d in _documents(default_db).items() if i.startswith("demo_run.")) == 2


# ----------------------------------------------------------------------
# FastAPI の依存(guard)
# ----------------------------------------------------------------------


def _guarded_app(limiter: RateLimiter) -> FastAPI:
    app = FastAPI()

    @app.get("/probe", dependencies=[Depends(limiter.guard("demo_run"))])
    async def probe() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _http(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://web.test")


async def test_the_guard_answers_429_with_retry_after_and_the_reason_when_the_limit_is_reached(default_db, clock):
    # 超えたら 429。Retry-After は窓の終わりまでの秒数。本文は、画面が「実演」として理由を出せる形(入口・枠・上限・窓の長さ・秒数)。
    limiter = RateLimiter(default_db, clock, _config(per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
    async with _http(_guarded_app(limiter)) as client:
        assert (await client.get("/probe")).status_code == 200
        assert (await client.get("/probe")).status_code == 200
        clock.advance(dt.timedelta(seconds=45))
        refused = await client.get("/probe")

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == str(WINDOW - 45)
    assert refused.json() == {
        "detail": {
            "code": "rate_limited",
            "entrance": "demo_run",
            "scope": "client",
            "limit": 2,
            "window_seconds": WINDOW,
            "retry_after_seconds": WINDOW - 45,
        }
    }


async def test_the_guard_keys_the_client_by_the_last_forwarded_address_so_a_forged_head_makes_no_new_allowance(
    default_db, clock
):
    # 台帳 C-3・R-7: クライアントは X-Forwarded-For の末尾(Cloud Run が追記した値)。利用者が書ける先頭側を変えても、別の枠にならない。
    limiter = RateLimiter(default_db, clock, _config(per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
    async with _http(_guarded_app(limiter)) as client:
        for index in range(2):
            ok = await client.get("/probe", headers={"X-Forwarded-For": f"10.0.0.{index}, 198.51.100.7"})
            assert ok.status_code == 200
        forged = await client.get("/probe", headers={"X-Forwarded-For": "192.0.2.99, 198.51.100.7"})
        other = await client.get("/probe", headers={"X-Forwarded-For": "198.51.100.8"})

    assert forged.status_code == 429  # 先頭側を変えても、末尾が同じなら同じ枠
    assert other.status_code == 200  # 末尾が違えば別の枠


async def test_the_guard_answers_503_when_the_counter_cannot_be_used(default_db, clock):
    # 数えられないときは通さない。本文に内部の理由は出さない。
    flaky = _FlakyDb(default_db)
    limiter = RateLimiter(flaky, clock)
    flaky.failing = True
    async with _http(_guarded_app(limiter)) as client:
        failed = await client.get("/probe")
        flaky.failing = False
        recovered = await client.get("/probe")

    assert (failed.status_code, failed.json()) == (503, {"detail": "temporarily_unavailable"})
    assert recovered.status_code == 200
