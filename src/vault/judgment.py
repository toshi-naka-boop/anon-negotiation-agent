"""最終判定(design.md §3.6)。accept と同じトランザクションから呼ばれる純粋関数。

両者のコピーで、合意した組み合わせを評価し直す。どちらも「受けられる」なら、
全 18,000 通りのうち両者が「受けられる」組み合わせの数(広さ)を数え、閾値 T_high で
「高」「中」を分ける。どちらかが「受けられる」でなければ「なし」(package も持たない)。
"""

from negotiation_core import Package, Policy, Verdict, evaluate, iter_all_packages

from vault.models import NegotiationResult

_ALL_PACKAGES: tuple[Package, ...] = tuple(iter_all_packages())


def judge(
    candidate_policy: Policy, employer_policy: Policy, package: Package, t_high: int
) -> NegotiationResult:
    """§3.6 の判定。合意した package を両者のポリシーで評価し直してから広さを数える。"""
    if (
        evaluate(candidate_policy, package) is not Verdict.ACCEPTABLE
        or evaluate(employer_policy, package) is not Verdict.ACCEPTABLE
    ):
        return NegotiationResult(likelihood="none", package=None)

    breadth = sum(
        1
        for candidate_pkg in _ALL_PACKAGES
        if evaluate(candidate_policy, candidate_pkg) is Verdict.ACCEPTABLE
        and evaluate(employer_policy, candidate_pkg) is Verdict.ACCEPTABLE
    )
    likelihood = "high" if breadth >= t_high else "medium"
    return NegotiationResult(likelihood=likelihood, package=package)
