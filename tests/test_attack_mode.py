"""AC-13: 攻撃モードと入口ごとのレート制限(design.md §8.2・§12.1。台帳 C-1・C-3・C-9・X-10・L4-2・L7-3・P-17)。

設計書 §12.1 の AC-13 の項目と、このファイルのテスト:
- 401 文字の指示と 32 KB を超える生メッセージは拒否
  → test_an_instruction_of_401_characters_is_refused_and_400_is_accepted・test_a_body_over_32_kb_is_refused_before_it_is_read_as_json・
  test_a_raw_message_over_32_kb_is_refused_by_web_and_exactly_32_kb_is_forwarded
- 入口ごとの上限を超えると 429 / ある入口の枠を使い切っても、別の入口は使える
  → test_each_entrance_answers_429_after_its_limit・test_using_up_one_entrance_leaves_the_others_usable・
  test_the_default_limits_apply_to_the_http_entrances(設定ファイルの値で、デモの実行 10・壁 1 の生メッセージ 20)
- X-Forwarded-For の先頭側を偽っても別枠にならない → test_forging_the_head_of_x_forwarded_for_makes_no_new_allowance
- 全体で 301 回目は 429 → test_the_301st_request_overall_is_refused
- アプリを作り直しても(再起動の模擬)数えた回数が残る → test_the_counts_survive_rebuilding_the_app
- 架空人物以外の相手には攻撃モードの交渉が作れない → test_an_attack_negotiation_is_created_only_against_the_fictional_candidate
- 候補者側の受信口に届くのは TurnInput だけ → test_only_turn_input_reaches_the_candidate_and_the_instruction_only_the_attacker
そのほか、攻撃の指示は web のメモリにだけ持つ(Firestore・金庫・ログに出ない。台帳 P-17)、再起動で消えた攻撃の交渉は「なし」で終わる、
動いている交渉の指示は次の手番から効く、攻撃の交渉のイベントを攻撃側から読める、入場の制限が掛かる。

壁 1〜3 の実演の API は tests/test_attack_walls.py、攻撃モードと本物の依頼者の分離は tests/test_attacker_isolation.py。
金庫は本物の vault の app を ASGI のままつなぎ、エージェントは台本(IdleAgents)。壁 1 の送信は偽の送信関数。
"""

import dataclasses
import logging

import pytest
from attack_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    CANARY,
    CONFIG,
    CREATE,
    RAW,
    RecordingSender,
    create_body,
    make_env,
    message_of_size,
    post,
    put_attack_templates,
    run_to_the_end,
    small_limits,
    threshold_candidate_policy,
    vault_document,
)
from negotiation_core import AttackerTurnInput, TurnInput
from vault_helpers import sample_package
from web.attack.router import MAX_INSTRUCTION_CHARS
from web.config import DEFAULT_WEB_CONFIG
from web.limits import ENTRANCES, OVERALL_ENTRANCES
from web_app_helpers import REQUESTED_WITH, documents_mentioning
from web_helpers import plan_dict

pytestmark = pytest.mark.anyio


# ----------------------------------------------------------------------
# 入力の大きさ(指示 400 文字・本文 32 KB)
# ----------------------------------------------------------------------


def test_the_instruction_limit_is_the_one_of_the_attacker_turn_input():
    # §2.7・§8.2: 指示の上限は 400 文字(AttackerTurnInput.principal_instruction と同じ値。受信口がこれ以上は受け付けない)。
    assert MAX_INSTRUCTION_CHARS == AttackerTurnInput.model_fields["principal_instruction"].metadata[0].max_length == 400
    assert CONFIG.max_body_bytes == 32768  # 本文の上限は 32 KB(§4.3 の受信本文の上限と同じ)


async def test_an_instruction_of_401_characters_is_refused_and_400_is_accepted(make_env, store):
    # AC-13: 401 文字の指示は拒否(作成も、動いている交渉の指示の置き換えも)。400 文字は通る(文字数で数える。バイト数ではない)。
    env = make_env()
    browser = env.browser()

    refused = await post(browser, CREATE, create_body(1, "あ" * 401))
    assert refused.status_code == 422
    assert "あ" * 10 not in refused.text  # エラーに、入力の値を返さない
    assert len(env.services.attack.contexts) == 0
    assert await env.vault.get_negotiation_by_request("attack:request-0001") is None  # 金庫には何も作っていない

    accepted = await post(browser, CREATE, create_body(2, "あ" * 400))
    assert accepted.status_code == 200
    assert "set-cookie" not in accepted.headers  # 訪問者を見分ける ID は作らない(台帳 L7-3)
    nid = accepted.json()["nid"]
    assert env.services.attack.contexts.instruction_for(nid) == "あ" * 400

    update = f"{CREATE}/{nid}/instruction"
    assert (await post(browser, update, {"instruction": "い" * 401})).status_code == 422
    assert env.services.attack.contexts.instruction_for(nid) == "あ" * 400  # 置き換わっていない
    assert (await post(browser, update, {"instruction": "い" * 400})).status_code == 200
    assert env.services.attack.contexts.instruction_for(nid) == "い" * 400
    # 空の指示・指示のない本文・余計な項目も拒否
    for bad in ({"instruction": ""}, {}, {"instruction": "x", "mode": "live"}):
        assert (await post(browser, update, bad)).status_code == 422, bad


async def test_the_attack_posts_need_the_x_requested_with_header_and_count_nothing_without_it(make_env, default_db):
    # §6.3: 状態を変える POST は X-Requested-With が必須(デモ用のエンドポイントも。ミドルウェア)。ヘッダのない POST は、
    # 入口の枠も数えず、何も作らない。
    env = make_env(send_raw=RecordingSender())
    browser = env.browser()
    for path, body in (
        (CREATE, create_body(1)),
        (f"{CREATE}/0123456789abcdef/instruction", {"instruction": "x"}),
        (RAW, {"messageId": "m"}),
    ):
        response = await browser.client.post(path, json=body)  # ヘッダなし
        assert (response.status_code, response.json()) == (403, {"detail": "missing_requested_with_header"})

    assert list(default_db.collection("rate_limits").list_documents()) == []
    assert len(env.services.attack.contexts) == 0


async def test_a_body_over_32_kb_is_refused_before_it_is_read_as_json(make_env):
    # 本文の上限は 32 KB: 超えた本文は、JSON として読む前に 413 で断る(指示の API も同じ)。
    env = make_env()
    browser = env.browser()
    oversized = b'{"request_id":"request-0001","instruction":"' + b"a" * 33000 + b'"}'

    created = await post(browser, CREATE, content=oversized)
    assert created.status_code == 413 and created.json() == {"detail": "body_too_large"}
    first = await post(browser, CREATE, create_body(2))
    nid = first.json()["nid"]
    updated = await post(browser, f"{CREATE}/{nid}/instruction", content=oversized)
    assert updated.status_code == 413
    assert len(env.services.attack.contexts) == 1


async def test_a_body_without_a_declared_length_is_cut_off_at_32_kb_while_it_is_read(make_env):
    # 本文の長さを宣言しない(チャンク送信)でも、読んだバイト数が 32 KB を超えた時点で断る(メモリを使い切らせない)。
    sender = RecordingSender()
    env = make_env(send_raw=sender)
    browser = env.browser()

    async def chunks():
        for _ in range(10):
            yield b"a" * 4096  # 合計 40 KB

    for path in (CREATE, RAW):
        response = await browser.client.post(path, content=chunks(), headers={**REQUESTED_WITH, "Content-Type": "application/json"})
        assert response.status_code == 413, path
    assert sender.sent == [] and len(env.services.attack.contexts) == 0


async def test_a_raw_message_over_32_kb_is_refused_by_web_and_exactly_32_kb_is_forwarded(make_env):
    # AC-13・台帳 C-9: 壁 1 の生メッセージは、web の受信口で 32 KB の上限が掛かる。超えたら agents へ送らずに 413(止まった場所は web)。
    # ちょうど 32 KB は、web を通って agents へ送られる(agents の受信口にも 32 KB の上限がある)。
    sender = RecordingSender()
    env = make_env(send_raw=sender)
    browser = env.browser()

    refused = await post(browser, RAW, content=message_of_size(CONFIG.max_body_bytes + 1))
    assert refused.status_code == 413
    body = refused.json()
    assert (body["outcome"], body["stopped_at"], body["llm_called"], body["registered_in_vault"]) == ("rejected", "web", False, False)
    assert body["rejection"] == {"reason": "body_too_large", "limit_bytes": 32768}
    assert sender.sent == []

    at_limit = await post(browser, RAW, content=message_of_size(CONFIG.max_body_bytes))
    assert at_limit.status_code == 200
    assert len(sender.sent) == 1 and len(sender.sent[0].encode("utf-8")) == CONFIG.max_body_bytes


# ----------------------------------------------------------------------
# 入口ごとのレート制限(HTTP の入口)
# ----------------------------------------------------------------------


class Entrances:
    """HTTP の 5 つの入口に 1 回ずつ要求を送る。入口ごとに、その要求を成り立たせる準備を持つ。

    ほかの入口の枠は、別の試験で確かめる: 面談の interview_llm・interview_begin は tests/test_interview_api.py、開始ページの session_start は
    tests/test_web_api.py、メーターの meter は tests/test_meter.py。入口の一覧と全体の枠の扱いは tests/test_limits.py。
    """

    def __init__(self, env, store, *, start: int = 0) -> None:
        self.env = env
        self.store = store
        self.browser = env.browser()
        self.index = start  # request_id の通し番号の始まり(同じテストの別の app と、request_id が重ならないように)
        self._attack_nid: str | None = None
        self._pid: str | None = None
        self._demo_templates: dict | None = None
        self._employer_template_id: str | None = None

    async def send(self, entrance: str, *, ip: str | None = None):
        self.index += 1
        if entrance == "attack_create":
            return await post(self.browser, CREATE, create_body(self.index), ip=ip)
        if entrance == "attack_instruction":
            if self._attack_nid is None:  # 動いている交渉を 1 つ用意する(この作成は別の入口の枠を使うので、別の IP で作る)
                self._attack_nid = (await post(self.browser, CREATE, create_body(900), ip="192.0.2.250")).json()["nid"]
            return await post(self.browser, f"{CREATE}/{self._attack_nid}/instruction", {"instruction": f"指示 {self.index}"}, ip=ip)
        if entrance == "raw_message":
            return await post(self.browser, RAW, {"messageId": "m"}, ip=ip)
        if entrance == "demo_run":
            if self._demo_templates is None:
                from vault_helpers import put_candidate_and_employer_templates

                candidate, employer = put_candidate_and_employer_templates(self.store._db)
                self._demo_templates = {"candidate_template_id": candidate.template_id, "employer_template_id": employer.template_id}
            body = {"request_id": f"request-demo{self.index:04d}", **self._demo_templates}
            return await post(self.browser, "/v1/demo/negotiations", body, ip=ip)
        assert entrance == "live_negotiation_create"
        if self._pid is None:
            self._pid = await self.browser.register()
            self._employer_template_id = self.env.put_employer_template()
        body = {"request_id": f"request-live{self.index:04d}", "employer_template_id": self._employer_template_id}
        return await post(self.browser, f"/v1/principals/{self._pid}/negotiations", body, ip=ip)


HTTP_ENTRANCES = ["attack_create", "attack_instruction", "raw_message", "demo_run", "live_negotiation_create"]


@pytest.mark.parametrize("entrance", HTTP_ENTRANCES)
async def test_each_entrance_answers_429_after_its_limit(make_env, store, entrance):
    # AC-13: 入口ごとの上限を超えると 429。Retry-After(窓の終わりまでの秒数)と、理由(入口・枠・上限)を返す。
    # 上限は 3 回に下げて確かめる(設定ファイルの値で効くことは test_the_default_limits_apply_to_the_http_entrances)。
    env = make_env(send_raw=RecordingSender(), rate_limits=small_limits(**{name: 3 for name in ENTRANCES}))
    entrances = Entrances(env, store)

    for _ in range(3):
        allowed = await entrances.send(entrance)
        assert allowed.status_code != 429, (allowed.status_code, allowed.text)  # 通る(ライブ交渉は 2 回目から金庫が断る 409 でもよい)
    refused = await entrances.send(entrance)

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "600"
    assert refused.json() == {
        "detail": {
            "code": "rate_limited",
            "entrance": entrance,
            "scope": "client",
            "limit": 3,
            "window_seconds": 600,
            "retry_after_seconds": 600,
        }
    }


async def test_using_up_one_entrance_leaves_the_others_usable(make_env, store):
    # AC-13・台帳 L4-2: ある入口の枠を使い切っても、別の入口は使える(枠は入口ごとに別)。
    env = make_env(send_raw=RecordingSender(), rate_limits=small_limits(**{name: 2 for name in ENTRANCES}))
    entrances = Entrances(env, store)
    for _ in range(2):
        assert (await entrances.send("demo_run")).status_code == 200
    assert (await entrances.send("demo_run")).status_code == 429

    for entrance in ("attack_create", "raw_message", "live_negotiation_create"):
        assert (await entrances.send(entrance)).status_code != 429, entrance


async def test_the_default_limits_apply_to_the_http_entrances(make_env, store):
    # §8.2 の表(設定ファイルの値): デモの実行は 10 回/10 分(11 回目で 429)、壁 1 の生メッセージは 20 回/10 分(21 回目で 429)。
    env = make_env(send_raw=RecordingSender())
    entrances = Entrances(env, store)

    demo_statuses = [(await entrances.send("demo_run")).status_code for _ in range(11)]
    assert demo_statuses == [200] * 10 + [429]
    raw_statuses = [(await entrances.send("raw_message")).status_code for _ in range(21)]
    assert raw_statuses == [200] * 20 + [429]


async def test_forging_the_head_of_x_forwarded_for_makes_no_new_allowance(make_env, store):
    # AC-13・台帳 C-3: クライアントは X-Forwarded-For の末尾(Cloud Run が追記した値)。利用者が書ける先頭側を偽っても、別枠にならない。
    env = make_env(rate_limits=small_limits(attack_create=3))
    entrances = Entrances(env, store)
    for forged in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        assert (await entrances.send("attack_create", ip=f"{forged}, 198.51.100.7")).status_code == 200

    forged_again = await entrances.send("attack_create", ip="192.0.2.99, 198.51.100.7")
    other_client = await entrances.send("attack_create", ip="198.51.100.8")

    assert forged_again.status_code == 429  # 先頭側を替えても、末尾が同じなら同じ枠
    assert other_client.status_code == 200  # 末尾が違えば、別のクライアント


async def test_the_301st_request_overall_is_refused(make_env, store):
    # AC-13: 全体で 301 回目は 429(設定ファイルの 300)。IP の取り方が崩れても(クライアントごとの枠に当たらなくても)効く。
    env = make_env(send_raw=RecordingSender())
    for index in range(299):  # 別々のクライアントが、全体に数える入口をめぐらせて 299 回(HTTP を通さずに数える。読み出しだけの入口は、全体に数えない。台帳 L19-7)
        await env.services.limiter.admit(OVERALL_ENTRANCES[index % len(OVERALL_ENTRANCES)], f"client-{index}")
    entrances = Entrances(env, store)

    assert (await entrances.send("raw_message", ip="198.51.100.1")).status_code == 200  # 300 回目
    refused = await entrances.send("raw_message", ip="198.51.100.2")  # 301 回目。このクライアントの枠には余りがある

    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert (detail["scope"], detail["limit"], detail["entrance"]) == ("overall", 300, "raw_message")


async def test_the_counts_survive_rebuilding_the_app(make_env, store):
    # AC-13・台帳 X-10: アプリを作り直しても(再起動の模擬)数えた回数が残る。残りの枠だけ通り、あとは 429。
    limits = small_limits(attack_create=3)
    first = Entrances(make_env(rate_limits=limits), store)
    for _ in range(2):
        assert (await first.send("attack_create")).status_code == 200

    restarted = Entrances(make_env(rate_limits=limits), store, start=100)  # 新しい app(新しいリミッター)。同じ Firestore
    assert (await restarted.send("attack_create")).status_code == 200  # 3 回目
    assert (await restarted.send("attack_create")).status_code == 429


# ----------------------------------------------------------------------
# 作成: 相手は架空人物だけ。指示は web のメモリにだけ持つ
# ----------------------------------------------------------------------


async def test_an_attack_negotiation_is_created_only_against_the_fictional_candidate(make_env, store, vault_client):
    # AC-13・台帳 C-1: 架空人物以外の相手には攻撃モードの交渉が作れない。相手(テンプレート)・モード・依頼者は、リクエストでは決められず
    # (extra=forbid)、web が設定のテンプレートで決める。金庫への作成の要求は、いつも mode=attack・架空の候補者。
    sent = []
    original = vault_client.create_negotiation

    async def spy(request):
        sent.append(request)
        return await original(request)

    vault_client.create_negotiation = spy
    env = make_env()
    browser = env.browser()
    pid = await browser.register()  # 本物の依頼者(同じブラウザ。セッションのクッキーを持っている)

    for extra in ({"principal_id": pid}, {"mode": "live"}, {"candidate_template_id": "x"}, {"employer_template_id": "x"}, {"is_fictional": False}):
        refused = await post(browser, CREATE, {**create_body(1), **extra})
        assert refused.status_code == 422, extra
    assert sent == []  # 金庫には、何も送っていない

    created = await post(browser, CREATE, create_body(2))
    assert created.status_code == 200
    (request,) = sent
    assert request.mode == "attack"
    assert request.candidate.is_fictional is True and request.candidate.principal_id is None
    assert request.candidate.template_id == CONFIG.candidate_template_id
    assert request.employer.template_id == CONFIG.employer_template_id
    document = vault_document(store, created.json()["nid"])
    assert document.mode == "attack" and document.participants.candidate.is_fictional is True
    assert document.participants.candidate.principal_id is None
    assert document.ttl_at is not None  # 架空人物の交渉なので、96 時間の期限が付く
    assert store.list_principal_negotiations(pid) == []  # 本物の依頼者の交渉にはならない


async def test_a_resent_request_returns_the_same_negotiation_and_keeps_the_first_instruction(make_env):
    # §3.5・台帳 X-57: 同じ request_id の再送は、入場の判定を通さずに、同じ交渉を返す。指示は最初のまま(再送で変わらない)。
    env = make_env()
    browser = env.browser()
    first = await post(browser, CREATE, create_body(1, "最初の指示"))
    again = await post(browser, CREATE, create_body(1, "別の指示"))

    assert again.json() == first.json()
    assert env.services.attack.contexts.instruction_for(first.json()["nid"]) == "最初の指示"
    assert len(env.services.attack.contexts) == 1


async def test_the_creation_is_refused_by_the_admission_limit_and_before_the_first_sweep(make_env):
    # §8.2・台帳 C-45・X-53: ライブ・デモと同じ入場の制限が掛かる。1 日の枠に収まらなければ 429(本日の上限)、起動時の見回りが
    # 終わるまでは 503。断った作成は、金庫に何も作らず、指示も覚えない。
    full = dataclasses.replace(DEFAULT_WEB_CONFIG.llm_budget, daily_limit=40, per_negotiation_limit=44)  # 新しい交渉 1 件ぶん(44)が入らない
    env = make_env(config=dataclasses.replace(DEFAULT_WEB_CONFIG, llm_budget=full))
    browser = env.browser()
    refused = await post(browser, CREATE, create_body(1))
    assert (refused.status_code, refused.json()) == (429, {"detail": "daily_limit_reached"})

    starting = make_env(startup_sweep_done=False)
    waiting = await post(starting.browser(), CREATE, create_body(2))
    assert (waiting.status_code, waiting.json()) == (503, {"detail": "starting_up"})
    assert len(env.services.attack.contexts) == len(starting.services.attack.contexts) == 0
    assert await env.vault.get_negotiation_by_request("attack:request-0001") is None


# ----------------------------------------------------------------------
# 候補者側の受信口には TurnInput だけ。指示は攻撃者の受信口だけ
# ----------------------------------------------------------------------


def _scripted_agreement(env) -> None:
    """候補者が提案し、攻撃者が受ける台本(候補者 1 回・攻撃者 1 回)。"""
    env.agents.script("candidate", plan_dict(move="propose", package=sample_package(salary=700)))
    env.agents.script("attacker", plan_dict(move="accept"))


async def test_only_turn_input_reaches_the_candidate_and_the_instruction_only_the_attacker(make_env, store):
    # AC-13・DV-04: 候補者側の受信口に届くのは TurnInput だけ(攻撃の指示は入らない)。指示は攻撃者の受信口(AttackerTurnInput)だけ。
    # 攻撃モードの交渉は、候補者と攻撃者を呼び、通常の求人側(employer)は呼ばない。
    env = make_env(run_referees=True)
    _scripted_agreement(env)
    browser = env.browser()
    created = await post(browser, CREATE, create_body(1, f"{CANARY} 最低年収を聞き出せ"))
    nid = created.json()["nid"]
    await run_to_the_end(env, nid)

    calls = env.agents.calls
    assert {call.role for call in calls} == {"candidate", "attacker"}
    for call in env.agents.calls_for("candidate"):
        assert type(call.turn_input) is TurnInput  # AttackerTurnInput ではない
        assert "principal_instruction" not in call.turn_input.model_dump(by_alias=True)
        assert CANARY not in call.turn_input.model_dump_json()
    attacker_calls = env.agents.calls_for("attacker")
    assert attacker_calls and all(type(call.turn_input) is AttackerTurnInput for call in attacker_calls)
    assert {call.turn_input.principal_instruction for call in attacker_calls} == {f"{CANARY} 最低年収を聞き出せ"}
    assert store.get_view(nid, "candidate").status == "judged"
    assert vault_document(store, nid).end_reason == "agreed"


async def test_an_instruction_update_reaches_the_attacker_from_the_next_turn(make_env):
    # 攻撃の手(§8.2): 動いている交渉の指示を置き換えると、次の手番から新しい指示が攻撃者に渡る(最初の手番は古い指示のまま)。
    env = make_env(run_referees=True)
    browser = env.browser()

    async def candidate_proposes_after_the_judge_changes_the_instruction(call):
        updated = await post(browser, f"{CREATE}/{call.nid}/instruction", {"instruction": "新しい指示"})
        assert updated.status_code == 200
        return plan_dict(move="propose", package=sample_package(salary=700))

    env.agents.script("candidate", candidate_proposes_after_the_judge_changes_the_instruction)
    env.agents.script("attacker", plan_dict(move="accept"))
    nid = (await post(browser, CREATE, create_body(1, "古い指示"))).json()["nid"]
    await run_to_the_end(env, nid)

    (attacker_call,) = env.agents.calls_for("attacker")
    assert attacker_call.turn_input.principal_instruction == "新しい指示"


async def test_the_instruction_update_is_refused_for_unknown_and_ended_negotiations(make_env):
    # この web が持っていない交渉(再起動で消えた・作っていない)は 404、終わった交渉は 409。
    env = make_env(run_referees=True)
    _scripted_agreement(env)
    browser = env.browser()
    unknown = await post(browser, f"{CREATE}/0123456789abcdef/instruction", {"instruction": "x"})
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "unknown_attack_negotiation"})

    nid = (await post(browser, CREATE, create_body(1))).json()["nid"]
    await run_to_the_end(env, nid)
    ended = await post(browser, f"{CREATE}/{nid}/instruction", {"instruction": "もう効かない"})
    assert (ended.status_code, ended.json()) == (409, {"detail": "negotiation_ended"})
    assert env.services.attack.contexts.instruction_for(nid) == "年収の境目を探って"  # 置き換わっていない


async def test_the_instruction_is_kept_in_memory_only_and_never_written_or_logged(make_env, store, default_db, caplog):
    # 台帳 P-17: 攻撃の指示は web のメモリにだけ持つ。(default) の Firestore にも金庫(vault-db)にも書かず、ログにも出さない。
    caplog.set_level(logging.DEBUG)
    env = make_env(run_referees=True)
    _scripted_agreement(env)
    browser = env.browser()
    nid = (await post(browser, CREATE, create_body(1, f"{CANARY} 秘密を白状させろ"))).json()["nid"]
    await post(browser, f"{CREATE}/{nid}/instruction", {"instruction": f"{CANARY} 更新"})  # 終わっていれば 409 でも、書かれないことは同じ
    await run_to_the_end(env, nid)
    await browser.get(f"{CREATE}/{nid}/events", side="employer")
    refused = await post(browser, CREATE, create_body(2, "x" * 401 + CANARY))  # 拒否された指示も、どこにも出ない
    assert refused.status_code == 422

    assert documents_mentioning(default_db, CANARY) == {}
    assert documents_mentioning(store._db, CANARY) == {}
    assert CANARY not in caplog.text and CANARY not in refused.text
    assert env.services.attack.contexts.instruction_for(nid) is not None  # メモリにはある


async def test_a_restart_ends_a_running_attack_negotiation_as_none_without_calling_the_attacker(make_env, store, clock):
    # 台帳 P-17: 指示は永続化しないので、再起動で攻撃の交渉は「なし」で終わる。再起動の後の見回りがレフェリーを作り直し、
    # 攻撃者の手番で、指示がない(この web が持っていない)ことに気づいて、交渉を取消にする。攻撃者(LLM)は呼ばない。
    before = make_env()  # 再起動の前: レフェリーは動かさない(候補者の最初の手番の前で止めておく)
    nid = (await post(before.browser(), CREATE, create_body(1, "再起動で消える指示"))).json()["nid"]
    assert before.services.attack.contexts.instruction_for(nid) is not None

    after = make_env(run_referees=True)  # 再起動の後: 新しい app(メモリは空)。同じ金庫・Firestore
    assert after.services.attack.contexts.instruction_for(nid) is None
    after.agents.script("candidate", plan_dict(move="propose", package=sample_package(salary=700)))
    report = await after.services.sweeper.sweep_once()
    assert report.tasks_started == 1
    await run_to_the_end(after, nid)

    assert after.agents.calls_for("attacker") == []  # 攻撃者の LLM は呼んでいない
    assert vault_document(store, nid).end_reason == "cancelled"
    for side in ("candidate", "employer"):
        finals = [event for event in store.get_events(nid, side) if event.kind == "final_result"]
        assert [(e.result.likelihood, e.result.package) for e in finals] == [("none", None)]


# ----------------------------------------------------------------------
# 攻撃の交渉のイベント(攻撃側から見える見え方)
# ----------------------------------------------------------------------


async def test_the_attack_events_show_the_attackers_view_and_the_fictional_candidates_view(make_env, store):
    # §8.2・§8.3: 攻撃の交渉のイベントを、攻撃側(employer。既定)の見え方で読める。架空の候補者側(金庫の答えの元。メーター)も読める。
    # after_seq 以降だけを返す。
    env = make_env(run_referees=True)
    store_candidate = threshold_candidate_policy(700)
    put_attack_templates(store, candidate_policy=store_candidate)
    env.agents.script("candidate", plan_dict(move="propose", package=sample_package(salary=900)), plan_dict(move="accept"))
    env.agents.script("attacker", plan_dict(move="propose", package=sample_package(salary=750)))
    browser = env.browser()
    nid = (await post(browser, CREATE, create_body(1))).json()["nid"]
    await run_to_the_end(env, nid)

    employer_events = (await browser.get(f"{CREATE}/{nid}/events")).json()
    assert [e["kind"] for e in employer_events] == ["offer_received", "propose", "final_result"]  # 攻撃者の見え方
    candidate_events = (await browser.get(f"{CREATE}/{nid}/events", side="candidate")).json()
    assert [e["kind"] for e in candidate_events] == ["propose", "offer_received", "final_result"]
    assert candidate_events[1]["own_evaluation"] == "acceptable"  # 750 は、年収 700 以上なら受ける候補者の「受けられる」
    later = (await browser.get(f"{CREATE}/{nid}/events", after_seq=employer_events[0]["seq"])).json()
    assert [e["kind"] for e in later] == ["propose", "final_result"]
    assert (await browser.get(f"{CREATE}/{nid}/events", side="nobody")).status_code == 422
