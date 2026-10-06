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
- クライアントの枠のキー(台帳 C-71): IPv4 はアドレス単位・IPv6 は /64 単位(同じ /64 の中のアドレスは同じ枠。IPv4 射影・unknown・書き方の違いも)。
- 読み取りの枠(台帳 C-68・C-72・C-73。v24): 金庫か Firestore を読む GET(セッションの有無によらない。SSE の開始・再接続を含む)に、送信元ごとの 1 分 120 回
  (メモリ。全体の枠・Firestore に数えない)。121 回目は 429(Retry-After つき。セッションの確認にも金庫にも届く前)・別の送信元は影響されない。数えない GET は、
  静的ファイル・/health・面談の注記・ケースとリプレイの一覧・attestation・GET /start だけ。本番の app(TEE ありとなし)の GET の経路の全体を、「数える」「数えない」に分類させる
  (分類のない GET があれば落ちる)。画面の 2 秒ごとの再取得は枠に収まる。
- 本文の全体の上限と読み取りの期限(台帳 X-85・X-90): 64 KB を超える本文は、ルートの前(セッションより外)で 413。宣言があれば読まずに、なければ読みながら数えて。
  本文は、上限まで読み切ってから内側(セッションのミドルウェアとルート)に渡す(宣言なしの大きな本文は、セッションの確認より前に 413)。期限(10 秒)までに読み切れなければ 408。
  本文のない要求は、待たずに通す。
HTTP の入口(攻撃モード・デモ・ライブ)で枠が効くことは tests/test_attack_mode.py、面談の LLM を呼ぶ API と面談の同時数・寿命は tests/test_interview_api.py、
推定区間メーターは tests/test_meter.py で確かめる。
"""

import asyncio
import dataclasses
import datetime as dt
import hashlib
import hmac
import inspect
import ipaddress
import json
import random
import re
import time
from pathlib import Path

import httpx
import pytest
from attack_helpers import CREATE, create_body, make_env, post  # noqa: F401  (make_env はフィクスチャ)
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.routing import iter_route_contexts
from starlette.requests import Request
from starlette.routing import Mount
from test_web_tee_api import entry  # noqa: F401  (本番の起動口 create_app_from_env を、スタブの接続で呼ぶフィクスチャ)
from vault.clock import FixedClock
from web.api import TEE_ATTESTATION_PATH, TeeAttestationConfig
from web.app import READ_LIMIT_EXEMPT_GET_ROUTES, create_app
from web.body_limit import RequestBodyLimitMiddleware
from web.client_ip import UNKNOWN_CLIENT, client_key, normalize_client
from web.config import DEFAULT_WEB_CONFIG, load_web_config
from web.limits import (
    ANONYMOUS_READ_WINDOW_SECONDS,
    CLIENT_ONLY_ENTRANCES,
    DEFAULT_ANONYMOUS_READ_LIMIT_CONFIG,
    DEFAULT_RATE_LIMIT_CONFIG,
    DEFAULT_SSE_LIMIT_CONFIG,
    ENTRANCES,
    OVERALL_ENTRANCES,
    RATE_LIMITS_COLLECTION,
    AnonymousReadLimitConfig,
    AnonymousReadLimiter,
    AnonymousReadLimitMiddleware,
    AnonymousReadLimitReached,
    RateLimitConfig,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    SseConnectionLimiter,
    SseLimitConfig,
    SseLimitReached,
    derive_limiter_key,
    load_anonymous_read_limit_config,
    load_rate_limit_config,
    load_sse_limit_config,
)
from web.session import WeakSessionKeyError
from web_app_helpers import REQUESTED_WITH, GatedVault, build_web_env, dump_documents

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


# ----------------------------------------------------------------------
# クライアントの枠のキー(台帳 C-71): IPv4 はアドレス単位・IPv6 は /64 単位
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("203.0.113.5", "203.0.113.5"),  # IPv4 は、アドレス単位
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),  # IPv6 は、/64 の接頭辞
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),  # 同じ /64 の端
        ("2001:DB8:1:2:0:0:0:1", "2001:db8:1:2::/64"),  # 書き方(大文字・省略なし)が違っても、同じキー
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),  # 次の /64 は、別のキー
        ("::ffff:203.0.113.5", "203.0.113.5"),  # IPv4 射影アドレスは IPv4 に直す(そのまま /64 にすると、IPv4 の利用者が全員 ::/64 の 1 つのキーになる)
        ("::ffff:cb00:7105", "203.0.113.5"),
        ("fe80::1%eth0", "fe80::/64"),  # ゾーン ID が付いていても壊れない
        ("unknown", "unknown"),  # 分からない要求の共有キーは、そのまま
        ("not an address", "not an address"),  # IP として読めない値(テストの任意の文字列)も、そのまま
        ("", ""),
    ],
)
def test_the_client_key_is_the_ipv4_address_or_the_ipv6_64_prefix(address, expected):
    assert normalize_client(address) == expected


def test_every_address_in_one_ipv6_64_prefix_has_the_same_key_and_another_prefix_has_another_key():
    rng = random.Random(20260705)
    network = ipaddress.IPv6Network("2001:db8:aa:bb::/64")
    keys = {normalize_client(str(network[rng.randrange(network.num_addresses)])) for _ in range(300)}

    assert keys == {"2001:db8:aa:bb::/64"}
    neighbours = [ipaddress.IPv6Network(f"2001:db8:aa:{suffix}::/64") for suffix in ("ba", "bc", "cb")]
    assert all(normalize_client(str(other[rng.randrange(1000)])) not in keys for other in neighbours)


def test_the_client_key_is_made_from_the_last_forwarded_address_and_keeps_the_unknown_key():
    # client_ip の取り方(X-Forwarded-For の最後の要素)は変えず、その値をキーにする(台帳 C-3・C-71)
    assert client_key(_request("10.0.0.1, 2001:db8:1:2::77")) == "2001:db8:1:2::/64"
    assert client_key(_request("2001:db8:9::1, 198.51.100.7")) == "198.51.100.7"  # 先頭側(利用者が書ける値)は、見ない
    assert client_key(_request()) == UNKNOWN_CLIENT == "unknown"  # ヘッダも接続元もない
    assert client_key(Request({"type": "http", "headers": [], "client": ("2001:db8:5:6::1", 1)})) == "2001:db8:5:6::/64"  # 接続元


async def test_the_guard_counts_the_addresses_of_one_ipv6_64_prefix_as_one_client_and_keys_the_document_by_the_prefix(
    default_db, clock
):
    # AC-13(v23): IPv6 は同じ /64 の中のアドレスが同じ枠になる。別の /64 は別の枠。文書の ID の元も /64 の接頭辞(アドレス単位では数えない)。
    limiter = _limiter(default_db, clock, _config(per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, "demo_run": 2}))
    async with _http(_guarded_app(limiter)) as client:
        for address in ("2001:db8:1:2::1", "2001:DB8:1:2:ffff::2"):  # 同じ /64 の 2 つのアドレスで、枠(2 回)を使い切る
            assert (await client.get("/probe", headers={"X-Forwarded-For": address})).status_code == 200
        same_prefix = await client.get("/probe", headers={"X-Forwarded-For": "2001:db8:1:2:1234:5678:9abc:def0"})
        another_prefix = await client.get("/probe", headers={"X-Forwarded-For": "2001:db8:1:3::1"})
        ipv4 = await client.get("/probe", headers={"X-Forwarded-For": "203.0.113.5"})

    assert same_prefix.status_code == 429  # 3 つ目のアドレスも、同じ枠
    assert (another_prefix.status_code, ipv4.status_code) == (200, 200)
    window = int(clock.now().timestamp()) // WINDOW
    expected = hmac.new(KEY, b"2001:db8:1:2::/64", hashlib.sha256).hexdigest()[:32]
    assert f"demo_run.{window}.{expected}" in _documents(default_db)  # /64 の接頭辞の文字列から作った文書(2 回の通過がここに数えられている)
    assert _documents(default_db)[f"demo_run.{window}.{expected}"]["count"] == 2


def test_the_sse_limiter_counts_an_ipv6_64_prefix_as_one_client():
    limiter = _sse(max_connections=10, max_connections_per_client=2)
    held = [limiter.acquire_for_request(_request("2001:db8:1:2::1")), limiter.acquire_for_request(_request("2001:db8:1:2:aaaa::2"))]

    with pytest.raises(HTTPException) as refused:
        limiter.acquire_for_request(_request("2001:db8:1:2:bbbb::3"))  # 同じ /64 の 3 つ目のアドレス

    assert refused.value.status_code == 429 and refused.value.detail["scope"] == "client"
    held.append(limiter.acquire_for_request(_request("2001:db8:1:3::1")))  # 別の /64 は、別の枠
    assert len(limiter) == 3
    for slot in held:
        slot.release()

# ----------------------------------------------------------------------
# 読み取りの枠(台帳 C-68・C-72・C-73): 金庫か Firestore を読む GET(セッションの有無によらない。SSE の開始を含む)に、送信元ごとの 1 分あたりの枠(メモリ)
# ----------------------------------------------------------------------


def test_the_anonymous_read_config_holds_the_design_limit():
    # §8.2(v23・v24): 送信元ごとに 1 分 120 回(キーの名前は、最初に作ったときのまま)
    assert DEFAULT_ANONYMOUS_READ_LIMIT_CONFIG == AnonymousReadLimitConfig(per_minute=120)
    assert ANONYMOUS_READ_WINDOW_SECONDS == 60


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: re.sub(r"^anonymous_read_per_minute = \d+", "anonymous_read_per_minute = 0", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^anonymous_read_per_minute = \d+", "anonymous_read_per_minute = 1.5", text, flags=re.MULTILINE),
        lambda text: re.sub(r"^anonymous_read_per_minute = \d+\n", "", text, flags=re.MULTILINE),  # 足りない項目
    ],
    ids=["zero", "float", "missing"],
)
def test_the_anonymous_read_config_rejects_values_that_do_not_make_sense(tmp_path, edit):
    path = _broken_config(tmp_path, edit)
    with pytest.raises(ValueError, match="web.limits"):
        load_anonymous_read_limit_config(path)


def test_the_body_limit_config_holds_the_design_values_and_rejects_nonsense(tmp_path):
    # §8.2(v23・v24): 本文の全体の上限は 64 KB([web.limits] max_request_body_bytes)、本文を読み切るまでの期限は 10 秒(request_body_timeout_seconds。X-90)
    assert DEFAULT_WEB_CONFIG.limits.max_request_body_bytes == 65536
    assert DEFAULT_WEB_CONFIG.limits.request_body_timeout_seconds == 10
    for edit in (
        lambda text: text.replace("max_request_body_bytes = 65536\n", ""),  # 足りない項目
        lambda text: text.replace("max_request_body_bytes = 65536", "max_request_body_bytes = 0"),
    ):
        with pytest.raises(ValueError, match="max_request_body_bytes"):
            load_web_config(_broken_config(tmp_path, edit))
    for edit in (
        lambda text: text.replace("request_body_timeout_seconds = 10\n", ""),  # 足りない項目
        lambda text: text.replace("request_body_timeout_seconds = 10", "request_body_timeout_seconds = 0"),
        lambda text: text.replace("request_body_timeout_seconds = 10", "request_body_timeout_seconds = -3"),
    ):
        with pytest.raises(ValueError, match="request_body_timeout_seconds"):
            load_web_config(_broken_config(tmp_path, edit))


def _read_limiter(clock, per_minute: int = 120) -> AnonymousReadLimiter:
    return AnonymousReadLimiter(clock, AnonymousReadLimitConfig(per_minute=per_minute))


def test_a_client_is_admitted_120_times_in_a_minute_and_the_121st_is_refused_with_the_time_left(clock):
    limiter = _read_limiter(clock)
    for _ in range(120):
        limiter.admit("203.0.113.5")
    clock.advance(dt.timedelta(seconds=20))

    with pytest.raises(AnonymousReadLimitReached) as raised:
        limiter.admit("203.0.113.5")

    assert (raised.value.limit, raised.value.retry_after_seconds) == (120, 40)  # 窓の終わりまでの秒数


def test_each_client_has_its_own_read_allowance_and_the_count_starts_again_at_the_window_boundary(clock):
    limiter = _read_limiter(clock, per_minute=3)
    for _ in range(3):
        limiter.admit("a")
    for _ in range(10):  # 断られ続ける
        with pytest.raises(AnonymousReadLimitReached):
            limiter.admit("a")
    limiter.admit("b")  # 別のクライアントは、別の枠

    clock.set(dt.datetime(2026, 1, 1, 0, 0, 59, 500000, tzinfo=dt.timezone.utc))
    with pytest.raises(AnonymousReadLimitReached) as raised:
        limiter.admit("a")
    assert raised.value.retry_after_seconds == 1  # 0.5 秒残っていても、1 秒未満には丸めない

    clock.set(dt.datetime(2026, 1, 1, 0, 1, 0, tzinfo=dt.timezone.utc))  # 次の窓の始まり
    for _ in range(3):  # 窓が変われば、また 3 回通る
        limiter.admit("a")
    with pytest.raises(AnonymousReadLimitReached) as again:
        limiter.admit("a")
    assert again.value.retry_after_seconds == 60  # 窓の始まりでは、窓の長さぶん残っている


def test_the_read_limiter_remembers_only_the_clients_of_the_current_window(clock):
    limiter = _read_limiter(clock)
    for index in range(50):
        limiter.admit(f"client-{index}")
    assert len(limiter) == 50

    clock.advance(dt.timedelta(minutes=1))
    limiter.admit("client-0")

    assert len(limiter) == 1  # 窓が変わったら、前の窓のクライアントの記録は捨てる(増え続けない)


# 数える GET の見本(本番の app と同じ経路の型。{x} は適当な値)。数えない経路は、本番と同じ表(web.app の READ_LIMIT_EXEMPT_GET_ROUTES)だけが決める。
COUNTED_PROBE_PATHS = [
    "/v1/demo/negotiations/n1/activity",  # セッションなしで金庫を読む GET(デモ)
    "/v1/demo/negotiations/n1/stage",
    "/v1/demo/attack/negotiations/n1/events",
    "/v1/demo/attack/walls/2/n1",
    "/v1/demo/attack/walls/3/n1",
    "/v1/principals/p1/negotiations",  # セッションのある GET(C-73: セッションは GET /start で誰でも作れるので、線引きにしない)
    "/v1/negotiations/n1/panels",
    "/v1/principals/p1/interview/state",
    "/v1/session",
    "/v1/jobs",  # 表に名前がない GET は、金庫を読まなくても数える(表は、数えない経路の限定)
    "/v1/demo/attack/walls/1/example",
    "/v1/demo/meter/simulation",
    "/v1/stream/demo/negotiations/n1/activity",  # SSE の開始(C-72・X-89)
    "/v1/stream/negotiations/n1/activity",
]
EXEMPT_PROBE_PATHS = [
    "/static/api.js",  # 静的ファイル
    "/health",
    "/start",  # 独自の枠(session_start)
    "/v1/interview/notice",  # 面談の入口の注記
    "/v1/demo/cases",  # ケースとリプレイの一覧(フィクスチャ)
    "/v1/demo/replays/1",
    "/api/tee/attestation",  # 独自の転送の間隔
]


def _read_probe_app(per_minute: int) -> tuple[FastAPI, AnonymousReadLimiter]:
    """読み取りの枠だけを掛けた小さな app(本番と同じ表 READ_LIMIT_EXEMPT_GET_ROUTES で、数えない経路を選ぶ)。"""
    limiter = _read_limiter(FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)), per_minute)
    app = FastAPI()
    for path in (*COUNTED_PROBE_PATHS, *EXEMPT_PROBE_PATHS):
        app.add_api_route(path, lambda: {"ok": True}, methods=["GET"])
    app.add_api_route("/v1/demo/negotiations/n1/events", lambda: {"ok": True}, methods=["POST", "GET"])
    app.add_middleware(AnonymousReadLimitMiddleware, limiter=limiter, exempt_routes=tuple(READ_LIMIT_EXEMPT_GET_ROUTES))
    return app, limiter


@pytest.mark.parametrize("path", COUNTED_PROBE_PATHS)
async def test_a_get_that_reads_the_vault_or_firestore_is_counted_with_or_without_a_session_and_refused_with_a_429(path):
    app, limiter = _read_probe_app(per_minute=1)
    async with _http(app) as client:
        first = await client.get(path)
        second = await client.get(path)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {
        "detail": {
            "code": "rate_limited",
            "entrance": "anonymous_read",
            "scope": "client",
            "limit": 1,
            "window_seconds": 60,
            "retry_after_seconds": 60,
        }
    }
    assert second.headers["Retry-After"] == "60"
    assert len(limiter) == 1


@pytest.mark.parametrize("path", EXEMPT_PROBE_PATHS)
async def test_the_gets_in_the_exemption_table_are_not_counted(path):
    app, limiter = _read_probe_app(per_minute=1)
    async with _http(app) as client:
        statuses = {(await client.get(path)).status_code for _ in range(5)}
        counted = await client.get("/v1/demo/negotiations/n1/activity")  # 枠は、1 回も使っていない

    assert statuses == {200} and counted.status_code == 200 and len(limiter) == 1


async def test_a_get_to_a_path_that_has_no_route_is_counted_too():
    # 表にない GET は、経路がなくても数える(足し忘れた経路が、枠から漏れない側に倒す。セッションのミドルウェアは、経路がなくても、クッキーがあれば利用記録を読む)
    app, limiter = _read_probe_app(per_minute=1)
    async with _http(app) as client:
        first = await client.get("/no/such/route")
        second = await client.get("/no/such/route")

    assert (first.status_code, second.status_code) == (404, 429) and len(limiter) == 1


@pytest.mark.parametrize(
    "path",
    ["/health/", "/start/x", "/v1/interview/notice/", "/v1/demo/replays/1/2", "/v1/demo/replays/", "/v1/demo/cases/1", "/api/tee/attestation/x", "/static", "/me/x", "//health", "/Health"],
)
def test_a_path_that_only_looks_like_an_exempt_one_is_counted(path, clock):
    # 数えない経路の型は、ルーターと同じ正規表現で、経路の全体に当てる。前置きや大文字小文字・余分な区切りで、数えない側に抜けられない。
    middleware = AnonymousReadLimitMiddleware(None, limiter=_read_limiter(clock), exempt_routes=tuple(READ_LIMIT_EXEMPT_GET_ROUTES))

    assert middleware.counts(path)


async def test_only_get_requests_are_counted():
    # POST は、それぞれの入口の枠(demo_run など)が受け持つ。POST まで数えると、読み取りの枠を、書き込みで食ってしまう。
    app, limiter = _read_probe_app(per_minute=1)
    async with _http(app) as client:
        posted = [await client.post("/v1/demo/negotiations/n1/events") for _ in range(3)]
        counted = await client.get("/v1/demo/negotiations/n1/events")

    assert [response.status_code for response in posted] == [200, 200, 200]
    assert counted.status_code == 200 and len(limiter) == 1


async def test_the_read_limit_counts_by_the_client_key_so_the_same_ipv6_64_prefix_shares_one_allowance():
    app, _ = _read_probe_app(per_minute=2)
    path = "/v1/demo/negotiations/n1/activity"
    async with _http(app) as client:
        for address in ("2001:db8:1:2::1", "2001:db8:1:2::2"):
            assert (await client.get(path, headers={"X-Forwarded-For": address})).status_code == 200
        same_prefix = await client.get(path, headers={"X-Forwarded-For": "10.0.0.1, 2001:db8:1:2:aaaa::3"})
        another_prefix = await client.get(path, headers={"X-Forwarded-For": "2001:db8:1:3::1"})
        other_ipv4 = await client.get(path, headers={"X-Forwarded-For": "198.51.100.9"})

    assert same_prefix.status_code == 429  # 同じ /64(先頭側の値は見ない)
    assert (another_prefix.status_code, other_ipv4.status_code) == (200, 200)


# --- GET の経路の分類(台帳 C-72・C-73): 本番の app の GET の経路の全体を、「数える」「数えない」のどちらかに分類させる ---

# 数えると決めた GET の経路(本番の app の経路の型。金庫か Firestore を読む。セッションの有無によらない。SSE の開始を含む)。数えない経路は web.app の READ_LIMIT_EXEMPT_GET_ROUTES。
# 実行時は、表にない GET は、すべて数える(足し忘れの安全側)。この一覧は、経路を足したときに、数えるか数えないかを、人が決めたことを残すためのもの。
COUNTED_GET_ROUTES = frozenset(
    {
        # セッションなしの口(デモ・攻撃・メーター)
        "/v1/demo/negotiations/{nid}/events",
        "/v1/demo/negotiations/{nid}/activity",
        "/v1/demo/negotiations/{nid}/panels",
        "/v1/demo/negotiations/{nid}/stage",
        "/v1/demo/attack/negotiations/{nid}/events",
        "/v1/demo/attack/walls/1/example",
        "/v1/demo/attack/walls/2/{nid}",
        "/v1/demo/attack/walls/3/{nid}",
        "/v1/demo/meter/simulation",
        # セッションのある口(本人)
        "/v1/principals/{pid}/policy",
        "/v1/principals/{pid}/negotiations",
        "/v1/principals/{pid}/ledger",
        "/v1/principals/{pid}/panels",
        "/v1/negotiations/{nid}/events",
        "/v1/negotiations/{nid}/activity",
        "/v1/negotiations/{nid}/panels",
        "/v1/negotiations/{nid}/stage",
        # 面談の読み取り(状態はメモリだが、セッションのミドルウェアが利用記録 [Firestore] を読む)
        "/v1/principals/{pid}/interview/state",
        "/v1/principals/{pid}/interview/axes",
        "/v1/principals/{pid}/interview/choices",
        "/v1/principals/{pid}/interview/confirmation",
        "/v1/principals/{pid}/interview/worst-case",
        "/v1/principals/{pid}/interview/companies",
        # 画面に要る口
        "/v1/session",
        "/v1/jobs",
        # SSE の開始(再接続を含む。1 回と数える)
        "/v1/stream/negotiations/{nid}/activity",
        "/v1/stream/demo/negotiations/{nid}/activity",
        # 画面のページ(設計書の「数えない GET」にない。有効なクッキーがあるとセッションの確認〔Firestore〕が走る)
        "/",
        "/interview",
        "/me",
        "/demo",
        "/attack",
    }
)


def _get_route_templates(app) -> set[str]:
    """app の GET の経路の型の全体(include したルーターの中も、マウントした /static も含む)。

    FastAPI 0.141 は、include_router したルーターを app.routes に展開せず、遅延で持つので、iter_route_contexts でたどる。
    マウントは、その下のすべての経路を受けるので、"{マウントの経路}/{path:path}" の 1 つとして数える(静的ファイルは GET を受ける)。
    """
    templates: set[str] = set()
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, Mount):
            templates.add(f"{route.path}/{{path:path}}")
        elif "GET" in (context.methods or ()):
            templates.add(context.path)
    return templates


def _assert_every_get_route_is_classified(app) -> None:
    """app の GET の経路が、すべて「数える」(COUNTED_GET_ROUTES)か「数えない」(READ_LIMIT_EXEMPT_GET_ROUTES)のどちらかにあること。どちらにもなければ失敗する。"""
    unclassified = _get_route_templates(app) - COUNTED_GET_ROUTES - set(READ_LIMIT_EXEMPT_GET_ROUTES)
    assert not unclassified, (
        f"GET の経路が分類されていない: {sorted(unclassified)}"
        "(金庫か Firestore を読むなら、このファイルの COUNTED_GET_ROUTES に。数えない理由があるなら、web.app の READ_LIMIT_EXEMPT_GET_ROUTES に、理由つきで)"
    )


def _plain_and_tee_apps(entry):
    """本番の起動口(create_app_from_env)で組み立てた、TEE なしと TEE ありの app。"""
    plain = entry(VAULT_TEE=None, VAULT_SERVICE_ACCOUNT=None, GOOGLE_CLOUD_PROJECT=None, VAULT_RELEASES_FILE=None)
    return plain, entry()


def test_every_get_route_of_the_production_app_is_classified_as_counted_or_exempt(entry):
    plain, tee = _plain_and_tee_apps(entry)

    for app in (plain, tee):
        _assert_every_get_route_is_classified(app)
    plain_routes, tee_routes = _get_route_templates(plain), _get_route_templates(tee)
    assert len(plain_routes) > 30  # 経路の取り出しが壊れて、何も確かめずに通らない
    assert tee_routes - plain_routes == {TEE_ATTESTATION_PATH} and plain_routes <= tee_routes  # TEE のときだけ、attestation の口がある
    exempt = set(READ_LIMIT_EXEMPT_GET_ROUTES)
    assert not (COUNTED_GET_ROUTES & exempt)  # どちらか一方だけ
    assert (COUNTED_GET_ROUTES | exempt) - tee_routes == set()  # 分類にあって、本番の app にない経路(古くなった記述)が残っていない
    # 数えない経路は、設計書 §8.2 の限定どおり(静的ファイル・/health・入口の注記・ケースとリプレイの一覧・attestation・GET /start)
    assert exempt - {"/", "/interview", "/me", "/demo", "/attack"} == {
        "/static/{path:path}",
        "/health",
        "/start",
        "/v1/interview/notice",
        "/v1/demo/cases",
        "/v1/demo/replays/{case}",
        TEE_ATTESTATION_PATH,
    }
    assert all(READ_LIMIT_EXEMPT_GET_ROUTES.values())  # どれも、数えない理由が書いてある


async def test_the_classification_check_fails_when_a_get_route_is_not_classified(web_app):
    # 分類のない GET を足すと、見張りの試験が落ちる(足し忘れを、人が決めるまで通さない)。include したルーターの中の経路も、マウントも見つける。
    app = web_app.app
    _assert_every_get_route_is_classified(app)  # 足す前は、通る

    app.add_api_route("/v1/new/reads-the-vault", lambda: {}, methods=["GET"])
    with pytest.raises(AssertionError, match="/v1/new/reads-the-vault"):
        _assert_every_get_route_is_classified(app)

    router = APIRouter()
    router.add_api_route("/v1/included/reads-firestore", lambda: {}, methods=["GET"])
    app.include_router(router)
    app.mount("/legacy", FastAPI())
    with pytest.raises(AssertionError) as raised:
        _assert_every_get_route_is_classified(app)
    for found in ("/v1/new/reads-the-vault", "/v1/included/reads-firestore", "/legacy/{path:path}"):
        assert found in str(raised.value)
    # POST だけの経路は、GET ではないので、分類は要らない
    app.add_api_route("/v1/new/only-post", lambda: {}, methods=["POST"])
    assert "/v1/new/only-post" not in _get_route_templates(app)


def _sample_path(template: str) -> str:
    """経路の型の {x} を、適当な値にした経路(実際に GET する)。"""
    return re.sub(r"\{\w+\}", "x1", re.sub(r"\{\w+:path\}", "app.css", template))


async def test_the_read_limit_counts_exactly_the_get_routes_classified_as_counted(
    clock, vault_client, default_db, session_key, monkeypatch
):
    # 経路の分類と、実際のミドルウェアの判断が合っていること: 本番の組み立て(create_app)の app に、GET の経路の全体を 1 回ずつ送り、枠に数えられた経路が、
    # 「数える」に分類した経路と一致する(TEE ありの app も。attestation は数えない側)。経路ごとに別の送信元にして、どの経路が数えられたかを見分ける。
    from test_web_tee_api import RELEASES, FakeAttestationSource

    plain = create_app(vault=vault_client, default_db=default_db, session_key=session_key, clock=clock)
    tee_config = TeeAttestationConfig(FakeAttestationSource(), RELEASES)
    tee = create_app(vault=vault_client, default_db=default_db, session_key=session_key, clock=clock, tee=tee_config)

    for app in (plain, tee):
        admitted: list[str] = []
        limiter = app.state.services.read_limiter
        real_admit = limiter.admit

        def recording_admit(client, real_admit=real_admit, admitted=admitted):
            admitted.append(client)
            real_admit(client)

        monkeypatch.setattr(limiter, "admit", recording_admit)
        templates = sorted(_get_route_templates(app))
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://web.test") as client:
            for index, template in enumerate(templates):
                await client.get(_sample_path(template), headers={"X-Forwarded-For": f"198.51.100.{index + 1}"})

        counted = {template for index, template in enumerate(templates) if f"198.51.100.{index + 1}" in admitted}
        assert len(admitted) == len(counted)  # 1 経路 1 回
        assert counted == set(templates) - set(READ_LIMIT_EXEMPT_GET_ROUTES)
        assert counted == set(templates) & COUNTED_GET_ROUTES


# --- 数えた枠が、実際の読み出しより前に効くこと(記録する偽物で、読み出しがなかったことを示す) ---


def _recording_env(store, clock, vault_client, default_db, session_key, monkeypatch):
    """金庫の呼び出しと、Firestore の読み出し(利用記録・段の状態)・SSE の席の取得を記録する web 一式。(env, 金庫の記録, 読み出しの記録)。"""
    vault = GatedVault(vault_client)
    env = build_web_env(store=store, clock=clock, vault=vault, default_db=default_db, session_key=session_key)
    reads: list[str] = []

    def record(owner, name, label):
        original = getattr(owner, name)
        if inspect.iscoroutinefunction(original):

            async def wrapper(*args, **kwargs):
                reads.append(label)
                return await original(*args, **kwargs)

        else:

            def wrapper(*args, **kwargs):
                reads.append(label)
                return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    record(env.services.meta, "touch", "firestore:principals_meta.touch")  # セッションのミドルウェアが、クッキーのある要求ごとに読む
    record(env.services.meta, "get", "firestore:principals_meta.get")
    record(env.services.stages, "is_fictional_negotiation", "firestore:stages")
    record(env.services.stream_limiter, "acquire_for_request", "sse:slot")  # SSE の席(読み出しより前に取る)
    return env, vault, reads


async def test_the_121st_get_with_a_session_in_a_minute_is_refused_before_any_vault_or_firestore_read(
    store, clock, vault_client, default_db, session_key, monkeypatch
):
    # AC-13(v24): セッションのある GET も、送信元ごとに 1 分 121 回目が 429。断った要求は、セッションの確認(利用記録の更新)にも、金庫にも届かない。
    env, vault, reads = _recording_env(store, clock, vault_client, default_db, session_key, monkeypatch)
    try:
        browser = env.browser()
        pid = await browser.register()
        paths = [f"/v1/principals/{pid}/negotiations", "/v1/principals/me/panels", f"/v1/principals/{pid}/ledger", "/v1/session"]
        reads.clear()
        vault.events.clear()

        for index in range(120):
            response = await browser.client.get(paths[index % len(paths)])
            assert response.status_code == 200, (index, paths[index % len(paths)])
        assert reads.count("firestore:principals_meta.touch") == 120 and vault.events  # 120 回は、通って、読んだ(以下の「読んでいない」が、足場の不備でない)
        reads_before, events_before = list(reads), list(vault.events)

        refused = await browser.client.get(f"/v1/principals/{pid}/negotiations")  # 121 回目
        another = await browser.client.get("/v1/session")  # 経路をまたいで、1 つの枠

        assert refused.status_code == another.status_code == 429
        assert refused.json() == {
            "detail": {
                "code": "rate_limited",
                "entrance": "anonymous_read",
                "scope": "client",
                "limit": 120,
                "window_seconds": 60,
                "retry_after_seconds": 60,
            }
        }
        assert refused.headers["Retry-After"] == "60"
        assert (reads, vault.events) == (reads_before, events_before)  # 断った要求は、何も読んでいない
        assert (await browser.client.get(f"/v1/principals/{pid}/negotiations", headers={"X-Forwarded-For": "198.51.100.77"})).status_code == 200  # 別の送信元
        env.clock.advance(dt.timedelta(seconds=60))  # 窓が変われば、また読める
        assert (await browser.client.get(f"/v1/principals/{pid}/negotiations")).status_code == 200
    finally:
        await env.aclose()


async def test_an_sse_start_is_counted_once_before_anything_is_read_and_the_121st_in_a_minute_is_refused(
    store, clock, vault_client, default_db, session_key, monkeypatch
):
    # AC-13(v24): SSE の開始(再接続を含む)も、同じ枠で 1 回と数える。席の確認・権限の確認(Firestore と金庫を読む)より前に数え、121 回目は 429。
    # 存在しない交渉への開始は、すぐ 403 で終わって席を返すので、同時本数の上限(2 本)には当たらないまま、開き直せる(これが、枠のない v23 で止められなかったもの)。
    env, vault, reads = _recording_env(store, clock, vault_client, default_db, session_key, monkeypatch)
    try:
        browser = env.browser()
        pid = await browser.register()
        nid = "0123456789abcdef"
        starts = [f"/v1/stream/negotiations/{nid}/activity", f"/v1/stream/demo/negotiations/{nid}/activity?side=candidate"]
        reads.clear()
        vault.events.clear()

        for index in range(120):
            response = await browser.client.get(starts[index % 2])
            assert response.status_code == 403, (index, starts[index % 2])  # 確認まで進んで、断られた(席は戻る)
        assert len(env.services.stream_limiter) == 0
        assert reads.count("sse:slot") == 120 and "firestore:stages" in reads and vault.events  # 確認の読み出しまで進んだ(足場)
        reads_before, events_before = list(reads), list(vault.events)

        refused_own = await browser.client.get(starts[0])  # 121 回目
        refused_demo = await browser.client.get(starts[1])
        session_get = await browser.client.get(f"/v1/principals/{pid}/negotiations")  # SSE の開始と、セッションのある GET は、1 つの枠

        assert refused_own.status_code == refused_demo.status_code == session_get.status_code == 429
        assert refused_own.json()["detail"]["entrance"] == "anonymous_read"
        assert (reads, vault.events) == (reads_before, events_before)  # 席の確認にも、権限の確認(Firestore・金庫)にも届いていない
        env.clock.advance(dt.timedelta(seconds=60))
        assert (await browser.client.get(starts[0])).status_code == 403  # 窓が変われば、確認まで進む
    finally:
        await env.aclose()


async def test_the_121st_anonymous_read_in_a_minute_is_refused_with_retry_after_and_another_client_is_not(make_env):
    # AC-13(v23): セッションなしで金庫を読む口は、送信元ごとに 1 分 121 回目が 429(Retry-After つき)。経路をまたいで 1 つの枠。別の送信元は影響されない。
    env = make_env()
    browser = env.browser()
    nid = (await post(browser, CREATE, create_body(1))).json()["nid"]  # 架空人物の攻撃の交渉(読み出しの相手)
    reads = [
        (f"/v1/demo/negotiations/{nid}/activity", {"side": "candidate"}),
        (f"/v1/demo/negotiations/{nid}/panels", {}),
        (f"/v1/demo/attack/negotiations/{nid}/events", {}),
    ]
    for index in range(120):
        path, params = reads[index % len(reads)]
        response = await browser.client.get(path, params=params)
        assert response.status_code == 200, (index, path)

    refused = await browser.client.get(f"/v1/demo/negotiations/{nid}/stage")  # 121 回目(別の経路でも、同じ枠)
    again = await browser.client.get(f"/v1/demo/attack/walls/3/{nid}")

    assert refused.status_code == again.status_code == 429
    detail = refused.json()["detail"]
    assert detail == {
        "code": "rate_limited",
        "entrance": "anonymous_read",
        "scope": "client",
        "limit": 120,
        "window_seconds": 60,
        "retry_after_seconds": 60,
    }
    assert refused.headers["Retry-After"] == "60"
    other = await browser.client.get(f"/v1/demo/negotiations/{nid}/panels", headers={"X-Forwarded-For": "198.51.100.77"})
    assert other.status_code == 200  # 別の送信元は、影響されない

    # 窓が変われば、また読める
    env.clock.advance(dt.timedelta(seconds=60))
    assert (await browser.client.get(f"/v1/demo/negotiations/{nid}/stage")).status_code == 200
    # 読み取りは、全体の枠(300)にも Firestore にも数えない: rate_limits にあるのは、交渉の作成(attack_create)の分だけ
    counters = {path: data for path, data in dump_documents(env.default_db).items() if path.startswith("rate_limits/")}
    assert sorted(path.split("/")[1].split(".")[0] for path in counters) == ["attack_create", "overall"]
    assert [data["count"] for path, data in counters.items() if path.startswith("rate_limits/overall.")] == [1]


def test_the_two_second_polling_fallback_of_the_pages_stays_well_inside_the_default_read_limit(clock):
    # 画面の再取得(SSE がつながらないときの 2 秒ごと。static/ui.js の pollInterval)は、1 ページで毎分 30 回(両側を 1 回で返す /panels)。
    # 攻撃画面は、記録が届くたびに壁 3 を 1 回読み直す(続けて届くときはまとめて 1 回)ので、最大 60 回。デモの画面と攻撃画面を両方開いて、
    # 5 分続けても(毎分 30 + 60 = 90 回)、1 分 120 回の枠に収まる。画面のコードから数字を引く試験は tests/test_ui_static.py(v24 で、SSE の開始・セッションのある GET も数える)。
    poll_seconds = 2
    streams = ("demo /panels", "attack /panels", "attack walls/3")  # 同じ送信元から、同時に動く読み取りの流れ
    limit = DEFAULT_ANONYMOUS_READ_LIMIT_CONFIG.per_minute
    assert len(streams) * (60 // poll_seconds) < limit  # 毎分 90 回 < 120 回

    limiter = _read_limiter(clock, limit)
    for _ in range(5 * 60 // poll_seconds):
        for _stream in streams:
            limiter.admit("203.0.113.5")  # 例外にならない(= 429 にならない)
        clock.advance(dt.timedelta(seconds=poll_seconds))


# ----------------------------------------------------------------------
# 本文の全体の上限と、読み取りの期限(台帳 X-85・X-90): すべての要求の本文を、ルートの前で 64 KB までに抑え、読み切ってから内側に渡す
# ----------------------------------------------------------------------

BODY_LIMIT = DEFAULT_WEB_CONFIG.limits.max_request_body_bytes
JSON_HEADERS = {**REQUESTED_WITH, "Content-Type": "application/json"}
# 本文を宣言したルート(FastAPI が、依存より先に本文を読む)・自分で本文を読むルート(ルートごとの 32 KB の上限がある)・本文を宣言しないルートの、代表。
# セッションも枠も要らない形で呼ぶ(本文の上限は、それより先に効く)。
BODY_ROUTES = [
    "/v1/principals/someone/interview/begin",
    "/v1/principals/someone/interview/profile",
    "/v1/principals/someone/interview/salary/answers",
    "/v1/principals/someone/negotiations",
    "/v1/principals/someone/blocklist",
    "/v1/principals/someone/delete",
    "/v1/negotiations/0123456789abcdef/principal-answer",
    "/v1/negotiations/0123456789abcdef/stage/meet",
    "/v1/demo/negotiations",
    "/v1/demo/meter",
    "/v1/demo/attack/negotiations",
    "/v1/demo/attack/walls/1",
]


@pytest.mark.parametrize("path", BODY_ROUTES)
async def test_a_body_over_64_kb_is_refused_with_413_before_the_route_reads_or_parses_it(web_app, path):
    # AC-13(v23): 64 KB を超える本文は、解析の前に 413(Content-Length が超えていれば、本文を読まずに断る)。本文は JSON ですらないので、ルートが読めば 422 になる。
    browser = web_app.browser()

    refused = await browser.client.post(path, content=b"x" * (BODY_LIMIT + 1), headers=JSON_HEADERS)

    assert (refused.status_code, refused.json()) == (413, {"detail": "request_body_too_large"})
    assert refused.headers["Connection"] == "close"  # 読み切っていない本文を残さない


async def test_a_body_of_exactly_the_limit_is_handed_to_the_route_and_one_byte_more_is_refused(web_app):
    # 比べるために: 小さい壊れた JSON は 422(ルートが解析した)。ちょうど 64 KB の壊れた本文も 422(上限は超えていない)。1 バイト超えれば 413。
    browser = web_app.browser()
    path = "/v1/demo/negotiations"

    small = await browser.client.post(path, content=b"x" * 100, headers=JSON_HEADERS)
    at_limit = await browser.client.post(path, content=b"x" * BODY_LIMIT, headers=JSON_HEADERS)
    over = await browser.client.post(path, content=b"x" * (BODY_LIMIT + 1), headers=JSON_HEADERS)

    assert (small.status_code, at_limit.status_code, over.status_code) == (422, 422, 413)


async def test_a_body_over_64_kb_is_refused_before_the_session_the_header_check_and_the_rate_limits(web_app, monkeypatch):
    # ミドルウェアの順(外 → 内): 本文の上限 → セッション。有効なクッキーがあっても、利用記録に触れず、入口の枠を数えない。
    # X-Requested-With がなくても 413(セッションのミドルウェアの 403 より先)。
    browser = web_app.browser()
    pid = await browser.register()
    touched, admitted = [], []

    async def touch(principal_id):
        touched.append(principal_id)

    async def admit(entrance, client):
        admitted.append(entrance)

    monkeypatch.setattr(web_app.services.meta, "touch", touch)
    monkeypatch.setattr(web_app.services.limiter, "admit", admit)
    oversized = b"x" * (BODY_LIMIT + 1)

    with_cookie = await browser.client.post(f"/v1/principals/{pid}/interview/begin", content=oversized, headers=JSON_HEADERS)
    without_header = await browser.client.post(f"/v1/principals/{pid}/interview/begin", content=oversized)  # X-Requested-With なし

    assert with_cookie.status_code == without_header.status_code == 413
    assert (touched, admitted) == ([], [])  # セッションのミドルウェアにも、入口の枠にも、届いていない


@pytest.mark.parametrize("path", ["/v1/demo/negotiations", "/v1/principals/someone/interview/begin", "/v1/demo/meter"])
async def test_a_body_without_a_declared_length_is_counted_while_it_is_read_and_cut_off_at_64_kb(web_app, path):
    # AC-13(v23): 宣言のない本文(チャンク送信)も、読みながら数えて 413。上限を超えた時点で、読むのをやめる(4,096 バイトずつ 16 個 = ちょうど 64 KB
    # までは読み、17 個目で超えて止まる。残りの 83 個は、読まない)。
    browser = web_app.browser()
    pulled = []

    async def chunks():
        for index in range(100):
            pulled.append(index)
            yield b"a" * 4096

    response = await browser.client.post(path, content=chunks(), headers=JSON_HEADERS)

    assert (response.status_code, response.json()) == (413, {"detail": "request_body_too_large"})
    assert response.headers["Connection"] == "close"
    assert len(pulled) == 17


async def test_a_chunked_body_with_a_valid_session_is_refused_with_413_before_the_session_touch_the_lock_or_the_route(web_app, monkeypatch):
    # AC-13(v24。X-90): 宣言のない大きな本文は、有効なクッキーつきでも、セッションのミドルウェア(利用記録の更新・依頼者のロック)より前に 413 になる。
    # 本文を読まないルート(discard)も、上限の判定なしに状態を変えない。v23 では、本文を読むルートが読むまで数えなかったので、413 の前にこれらが走った。
    browser = web_app.browser()
    pid = await browser.register()
    touched, locked, called = [], [], []
    real_touch, real_lock = web_app.services.meta.touch, web_app.services.locks.lock

    async def touch(principal_id):
        touched.append(principal_id)
        return await real_touch(principal_id)

    def lock(principal_id):
        locked.append(principal_id)
        return real_lock(principal_id)

    monkeypatch.setattr(web_app.services.meta, "touch", touch)
    monkeypatch.setattr(web_app.services.locks, "lock", lock)
    monkeypatch.setattr(web_app.services.interview, "discard", lambda principal_id: called.append(principal_id) or {"status": "discarded"})
    pulled = []

    async def chunks():
        for index in range(100):
            pulled.append(index)
            yield b"a" * 4096

    for route in ("begin", "discard"):  # 本文を宣言するルートと、本文を読まないルート
        pulled.clear()
        response = await browser.client.post(f"/v1/principals/{pid}/interview/{route}", content=chunks(), headers=JSON_HEADERS)

        assert (response.status_code, response.json()) == (413, {"detail": "request_body_too_large"}), route
        assert response.headers["Connection"] == "close" and len(pulled) == 17, route
    assert (touched, locked, called) == ([], [], [])  # セッションにも、ロックにも、ルートにも届いていない

    # 対照(足場の不備でないこと): 小さな本文なら、同じ要求が、セッションのミドルウェアにもルートにも届く
    async def small():
        yield b"{}"

    response = await browser.client.post(f"/v1/principals/{pid}/interview/discard", content=small(), headers=JSON_HEADERS)

    assert response.status_code == 200
    assert (touched, locked, called) == ([pid], [pid], [pid])


async def test_a_slow_body_times_out_with_408_without_reaching_the_session_or_the_route(make_env, monkeypatch):
    # AC-13(v24。X-90): 本文が期限(ここでは 0.2 秒)までに読み切れなければ 408。セッションのミドルウェアにもルートにも渡さない。応答には Connection: close。
    short = dataclasses.replace(
        DEFAULT_WEB_CONFIG, limits=dataclasses.replace(DEFAULT_WEB_CONFIG.limits, request_body_timeout_seconds=0.2)
    )
    env = make_env(config=short)
    browser = env.browser()
    pid = await browser.register()
    touched, begun = [], []

    async def touch(principal_id):
        touched.append(principal_id)
        raise AssertionError("the session middleware must not run for a body that timed out")

    monkeypatch.setattr(env.services.meta, "touch", touch)
    monkeypatch.setattr(env.services.interview, "begin", lambda *args: begun.append(args) or {})

    async def stalled():
        yield b'{"restart": '
        await asyncio.sleep(30)  # 残りが届かない(期限で打ち切られるので、30 秒は待たない)
        yield b"false}"

    started = time.monotonic()
    response = await browser.client.post(f"/v1/principals/{pid}/interview/begin", content=stalled(), headers=JSON_HEADERS)

    assert (response.status_code, response.json()) == (408, {"detail": "request_body_timeout"})
    assert response.headers["Connection"] == "close"
    assert time.monotonic() - started < 5
    assert (touched, begun) == ([], [])


async def test_normal_small_posts_still_work_with_and_without_a_declared_length(make_env):
    # 本文を読み切ってから渡し直しても、普通の小さな POST は、そのまま通る(Content-Length あり・チャンク送信)。ルートは、本文の全体を読む。
    env = make_env()
    browser = env.browser()
    body = json.dumps(create_body(1)).encode()

    declared = await post(browser, CREATE, content=body)

    async def in_chunks():
        for start in range(0, len(body), 20):
            yield body[start : start + 20]

    chunked = await browser.client.post(CREATE, content=in_chunks(), headers=JSON_HEADERS)
    empty = await browser.client.post("/v1/principals/someone/delete", headers=REQUESTED_WITH)  # 本文のない POST(Content-Length: 0)も、待たずに通る

    assert declared.status_code == 200
    assert chunked.status_code == 200 and chunked.json() == declared.json()  # 同じ request_id の再送: 同じ交渉
    assert empty.status_code == 401  # 本文のない要求が、ルートまで届いている(セッションがないので 401)


async def test_the_limit_counts_a_body_on_any_method_and_leaves_requests_without_a_body_alone(web_app):
    browser = web_app.browser()

    plain = await browser.client.get("/health")
    with_body = await browser.client.request("GET", "/health", content=b"x" * (BODY_LIMIT + 1))

    assert plain.status_code == 200
    assert (with_body.status_code, with_body.json()) == (413, {"detail": "request_body_too_large"})


async def test_the_per_route_limits_stay_in_place_under_the_overall_limit(make_env):
    # ルートごとの上限(攻撃 32 KB)は、全体の上限(64 KB)の内側にそのまま残る: 33,000 バイトは、全体の上限は通り、攻撃の口の上限で断る(body_too_large)。
    env = make_env()
    browser = env.browser()
    body = b'{"request_id":"request-0001","instruction":"' + b"a" * 33000 + b'"}'

    refused = await post(browser, CREATE, content=body)

    assert (refused.status_code, refused.json()) == (413, {"detail": "body_too_large"})


# --- ミドルウェアそのもの(ASGI のまま) ---

CHUNKED = [(b"transfer-encoding", b"chunked")]


async def _call(app, headers: list[tuple[bytes, bytes]], chunks: list[bytes], *, method: str = "POST") -> list[dict]:
    """ミドルウェアを ASGI のまま呼ぶ(本文は chunks を 1 個ずつ渡す。最後の 1 個だけ more_body が偽)。app が送ったメッセージを返す。"""
    sent: list[dict] = []
    pending = list(chunks)

    async def receive():
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": method, "path": "/", "headers": headers}, receive, send)
    return sent


def _reading_app(seen: list[int]):
    """本文をすべて読んで、読んだバイト数を seen に残し、200 を返す app。"""

    async def app(scope, receive, send):
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        seen.append(total)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def _limit(app, *, max_bytes: int = 10, timeout_seconds: float = 5) -> RequestBodyLimitMiddleware:
    return RequestBodyLimitMiddleware(app, max_bytes=max_bytes, timeout_seconds=timeout_seconds)


async def test_the_middleware_refuses_a_declared_length_over_the_limit_without_calling_the_app():
    seen: list[int] = []
    middleware = _limit(_reading_app(seen))

    sent = await _call(middleware, [(b"content-length", b"11")], [b"x" * 11])

    assert sent[0]["status"] == 413 and (b"connection", b"close") in sent[0]["headers"]
    assert json.loads(sent[1]["body"]) == {"detail": "request_body_too_large"}
    assert seen == []  # 下流の app は、呼ばれていない(本文も読んでいない)


@pytest.mark.parametrize("declared", [b"abc", b"-5", b""], ids=["text", "negative", "empty"])
async def test_a_content_length_that_is_not_a_number_is_ignored_and_the_bytes_are_counted_instead(declared):
    # 数字でない宣言は信じない: 宣言なしと同じに、読みながら数える。ちょうど上限は通り、超えた時点で 413(下流の app は呼ばない)。
    seen: list[int] = []
    middleware = _limit(_reading_app(seen))

    ok = await _call(middleware, [(b"content-length", declared)], [b"x" * 10])
    assert ok[0]["status"] == 200 and seen == [10]
    over = await _call(middleware, [(b"content-length", declared)], [b"x" * 6, b"y" * 5])
    assert over[0]["status"] == 413 and (b"connection", b"close") in over[0]["headers"]
    assert seen == [10]  # 下流の app は、2 回目には呼ばれていない


async def test_a_declared_length_smaller_than_the_real_body_is_not_trusted():
    # 宣言(5)が小さくても、実際に届いた本文のバイト数を数える
    seen: list[int] = []
    middleware = _limit(_reading_app(seen))

    sent = await _call(middleware, [(b"content-length", b"5")], [b"x" * 6, b"y" * 6])

    assert sent[0]["status"] == 413 and seen == []


async def test_the_body_is_read_to_the_end_before_the_app_is_called_and_handed_over_as_one_message():
    # X-90: 本文は、内側(セッションのミドルウェアとルート)を呼ぶ前に、すべて読む。渡し直した本文は 1 つのメッセージ(more_body は偽)。
    # 渡し直したあとの receive は、元の receive のまま(切断の知らせが届く)。
    events: list[str] = []
    pending = [b"abc", b"def", b"gh"]

    async def receive():
        if pending:
            events.append(f"server:{pending[0].decode()}")
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        events.append("server:disconnect")
        return {"type": "http.disconnect"}

    received: list[dict] = []

    async def app(scope, inner_receive, send):
        events.append("app:start")
        received.append(await inner_receive())
        received.append(await inner_receive())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await _limit(app)({"type": "http", "method": "POST", "path": "/", "headers": CHUNKED}, receive, send)

    assert events == ["server:abc", "server:def", "server:gh", "app:start", "server:disconnect"]  # 本文を読み切ってから、app が始まる
    assert received == [{"type": "http.request", "body": b"abcdefgh", "more_body": False}, {"type": "http.disconnect"}]
    assert sent[0]["status"] == 204


async def test_a_body_that_is_not_complete_within_the_time_gets_a_408_and_the_app_is_not_called():
    # X-90: 期限は、本文の全体に対する(チャンクごとではない): 0.03 秒おきに届き続けても、全体が 0.1 秒を超えれば 408。
    called: list[int] = []

    async def app(scope, receive, send):
        called.append(1)

    async def receive():
        await asyncio.sleep(0.03)
        return {"type": "http.request", "body": b"x", "more_body": True}  # 終わらない

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    started = time.monotonic()
    await _limit(app, max_bytes=10_000, timeout_seconds=0.1)({"type": "http", "method": "POST", "path": "/", "headers": CHUNKED}, receive, send)

    assert time.monotonic() - started < 2
    assert sent[0]["status"] == 408 and (b"connection", b"close") in sent[0]["headers"]
    assert json.loads(sent[1]["body"]) == {"detail": "request_body_timeout"}
    assert called == []  # 下流の app は、呼ばれていない


async def test_a_body_that_stalls_before_its_first_byte_also_gets_a_408():
    called: list[int] = []

    async def app(scope, receive, send):
        called.append(1)

    async def receive():
        await asyncio.sleep(30)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    declared = [(b"content-length", b"100")]  # 100 バイトと宣言して、1 バイトも送らない
    await _limit(app, max_bytes=1000, timeout_seconds=0.05)({"type": "http", "method": "POST", "path": "/", "headers": declared}, receive, send)

    assert sent[0]["status"] == 408 and called == []


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"0")], [(b"accept", b"application/json")]], ids=["no-headers", "length-0", "other-headers"])
async def test_a_request_without_a_body_is_passed_on_at_once_without_reading_anything(headers):
    # X-90: 本文のない要求(GET・HEAD など。Content-Length がない・0 で、Transfer-Encoding もない)は、待たずに、何も読まずに通す(SSE を遅らせない)。
    # この receive は、読まれたら記録して、切断まで返らない(本文のない要求の receive が、サーバによっては、そうなる)。
    reads: list[int] = []
    given: list[object] = []

    async def receive():
        reads.append(1)
        await asyncio.sleep(3600)

    async def app(scope, inner_receive, send):
        given.append(inner_receive)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    for method in ("GET", "HEAD", "POST"):
        await asyncio.wait_for(
            _limit(app, timeout_seconds=0.05)({"type": "http", "method": method, "path": "/", "headers": headers}, receive, send), 2
        )

    assert reads == []  # 1 回も読んでいない
    assert given == [receive] * 3 and [message["status"] for message in sent[::2]] == [204] * 3  # 元の receive を、そのまま渡した


async def test_a_client_that_goes_away_before_the_body_is_complete_gets_no_response_and_the_app_is_not_called():
    called: list[int] = []

    async def app(scope, receive, send):
        called.append(1)

    messages = iter([{"type": "http.request", "body": b"ab", "more_body": True}, {"type": "http.disconnect"}])

    async def receive():
        return next(messages)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await _limit(app)({"type": "http", "method": "POST", "path": "/", "headers": CHUNKED}, receive, send)

    assert (called, sent) == ([], [])  # 返す相手がいない


async def test_a_body_over_the_limit_stops_the_reading_at_the_chunk_that_crosses_it():
    pulled: list[int] = []

    async def app(scope, receive, send):
        raise AssertionError("not called")

    async def receive():
        pulled.append(1)
        return {"type": "http.request", "body": b"x" * 4, "more_body": True}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await _limit(app, max_bytes=10)({"type": "http", "method": "POST", "path": "/", "headers": CHUNKED}, receive, send)

    assert len(pulled) == 3 and sent[0]["status"] == 413  # 4 + 4 + 4 バイト目で 10 を超えた: そこで止める


async def test_the_middleware_passes_other_scopes_through_and_refuses_a_nonsense_limit():
    events: list[str] = []

    async def inner(scope, receive, send):
        events.append(scope["type"])

    await _limit(inner)({"type": "lifespan"}, None, None)

    assert events == ["lifespan"]
    for bad in (0, -1):
        with pytest.raises(ValueError, match="max_bytes"):
            _limit(inner, max_bytes=bad)
    for bad in (0, -1, -0.5):
        with pytest.raises(ValueError, match="timeout_seconds"):
            _limit(inner, timeout_seconds=bad)


@pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_methods_other_than_get_and_post_are_refused_before_the_session_touch(method):
    # 有効なクッキーの有無によらず、GET・POST 以外は、クッキーの読み出しとセッションの確認(利用記録の touch)より前に 405(v24。C-73 の抜け道)
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from web.session_middleware import PrincipalSessionMiddleware

    calls = []

    class Refuse:
        def read(self, *args, **kwargs):
            calls.append("codec.read")
            raise AssertionError("the cookie must not be read")

        async def touch(self, *args, **kwargs):
            calls.append("meta.touch")
            raise AssertionError("touch must not run")

        def lock(self, *args, **kwargs):
            calls.append("locks.lock")
            raise AssertionError("the principal lock must not be taken")

    inner = Starlette(routes=[Route("/v1/session", lambda request: PlainTextResponse("ok"), methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"])])
    middleware = PrincipalSessionMiddleware.__new__(PrincipalSessionMiddleware)
    middleware.app = inner
    middleware._session_free_prefixes = ()
    middleware._codec = middleware._meta = middleware._locks = Refuse()
    client = TestClient(middleware)
    response = client.request(method, "/v1/session", headers={"Cookie": "anon_session=whatever", "X-Requested-With": "x"})

    assert response.status_code == 405
    assert calls == []
