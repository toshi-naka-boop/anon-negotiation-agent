"""架空人物の自動応答の配線(design.md §4.4・§6.2。ギャップ調査 2.6・2.20、AC-07・DV-11 のうち配線の部分)。

本番の組み立て(create_app_from_env)が、フィクスチャ(fixtures/case*.toml)から、次の 2 つの自動応答を渡すこと。
- 途中確認の自動回答(CatalogAnswerer。FictionalAnswerer の本番の中身): 以前は渡しておらず、架空人物への途中確認が 24 時間止まった。
- 段階開示の自動応答(StageFlow): 架空の求人の「会う」「承認」。
CatalogAnswerer は、交渉ごとに、該当するフィクスチャの生の条件で答える(複数のケースの振り分け)。求人の規則は、候補者の属性帯の条件で
選ぶ。本物の候補者とフィクスチャの求人の交渉では、本物の候補者の属性帯(金庫の view の counterparty)で選ぶ。引けない交渉は、受けるとは
言わない(reject)。

金庫は本物の vault の app を ASGI のままつなぐ。LLM・GCP には接続しない。
"""

import asyncio
import dataclasses
import itertools
import logging

import pytest
from negotiation_core import AXES, iter_all_packages
from test_stages import (  # noqa: F401  (stage_env はフィクスチャ)
    CANDIDATE_TEMPLATE_ID,
    EMPLOYER_JOB_ID,
    EMPLOYER_TEMPLATE_ID,
    demo_negotiation,
    live_negotiation,
    make_case,
    stage_doc,
    stage_env,
)
from vault.fixtures import (
    FIXTURES_DIRECTORY,
    CaseFixture,
    EmployerRuleFixture,
    RawConditions,
    load_case_fixture,
    put_fixture_templates,
)
from vault.models import EmployerRule
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    make_candidate_template,
    make_employer_template,
    needs_confirmation_policy,
    sample_package,
)
from web import app as web_app_module
from web.app import create_app, create_app_from_env
from web.fictional_answerer import CatalogAnswerer, FixtureAnswerer, FixtureCatalog
from web.session import SESSION_KEY_ENV
from web_app_helpers import build_web_env
from web_helpers import ScriptedAnswerer, move_dict

_BOUND_AXES = ("remote_days", "night_duty", "review_months")


def answerer_of(env):
    """app の組み立てがレフェリーに渡した、途中確認の自動回答。"""
    return env.services.referees._deps.answerer


# ----------------------------------------------------------------------
# 本番の組み立て
# ----------------------------------------------------------------------


def test_the_production_entry_point_wires_the_fixture_answerer_and_the_stage_flow(default_db, session_key, monkeypatch):
    # ギャップ調査 2.6: create_app_from_env が answerer を渡していなかった(架空人物への途中確認が 24 時間止まる)。
    # 本番の起動口は、fixtures/ のケースを読み、途中確認の自動回答(CatalogAnswerer)と、段階開示の自動応答に渡す。
    monkeypatch.setattr(web_app_module, "_create_default_db", lambda: default_db)

    app = create_app_from_env({SESSION_KEY_ENV: session_key, "VAULT_BASE_URL": "http://vault.test"})

    services = app.state.services
    assert isinstance(services.referees._deps.answerer, CatalogAnswerer)
    catalog = services.stage_flow._fixtures
    assert catalog.employer_by_template("case1-employer").company_name == load_case_fixture(1).employer.company_name
    assert catalog.employer_by_job("case1-job") is catalog.employer_by_template("case1-employer")
    assert catalog.candidate_by_template("case1-candidate").contact == load_case_fixture(1).candidate.contact
    assert catalog.employer_by_template("no-such-template") is None


@pytest.mark.anyio
async def test_the_production_answerer_answers_for_the_fixtures_without_reaching_for_the_vault(default_db, session_key, monkeypatch):
    # 本番の組み立てのまま(金庫は届かない URL)、デモの候補者への途中確認に、フィクスチャの生の条件で答える。
    # 候補者側の自動回答は、段の状態に控えたテンプレート ID とフィクスチャだけで決まる(金庫を呼ばない)。
    monkeypatch.setattr(web_app_module, "_create_default_db", lambda: default_db)
    app = create_app_from_env({SESSION_KEY_ENV: session_key, "VAULT_BASE_URL": "http://vault.test"})
    services = app.state.services
    nid = "0123456789abcdef"
    await services.stages.ensure(nid, None, employer_template_id="case1-employer", candidate_template_id="case1-candidate")
    answerer = services.referees._deps.answerer
    case1 = load_case_fixture(1)

    for package in itertools.islice(iter_all_packages(), 0, None, 137):
        expected = await FixtureAnswerer(case1)(nid=nid, side="candidate", package=package)
        assert await answerer(nid=nid, side="candidate", package=package) == expected
    assert await answerer(nid=nid, side="candidate", package=sample_package(salary=1000, remote_days=5, night_duty=0)) == "accept"
    assert await answerer(nid=nid, side="candidate", package=sample_package(salary=400)) == "reject"


def test_without_fixtures_there_is_no_answerer_and_an_explicit_answerer_is_kept(default_db, session_key):
    # 既存の組み立て(テスト・スクリプト)は変わらない: fixtures を渡さなければ、answerer は渡したものだけ(なければ None)。
    # 明示の answerer は、fixtures があっても優先する。
    plain = create_app(vault=object(), default_db=default_db, session_key=session_key)
    explicit = ScriptedAnswerer()
    kept = create_app(vault=object(), default_db=default_db, session_key=session_key, answerer=explicit, fixtures=FixtureCatalog())
    derived = create_app(vault=object(), default_db=default_db, session_key=session_key, fixtures=FixtureCatalog())

    assert plain.state.services.referees._deps.answerer is None
    assert kept.state.services.referees._deps.answerer is explicit
    assert isinstance(derived.state.services.referees._deps.answerer, CatalogAnswerer)


# ----------------------------------------------------------------------
# フィクスチャの表
# ----------------------------------------------------------------------


def test_the_catalog_loads_every_case_file_and_looks_up_by_template_and_job(tmp_path, caplog):
    (tmp_path / "case1.toml").write_text(load_case_file_text(1), encoding="utf-8")
    other = load_case_file_text(1).replace("case = 1", "case = 2").replace("case1-", "case2-")
    (tmp_path / "case2.toml").write_text(other, encoding="utf-8")
    (tmp_path / "notes.toml").write_text("not = 'a case'", encoding="utf-8")  # case{N}.toml でないファイルは読まない
    (tmp_path / "case3.toml.bak").write_text("broken", encoding="utf-8")

    catalog = FixtureCatalog.load(tmp_path)

    assert catalog.employer_by_template("case1-employer").job_id == "case1-job"
    assert catalog.employer_by_job("case2-job").template_id == "case2-employer"
    assert catalog.candidate_by_template("case2-candidate").template_id == "case2-candidate"
    assert catalog.candidate_by_template("case9-candidate") is None
    with caplog.at_level(logging.WARNING):
        assert FixtureCatalog.load(tmp_path / "nothing-here").employer_by_template("case1-employer") is None
    assert "no fixture cases" in caplog.text


def test_two_fixtures_with_the_same_template_or_job_are_refused(tmp_path):
    # 同じテンプレート ID・求人 ID が 2 つのケースにあると、どちらを引くか決まらない。起動を拒否する。
    (tmp_path / "case1.toml").write_text(load_case_file_text(1), encoding="utf-8")
    (tmp_path / "case2.toml").write_text(load_case_file_text(1).replace("case = 1", "case = 2"), encoding="utf-8")

    with pytest.raises(ValueError, match="same"):
        FixtureCatalog.load(tmp_path)


def load_case_file_text(case: int) -> str:
    return (FIXTURES_DIRECTORY / f"case{case}.toml").read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# CatalogAnswerer: 交渉ごとのフィクスチャで答える
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_catalog_answerer_matches_the_case1_raw_conditions_for_both_sides(store, clock, vault_client, default_db, session_key):
    # AC-07・DV-11(配線): 本物のケース 1(fixtures/case1.toml)の交渉で、本番と同じ CatalogAnswerer の答えが、FixtureAnswerer(生の条件)と
    # 一致する(候補者側・求人側とも。全 18,000 通りから間引いた組み合わせと、境目の組み合わせ)。求人側は、金庫の view から候補者の帯を引く。
    case1 = load_case_fixture(1)
    put_fixture_templates(store._db, case1)
    env = build_web_env(store=store, clock=clock, vault=vault_client, default_db=default_db, session_key=session_key, fixtures=FixtureCatalog.load())
    try:
        visitor = env.browser()
        response = await visitor.post(
            "/v1/demo/negotiations",
            dict(request_id="request-demo1", candidate_template_id="case1-candidate", employer_template_id="case1-employer"),
        )
        nid = response.json()["nid"]
        boundary = [
            sample_package(salary=salary, remote_days=remote, night_duty=night, review_months=6)
            for salary in (600, 650, 700)
            for remote in (0, 1, 2, 4)
            for night in (0, 2, 4)
        ]
        packages = list(itertools.islice(iter_all_packages(), 0, None, 300)) + boundary
        for side in ("candidate", "employer"):
            for package in packages:
                expected = await FixtureAnswerer(case1)(nid=nid, side=side, package=package)
                assert await answerer_of(env)(nid=nid, side=side, package=package) == expected, (side, package)
    finally:
        await env.aclose()


def two_rule_case(second_rule_accepts_up_to: int) -> CaseFixture:
    """求人が 2 つの規則を持つケース: 経験年数帯が 3〜5 年なら年収 500 万まで、それ以外(when が空)は上限まで受ける。"""
    base = make_case()
    columns = list(itertools.product(*(AXES[axis].grid for axis in _BOUND_AXES)))

    def rule(when, limit):
        return EmployerRuleFixture(
            when=when, raw=RawConditions("employer", dict.fromkeys(columns, limit)), policy=accept_all_policy("employer")
        )

    employer = dataclasses.replace(
        base.employer, rules=(rule(dict(experience_band="3_to_5y"), 500), rule(dict(), second_rule_accepts_up_to))
    )
    return dataclasses.replace(base, employer=employer)


@pytest.mark.anyio
async def test_the_employer_rule_is_chosen_by_the_real_candidates_own_attribute_bands(
    store, clock, vault_client, default_db, session_key
):
    # 本物の候補者 × フィクスチャの求人: 求人の規則を選ぶ属性帯は、フィクスチャの候補者の帯ではなく、金庫が持つ本物の候補者の帯
    # (面談で保存した 3〜5 年)。FixtureAnswerer(フィクスチャの候補者の帯 5〜10 年で選ぶ)とは違う規則を選ぶ。
    case = two_rule_case(second_rule_accepts_up_to=900)
    assert case.candidate.attribute_bands.experience_band == "5_to_10y"
    env = build_web_env(
        store=store, clock=clock, vault=vault_client, default_db=default_db, session_key=session_key,
        fixtures=FixtureCatalog([case]),
    )  # fmt: skip
    try:
        env.put_employer_template(template_id=EMPLOYER_TEMPLATE_ID, job_id=EMPLOYER_JOB_ID)
        browser = env.browser()
        pid, nid = await live_negotiation(env, browser, agreed=False)
        answerer = answerer_of(env)
        expensive, cheap = sample_package(salary=600), sample_package(salary=500)

        by_real_bands = [await answerer(nid=nid, side="employer", package=package) for package in (expensive, cheap)]
        by_fixture_bands = [await FixtureAnswerer(case)(nid=nid, side="employer", package=package) for package in (expensive, cheap)]

        assert by_real_bands == ["reject", "accept"]  # 本物の候補者は 3〜5 年 → 上限 500 万の規則
        assert by_fixture_bands == ["accept", "accept"]  # フィクスチャの候補者は 5〜10 年 → 上限 900 万の規則
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_a_negotiation_the_catalog_cannot_resolve_is_answered_with_reject(stage_env):
    # フィクスチャを引けない交渉(フィクスチャにない求人・テンプレート ID を控えていないデモ・段の状態がない交渉)には、受けるとは言わない。
    # 答えないまま待つ(24 時間止まる)のではなく、reject で進める。
    env = stage_env()
    browser = env.browser()
    unknown_template = env.put_employer_template()
    pid = await browser.register()
    unknown_live = await browser.create_negotiation(pid, unknown_template)
    unattributed_demo = await demo_negotiation(env, browser, agreed=False)
    env.default_db.collection("stages").document(unattributed_demo).delete()
    await env.services.stages.ensure(unattributed_demo, None)  # テンプレート ID を控えない段の状態
    answerer = answerer_of(env)
    package = sample_package(salary=1000, remote_days=5, night_duty=0)

    answers = [
        await answerer(nid=unknown_live, side="employer", package=package),
        await answerer(nid=unattributed_demo, side="employer", package=package),
        await answerer(nid=unattributed_demo, side="candidate", package=package),
        await answerer(nid="0123456789abcdef", side="candidate", package=package),  # 段の状態がない
    ]

    assert answers == ["reject"] * 4


@pytest.mark.anyio
@pytest.mark.parametrize(("salary", "expected_answer"), [(650, "accept"), (700, "reject")])
async def test_the_referee_answers_a_fictional_employers_question_with_the_production_wiring(
    store, clock, vault_client, default_db, session_key, salary, expected_answer
):
    # 配線の確認(エンドツーエンド): 本番と同じ組み立て(fixtures を渡すだけ)で、求人側が途中確認(ask_principal)を出すと、レフェリーが
    # CatalogAnswerer で自動回答し、金庫が回答を追記して、交渉が進む(以前は answerer が None で、24 時間止まった)。
    env = build_web_env(
        store=store, clock=clock, vault=vault_client, default_db=default_db, session_key=session_key,
        fixtures=FixtureCatalog([make_case()]), run_referees=True,
    )  # fmt: skip
    try:
        put_template(store._db, make_candidate_template(template_id=CANDIDATE_TEMPLATE_ID))
        put_template(
            store._db,
            make_employer_template(
                template_id=EMPLOYER_TEMPLATE_ID,
                job_id=EMPLOYER_JOB_ID,
                rules=[EmployerRule(when=dict(), policy=needs_confirmation_policy("employer"))],
            ),
        )
        package = sample_package(salary=salary, remote_days=0, night_duty=0, review_months=6)
        env.agents.script("candidate", move_dict("propose", package))
        env.agents.script("employer", move_dict("ask_principal", package), move_dict("end"))
        nid = await demo_negotiation(env, env.browser(), agreed=False)

        await asyncio.wait_for(env.services.referees.task(nid), 30)

        answers = [event for event in store.get_events(nid, "employer") if event.kind == "principal_answer"]
        assert [event.answer for event in answers] == [expected_answer]
        assert store.get_view(nid, "employer").status == "judged"
    finally:
        await env.aclose()


@pytest.mark.anyio
async def test_a_resent_demo_creation_keeps_the_template_ids_on_the_stage_document(stage_env):
    # 同じ request_id の再送は、同じ交渉を返し(二重に作らない)、段の状態のテンプレート ID は残る(自動応答がフィクスチャを引く元)。
    env = stage_env()
    visitor = env.browser()
    first = await demo_negotiation(env, visitor, agreed=False)
    second = await demo_negotiation(env, visitor, agreed=False)  # 同じ request_id

    assert first == second
    document = stage_doc(env, first)
    assert (document["employer_template_id"], document["candidate_template_id"]) == (EMPLOYER_TEMPLATE_ID, CANDIDATE_TEMPLATE_ID)
