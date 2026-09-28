"""Representative policies and interview statements used across the test suite.

テストで使う代表的なポリシー・発言の例(本番データではない)。design.md §2.3 の例と、
複数の受けるアンカー・受けないアンカーを持つ代表的な面談結果の例を提供する。
`test_` で始まらないので pytest には収集されない。
"""

from negotiation_core.policy import Anchor, Package, Policy
from negotiation_core.statements import (
    PartialStatement,
    TwoChoiceResponse,
    apply_axis_removal,
    convert_statement_to_anchor,
    convert_two_choice_answer_to_anchor,
)
from negotiation_core.vocabulary import AXES, AnchorType, Side


def design_doc_example_policy(side: Side = "candidate") -> Policy:
    """design.md §2.3 の例そのもの(2 つの発言が矛盾なく書き込めることの確認に使う)。

    「年収 650 万以上なら、フル出社でも行く」(受けるアンカー)と
    「当直が月 4 回以上なら行かない」(受けないアンカー)。
    """
    accept = convert_statement_to_anchor(
        PartialStatement(polarity="accept", salary=650, remote_days=0), side
    )
    reject = convert_statement_to_anchor(
        PartialStatement(polarity="reject", night_duty=4), side
    )
    return Policy(side=side, accept_anchors=[accept], reject_anchors=[reject])


# --- DV-05 用: 単調な「本人のモデル」と代表的な面談(design.md §2.4・§5 手順 4) ---
#
# negotiation_core の Anchor/Policy/evaluate とは独立に、仮想的な本人が実際に
# 「行く/行かない」を判断するとしたらどう答えるかを、決定的な述語として直接書く。
# これを ground truth にして、二択・自由コメントへの回答を作り、変換後のポリシーが
# (a) 単調性(本人の判断を裏切らない)・(b) 健全性(受けられる、と言うときは本人も行く)
# を満たすかを DV-05 で確かめる。

# design.md §2.3・§5 の例の人物をそのまま条件にする:
# 「年収 650 万以上なら、フル出社でも行く」「当直が月 4 回以上なら行かない」。
# 触れていない軸(remote_days・review_months・training・side_job・start)には
# 好みを足さない(=どの値でも構わない)。これにより、単調性は自明に成り立つ
# (salary は高いほど、night_duty は少ないほど、行く判断に有利にしか働かない)。


def principal_decision(package: Package) -> bool:
    """design.md §2.3 の例の候補者が、package に対して「行く」と答えるなら True。

    negotiation_core の Anchor/Policy/evaluate を一切使わない、独立した判定関数
    (DV-05 の健全性チェックの基準にするため)。
    """
    return package.salary >= 650 and package.night_duty < 4


# 二択で見せる、代表的な組み合わせ(§5 手順 4「本人が言った年収の周辺の組み合わせ」の例)。
# 全 7 軸を指定した完全な組み合わせにしてある。principal_decision による行く/行かないの
# 内訳: 1=no_go(給与不足)、2=go、3=go、4=no_go(給与不足)、5=no_go(当直過多)、6=go、
# 7=no_go(給与不足)。
TWO_CHOICE_PACKAGES: list[dict] = [
    {
        "salary": 600,
        "remote_days": 0,
        "night_duty": 0,
        "review_months": 6,
        "training": "none",
        "side_job": "not_allowed",
        "start": "within_1_month",
    },
    {
        "salary": 650,
        "remote_days": 2,
        "night_duty": 2,
        "review_months": 6,
        "training": "available",
        "side_job": "allowed",
        "start": "within_3_months",
    },
    {
        "salary": 700,
        "remote_days": 5,
        "night_duty": 0,
        "review_months": 12,
        "training": "none",
        "side_job": "allowed",
        "start": "within_1_month",
    },
    {
        "salary": 600,
        "remote_days": 0,
        "night_duty": 4,
        "review_months": 6,
        "training": "available",
        "side_job": "not_allowed",
        "start": "within_6_months",
    },
    {
        "salary": 750,
        "remote_days": 3,
        "night_duty": 6,
        "review_months": 12,
        "training": "none",
        "side_job": "not_allowed",
        "start": "within_3_months",
    },
    {
        "salary": 650,
        "remote_days": 0,
        "night_duty": 0,
        "review_months": 12,
        "training": "available",
        "side_job": "allowed",
        "start": "within_6_months",
    },
    {
        "salary": 500,
        "remote_days": 1,
        "night_duty": 0,
        "review_months": 6,
        "training": "none",
        "side_job": "allowed",
        "start": "within_1_month",
    },
]

# 常に聞く自由コメント(design.md §2.3 の例の 2 文そのもの。軸を外していても、
# night_duty・salary を外していない限りそのまま残る)。
ALWAYS_FREE_COMMENTS: list[PartialStatement] = [
    PartialStatement(polarity="accept", salary=650, remote_days=0),
    PartialStatement(polarity="reject", night_duty=4),
]

# 軸を外したときに、その軸に触れる自由コメントが保存されないことを確かめるための、
# 軸ごとの追加コメント(night_duty は ALWAYS_FREE_COMMENTS の reject 文がすでに触れている)。
AXIS_TOUCHING_COMMENT: dict[str, PartialStatement] = {
    "remote_days": PartialStatement(polarity="accept", remote_days=3),
    "night_duty": PartialStatement(polarity="reject", night_duty=4),
    "review_months": PartialStatement(polarity="accept", review_months=6),
    "training": PartialStatement(polarity="accept", training="available"),
    "side_job": PartialStatement(polarity="accept", side_job="allowed"),
    "start": PartialStatement(polarity="accept", start="within_1_month"),
}

# design.md §2.1 の「離散軸」6 本(年収以外のすべて)。
DISCRETE_AXES_TO_REMOVE: tuple[str, ...] = (
    "remote_days",
    "night_duty",
    "review_months",
    "training",
    "side_job",
    "start",
)


def _principal_response(shown_values: dict, removed_axis: str | None) -> TwoChoiceResponse:
    """shown_values(二択で実際に見せた組み合わせ)に対する、本人のモデルの回答。

    区分軸を外して「どちらでも」と見せたときは、その軸のどの値でも行くときだけ
    「行く」と答える(design.md の指示どおり全称量化する)。順序のある軸を外して
    最悪値で見せたときは、その値のまま直接判定すればよい(すでに具体的な値のため)。
    principal_decision は候補者固定のモデルなので、side は取らない。
    """
    if removed_axis is not None and AXES[removed_axis].kind == "categorical":
        for candidate_value in AXES[removed_axis].grid:
            probe = dict(shown_values)
            probe[removed_axis] = candidate_value
            if not principal_decision(Package(**probe)):
                return "no_go"
        return "go"
    return "go" if principal_decision(Package(**shown_values)) else "no_go"


def build_policy_from_interview(side: Side, removed_axis: str | None) -> Policy:
    """principal_decision を本人として、TWO_CHOICE_PACKAGES と自由コメントに答えさせ、
    negotiation_core の変換規則(convert_two_choice_answer_to_anchor・
    convert_statement_to_anchor)でポリシーを組み立てる(DV-05 のテスト専用)。

    removed_axis が None なら、軸を外さない §5 手順 4 どおりの通常の二択。
    removed_axis を指定すると、§2.4 の軸を外す扱い(二択はその軸を中立にして見せ、
    「行かない」は保存しない。自由コメントもその軸に触れるものは保存しない)。
    """
    accept_anchors: list[Anchor] = []
    reject_anchors: list[Anchor] = []

    for base_values in TWO_CHOICE_PACKAGES:
        if removed_axis is not None:
            shown_values = apply_axis_removal(base_values, removed_axis, side)
        else:
            shown_values = dict(base_values)
        response = _principal_response(shown_values, removed_axis)
        result = convert_two_choice_answer_to_anchor(base_values, response, side, removed_axis)
        if result is not None:
            anchor, anchor_type = result
            (accept_anchors if anchor_type == "accept" else reject_anchors).append(anchor)

    comments = list(ALWAYS_FREE_COMMENTS)
    if removed_axis is not None:
        comments.append(AXIS_TOUCHING_COMMENT[removed_axis])
    for statement in comments:
        anchor = convert_statement_to_anchor(statement, side, removed_axis=removed_axis)
        if anchor is not None:
            anchor_type: AnchorType = statement.polarity
            (accept_anchors if anchor_type == "accept" else reject_anchors).append(anchor)

    return Policy(side=side, accept_anchors=accept_anchors, reject_anchors=reject_anchors)
