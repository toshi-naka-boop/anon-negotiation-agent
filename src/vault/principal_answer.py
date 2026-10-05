"""途中確認の回答をアンカーに直し、ポリシーへ追記する(design.md §4.4)。

P(ask_principal で確認された組み合わせ)はすでに全軸の値が決まっているので、§2.3 の
部分的な発言をアンカーに直す規則は要らない(design.md §4.4 の 1)。ここに置くのは、
Firestore に一切触れない純粋な関数だけ(vault/judgment.py・vault/stop_rule.py と同じ
立て付け)。追記先(本体・交渉用コピーのどちらに書くか)の使い分けと、削除中の依頼者の
拒否は store.py 側(Firestore の読み書き・トランザクション)が行う。
"""

from negotiation_core import (
    Anchor,
    CATEGORICAL_AXIS_KEYS,
    Package,
    Policy,
    Side,
    best_value,
    is_contradictory,
    worst_value,
)

from vault.models import PrincipalAnswerKind


def anchor_from_package(package: Package) -> Anchor:
    """P をそのままアンカーにする(§4.4 の 1: 全軸が決まっているので §2.3 の規則は不要)。"""
    return Anchor(**package.model_dump(mode="python"))


def is_neutral_for_axis(anchor: Anchor, axis: str, side: Side, answer: PrincipalAnswerKind) -> bool:
    """anchor が axis について中立か(§2.2 の定義)。

    受けるアンカーなら axis の値が依頼者にとって最も悪い値、受けないアンカーなら
    最も良い値であるか、* なら中立(その軸を制約していない)。区分軸は、P から作った
    anchor では常に具体的な値(* ではない)になるので、中立にはならない。
    """
    value = getattr(anchor, axis)
    if value == "*":
        return True
    if axis in CATEGORICAL_AXIS_KEYS:
        return False
    reference = worst_value(axis, side) if answer == "accept" else best_value(axis, side)
    return value == reference


def is_neutral_for_all_removed_axes(
    anchor: Anchor, removed_axes: list[str], side: Side, answer: PrincipalAnswerKind
) -> bool:
    """anchor が、removed_axes に挙げたすべての軸について中立か(§2.4「中立でない回答」)。

    removed_axes が空なら(何も外していなければ)常に True。
    """
    return all(is_neutral_for_axis(anchor, axis, side, answer) for axis in removed_axes)


def append_anchor_if_consistent(
    policy: Policy, anchor: Anchor, answer: PrincipalAnswerKind
) -> tuple[Policy, bool]:
    """矛盾検査を通れば anchor を追記した新しい Policy を返す(§4.4: 矛盾したら追記しない)。

    「受ける」なら受けるアンカー、「受けない」なら受けないアンカーとして追記する。
    追記先の既存の反対側のアンカー集合との矛盾(negotiation_core.is_contradictory と
    同じ判定)だけを確かめる。既存のアンカーどうしはすでに書き込み時点で矛盾していない
    はずなので、これで Policy 自身の検証(contradiction チェック)にも必ず通る。
    戻り値の 2 つ目は、実際に追記したかどうか(呼び出し側のログ・テスト用)。
    """
    if answer == "accept":
        contradicts = any(is_contradictory(anchor, reject, policy.side) for reject in policy.reject_anchors)
        if contradicts:
            return policy, False
        updated = Policy(
            side=policy.side,
            accept_anchors=[*policy.accept_anchors, anchor],
            reject_anchors=policy.reject_anchors,
        )
        return updated, True

    contradicts = any(is_contradictory(accept, anchor, policy.side) for accept in policy.accept_anchors)
    if contradicts:
        return policy, False
    updated = Policy(
        side=policy.side,
        accept_anchors=policy.accept_anchors,
        reject_anchors=[*policy.reject_anchors, anchor],
    )
    return updated, True
