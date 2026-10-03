"""攻撃モード・入口ごとのレート制限・3 枚の壁のテストで共通に使う部品(design.md §8.1・§8.2)。

`test_` で始まらないので pytest には収集されない(tests/web_app_helpers.py と同じ扱い)。フィクスチャ(make_env)は、
使うテストファイルが import して使う。

本物の LLM・GCP には接続しない。金庫は本物の vault の app を ASGI のままつなぎ、エージェントは台本(IdleAgents)、時計は注入、
壁 1 の送信は偽の送信関数(RecordingSender)か、agents の app につないだ通信路。
"""

import asyncio
import dataclasses

import pytest
from negotiation_core import Anchor, Policy
from vault.models import NegotiationDocument
from vault.serialization import model_from_firestore
from vault.templates import put_template
from vault_helpers import make_candidate_template, make_employer_template
from web.attack import DEFAULT_ATTACK_CONFIG
from web.attack.raw_message import RawReply
from web.limits import DEFAULT_RATE_LIMIT_CONFIG, RateLimitConfig
from web_app_helpers import REQUESTED_WITH, build_web_env

CONFIG = DEFAULT_ATTACK_CONFIG
CREATE = "/v1/demo/attack/negotiations"
RAW = "/v1/demo/attack/walls/1"
EXAMPLE = "/v1/demo/attack/walls/1/example"
CANARY = "CANARY-ATTACK-7F3A"  # 攻撃の指示に入れる、どこにも残らないはずの文字列

REJECTED = RawReply(
    200, {"jsonrpc": "2.0", "id": "x", "error": {"code": -32602, "message": "message must contain exactly one DataPart"}}
)


class RecordingSender:
    """壁 1 の送信関数の形の偽物。送られたメッセージ(JSON の文字列)を記録して、決まった応答を返す。"""

    def __init__(self, reply: RawReply = REJECTED) -> None:
        self.reply = reply
        self.sent: list[str] = []

    async def __call__(self, message_json: str, *, timeout_s: float) -> RawReply:
        self.sent.append(message_json)
        return self.reply


def put_attack_templates(store, *, candidate_policy: Policy | None = None) -> None:
    """設定の名前で、攻撃の相手のテンプレートを金庫に置く(架空の候補者と、何でも受ける求人)。同じ名前なら置き換える。"""
    put_template(store._db, make_candidate_template(template_id=CONFIG.candidate_template_id, policy=candidate_policy))
    put_template(store._db, make_employer_template(template_id=CONFIG.employer_template_id))


def threshold_candidate_policy(salary: int = 700) -> Policy:
    """年収が salary 以上なら受ける、その 1 段下(50 万)以下なら受けない候補者(ほかの軸は気にしない)。"""

    def anchor(value: int, **worst) -> Anchor:
        return Anchor(salary=value, training="*", side_job="*", start="*", **worst)

    return Policy(
        side="candidate",
        accept_anchors=[anchor(salary, remote_days=0, night_duty=8, review_months=12)],
        reject_anchors=[anchor(salary - 50, remote_days=5, night_duty=0, review_months=6)],
    )


def small_limits(**per_client) -> RateLimitConfig:
    """設定ファイルの値から、入口ごとの上限を替えたレート制限の設定(per_client で替える入口だけ指定)。"""
    return dataclasses.replace(
        DEFAULT_RATE_LIMIT_CONFIG, per_client={**DEFAULT_RATE_LIMIT_CONFIG.per_client, **per_client}
    )


@pytest.fixture
async def make_env(store, clock, vault_client, default_db, session_key):
    """web の app 一式を作る工場(テストの終わりに、作った app のタスクを止める)。攻撃の相手のテンプレートも置く。"""
    created = []
    put_attack_templates(store)

    def factory(**kwargs):
        env = build_web_env(
            store=store, clock=clock, vault=vault_client, default_db=default_db, session_key=session_key, **kwargs
        )
        created.append(env)
        return env

    yield factory
    for env in created:
        await env.aclose()


async def post(browser, path: str, body: dict | None = None, *, ip: str | None = None, content: bytes | None = None):
    """X-Requested-With つきの POST(ip を渡すと X-Forwarded-For の末尾にする。content を渡すと、その本文のまま)。"""
    headers = dict(REQUESTED_WITH)
    if ip is not None:
        headers["X-Forwarded-For"] = ip
    if content is not None:
        headers["Content-Type"] = "application/json"
        return await browser.client.post(path, content=content, headers=headers)
    return await browser.client.post(path, json=body, headers=headers)


def create_body(index: int = 1, instruction: str = "年収の境目を探って") -> dict:
    return {"request_id": f"request-{index:04d}", "instruction": instruction}


def message_of_size(size: int) -> bytes:
    """ちょうど size バイトの、JSON のオブジェクト(メッセージとして有効かどうかは問わない)。"""
    prefix, suffix = b'{"padding":"', b'"}'
    return prefix + b"a" * (size - len(prefix) - len(suffix)) + suffix


async def run_to_the_end(env, nid: str) -> None:
    """nid のレフェリーのタスクが終わる(交渉が終わる)まで待つ。"""
    await asyncio.wait_for(env.services.referees.task(nid), 30)


def vault_document(store, nid: str) -> NegotiationDocument:
    """金庫の交渉の文書(正本)。"""
    return model_from_firestore(NegotiationDocument, store._negotiation_ref(nid).get().to_dict())
