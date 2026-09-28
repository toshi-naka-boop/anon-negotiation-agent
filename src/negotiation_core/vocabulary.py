"""Vocabulary, grid and attribute bands (design.md §2.1, §2.6).

語彙（軸）・グリッド・候補者の属性帯を、config/params.toml から読み込んで型付きで公開する。
値そのもの（暫定値）は設定ファイル側に置き、ここでは軸の集合や向きの意味づけだけを扱う。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Side = Literal["candidate", "employer"]
Direction = Literal["higher_is_better", "lower_is_better", "unordered"]
AxisKind = Literal["numeric", "categorical"]
AnchorType = Literal["accept", "reject"]

# negotiation_core/vocabulary.py から見て、プロジェクト直下の config/params.toml を指す。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"

# 語彙そのもの（7 軸の存在とキー名）は design.md §2.1 の表に固定された構造なのでコードに書く。
# グリッドの中身・向き・表示名だけを設定ファイルから読む（暫定値、U-02・U-03）。
AXIS_KEYS: tuple[str, ...] = (
    "salary",
    "remote_days",
    "night_duty",
    "review_months",
    "training",
    "side_job",
    "start",
)
ATTRIBUTE_BAND_KEYS: tuple[str, ...] = (
    "experience_band",
    "region_block",
    "job_category",
)


@dataclass(frozen=True)
class AxisDefinition:
    """1 軸ぶんの語彙定義(キー・型・グリッド・向き)。"""

    key: str
    label: str
    kind: AxisKind
    discrete: bool
    grid: tuple
    candidate_direction: Direction
    employer_direction: Direction

    def direction_for(self, side: Side) -> Direction:
        """side にとっての、この軸の向き(高い/低いどちらが良いか)を返す。"""
        return self.candidate_direction if side == "candidate" else self.employer_direction


@dataclass(frozen=True)
class AttributeBandDefinition:
    """候補者の属性帯 1 種ぶんの定義(§2.6)。"""

    key: str
    label: str
    grid: tuple[str, ...]


def _load_raw_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _build_axes(raw: dict) -> dict[str, AxisDefinition]:
    raw_axes = raw.get("axes", {})
    missing = [k for k in AXIS_KEYS if k not in raw_axes]
    if missing:
        raise ValueError(f"config/params.toml is missing axes: {missing}")
    axes: dict[str, AxisDefinition] = {}
    for key in AXIS_KEYS:
        entry = raw_axes[key]
        axes[key] = AxisDefinition(
            key=key,
            label=entry["label"],
            kind=entry["kind"],
            discrete=entry["discrete"],
            grid=tuple(entry["grid"]),
            candidate_direction=entry["candidate_direction"],
            employer_direction=entry["employer_direction"],
        )
    return axes


def _build_attribute_bands(raw: dict) -> dict[str, AttributeBandDefinition]:
    raw_bands = raw.get("attribute_bands", {})
    missing = [k for k in ATTRIBUTE_BAND_KEYS if k not in raw_bands]
    if missing:
        raise ValueError(f"config/params.toml is missing attribute_bands: {missing}")
    bands: dict[str, AttributeBandDefinition] = {}
    for key in ATTRIBUTE_BAND_KEYS:
        entry = raw_bands[key]
        bands[key] = AttributeBandDefinition(key=key, label=entry["label"], grid=tuple(entry["grid"]))
    return bands


_RAW_CONFIG = _load_raw_config(_CONFIG_PATH)

AXES: dict[str, AxisDefinition] = _build_axes(_RAW_CONFIG)
ATTRIBUTE_BANDS: dict[str, AttributeBandDefinition] = _build_attribute_bands(_RAW_CONFIG)

NUMERIC_AXIS_KEYS: tuple[str, ...] = tuple(k for k in AXIS_KEYS if AXES[k].kind == "numeric")
CATEGORICAL_AXIS_KEYS: tuple[str, ...] = tuple(k for k in AXIS_KEYS if AXES[k].kind == "categorical")

# 軸ごとのグリッド値を列挙型として公開する(Move の output_schema 等が列挙値で
# グリッド外を出させないため。§2.7)。Literal[tuple(...)] は Literal[(v1, v2, ...)] と同義。
SalaryValue = Literal[tuple(AXES["salary"].grid)]
RemoteDaysValue = Literal[tuple(AXES["remote_days"].grid)]
NightDutyValue = Literal[tuple(AXES["night_duty"].grid)]
ReviewMonthsValue = Literal[tuple(AXES["review_months"].grid)]
TrainingValue = Literal[tuple(AXES["training"].grid)]
SideJobValue = Literal[tuple(AXES["side_job"].grid)]
StartValue = Literal[tuple(AXES["start"].grid)]

ExperienceBandValue = Literal[tuple(ATTRIBUTE_BANDS["experience_band"].grid)]
RegionBlockValue = Literal[tuple(ATTRIBUTE_BANDS["region_block"].grid)]
JobCategoryValue = Literal[tuple(ATTRIBUTE_BANDS["job_category"].grid)]


def goodness_rank(axis: str, value, side: Side) -> int:
    """axis の value を、side にとっての「良さ」の順位に変換する(大きいほど良い)。

    区分軸(順序なし)には順位がないので呼び出せない。数値軸専用。
    """
    definition = AXES[axis]
    if definition.kind != "numeric":
        raise ValueError(f"axis {axis!r} is categorical; it has no goodness order")
    index = definition.grid.index(value)
    direction = definition.direction_for(side)
    if direction == "higher_is_better":
        return index
    return -index  # lower_is_better


def best_value(axis: str, side: Side):
    """axis において side にとって最も良い値を返す(数値軸専用。§2.3 の穴埋め・§2.5 の丸めで使う)。"""
    definition = AXES[axis]
    if definition.kind != "numeric":
        raise ValueError(f"axis {axis!r} is categorical; it has no best value")
    direction = definition.direction_for(side)
    return definition.grid[-1] if direction == "higher_is_better" else definition.grid[0]


def worst_value(axis: str, side: Side):
    """axis において side にとって最も悪い値を返す(数値軸専用。§2.4 の軸を外す処理で使う)。"""
    definition = AXES[axis]
    if definition.kind != "numeric":
        raise ValueError(f"axis {axis!r} is categorical; it has no worst value")
    direction = definition.direction_for(side)
    return definition.grid[0] if direction == "higher_is_better" else definition.grid[-1]
