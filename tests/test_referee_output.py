"""AC-08: 金庫の最終記録(design.md §3.2・§3.6)。

見込みは最終記録にしか現れない。中身は双方とも同じ {likelihood, package} だけで、
理由を含まないことを確かめる。
"""

from vault.api_models import MoveRequest
from vault_helpers import demo_create_request, put_candidate_and_employer_templates, sample_package


def _create(store):
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def test_likelihood_appears_only_in_the_final_record(store):
    # AC-08: 見込みは最終記録にしか現れない。合意までの途中の記録(propose・offer_received)
    # には、result(見込みを含む)が一切現れないことを確かめる。
    nid = _create(store)
    package = sample_package()

    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    assert propose_response.valid is True

    accept_response = store.process_move(
        nid, MoveRequest(expected_version=propose_response.version, side="employer", move="accept")
    )
    assert accept_response.status == "judged"
    assert accept_response.end_reason == "agreed"

    for side in ("candidate", "employer"):
        events = store.get_events(nid, side)
        assert len(events) >= 1
        non_final_events = events[:-1]
        final_event = events[-1]

        for event in non_final_events:
            assert event.kind != "final_result"
            assert event.result is None  # 途中の記録に見込みが現れない

        assert final_event.kind == "final_result"
        assert final_event.result is not None
        assert final_event.result.likelihood in ("high", "medium")


def test_final_record_content_is_identical_on_both_sides_and_has_no_reason(store):
    # AC-08: 最終記録の中身は双方とも同じ {likelihood, package} だけで、理由を含まない。
    nid = _create(store)
    package = sample_package()

    propose_response = store.process_move(
        nid, MoveRequest(expected_version=0, side="candidate", move="propose", package=package)
    )
    accept_response = store.process_move(
        nid, MoveRequest(expected_version=propose_response.version, side="employer", move="accept")
    )
    assert accept_response.status == "judged"

    candidate_final = store.get_events(nid, "candidate")[-1]
    employer_final = store.get_events(nid, "employer")[-1]

    assert candidate_final.result == employer_final.result  # 双方まったく同じ内容
    assert candidate_final.result.package == package

    # {likelihood, package} だけで、理由(reason)などほかの項目を持たない。
    assert set(candidate_final.result.model_dump().keys()) == {"likelihood", "package"}
    assert candidate_final.reason is None
    assert employer_final.reason is None


def test_final_record_when_not_agreed_has_none_likelihood_and_no_package(store):
    # AC-08 の裏付け: 合意しなかった場合も、最終記録は双方に {likelihood: "none", package: None}
    # だけで、理由(なぜ終わったか)を含まない。
    nid = _create(store)
    end_response = store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="end"))
    assert end_response.status == "judged"

    for side in ("candidate", "employer"):
        final_event = store.get_events(nid, side)[-1]
        assert final_event.kind == "final_result"
        assert final_event.result.likelihood == "none"
        assert final_event.result.package is None
        assert final_event.reason is None
