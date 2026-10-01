"""ケース別のフィクスチャ(design.md §8.4)の読み込みと、金庫のテンプレート(§3.7)への登録。

フィクスチャは `fixtures/case{N}.toml` に、データ(表)として置く。1 ファイルが 1 つのケースで、候補者と求人を持つ。
- 候補者: 生の条件・属性帯・職務要約・連絡先(架空)。丸め済みポリシーは、読み込むときに生の条件から作る。
- 求人: 企業名・公開求人・自動応答の設定・規則(属性帯の条件 when と、その生の裁量)。丸め済みポリシーは、規則ごとに
  読み込むときに作る。
生の条件は Python の関数ではなく表(軸の値の組 → 年収の境目)で持つので、同じ表から次の 2 つが作れる。
- 丸め済みポリシー(`build_rounded_policy`): 金庫のテンプレートに置く。
- 途中確認への自動回答(`RawConditions.accepts`。§4.4): web が、生の条件で「受ける／受けない」を答える。

丸め済みポリシーをファイルに持たず、読み込み時に作るのは、生の条件と丸め済みポリシーが食い違うことを、作り方の
上でなくすため(丸めた結果を人が手で写すと、片方だけ直したときに食い違う)。

丸め済みポリシーの作り方は、7 巡目のシミュレーション(design/anon-negotiation-agent/reviews/round-7-sim/exp6.py の build)と
同じ考え方。軸の値の組(列)ごとに、年収の境目でちょうど「受ける」と「受けない」に分かれるアンカーを 1 組ずつ置く。
- 候補者(下限): 境目以上は受ける、境目より 1 万円でも低ければ受けない。
- 求人(上限): 境目以下は受ける、境目より 1 万円でも高ければ受けない。
- 表に当てはまらない列は、年収がいくらでも受けない(受けないアンカーを、年収はその側の最良の値で置く)。
丸めは §2.5 の規則(negotiation_core.round_anchor)をそのまま使う。さらに、ほかのアンカーに完全に含まれるアンカーは除く
(判定は変わらない。除かないと、列の数だけアンカーが増え、最終判定(全 18,000 通りの数え上げ。§3.6)が重くなる)。
"""

import itertools
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from google.cloud import firestore
from pydantic import BaseModel, ConfigDict

from negotiation_core import (
    AXES,
    NUMERIC_AXIS_KEYS,
    Anchor,
    CandidateAttributeBands,
    JobCategoryInfo,
    Package,
    Policy,
    Side,
    best_value,
    goodness_rank,
    round_anchor,
)
from negotiation_core.vocabulary import AnchorType, JobCategoryValue

from vault.models import CandidateTemplate, EmployerRule, EmployerTemplate
from vault.templates import _rule_matches, put_template

# src/vault/fixtures.py から見て、プロジェクト直下の fixtures/ を指す。
FIXTURES_DIRECTORY = Path(__file__).resolve().parents[2] / "fixtures"

# 年収の境目を決める軸(表の列を作る軸)。生の条件の表は、この 3 軸の値の組ごとに年収の境目を持つ。
_BOUND_AXES = ("remote_days", "night_duty", "review_months")


# --- ファイルの形(読み込みで検証する) ---


class _FileModel(BaseModel):
    """フィクスチャのファイルの型の共通設定(知らない項目は拒否する)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ContactInfo(_FileModel):
    """候補者の連絡先(架空。段 2 で開示する。§6.2)。"""

    name: str
    email: str


class PublicJob(_FileModel):
    """公開求人(求人の一覧に出す情報。§6.1)。confidential が True なら、企業名を段 1 まで伏せる。"""

    title: str
    summary: str
    confidential: bool
    job_category: JobCategoryValue


class AutoResponse(_FileModel):
    """架空の求人の自動応答の設定(「会う」「承認」を自動で押すか。§6.2・P-2)。"""

    meet: bool
    approve: bool


class _Row(_FileModel):
    """生の条件の表の 1 行。軸の値を並べた組の集まりに、同じ年収の境目を持たせる。書かない軸は、どの値でも同じ。"""

    remote_days: list[int] | None = None
    night_duty: list[int] | None = None
    review_months: list[int] | None = None


class _CandidateRow(_Row):
    min_salary: int  # この年収(万円)以上なら受ける、未満なら受けない


class _EmployerRow(_Row):
    max_salary: int  # この年収(万円)以下なら受ける、超えるなら受けない


class _CandidateFile(_FileModel):
    template_id: str
    attribute_bands: CandidateAttributeBands
    job_summary: str
    contact: ContactInfo
    raw_conditions: list[_CandidateRow]


class _EmployerRuleFile(_FileModel):
    when: dict[str, str]
    raw_conditions: list[_EmployerRow]


class _EmployerFile(_FileModel):
    template_id: str
    company_id: str
    job_id: str
    company_name: str
    public_job: PublicJob
    auto_response: AutoResponse
    rules: list[_EmployerRuleFile]


class _CaseFile(_FileModel):
    case: int
    candidate: _CandidateFile
    employer: _EmployerFile


# --- 生の条件と、丸め済みポリシー ---


@dataclass(frozen=True)
class RawConditions:
    """生の条件(丸める前の、本人の年収の境目。§4.4・§8.4)。

    bounds は、(リモート日数, 当直回数, 昇給見直し月数)の組ごとの年収の境目(万円)。含まれない組は、年収がいくらでも
    受けない。区分軸(研修・副業・入職時期)は、条件に入っていない(どちらでも)。
    """

    side: Side
    bounds: Mapping[tuple[int, int, int], int]

    def accepts(self, package: Package) -> bool:
        """package を、この人物が「受ける」か(受けないなら False)。途中確認への自動回答(§4.4)の元。"""
        bound = self.bounds.get((package.remote_days, package.night_duty, package.review_months))
        if bound is None:
            return False
        return package.salary >= bound if self.side == "candidate" else package.salary <= bound


def _expand_rows(rows: list, bound_of: Callable) -> dict[tuple[int, int, int], int]:
    """表の行を、(軸の値の組 → 年収の境目)に展開する。グリッド外の値と、同じ組の重複は拒否する。"""
    bounds: dict[tuple[int, int, int], int] = {}
    for row in rows:
        values_by_axis = []
        for axis in _BOUND_AXES:
            grid = AXES[axis].grid
            values = getattr(row, axis)
            values = list(grid) if values is None else values  # 書かない軸は、どの値でも同じ
            off_grid = [value for value in values if value not in grid]
            if off_grid:
                raise ValueError(f"{axis}: the values {off_grid} are not on the grid")
            values_by_axis.append(values)
        for column in itertools.product(*values_by_axis):
            if column in bounds:
                raise ValueError(f"two rows of the raw conditions cover the same combination {column}")
            bounds[column] = bound_of(row)
    return bounds


def _anchor(salary: int, column: tuple[int, int, int], kind: AnchorType, side: Side) -> Anchor:
    """列(リモート日数・当直回数・昇給見直し月数)と年収から、区分軸は「どちらでも」のアンカーを丸めて作る(§2.5)。"""
    raw_values = dict(zip(_BOUND_AXES, column, strict=True))
    raw_values.update(salary=salary, training="*", side_job="*", start="*")
    return round_anchor(raw_values, kind, side)


def _prune(anchors: list[Anchor], side: Side, kind: AnchorType) -> list[Anchor]:
    """ほかのアンカーに完全に含まれるアンカーを除く(判定は変わらない)。同じアンカーは、先のものを残す。

    受けるアンカーは、数値軸がすべて自分以上に良い組み合わせを受ける。別のアンカーがそれより同じかゆるければ、
    そちらがこのアンカーの範囲を覆う。受けないアンカーは、数値軸がすべて自分以下の良さの組み合わせを含む。
    ここで作るアンカーは、区分軸がすべて「どちらでも」なので、数値軸だけを見ればよい。
    """
    ranks = [[goodness_rank(axis, getattr(anchor, axis), side) for axis in NUMERIC_AXIS_KEYS] for anchor in anchors]

    def covers(wide: int, narrow: int) -> bool:
        """wide の範囲が、narrow の範囲を含むか。"""
        pairs = zip(ranks[wide], ranks[narrow], strict=True)
        if kind == "accept":
            return all(narrow_rank >= wide_rank for wide_rank, narrow_rank in pairs)
        return all(narrow_rank <= wide_rank for wide_rank, narrow_rank in pairs)

    return [
        anchor
        for narrow, anchor in enumerate(anchors)
        if not any(
            covers(wide, narrow) and (wide < narrow or not covers(narrow, wide))
            for wide in range(len(anchors))
            if wide != narrow
        )
    ]


def build_rounded_policy(raw: RawConditions) -> Policy:
    """生の条件から、丸め済みポリシーを作る(§2.5。7 巡目のシミュレーションの build と同じ考え方)。

    すべての列(リモート日数・当直回数・昇給見直し月数の組)について、年収の境目の「受ける」側と「受けない」側の
    アンカーを丸めて置く。作ったポリシーは、グリッド上のどの組み合わせでも、生の条件と同じ答えを返す。矛盾は、
    Policy の検証(書き込み時点の矛盾検査。§2.2)で拒否される。
    """
    side = raw.side
    salary_grid = AXES["salary"].grid
    outside_step = -1 if side == "candidate" else 1  # 「受けない」側は、境目の 1 万円外(候補者は下、求人は上)
    accept: list[Anchor] = []
    reject: list[Anchor] = []
    for column in itertools.product(*(AXES[axis].grid for axis in _BOUND_AXES)):
        bound = raw.bounds.get(column)
        if bound is None:
            # 年収がいくらでも受けない列。年収は、その側の最良の値(これ以下はすべて含む)で置く。
            reject.append(_anchor(best_value("salary", side), column, "reject", side))
            continue
        accept.append(_anchor(bound, column, "accept", side))
        outside = bound + outside_step
        if salary_grid[0] <= outside <= salary_grid[-1]:  # グリッドの端なら、その外側はない
            reject.append(_anchor(outside, column, "reject", side))
    return Policy(
        side=side,
        accept_anchors=_prune(accept, side, "accept"),
        reject_anchors=_prune(reject, side, "reject"),
    )


# --- 読み込んだフィクスチャ ---


@dataclass(frozen=True)
class CandidateFixture:
    """架空の候補者(§8.4: 生の条件・丸め済みポリシー・属性帯・職務要約・連絡先)。"""

    template_id: str
    attribute_bands: CandidateAttributeBands
    job_summary: str
    contact: ContactInfo
    raw: RawConditions
    policy: Policy  # 丸め済み


@dataclass(frozen=True)
class EmployerRuleFixture:
    """求人の規則 1 つ。属性帯の条件(when。§2.2)と、その生の裁量・丸め済みポリシー。"""

    when: dict[str, str]
    raw: RawConditions
    policy: Policy  # 丸め済み


@dataclass(frozen=True)
class EmployerFixture:
    """架空の求人(§8.4: 企業名・公開求人・生の裁量・丸め済みポリシー(属性帯の条件付き)・自動応答の設定)。"""

    template_id: str
    company_id: str
    job_id: str
    company_name: str
    public_job: PublicJob
    auto_response: AutoResponse
    rules: tuple[EmployerRuleFixture, ...]

    def rule_for(self, attribute_bands: CandidateAttributeBands) -> EmployerRuleFixture | None:
        """候補者の属性帯に当てはまる規則(上から順に見て、最初に合うもの。金庫のテンプレートの解決と同じ。§2.2)。"""
        for rule in self.rules:
            if _rule_matches(rule.when, attribute_bands):
                return rule
        return None


@dataclass(frozen=True)
class CaseFixture:
    """1 つのケースのフィクスチャ(候補者と求人)。"""

    case: int
    candidate: CandidateFixture
    employer: EmployerFixture

    def templates(self) -> tuple[CandidateTemplate, EmployerTemplate]:
        """金庫のテンプレート(§3.7)の形にする。生の条件は、金庫には渡さない(丸め済みポリシーだけ)。"""
        candidate = CandidateTemplate(
            template_id=self.candidate.template_id,
            policy=self.candidate.policy,
            attribute_bands=self.candidate.attribute_bands,
        )
        employer = EmployerTemplate(
            template_id=self.employer.template_id,
            company_id=self.employer.company_id,
            job_id=self.employer.job_id,
            rules=[EmployerRule(when=rule.when, policy=rule.policy) for rule in self.employer.rules],
            job_category_info=JobCategoryInfo(job_category=self.employer.public_job.job_category),
        )
        return candidate, employer


def load_case_fixture(case: int, directory: Path = FIXTURES_DIRECTORY) -> CaseFixture:
    """`fixtures/case{case}.toml` を読み込む。形の違い・グリッド外の値・同じ組の重複・ポリシーの矛盾は ValueError。"""
    with (directory / f"case{case}.toml").open("rb") as f:
        parsed = _CaseFile.model_validate(tomllib.load(f))
    if parsed.case != case:
        raise ValueError(f"fixtures/case{case}.toml declares case = {parsed.case}")

    candidate_raw = RawConditions("candidate", _expand_rows(parsed.candidate.raw_conditions, lambda row: row.min_salary))
    candidate = CandidateFixture(
        template_id=parsed.candidate.template_id,
        attribute_bands=parsed.candidate.attribute_bands,
        job_summary=parsed.candidate.job_summary,
        contact=parsed.candidate.contact,
        raw=candidate_raw,
        policy=build_rounded_policy(candidate_raw),
    )

    rules = []
    for rule in parsed.employer.rules:
        raw = RawConditions("employer", _expand_rows(rule.raw_conditions, lambda row: row.max_salary))
        rules.append(EmployerRuleFixture(when=rule.when, raw=raw, policy=build_rounded_policy(raw)))
    employer = EmployerFixture(
        template_id=parsed.employer.template_id,
        company_id=parsed.employer.company_id,
        job_id=parsed.employer.job_id,
        company_name=parsed.employer.company_name,
        public_job=parsed.employer.public_job,
        auto_response=parsed.employer.auto_response,
        rules=tuple(rules),
    )
    return CaseFixture(case=case, candidate=candidate, employer=employer)


def put_fixture_templates(db: firestore.Client, fixture: CaseFixture) -> None:
    """フィクスチャの候補者と求人を、金庫のテンプレート(§3.7)に置く。同じ template_id なら置き換える。

    デモ・攻撃・架空の求人の交渉は、作成のときに、ここで置いたテンプレートから交渉用コピーを写す。
    """
    candidate_template, employer_template = fixture.templates()
    put_template(db, candidate_template)
    put_template(db, employer_template)
