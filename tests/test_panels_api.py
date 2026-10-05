"""FR-39・X-6: 並べて見る画面の 2 つのパネル「最悪漏れてもここまで」「まだ隠しているもの」の API(design.md §7・§5 の 7・§2.5。台帳 I-23)。

- 「最悪漏れてもここまで」は、本人の丸め済みポリシー(金庫の GET policy と同じ読み出し口)から、軸ごとに、丸めたマスを返す。
  外した軸は removed の印だけで、マスは出さない。
- 「まだ隠しているもの」は値を持たない。軸ごとに、種類(range・category)とマスの数だけ。生の値も、丸めたマスの範囲も、ここには出ない
  (生の値はどこにも保存していないので、「丸めたマスの幅の中のどこか」としか言えない)。
- 表示のたびに金庫から作り、web には保存しない。本人のセッションが要る(401)。ほかの依頼者の ID は 403。

金庫は本物の vault の app を ASGI のままつなぎ、web の app へは Browser(クッキーを持つ httpx のクライアント)から入る。
"""

import json
import re

import pytest

from negotiation_core import AXIS_KEYS, Anchor, Policy
from vault.api_models import PolicyView
from vault.fixtures import load_case_fixture
from web.panels_api import build_panels
from web_app_helpers import dump_documents, submit_interview


def panels_path(who: str = "me") -> str:
    return f"/v1/principals/{who}/panels"


def _anchor(**values) -> dict:
    """面談の送信の、丸める前のアンカー(全軸の値)。"""
    base = dict(salary=650, remote_days=2, night_duty=4, review_months=12, training="*", side_job="*", start="*")
    return {**base, **values}


def _cells(entries: list[dict]) -> dict[str, list[dict]]:
    return {entry["axis"]: entry["cells"] for entry in entries}


# 面談の既定の本文(tests/web_app_helpers.py の interview_body)を丸めたポリシー: 受けるアンカー(650・リモート 2・当直 4・見直し 12)と
# 受けないアンカー(400・リモート 0・当直 8・見直し 12)。生の 620 万・410 万は、丸めて 650 万・400 万になる。
DEFAULT_WORST_CASE = [
    {"axis": "salary", "removed": False, "cells": [{"low": 400, "high": 450}, {"low": 600, "high": 650}]},
    {"axis": "remote_days", "removed": False, "cells": [{"low": 0, "high": 0}, {"low": 2, "high": 2}]},
    {"axis": "night_duty", "removed": False, "cells": [{"low": 4, "high": 4}, {"low": 8, "high": 8}]},
    {"axis": "review_months", "removed": False, "cells": [{"low": 12, "high": 12}]},
    {"axis": "training", "removed": False, "cells": []},
    {"axis": "side_job", "removed": False, "cells": []},
    {"axis": "start", "removed": False, "cells": []},
]
DEFAULT_STILL_HIDDEN = [
    {"axis": "salary", "kind": "range", "removed": False, "cells": 2},
    {"axis": "remote_days", "kind": "range", "removed": False, "cells": 0},
    {"axis": "night_duty", "kind": "range", "removed": False, "cells": 0},
    {"axis": "review_months", "kind": "range", "removed": False, "cells": 0},
    {"axis": "training", "kind": "category", "removed": False, "cells": 0},
    {"axis": "side_job", "kind": "category", "removed": False, "cells": 0},
    {"axis": "start", "kind": "category", "removed": False, "cells": 0},
]


# ----------------------------------------------------------------------
# 本人のセッションで読む(API)
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_panels_show_the_rounded_cells_per_axis_and_only_the_kind_and_number_of_cells_for_what_is_hidden(web_app):
    browser = web_app.browser()
    await browser.register()

    response = await browser.get(panels_path())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"worst_case", "still_hidden"}
    assert body["worst_case"] == DEFAULT_WORST_CASE
    assert body["still_hidden"] == DEFAULT_STILL_HIDDEN
    # 軸ごとに 1 件、語彙の順
    assert [e["axis"] for e in body["worst_case"]] == [e["axis"] for e in body["still_hidden"]] == list(AXIS_KEYS)
    assert "set-cookie" not in response.headers


@pytest.mark.anyio
async def test_the_raw_values_never_appear_and_still_hidden_has_no_values_at_all(web_app):
    # X-6: 生の値は、どこにも保存していないので、パネルにも出ない。「まだ隠しているもの」は、丸めたマスの範囲も含めて、値を持たない。
    # カナリア: 面談に入れた生の年収(1130 万・1010 万)と、丸めた後の値(1150・1100・1000・1050)が、still_hidden のどこにも現れない。
    raw_accept = _anchor(salary=1130, remote_days=3, night_duty=6, review_months=6, training="available")
    raw_reject = _anchor(salary=1010, remote_days=1, night_duty=8, review_months=12, training="none")
    browser = web_app.browser()
    await browser.register(accept_anchors=[raw_accept], reject_anchors=[raw_reject])

    response = await browser.get(panels_path())

    body = response.json()
    assert _cells(body["worst_case"])["salary"] == [{"low": 1000, "high": 1050}, {"low": 1100, "high": 1150}]  # 丸めたマスは worst_case にある
    still_hidden = json.dumps(body["still_hidden"])
    for canary in ("1130", "1010", "1150", "1100", "1000", "1050"):
        assert canary not in still_hidden, canary
    assert re.search(r"\d{3,}", still_hidden) is None  # 3 桁以上の数(値らしいもの)は、ひとつもない
    assert all(set(entry) == {"axis", "kind", "removed", "cells"} for entry in body["still_hidden"])
    assert all(isinstance(entry["cells"], int) and 0 <= entry["cells"] <= 6 for entry in body["still_hidden"])
    assert {entry["axis"]: entry["cells"] for entry in body["still_hidden"]}["salary"] == 2  # 隠れているマスの数だけ
    # 生の値は、応答のどこにも出ない(グリッドの上の値だけ)
    assert "1130" not in response.text and "1010" not in response.text


@pytest.mark.anyio
async def test_the_default_interviews_raw_values_do_not_appear_either(web_app):
    browser = web_app.browser()
    await browser.register()  # 面談の既定の本文: 受けるアンカーの年収は生の 620 万、受けないアンカーは生の 410 万

    response = await browser.get(panels_path())

    assert "620" not in response.text and "410" not in response.text
    assert "650" in response.text and "400" in response.text  # 丸めた後の値は worst_case にある(対照)


@pytest.mark.anyio
async def test_removed_axes_are_marked_and_show_no_cells_whatever_the_stored_policy_holds(web_app):
    # 外した軸(§2.4): 「外しています(交渉中に確認)」の印だけ。この軸の意向は出さない(保存されたアンカーにこの軸の値が残っていても)。
    # 数値軸(当直)と区分軸(研修)のそれぞれ。
    browser = web_app.browser()
    await browser.register(removed_axes=["night_duty", "training"])

    body = (await browser.get(panels_path())).json()

    worst = {entry["axis"]: entry for entry in body["worst_case"]}
    hidden = {entry["axis"]: entry for entry in body["still_hidden"]}
    assert worst["night_duty"] == {"axis": "night_duty", "removed": True, "cells": []}  # 保存されたアンカーは当直 4 と 8 を持つが、出ない
    assert worst["training"] == {"axis": "training", "removed": True, "cells": []}
    assert hidden["night_duty"] == {"axis": "night_duty", "kind": "range", "removed": True, "cells": 5}  # その軸の値の数(全体が未確定)
    assert hidden["training"] == {"axis": "training", "kind": "category", "removed": True, "cells": 2}
    # 外していない軸は、外した軸の有無に関わらず同じ
    for axis in AXIS_KEYS:
        if axis not in ("night_duty", "training"):
            assert worst[axis] == next(e for e in DEFAULT_WORST_CASE if e["axis"] == axis)
            assert hidden[axis] == next(e for e in DEFAULT_STILL_HIDDEN if e["axis"] == axis)
    assert [e["axis"] for e in body["worst_case"] if e["removed"]] == ["night_duty", "training"]


@pytest.mark.anyio
async def test_a_policy_that_says_nothing_has_no_cells_and_hides_nothing(web_app):
    browser = web_app.browser()
    await browser.register(accept_anchors=[], reject_anchors=[])

    body = (await browser.get(panels_path())).json()

    assert all(entry["cells"] == [] and entry["removed"] is False for entry in body["worst_case"])
    assert all(entry["cells"] == 0 for entry in body["still_hidden"])


@pytest.mark.anyio
async def test_without_a_session_it_is_401_and_another_principals_id_is_403_and_me_means_the_caller(web_app):
    # §6.3: セッションがなければ 401(ID を発行しない)。ほかの依頼者の ID は 403。me は、セッションの依頼者(自分の ID と同じ答え)。
    mine, others, stranger = web_app.browser(), web_app.browser(), web_app.browser()
    pid = await mine.register()
    other_pid = await others.register(
        accept_anchors=[_anchor(salary=1130)], reject_anchors=[_anchor(salary=1010, remote_days=0, night_duty=8)]
    )

    by_alias = await mine.get(panels_path("me"))
    by_id = await mine.get(panels_path(pid))
    others_own = await others.get(panels_path("me"))
    refused = await mine.get(panels_path(other_pid))
    anonymous = [await stranger.get(panels_path(who)) for who in ("me", pid, other_pid)]

    assert by_alias.status_code == by_id.status_code == 200 and by_alias.json() == by_id.json()
    assert by_alias.json()["worst_case"] == DEFAULT_WORST_CASE
    assert others_own.json() != by_alias.json()  # 別の人には、別の答え(me は呼んだ人のもの)
    assert (refused.status_code, refused.json()) == (403, {"detail": "forbidden"})
    assert "1150" not in refused.text
    assert [(r.status_code, r.json()) for r in anonymous] == [(401, {"detail": "no_session"})] * 3
    assert all("set-cookie" not in r.headers for r in anonymous)


@pytest.mark.anyio
async def test_a_visitor_who_has_not_submitted_an_interview_has_no_panels(web_app):
    # 金庫にポリシーがない(面談を送っていない)依頼者は 404(GET policy と同じ)。
    browser = web_app.browser()
    pid = await browser.open_start_page()

    response = await browser.get(panels_path())

    assert (response.status_code, response.json()) == (404, {"detail": "not_found"})
    assert (await browser.get(f"/v1/principals/{pid}/policy")).status_code == 404  # 読み出し口が同じ


@pytest.mark.anyio
async def test_the_panels_are_built_from_the_vault_at_every_call_and_nothing_is_stored(web_app):
    # §7: 表示のたびに金庫の読み出し口から作る。web には保存しない: 読んでも (default) も金庫も変わらず、ポリシーが変われば次の表示が変わる。
    browser = web_app.browser()
    pid = await browser.register()
    first = await browser.get(panels_path())
    vault_before, default_before = dump_documents(web_app.store._db), dump_documents(web_app.default_db)

    again = await browser.get(panels_path())

    assert again.json() == first.json()
    assert dump_documents(web_app.store._db) == vault_before and dump_documents(web_app.default_db) == default_before
    # ポリシーを置き直す(面談をやり直す)と、次の表示にそのまま出る
    await submit_interview(web_app.services, pid, accept_anchors=[_anchor(salary=900)], reject_anchors=[])
    changed = (await browser.get(panels_path())).json()
    assert _cells(changed["worst_case"])["salary"] == [{"low": 850, "high": 900}]
    assert changed != first.json()


# ----------------------------------------------------------------------
# 純粋な計算(build_panels)
# ----------------------------------------------------------------------


def _view(policy: Policy, removed_axes=()) -> PolicyView:
    return PolicyView(policy=policy, removed_axes=list(removed_axes), attribute_bands=None)


def test_the_cells_of_case3s_candidate_are_the_three_salary_cells_of_its_boundaries():
    # ケース 3 の候補者(リモート 0 日 680・1 日 620・2 日以上 570 → 丸めて 700・650・600)。年収の境目は 3 つのマスの中のどこか。
    panels = build_panels(_view(load_case_fixture(3).candidate.policy))

    worst = {entry.axis: [c.model_dump() for c in entry.cells] for entry in panels.worst_case}
    hidden = {entry.axis: (entry.kind, entry.cells) for entry in panels.still_hidden}
    assert worst["salary"] == [{"low": 550, "high": 600}, {"low": 600, "high": 650}, {"low": 650, "high": 700}]
    assert hidden["salary"] == ("range", 3)  # マスの数。範囲は worst_case にあって、ここにはない
    assert worst["remote_days"] == [{"low": 0, "high": 0}, {"low": 1, "high": 1}, {"low": 2, "high": 2}]
    assert hidden["remote_days"] == ("range", 0)  # 離散軸は、丸めても情報が減らない(条件そのものが知られ得る)
    assert worst["review_months"] == []  # 昇給見直しは気にしない(どのアンカーも中立)


def test_a_categorical_axis_shows_the_values_the_policy_limits_and_nothing_for_either():
    accept = Anchor(salary=300, remote_days=0, night_duty=8, review_months=12, training="available", side_job="*", start="within_6_months")
    reject = Anchor(salary=1500, remote_days=5, night_duty=0, review_months=6, training="none", side_job="*", start="within_1_month")
    panels = build_panels(_view(Policy(side="candidate", accept_anchors=[accept], reject_anchors=[reject])))

    worst = {entry.axis: [c.model_dump() for c in entry.cells] for entry in panels.worst_case}
    assert worst["training"] == [{"value": "none"}, {"value": "available"}]  # 語彙の順
    assert worst["start"] == [{"value": "within_1_month"}, {"value": "within_6_months"}]
    assert worst["side_job"] == []  # * は条件ではない
    assert all(not worst[axis] for axis in ("salary", "remote_days", "night_duty", "review_months"))  # 中立な値は条件ではない
    assert [(e.axis, e.cells) for e in panels.still_hidden if e.kind == "category"] == [("training", 0), ("side_job", 0), ("start", 0)]


def test_the_cells_follow_the_direction_of_the_side_of_the_policy():
    # 求人側(年収は低いほど良い)の政策でも、受けるアンカーは悪い側(高い方)の隣まで、受けないアンカーは良い側(低い方)の隣まで。
    accept = Anchor(salary=600, remote_days=5, night_duty=0, review_months=6, training="*", side_job="*", start="*")
    reject = Anchor(salary=700, remote_days=0, night_duty=8, review_months=12, training="*", side_job="*", start="*")
    panels = build_panels(_view(Policy(side="employer", accept_anchors=[accept], reject_anchors=[reject])))

    (salary,) = [e for e in panels.worst_case if e.axis == "salary"]
    assert [c.model_dump() for c in salary.cells] == [{"low": 600, "high": 650}, {"low": 650, "high": 700}]
    assert all(not e.cells for e in panels.worst_case if e.axis != "salary")


@pytest.mark.anyio
async def test_the_panel_routes_are_in_the_openapi_schema_of_the_web_app(web_app):
    paths = web_app.app.openapi()["paths"]

    assert list(paths["/v1/principals/{pid}/panels"]) == ["get"]
