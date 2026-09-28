"""design.md §3.6: 最終判定。

広さを全 18,000 通りで数えること、T_high で「高」「中」を分けること、どちらかが
「受けられる」でなければ「なし」になることを確かめる。Firestore を使わない純粋関数の
テストなので、他のテストより先に、かつ高速に検証できる。
"""

from negotiation_core import Anchor, Policy, Verdict, evaluate, iter_all_packages

from vault.judgment import judge
from vault_helpers import accept_all_policy, reject_all_policy, sample_package

_ALL_PACKAGES = list(iter_all_packages())
_TOTAL_PACKAGE_COUNT = 25 * 6 * 5 * 2 * 2 * 2 * 3  # design.md §2.1 の 18,000 通り


def test_total_package_count_is_18000():
    # §3.6: 「広さ」は全 18,000 通りのうちの数え上げ。前提となる総数を確認しておく。
    assert _TOTAL_PACKAGE_COUNT == 18000
    assert len(_ALL_PACKAGES) == 18000


def test_no_agreement_when_either_side_cannot_accept():
    # §3.6: どちらかが「受けられる」でなければ「なし」(package も持たない)。
    package = sample_package()

    result = judge(accept_all_policy("candidate"), reject_all_policy("employer"), package, t_high=10)
    assert result.likelihood == "none"
    assert result.package is None

    result = judge(reject_all_policy("candidate"), accept_all_policy("employer"), package, t_high=10)
    assert result.likelihood == "none"
    assert result.package is None


def test_breadth_counts_over_all_18000_combinations_and_is_high_when_everything_is_acceptable():
    # §3.6: 両者が何でも受けるなら、広さは 18,000(全件)になり、T_high(10)以上なので「高」。
    package = sample_package()
    result = judge(accept_all_policy("candidate"), accept_all_policy("employer"), package, t_high=10)
    assert result.likelihood == "high"
    assert result.package == package

    # 独立に数えても 18,000 件全部が両立(両者とも ACCEPTABLE)であることを確かめる。
    candidate_policy = accept_all_policy("candidate")
    employer_policy = accept_all_policy("employer")
    breadth = sum(
        1
        for pkg in _ALL_PACKAGES
        if evaluate(candidate_policy, pkg) is Verdict.ACCEPTABLE
        and evaluate(employer_policy, pkg) is Verdict.ACCEPTABLE
    )
    assert breadth == 18000


def test_breadth_of_exactly_ten_sits_on_the_t_high_boundary():
    # §3.6: T_high で「高」「中」を分ける境目そのものを確かめる。求人側のアンカーを、
    # 2 軸(当直・昇給見直し)だけ無条件にし、残り 5 軸を求人にとって最良の 1 点に
    # 固定すると、当直のグリッド(5 通り)×見直しのグリッド(2 通り)= 10 通りだけが
    # 両立する(手計算した値を、独立な数え上げでも確かめる)。
    narrow_anchor = Anchor(
        salary=300,  # 求人にとって最良(lower_is_better なので最小)
        remote_days=0,  # 求人にとって最良(lower_is_better なので最小)
        night_duty=0,  # 求人にとって最悪の値 → night_duty については無条件
        review_months=6,  # 求人にとって最悪の値 → review_months については無条件
        training="none",
        side_job="not_allowed",
        start="within_1_month",
    )
    employer_policy = Policy(side="employer", accept_anchors=[narrow_anchor], reject_anchors=[])
    candidate_policy = accept_all_policy("candidate")

    breadth = sum(
        1
        for pkg in _ALL_PACKAGES
        if evaluate(candidate_policy, pkg) is Verdict.ACCEPTABLE
        and evaluate(employer_policy, pkg) is Verdict.ACCEPTABLE
    )
    assert breadth == 10  # 手計算(night_duty 5 通り × review_months 2 通り)どおり

    agreed_package = sample_package(
        salary=300,
        remote_days=0,
        night_duty=4,
        review_months=12,
        training="none",
        side_job="not_allowed",
        start="within_1_month",
    )
    assert evaluate(employer_policy, agreed_package) is Verdict.ACCEPTABLE  # 合意した組み合わせ自体は受けられる

    # 広さ 10 は T_high=10 以上なので「高」。
    result_high = judge(candidate_policy, employer_policy, agreed_package, t_high=10)
    assert result_high.likelihood == "high"

    # 同じ広さ 10 でも、閾値を 11 に上げれば「中」になる(境目の分け方そのものの確認)。
    result_medium = judge(candidate_policy, employer_policy, agreed_package, t_high=11)
    assert result_medium.likelihood == "medium"


def test_result_never_reveals_a_reason():
    # AC-08 と同じ性質だが、判定関数そのものが理由を持たないことも §3.6 の対象として確認する。
    package = sample_package()
    result = judge(accept_all_policy("candidate"), accept_all_policy("employer"), package, t_high=10)
    assert set(result.model_dump().keys()) == {"likelihood", "package"}
