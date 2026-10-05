"""§2.5: rounding preserves judgments of on-grid combinations (design.md §2.5).

design.md §2.5 の「性質」を確かめる。
- グリッド上の組み合わせについては、丸めの前と後で判定が 1 件も変わらない。
  (「高いほど良い軸でグリッド点 g について『g≥620⇔g≥650』が成り立つ」という
  設計書の説明を、round_numeric_value の実装から独立した数値比較で検算する。)
- ちょうどグリッド上にある値は、丸めても変わらない(丸めが冪等)。
"""

import pytest

from negotiation_core.rounding import round_numeric_value
from negotiation_core.vocabulary import AXES, NUMERIC_AXIS_KEYS


def _judgment_against_raw_threshold(
    grid_point: float, raw_threshold: float, direction: str, anchor_type: str
) -> bool:
    """grid_point が raw_threshold というアンカーに該当するかを、グリッドに頼らず直接
    数値だけで求める(round_numeric_value とは独立な検算のため、あえて実装を再利用しない)。

    受けるアンカーなら「満たす」(grid_point の良さ >= raw_threshold の良さ)、
    受けないアンカーなら「含まれる」(grid_point の良さ <= raw_threshold の良さ。§2.2)。
    """
    if anchor_type == "accept":
        if direction == "higher_is_better":
            return grid_point >= raw_threshold
        return grid_point <= raw_threshold  # lower_is_better
    # reject: 含まれる(良さが raw_threshold 以下)
    if direction == "higher_is_better":
        return grid_point <= raw_threshold
    return grid_point >= raw_threshold  # lower_is_better


@pytest.mark.parametrize("axis", NUMERIC_AXIS_KEYS)
@pytest.mark.parametrize("side", ["candidate", "employer"])
@pytest.mark.parametrize("anchor_type", ["accept", "reject"])
def test_rounding_never_changes_the_judgment_of_an_on_grid_combination(axis, side, anchor_type):
    # §2.5 性質: グリッド上の組み合わせについては、丸めの前と後で判定が 1 件も変わらない。
    grid = AXES[axis].grid
    direction = AXES[axis].direction_for(side)
    middle_index = len(grid) // 2
    lower_point, upper_point = grid[middle_index - 1], grid[middle_index]
    raw_threshold = lower_point + 0.5 * (upper_point - lower_point)  # グリッドの外(マスの中)

    rounded_threshold = round_numeric_value(axis, raw_threshold, side, anchor_type)

    for candidate_grid_point in grid:
        before = _judgment_against_raw_threshold(candidate_grid_point, raw_threshold, direction, anchor_type)
        after = _judgment_against_raw_threshold(candidate_grid_point, rounded_threshold, direction, anchor_type)
        assert before == after, (
            f"axis={axis} side={side} anchor_type={anchor_type} "
            f"candidate={candidate_grid_point}: 丸め前後で判定が変わった"
        )


@pytest.mark.parametrize("axis", NUMERIC_AXIS_KEYS)
@pytest.mark.parametrize("side", ["candidate", "employer"])
@pytest.mark.parametrize("anchor_type", ["accept", "reject"])
def test_rounding_a_value_already_on_the_grid_is_a_no_op(axis, side, anchor_type):
    # §2.5 性質: ちょうどグリッド上にある値は、丸めても変わらない
    # (「提案値はグリッド上に限る」ので交渉の結果は丸めで変わらない、という前提の基礎)。
    grid = AXES[axis].grid
    for grid_point in grid:
        assert round_numeric_value(axis, grid_point, side, anchor_type) == grid_point
