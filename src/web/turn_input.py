"""TurnInput の組み立て(design.md §2.7・§4.1 の 1)。

手番の側の金庫の view と、イベント列のその側の見え方だけから TurnInput を作る純粋な関数。
web はイベントを写さないので(§3.2)、履歴はこの関数に渡されたイベント列からだけ作る。
そのため、レフェリーを途中で止めて作り直しても、履歴に欠けも重複も生じない(DV-10)。

作らないもの: view の version(手を登録するためだけに使い、LLM にも画面にも渡さない。§3.3)、
相手側の評価・確認結果・残り回数(view にもイベント列の見え方にもそもそも含まれない)。

設計書が決めていない読み方(§2.7)は、最も保守的な読み方で次のようにした。
- history の result は「その組み合わせについての、自分側の金庫の評価」で統一する。
  イベントに評価が記録されているもの(確認手・相手の提案の受領)はその値を使う。記録されて
  いないものは、金庫が有効な手として受け付けた事実から決まる値を使う: 自分の提案は
  「受けられる」(ガードを通ったため)、自分の途中確認は「本人確認が必要」(そうでなければ
  無効手になるため)、断る手・断られた手は、その元になった提案の評価。
- history に入れるのは「手」だけ。無効手(何を打とうとしたかは記録されない。理由は last_error に出る)、
  途中確認の回答(手ではない。評価し直した結果は pending_offer・last_check に出る)、一時停止・再開、
  最終記録は入れない。
- own_move_number は、自分側が打った手(確認手・提案・断る・途中確認・無効手)の数。
  手数の上限に数えるもの(有効な確認手を除く)とは別の、単純な数え方にした。
"""

from typing import Literal

from negotiation_core import (
    AttackerTurnInput,
    HistoryEntry,
    LastErrorReason,
    MoveType,
    Package,
    Side,
    TurnInput,
    Verdict,
)

from vault.api_models import EventViewItem, NegotiationViewResponse

# 自分側が打った手として数えるイベントの種類。
_OWN_MOVE_KINDS = frozenset({"check", "propose", "reject", "ask_principal", "invalid"})
# 「直前の手」を探すときに見るイベントの種類(自分の手と、相手の手)。
_MOVE_LIKE_KINDS = _OWN_MOVE_KINDS | {"offer_received", "offer_rejected"}


def build_history(events: list[EventViewItem]) -> list[HistoryEntry]:
    """イベント列のその側の見え方から、TurnInput.history を作る(§2.7)。"""
    history: list[HistoryEntry] = []
    # 直近の提案(自分の提案・相手の提案)について、自分側の金庫が示した評価。
    # 断る手・断られた手の result は、これを使う。
    last_offer_result: Verdict | None = None

    for event in events:
        package = event.package
        if package is None:
            continue  # history に入れる種類は、どれも組み合わせを持つ(無効手などは持たないので除く)

        if event.kind == "check":
            history.append(_entry("self", "check", package, _recorded_verdict(event)))
        elif event.kind == "propose":
            last_offer_result = Verdict.ACCEPTABLE
            history.append(_entry("self", "propose", package, last_offer_result))
        elif event.kind == "offer_received":
            last_offer_result = _recorded_verdict(event)
            history.append(_entry("counterparty", "propose", package, last_offer_result))
        elif event.kind == "ask_principal":
            history.append(_entry("self", "ask_principal", package, Verdict.NEEDS_CONFIRMATION))
        elif event.kind == "reject" and last_offer_result is not None:
            history.append(_entry("self", "reject", package, last_offer_result))
        elif event.kind == "offer_rejected" and last_offer_result is not None:
            history.append(_entry("counterparty", "reject", package, last_offer_result))
    return history


def _entry(
    by: Literal["self", "counterparty"], move: MoveType, package: Package, result: Verdict
) -> HistoryEntry:
    return HistoryEntry(by=by, move=move, package=package, result=result)


def _recorded_verdict(event: EventViewItem) -> Verdict:
    """イベントに記録された自分側の評価(check・offer_received は必ず持つ)。"""
    if event.own_evaluation is None:
        raise ValueError(f"event kind={event.kind!r} has no recorded evaluation")
    return Verdict(event.own_evaluation)


def build_last_error(events: list[EventViewItem]) -> LastErrorReason | None:
    """直前の手が無効だったときの理由(なければ None。§2.7)。

    「手」の種類のイベントのうち一番新しいものが無効手なら、その理由。一時停止・再開・
    途中確認の回答などは、手ではないので飛ばして探す。
    """
    for event in reversed(events):
        if event.kind in _MOVE_LIKE_KINDS:
            return event.reason if event.kind == "invalid" else None  # 理由は金庫が付ける列挙値
    return None


def count_own_moves(events: list[EventViewItem]) -> int:
    """自分側が打った手の数(own_move_number。§2.7)。"""
    return sum(1 for event in events if event.kind in _OWN_MOVE_KINDS)


def build_turn_input(
    *, side: Side, view: NegotiationViewResponse, events: list[EventViewItem]
) -> TurnInput:
    """手番の側(side)の view とイベント列から、エージェントに渡す TurnInput を作る(§4.1 の 1)。

    view は side 側から見たもの、events はその側の見え方でなければならない(呼び出し側の責任)。
    """
    return TurnInput(
        schema="turn-input/v1",
        side=side,
        own_move_number=count_own_moves(events),
        counterparty=view.counterparty,
        history=build_history(events),
        pending_offer=view.pending_offer,
        last_check=view.last_check,
        last_error=build_last_error(events),
        budget=view.budget,
    )


def to_attacker_turn_input(turn_input: TurnInput, principal_instruction: str) -> AttackerTurnInput:
    """攻撃モードの求人エージェント向けに、TurnInput へ principal_instruction を足す(§2.7)。

    この型は /a2a/attacker だけが受け付ける(§4.3)。自由文は、この経路以外には入れない。
    """
    return AttackerTurnInput.model_validate(
        {**turn_input.model_dump(by_alias=True), "principal_instruction": principal_instruction}
    )
