"""DV-07: デモの分離(design.md §3.7・§12.2)。途中確認の追記は 1b-2。

2 つのデモを並行して動かしても、回数が互いに影響しないこと。実行の後、テンプレートが
変わっていないことを確かめる。
"""

from vault.api_models import MoveRequest
from vault.templates import get_template
from vault_helpers import demo_create_request, put_candidate_and_employer_templates, sample_package


def test_two_concurrent_demos_do_not_share_counters(store):
    # DV-07: 2 つのデモ(同じテンプレートから作った、別々の交渉)を交互に動かしても、
    # 互いの回数(評価・手数)に影響しない。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)

    demo_1 = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    demo_2 = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert demo_1.status == "created"
    assert demo_2.status == "created"
    assert demo_1.nid != demo_2.nid

    package = sample_package()

    # デモ 1 だけで check を 3 回。
    v1 = 0
    for _ in range(3):
        response = store.process_move(
            demo_1.nid, MoveRequest(expected_version=v1, side="candidate", move="check", package=package)
        )
        assert response.valid is True
        v1 = response.version

    # デモ 2 では、まだ何も操作していないので、評価回数は満額残っているはず。
    demo_2_view = store.get_view(demo_2.nid, "candidate")
    assert demo_2_view.budget.remaining_evaluations == 16

    # デモ 2 で check を 1 回。デモ 1 の残りには影響しない。
    v2 = 0
    response = store.process_move(
        demo_2.nid, MoveRequest(expected_version=v2, side="candidate", move="check", package=package)
    )
    assert response.valid is True
    v2 = response.version

    demo_1_view = store.get_view(demo_1.nid, "candidate")
    demo_2_view = store.get_view(demo_2.nid, "candidate")
    assert demo_1_view.budget.remaining_evaluations == 16 - 3
    assert demo_2_view.budget.remaining_evaluations == 16 - 1

    # イベント数もそれぞれ独立している。
    assert len(store.get_events(demo_1.nid, "candidate")) == 3
    assert len(store.get_events(demo_2.nid, "candidate")) == 1


def test_templates_are_unchanged_after_running_demos(store):
    # DV-07: 実行の後、テンプレートが変わっていない(交渉用コピーにしか書き込まない)。
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    candidate_template_before = get_template(store._db, candidate_template.template_id)
    employer_template_before = get_template(store._db, employer_template.template_id)

    demo = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert demo.status == "created"

    package = sample_package()
    v = 0
    response = store.process_move(
        demo.nid, MoveRequest(expected_version=v, side="candidate", move="propose", package=package)
    )
    v = response.version
    store.process_move(demo.nid, MoveRequest(expected_version=v, side="employer", move="accept"))

    candidate_template_after = get_template(store._db, candidate_template.template_id)
    employer_template_after = get_template(store._db, employer_template.template_id)

    assert candidate_template_after == candidate_template_before
    assert employer_template_after == employer_template_before
