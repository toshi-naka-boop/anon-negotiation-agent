"""入口ごとのレート制限(design.md §8.2「レート制限」。台帳 L4-2・C-3・X-10・X-30)の、時間窓カウンタ(web.limits)。

Firestore エミュレータ(`(default)` の代わり)と、注入した時計で確かめる。AC-13 のうち、回数の数え方そのもの:
- 入口ごとに別の枠(ある入口の枠を使い切っても、別の入口は使える)。クライアントごとに別の枠。
- 全入口・全クライアントの合計の枠(301 回目は断る)。断った要求は、どの枠にも数えない。
- 窓の境目で数え直す。Retry-After は窓の終わりまでの秒数。
- 再起動(新しいリミッター)しても、数えた回数が残る。文書には TTL を付け、IP をそのまま入れない。
- カウンタに書けない・読めない・壊れているときは、通さない(閉じる側)。回復すれば続く。
- FastAPI の依存(guard)は、超えたら 429(Retry-After と、理由の本文)、数えられなければ 503。クライアントは X-Forwarded-For の末尾の IP。
- 全体の枠に数えない入口(台帳 L19-7): LLM を呼ばず、金庫かメモリを読むだけの入口(メーター・開始ページ・面談の開始)は、クライアントごとの枠だけで数え、
  全体の枠(300)に数えず、全体の枠が埋まっていても断らない。
- 文書 ID の IP のハッシュは、鍵つきの HMAC-SHA256(台帳 L19-12)。鍵は、署名の鍵から固定のラベルで派生させる。鍵が違えば文書 ID も違い、鍵なしの SHA-256 とは一致しない。
- SSE の同時本数の上限(台帳 C-65): 全体とクライアントごと。メモリの中だけで数え、戻すのは何度呼んでも 1 回(web.limits の SseConnectionLimiter)。
  HTTP の SSE の口で効くこと・接続が終わる、どの終わり方でも席が戻ることは tests/test_ui_static.py で確かめる。
HTTP の入口(攻撃モード・デモ・ライブ)で枠が効くことは tests/test_attack_mode.py、面談の LLM を呼ぶ API は tests/test_interview_api.py、
推定区間メーターは tests/test_meter.py で確かめる。
"""

import asyncio
import dataclasses
import datetime as dt
import hashlib
import hmac
import re
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from starlette.requests import Request
from web.limits import (
    CLIENT_ONLY_ENTRANCES,
    DEFAULT_RATE_LIMIT_CONFIG,
    DEFAULT_SSE_LIMIT_CONFIG,
    ENTRANCES,
    OVERALL_ENTRANCES,
    RATE_LIMITS_COLLECTION,
    RateLimitConfig,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    SseConnectionLimiter,
    SseLimitConfig,
    SseLimitReached,
    derive_limiter_key,
    load_rate_limit_config,
    load_sse_limit_config,
)
from web.session import WeakSessionKeyError

pytestmark = pytest.mark.anyio

WINDOW = DEFAULT_RATE_LIMIT_CONFIG.window_seconds  # 600
PARAMS_TOML = Path(__file__).resolve().parents[1] / "config" / "params.toml"
# 文書 ID の HMAC の鍵(台帳 L19-12)を派生させる、署名の鍵のテスト用の値(secrets.token_urlsafe(32) で作った、テスト専用の値。本番の鍵には使わない)
SESSION_KEY = "EsfcdMw60b79BMNyIn3_wHgMC_UFr5Viinl2crKhH4g"
OTHER_SESSION_KEY = "ZKoXrIclBnhfK7Q6jSZWvQLnXKLhKZxQSuNX-5uGuTo"
KEY = derive_limiter_key(SESSION_KEY)
OTHER_KEY = derive_limiter_key(OTHER_SESSION_KEY)


def _config(**changes) -> RateLimitConfig:
    """設定ファイルの値から、一部だけ替えた設定。per_client の一部を替えるときは per_client={...} を丸ごと渡す。"""
    return dataclasses.replace(DEFAULT_RATE_LIMIT_CONFIG, **changes)


def _limiter(db, clock, config: RateLimitConfig = DEFAULT_RATE_LIMIT_CONFIG) -> RateLimiter:
    """テスト用の鍵(KEY)で作ったリミッター。鍵は必須(鍵なしのハッシュには戻さない。台帳 L19-12)。"""
    return RateLimiter(db, clock, config, key=KEY)


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
    # 設計書の表にない攻撃モードの交渉の作成は、デモの実行と同じ 10。同じく表にない推定区間メーター(1 回で最大 20 件を読む)は 60、
    # 開始ページ(GET /start)は 20、面談の開始(POST .../interview/begin)は 10(台帳 C-66)。
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
        "meter": 60,
        "session_start": 20,
        "interview_begin": 10,
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
        lambda text: re.sub(r"^meter = \d+\n", "", text, flags=re.MULTILINE),  # 足りない入口(あとから足したメーター)
        lambda text: re.sub(r"^session_start = \d+\n", "", text, flags=re.MULTILINE),  # 足りない入口(台帳 C-66 で足した開始ページ)
        lambda text: re.sub(r"^interview_begin = \d+\n", "", text, flags=re.MULTILINE),  # 足りない入口(台帳 C-66 で足した面談の開始)
        lambda text: text.replace("raw_message = 20\n", "raw_message = 20\nraw_mesage = 5\n"),  # 打ち間違いの入口
        lambda text: re.sub(r"^rate_counter_ttl_seconds = \d+\n", "", text, flags=re.MULTILINE),  # 足りない項目
    ],
    ids=[
        "overall_zero", "window_zero", "entrance_zero", "entrance_float", "missing_entrance", "missing_meter_entrance",
        "missing_session_start_entrance", "missing_interview_begin_entrance", "unknown_entrance", "missing_key",
    ],
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
    limiter = _limiter(default_db, clock)
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
    limiter = _limiter(default_db, clock)
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
    limiter = _limiter(default_db, clock)
    await _admit_many(limiter, "raw_message", "203.0.113.5", 20)
    with pytest.raises(RateLimitExceeded):
        await limiter.admit("raw_message", "203.0.113.5")

    await limiter.admit("raw_message", "203.0.113.6")  # 別のクライアントは通る


# ----------------------------------------------------------------------
# 全入口の合計の枠
# ----------------------------------------------------------------------


async def test_the_overall_allowance_counts_every_entrance_and_every_client(default_db, clock):
    # §8.2: 全入口の合計で、全体として窓あたりの上限まで。IP の取り方が崩れても(クライアントごとの枠に当たらなくても)効く。
    limiter = _limiter(default_db, clock, _config(overall_limit=5))
    for index in range(5):
        await limiter.admit(OVERALL_ENTRANCES[index % len(OVERALL_ENTRANCES)], f"203.0.113.{index}")

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "203.0.113.200")  # どのクライアント・入口の枠にも余りがあるのに、全体で断る

    assert (raised.value.scope, raised.value.limit) == ("overall", 5)
    assert raised.value.retry_after_seconds == WINDOW


async def test_the_301st_request_in_a_window_is_refused_by_the_default_overall_limit(default_db, clock):
    # AC-13: 全体で 301 回目は 429(設定ファイルの 300)。300 の別々のクライアントが、全体に数える入口をめぐらせて 1 回ずつ
    # (LLM を呼ばず読むだけの入口は、全体に数えない。台帳 L19-7)。
    limiter = _limiter(default_db, clock)
    for index in range(300):
        await limiter.admit(OVERALL_ENTRANCES[index % len(OVERALL_ENTRANCES)], f"client-{index}")

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "client-new")

    assert (raised.value.scope, raised.value.limit) == ("overall", 300)


async def test_the_client_allowance_is_reported_before_the_overall_one(default_db, clock):
    # 両方に当たるときは、より具体的なクライアントの枠を理由にする。
    limiter = _limiter(default_db, clock, _config(overall_limit=2, per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
    await _admit_many(limiter, "demo_run", "203.0.113.5", 2)

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("demo_run", "203.0.113.5")

    assert raised.value.scope == "client"


async def test_a_refused_request_is_not_counted_in_any_allowance(default_db, clock):
    # 拒否した要求は、どの枠にも数えない(web.llm_budget と同じ): 断られ続けても、クライアントの数は上限のまま、全体の数も増えない。
    limiter = _limiter(default_db, clock, _config(overall_limit=50))
    await _admit_many(limiter, "demo_run", "203.0.113.5", 10)
    for _ in range(5):
        with pytest.raises(RateLimitExceeded):
            await limiter.admit("demo_run", "203.0.113.5")

    counts = {doc_id: data["count"] for doc_id, data in _documents(default_db).items()}
    assert sorted(counts.values()) == [10, 10]  # クライアントの文書と全体の文書。どちらも通った 10 回だけ
    await limiter.admit("raw_message", "203.0.113.6")  # 全体の枠は、通った分(11 回)しか使っていない
    assert sorted(data["count"] for data in _documents(default_db).values()) == [1, 10, 11]


# ----------------------------------------------------------------------
# 全体の枠に数えない入口(台帳 L19-7): LLM を呼ばず、金庫かメモリを読むだけの入口
# ----------------------------------------------------------------------


def test_only_the_entrances_that_do_not_call_the_llm_are_left_out_of_the_overall_allowance():
    # メーター(金庫を読む)・開始ページ(依頼者 ID を発行する)・面談の開始(メモリに面談の状態を作る)は、全体の枠に数えない。残りは数える。
    assert CLIENT_ONLY_ENTRANCES == {"meter", "session_start", "interview_begin"}
    assert set(OVERALL_ENTRANCES) == {
        "interview_llm", "demo_run", "live_negotiation_create", "attack_create", "attack_instruction", "raw_message",
    }  # fmt: skip
    assert set(OVERALL_ENTRANCES) | CLIENT_ONLY_ENTRANCES == set(ENTRANCES)  # 入口は、必ずどちらかに入る
    assert not set(OVERALL_ENTRANCES) & CLIENT_ONLY_ENTRANCES  # 両方には入らない


async def test_reading_entrances_never_use_up_the_overall_allowance_so_the_llm_entrances_stay_open(default_db, clock):
    # 台帳 L19-7: メーターを全体の枠(300)より多く呼んでも、面談の入口が scope=overall の 429 にならない。別々のクライアントが 301 回
    # (クライアントごとの枠には当たらない)。v20 までは、メーターも全体を消費し、2 つの IP で本物の利用者の枠が埋まった。
    limiter = _limiter(default_db, clock)
    for index in range(DEFAULT_RATE_LIMIT_CONFIG.overall_limit + 1):
        await limiter.admit("meter", f"client-{index}")

    await limiter.admit("interview_llm", "203.0.113.5")  # 全体の枠は、1 回も使っていない
    await limiter.admit("live_negotiation_create", "203.0.113.6")

    overall = {doc_id: data for doc_id, data in _documents(default_db).items() if doc_id.startswith("overall.")}
    assert [data["count"] for data in overall.values()] == [2]  # 全体に数えたのは、LLM・作成の 2 回だけ


@pytest.mark.parametrize("entrance", sorted(CLIENT_ONLY_ENTRANCES))
async def test_a_reading_entrance_neither_counts_in_nor_is_stopped_by_the_overall_allowance(default_db, clock, entrance):
    # LLM・作成の入口が全体の枠を使い切っていても、読み出しの入口は通る。通っても、全体の数は進まない。
    limiter = _limiter(default_db, clock, _config(overall_limit=2))
    await limiter.admit("demo_run", "203.0.113.1")
    await limiter.admit("raw_message", "203.0.113.2")
    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit("interview_llm", "203.0.113.3")
    assert raised.value.scope == "overall"  # 全体の枠は埋まっている(確認の前提)

    await limiter.admit(entrance, "203.0.113.4")  # 通る

    documents = _documents(default_db)
    assert next(data["count"] for doc_id, data in documents.items() if doc_id.startswith("overall.")) == 2  # 進んでいない
    mine = [data for doc_id, data in documents.items() if doc_id.startswith(f"{entrance}.")]
    assert [(data["count"], data["entrance"]) for data in mine] == [(1, entrance)]  # 入口とクライアントの文書だけが増えた


@pytest.mark.parametrize("entrance", sorted(CLIENT_ONLY_ENTRANCES))
async def test_a_reading_entrance_keeps_its_own_per_client_allowance(default_db, clock, entrance):
    # 全体の枠に数えなくても、クライアントごとの枠は効く(上限まで通り、次は scope=client で断る)。別のクライアントは、別の枠。
    limit = DEFAULT_RATE_LIMIT_CONFIG.per_client[entrance]
    limiter = _limiter(default_db, clock)
    await _admit_many(limiter, entrance, "203.0.113.5", limit)

    with pytest.raises(RateLimitExceeded) as raised:
        await limiter.admit(entrance, "203.0.113.5")
    assert (raised.value.entrance, raised.value.scope, raised.value.limit) == (entrance, "client", limit)
    await limiter.admit(entrance, "203.0.113.6")
    assert not any(doc_id.startswith("overall.") for doc_id in _documents(default_db))  # 全体の文書は、作っていない


# ----------------------------------------------------------------------
# 窓の境目・再起動・文書の中身
# ----------------------------------------------------------------------


async def test_the_count_starts_again_at_the_window_boundary(default_db, clock):
    # 固定の窓: 窓の最後の 1 秒までは同じ窓(断る。Retry-After は 1)、窓の始まりの瞬間に数え直す。
    limiter = _limiter(default_db, clock)
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
    await _admit_many(_limiter(default_db, clock), "demo_run", "203.0.113.5", 7)

    restarted = _limiter(default_db, clock)
    await _admit_many(restarted, "demo_run", "203.0.113.5", 3)  # 7 + 3 = 10

    with pytest.raises(RateLimitExceeded):
        await restarted.admit("demo_run", "203.0.113.5")
    with pytest.raises(RateLimitExceeded):
        await _limiter(default_db, clock).admit("demo_run", "203.0.113.5")  # もう 1 回作り直しても同じ


async def test_the_documents_carry_a_ttl_and_do_not_hold_the_client_address(default_db, clock):
    # 文書には TTL 用の ttl_at(窓の終わりから設定の秒数)を付ける。IP は文書 ID にも内容にも、そのまま入れない。
    await _limiter(default_db, clock).admit("raw_message", "203.0.113.77")

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
    limiter = _limiter(default_db, clock)
    for weird in ("a/b/c", "..", "__x__", "", "日本語", "x" * 5000):
        await limiter.admit("demo_run", weird)


async def test_the_client_part_of_a_document_id_is_a_keyed_hash_so_the_address_cannot_be_found_by_trying_every_address(
    default_db, clock
):
    # 台帳 L19-12: 鍵なしの SHA-256 だと、IPv4 の全数(約 43 億)を試して、文書 ID から IP を戻せる(`(default)` を読める者に、直近の窓で入口を使った
    # IP の一覧が渡る)。鍵つきの HMAC-SHA256 なら、鍵を知らない者には戻せない。同じ IP でも、鍵が違えば別の文書。
    ip = "203.0.113.77"
    await RateLimiter(default_db, clock, key=KEY).admit("raw_message", ip)
    await RateLimiter(default_db, clock, key=OTHER_KEY).admit("raw_message", ip)

    window = int(clock.now().timestamp()) // WINDOW

    def keyed(key: bytes) -> str:
        return hmac.new(key, ip.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    ids = sorted(doc_id for doc_id in _documents(default_db) if doc_id.startswith("raw_message."))
    assert ids == sorted([f"raw_message.{window}.{keyed(KEY)}", f"raw_message.{window}.{keyed(OTHER_KEY)}"])  # 鍵が違えば、別の文書
    unkeyed = hashlib.sha256(ip.encode("utf-8")).hexdigest()[:32]
    assert all(not doc_id.endswith(unkeyed) for doc_id in _documents(default_db))  # 鍵なしの SHA-256 とは、どの文書とも一致しない


def test_the_limiter_key_is_derived_from_the_session_signing_key_with_a_fixed_label():
    # 台帳 L19-12: 署名の鍵そのものではなく、用途を表す固定のラベル(b"rate-limits")で HMAC-SHA256 をとった値。署名の鍵が違えば、鍵も違う。
    assert KEY == hmac.new(SESSION_KEY.encode("utf-8"), b"rate-limits", hashlib.sha256).digest()
    assert len(KEY) == 32 and KEY != SESSION_KEY.encode("utf-8") and KEY != OTHER_KEY
    assert derive_limiter_key(SESSION_KEY) == KEY  # 同じ署名の鍵からは、いつも同じ値(再起動をまたいで、同じ文書に数える)


@pytest.mark.parametrize("weak", ["", "short", "A" * 43], ids=["empty", "short", "not_random"])
def test_a_weak_or_empty_signing_key_does_not_make_a_limiter_key(weak):
    # web.session の鍵の検証(base64url で 32 バイト以上・明らかな乱数でないものは拒否)を通らない鍵からは、作らない。
    with pytest.raises(WeakSessionKeyError):
        derive_limiter_key(weak)


@pytest.mark.parametrize("bad", [b"", None, "text-key"], ids=["empty", "none", "text"])
def test_a_limiter_without_a_usable_key_is_refused(bad):
    # 鍵なしのハッシュには戻さない: 空・None・文字列は、組み立てで拒否する。鍵を渡さなければ、引数が足りない。
    with pytest.raises(ValueError, match="key"):
        RateLimiter(object(), object(), key=bad)
    with pytest.raises(TypeError):
        RateLimiter(object(), object())


@pytest.mark.anyio
async def test_the_web_app_keys_its_documents_with_a_key_derived_from_its_signing_key(web_app, session_key):
    # 本番の組み立て(build_services)は、署名の鍵から派生させた鍵を RateLimiter に渡す(鍵なしの SHA-256 で数えない)。
    ip = "198.51.100.23"
    response = await web_app.browser().client.get("/start", headers={"X-Forwarded-For": ip})
    assert response.status_code == 200

    window = int(web_app.clock.now().timestamp()) // WINDOW
    expected = hmac.new(derive_limiter_key(session_key), ip.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    assert list(_documents(web_app.default_db)) == [f"session_start.{window}.{expected}"]  # 開始ページだけを呼んだ


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
    limiter = _limiter(flaky, clock)
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
    limiter = _limiter(default_db, clock)
    await limiter.admit("demo_run", "203.0.113.5")
    client_ref = next(ref for ref in default_db.collection(RATE_LIMITS_COLLECTION).list_documents() if ref.id.startswith("demo_run."))
    client_ref.update({"count": bad_count})

    with pytest.raises(RateLimiterUnavailable):
        await limiter.admit("demo_run", "203.0.113.5")


async def test_concurrent_requests_never_push_the_counters_past_the_limit(default_db, clock):
    # 「上限に達していなければ 1 進める。達していれば進めずに断る」は 1 つのトランザクション。並行して送っても、通るのは上限までで、
    # カウンタは通った分だけ進む(競合で数えられなかった分は RateLimiterUnavailable で通さない側)。エミュレータは、同じ文書への
    # 並行の書き込みが重いので、並行は 4 本(web.llm_budget の同じ試験と同じ数)。
    limiter = _limiter(default_db, clock, _config(per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
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
    limiter = _limiter(default_db, clock, _config(per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
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
    limiter = _limiter(default_db, clock, _config(per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
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
    limiter = _limiter(flaky, clock)
    flaky.failing = True
    async with _http(_guarded_app(limiter)) as client:
        failed = await client.get("/probe")
        flaky.failing = False
        recovered = await client.get("/probe")

    assert (failed.status_code, failed.json()) == (503, {"detail": "temporarily_unavailable"})
    assert recovered.status_code == 200


# ----------------------------------------------------------------------
# SSE の同時本数の上限(台帳 C-65): web.limits の SseConnectionLimiter(メモリの中だけ)
# ----------------------------------------------------------------------


def _sse(**changes) -> SseConnectionLimiter:
    """全体 3 本・クライアントごと 2 本の、小さな上限のリミッター(changes で替える)。"""
    return SseConnectionLimiter(SseLimitConfig(**{"max_connections": 3, "max_connections_per_client": 2, **changes}))


def _request(forwarded: str | None = None) -> Request:
    """X-Forwarded-For(web.client_ip がクライアントを決める)だけを持つ要求。"""
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode("ascii"))]
    return Request({"type": "http", "headers": headers})


def test_the_sse_config_holds_the_design_limits():
    # §4.1・§8.2(台帳 C-65。暫定): 全体 20 本・クライアント IP ごと 2 本。
    assert DEFAULT_SSE_LIMIT_CONFIG == SseLimitConfig(max_connections=20, max_connections_per_client=2)


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: re.sub(r"^sse_max_connections = \d+", "sse_max_connections = 0", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^sse_max_connections_per_client = \d+", "sse_max_connections_per_client = 0", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^sse_max_connections = \d+", "sse_max_connections = 2.5", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^sse_max_connections = \d+\n", "", text, flags=re.MULTILINE),  # 足りない項目
        lambda text: re.sub(r"^sse_max_connections_per_client = \d+\n", "", text, flags=re.MULTILINE),  # 足りない項目
    ],
    ids=["total_zero", "per_client_zero", "total_float", "missing_total", "missing_per_client"],
)
def test_the_sse_config_rejects_values_that_do_not_make_sense(tmp_path, edit):
    # 0 だと SSE が黙って全部閉じる・足りない項目が上限なしで通る。読み込みで拒否する。
    path = _broken_config(tmp_path, edit)
    with pytest.raises(ValueError, match="web.limits"):
        load_sse_limit_config(path)


def test_the_sse_limiter_admits_up_to_the_per_client_limit_and_refuses_the_next_one_without_counting_it():
    limiter = _sse()
    first, second = limiter.acquire("203.0.113.5"), limiter.acquire("203.0.113.5")  # 上限ちょうど(2 本)まで通る

    with pytest.raises(SseLimitReached) as raised:
        limiter.acquire("203.0.113.5")

    assert (raised.value.scope, raised.value.limit) == ("client", 2)
    assert (len(limiter), limiter.open_for("203.0.113.5")) == (2, 2)  # 断った分は、数えていない
    first.release()
    second.release()


def test_the_sse_limiter_counts_each_client_separately_and_stops_at_the_overall_limit():
    limiter = _sse()  # 全体 3 本・クライアントごと 2 本
    held = [limiter.acquire("a"), limiter.acquire("a"), limiter.acquire("b")]  # 別のクライアントは、別に数える

    with pytest.raises(SseLimitReached) as raised:
        limiter.acquire("c")  # c の枠には余りがあるが、全体の上限で断る

    assert (raised.value.scope, raised.value.limit) == ("overall", 3)
    assert len(limiter) == 3 and limiter.open_for("c") == 0
    # クライアントの枠と全体の両方に当たるときは、より具体的なクライアントの枠を理由にする
    with pytest.raises(SseLimitReached) as both:
        limiter.acquire("a")
    assert both.value.scope == "client"
    for slot in held:
        slot.release()


def test_releasing_a_slot_gives_the_place_back_exactly_once_even_when_it_is_released_again():
    limiter = _sse()
    slot, other = limiter.acquire("a"), limiter.acquire("a")

    for _ in range(3):  # 応答の終わりと確認の失敗の両方から呼ばれても、数がずれない
        slot.release()

    assert (len(limiter), limiter.open_for("a")) == (1, 1)
    again = limiter.acquire("a")  # 空いた 1 本ぶんは、また取れる(2 本まで)
    with pytest.raises(SseLimitReached):
        limiter.acquire("a")
    again.release()
    other.release()
    assert len(limiter) == 0


def test_the_sse_limiter_remembers_only_the_clients_that_are_connected():
    limiter = _sse(max_connections=100)
    slots = [limiter.acquire(f"client-{index}") for index in range(50)]
    for slot in slots:
        slot.release()

    assert len(limiter) == 0
    assert limiter._open_by_client == {}  # つないでいる IP だけを覚える(IP ごとの記録が、つながるたびに増え続けない)


def test_a_refused_sse_connection_becomes_a_429_shaped_like_the_other_entrances():
    # 本文は他の入口と同じ形(code・entrance・scope・limit・window_seconds・retry_after_seconds)。入口は sse、時間窓がないので window_seconds は null、
    # Retry-After は 2 秒(画面が、通常の GET の再取得に切り替える間隔)。クライアントは X-Forwarded-For の末尾(web.client_ip)。
    limiter = _sse(max_connections=2, max_connections_per_client=1)
    limiter.acquire_for_request(_request("198.51.100.7"))

    with pytest.raises(HTTPException) as same_client:
        limiter.acquire_for_request(_request("10.0.0.1, 198.51.100.7"))  # 先頭側を変えても、末尾が同じなら同じクライアント
    limiter.acquire_for_request(_request("198.51.100.8"))
    with pytest.raises(HTTPException) as overall:
        limiter.acquire_for_request(_request("198.51.100.9"))

    for refused, scope, limit in ((same_client.value, "client", 1), (overall.value, "overall", 2)):
        assert refused.status_code == 429 and refused.headers == {"Retry-After": "2"}
        assert refused.detail == {
            "code": "rate_limited",
            "entrance": "sse",
            "scope": scope,
            "limit": limit,
            "window_seconds": None,
            "retry_after_seconds": 2,
        }
    assert len(limiter) == 2  # 断った分は、数えていない
