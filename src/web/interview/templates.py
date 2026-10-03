"""面談の設問テンプレート fixtures/interview_templates.toml の読み込みと検証(design.md §5・§2.4・§2.6。未決事項 U-01)。

文面は、すべて暫定(U-01)。文面を差し替えるだけで済むように、コードには文面を書かず、このファイルが読む表に置く。
読み込みのときに、設計の約束と食い違う表(軸のキー・グリッドの値の食い違い、組の数が足りない、都道府県の抜けなど)は
ValueError で拒否する(起動を止める)。

- 二択の組(pairs)の雛形: 年収は「本人の年収を寄せたグリッド点から何段上か下か」(salary_steps)で書く。区分軸は、書かなければ
  「どちらでも」。年収と 1 つの軸のトレードオフの組(traded_axes が空でない)と、年収だけを動かす組(空)がある。
  軸を外すと、トレードオフの軸がすべて外れた組は出さない(web.interview.choices)。年収だけの組が最少の組数(5)以上あるので、
  すべての軸を外しても最少の組数に届く。
- 外した軸の見せ方の文(§2.4): 順序のある軸は最も悪い値({value})、順序のない区分軸は「どちらでも」。
"""

import tomllib
from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from negotiation_core import ATTRIBUTE_BANDS, AXES, AXIS_KEYS
from negotiation_core.vocabulary import SideJobValue, StartValue, TrainingValue

from web.interview.config import MIN_CHOICE_PAIRS

# src/web/interview/templates.py から見て、プロジェクト直下の fixtures/ を指す。
FIXTURES_DIRECTORY = Path(__file__).resolve().parents[3] / "fixtures"
TEMPLATES_PATH = FIXTURES_DIRECTORY / "interview_templates.toml"

# 年収以外の軸(外せる軸。離散軸。§2.1・§5 の手順 3)
DISCRETE_AXIS_KEYS = tuple(axis for axis in AXIS_KEYS if AXES[axis].discrete)


class _FileModel(BaseModel):
    """表の型の共通設定(知らない項目は拒否する)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Notice(_FileModel):
    """面談の入口の注記(§5 の末尾・§1.2・§6.3)。auto_delete の {days} は、データを自動で消すまでの日数。"""

    title: str
    vertex_ai: str
    global_endpoint: str
    server_memory: str
    auto_delete: str


class FreeComment(_FileModel):
    prompt: str


class ReasonForLeaving(_FileModel):
    prompt: str


class AxesTexts(_FileModel):
    notice: str
    remove_label: str


class AnswerLabels(_FileModel):
    go: str
    no_go: str
    undecided: str


class TwoChoiceTexts(_FileModel):
    intro: str
    closing: str
    not_saved: str
    answers: AnswerLabels
    removed_axis_phrases: dict[str, str]


class PairOption(_FileModel):
    """二択の選択肢 1 つの雛形。数値軸は全部、区分軸は書いたものだけ(書かなければ「どちらでも」)。"""

    salary_steps: int = Field(ge=-10, le=10)
    remote_days: int
    night_duty: int
    review_months: int
    training: TrainingValue | None = None
    side_job: SideJobValue | None = None
    start: StartValue | None = None


class PairTemplate(_FileModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]
    a: PairOption
    b: PairOption

    @property
    def traded_axes(self) -> frozenset[str]:
        """a と b で値が違う、年収以外の軸(トレードオフの相手の軸)。空なら、年収だけを動かす組。"""
        return frozenset(axis for axis in AXIS_KEYS if axis != "salary" and getattr(self.a, axis) != getattr(self.b, axis))


class BlocklistTexts(_FileModel):
    prompt: str
    note: str


class Labels(_FileModel):
    experience_band: dict[str, str]
    region_block: dict[str, str]
    job_category: dict[str, str]


class InterviewTemplates(_FileModel):
    provisional: bool
    notice: Notice
    salary_questions: list[str]
    free_comment: FreeComment
    reason_for_leaving: ReasonForLeaving
    axes: AxesTexts
    two_choice: TwoChoiceTexts
    pairs: list[PairTemplate]
    blocklist: BlocklistTexts
    labels: Labels
    region_prefectures: dict[str, list[str]]

    @model_validator(mode="after")
    def _agrees_with_the_vocabulary(self) -> "InterviewTemplates":
        if len(self.salary_questions) != 3:
            raise ValueError("salary_questions must have exactly 3 questions (§5 の 2)")
        if set(self.two_choice.removed_axis_phrases) != set(DISCRETE_AXIS_KEYS):
            raise ValueError(f"removed_axis_phrases must have exactly the discrete axes {DISCRETE_AXIS_KEYS}")
        for axis, phrase in self.two_choice.removed_axis_phrases.items():
            if AXES[axis].kind == "numeric" and "{value}" not in phrase:
                raise ValueError(f"removed_axis_phrases.{axis} must contain {{value}} (the worst value is put there)")
        ids = [pair.id for pair in self.pairs]
        if len(set(ids)) != len(ids):
            raise ValueError("pair ids must be distinct")
        for pair in self.pairs:
            for option in (pair.a, pair.b):
                for axis in ("remote_days", "night_duty", "review_months"):
                    if getattr(option, axis) not in AXES[axis].grid:
                        raise ValueError(f"pair {pair.id}: {axis}={getattr(option, axis)!r} is not on the grid")
            if pair.a == pair.b:
                raise ValueError(f"pair {pair.id}: the two options are identical")
        if sum(1 for pair in self.pairs if not pair.traded_axes) < MIN_CHOICE_PAIRS:
            raise ValueError(
                f"at least {MIN_CHOICE_PAIRS} salary-only pairs are needed, so that removing every axis still leaves "
                "the minimum number of questions (§5 の 4)"
            )
        for band, definition in ATTRIBUTE_BANDS.items():
            if set(getattr(self.labels, band)) != set(definition.grid):
                raise ValueError(f"labels.{band} must have exactly the keys of the {band} grid")
        if set(self.region_prefectures) != set(ATTRIBUTE_BANDS["region_block"].grid):
            raise ValueError("region_prefectures must have exactly the keys of the region_block grid")
        prefectures = [name for names in self.region_prefectures.values() for name in names]
        if len(prefectures) != 47 or len(set(prefectures)) != 47:
            raise ValueError("region_prefectures must list each of the 47 prefectures exactly once")
        return self


def load_interview_templates(path: Path = TEMPLATES_PATH) -> InterviewTemplates:
    """fixtures/interview_templates.toml を読み込んで検証する。形の違い・語彙との食い違いは ValueError。"""
    with path.open("rb") as f:
        return InterviewTemplates.model_validate(tomllib.load(f))


@cache
def default_templates() -> InterviewTemplates:
    """既定の場所のテンプレート(一度だけ読む)。"""
    return load_interview_templates()
