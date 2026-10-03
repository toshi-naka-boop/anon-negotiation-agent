"""面談の API(web/interview/api.py・service.py。design.md §5 の手順 1〜9。AC-01・AC-02・AC-17・AC-18 の面談の部分)。

金庫は本物の vault の app を ASGI のままつなぎ、web の app へは Browser(クッキーを持つ httpx のクライアント)から入る。LLM は
スタブ(ScriptedLlm)で、本物には接続しない。時計は注入(FixedClock)、sleep はしない。

- 入口の注記(最初の応答に含まれる)、権限(本人のセッションだけ)
- 手順の順番(AC-01: 年収の確認と二択 5 組が終わるまで確認画面に進めない。確認前の金庫への書き込みは 0 件)
- 軸を外す(§2.4)・受けるアンカーが 0 件の警告(I-1)・項目を消す/付け直す・矛盾・年収の換算・LLM の失敗の伝え方
- 本文 32 KB の上限(C-49)・企業の一覧とブロック先・面談の状態の寿命(メモリのみ)
- 送信のあと、生の値がどこにも残らない(AC-18: カナリアを Firestore の全文書とログに探す)・辞めた理由の原文を持たない(AC-17)
"""

import dataclasses
import datetime as dt
import json
import logging

import pytest
from agents_helpers import StubLlm, llm_response
from google.genai import errors as genai_errors
from google.genai import types

from negotiation_core import AXES, AXIS_KEYS
from web.llm_budget import LlmBudgetUnavailable, jst_date
from web.vault_client import VaultUnavailableError
from web_app_helpers import REQUESTED_WITH, documents_mentioning

BASIS = {
    "amount_man_yen": 600,
    "amount_period": "annual",
    "amount_kind": "gross",
    "bonus_included": True,
    "bonus_months": 0,
    "fixed_overtime_man_yen_per_month": 0,
}
PROFILE = {"experience_years": 7.3141, "prefecture": "神奈川県", "job_category": "it_web"}
SALARY_ANSWERS = ["年収は額面で 600 万円です", "固定残業代はありません", "賞与は含まれています"]


def statement(polarity, **axes) -> dict:
    """面談エージェントが返す発言 1 つ(8 つの項目をすべて書く。触れていない項目は null)。"""
    values = {axis: None for axis in AXIS_KEYS}
    values.update(axes)
    return {"polarity": polarity, **values}


class ScriptedLlm:
    """面談の LLM(スタブ)。入力の task で、年収の読み取りか、発言の構造化かを分けて返す。"""

    def __init__(self) -> None:
        self.salary_basis = dict(BASIS)
        self.statements = {"free_comment": [], "reason_for_leaving": []}
        self.stub = StubLlm(behavior=self._respond)

    def _respond(self, request):
        task = json.loads(request.contents[-1].parts[0].text)["task"]
        if task == "salary_basis":
            return json.dumps(self.salary_basis)
        return json.dumps({"statements": self.statements[task]})


@pytest.fixture
def llm() -> ScriptedLlm:
    return ScriptedLlm()


@pytest.fixture
async def env(web_app, llm):
    """web 一式。面談の LLM をスタブにする。"""
    web_app.services.interview.agent.use_model(llm.stub)
    return web_app


class Flow:
    """1 人の依頼者の面談を、API で進める。"""

    def __init__(self, env, browser, pid: str) -> None:
        self.env, self.browser, self.pid = env, browser, pid

    @property
    def base(self) -> str:
        return f"/v1/principals/{self.pid}/interview"

    async def get(self, suffix: str, **params):
        return await self.browser.get(f"{self.base}/{suffix}", **params)

    async def post(self, suffix: str, body: dict | None = None):
        return await self.browser.post(f"{self.base}/{suffix}", body)

    async def ok(self, method: str, suffix: str, body: dict | None = None) -> dict:
        response = await (self.get(suffix) if method == "GET" else self.post(suffix, body))
        assert response.status_code == 200, (suffix, response.status_code, response.text)
        return response.json()

    async def begin(self, **body) -> dict:
        return await self.ok("POST", "begin", body)

    async def profile(self, **overrides) -> dict:
        return await self.ok("POST", "profile", {**PROFILE, **overrides})

    async def salary(self, answers=None) -> dict:
        proposal = await self.ok("POST", "salary/answers", {"answers": answers or SALARY_ANSWERS})
        return await self.ok("POST", "salary/confirm", {"salary_basis": proposal["salary_basis"]})

    async def axes(self, removed=()) -> dict:
        return await self.ok("POST", "axes", {"removed_axes": list(removed)})

    async def answer(self, pair_id: str, option: str, response: str) -> dict:
        return await self.ok("POST", "choices/answer", {"pair_id": pair_id, "option": option, "response": response})

    async def answer_pairs(self, count: int, a: str = "go", b: str = "no_go") -> list[str]:
        """先頭から count 組に答える(A に a、B に b)。答えた組の ID を返す。"""
        pairs = (await self.ok("GET", "choices"))["pairs"]
        for pair in pairs[:count]:
            await self.answer(pair["id"], "a", a)
            await self.answer(pair["id"], "b", b)
        return [pair["id"] for pair in pairs[:count]]

    async def until_choices(self, removed=(), pairs: int = 5, **answers) -> None:
        await self.begin()
        await self.profile()
        await self.salary()
        await self.axes(removed)
        await self.answer_pairs(pairs, **answers)

    async def until_ready(self, removed=(), **answers) -> None:
        """送信できる状態まで進める(確認と「最悪ここまで」の承認を済ませる)。"""
        await self.until_choices(removed, **answers)
        await self.ok("POST", "confirm", {"proceed_without_accept_anchors": True})
        await self.ok("POST", "worst-case/approve")


@pytest.fixture
async def flow(env):
    browser = env.browser()
    return Flow(env, browser, await browser.open_start_page())


def vault_has_principal(env, pid: str) -> bool:
    return env.store._principal_ref(pid).get().exists


def meta_exists(env, pid: str) -> bool:
    return env.default_db.collection("principals_meta").document(pid).get().exists


# ---------------------------------------------------------------------------
# 入口の注記・権限
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_first_response_has_the_entrance_notice_and_nothing_else_works_before_it(flow):
    # 手順 0: 入口の注記(Vertex AI 側の記録・global エンドポイントで処理される国が決まらないこと・30 日で自動削除)が、最初の応答に含まれる。
    early = await flow.post("profile", PROFILE)
    assert (early.status_code, early.json()["detail"]) == (409, "interview_not_started")  # begin より先には、入力を受け付けない

    first = await flow.begin()

    items = {item["id"]: item["text"] for item in first["notice"]["items"]}
    assert set(items) == {"vertex_ai", "global_endpoint", "server_memory", "auto_delete"}
    assert "Vertex AI" in items["vertex_ai"] and "記録" in items["vertex_ai"]
    assert "global" in items["global_endpoint"] and "国は決まっておらず" in items["global_endpoint"]
    assert "30 日" in items["auto_delete"] and "自動で削除" in items["auto_delete"]  # web.principals.retention_seconds から作る
    assert first["state"]["stage"] == "profile"
    assert len(first["texts"]["salary_questions"]) == 3 and first["texts"]["provisional"] is True
    regions = first["texts"]["profile"]["regions"]
    assert {region["block"] for region in regions} == {
        "hokkaido_tohoku", "kanto", "chubu", "kinki", "chugoku_shikoku", "kyushu_okinawa",
    }
    assert sum(len(region["prefectures"]) for region in regions) == 47


@pytest.mark.anyio
async def test_the_steps_that_take_no_input_work_without_a_request_body(flow):
    # 本文のない POST(begin・confirm・discard など)も受け付ける。X-Requested-With だけは要る(ミドルウェア)
    async def bodyless(suffix):
        return await flow.browser.client.post(f"{flow.base}/{suffix}", headers=REQUESTED_WITH)

    assert (await bodyless("begin")).status_code == 200
    assert (await bodyless("discard")).json() == {"status": "discarded"}
    assert (await bodyless("begin")).json()["state"]["stage"] == "profile"
    refused = await bodyless("confirm")
    assert refused.status_code == 409  # 手順の前提(年収の確認など)が足りない。本文がなくても、422 にはならない


@pytest.mark.anyio
async def test_the_interview_needs_the_own_session_and_responses_are_not_cacheable(env, flow):
    anonymous = env.browser()  # クッキーなし
    no_session = await anonymous.post(f"/v1/principals/{flow.pid}/interview/begin", {})
    assert (no_session.status_code, no_session.json()["detail"]) == (401, "no_session")
    other = env.browser()
    other_pid = await other.open_start_page()
    foreign = await other.post(f"/v1/principals/{flow.pid}/interview/begin", {})
    assert (foreign.status_code, foreign.json()["detail"]) == (403, "forbidden")  # 他人の依頼者 ID
    assert (await other.get(f"/v1/principals/{flow.pid}/interview/state")).status_code == 403
    assert other_pid != flow.pid

    response = await flow.post("begin", {})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"  # 生の値を共有の置き場に残さない
    missing_header = await flow.browser.post(f"{flow.base}/discard", {}, requested_with=False)
    assert missing_header.status_code == 403  # X-Requested-With のない状態変更は拒否


# ---------------------------------------------------------------------------
# 手順の順番(AC-01)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_confirmation_is_not_reachable_until_salary_and_five_pairs_are_done_and_nothing_is_written_before_submit(
    env, flow, monkeypatch
):
    vault_requests = []
    original_send = env.services.vault._send

    async def recording_send(method, path, **kwargs):
        vault_requests.append(method)
        return await original_send(method, path, **kwargs)

    monkeypatch.setattr(env.services.vault, "_send", recording_send)
    await flow.begin()

    async def detail(method, suffix, body=None):
        response = await (flow.get(suffix) if method == "GET" else flow.post(suffix, body))
        return response.status_code, response.json().get("detail")

    # 前の手順を終えていなければ、先へは進めない(足りない手順の名前を返す)
    assert await detail("POST", "salary/answers", {"answers": SALARY_ANSWERS}) == (409, "profile_missing")
    await flow.profile()
    assert await detail("POST", "salary/confirm", {"salary_basis": BASIS}) == (409, "salary_proposal_missing")
    assert await detail("POST", "axes", {"removed_axes": []}) == (409, "salary_not_confirmed")
    assert await detail("GET", "choices") == (409, "salary_not_confirmed")
    await flow.salary()
    assert await detail("GET", "choices") == (409, "axes_not_chosen")
    assert await detail("POST", "comment", {"text": "言い足し"}) == (409, "axes_not_chosen")
    assert await detail("POST", "reason", {"text": "理由"}) == (409, "axes_not_chosen")
    await flow.axes()
    assert (await flow.ok("GET", "choices"))["required"] == 5

    # 二択を 5 組答えるまで、確認画面には進めない。答えていない・片方だけの組は数えない
    assert await detail("GET", "confirmation") == (409, "choices_incomplete")
    assert await detail("POST", "confirm", {}) == (409, "choices_incomplete")
    await flow.answer_pairs(4)
    pairs = (await flow.ok("GET", "choices"))["pairs"]
    await flow.answer(pairs[4]["id"], "a", "go")  # 5 組目は、片方だけ
    assert await detail("GET", "confirmation") == (409, "choices_incomplete")
    await flow.answer(pairs[4]["id"], "b", "no_go")
    confirmation = await flow.ok("GET", "confirmation")
    assert confirmation["entries"] and confirmation["confirmed"] is False

    # ここまでの間、金庫にも利用記録にも、何も書いていない(AC-01: 確認前に vault への書き込みは 0 件)
    assert not vault_has_principal(env, flow.pid) and not meta_exists(env, flow.pid)
    assert await detail("GET", "worst-case") == (409, "not_confirmed")
    assert await detail("POST", "worst-case/approve") == (409, "not_confirmed")
    assert await detail("POST", "submit") == (409, "not_confirmed")
    await flow.ok("POST", "confirm", {})
    assert await detail("POST", "submit") == (409, "worst_case_not_approved")
    assert not vault_has_principal(env, flow.pid) and not meta_exists(env, flow.pid)

    await flow.ok("POST", "worst-case/approve")
    assert (await flow.ok("GET", "state"))["stage"] == "ready"
    assert vault_requests == []  # 送信の前は、金庫への呼び出しが 1 回もない(読み出しも書き込みも。AC-01)

    await flow.ok("POST", "submit")
    assert vault_requests == ["PUT"]  # 送信で初めて、金庫に書く(ブロック先の手順を行っていないので、ポリシーの 1 回だけ)


@pytest.mark.anyio
async def test_changing_the_content_after_confirming_means_confirming_and_approving_again(env, flow):
    await flow.until_ready()
    assert (await flow.ok("GET", "state"))["stage"] == "ready"
    key = (await flow.ok("GET", "confirmation"))["entries"][0]["key"]

    await flow.ok("POST", f"anchors/{key}/active", {"active": False})  # 内容が変わった

    state = await flow.ok("GET", "state")
    assert state["confirmed"] is False and state["worst_case_approved"] is False and state["stage"] == "confirm"
    stale = await flow.post("submit")
    assert (stale.status_code, stale.json()["detail"]) == (409, "not_confirmed")
    assert not vault_has_principal(env, flow.pid)
    await flow.ok("POST", "confirm", {"proceed_without_accept_anchors": True})
    stale = await flow.post("submit")
    assert (stale.status_code, stale.json()["detail"]) == (409, "worst_case_not_approved")
    await flow.ok("POST", "worst-case/approve")
    assert (await flow.ok("POST", "submit"))["status"] == "submitted"


# ---------------------------------------------------------------------------
# 送信(§5 の 9)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_submitting_rounds_on_the_web_side_and_writes_the_policy_the_bands_and_the_blocklist_then_forgets_the_interview(env, flow, llm):
    llm.statements["free_comment"] = [statement("accept", salary=620.5, remote_days=0)]
    await flow.until_choices(pairs=5)
    await flow.ok("POST", "comment", {"text": "年収 620.5 万円以上ならフル出社でも行く"})
    await flow.ok("POST", "blocklist", {"company_ids": ["case1-company"]})
    await flow.ok("POST", "confirm", {})
    worst = await flow.ok("GET", "worst-case")
    assert worst["approved"] is False and [item["axis"] for item in worst["axes"]] == list(AXIS_KEYS)
    await flow.ok("POST", "worst-case/approve")
    assert not vault_has_principal(env, flow.pid)

    submitted = await flow.ok("POST", "submit")

    assert submitted == {"status": "submitted"}
    assert meta_exists(env, flow.pid)  # 利用記録は、金庫に初めて書く前に作る(§5 の 9・§6.3)
    stored = env.store.get_policy(flow.pid)
    salaries = sorted(anchor.salary for anchor in stored.policy.accept_anchors)
    assert 650 in salaries and all(salary in AXES["salary"].grid for salary in salaries)  # 620.5 → 650(良い側へ丸める)
    for anchor in [*stored.policy.accept_anchors, *stored.policy.reject_anchors]:
        for axis in ("salary", "remote_days", "night_duty", "review_months"):
            assert getattr(anchor, axis) in AXES[axis].grid  # 金庫に届いたのは、丸め済みの値だけ
    assert stored.attribute_bands.model_dump() == {"experience_band": "5_to_10y", "region_block": "kanto", "job_category": "it_web"}
    assert stored.removed_axes == []
    assert env.store._principal_ref(flow.pid).get().to_dict()["blocklist"] == ["case1-company"]
    # 面談の状態は消えている(送信後は、もう続けられない)
    assert env.services.interview.store.get(flow.pid) is None
    gone = await flow.get("state")
    assert (gone.status_code, gone.json()["detail"]) == (409, "interview_not_started")
    # 同じ依頼者が面談をやり直して送れば、置き換わる
    await flow.until_ready(removed=("night_duty",))
    await flow.ok("POST", "submit")
    assert env.store.get_policy(flow.pid).removed_axes == ["night_duty"]


@pytest.mark.anyio
async def test_skipping_the_blocklist_step_leaves_an_existing_blocklist_untouched(env, flow):
    await flow.until_choices()
    await flow.ok("POST", "blocklist", {"company_ids": ["case1-company"]})
    await flow.ok("POST", "confirm", {})
    await flow.ok("POST", "worst-case/approve")
    await flow.ok("POST", "submit")
    assert env.store._principal_ref(flow.pid).get().to_dict()["blocklist"] == ["case1-company"]

    await flow.until_ready()  # ブロック先の手順を行わない面談
    await flow.ok("POST", "submit")

    assert env.store._principal_ref(flow.pid).get().to_dict()["blocklist"] == ["case1-company"]


@pytest.mark.anyio
async def test_a_principal_being_deleted_cannot_submit(env, flow):
    await flow.until_ready()
    await env.services.meta.create_if_absent(flow.pid)
    await env.services.meta.mark_deleting(flow.pid)

    response = await flow.post("submit")

    assert response.status_code == 409  # 削除中の依頼者の操作は、ミドルウェアが拒否する(§6.3)
    assert not vault_has_principal(env, flow.pid)


# ---------------------------------------------------------------------------
# 軸を外す(§2.4)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_removed_axes_are_shown_at_their_worst_value_the_question_says_so_and_no_go_is_not_saved(env, flow, llm):
    removed = ("night_duty", "training")
    llm.statements["free_comment"] = [
        statement("reject", night_duty=4),  # 外した軸に触れる: 保存しない
        statement("accept", salary=700),  # 触れていない: §2.3 の規則で埋めて保存する
    ]
    await flow.begin()
    await flow.profile()
    await flow.salary()
    axes = await flow.ok("GET", "axes")
    assert [item["axis"] for item in axes["axes"]] == ["remote_days", "night_duty", "review_months", "training", "side_job", "start"]
    assert axes["notice"] == "この軸は、条件そのものが外から知られ得ます。" and axes["chosen"] is False
    await flow.axes(removed)

    choices = await flow.ok("GET", "choices")
    assert 5 <= len(choices["pairs"]) <= 8
    assert "night_duty" not in {pair["id"] for pair in choices["pairs"]}  # その軸のトレードオフの組は出さない
    for pair in choices["pairs"]:
        assert pair["question"] == "当直が月 8 回でも、研修があってもなくても、この条件なら行きますか?"  # §2.4 の見せ方
        for option in pair["options"].values():
            shown = {item["axis"]: item for item in option["axes"]}
            assert shown["night_duty"]["value"] == "月 8 回" and shown["night_duty"]["removed"] is True
            assert shown["training"]["value"] == "どちらでも"
            assert "当直" not in option["text"] and "研修" not in option["text"]

    first = choices["pairs"][0]["id"]
    saved = await flow.answer(first, "a", "go")
    assert saved["anchor_saved"] is True and saved["message"] is None
    not_saved = await flow.answer(first, "b", "no_go")
    assert not_saved == {**not_saved, "anchor_saved": False, "reason": "removed_axis"}
    assert "保存せず、交渉中に確認します" in not_saved["message"]  # 画面に出す文(§2.4)
    maybe = await flow.answer(choices["pairs"][1]["id"], "a", "undecided")
    assert maybe["anchor_saved"] is False and maybe["reason"] is None  # 迷うは、もともとアンカーにしない

    comment = await flow.ok("POST", "comment", {"text": "当直が月 4 回以上なら行かない。年収 700 万円以上なら行く"})
    assert [(s["saved"], s["reason"]) for s in comment["statements"]] == [(False, "removed_axis"), (True, None)]
    assert comment["statements"][0]["sentence"] == "当直が月 4 回以上なら行かない"
    assert comment["statements"][1]["sentence"] == "年収 700 万円以上なら行く"

    await flow.answer_pairs(5)
    confirmation = await flow.ok("GET", "confirmation")
    assert {entry["polarity"] for entry in confirmation["entries"]} == {"accept"}  # 受けないアンカーは 1 つもない
    assert [item["axis"] for item in confirmation["removed_axes"]] == ["night_duty", "training"]
    assert {item["source"] for item in confirmation["not_saved"]} == {"choice", "comment"}
    for entry in confirmation["entries"]:
        if entry["source"] == "choice":  # 二択の「行く」は、外した軸について中立(何も制約しない)ので、文に出ない
            assert "当直" not in entry["sentence"] and "外した軸" not in entry["sentence"]
        else:  # 自由コメントの文は、規則で埋めた値(当直なし)が残るので、本人の意向ではない印が付く
            assert "当直なし(外した軸。規則で埋めた値)" in entry["sentence"]
    await flow.ok("POST", "confirm", {})
    worst = {item["axis"]: item for item in (await flow.ok("GET", "worst-case"))["axes"]}
    assert worst["night_duty"]["removed"] is True and worst["night_duty"]["text"] == "外しています(交渉中に確認)"
    assert worst["training"]["removed"] is True
    await flow.ok("POST", "worst-case/approve")
    await flow.ok("POST", "submit")
    stored = env.store.get_policy(flow.pid)
    assert stored.removed_axes == ["night_duty", "training"] and stored.policy.reject_anchors == []
    neutral = [a for a in stored.policy.accept_anchors if a.night_duty == 8]  # 二択から作った分は、外した軸について中立
    filled = [a for a in stored.policy.accept_anchors if a.night_duty == 0]  # 自由コメント(年収 700)は、規則で埋めた値(最も良い値)
    assert len(neutral) == 5 and [a.salary for a in filled] == [700]


@pytest.mark.anyio
async def test_changing_the_removed_axes_after_the_choices_started_starts_the_choices_over(flow, llm):
    llm.statements["free_comment"] = [statement("accept", salary=700)]
    await flow.until_choices(pairs=5)
    await flow.ok("POST", "comment", {"text": "言い足し"})
    await flow.ok("POST", "confirm", {})
    assert (await flow.ok("GET", "state"))["choices"]["answered"] == 5

    result = await flow.axes(("night_duty",))  # 二択の後で外したくなったら、二択からやり直す(§2.4)

    assert result["choices_reset"] is True and result["removed_axes"] == ["night_duty"]
    state = result["state"]
    assert state["choices"]["answered"] == 0 and state["confirmed"] is False and state["stage"] == "choices"
    confirmation_attempt = await flow.get("confirmation")
    assert confirmation_attempt.json()["detail"] == "choices_incomplete"
    # 同じ選択をもう一度送っても、やり直しにはならない。外すのをやめれば、また二択からやり直す
    await flow.answer_pairs(5)
    assert (await flow.axes(("night_duty",)))["choices_reset"] is False
    assert (await flow.axes(()))["choices_reset"] is True


@pytest.mark.anyio
async def test_no_accept_anchor_warns_and_the_person_chooses_to_start_over_or_to_go_on(env, flow):
    removed = ("night_duty",)
    await flow.until_choices(removed, a="no_go", b="no_go")  # 外した軸があると「行かない」は保存されず、受けるアンカーも作られない

    confirmation = await flow.ok("GET", "confirmation")

    assert confirmation["entries"] == []
    (warning,) = confirmation["warnings"]
    assert warning["code"] == "no_accept_anchors" and warning["caused_by_removed_axes"] is True
    assert "この条件を外すと、事前に受けられる組み合わせがなくなります" in warning["message"]  # §5 の 6 の文
    assert warning["options"] == ["restart_choices", "proceed"]
    refused = await flow.post("confirm", {})
    assert (refused.status_code, refused.json()["detail"]) == (409, "no_accept_anchors")
    refused = await flow.post("confirm", {"proceed_without_accept_anchors": False})
    assert refused.status_code == 409
    # 外すのをやめて二択からやり直す
    restarted = await flow.axes(())
    assert restarted["choices_reset"] is True
    await flow.answer_pairs(5)
    assert (await flow.ok("GET", "confirmation"))["warnings"] == []
    # そのまま進むことを選ぶ(外した軸のまま)
    await flow.axes(removed)
    await flow.answer_pairs(5, a="no_go", b="no_go")
    await flow.ok("POST", "confirm", {"proceed_without_accept_anchors": True})
    await flow.ok("POST", "worst-case/approve")
    await flow.ok("POST", "submit")
    stored = env.store.get_policy(flow.pid)
    assert stored.policy.accept_anchors == [] and stored.removed_axes == ["night_duty"]


@pytest.mark.anyio
async def test_the_zero_accept_warning_is_also_given_without_removed_axes(flow):
    await flow.until_choices(a="no_go", b="undecided")
    confirmation = await flow.ok("GET", "confirmation")
    (warning,) = confirmation["warnings"]
    assert warning["caused_by_removed_axes"] is False and "途中確認" in warning["message"]
    assert confirmation["entries"] and all(entry["polarity"] == "reject" for entry in confirmation["entries"])


# ---------------------------------------------------------------------------
# 確認画面: 項目を消す・付け直す・矛盾
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_entries_can_be_removed_and_put_back_and_only_active_ones_are_submitted(env, flow):
    await flow.until_choices()
    entries = (await flow.ok("GET", "confirmation"))["entries"]
    accepts = [entry for entry in entries if entry["polarity"] == "accept"]
    assert len(accepts) == 5 and all(entry["active"] for entry in entries)
    removed_key = accepts[0]["key"]

    after = await flow.ok("POST", f"anchors/{removed_key}/active", {"active": False})
    assert next(entry for entry in after["entries"] if entry["key"] == removed_key)["active"] is False  # 一覧に残る
    assert (await flow.ok("GET", "state"))["entries"]["inactive"] == 1
    await flow.ok("POST", "confirm", {})
    await flow.ok("POST", "worst-case/approve")
    await flow.ok("POST", "submit")
    assert len(env.store.get_policy(flow.pid).policy.accept_anchors) == 4  # 消した分は金庫に送らない

    # もう一度面談して、付け直す
    await flow.until_choices()
    entries = (await flow.ok("GET", "confirmation"))["entries"]
    await flow.ok("POST", f"anchors/{entries[0]['key']}/active", {"active": False})
    again = await flow.ok("POST", f"anchors/{entries[0]['key']}/active", {"active": True})
    assert all(entry["active"] for entry in again["entries"])
    unknown = await flow.post("anchors/not-a-key/active", {"active": False})
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "unknown_entry")


@pytest.mark.anyio
async def test_contradicting_entries_block_the_confirmation_until_one_is_removed(flow, llm):
    llm.statements["free_comment"] = [statement("accept", salary=500), statement("reject", salary=550)]
    await flow.until_choices()
    await flow.ok("POST", "comment", {"text": "年収 500 万円以上なら行く。年収 550 万円以下なら行かない"})

    confirmation = await flow.ok("GET", "confirmation")

    (conflict,) = confirmation["conflicts"]
    assert conflict["accept"].startswith("comment-1-") and conflict["reject"].startswith("comment-1-")
    assert "年収 500 万円以上" in conflict["accept_sentence"] and "年収 550 万円以下" in conflict["reject_sentence"]
    refused = await flow.post("confirm", {})
    assert (refused.status_code, refused.json()["detail"]) == (409, "contradiction")
    await flow.ok("POST", f"anchors/{conflict['reject']}/active", {"active": False})
    assert (await flow.ok("GET", "confirmation"))["conflicts"] == []
    await flow.ok("POST", "confirm", {})


# ---------------------------------------------------------------------------
# 年収の正規化(§5 の 2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_salary_answers_become_a_formula_and_assumptions_that_the_person_can_correct(flow, llm):
    await flow.begin()
    await flow.profile()
    llm.salary_basis = {**BASIS, "bonus_months": 4, "fixed_overtime_man_yen_per_month": 3}

    proposal = await flow.ok("POST", "salary/answers", {"answers": SALARY_ANSWERS})

    assert proposal["normalized_man_yen"] == 564
    assert proposal["formula"] == "比較基準年収 = 額面の年間総額(賞与を含む)600 万円 − 固定残業代の年額 36 万円 = 564 万円"
    assert any("固定残業代は月 3 万円" in line for line in proposal["assumptions"])
    assert proposal["salary_basis"]["amount_kind"] == "gross"
    sent = json.loads(llm.stub.requests[0].contents[0][1][0])
    assert [item["answer"] for item in sent["qa"]] == SALARY_ANSWERS  # 3 問の質問と回答
    assert [item["question"] for item in sent["qa"]] == (await flow.begin())["texts"]["salary_questions"]
    state = await flow.ok("GET", "state")
    assert state["salary"] == {"proposed": True, "confirmed": False}

    # 読み取りが違っていれば、本人が直して確かめる(LLM は呼ばない)
    calls = len(llm.stub.requests)
    corrected = await flow.ok("POST", "salary/confirm", {"salary_basis": {**proposal["salary_basis"], "amount_kind": "net", "bonus_months": 0}})
    assert corrected["normalized_man_yen"] == pytest.approx(750 - 36)  # 手取り 600 万円 ÷ 0.8 − 固定残業代
    assert any("手取り" in line and "80%" in line for line in corrected["assumptions"])
    assert corrected["state"]["salary"]["confirmed"] is True and len(llm.stub.requests) == calls

    too_large = await flow.post("salary/confirm", {"salary_basis": {**proposal["salary_basis"], "amount_man_yen": 30, "fixed_overtime_man_yen_per_month": 5}})
    assert (too_large.status_code, too_large.json()["detail"]) == (422, "salary_basis_invalid")  # 結果が 0 以下
    bad_type = await flow.post("salary/confirm", {"salary_basis": {**proposal["salary_basis"], "amount_kind": "unknown"}})
    assert bad_type.status_code == 422


@pytest.mark.anyio
async def test_an_unusable_salary_reading_is_refused_and_the_wrong_number_of_answers_is_a_validation_error(flow, llm):
    await flow.begin()
    await flow.profile()
    llm.salary_basis = {**BASIS, "amount_man_yen": 30, "fixed_overtime_man_yen_per_month": 5}

    unusable = await flow.post("salary/answers", {"answers": SALARY_ANSWERS})
    assert (unusable.status_code, unusable.json()["detail"]) == (422, "salary_basis_invalid")
    for answers in (SALARY_ANSWERS[:2], [*SALARY_ANSWERS, "4 つ目"], ["", "b", "c"], "text"):
        assert (await flow.post("salary/answers", {"answers": answers})).status_code == 422


@pytest.mark.anyio
async def test_a_changed_salary_base_after_answering_starts_the_choices_over(flow, llm):
    await flow.until_choices(pairs=5)
    state = await flow.ok("GET", "state")
    assert state["choices"]["answered"] == 5

    await flow.ok("POST", "salary/answers", {"answers": SALARY_ANSWERS})  # 同じ年収(600)のまま確かめ直す
    same = await flow.ok("POST", "salary/confirm", {"salary_basis": BASIS})
    assert same["state"]["choices"]["answered"] == 5  # 土台のグリッド点が同じなら、答えは残る

    different = await flow.ok("POST", "salary/confirm", {"salary_basis": {**BASIS, "amount_man_yen": 900}})
    assert different["state"]["choices"]["answered"] == 0 and different["state"]["confirmed"] is False
    pairs = (await flow.ok("GET", "choices"))["pairs"]
    assert any("年収 900 万円" in pair["options"][name]["text"] for pair in pairs for name in ("a", "b"))  # 二択は新しい年収の周辺


# ---------------------------------------------------------------------------
# LLM の失敗の伝え方・本文の上限(DV-18 の面談の部分)
# ---------------------------------------------------------------------------


def raising(error):
    """呼ばれたら error を投げる、スタブの LLM の振る舞い。"""

    def behavior(request):
        raise error

    return behavior


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("behavior", "status", "detail"),
    [
        pytest.param(lambda request: "これは JSON ではありません", 502, "output_invalid", id="invalid output"),
        pytest.param(
            lambda request: llm_response('{"amount_man_yen"', finish_reason=types.FinishReason.MAX_TOKENS), 502, "output_truncated", id="truncated"
        ),
        pytest.param(raising(genai_errors.ClientError(400, {"error": {"message": "bad"}})), 502, "llm_failed", id="client error"),
        pytest.param(raising(genai_errors.ServerError(503, {"error": {"message": "down"}})), 503, "llm_unavailable", id="transient"),
    ],
)
async def test_failures_of_the_model_are_reported_with_their_reason_and_keep_the_state(env, flow, llm, behavior, status, detail):
    await flow.begin()
    await flow.profile()
    llm.stub.behavior = behavior

    response = await flow.post("salary/answers", {"answers": SALARY_ANSWERS})

    assert (response.status_code, response.json()["detail"]) == (status, detail)
    assert (await flow.ok("GET", "state"))["salary"] == {"proposed": False, "confirmed": False}  # 状態は残り、やり直せる
    llm.stub.behavior = lambda request: json.dumps(BASIS)
    assert (await flow.post("salary/answers", {"answers": SALARY_ANSWERS})).status_code == 200


@pytest.mark.anyio
async def test_when_the_daily_limit_is_reached_the_model_is_not_called_and_the_screen_can_say_so(env, flow, llm):
    await flow.begin()
    await flow.profile()
    limit = env.services.config.llm_budget.daily_limit
    snapshot = env.default_db.collection("llm_call_counters")
    snapshot.document(jst_date(env.clock.now())).set({"count": limit, "ttl_at": env.clock.now() + dt.timedelta(days=7)})

    response = await flow.post("salary/answers", {"answers": SALARY_ANSWERS})

    assert (response.status_code, response.json()["detail"]) == (429, "daily_limit_reached")
    assert llm.stub.requests == []  # 送っていない
    assert snapshot.document(jst_date(env.clock.now())).get().to_dict()["count"] == limit  # 断った分は進めない


@pytest.mark.anyio
async def test_when_the_counter_cannot_be_written_the_model_is_not_called(env, flow, llm, monkeypatch):
    await flow.begin()
    await flow.profile()

    async def unavailable(nid=None):
        raise LlmBudgetUnavailable("Aborted")

    monkeypatch.setattr(env.services.llm_budget, "reserve", unavailable)

    response = await flow.post("salary/answers", {"answers": SALARY_ANSWERS})

    assert (response.status_code, response.json()["detail"]) == (503, "temporarily_unavailable")
    assert llm.stub.requests == []


@pytest.mark.anyio
async def test_the_per_principal_window_gives_429_after_a_burst(env, flow, llm):
    await flow.begin()
    await flow.profile()
    service = env.services.interview
    service.agent._config = dataclasses.replace(service.agent._config, llm_calls_per_window=2)

    first = [await flow.post("salary/answers", {"answers": SALARY_ANSWERS}) for _ in range(2)]
    third = await flow.post("salary/answers", {"answers": SALARY_ANSWERS})

    assert [response.status_code for response in first] == [200, 200]
    assert (third.status_code, third.json()["detail"]) == (429, "rate_limited")
    assert len(llm.stub.requests) == 2


def _body_of_size(size: int, key: str = "text") -> bytes:
    padding = size - len(json.dumps({key: ""}))
    body = json.dumps({key: "a" * padding}).encode()
    assert len(body) == size
    return body


async def post_bytes(flow: Flow, suffix: str, body: bytes):
    return await flow.browser.client.post(f"{flow.base}/{suffix}", content=body, headers={**REQUESTED_WITH, "Content-Type": "application/json"})


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["comment", "reason"])
async def test_text_bodies_over_32_kb_are_refused_before_the_model_is_called(env, flow, llm, suffix):
    await flow.until_choices(pairs=0)
    limit = env.services.interview.max_body_bytes
    assert limit == 32768  # C-49
    calls = len(llm.stub.requests)

    at_limit = await post_bytes(flow, suffix, _body_of_size(limit))
    over = await post_bytes(flow, suffix, _body_of_size(limit + 1))

    assert at_limit.status_code == 200  # ちょうど上限は受け付ける
    assert (over.status_code, over.json()["detail"]) == (413, "payload_too_large")
    assert len(llm.stub.requests) == calls + 1  # 上限を超えた分は、LLM に送っていない
    assert (await post_bytes(flow, suffix, _body_of_size(limit * 3))).status_code == 413


@pytest.mark.anyio
async def test_a_body_without_content_length_is_counted_while_it_is_read(env, flow, llm):
    # チャンク送信(Content-Length がない)でも、受け取ったバイト数が上限を超えた時点で断る。
    await flow.until_choices(pairs=0)
    limit = env.services.interview.max_body_bytes
    calls = len(llm.stub.requests)

    async def chunks(total: int):
        yield b'{"text": "'
        for _ in range(total // 1024):
            yield b"a" * 1024
        yield b'"}'

    response = await flow.browser.client.post(
        f"{flow.base}/comment", content=chunks(limit * 2), headers={**REQUESTED_WITH, "Content-Type": "application/json"}
    )

    assert (response.status_code, response.json()["detail"]) == (413, "payload_too_large")
    assert len(llm.stub.requests) == calls


@pytest.mark.anyio
async def test_the_three_answers_of_the_salary_questions_share_the_same_32_kb_limit(env, flow, llm):
    await flow.begin()
    await flow.profile()
    limit = env.services.interview.max_body_bytes
    padding = limit - len(json.dumps({"answers": ["", "x", "y"]}))
    at_limit = json.dumps({"answers": ["a" * padding, "x", "y"]}).encode()
    assert len(at_limit) == limit

    assert (await post_bytes(flow, "salary/answers", at_limit)).status_code == 200
    over = await post_bytes(flow, "salary/answers", json.dumps({"answers": ["a" * (padding + 1), "x", "y"]}).encode())
    assert (over.status_code, over.json()["detail"]) == (413, "payload_too_large")
    assert len(llm.stub.requests) == 1


@pytest.mark.anyio
async def test_bad_bodies_are_rejected_without_echoing_the_input_or_calling_the_model(flow, llm):
    await flow.until_choices(pairs=0)
    calls = len(llm.stub.requests)  # 年収の読み取りまでの呼び出し
    canary = "CANARY-7F3A-ECHO"
    not_json = await post_bytes(flow, "comment", f"{{not json {canary}".encode())
    assert (not_json.status_code, not_json.json()["detail"]) == (400, "invalid_json")
    for body in ({"text": ""}, {"text": 5}, {"text": "ok", "extra": canary}, {"nothing": canary}, [canary]):
        response = await post_bytes(flow, "reason", json.dumps(body).encode())
        assert response.status_code == 422 and canary not in response.text
    profile = await flow.post("profile", {**PROFILE, "prefecture": canary})
    assert (profile.status_code, profile.json()["detail"]) == (422, "unknown_region") and canary not in profile.text
    job = await flow.post("profile", {**PROFILE, "job_category": canary})
    assert (job.status_code, job.json()["detail"]) == (422, "unknown_job_category") and canary not in job.text
    assert (await flow.post("profile", {**PROFILE, "experience_years": -1})).status_code == 422
    assert (await flow.post("axes", {"removed_axes": ["salary"]})).status_code == 422  # 外せるのは離散軸だけ
    assert (await flow.post("axes", {"removed_axes": ["night_duty", "night_duty"]})).status_code == 422
    assert (await flow.post("choices/answer", {"pair_id": "remote", "option": "c", "response": "go"})).status_code == 422
    assert (await flow.post("choices/answer", {"pair_id": "nonexistent", "option": "a", "response": "go"})).json()["detail"] == "unknown_pair"
    assert len(llm.stub.requests) == calls  # 拒否した入力は、どれも LLM に送っていない


# ---------------------------------------------------------------------------
# 企業の一覧とブロック先(§5 の 8)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_company_list_does_not_say_whether_a_company_has_jobs_and_unknown_companies_are_refused(flow):
    await flow.begin()

    listing = await flow.ok("GET", "companies")

    assert "現職の企業だけ" in listing["note"]  # 登録できるのは本人の現職企業だけと、画面の説明で定める(P-1)
    # ケース 2・3 のフィクスチャの企業も並ぶ(J)。ケース 1 の企業があり、求人の有無を示す項目がないことを確かめる
    assert {"company_id": "case1-company", "company_name": "株式会社サンプルシステムズ(架空)"} in listing["companies"]
    assert all(set(company) == {"company_id", "company_name"} for company in listing["companies"])
    unknown = await flow.post("blocklist", {"company_ids": ["no-such-company"]})
    assert (unknown.status_code, unknown.json()["detail"]) == (422, "unknown_company")
    state = await flow.ok("POST", "blocklist", {"company_ids": ["case1-company", "case1-company"]})
    assert state["blocklist"] == ["case1-company"]  # 重複はまとめる
    assert (await flow.ok("POST", "blocklist", {"company_ids": []}))["blocklist"] == []  # 空にもできる


# ---------------------------------------------------------------------------
# 面談の状態の寿命(メモリのみ)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_interview_state_is_removed_by_discard_restart_idle_expiry_and_submit_and_is_capped(env, flow, monkeypatch):
    service = env.services.interview
    await flow.begin()
    await flow.profile()
    assert len(service.store) == 1

    # begin をもう一度呼んでも、途中の状態は続く。restart で最初から
    assert (await flow.begin())["state"]["bands"] is not None
    assert (await flow.begin(restart=True))["state"]["bands"] is None

    # 放置された面談は、最後に使ってから寿命を過ぎるとメモリから消える
    await flow.profile()
    env.clock.advance(dt.timedelta(seconds=service.store._idle_ttl.total_seconds() - 1))
    assert (await flow.get("state")).status_code == 200  # 使ったので、寿命は延びる
    env.clock.advance(dt.timedelta(seconds=service.store._idle_ttl.total_seconds()))
    expired = await flow.get("state")
    assert (expired.status_code, expired.json()["detail"]) == (409, "interview_not_started") and len(service.store) == 0

    # 破棄
    await flow.begin()
    assert (await flow.ok("POST", "discard")) == {"status": "discarded"}
    assert len(service.store) == 0

    # 同時に持てる面談の数の上限
    monkeypatch.setattr(service.store, "_max_states", 1)
    await flow.begin()
    other = env.browser()
    other_flow = Flow(env, other, await other.open_start_page())
    full = await other_flow.post("begin", {})
    assert (full.status_code, full.json()["detail"]) == (503, "too_many_interviews")
    assert (await flow.begin(restart=True))["state"]["stage"] == "profile"  # 自分の面談のやり直しは、上限に関係なくできる


# ---------------------------------------------------------------------------
# 生の値が残らない(AC-02・AC-17・AC-18 の面談の部分)
# ---------------------------------------------------------------------------

RAW_NEEDLES = ("CANARY-7F3A", "623.45", "617.77", "411.11", "7.3141", "神奈川")


@pytest.mark.anyio
async def test_after_submitting_no_raw_value_or_original_text_is_left_in_firestore_the_logs_or_the_memory(env, flow, llm, caplog):
    # カナリア: プロフィールの正確な値(7.3141 年・神奈川県)、年収の回答(623.45 万円)、自由コメント(617.77)、辞めた理由(411.11)の原文と数値。
    caplog.set_level(logging.INFO)  # 本番と同じ水準(ADK と a2a-sdk は DEBUG で本文を出す。本番は INFO 以上。§10)
    caplog.set_level(logging.DEBUG, logger="web")
    llm.salary_basis = {**BASIS, "amount_man_yen": 623.45}
    llm.statements["free_comment"] = [statement("accept", salary=617.77)]
    llm.statements["reason_for_leaving"] = [statement("reject", salary=411.11)]
    await flow.begin()
    await flow.profile(experience_years=7.3141, prefecture="神奈川県")
    await flow.salary(["CANARY-7F3A-SALARY 年収は 623.45 万円です", "固定残業代なし", "賞与込み"])
    await flow.axes()
    await flow.answer_pairs(5)
    await flow.ok("POST", "comment", {"text": "CANARY-7F3A-COMMENT 年収 617.77 万円以上なら行く"})
    await flow.ok("POST", "reason", {"text": "CANARY-7F3A-REASON 年収 411.11 万円以下の仕事だったので辞めた"})
    await flow.ok("POST", "blocklist", {"company_ids": ["case1-company"]})

    # 送信の前: 面談の状態(メモリ)には、原文を持たない(取り出した発言だけ)。Firestore には何も書いていない
    state = env.services.interview.store.get(flow.pid)
    for secret in ("CANARY-7F3A", "神奈川", "7.3141"):
        assert secret not in repr(state), secret  # 原文・プロフィールの正確な値は、メモリにも持たない(辞めた理由は確認の前に捨てている)
    assert not documents_mentioning(env.default_db, *RAW_NEEDLES) and not vault_has_principal(env, flow.pid)

    await flow.ok("POST", "confirm", {})
    await flow.ok("POST", "worst-case/approve")
    await flow.ok("POST", "submit")

    # 送信のあと: 金庫の全文書にも (default) の全文書にも、ログにも、生の値は現れない
    assert vault_has_principal(env, flow.pid)  # 確かめているのは空の Firestore ではない
    assert documents_mentioning(env.store._db, flow.pid)  # 金庫には、この依頼者のデータがある
    assert documents_mentioning(env.default_db, flow.pid)  # (default) にも、利用記録がある
    assert documents_mentioning(env.store._db, *RAW_NEEDLES) == {}
    assert documents_mentioning(env.default_db, *RAW_NEEDLES) == {}
    assert caplog.records  # ログは出ている(空だから通るのではない)
    for secret in (*RAW_NEEDLES, flow.browser.cookie):
        assert secret not in caplog.text, secret
    assert env.services.interview.store.get(flow.pid) is None
    # 確かめが意味を持つこと: 原文は、LLM には届いている(届いても、このシステムの記録には残らない)
    sent = "".join(request.dump for request in llm.stub.requests)
    for secret in ("CANARY-7F3A-SALARY", "CANARY-7F3A-COMMENT", "CANARY-7F3A-REASON"):
        assert secret in sent
    for request in llm.stub.requests:  # LLM の入力に、依頼者の ID もクッキーも入らない
        assert flow.pid not in request.dump and flow.browser.cookie not in request.dump
    # 保存されたのは、丸め済みのポリシーだけ
    stored = env.store.get_policy(flow.pid)
    assert {a.salary for a in stored.policy.accept_anchors} >= {650}  # 623.45・617.77 → 650
    assert {a.salary for a in stored.policy.reject_anchors} >= {400}  # 411.11 → 400


@pytest.mark.anyio
async def test_the_original_reason_text_is_not_kept_even_before_the_person_confirms(env, flow, llm):
    # FR-06: 辞めた理由は制約に変換し、本人が確認した時点で破棄する。この実装では、変換した直後に原文を持たない(確認の前でも)。
    llm.statements["reason_for_leaving"] = [statement("reject", night_duty=6)]
    await flow.until_choices(pairs=0)

    result = await flow.ok("POST", "reason", {"text": "CANARY-7F3A-REASON 夜勤が多すぎた"})

    assert result["statements"][0]["sentence"] == "当直が月 6 回以上なら行かない"
    assert "CANARY" not in repr(env.services.interview.store.get(flow.pid))
    assert "CANARY" not in json.dumps(result, ensure_ascii=False) and "夜勤" not in json.dumps(result, ensure_ascii=False)
    assert documents_mentioning(env.default_db, "CANARY") == {} and documents_mentioning(env.store._db, "CANARY") == {}


@pytest.mark.anyio
async def test_the_canary_checks_can_actually_find_a_value_in_firestore_and_in_the_logs(env, caplog):
    # 上の確認が空振りでないことの対照: 同じ調べ方(Firestore の全文書の探索・本番と同じ水準のログの収集)で、置いたカナリアは見つかる。
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="web")
    control = "CANARY-7F3A-CONTROL"
    env.default_db.collection("control").document("planted").set({"note": control})
    env.store._db.collection("control").document("planted").set({"note": control})
    logging.getLogger("web.interview.service").info("control %s", control)

    assert set(documents_mentioning(env.default_db, control)) == {"control/planted"}
    assert set(documents_mentioning(env.store._db, control)) == {"control/planted"}
    assert control in caplog.text


@pytest.mark.anyio
async def test_the_interview_routes_are_part_of_the_web_app_and_the_direct_submit_route_remains(env):
    paths = env.app.openapi()["paths"]
    base = "/v1/principals/{pid}/interview"
    expected = {
        "": {"post"},  # 既存の直接の送信(丸める前のアンカーを受ける)。面談の API とは別に、そのまま残る
        "/begin": {"post"},
        "/state": {"get"},
        "/profile": {"post"},
        "/salary/answers": {"post"},
        "/salary/confirm": {"post"},
        "/axes": {"get", "post"},
        "/choices": {"get"},
        "/choices/answer": {"post"},
        "/comment": {"post"},
        "/reason": {"post"},
        "/confirmation": {"get"},
        "/anchors/{key}/active": {"post"},
        "/confirm": {"post"},
        "/worst-case": {"get"},
        "/worst-case/approve": {"post"},
        "/companies": {"get"},
        "/blocklist": {"post"},
        "/submit": {"post"},
        "/discard": {"post"},
    }
    for suffix, methods in expected.items():
        assert set(paths[base + suffix]) == methods, suffix


@pytest.mark.anyio
async def test_statements_are_capped_so_that_the_memory_and_the_submit_limit_cannot_be_exceeded(env, flow, llm):
    limit = env.services.config.limits.max_anchors_per_kind
    await flow.until_choices(pairs=0)
    llm.statements["free_comment"] = [statement("accept", salary=400 + 50 * i) for i in range(env.services.interview._config.max_statements_per_extraction)]
    accepted = 0
    while accepted * 10 < 2 * limit:
        response = await flow.post("comment", {"text": "もっと"})
        if response.status_code != 200:
            break
        accepted += 1
    assert accepted * 10 <= 2 * limit and response.status_code == 409 and response.json()["detail"] == "too_many_anchors"
    assert accepted >= 1


@pytest.mark.anyio
async def test_statements_that_are_not_saved_are_capped_too(env, flow, llm):
    limit = env.services.config.limits.max_anchors_per_kind
    await flow.until_choices(("night_duty",), pairs=0)
    per_call = env.services.interview._config.max_statements_per_extraction
    llm.statements["reason_for_leaving"] = [statement("reject", night_duty=4)] * per_call  # 外した軸に触れる: 保存されないが、メモリに積まれる
    accepted = 0
    while True:
        response = await flow.post("reason", {"text": "もっと"})
        if response.status_code != 200:
            break
        accepted += 1
        assert all(item["saved"] is False for item in response.json()["statements"])

    assert (response.status_code, response.json()["detail"]) == (409, "too_many_anchors")
    assert accepted * per_call == 2 * limit  # 保存しない発言も、無限には積まない


@pytest.mark.anyio
async def test_when_the_vault_cannot_be_written_the_interview_is_kept_so_that_the_person_can_try_again(env, flow, monkeypatch):
    await flow.until_ready()
    original = env.services.vault.put_policy
    calls = []

    async def failing_once(principal_id, request):
        calls.append(1)
        if len(calls) == 1:
            raise VaultUnavailableError("vault is unreachable")
        return await original(principal_id, request)

    monkeypatch.setattr(env.services.vault, "put_policy", failing_once)

    failed = await flow.post("submit")

    assert failed.status_code == 503
    assert env.services.interview.store.get(flow.pid) is not None  # 面談の状態は残る(やり直せる)
    assert (await flow.ok("GET", "state"))["stage"] == "ready"
    assert (await flow.ok("POST", "submit")) == {"status": "submitted"}
    assert env.services.interview.store.get(flow.pid) is None and vault_has_principal(env, flow.pid)
