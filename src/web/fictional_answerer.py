"""架空人物の途中確認への自動回答(design.md §4.4)。

web.referee.FictionalAnswerer の中身。架空人物(デモの候補者・フィクスチャの求人)が途中確認(ask_principal)で
聞かれたとき、フィクスチャの生の条件(vault.fixtures.RawConditions)で、その組み合わせを「受ける／受けない」と答える。
答えは、組み合わせと生の条件だけで決まる(状態を持たない)。回答の追記(交渉用コピーへの追加)と評価し直しは、金庫が行う
(§4.4。テンプレートは変わらない)。

求人側の規則は、候補者の属性帯の条件(when。§2.2)で選ぶ。ここでは、フィクスチャの候補者(デモの候補者)の属性帯で選ぶ。
金庫が交渉の作成時に選んだ規則と同じになるのは、デモ・攻撃の交渉(候補者がフィクスチャ)のときだけ。本物の候補者と
フィクスチャの求人の交渉では、金庫が持つ候補者の属性帯(求人側の view の counterparty)で選ぶ必要がある
(求人の規則が属性帯で分かれているケースを、本物の候補者で動かすとき。④の自動応答)。
"""

from negotiation_core import Package, Side

from vault.fixtures import CaseFixture, RawConditions
from vault.models import PrincipalAnswerKind


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
