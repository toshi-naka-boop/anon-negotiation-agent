"""架空人物のテンプレート(design.md §3.7)。

テンプレートは vault-db に読み取り専用で置き、交渉では変更されない。交渉の作成時に、
テンプレートから交渉用コピー(snapshots)を直接写す(複製の依頼者は作らない)。
本番のフィクスチャ(U-09)は 1b-1 では作らない。テスト用のテンプレートは tests/ 側に置く。
"""

from google.cloud import firestore

from negotiation_core import CandidateAttributeBands, Policy

from vault.models import CandidateTemplate, EmployerTemplate, Template
from vault.serialization import model_from_firestore, model_to_firestore

TEMPLATES_COLLECTION = "templates"


def put_template(db: firestore.Client, template: Template) -> None:
    """テンプレートを vault-db に置く関数(§3.7)。同じ template_id ならそのまま置き換える。"""
    db.collection(TEMPLATES_COLLECTION).document(template.template_id).set(model_to_firestore(template))


def get_template(db: firestore.Client, template_id: str) -> Template | None:
    """template_id のテンプレートを読む(なければ None)。"""
    snap = db.collection(TEMPLATES_COLLECTION).document(template_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    if data.get("side") == "candidate":
        return model_from_firestore(CandidateTemplate, data)
    return model_from_firestore(EmployerTemplate, data)


def _rule_matches(rule_when: dict[str, str], attribute_bands: CandidateAttributeBands) -> bool:
    """when の全キーについて、"*" か候補者の値と一致すれば、そのルールは合う(§2.2)。

    when に現れないキーは、そのキーについて無条件(どの値にもマッチ)として扱う。
    """
    bands = attribute_bands.model_dump(mode="python")
    for key, expected in rule_when.items():
        if expected == "*":
            continue
        if bands.get(key) != expected:
            return False
    return True


def resolve_employer_policy(template: EmployerTemplate, attribute_bands: CandidateAttributeBands) -> Policy:
    """求人側テンプレートのルールを候補者の属性帯に当てはめ、1 つの Policy にする(§2.2)。

    上から順に見て、最初に条件が合ったルールを使う。どのルールにも合わなければ、
    アンカーを持たない空のポリシーにする(evaluate は常に NEEDS_CONFIRMATION を返す。
    「どのルールにも合わなければ、すべて『本人確認が必要』になる」に対応)。
    """
    for rule in template.rules:
        if _rule_matches(rule.when, attribute_bands):
            return rule.policy
    return Policy(side="employer", accept_anchors=[], reject_anchors=[])
