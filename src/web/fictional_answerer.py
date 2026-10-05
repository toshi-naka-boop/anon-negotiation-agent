"""架空人物の自動応答(design.md §4.4・§6.2)。フィクスチャ(fixtures/case*.toml)の生の条件・設定で、架空人物の代わりに答える。

- FixtureAnswerer: 1 つのケース(CaseFixture)の生の条件(vault.fixtures.RawConditions.accepts)で、途中確認(ask_principal)に
  「受ける／受けない」と答える。答えは、組み合わせと生の条件だけで決まる(状態を持たない)。回答の追記(交渉用コピーへの追加)と評価し直しは、
  金庫が行う(§4.4。テンプレートは変わらない)。
- FixtureCatalog: fixtures/ のケースをまとめて引く表(テンプレート ID・求人 ID から)。段階開示の自動応答(web.stages)も、これを使う。
- CatalogAnswerer: 本番の FictionalAnswerer(web.referee)。交渉 ID から、その交渉のフィクスチャ(候補者・求人・求人の規則を選ぶ属性帯)を
  引いて答える(複数のケースを振り分ける)。引けない交渉には、受けるとは言わない(「受けない」と答える。§2.3 の控えめな読み方と同じ)。

求人側の規則は、候補者の属性帯の条件(when。§2.2)で選ぶ。金庫が交渉の作成時に選んだ規則と同じものを選ぶため、属性帯は、金庫が持つ候補者の
属性帯(求人側の view の counterparty)を使う。デモの候補者なら、フィクスチャの候補者の属性帯と同じ。本物の候補者とフィクスチャの求人の
交渉でも、本物の候補者の属性帯で選ぶ(FixtureAnswerer は、フィクスチャの候補者の属性帯でしか選べない)。
"""

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from negotiation_core import CandidateAttributeBands, Package, Side

from vault.fixtures import (
    FIXTURES_DIRECTORY,
    CandidateFixture,
    CaseFixture,
    EmployerFixture,
    RawConditions,
    load_case_fixture,
)
from vault.models import PrincipalAnswerKind

_log = logging.getLogger(__name__)

_CASE_FILE = re.compile(r"case(\d+)\.toml")


class FixtureAnswerer:
    """FictionalAnswerer の中身。フィクスチャの生の条件で、途中確認に答える(§4.4)。"""

    def __init__(self, fixture: CaseFixture) -> None:
        self._fixture = fixture

    def _raw_conditions(self, side: Side) -> RawConditions | None:
        if side == "candidate":
            return self._fixture.candidate.raw
        rule = self._fixture.employer.rule_for(self._fixture.candidate.attribute_bands)
        return rule.raw if rule is not None else None

    async def __call__(self, *, nid: str, side: Side, package: Package) -> PrincipalAnswerKind:
        """package を「受ける」なら accept、受けないなら reject。nid は使わない(答えは生の条件だけで決まる)。

        どの規則にも当てはまらない求人は、金庫でも空のポリシーになる(すべて本人確認が必要)。生の条件がないので、
        受けるとは言わない(§2.3 の控えめな読み方と同じ)。
        """
        raw = self._raw_conditions(side)
        return "accept" if raw is not None and raw.accepts(package) else "reject"


class FixtureCatalog:
    """fixtures/ のケースの表。求人はテンプレート ID・求人 ID で、候補者はテンプレート ID で引く。"""

    def __init__(self, cases: Iterable[CaseFixture] = ()) -> None:
        self._employer_by_template: dict[str, EmployerFixture] = {}
        self._employer_by_job: dict[str, EmployerFixture] = {}
        self._candidate_by_template: dict[str, CandidateFixture] = {}
        for case in cases:
            self._register(self._employer_by_template, case.employer.template_id, case.employer, "employer template")
            self._register(self._employer_by_job, case.employer.job_id, case.employer, "employer job")
            self._register(self._candidate_by_template, case.candidate.template_id, case.candidate, "candidate template")

    @staticmethod
    def _register(table: dict, key: str, value, what: str) -> None:
        if key in table:
            raise ValueError(f"two fixtures have the same {what} id: {key!r}")
        table[key] = value

    @classmethod
    def load(cls, directory: Path = FIXTURES_DIRECTORY) -> "FixtureCatalog":
        """directory の case{N}.toml をすべて読む(番号の順)。形の違うファイルは ValueError(起動を拒否する)。"""
        matches = [_CASE_FILE.fullmatch(path.name) for path in directory.glob("case*.toml")]
        numbers = sorted(int(match.group(1)) for match in matches if match is not None)
        if not numbers:
            _log.warning("no fixture cases were found; fictional persons will not answer or respond")
        return cls(load_case_fixture(number, directory) for number in numbers)

    def employer_by_template(self, template_id: str) -> EmployerFixture | None:
        return self._employer_by_template.get(template_id)

    def employer_by_job(self, job_id: str) -> EmployerFixture | None:
        return self._employer_by_job.get(job_id)

    def candidate_by_template(self, template_id: str) -> CandidateFixture | None:
        return self._candidate_by_template.get(template_id)


class FixtureLookup(Protocol):
    """交渉 ID から、その交渉のフィクスチャを引く口(実装は web.stages.StageFlow。段の状態と金庫から引く)。"""

    async def employer_fixture(self, nid: str) -> EmployerFixture | None: ...

    async def candidate_fixture(self, nid: str) -> CandidateFixture | None: ...

    async def candidate_bands(self, nid: str) -> CandidateAttributeBands | None: ...


class CatalogAnswerer:
    """本番の FictionalAnswerer。交渉ごとに、該当するフィクスチャの生の条件で、途中確認に答える(複数のケースを振り分ける)。"""

    def __init__(self, lookup: FixtureLookup) -> None:
        self._lookup = lookup

    async def _raw_conditions(self, nid: str, side: Side) -> RawConditions | None:
        if side == "candidate":
            candidate = await self._lookup.candidate_fixture(nid)
            return candidate.raw if candidate is not None else None
        employer = await self._lookup.employer_fixture(nid)
        if employer is None:
            return None
        bands = await self._lookup.candidate_bands(nid)
        if bands is None:  # 候補者の属性帯が分からないときは、フィクスチャの候補者の帯で選ぶ(デモ)
            candidate = await self._lookup.candidate_fixture(nid)
            bands = candidate.attribute_bands if candidate is not None else None
        if bands is None:
            return None
        rule = employer.rule_for(bands)
        return rule.raw if rule is not None else None

    async def __call__(self, *, nid: str, side: Side, package: Package) -> PrincipalAnswerKind:
        """package を「受ける」なら accept。受けない・フィクスチャを引けない・どの規則にも当てはまらないなら reject。"""
        raw = await self._raw_conditions(nid, side)
        return "accept" if raw is not None and raw.accepts(package) else "reject"
