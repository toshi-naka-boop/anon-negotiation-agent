"""DV-09: converting statements into anchors (design.md §2.3・§4.4, §12.2)。

DV-09 の 1 つ目(§2.3 の例の 2 文が矛盾なく書き込める)と 2 つ目(受けるアンカーの補完は
最も良い値になる)、3 つ目(架空人物への途中確認の回答は交渉用コピーにだけ入り、
テンプレートを変えない)を確かめる。
"""

from negotiation_core.policy import Policy, is_contradictory
from negotiation_core.statements import PartialStatement, convert_statement_to_anchor
from negotiation_core.vocabulary import best_value

from sample_data import design_doc_example_policy
from vault.api_models import MoveRequest, PrincipalAnswerRequest
from vault.models import EmployerRule
from vault.templates import get_template
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    needs_confirmation_policy,
    put_candidate_and_employer_templates,
    sample_package,
)


def test_design_doc_example_sentences_do_not_contradict():
    # DV-09 (1): 「年収650万以上なら、フル出社でも行く」と「当直が月4回以上なら行かない」が
    # 両方とも矛盾なく書き込める(design.md §2.3 の例そのもの)。
    accept = convert_statement_to_anchor(
        PartialStatement(polarity="accept", salary=650, remote_days=0), "candidate"
    )
    reject = convert_statement_to_anchor(
        PartialStatement(polarity="reject", night_duty=4), "candidate"
    )

    # design.md が示す変換結果と一致すること。
    assert accept.model_dump() == {
        "salary": 650,
        "remote_days": 0,
        "night_duty": 0,
        "review_months": 6,
        "training": "*",
        "side_job": "*",
        "start": "*",
    }
    assert reject.model_dump() == {
        "salary": 1500,
        "remote_days": 5,
        "night_duty": 4,
        "review_months": 6,
        "training": "*",
        "side_job": "*",
        "start": "*",
    }

    assert is_contradictory(accept, reject, "candidate") is False

    # Policy の構築(=書き込み)自体が矛盾チェックを兼ねる。例外が出なければ「書き込める」。
    policy = Policy(side="candidate", accept_anchors=[accept], reject_anchors=[reject])
    assert policy.accept_anchors == [accept]
    assert policy.reject_anchors == [reject]


def test_design_doc_example_policy_helper_matches_the_same_shape():
    # DV-09 (1): sample_data のヘルパー経由でも同じ結果になることの確認。
    policy = design_doc_example_policy("candidate")
    assert len(policy.accept_anchors) == 1
    assert len(policy.reject_anchors) == 1


def test_accept_anchor_fills_unmentioned_numeric_axes_with_best_value():
    # DV-09 (2): 受けるアンカーの補完(触れていない数値軸)は、依頼者にとって最も良い値になる。
    # salary だけに触れた「〜なら行く」発言を候補者側で変換する。
    statement = PartialStatement(polarity="accept", salary=650)
    anchor = convert_statement_to_anchor(statement, "candidate")

    # 候補者にとっての最も良い値: リモートは多いほど・当直は少ないほど・見直しは短いほど良い。
    assert anchor.salary == 650  # 触れた軸はそのまま(グリッド上なので丸めても不変)
    assert anchor.remote_days == best_value("remote_days", "candidate") == 5
    assert anchor.night_duty == best_value("night_duty", "candidate") == 0
    assert anchor.review_months == best_value("review_months", "candidate") == 6
    # 触れていない区分軸は * になる(数値軸の「最も良い値」とは別の埋め方)。
    assert anchor.training == "*"
    assert anchor.side_job == "*"
    assert anchor.start == "*"


def test_accept_anchor_fill_value_is_side_dependent():
    # DV-09 (2): 「最も良い値」は依頼者(側)によって向きが変わることの確認(求人側)。
    statement = PartialStatement(polarity="accept", salary=650)
    anchor = convert_statement_to_anchor(statement, "employer")

    # 求人側にとっての最も良い値: リモートは少ないほど・当直は多いほど・見直しは長いほど良い。
    assert anchor.remote_days == best_value("remote_days", "employer") == 0
    assert anchor.night_duty == best_value("night_duty", "employer") == 8
    assert anchor.review_months == best_value("review_months", "employer") == 12


def test_reject_anchor_also_fills_unmentioned_numeric_axes_with_best_value():
    # DV-09 (2) の裏取り: 受けないアンカーの補完も、design.md §2.3 の表どおり最も良い値になる
    # (受けないアンカーが不必要に広くならないようにするための穴埋め)。
    statement = PartialStatement(polarity="reject", night_duty=4)
    anchor = convert_statement_to_anchor(statement, "candidate")

    assert anchor.salary == best_value("salary", "candidate") == 1500
    assert anchor.remote_days == best_value("remote_days", "candidate") == 5
    assert anchor.review_months == best_value("review_months", "candidate") == 6


def test_fictional_candidates_principal_answer_only_enters_the_negotiation_copy(store):
    # DV-09 (3): 架空人物(デモの候補者)への途中確認の回答は、交渉用コピーにだけ入り、
    # テンプレートを変えない。
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db,
        candidate_policy=needs_confirmation_policy("candidate"),
        employer_rules=[EmployerRule(when={}, policy=accept_all_policy("employer"))],
    )
    template_before = get_template(store._db, candidate_template.template_id)

    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    nid = result.nid
    package = sample_package()

    ask_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="ask_principal", package=package)
    )
    assert ask_response.status == "awaiting_principal"

    answer_response = store.process_principal_answer(
        nid,
        PrincipalAnswerRequest(
            expected_version=ask_response.version, side="candidate", package=package, answer="accept"
        ),
    )
    assert answer_response.status == "active"

    # 交渉用コピーには受けるアンカーが増えている。
    doc = store._negotiation_ref(nid).get().to_dict()
    assert len(doc["snapshots"]["candidate"]["accept_anchors"]) == 1

    # テンプレートは変わっていない(交渉用コピーにしか書き込まれない)。
    template_after = get_template(store._db, candidate_template.template_id)
    assert template_after == template_before
