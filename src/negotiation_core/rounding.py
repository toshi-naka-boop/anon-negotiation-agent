"""Rounding raw anchor values onto the shared grid (design.md §2.5).

受けるアンカーは依頼者にとって良い側、受けないアンカーは悪い側の、最も近いグリッド点に丸める。
グリッド上の組み合わせについては、丸めの前後で判定が 1 件も変わらない(§2.5 の性質)。
"""

from negotiation_core.policy import Anchor
from negotiation_core.vocabulary import (
    AXES,
    CATEGORICAL_AXIS_KEYS,
    NUMERIC_AXIS_KEYS,
    AnchorType,
    Side,
)


def round_numeric_value(axis: str, raw_value: float, side: Side, anchor_type: AnchorType):
    """raw_value を axis のグリッド上の点に丸める(§2.5)。

    受けるアンカー(accept)は依頼者にとって良い側、受けないアンカー(reject)は悪い側の、
    最も近いグリッド点に寄せる。raw_value がちょうどグリッド上にあるときは、その点のまま返す
    (丸めが判定を変えないという性質の前提)。
    """
    definition = AXES[axis]
    if definition.kind != "numeric":
        raise ValueError(f"axis {axis!r} is categorical; rounding does not apply")
    grid = definition.grid
    direction = definition.direction_for(side)

    # 「良い側」への丸めか、「悪い側」への丸めか。
    round_toward_good_side = anchor_type == "accept"

    if direction == "higher_is_better":
        # 良い側 = 値が大きい方向。良い側に寄せるなら切り上げ、悪い側に寄せるなら切り下げ。
        round_up = round_toward_good_side
    else:
        # lower_is_better: 良い側 = 値が小さい方向。良い側に寄せるなら切り下げ、悪い側なら切り上げ。
        round_up = not round_toward_good_side

    if round_up:
        candidates = [g for g in grid if g >= raw_value]
        if not candidates:
            raise ValueError(f"{axis}: raw value {raw_value!r} is above the grid maximum {grid[-1]!r}")
        return min(candidates)
    candidates = [g for g in grid if g <= raw_value]
    if not candidates:
        raise ValueError(f"{axis}: raw value {raw_value!r} is below the grid minimum {grid[0]!r}")
    return max(candidates)


def round_anchor(raw_values: dict, anchor_type: AnchorType, side: Side) -> Anchor:
    """raw_values(軸ごとの生の値。数値軸はグリッド外もあり得る)を丸めて Anchor を作る(§2.5)。

    区分軸はもともとグリッドが値の集合そのものなので丸めない(そのまま渡す。* も可)。
    """
    rounded: dict = {}
    for axis in NUMERIC_AXIS_KEYS:
        rounded[axis] = round_numeric_value(axis, raw_values[axis], side, anchor_type)
    for axis in CATEGORICAL_AXIS_KEYS:
        rounded[axis] = raw_values[axis]
    return Anchor(**rounded)
