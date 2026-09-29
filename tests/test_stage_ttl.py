"""台帳 I-6: デモ・攻撃の交渉の段の状態 stages/{nid} に、金庫と同じ 96 時間の TTL を付ける(design.md §3.8・§6.2)。

見回りは、架空の候補者の交渉にも段 0 の stages/{nid} を作る。金庫のデモ・攻撃の交渉には TTL(96 時間)があるが、
web の stages/{nid} に TTL も削除もなければ、デモと攻撃モードを使うたびに (default) に文書が増え続ける。
そのため、デモ・攻撃の段の状態には期限の項目(ttl_at)を付ける。本物の利用者の段の状態には付けない
(本人の削除と 30 日の自動削除で消える)。Firestore の TTL ポリシー自体の設定はデプロイの段で行う。
"""

import datetime as dt

import pytest
from vault.config import DEFAULT_VAULT_CONFIG
from vault_helpers import put_candidate_and_employer_templates
from web.config import DEFAULT_WEB_CONFIG
from web_helpers import create_demo_negotiation

_TTL = dt.timedelta(hours=96)


def _stage(default_db, nid: str) -> dict:
    return default_db.collection("stages").document(nid).get().to_dict()


def test_the_stage_ttl_is_the_same_96_hours_as_the_vaults():
    # 金庫のデモ・攻撃の交渉の TTL と同じ(どちらかだけを変えると、この確認が知らせる)。
    assert DEFAULT_WEB_CONFIG.retention.fictional_stage_ttl_seconds == 96 * 3600
    assert (
        DEFAULT_WEB_CONFIG.retention.fictional_stage_ttl_seconds
        == DEFAULT_VAULT_CONFIG.fictional_negotiation_ttl_seconds
    )


@pytest.mark.anyio
async def test_a_demo_stage_created_through_the_api_has_a_96_hour_ttl_like_the_vaults_negotiation(web_app):
    # I-6: デモの交渉を作った直後の stages/{nid} に、96 時間の期限の項目がある(金庫の交渉の期限と同じ時刻)。
    candidate_template, employer_template = put_candidate_and_employer_templates(web_app.store._db)
    browser = web_app.browser()

    response = await browser.post(
        "/v1/demo/negotiations",
        {
            "request_id": "request-demo1",
            "candidate_template_id": candidate_template.template_id,
            "employer_template_id": employer_template.template_id,
        },
    )

    assert response.status_code == 200
    nid = response.json()["nid"]
    stage = _stage(web_app.default_db, nid)
    assert stage["candidate_principal_id"] is None
    assert stage["ttl_at"] == web_app.clock.now() + _TTL
    vault_negotiation = web_app.store._negotiation_ref(nid).get().to_dict()
    assert stage["ttl_at"] == vault_negotiation["ttl_at"]  # 金庫の交渉と同じ期限


@pytest.mark.anyio
async def test_the_sweeper_gives_demo_and_attack_stages_a_ttl_and_a_real_principals_stage_none(store, web_app):
    # I-6: 見回りが作る段の状態にも同じ。デモ・攻撃(候補者が架空人物)には期限の項目があり、本物の利用者の
    # 段の状態には、期限の項目そのものがない(見回りが作り直しても同じ)。
    demo_nid = create_demo_negotiation(store)
    attack_nid = create_demo_negotiation(store, mode="attack")
    browser = web_app.browser()
    pid = await browser.register()
    live_nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    web_app.default_db.collection("stages").document(live_nid).delete()  # 作り損ねた状態を作る

    report = await web_app.services.sweeper.sweep_once()

    assert report.stages_created == 3
    now = web_app.clock.now()
    for nid in (demo_nid, attack_nid):
        stage = _stage(web_app.default_db, nid)
        assert stage["candidate_principal_id"] is None
        assert stage["ttl_at"] == now + _TTL
    live_stage = _stage(web_app.default_db, live_nid)
    assert live_stage["candidate_principal_id"] == pid
    assert "ttl_at" not in live_stage  # 本物の利用者の段の状態には付けない


@pytest.mark.anyio
async def test_a_real_principals_stage_created_with_the_negotiation_has_no_ttl(web_app):
    # I-6: 本物の利用者が交渉を作った直後の段の状態にも、期限の項目はない(TTL で消えない)。
    browser = web_app.browser()
    pid = await browser.register()

    nid = await browser.create_negotiation(pid, web_app.put_employer_template())

    stage = _stage(web_app.default_db, nid)
    assert stage["candidate_principal_id"] == pid
    assert "ttl_at" not in stage
    web_app.clock.advance(dt.timedelta(days=10))  # 時間がたっても付かない(段の状態を作り直さない)
    assert "ttl_at" not in _stage(web_app.default_db, nid)
