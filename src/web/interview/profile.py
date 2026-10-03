"""プロフィール(経験年数・地域・職種)を、候補者の属性帯に変換する(design.md §5 の 1・§2.6)。

候補者は正確な値をフォームに入れる。web はそれをその場で帯に変換し、正確な値は捨てる(この module の関数は、帯だけを返し、
正確な値を保存しない。呼び出し側も保存しない)。帯のグリッドは config/params.toml の [attribute_bands.*]、帯の境目は
[web.interview] の experience_band_upper_bounds、都道府県と地域ブロックの対応は fixtures/interview_templates.toml。
"""

from collections.abc import Mapping, Sequence

from negotiation_core import ATTRIBUTE_BANDS, CandidateAttributeBands

# 都道府県の入力で、末尾の「都・道・府・県」を省いて書かれたとき(例: 東京)に、補って探す順。
_PREFECTURE_SUFFIXES = ("都", "道", "府", "県")


class ProfileError(ValueError):
    """プロフィールの入力が、帯に変換できない。code は API の detail に使う(入力の値は含めない)。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def experience_band(years: float, upper_bounds: Sequence[float]) -> str:
    """経験年数(年)を、帯に変換する。upper_bounds は、最後の帯を除く各帯の上の境目(この値未満がその帯)。

    例: 境目が [3, 5, 10] なら、3 年未満は under_3y、3 年ちょうどは 3_to_5y、10 年以上は 10y_plus。
    """
    grid = ATTRIBUTE_BANDS["experience_band"].grid
    for band, bound in zip(grid, upper_bounds, strict=False):
        if years < bound:
            return band
    return grid[-1]


def region_block(prefecture: str, region_prefectures: Mapping[str, Sequence[str]]) -> str:
    """都道府県の名前を、地域ブロックに変換する。「東京」のように末尾を省いた書き方も受ける。知らない名前は ProfileError。"""
    name = prefecture.strip()
    candidates = [name, *(name + suffix for suffix in _PREFECTURE_SUFFIXES)]
    for candidate in candidates:
        for block, prefectures in region_prefectures.items():
            if candidate in prefectures:
                return block
    raise ProfileError("unknown_region")


def job_category(value: str) -> str:
    """職種の大分類(キー)を確かめる。知らない値は ProfileError。"""
    if value not in ATTRIBUTE_BANDS["job_category"].grid:
        raise ProfileError("unknown_job_category")
    return value


def profile_to_bands(
    *,
    experience_years: float,
    prefecture: str,
    job: str,
    upper_bounds: Sequence[float],
    region_prefectures: Mapping[str, Sequence[str]],
) -> CandidateAttributeBands:
    """プロフィールの正確な値を、属性帯に変換する(§2.6)。正確な値は、戻り値に含まれない。"""
    return CandidateAttributeBands(
        experience_band=experience_band(experience_years, upper_bounds),
        region_block=region_block(prefecture, region_prefectures),
        job_category=job_category(job),
    )
