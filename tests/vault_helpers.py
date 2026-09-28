"""vault のテストで共通に使う、テスト用テンプレート・ポリシー・組み合わせの組み立て。

`test_` で始まらないので pytest には収集されない(tests/sample_data.py と同じ扱い)。
ここで作るテンプレートはすべてテスト専用(design.md の指示どおり、本番のフィクスチャ
(U-09)はここでは作らない)。
"""

import uuid

from negotiation_core import (
    Anchor,
    CandidateAttributeBands,
    Package,
    Policy,
    Side,
    best_value,
    worst_value,
)

from vault.api_models import (
    CandidateParticipantRequest,
    CreateNegotiationRequest,
    EmployerParticipantRequest,
    PutPolicyRequest,
)
from vault.models import CandidateTemplate, EmployerRule, EmployerTemplate
from vault.templates import put_template


def new_id(prefix: str) -> str:
    """テスト用の一意な ID(衝突を避けるためだけの乱数)。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def sample_package(**overrides) -> Package:
    """グリッド上の代表的な組み合わせ(上書きで軸を変えられる)。"""
    values = {
        "salary": 700,
        "remote_days": 2,
        "night_duty": 2,
        "review_months": 6,
        "training": "available",
        "side_job": "allowed",
        "start": "within_1_month",
    }
    values.update(overrides)
    return Package(**values)


def _wildcard_anchor(numeric_value_for) -> Anchor:
    return Anchor(
        salary=numeric_value_for("salary"),
        remote_days=numeric_value_for("remote_days"),
        night_duty=numeric_value_for("night_duty"),
        review_months=numeric_value_for("review_months"),
        training="*",
        side_job="*",
        start="*",
    )


def accept_all_policy(side: Side) -> Policy:
    """どんな組み合わせでも ACCEPTABLE になるポリシー(全軸について中立な最悪値のアンカー)。"""
    anchor = _wildcard_anchor(lambda axis: worst_value(axis, side))
    return Policy(side=side, accept_anchors=[anchor], reject_anchors=[])


def reject_all_policy(side: Side) -> Policy:
    """どんな組み合わせでも NOT_ACCEPTABLE になるポリシー。"""
    anchor = _wildcard_anchor(lambda axis: best_value(axis, side))
    return Policy(side=side, accept_anchors=[], reject_anchors=[anchor])


def needs_confirmation_policy(side: Side) -> Policy:
    """アンカーを 1 つも持たない、常に NEEDS_CONFIRMATION になるポリシー。"""
    return Policy(side=side, accept_anchors=[], reject_anchors=[])


def default_attribute_bands() -> CandidateAttributeBands:
    return CandidateAttributeBands(
        experience_band="3_to_5y", region_block="kanto", job_category="it_web"
    )


def make_candidate_template(
    template_id: str | None = None,
    policy: Policy | None = None,
    attribute_bands: CandidateAttributeBands | None = None,
) -> CandidateTemplate:
    return CandidateTemplate(
        template_id=template_id or new_id("cand-tmpl"),
        policy=policy if policy is not None else accept_all_policy("candidate"),
        attribute_bands=attribute_bands or default_attribute_bands(),
    )


def make_employer_template(
    template_id: str | None = None,
    company_id: str | None = None,
    job_id: str | None = None,
    rules: list[EmployerRule] | None = None,
) -> EmployerTemplate:
    if rules is None:
        rules = [EmployerRule(when={}, policy=accept_all_policy("employer"))]
    template_id = template_id or new_id("emp-tmpl")
    return EmployerTemplate(
        template_id=template_id,
        company_id=company_id or new_id("company"),
        job_id=job_id or template_id,
        rules=rules,
    )


def put_candidate_and_employer_templates(
    db,
    *,
    candidate_policy: Policy | None = None,
    employer_rules: list[EmployerRule] | None = None,
    attribute_bands: CandidateAttributeBands | None = None,
) -> tuple[CandidateTemplate, EmployerTemplate]:
    """よく使う組(架空の候補者 1・架空の求人 1)を作って vault-db に置く。"""
    candidate_template = make_candidate_template(policy=candidate_policy, attribute_bands=attribute_bands)
    employer_template = make_employer_template(rules=employer_rules)
    put_template(db, candidate_template)
    put_template(db, employer_template)
    return candidate_template, employer_template


def demo_create_request(
    candidate_template_id: str,
    employer_template_id: str,
    *,
    mode: str = "demo",
    request_id: str | None = None,
) -> CreateNegotiationRequest:
    """架空人物どうしの交渉の作成リクエスト(mode は既定で "demo")。"""
    return CreateNegotiationRequest(
        request_id=request_id or new_id("req"),
        mode=mode,
        candidate=CandidateParticipantRequest(is_fictional=True, template_id=candidate_template_id),
        employer=EmployerParticipantRequest(template_id=employer_template_id),
    )


def live_create_request(
    candidate_principal_id: str,
    employer_template_id: str,
    *,
    request_id: str | None = None,
) -> CreateNegotiationRequest:
    """本物の候補者 対 架空の求人の交渉の作成リクエスト(mode="live")。

    台帳 I-2 により、本物の候補者の属性帯はこの要求には含めない(金庫が
    principals/{pid} から読む)。呼び出し側は、先に put_candidate_policy(store, pid, ...)
    でポリシーと帯を保存しておくこと。
    """
    return CreateNegotiationRequest(
        request_id=request_id or new_id("req"),
        mode="live",
        candidate=CandidateParticipantRequest(is_fictional=False, principal_id=candidate_principal_id),
        employer=EmployerParticipantRequest(template_id=employer_template_id),
    )


def put_candidate_policy(
    store,
    pid: str,
    *,
    policy: Policy | None = None,
    attribute_bands: CandidateAttributeBands | None = None,
    removed_axes: list[str] | None = None,
) -> None:
    """本物の候補者のポリシーと属性帯を vault-db に置く(テスト用の既定値つき)。

    live_create_request で交渉を作る前に、これで principals/{pid} を用意しておく
    (台帳 I-2: 属性帯は作成の要求ではなく、ここで保存した値を金庫が読む)。
    """
    store.put_policy(
        pid,
        PutPolicyRequest(
            policy=policy if policy is not None else accept_all_policy("candidate"),
            removed_axes=removed_axes or [],
            attribute_bands=attribute_bands if attribute_bands is not None else default_attribute_bands(),
        ),
    )


def put_candidate_policy_without_bands(
    store, pid: str, *, policy: Policy | None = None, removed_axes: list[str] | None = None
) -> None:
    """属性帯を保存しない版(DV-12: 帯のない候補者は交渉を作れないことを確かめる用)。"""
    store.put_policy(
        pid,
        PutPolicyRequest(
            policy=policy if policy is not None else accept_all_policy("candidate"),
            removed_axes=removed_axes or [],
        ),
    )
