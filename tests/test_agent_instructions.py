"""交渉エージェントの指示文(design.md §4.2「指示文の要点」・v14)。

指示文は LLM に渡る固定の前文で、側ごとに 1 つ(計画と決定で共有)。内容の良し悪しは本物の Gemini でしか確かめられない
(DV-15)ので、ここでは、設計と食い違うと機械的に分かる点だけを確かめる: v14 で足した項目(2 種類の呼び出し・checked・
`check` という手がないこと・output_truncated・評価の残しかた)があること、グリッドと軸の向きが設定と同じであること、
出せる手が AgentMoveType と同じであること。
"""

import re
from typing import get_args

import pytest
from negotiation_core import AXES
from negotiation_core.schema import AgentMoveType, LastErrorReason, Phase

from agents.instructions import load_instruction

ROLES_WITH_FULL_INSTRUCTION = ["candidate", "employer"]


@pytest.mark.parametrize("role", ROLES_WITH_FULL_INSTRUCTION)
def test_the_instruction_explains_the_two_kinds_of_calls_and_the_checked_results(role):
    # §4.2 (2 種類の呼び出し: 計画 plan は確かめたい案を最大 3 つ並べる(確かめが要らなければ手をそのまま出す)、決定 decide は
    # checked を見て手を 1 つ出す。出力は plan/v1・move/v1)
    text = load_instruction(role)
    for term in ("phase", "plan/v1", "move/v1", "checks", "checked", "最大 3 つ"):
        assert term in text, term
    assert list(get_args(Phase)) == ["plan", "decide"]
    assert "plan（出力は plan/v1）" in text and "decide（出力は move/v1）" in text


@pytest.mark.parametrize("role", ROLES_WITH_FULL_INSTRUCTION)
def test_the_instruction_lists_exactly_the_moves_an_agent_can_make_and_says_there_is_no_check(role):
    # §2.7・台帳 X-45 (出せる手は AgentMoveType の 5 つ。check という手はない。確かめられるのは plan の checks だけ)
    text = load_instruction(role)
    moves = "・".join(get_args(AgentMoveType))
    assert f"手は {moves} の {len(get_args(AgentMoveType))} つ。check という手はない。" in text
    assert "- check:" not in text and "check（確かめる）" not in text  # v13 までの、手としての check の説明は残っていない


@pytest.mark.parametrize("role", ROLES_WITH_FULL_INSTRUCTION)
def test_the_instruction_explains_output_truncated_and_the_allowance_of_evaluations(role):
    # §2.7・台帳 C-53・C-47 (output_truncated は、短く答える手がかり。評価は、残りの手数と途中確認の分を残す)
    text = load_instruction(role)
    assert "output_truncated" in get_args(LastErrorReason)
    assert re.search(r"output_truncated: .*短く答える", text)
    assert "remaining_moves と remaining_principal_checks の合計" in text
    for term in ("last_error", "last_invalid", "ask_principal", "end"):
        assert term in text, term


@pytest.mark.parametrize("role", ROLES_WITH_FULL_INSTRUCTION)
def test_the_instruction_keeps_the_mechanical_concession_procedure(role):
    # §4.2・台帳 I-13 (譲歩の手順は、機械的な手順のまま: S と T から N を作り、plan の checks に、N と、N の寄せた軸を 1 つ S に
    # 戻した案を、この順で並べる。差が縮んだら、T の salary だけを自分の側に 1 段寄せた案を先頭に置く。同じ案を 2 回提案しない)
    text = load_instruction(role)
    for fragment in (
        "あなたの直前の提案を S、相手の直前の提案を T とする",
        "salary: S と T の差の、およそ半分だけ T に寄せる（50 刻みに丸める）",
        "すべて 1 段ずつ T の側へ寄せる",
        "N と、2. で寄せた軸のうち 1 つ（salary 以外から、1 つずつ順に）を S の値に戻した案を、この順で並べる",
        "T の salary だけを、あなたの側に 1 段（50）寄せた案",  # L15-5: 寄せる軸を salary に決めた
        "同じ組み合わせを 2 回 propose しない",
    ):
        assert fragment in text, fragment


@pytest.mark.parametrize("role", ROLES_WITH_FULL_INSTRUCTION)
def test_the_grid_in_the_instruction_is_the_grid_in_the_config(role):
    # §2.1・§4.2 (前文は「指示文＋グリッド」。グリッドが設定と食い違うと、LLM は列挙外の値を前提に考えてしまう)
    text = load_instruction(role)
    salary = AXES["salary"].grid
    assert f"salary（比較基準年収、万円）: {salary[0]}〜{salary[-1]} の {salary[1] - salary[0]} 刻み" in text
    remote = AXES["remote_days"].grid
    assert f"remote_days（週のリモート日数）: {remote[0]}〜{remote[-1]}" in text
    assert "night_duty（月の当直回数）: " + "・".join(map(str, AXES["night_duty"].grid)) in text
    assert "review_months（昇給見直しまでの月数）: " + "・".join(map(str, AXES["review_months"].grid)) in text
    for axis, label in (("training", "研修"), ("side_job", "副業"), ("start", "入職時期")):
        assert f"{axis}（{label}）: " + "・".join(AXES[axis].grid) in text


@pytest.mark.parametrize(("role", "side"), [("candidate", "candidate"), ("employer", "employer")])
def test_the_axis_directions_in_the_instruction_are_the_directions_in_the_config(role, side):
    # §2.1・§4.2 (軸ごとの向きは、自分の側の分だけ指示文に書く。TurnInput に向きの情報がないため)
    text = load_instruction(role)
    higher = {"higher_is_better": "高い", "lower_is_better": "低い"}
    more = {"higher_is_better": "多い", "lower_is_better": "少ない"}
    directions = {axis: AXES[axis].direction_for(side) for axis in ("salary", "remote_days", "night_duty", "review_months")}
    line = (
        f"salary は{higher[directions['salary']]}ほど、remote_days は{more[directions['remote_days']]}ほど、"
        f"night_duty は{more[directions['night_duty']]}ほど、"
    )
    assert line in text
    short = "短い（6）" if directions["review_months"] == "lower_is_better" else "長い（12）"
    assert f"review_months は{short}ほど良い" in text


def test_the_two_sides_differ_only_in_who_they_act_for_and_the_directions():
    # §4.2 (指示文は側ごとに 1 つ。候補者側と求人側は、依頼者の説明・counterparty の説明・例・軸の向きだけが違う)
    candidate = load_instruction("candidate").splitlines()
    employer = load_instruction("employer").splitlines()
    assert len(candidate) == len(employer)
    differing = [i for i, (a, b) in enumerate(zip(candidate, employer)) if a != b]
    assert len(differing) == 5  # 1 行目(誰の代理か)・counterparty・decide の例・軸の向き・譲歩の例
    assert "候補者（あなたの依頼者）" in candidate[0] and "企業（あなたの依頼者）" in employer[0]


def test_the_attacker_instruction_is_still_a_stub_but_names_the_v14_outputs():
    # §8.2 (攻撃モードの指示文は ③ で書く。それまでの仮の文は、v14 の出力(plan/v1・move/v1)と矛盾しない)
    text = load_instruction("attacker")
    assert "仮の指示文" in text
    assert "plan/v1" in text and "move/v1" in text
    assert "principal_instruction" in text
