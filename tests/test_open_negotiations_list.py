"""見回り用の一覧 GET /v1/negotiations?open=true&cursor=(design.md §3.3・§4.1。台帳 L9-2)。

judged でない交渉を、nid の順にページ単位で返す。カーソルの位置は nid > cursor で決めるので、ページの間にカーソルの交渉が
終わって一覧から消えても、先頭から返し直さない(返し直すと、見回りが同じ交渉を重ねて処理する)。
"""

from vault.api_models import ControlRequest
from vault_helpers import demo_create_request, put_candidate_and_employer_templates


def _create(store) -> str:
    candidate_template, employer_template = put_candidate_and_employer_templates(store._db)
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert result.status == "created"
    return result.nid


def _cancel(store, nid: str) -> None:
    store.control(nid, ControlRequest(side="candidate", action="cancel"))


def test_the_cursor_continues_after_itself_even_when_its_negotiation_left_the_list_between_pages(store):
    # L9-2: 1 ページ目の後に、カーソルの交渉が終わって一覧から消えても、2 ページ目は、カーソルの次から続く
    # (以前は、カーソルの交渉が一覧になければ 0 から返し直し、1 ページ目の交渉をもう一度返した)。
    nids = sorted(_create(store) for _ in range(5))

    first = store.list_open_negotiations(page_size=2)
    assert [item.nid for item in first.items] == nids[:2]
    assert first.next_cursor == nids[1]

    _cancel(store, nids[1])  # ページの間に、カーソルの交渉が終わる

    second = store.list_open_negotiations(cursor=first.next_cursor, page_size=2)
    assert [item.nid for item in second.items] == nids[2:4]
    assert second.next_cursor == nids[3]
    third = store.list_open_negotiations(cursor=second.next_cursor, page_size=2)
    assert [item.nid for item in third.items] == nids[4:]
    assert third.next_cursor is None


def test_pages_cover_every_open_negotiation_exactly_once_in_nid_order(store):
    # 終わった交渉は載らない。ページをたどると、進行中の交渉が、nid の順に 1 回ずつ現れる(取りこぼしも重複もない)。
    nids = sorted(_create(store) for _ in range(5))
    _cancel(store, nids[2])

    collected: list[str] = []
    cursor = None
    while True:
        page = store.list_open_negotiations(cursor=cursor, page_size=2)
        collected.extend(item.nid for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert collected == [nid for nid in nids if nid != nids[2]]
