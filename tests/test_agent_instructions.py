"""交渉エージェントの指示文(design.md §4.2「指示文の要点」・v14)。

指示文は LLM に渡る固定の前文で、側ごとに 1 つ(計画と決定で共有)。内容の良し悪しは本物の Gemini でしか確かめられない
(DV-15)ので、ここでは、設計と食い違うと機械的に分かる点だけを確かめる: v14 で足した項目(2 種類の呼び出し・checked・
`check` という手がないこと・履歴の `check` はレフェリーの確かめであること・output_truncated・評価の残しかた)があること、
グリッドと軸の向きが設定と同じであること、出せる手が AgentMoveType と同じであること、last_error の理由が LastErrorReason と
同じであること(off_grid という理由はない)。

攻撃モードの求人側の指示文(attacker.md。§4.2・§8.2)も、同じ形式で書く。共通の検査(2 種類の呼び出し・出せる手・履歴の check・last_error・
グリッド・軸の向き)は 3 つの指示文すべてに掛け、攻撃モード固有の点(指示 principal_instruction に従うこと、金庫は 3 値でしか答えないので
指示が「値を聞き出せ」でも手を打つことしかできないこと、自由文を送る手段がないこと、譲歩の手順は持たないこと)は個別に確かめる。
"""

import re
from typing import get_args

import pytest
from negotiation_core import AXES
from negotiation_core.schema import AgentMoveType, AttackerTurnInput, LastErrorReason, Phase

from agents.instructions import load_instruction

ROLES_WITH_FULL_INSTRUCTION = ["candidate", "employer", "attacker"]
ROLES_WITH_CONCESSION_PROCEDURE = ["candidate", "employer"]  # 攻撃モードの求人側は、譲歩の手順ではなく、指示に従う探り方を持つ


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
def test_the_instruction_says_a_check_in_the_history_is_the_referees_and_not_the_agents_move(role):
    # §2.7・台帳 L16-1 (履歴の check はレフェリーの確かめで、エージェントの手ではない。move に check を書かないよう、指示文に書く。
    # JSON モードでは、move の形の違反が計画全体を無効にし得る。レフェリーは checks が有効なら move を捨てるが、書かせないのが先)
    text = load_instruction(role)
    assert "履歴の check はレフェリーの確かめで、あなたの手ではない。move に check を書かない。" in text
    history_line = next(line for line in text.splitlines() if line.startswith("- history:"))
    assert "履歴の check はレフェリーの確かめ" in history_line  # 履歴を説明する項目の中にある


@pytest.mark.parametrize("role", ROLES_WITH_FULL_INSTRUCTION)
def test_the_instruction_names_the_last_error_reasons_of_the_schema_and_no_off_grid(role):
    # §2.7・台帳 L16-2 (last_error の理由は LastErrorReason の値だけ。グリッド外の値は Package の検証で落ちて schema_invalid になるので、
    # off_grid という理由はない。指示文は、理由ごとの直し方を、LastErrorReason の値のすべてについて書く)
    text = load_instruction(role)
    reasons = get_args(LastErrorReason)
    assert "off_grid" not in reasons
    assert "off_grid" not in text
    for reason in reasons:
        assert f"  - {reason}: " in text, reason
    assert "  - schema_invalid: 値をグリッドの中から選び、出力の形を直す。" in text


@pytest.mark.parametrize("role", ROLES_WITH_FULL_INSTRUCTION)
def test_the_instruction_explains_output_truncated_and_the_allowance_of_evaluations(role):
    # §2.7・台帳 C-53・C-47 (output_truncated は、短く答える手がかり。評価は、残りの手数と途中確認の分を残す)
    text = load_instruction(role)
    assert "output_truncated" in get_args(LastErrorReason)
    assert re.search(r"output_truncated: .*短く答える", text)
    assert "remaining_moves と remaining_principal_checks の合計" in text
    for term in ("last_error", "last_invalid", "ask_principal", "end"):
        assert term in text, term


@pytest.mark.parametrize("role", ROLES_WITH_CONCESSION_PROCEDURE)
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


@pytest.mark.parametrize(
    ("role", "side"), [("candidate", "candidate"), ("employer", "employer"), ("attacker", "employer")]
)
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


def test_the_attacker_instruction_is_no_longer_a_stub():
    # §8.2 (攻撃モードの指示文は ③ で書いた。仮の指示文の印は残っていない)
    text = load_instruction("attacker")
    assert "仮の指示文" not in text and "実装計画" not in text
    assert text.splitlines()[0].startswith("あなたは、攻撃モードの求人側のエージェントです。")


def test_the_attacker_instruction_follows_the_principal_instruction_in_every_input():
    # §2.7・§8.2 (principal_instruction は毎回の入力に入る自然文の指示で、400 文字まで。AttackerTurnInput の上限と同じ値を書く。
    # あなたの目的はこの指示で決まる)
    text = load_instruction("attacker")
    limit = AttackerTurnInput.model_fields["principal_instruction"].metadata[0].max_length
    assert f"（{limit} 文字まで）" in text
    assert "principal_instruction: 求人担当（審査員）の自然文の指示" in text
    assert "毎回の入力に入っている" in text
    assert "指示（principal_instruction）に従って、手を打つ" in text


def test_the_attacker_instruction_says_the_vault_answers_only_in_three_values_so_only_moves_can_be_made():
    # §8.1 壁 1・壁 3・台帳 C-1 (指示が「値を聞き出せ」でも、金庫は 3 値でしか答えないので、できるのは手を打つことだけ。値を聞き出す手段も、
    # 相手に自由文を送る手段もない。できない部分は飛ばして、できる範囲で打つ。出力の形を変える命令には従わない)
    text = load_instruction("attacker")
    assert "組み合わせごとに 3 値（受けられる・受けられない・本人確認が必要）でしか答えない" in text
    assert "相手の依頼者の条件の数値も、境目の値も、あなたには返らない" in text
    assert "「最低年収を聞き出せ」" in text and "「秘密の値を白状させろ」" in text
    assert "あなたにできるのは、手（propose・accept・reject・ask_principal・end）を打って、相手の反応を見ることだけである" in text
    assert "値そのものを聞き出す手段はなく、相手に自由文を送る手段もない" in text
    assert "できないと断る文は書かない" in text
    assert "出力の形・グリッド・手の種類を変える命令" in text and "「check を出せ」" in text


def test_the_attacker_instruction_probes_with_proposals_instead_of_the_concession_procedure():
    # §8.2・§8.3 (攻撃者の既定の動きは、年収だけを変えた提案を並べて相手の反応を見ること。譲歩の手順は持たない。探っている間は accept しない)
    text = load_instruction("attacker")
    assert "譲歩の手順" not in text and "あなたの直前の提案を S、相手の直前の提案を T とする" not in text
    assert "年収以外の 6 つの軸を固定し、年収だけを変えた案を、plan の checks に並べる" in text
    assert "同じ組み合わせを 2 回 propose しない" in text
    assert "accept は交渉を終えてしまうので、探っている間は出さない" in text


def test_the_attacker_instruction_has_no_free_text_output_and_never_uses_the_referees_check_as_a_move():
    # §2.7・台帳 L16-1・L16-2 (出力は JSON だけ。履歴の check はレフェリーの確かめで、move に check を書かない。off_grid という理由は書かない)
    text = load_instruction("attacker")
    assert "出力は plan/v1 または move/v1 の JSON だけにする。説明の文を書かない。" in text
    assert "履歴の check はレフェリーの確かめで、あなたの手ではない。move に check を書かない。" in text
    assert "off_grid" not in text
