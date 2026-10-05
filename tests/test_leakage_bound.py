"""AC-05: the leakage bound of grid rounding (design.md §2.5, §12.1).

AC-05 が求める性質は「複数の求人・複数の交渉から適応的に攻める攻撃者(予算を無制限にしても)
で試しても、観測と矛盾しない境目の範囲が常に真の値のマス全体を含む」こと。

negotiation_core には(1a の時点では)状態機械・回数・予算・セッションの概念がなく、
evaluate() は純粋関数(同じ Policy と Package なら常に同じ判定を返す)。そのため、
「予算が無制限で、複数の求人・複数の交渉にまたがって適応的に攻める攻撃者」が観測しうる
情報の上限は、「グリッド上のあり得る全組み合わせ(18,000 通り)を 1 回ずつ問い合わせた
ときに得られる情報」と一致する(それ以上のことは、どんなに賢く適応的に聞いても、何度
交渉をやり直しても分からない)。よって、全組み合わせを網羅的に問い合わせる最強の攻撃者で
試す。これは、予算のある・複数交渉をまたぐ・適応的な、あらゆる弱い攻撃者を包含する。

テストは 2 段構えにしてある。
1. **全マス**(軽い): round_numeric_value だけを使い、マスの中の複数の生の値(含まれる側の
   端を含む)が同じ値に丸められ、含まれない側の端は別の値に丸められることを、全数値軸・
   全マス・両側・受ける/受けないの両方で確かめる(Package/Policy を作らないので軽い)。
2. **代表的なマスだけ**(重い): 1 マスの中の生の値どうしが、全 18,000 通りへの応答まで
   完全に区別不能であること(indistinguishability)と、隣のマスとは区別できること
   (tightness)を、軸ごとに最初・中央・最後のマスに絞って確かめる(実行時間を抑えるため)。
"""

import pytest

from negotiation_core.policy import Package, Policy, evaluate, iter_all_packages
from negotiation_core.rounding import round_numeric_value
from negotiation_core.statements import PartialStatement, convert_statement_to_anchor
from negotiation_core.vocabulary import AXES, NUMERIC_AXIS_KEYS, AnchorType, Side

SIDES: tuple[Side, ...] = ("candidate", "employer")
ANCHOR_TYPES: tuple[AnchorType, ...] = ("accept", "reject")

# 18,000 通りの Package は、このモジュール内の「重い」テストで使い回す(構築コストを避ける)。
ALL_PACKAGES: list[Package] = list(iter_all_packages())

_ALL_CELL_CASES = [
    (axis, side, anchor_type)
    for axis in NUMERIC_AXIS_KEYS
    for side in SIDES
    for anchor_type in ANCHOR_TYPES
]


def _sample_cell_indices(grid_length: int) -> list[int]:
    """全マス(0 〜 grid_length-2)のうち、最初・中央・最後の代表 3 点(またはそれ以下)を返す。"""
    if grid_length < 2:
        return []
    last_cell_index = grid_length - 2
    middle_cell_index = (grid_length - 1) // 2
    return sorted({0, middle_cell_index, last_cell_index})


_SAMPLED_CELL_CASES = [
    (axis, side, anchor_type, cell_index)
    for axis in NUMERIC_AXIS_KEYS
    for side in SIDES
    for anchor_type in ANCHOR_TYPES
    for cell_index in _sample_cell_indices(len(AXES[axis].grid))
]


def _policy_from_raw_value(axis: str, raw_value: float, anchor_type: AnchorType, side: Side) -> Policy:
    """axis だけに触れた発言から Policy を 1 つ作る(他の軸は §2.3 の穴埋め規則で中立に埋まる)。"""
    statement = PartialStatement(polarity=anchor_type, **{axis: raw_value})
    anchor = convert_statement_to_anchor(statement, side)
    if anchor_type == "accept":
        return Policy(side=side, accept_anchors=[anchor], reject_anchors=[])
    return Policy(side=side, accept_anchors=[], reject_anchors=[anchor])


def _verdict_vector(policy: Policy) -> tuple:
    """全 18,000 通りへの応答をまとめて返す。「予算無制限・適応的な攻撃者」が
    最終的に観測しうる情報のすべてを表す(丸めの前後で漏れる情報の上限を測るため)。
    """
    return tuple(evaluate(policy, package) for package in ALL_PACKAGES)


# --- 1. 全マス(軽い): round_numeric_value だけで確かめる ---


@pytest.mark.parametrize("axis,side,anchor_type", _ALL_CELL_CASES)
def test_every_cell_rounds_its_raw_values_to_one_boundary_and_not_the_other(axis, side, anchor_type):
    # AC-05: 全数値軸の全マスについて、候補者・求人の両側、受ける/受けないの両方で確かめる。
    # マスの中の複数の生の値(そのマスに含まれる側の端を含む)は同じ値に丸められ、
    # 含まれない側の端は別の値に丸められる。
    grid = AXES[axis].grid
    for lower_point, upper_point in zip(grid, grid[1:], strict=False):
        interior_values = [
            lower_point + fraction * (upper_point - lower_point) for fraction in (0.1, 0.5, 0.9)
        ]

        rounded_interior = {round_numeric_value(axis, v, side, anchor_type) for v in interior_values}
        assert len(rounded_interior) == 1, (
            f"axis={axis} side={side} anchor_type={anchor_type} "
            f"cell=({lower_point},{upper_point}): マスの中の生の値が別々の値に丸められた"
        )
        included_point = rounded_interior.pop()
        assert included_point in (lower_point, upper_point)

        # 含まれる側の端そのものも、同じ値に丸められる(丸めが冪等であることも合わせて確認)。
        assert round_numeric_value(axis, included_point, side, anchor_type) == included_point

        # 含まれない側の端は、別の値に丸められる(粒度がちょうど 1 マスであることの確認)。
        excluded_point = upper_point if included_point == lower_point else lower_point
        assert round_numeric_value(axis, excluded_point, side, anchor_type) != included_point


# --- 2. 代表的なマスだけ(重い): 全 18,000 通りへの応答まで確かめる ---


@pytest.mark.parametrize("axis,side,anchor_type,cell_index", _SAMPLED_CELL_CASES)
def test_raw_values_in_the_same_grid_cell_are_indistinguishable(axis, side, anchor_type, cell_index):
    # AC-05: 同じマス(隣接するグリッド点の間)にある生の値は、どんな問い合わせを
    # 何回重ねても(=全 18,000 通りを尋ねても)区別できない。
    grid = AXES[axis].grid
    lower_point, upper_point = grid[cell_index], grid[cell_index + 1]

    raw_values_in_same_cell = [
        lower_point + fraction * (upper_point - lower_point) for fraction in (0.2, 0.5, 0.8)
    ]

    vectors = [
        _verdict_vector(_policy_from_raw_value(axis, raw, anchor_type, side))
        for raw in raw_values_in_same_cell
    ]

    first_vector = vectors[0]
    for raw, vector in zip(raw_values_in_same_cell, vectors, strict=True):
        assert vector == first_vector, (
            f"axis={axis} side={side} anchor_type={anchor_type} cell_index={cell_index}: "
            f"raw={raw} が他の同じマスの生の値と異なる判定を生んだ(漏洩の上限を超えている)"
        )


@pytest.mark.parametrize("axis,side,anchor_type,cell_index", _SAMPLED_CELL_CASES)
def test_adjacent_grid_points_are_distinguishable(axis, side, anchor_type, cell_index):
    # AC-05: 「常に真の値のマス全体を含む」が空虚な(何も分からないだけの)主張ではなく、
    # ちょうど 1 マスぶんの粒度になっていることの確認。隣接するグリッド点どうしは、
    # 少なくとも 1 つの組み合わせで判定が変わる(=攻撃者はマスの境目までは特定できる)。
    grid = AXES[axis].grid
    lower_point, upper_point = grid[cell_index], grid[cell_index + 1]

    vector_lower = _verdict_vector(_policy_from_raw_value(axis, lower_point, anchor_type, side))
    vector_upper = _verdict_vector(_policy_from_raw_value(axis, upper_point, anchor_type, side))

    assert vector_lower != vector_upper, (
        f"axis={axis} side={side} anchor_type={anchor_type} cell_index={cell_index}: "
        "隣接するグリッド点どうしが区別できず、粒度が 1 マスより粗くなっている"
    )


@pytest.mark.parametrize("axis,side,anchor_type,cell_index", _SAMPLED_CELL_CASES)
def test_consistent_boundary_region_always_contains_the_true_cell(axis, side, anchor_type, cell_index):
    # AC-05: 予算無制限・複数交渉にまたがる適応的な攻撃者(=全 18,000 通りを問い合わせる
    # 攻撃者)が、観測と矛盾しない範囲を組み立てても、その範囲は常に真の値のマス全体を含む。
    # ここでは、真の生の値の丸め結果と同じ判定ベクトルを持つ「観測と矛盾しない」グリッド点の
    # 集合を求め、そこに真の値が実際に丸められる先(真のマスの境界点)が含まれることを示す。
    grid = AXES[axis].grid
    lower_point, upper_point = grid[cell_index], grid[cell_index + 1]
    true_raw_value = lower_point + 0.5 * (upper_point - lower_point)

    true_vector = _verdict_vector(_policy_from_raw_value(axis, true_raw_value, anchor_type, side))

    consistent_grid_points = [
        candidate
        for candidate in grid
        if _verdict_vector(_policy_from_raw_value(axis, candidate, anchor_type, side)) == true_vector
    ]

    rounded_point = round_numeric_value(axis, true_raw_value, side, anchor_type)
    assert rounded_point in consistent_grid_points
