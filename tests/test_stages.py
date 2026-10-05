"""AC-14・AC-06(段ごとに見えるもの): 段階開示 ④ の遷移・架空の求人の自動応答・模擬表示(design.md §6.2・§6.3、台帳 P-2・C-35・L9-4)。

- 段 1・段 2 は、双方の操作なしでは開かない。段 1 は、双方が「会う」を押した時点で開く(AC-14)。承認は、段 1 が開いてから。
- 段ごとに、相手(求人側)に見えるものが、§6.2 の表のとおり。それ以外は見せない(AC-06 のうち段階開示の部分)。見込みは、判定の後にだけ出る(FR-26)。
- 架空の求人は、フィクスチャの設定に従って、「会う」「承認」を自動で押す。実ユーザーの段 2 は模擬表示(連絡先を集めない)。デモ・攻撃の
  架空の候補者は、フィクスチャの職務要約・連絡先で、サーバが自動で押す。求人側を押す口は、HTTP にない(ほかの訪問者が求人側を操作しない)。
- 匿名職務要約の入力(前後の空白を除いて 1〜400 文字、本文 32 KB まで)。エラーの応答に入力を返さない。
- 画面で交渉を開いたとき、判定の後に作り損ねた段の状態を作る(台帳 L9-4。冪等な作成)。
- 取消・一時停止との関係: 判定の前は段階開示の操作を受け付けず、判定の後の取消・一時停止は段を変えない(§3.4)。
- GET は純粋な読み出し(台帳 X-84): 何度呼んでも、段の状態も台帳も変わらない。判定の検出(agreed_at)・架空人物の自動応答・台帳は、レフェリーの
  完了のフック(StageSettler.settle)と、見回りが行う(フックと見回りの試験は、tests/test_stages_settle.py)。
- 見込み「なし」で終わった交渉も、段 0 の開示を台帳に 1 行書く(台帳 L19-14)。段 1 以降には進めない。
- 段の状態の応答の settled: 決着処理(settled_at)を済ませたか。判定の直後で決着処理の前は false で、決着処理の前後で false → true に変わる(台帳 C-67)。
  GET は決着の印を立てない。画面は、判定の後 settled でない間だけ読み直す(static/stages.js。試験は tests/test_ui_static.py)。

金庫は本物の vault の app を ASGI のままつなぐ(web の app へは Browser から入る)。LLM・GCP には接続しない。ほかの段階開示の試験
(tests/test_stages_*.py)は、ここの部品(stage_env・make_case・agree・settle など)を import して使う。
"""

import dataclasses
import json

import pytest
from vault.api_models import MoveRequest
from vault.fixtures import AutoResponse, CaseFixture, load_case_fixture
from vault.templates import put_template
from vault_helpers import make_candidate_template, sample_package
from web.fictional_answerer import FixtureCatalog
from web.stages import EMPLOYER_SEES, OPENED_BY_STAGE
from web_app_helpers import CANARY, REQUESTED_WITH, build_web_env, documents_mentioning, dump_documents

EMPLOYER_TEMPLATE_ID = "stage-test-employer"
EMPLOYER_JOB_ID = "stage-test-job"
CANDIDATE_TEMPLATE_ID = "stage-test-candidate"
COMPANY_NAME = "株式会社ステージ試験(架空)"
JOB_SUMMARY = "Web サービスの運用と開発に約 8 年従事。5 名のチームのリーダーを務めた。"

# §6.2 を表にしたもの(段 n が開いているとき、求人側に見えるもの)。コードの表(web.stages.EMPLOYER_SEES)と食い違えば、この試験が落ちる。
EXPECTED_EMPLOYER_SEES = dict(
    [
        (0, ["likelihood", "package"]),
        (1, ["likelihood", "package", "job_summary"]),
        (2, ["likelihood", "package", "job_summary", "name", "email"]),
    ]
)
STAGE_FIELDS = ("likelihood", "package", "job_summary", "name", "email")


def make_case(*, meet: bool = True, approve: bool = True, confidential: bool = False) -> CaseFixture:
    """試験用のケース: ケース 1 の人物に、試験用の ID・企業名・自動応答の設定を付ける。"""
    base = load_case_fixture(1)
    public_job = base.employer.public_job.model_copy(update=dict(confidential=confidential))
    employer = dataclasses.replace(
        base.employer,
        template_id=EMPLOYER_TEMPLATE_ID,
        job_id=EMPLOYER_JOB_ID,
        company_name=COMPANY_NAME,
        public_job=public_job,
        auto_response=AutoResponse(meet=meet, approve=approve),
    )
    candidate = dataclasses.replace(base.candidate, template_id=CANDIDATE_TEMPLATE_ID, job_summary=JOB_SUMMARY)
    return CaseFixture(case=9, candidate=candidate, employer=employer)


@pytest.fixture
async def stage_env(store, clock, vault_client, default_db, session_key):
    """段階開示の試験用の web 一式を作る口。架空の求人の自動応答の設定(meet・approve)・非公開求人かを、試験ごとに選べる。

    金庫には、試験用のフィクスチャと同じ ID の求人・候補者のテンプレート(何でも受ける)を置く。終わったら、タスクを止める。
    """
    envs = []

    def build(**case_options):
        env = build_web_env(
            store=store,
            clock=clock,
            vault=vault_client,
            default_db=default_db,
            session_key=session_key,
            fixtures=FixtureCatalog([make_case(**case_options)]),
        )
        env.put_employer_template(template_id=EMPLOYER_TEMPLATE_ID, job_id=EMPLOYER_JOB_ID)
        put_template(store._db, make_candidate_template(template_id=CANDIDATE_TEMPLATE_ID))
        envs.append(env)
        return env

    yield build
    for env in envs:
        await env.aclose()


def agree(store, nid: str) -> None:
    """候補者が提案し、求人側が受けて、合意で終わらせる(両者とも何でも受けるので、見込みは「高」)。"""
    version = store.get_view(nid, "candidate").version  # 一時停止・再開でも version は進む
    proposed = store.process_move(
        nid, MoveRequest(expected_version=version, side="candidate", move="propose", package=sample_package())
    )
    store.process_move(nid, MoveRequest(expected_version=proposed.version, side="employer", move="accept"))


async def settle(env, nid: str, pid: str | None = None) -> bool:
    """判定の直後の決着処理(判定の検出・架空人物の自動応答・台帳)を、レフェリーを動かさずに行う。pid は本物の候補者の依頼者 ID(デモなら None)。

    本番では、レフェリーの完了のフック(RefereeDeps.on_finished)が、判定の直後に行う(台帳 X-84)。この試験の補助は、合意で終わらせる agree() が
    レフェリーを通らないので、フックの代わりに、フックと同じ部品(StageSettler.settle)を直接呼ぶ。GET は、段の状態を書かない。
    """
    return await env.services.stage_settler.settle(nid, pid)


async def live_negotiation(
    env, browser, *, agreed: bool = True, settled: bool = True, request_id: str = "request-0001"
) -> tuple[str, str]:
    """面談を送った本物の候補者が、架空の求人と交渉を始める。agreed なら合意で終わらせ、settled なら決着処理まで済ませる(完了のフックの代わり。settle)。

    (依頼者 ID, 交渉 ID) を返す。settled=False は、合意で終わったが、web がまだ決着処理をしていない状態(フックの前・フックの失敗)。
    """
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, EMPLOYER_TEMPLATE_ID, request_id)
    if agreed:
        agree(env.store, nid)
        if settled:
            await settle(env, nid, pid)
    return pid, nid


async def demo_negotiation(
    env, browser, *, request_id: str = "request-demo1", agreed: bool = True, settled: bool = True
) -> str:
    """フィクスチャのテンプレートで、デモの交渉を web の API で作る(段の状態にテンプレート ID を控える)。agreed なら合意で終わらせ、settled なら決着処理まで済ませる。"""
    response = await browser.post(
        "/v1/demo/negotiations",
        dict(
            request_id=request_id,
            candidate_template_id=CANDIDATE_TEMPLATE_ID,
            employer_template_id=EMPLOYER_TEMPLATE_ID,
        ),
    )
    assert response.status_code == 200, response.text
    nid = response.json()["nid"]
    if agreed:
        agree(env.store, nid)
        if settled:
            await settle(env, nid, None)
    return nid


def stage_doc(env, nid: str) -> dict:
    return env.default_db.collection("stages").document(nid).get().to_dict()


def ledger_docs(env, pid: str) -> dict[str, dict]:
    """依頼者の台帳の全行を、文書 ID → 内容で返す。"""
    ledger = env.default_db.collection("principals").document(pid).collection("ledger")
    return dict((snap.id, snap.to_dict()) for snap in ledger.stream())


async def meet(browser, nid: str, job_summary: str = JOB_SUMMARY):
    return await browser.post(f"/v1/negotiations/{nid}/stage/meet", dict(job_summary=job_summary))


async def approve(browser, nid: str):
    return await browser.post(f"/v1/negotiations/{nid}/stage/approve")


async def stage_of(browser, nid: str) -> dict:
    response = await browser.get(f"/v1/negotiations/{nid}/stage")
    assert response.status_code == 200, response.text
    return response.json()


def all_keys(value) -> set[str]:
    """JSON の値の中の、すべての辞書のキー。"""
    if isinstance(value, dict):
        return set(value) | set(key for item in value.values() for key in all_keys(item))
    if isinstance(value, list):
        return set(key for item in value for key in all_keys(item))
    return set()


# ----------------------------------------------------------------------
# 判定の前後(見込みは終了時に 1 回だけ。合意でなければ段 0 で終わり)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_before_the_judgment_no_likelihood_is_shown_and_no_stage_action_is_accepted(stage_env):
    # FR-26: 見込みは、交渉中には出さない(判定の後の 1 回だけ)。判定の前の「会う」「承認」は 409 で、何も書かない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)

    view = await stage_of(browser, nid)

    assert (view["judged"], view["agreed"], view["stage"], view["result"]) == (False, False, 0, None)
    assert view["disclosed_to_employer"]["visible"] == []
    assert all(view["disclosed_to_employer"][field] is None for field in STAGE_FIELDS)
    for response in (await meet(browser, nid, CANARY), await approve(browser, nid)):
        assert (response.status_code, response.json()) == (409, dict(detail="not_judged"))
    document = stage_doc(env, nid)
    assert document["meet"] == dict(candidate=False, employer=False) and "job_summary" not in document
    assert "agreed_at" not in document
    assert ledger_docs(env, pid) == {}
    assert CANARY not in json.dumps(dump_documents(env.default_db), default=str, ensure_ascii=False)  # 拒否した要約は、保存しない


@pytest.mark.anyio
async def test_a_negotiation_that_ended_without_agreement_stops_at_stage_zero_with_nothing_but_the_likelihood(stage_env):
    # FR-28: 不成立のときに相手へ返すのは「なし」だけ。段 1 以降には進めない(409 not_agreed。agreed_at は付かない)。台帳には、段 0 の開示
    # (「なし」を双方に出したこと)の 1 行だけを書く(台帳 L19-14。決着処理が書く。GET は書かない)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)
    cancelled = await browser.post(f"/v1/negotiations/{nid}/control", dict(action="cancel"))
    assert cancelled.json()["status"] == "judged"

    view = await stage_of(browser, nid)

    assert (view["judged"], view["agreed"], view["stage"]) == (True, False, 0)
    assert view["result"] == dict(likelihood="none", package=None)
    assert view["disclosed_to_employer"]["visible"] == ["likelihood"]
    assert view["disclosed_to_employer"]["likelihood"] == "none" and view["disclosed_to_employer"]["package"] is None
    assert ledger_docs(env, pid) == {}  # 見るだけ(GET)では、台帳に書かない
    assert await settle(env, nid, pid) is True  # 判定の直後の決着処理(完了のフックの代わり)
    for response in (await meet(browser, nid), await approve(browser, nid)):
        assert (response.status_code, response.json()) == (409, dict(detail="not_agreed"))
    assert "agreed_at" not in stage_doc(env, nid)
    assert [(row["action"], row["stage"], row["operator"], row["items"], row["to"]) for row in ledger_docs(env, pid).values()] == [
        ("disclose", 0, "system", ["result"], "both")
    ]


# ----------------------------------------------------------------------
# 決着の印(settled。台帳 C-67): 画面は、判定の後 settled でない間だけ、読み直す
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_view_says_settled_only_after_the_settlement_and_a_get_never_sets_it(stage_env):
    # C-67: GET は読み出しだけ(X-84)なので、判定の直後で、決着処理(完了のフック・見回り)の前は、judged で settled でなく、段 0 のまま(架空の求人の
    # 自動応答もまだ)。何度 GET しても決着の印は立たない。決着処理を済ませると settled になり、段の状態(自動応答の結果)も揃う。その後は変わらない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)  # 合意で終わったが、決着処理はまだ

    unsettled = [await stage_of(browser, nid) for _ in range(3)]

    for view in unsettled:
        assert (view["judged"], view["agreed"], view["settled"], view["stage"]) == (True, True, False, 0)
        assert view["meet"] == dict(candidate=False, employer=False)
    assert stage_doc(env, nid)["settled_at"] is None  # 読み出しは、決着の印を立てない
    assert ledger_docs(env, pid) == {}

    assert await settle(env, nid, pid) is True  # 完了のフックの代わり
    settled = await stage_of(browser, nid)

    assert (settled["judged"], settled["agreed"], settled["settled"], settled["stage"]) == (True, True, True, 0)
    assert settled["meet"] == dict(candidate=False, employer=True)  # 自動応答が済んでいる
    assert stage_doc(env, nid)["settled_at"] is not None
    assert (await stage_of(browser, nid)) == settled  # 以後は変わらない


@pytest.mark.anyio
async def test_the_view_of_a_negotiation_without_agreement_turns_settled_when_the_sweeper_settles_it(stage_env):
    # C-67: 見込み「なし」の交渉は、決着処理が段 0 の台帳の行と決着の印を同じトランザクションで書く(L19-14)。完了のフックが失敗しても、見回りが決着させるので、
    # 画面は、見回りが拾うまで(最大 60 秒)読み直せば、settled を見られる。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)
    assert (await browser.post(f"/v1/negotiations/{nid}/control", dict(action="cancel"))).json()["status"] == "judged"

    before = await stage_of(browser, nid)
    assert (before["judged"], before["agreed"], before["settled"], before["stage"]) == (True, False, False, 0)
    assert ledger_docs(env, pid) == {}

    report = await env.services.sweeper.sweep_once()  # 完了のフックを通らずに終わった交渉を、見回りが拾う
    after = await stage_of(browser, nid)

    assert report.stages_settled == 1
    assert (after["judged"], after["agreed"], after["settled"], after["stage"]) == (True, False, True, 0)
    assert [row["items"] for row in ledger_docs(env, pid).values()] == [["result"]]


@pytest.mark.anyio
async def test_the_view_of_a_negotiation_that_is_still_running_is_not_settled_and_stays_so(stage_env):
    # 判定の前は、決着するものがない: settled は false のまま。決着処理は何もしない(False を返す)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)

    assert await settle(env, nid, pid) is False
    view = await stage_of(browser, nid)

    assert (view["judged"], view["settled"]) == (False, False)
    assert stage_doc(env, nid)["settled_at"] is None


@pytest.mark.anyio
async def test_the_demo_view_turns_settled_across_the_settlement(stage_env):
    # デモ・攻撃(候補者が架空人物)の口も同じ: 決着処理の前は settled が false(段 0 のまま)、後は true(自動応答が段 2 まで進める)。
    env = stage_env()
    visitor = env.browser()
    nid = await demo_negotiation(env, visitor, settled=False)

    before = (await visitor.get(f"/v1/demo/negotiations/{nid}/stage")).json()
    assert (before["judged"], before["settled"], before["stage"]) == (True, False, 0)

    assert await settle(env, nid) is True
    after = (await visitor.get(f"/v1/demo/negotiations/{nid}/stage")).json()

    assert (after["judged"], after["settled"], after["stage"]) == (True, True, 2)


# ----------------------------------------------------------------------
# AC-06(段階開示の部分): 段ごとに、相手に見えるものが表のとおり
# ----------------------------------------------------------------------


def test_the_visibility_table_in_the_code_is_the_table_in_the_design():
    # §6.2 の記述(段 0 は見込みと組み合わせ、段 1 は匿名職務要約、段 2 は氏名と連絡先。累積)を表にしたものと、コードの表が同じ。
    assert dict((stage, list(items)) for stage, items in EMPLOYER_SEES.items()) == EXPECTED_EMPLOYER_SEES
    assert dict((stage, list(items)) for stage, items in OPENED_BY_STAGE.items()) == dict(
        [(0, ["likelihood", "package"]), (1, ["job_summary"]), (2, ["name", "email"])]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("stage", [0, 1, 2])
async def test_what_the_employer_sees_at_each_stage_is_exactly_the_table_and_nothing_else(stage_env, stage):
    # AC-06(段階開示の部分): 段 n では、表の項目だけが見え(visible)、表にない項目は null。段 2 の氏名・連絡先は、実ユーザーでは模擬表示で値がない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    if stage >= 1:
        assert (await meet(browser, nid)).status_code == 200
    if stage >= 2:
        assert (await approve(browser, nid)).status_code == 200

    view = await stage_of(browser, nid)

    disclosed = view["disclosed_to_employer"]
    assert (view["stage"], disclosed["visible"]) == (stage, EXPECTED_EMPLOYER_SEES[stage])
    for field in STAGE_FIELDS:
        shown = disclosed[field] is not None
        if field in ("name", "email"):
            assert shown is False  # 実ユーザーの段 2 は模擬表示(値は集めていない)
        else:
            assert shown == (field in EXPECTED_EMPLOYER_SEES[stage]), field
    assert disclosed["simulated"] is (stage == 2)
    if stage >= 1:
        assert disclosed["job_summary"] == JOB_SUMMARY
    expected_result = dict(likelihood="high", package=sample_package().model_dump())
    assert view["result"] == expected_result
    assert (disclosed["likelihood"], disclosed["package"]) == ("high", sample_package().model_dump())


@pytest.mark.anyio
async def test_the_stage_view_shows_the_final_record_and_the_flags_but_never_the_vaults_internals(stage_env):
    # DV-10(画面の部分): 段の状態の応答に、version・期限・相手の残り回数・理由・依頼者 ID・TTL は現れない。最終記録の中身は {likelihood, package} だけ(AC-08)。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    view = await stage_of(browser, nid)

    assert set(view) == set(
        ["nid", "judged", "settled", "agreed", "stage", "result", "meet", "approve", "employer_fictional", "employer_auto_response", "company", "disclosed_to_employer"]
    )
    assert set(view["result"]) == set(["likelihood", "package"])
    assert set(view["disclosed_to_employer"]) == set(["visible", *STAGE_FIELDS, "simulated"])
    forbidden = set(["version", "expires_at", "deadline", "budget", "remaining_moves", "remaining_evaluations", "reason", "principal_id", "candidate_principal_id", "ttl_at", "llm_calls", "end_reason"])
    assert all_keys(view).isdisjoint(forbidden)
    assert pid not in json.dumps(view)
    assert view["employer_fictional"] is True and view["employer_auto_response"] is True
    # 架空の求人(自動応答)は、判定の直後の決着処理(live_negotiation が、完了のフックの代わりに行う)で、「会う」を押してある。候補者はまだ。
    assert view["meet"] == dict(candidate=False, employer=True)
    assert view["approve"] == dict(candidate=False, employer=False)


# ----------------------------------------------------------------------
# AC-14: 段 1・段 2 は、双方の操作なしでは開かない。段 1 は、双方が「会う」を押した時点で開く
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("candidate_first", [True, False], ids=["candidate_first", "employer_first"])
async def test_stage_one_opens_only_when_both_sides_have_pressed_meet(stage_env, candidate_first):
    # AC-14: 架空の求人の自動応答を切り(meet・approve とも False)、求人側の操作をテストが行う(将来の本物の求人の代わり)。
    # どちらの順でも、片側だけでは開かず、後から押した側のトランザクションで開く(開いた段は、後から押した側の press の結果に出る)。
    env = stage_env(meet=False, approve=False)
    stages = env.services.stages
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    assert (await stage_of(browser, nid))["stage"] == 0

    if candidate_first:
        response = await meet(browser, nid)
        assert response.status_code == 200
        view = response.json()
        assert (view["stage"], view["meet"]) == (0, dict(candidate=True, employer=False))
        assert view["disclosed_to_employer"]["job_summary"] is None  # 保存はしても、段 0 の間は出さない
        outcome = await stages.press(nid, "employer", "meet", operator="fictional_employer")
        assert outcome.advanced_to == 1
    else:
        outcome = await stages.press(nid, "employer", "meet", operator="fictional_employer")
        assert (outcome.advanced_to, outcome.state.stage) == (None, 0)
        assert (await stage_of(browser, nid))["stage"] == 0
        response = await meet(browser, nid)
        assert (response.status_code, response.json()["stage"]) == (200, 1)

    opened = await stage_of(browser, nid)
    assert (opened["stage"], opened["disclosed_to_employer"]["job_summary"]) == (1, JOB_SUMMARY)
    assert opened["disclosed_to_employer"]["visible"] == EXPECTED_EMPLOYER_SEES[1]


@pytest.mark.anyio
@pytest.mark.parametrize("candidate_first", [True, False], ids=["candidate_first", "employer_first"])
async def test_stage_two_opens_only_when_both_sides_have_approved(stage_env, candidate_first):
    # AC-14: 承認も、どちらの順でも、片側だけでは段 2 は開かない(氏名と連絡先は出ない)。双方がそろったトランザクションで開く。
    env = stage_env(meet=True, approve=False)
    stages = env.services.stages
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    assert (await meet(browser, nid)).json()["stage"] == 1  # 求人側は会うだけ自動。双方が会ったので段 1

    if candidate_first:
        view = (await approve(browser, nid)).json()
        assert (view["stage"], view["approve"]) == (1, dict(candidate=True, employer=False))
        assert view["disclosed_to_employer"]["visible"] == EXPECTED_EMPLOYER_SEES[1]
        outcome = await stages.press(nid, "employer", "approve", operator="fictional_employer")
        assert outcome.advanced_to == 2
    else:
        outcome = await stages.press(nid, "employer", "approve", operator="fictional_employer")
        assert (outcome.advanced_to, outcome.state.stage) == (None, 1)
        assert (await stage_of(browser, nid))["stage"] == 1
        view = (await approve(browser, nid)).json()
        assert view["stage"] == 2

    opened = await stage_of(browser, nid)
    assert opened["stage"] == 2
    assert (opened["disclosed_to_employer"]["visible"], opened["disclosed_to_employer"]["simulated"]) == (
        EXPECTED_EMPLOYER_SEES[2],
        True,
    )


@pytest.mark.anyio
async def test_stage_two_does_not_open_when_only_one_side_has_approved(stage_env):
    # AC-14: 承認が片側だけなら、段 2 は開かず、氏名と連絡先は出ない(求人側が承認しない設定。approve=False)。
    env = stage_env(meet=True, approve=False)
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    assert (await meet(browser, nid)).json()["stage"] == 1  # 双方が会う → 段 1

    response = await approve(browser, nid)

    assert response.status_code == 200
    view = response.json()
    assert (view["stage"], view["approve"]) == (1, dict(candidate=True, employer=False))
    assert view["disclosed_to_employer"]["visible"] == EXPECTED_EMPLOYER_SEES[1]
    assert view["disclosed_to_employer"]["simulated"] is False and view["disclosed_to_employer"]["name"] is None


@pytest.mark.anyio
async def test_stage_one_does_not_open_when_the_employer_never_meets(stage_env):
    # AC-14: 求人側が「会う」を押さない設定(meet=False)なら、候補者が押しても段 0 のまま。職務要約は(保存はしても)出ない。
    env = stage_env(meet=False, approve=True)
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    view = (await meet(browser, nid)).json()

    assert (view["stage"], view["meet"]) == (0, dict(candidate=True, employer=False))
    assert view["disclosed_to_employer"]["job_summary"] is None
    assert view["disclosed_to_employer"]["visible"] == EXPECTED_EMPLOYER_SEES[0]
    assert (await approve(browser, nid)).json() == dict(detail="stage_not_open")


@pytest.mark.anyio
async def test_the_approval_cannot_be_pressed_before_stage_one_is_open(stage_env):
    # 承認は、段 1 が開いてから。段 0 の承認は 409 stage_not_open で、フラグも立たず、台帳にも書かない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)  # 段 0 は、決着処理で整っている(求人側の「会う」を自動で押してある)
    before = ledger_docs(env, pid)

    response = await approve(browser, nid)

    assert (response.status_code, response.json()) == (409, dict(detail="stage_not_open"))
    assert stage_doc(env, nid)["approve"] == dict(candidate=False, employer=False)
    assert ledger_docs(env, pid) == before


@pytest.mark.anyio
async def test_the_store_itself_refuses_a_press_that_is_not_allowed_yet_and_writes_nothing(stage_env):
    # 段の状態の層(StageStore.press)も、API の手前の確認に頼らず、自分で断る: 判定を記録する前は not_agreed、承認は段 1 の前なら
    # stage_not_open、段の状態がなければ absent。断ったときは、フラグも要約も台帳も書かない。
    env = stage_env(meet=False, approve=False)
    stages = env.services.stages
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)  # 合意で終わったが、web はまだ判定を記録していない
    before = (stage_doc(env, nid), ledger_docs(env, pid))

    unrecorded = await stages.press(nid, "candidate", "meet", operator="principal", job_summary=CANARY)
    assert (unrecorded.refused, unrecorded.changed) == ("not_agreed", False)
    assert (stage_doc(env, nid), ledger_docs(env, pid)) == before

    assert await stages.record_agreement(nid) == "recorded"
    early = await stages.press(nid, "employer", "approve", operator="fictional_employer")
    assert (early.refused, early.changed) == ("stage_not_open", False)
    missing = await stages.press("0123456789abcdef", "candidate", "meet", operator="principal")
    assert (missing.refused, missing.state) == ("absent", None)
    assert stage_doc(env, nid)["approve"] == dict(candidate=False, employer=False)
    assert CANARY not in json.dumps(dump_documents(env.default_db), default=str, ensure_ascii=False)


# ----------------------------------------------------------------------
# 架空の求人の自動応答(P-2)と、実ユーザーの段 2 の模擬表示
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_fictional_employer_presses_meet_and_approve_by_itself_after_the_candidate(stage_env):
    # P-2: 架空の求人は、フィクスチャの設定に従って、自動で「会う」「承認」を押す。本物の候補者が押すと、その場で段が進む。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    first = await stage_of(browser, nid)
    after_meet = (await meet(browser, nid)).json()
    after_approve = (await approve(browser, nid)).json()

    assert (first["stage"], first["meet"]["employer"], first["approve"]["employer"]) == (0, True, False)
    assert (after_meet["stage"], after_meet["approve"]["employer"]) == (1, True)  # 段 1 が開いたので、求人側は承認も押した
    assert (after_approve["stage"], after_approve["approve"]) == (2, dict(candidate=True, employer=True))


@pytest.mark.anyio
async def test_stage_two_for_a_real_user_is_a_simulated_display_that_collects_no_contact(stage_env):
    # P-2・§6.2: 実ユーザーの段 2 は、連絡先を集めない。「ここで連絡先が開示されます」と見せるだけの模擬表示(simulated)。
    # フィクスチャの架空の連絡先も、(default) のどこにも書かない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    await meet(browser, nid)

    view = (await approve(browser, nid)).json()

    disclosed = view["disclosed_to_employer"]
    assert view["stage"] == 2 and disclosed["simulated"] is True
    assert "name" in disclosed["visible"] and "email" in disclosed["visible"]
    assert (disclosed["name"], disclosed["email"]) == (None, None)
    contact = load_case_fixture(1).candidate.contact
    assert documents_mentioning(env.default_db, contact.email, contact.name) == {}
    assert documents_mentioning(env.store._db, contact.email, contact.name) == {}


@pytest.mark.anyio
async def test_a_confidential_jobs_company_name_is_shown_to_the_candidate_from_stage_one(stage_env):
    # FR-32: 非公開求人は、段 1 で企業名を開示する(それまでは null)。公開の求人は、最初から出す(判定の前でも)。
    env = stage_env(confidential=True)
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    stage_zero = await stage_of(browser, nid)
    stage_one = (await meet(browser, nid)).json()

    assert stage_zero["company"] == dict(confidential=True, name=None)
    assert stage_one["stage"] == 1 and stage_one["company"] == dict(confidential=True, name=COMPANY_NAME)

    public_env = stage_env(confidential=False)
    public_browser = public_env.browser()
    _, public_nid = await live_negotiation(public_env, public_browser, agreed=False)
    assert (await stage_of(public_browser, public_nid))["company"] == dict(confidential=False, name=COMPANY_NAME)


@pytest.mark.anyio
async def test_an_unknown_job_has_no_company_and_no_automatic_response(stage_env):
    # フィクスチャにない求人(金庫のテンプレートだけある)は、企業名も自動応答もない: 求人側は押さないので、段 0 のまま。
    env = stage_env()
    browser = env.browser()
    other_template = env.put_employer_template()
    pid = await browser.register()
    nid = await browser.create_negotiation(pid, other_template)
    agree(env.store, nid)

    view = await stage_of(browser, nid)
    after_meet = (await meet(browser, nid)).json()

    assert (view["company"], view["employer_auto_response"], view["meet"]["employer"]) == (None, False, False)
    assert after_meet["stage"] == 0


# ----------------------------------------------------------------------
# 冪等・入力の検証
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_meet_and_approve_are_idempotent_and_the_first_summary_is_kept(stage_env):
    # 「会う」「承認」は、側ごとのフラグとして冪等に立てる。2 回目以降(再送)は何も変えず、200 で同じ状態を返す。要約は最初のものを残す。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    first = await meet(browser, nid, JOB_SUMMARY)
    again = await meet(browser, nid, "別の要約に書き換えようとした")
    approved = await approve(browser, nid)
    approved_again = await approve(browser, nid)

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert approved_again.json() == approved.json() and approved.json()["stage"] == 2
    assert stage_doc(env, nid)["job_summary"] == JOB_SUMMARY
    assert "別の要約" not in json.dumps(dump_documents(env.default_db), default=str, ensure_ascii=False)
    rows = ledger_docs(env, pid)
    assert [row["action"] for row in rows.values() if row["operator"] == "principal"].count("meet") == 1
    assert [row["action"] for row in rows.values() if row["operator"] == "principal"].count("approve") == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param(dict(), "invalid_body", id="missing"),
        pytest.param(dict(job_summary=""), "job_summary_invalid", id="empty"),
        pytest.param(dict(job_summary="  \n\t "), "job_summary_invalid", id="blank"),
        pytest.param(dict(job_summary=CANARY + "あ" * 401), "job_summary_invalid", id="too_long"),
        pytest.param(dict(job_summary=123), "invalid_body", id="not_a_string"),
        pytest.param(dict(job_summary=None), "invalid_body", id="null"),
        pytest.param(dict(job_summary=CANARY, side="employer"), "invalid_body", id="extra_field"),
    ],
)
async def test_an_invalid_summary_is_rejected_without_echoing_it_or_writing_anything(stage_env, body, detail):
    # C-35: 匿名職務要約は、前後の空白を除いて 1〜400 文字の文字列。違えば 422 で、エラーの応答に入力の値(カナリア)を返さない。
    # フラグも要約も台帳も、何も書かない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    await stage_of(browser, nid)
    before = (stage_doc(env, nid), ledger_docs(env, pid))

    response = await browser.post(f"/v1/negotiations/{nid}/stage/meet", body)

    assert (response.status_code, response.json()) == (422, dict(detail=detail))
    assert CANARY not in response.text
    assert (stage_doc(env, nid), ledger_docs(env, pid)) == before


@pytest.mark.anyio
async def test_a_body_that_is_not_json_is_rejected(stage_env):
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)

    response = await browser.client.post(
        f"/v1/negotiations/{nid}/stage/meet", content=b"job_summary=" + CANARY.encode(), headers=REQUESTED_WITH
    )

    assert (response.status_code, response.json()) == (422, dict(detail="invalid_body"))
    assert CANARY not in response.text


@pytest.mark.anyio
async def test_the_summary_is_stored_without_surrounding_whitespace_and_may_be_exactly_400_characters(stage_env):
    # 上限ちょうど(前後の空白を除いて 400 文字)は受け付け、空白を除いて保存する。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    summary = "あ" * 400

    response = await meet(browser, nid, f"  \n{summary}\n ")

    assert response.status_code == 200
    assert stage_doc(env, nid)["job_summary"] == summary
    assert response.json()["disclosed_to_employer"]["job_summary"] == summary


@pytest.mark.anyio
async def test_a_request_body_over_32_kb_is_refused_before_it_is_read_to_the_end(stage_env):
    # C-35・§8.2: 本文は 32 KB まで。超えたら 413 で、何も書かない。宣言された長さ(Content-Length)が超えているときも、
    # 長さを宣言しない送り方(分割転送)で超えるときも、同じ。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    await stage_of(browser, nid)
    before = (stage_doc(env, nid), ledger_docs(env, pid))
    big = json.dumps(dict(job_summary=CANARY, padding="x" * 33_000)).encode()

    declared = await browser.client.post(f"/v1/negotiations/{nid}/stage/meet", content=big, headers=REQUESTED_WITH)

    async def chunks():
        for start in range(0, len(big), 4096):
            yield big[start : start + 4096]

    streamed = await browser.client.post(f"/v1/negotiations/{nid}/stage/meet", content=chunks(), headers=REQUESTED_WITH)

    for response in (declared, streamed):
        assert (response.status_code, response.json()) == (413, dict(detail="request_too_large"))
        assert CANARY not in response.text
    assert (stage_doc(env, nid), ledger_docs(env, pid)) == before


# ----------------------------------------------------------------------
# 取消・一時停止との関係(§3.4)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_stage_actions_wait_for_the_judgment_and_a_later_cancel_or_pause_does_not_move_the_stage(stage_env):
    # §3.4: 一時停止中(判定の前)は段階開示の操作を受け付けない。判定の後は、取消・一時停止は何もしない(終わった交渉)ので、段は変わらない。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, agreed=False)
    paused = await browser.post(f"/v1/negotiations/{nid}/control", dict(action="pause"))
    assert paused.json() == dict(status="active", paused=True)
    assert (await meet(browser, nid)).json() == dict(detail="not_judged")
    await browser.post(f"/v1/negotiations/{nid}/control", dict(action="resume"))
    agree(env.store, nid)
    assert (await meet(browser, nid)).json()["stage"] == 1

    cancelled = await browser.post(f"/v1/negotiations/{nid}/control", dict(action="cancel"))
    paused_after = await browser.post(f"/v1/negotiations/{nid}/control", dict(action="pause"))

    assert cancelled.json() == dict(status="judged", paused=False)
    assert paused_after.json() == dict(status="judged", paused=False)
    view = await stage_of(browser, nid)
    assert (view["stage"], view["agreed"], view["result"]["likelihood"]) == (1, True, "high")  # 結果は一度決まったら変わらない
    assert (await approve(browser, nid)).json()["stage"] == 2


# ----------------------------------------------------------------------
# 画面で交渉を開いたとき、作り損ねた段の状態を作る(台帳 L9-4。DV-08)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_opening_a_judged_negotiation_whose_stage_was_never_created_creates_it(stage_env):
    # L9-4・DV-08: 判定の後に web が落ちて、段の状態を作り損ねた交渉(見回りは判定前の交渉しか見ない)も、画面で開いたとき(活動ログ・段の状態)に
    # 作る(冪等な作成)。本物の候補者の依頼者 ID つき・TTL なし・段 0 で、段 0 が表示される。作るだけで、判定の検出(agreed_at)と台帳は書かない
    # (台帳 X-84)。それらは、決着処理(見回りが拾う)が、段 0 の台帳の 1 行も含めて、1 回だけ書く。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser, settled=False)
    stages = env.default_db.collection("stages")

    stages.document(nid).delete()  # 作り損ねた状態
    events = await browser.get(f"/v1/negotiations/{nid}/events")
    assert events.status_code == 200
    recreated = stage_doc(env, nid)
    assert (recreated["candidate_principal_id"], recreated["stage"]) == (pid, 0)
    assert "ttl_at" not in recreated and "agreed_at" not in recreated and recreated["settled_at"] is None

    stages.document(nid).delete()  # もう一度。今度は段の状態の表示で
    view = await stage_of(browser, nid)
    assert (view["stage"], view["agreed"], view["result"]["likelihood"]) == (0, True, "high")  # 段 0 が表示される
    for _ in range(2):
        await stage_of(browser, nid)  # 何度見ても、書かない
    document = stage_doc(env, nid)
    assert document["candidate_principal_id"] == pid and "agreed_at" not in document and document["settled_at"] is None
    assert ledger_docs(env, pid) == {}

    report = await env.services.sweeper.sweep_once()  # 見回りが、判定済みで決着がまだの段を拾う
    await env.services.sweeper.sweep_once()
    assert report.stages_settled == 1
    assert "agreed_at" in stage_doc(env, nid) and stage_doc(env, nid)["settled_at"] is not None
    assert [row["stage"] for row in ledger_docs(env, pid).values() if row["action"] == "disclose"] == [0]


@pytest.mark.anyio
async def test_the_state_survives_a_restart_of_web(stage_env, store, clock, vault_client, default_db, session_key):
    # 段の状態は Firestore にある。web を起動し直しても(別の app が同じデータを読んでも)、同じ段から続ける。
    env = stage_env()
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    await meet(browser, nid)
    cookie = browser.cookie

    restarted = build_web_env(
        store=store,
        clock=clock,
        vault=vault_client,
        default_db=default_db,
        session_key=session_key,
        fixtures=FixtureCatalog([make_case()]),
    )
    try:
        again = restarted.browser()
        again.set_cookie(cookie)
        view = await stage_of(again, nid)
        assert (view["stage"], view["meet"], view["disclosed_to_employer"]["job_summary"]) == (
            1,
            dict(candidate=True, employer=True),
            JOB_SUMMARY,
        )
        assert (await approve(again, nid)).json()["stage"] == 2
    finally:
        await restarted.aclose()


# ----------------------------------------------------------------------
# デモ・攻撃の架空の候補者(サーバが自動で押す。本物の依頼者には触れない)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_demo_negotiation_plays_out_all_stages_with_the_fixtures_summary_and_contact(stage_env):
    # P-2・§6.2: デモの架空の候補者は、フィクスチャの職務要約・連絡先で、サーバが自動で「会う」「承認」を押す(架空の求人も自動)。
    # 段 2 は、実ユーザーではないので、フィクスチャの(架空の)連絡先をそのまま出す。段の状態の TTL(96 時間)は残り、台帳は作らない。
    # 自動で押すのは、判定の直後の決着処理(完了のフック・見回り)で、GET ではない(台帳 X-84): 決着処理の前は、GET は段 0 を見せるだけで何も書かない。
    env = stage_env()
    visitor = env.browser()
    nid = await demo_negotiation(env, visitor, settled=False)
    before = stage_doc(env, nid)
    unsettled = (await visitor.get(f"/v1/demo/negotiations/{nid}/stage")).json()  # クッキーのない訪問者
    assert (unsettled["agreed"], unsettled["stage"], unsettled["meet"]) == (True, 0, dict(candidate=False, employer=False))
    assert stage_doc(env, nid) == before  # 見ただけでは、何も書いていない

    assert await settle(env, nid) is True  # 判定の直後の決着処理(完了のフックの代わり)
    response = await visitor.get(f"/v1/demo/negotiations/{nid}/stage")

    assert response.status_code == 200, response.text
    view = response.json()
    contact = load_case_fixture(1).candidate.contact
    assert (view["stage"], view["meet"], view["approve"]) == (2, dict(candidate=True, employer=True), dict(candidate=True, employer=True))
    disclosed = view["disclosed_to_employer"]
    assert (disclosed["visible"], disclosed["simulated"]) == (EXPECTED_EMPLOYER_SEES[2], False)
    assert (disclosed["job_summary"], disclosed["name"], disclosed["email"]) == (JOB_SUMMARY, contact.name, contact.email)
    document = stage_doc(env, nid)
    assert document["candidate_principal_id"] is None
    assert document["ttl_at"] == env.clock.now() + env.services.stages._fictional_ttl  # 自動で押しても、TTL は変わらない
    assert (document["employer_template_id"], document["candidate_template_id"]) == (EMPLOYER_TEMPLATE_ID, CANDIDATE_TEMPLATE_ID)
    assert [path for path in dump_documents(env.default_db) if "/ledger/" in path] == []  # 台帳の持ち主(依頼者)がいない


@pytest.mark.anyio
async def test_a_demo_negotiation_without_fixtures_stays_at_stage_zero(stage_env):
    # 作成のときにテンプレート ID を控えていない(攻撃モードなど)・フィクスチャにないデモは、自動応答がなく、段 0 のまま。
    env = stage_env()
    visitor = env.browser()
    nid = await demo_negotiation(env, visitor, agreed=False)
    env.default_db.collection("stages").document(nid).delete()  # 作り損ねた(見回りが、テンプレート ID なしで作り直した)状態
    agree(env.store, nid)

    view = (await visitor.get(f"/v1/demo/negotiations/{nid}/stage")).json()

    assert (view["agreed"], view["stage"], view["employer_auto_response"]) == (True, 0, False)
    assert view["result"]["likelihood"] == "high"
    assert stage_doc(env, nid)["candidate_principal_id"] is None and "ttl_at" in stage_doc(env, nid)


@pytest.mark.anyio
async def test_the_demo_stage_route_refuses_a_real_negotiation_and_changes_nothing(stage_env):
    # DV-01・§6.3: デモ用の口は、本物の依頼者には触れない。本物の交渉・存在しない交渉・形の違う ID は、どれも 403 で、段の状態にも台帳にも書かない。
    env = stage_env()
    owner = env.browser()
    pid, nid = await live_negotiation(env, owner)
    before = (stage_doc(env, nid) if env.default_db.collection("stages").document(nid).get().exists else None, ledger_docs(env, pid))
    visitor = env.browser()

    for target in (nid, "0123456789abcdef", "not-a-negotiation-id"):
        response = await visitor.get(f"/v1/demo/negotiations/{target}/stage")
        assert (response.status_code, response.json()) == (403, dict(detail="forbidden")), target

    after = (stage_doc(env, nid) if env.default_db.collection("stages").document(nid).get().exists else None, ledger_docs(env, pid))
    assert after == before
    assert env.default_db.collection("stages").document("0123456789abcdef").get().exists is False


@pytest.mark.anyio
async def test_a_stage_document_that_says_real_makes_the_demo_route_refuse_even_for_a_demo_negotiation(stage_env):
    # 金庫が架空の候補者の交渉と認めても、web の段の状態(補助)が本物の候補者のものなら、拒否する(2 段の確認。どちらかが断れば 403)。
    env = stage_env()
    visitor = env.browser()
    nid = await demo_negotiation(env, visitor, settled=False)
    env.default_db.collection("stages").document(nid).update(dict(candidate_principal_id="0123456789abcdef"))

    response = await visitor.get(f"/v1/demo/negotiations/{nid}/stage")

    assert (response.status_code, response.json()) == (403, dict(detail="forbidden"))
    assert "agreed_at" not in stage_doc(env, nid)


# ----------------------------------------------------------------------
# 求人側を押す口は、HTTP にない
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_there_is_no_http_route_that_presses_for_the_employer(stage_env):
    # §6.2: ほかの訪問者が求人側を操作する経路は作らない。段階開示の HTTP の口は、候補者側の操作と読み出しだけ。
    # 本文に側を指定しても(extra は拒否)、求人側の URL を作っても、求人側は押せない。
    env = stage_env(meet=False, approve=False)
    browser = env.browser()
    pid, nid = await live_negotiation(env, browser)
    routes = set(  # FastAPI は include したルートを、app.routes には展開しない。OpenAPI の定義から、公開しているルートを引く
        (method.upper(), route_path)
        for route_path, operations in env.app.openapi()["paths"].items()
        for method in operations
        if "/stage" in route_path or "/ledger" in route_path
    )
    assert routes == set(
        [
            ("GET", "/v1/negotiations/{nid}/stage"),
            ("POST", "/v1/negotiations/{nid}/stage/meet"),
            ("POST", "/v1/negotiations/{nid}/stage/approve"),
            ("GET", "/v1/principals/{pid}/ledger"),
            ("GET", "/v1/demo/negotiations/{nid}/stage"),
        ]
    )

    forged = await browser.post(f"/v1/negotiations/{nid}/stage/meet", dict(job_summary=JOB_SUMMARY, side="employer"))
    elsewhere = [
        await browser.post(f"/v1/negotiations/{nid}/stage/employer/meet", dict(job_summary=JOB_SUMMARY)),
        await browser.post(f"/v1/demo/negotiations/{nid}/stage/meet", dict(job_summary=JOB_SUMMARY)),
        await browser.post(f"/v1/demo/negotiations/{nid}/stage", dict()),
    ]

    assert forged.status_code == 422
    assert [response.status_code for response in elsewhere] == [404, 404, 405]
    assert stage_doc(env, nid)["meet"] == dict(candidate=False, employer=False)
