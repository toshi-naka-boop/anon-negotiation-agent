"""TurnInput の組み立て(design.md §2.7・§4.1 の 2・4)と、その側の見え方にすでにある評価の読み出し(§4.1 の 3)。

手番の側の金庫の view と、イベント列のその側の見え方だけから TurnInput を作る純粋な関数。
web はイベントを写さないので(§3.2)、履歴はこの関数に渡されたイベント列からだけ作る。
そのため、レフェリーを途中で止めて作り直しても、履歴に欠けも重複も生じない(DV-10)。
呼び出しの種類(phase)は、計画(plan。checked は空)か決定(decide。checked に、その手番で確かめた結果)。

作らないもの: view の version(手を登録するためだけに使い、LLM にも画面にも渡さない。§3.3)、
相手側の評価・確認結果・残り回数(view にもイベント列の見え方にもそもそも含まれない)。

設計書が決めていない読み方(§2.7)は、最も保守的な読み方で次のようにした。
- history の result は「その組み合わせについての、自分側の金庫の評価」で統一する。
  イベントに評価が記録されているもの(確認手・相手の提案の受領)はその値を使う。記録されて
  いないものは、金庫が有効な手として受け付けた事実から決まる値を使う: 自分の提案は
  「受けられる」(ガードを通ったため)、自分の途中確認は「本人確認が必要」(そうでなければ
  無効手になるため)、断る手・断られた手は、その元になった提案の評価。
- history に入れるのは「手」だけ。無効手(理由は last_error に、打とうとした手の中身は last_invalid に出る)、
  途中確認の回答(手ではない。評価し直した結果は pending_offer・last_check に出る)、一時停止・再開、
  最終記録は入れない。v14 では、金庫の check はレフェリーが計画の中で登録する(エージェントの手ではない)が、イベント列にあるので、
  history には従来どおり self・check として入る。
- last_invalid(台帳 C-40)は、直前の自分の手が無効だったときの、打とうとした手の種類・組み合わせ・自分側の評価。
  状態を持たないエージェントが、無効になった手を繰り返さないための情報で、last_error と同じ規則(「エージェントの手」の
  イベントのうち一番新しいものが無効手か)で決める。エージェントの手とは、check 以外の手のこと: レフェリーの確かめ(有効な
  check と、評価回数が尽きて無効になった check)は飛ばして探す(台帳 X-51。確かめが間に入っても、直前の無効手の手がかりが
  消えない)。自分側の見え方にだけ入るので、相手への漏れは増えない。
  金庫の無効手の見え方(EventViewItem の attempted_move・package・own_evaluation)をそのまま詰める。
  レフェリーが登録した無効手(schema_invalid・agent_timeout・output_truncated)は、金庫には何を打とうとしたか分からないので、
  3 つとも None になる(last_invalid 自体は入る)。
- own_move_number は、エージェント自身が出した手(提案・断る・途中確認・無効手)の数。レフェリーが計画の中で登録した確かめ
  (check の記録。有効なものと、評価回数が尽きて無効になったもの)は数えない(台帳 L15-2)。
  手数の上限に数えるもの(有効な check と有効な ask_principal を除く。台帳 C-38)とは別の、単純な数え方にした。
"""

from collections.abc import Sequence
from typing import Literal

from negotiation_core import (
    AXIS_KEYS,
    AttackerTurnInput,
    CheckedPackage,
    HistoryEntry,
    LastErrorReason,
    LastInvalid,
    MoveType,
    Package,
    Phase,
    Side,
    TurnInput,
    Verdict,
)

from vault.api_models import EventViewItem, NegotiationViewResponse

# エージェント自身の手のイベントの種類。レフェリーの確かめ(check)は、エージェントの手ではないので含めない(台帳 L15-2・X-51)。
# 評価回数が尽きて無効になった確かめは invalid として記録されるので、_is_referee_check で別に除く。
_OWN_MOVE_KINDS = frozenset({"propose", "reject", "ask_principal", "invalid"})
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


def _is_referee_check(event: EventViewItem) -> bool:
    """レフェリーの確かめ(金庫の check)か。有効な check と、評価回数が尽きて無効になった check(attempted_move=check)。"""
    return event.kind == "check" or (event.kind == "invalid" and event.attempted_move == "check")


def _latest_agent_move_event(events: list[EventViewItem]) -> EventViewItem | None:
    """「エージェントの手」の種類のイベントのうち一番新しいもの(なければ None)。

    一時停止・再開・途中確認の回答などは、手ではないので飛ばして探す。v14 では、金庫の check はレフェリーが計画の中で登録する
    確かめで、エージェントの手ではない(エージェントは check を出せない)ので、これも飛ばす: 確かめが間に入っても、直前の
    エージェントの手が無効だったことが消えない(台帳 X-51)。last_error と last_invalid が、同じ規則で「直前の手」を決めるための
    共通の部分。
    """
    for event in reversed(events):
        if event.kind in _MOVE_LIKE_KINDS and not _is_referee_check(event):
            return event
    return None


def build_last_error(events: list[EventViewItem]) -> LastErrorReason | None:
    """直前のエージェントの手が無効だったときの理由(なければ None。§2.7)。

    「エージェントの手」の種類のイベントのうち一番新しいものが無効手なら、その理由。一時停止・再開・
    途中確認の回答などは手ではないので、レフェリーの確かめ(check)はエージェントの手ではないので、飛ばして探す(台帳 X-51)。
    """
    event = _latest_agent_move_event(events)
    if event is None or event.kind != "invalid":
        return None
    return event.reason  # 理由は金庫が付ける列挙値


def build_last_invalid(events: list[EventViewItem]) -> LastInvalid | None:
    """直前のエージェントの手が無効だったときの、打とうとした手の中身(なければ None。台帳 C-40)。

    build_last_error と同じ規則: 「エージェントの手」の種類のイベントのうち一番新しいものが無効手なら、
    LastInvalid(move=打とうとした手の種類, package=その組み合わせ, evaluation=自分側の評価)。それ以外は None。
    金庫が自分側のポリシーで評価して無効にした手(提案のガードの失敗・途中確認の対象外など)は、組み合わせと評価を
    持つ。評価しなかった無効手(例: 提案がないのに accept)は、評価が None。レフェリーが登録した無効手
    (schema_invalid・agent_timeout・output_truncated)は、3 つとも None。
    """
    event = _latest_agent_move_event(events)
    if event is None or event.kind != "invalid":
        return None
    evaluation = Verdict(event.own_evaluation) if event.own_evaluation is not None else None
    return LastInvalid(move=event.attempted_move, package=event.package, evaluation=evaluation)


def count_own_moves(events: list[EventViewItem]) -> int:
    """エージェント自身が出した手の数(own_move_number。§2.7・台帳 L15-2)。無効手は数え、レフェリーの確かめは数えない。"""
    return sum(1 for event in events if event.kind in _OWN_MOVE_KINDS and not _is_referee_check(event))


def package_key(package: Package) -> tuple:
    """組み合わせを辞書のキーにするための値(Package は、pydantic のモデルで、そのままでは使えない)。"""
    return tuple(getattr(package, axis) for axis in AXIS_KEYS)


def known_evaluations(*, view: NegotiationViewResponse, events: list[EventViewItem]) -> dict[tuple, Verdict]:
    """その側の見え方にすでにある、組み合わせごとの自分側の評価(確かめを金庫に頼らずに埋める元。§4.1 の 3)。

    キーは package_key。見るのは、金庫の view の pending_offer・last_check と、イベント列(history の元)だけ。
    - 履歴(イベント列)の記録: 確かめ(check)と相手の提案の受領(offer_received)は、記録された評価。自分の提案(propose)は
      「受けられる」(ガードを通った)、自分の途中確認(ask_principal)は「本人確認が必要」(そうでなければ無効手になる)。
      同じ組み合わせの記録が複数あれば、新しい方。
    - 「受けられる」「受けられない」は、アンカーが足されるだけで矛盾は拒否される(§2.2・§4.4)ので、変わらない。「本人確認が必要」は、
      自分側の途中確認の回答でしか変わらない。そのため「本人確認が必要」の記録は、その記録より後に自分側の途中確認の回答
      (principal_answer)があるときは、含めない(確かめ直す。台帳 C-43・C-48)。なければ含める(一時停止・再開・再起動で重ねて消費しない)。
    - view の pending_offer・last_check は、回答のたびに評価し直されている(§4.4)ので、履歴より優先する(台帳 L14-2)。
    """
    recorded: dict[tuple, tuple[Verdict, int]] = {}  # キー → (評価, その記録のイベント列の位置)
    last_answer = -1
    for index, event in enumerate(events):
        if event.kind == "principal_answer":
            last_answer = index
            continue
        package = event.package
        if package is None:
            continue
        if event.kind in ("check", "offer_received"):
            verdict = _recorded_verdict(event)
        elif event.kind == "propose":
            verdict = Verdict.ACCEPTABLE
        elif event.kind == "ask_principal":
            verdict = Verdict.NEEDS_CONFIRMATION
        else:
            continue  # 断る手・無効手などは、組み合わせそのものの評価を新しくは持たない
        recorded[package_key(package)] = (verdict, index)

    known = {
        key: verdict
        for key, (verdict, index) in recorded.items()
        if not (verdict is Verdict.NEEDS_CONFIRMATION and index < last_answer)
    }
    for evaluated in (view.last_check, view.pending_offer):
        if evaluated is not None:
            known[package_key(evaluated.package)] = evaluated.own_evaluation
    return known


def build_turn_input(
    *,
    side: Side,
    view: NegotiationViewResponse,
    events: list[EventViewItem],
    phase: Phase,
    checked: Sequence[CheckedPackage] = (),
) -> TurnInput:
    """手番の側(side)の view とイベント列から、エージェントに渡す TurnInput を作る(§4.1 の 2・4)。

    view は side 側から見たもの、events はその側の見え方でなければならない(呼び出し側の責任)。phase は呼び出しの種類
    (plan・decide)。checked は、決定(decide)のときだけ、その手番で確かめた結果を、計画の順に渡す(plan では空)。
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
        last_invalid=build_last_invalid(events),
        budget=view.budget,
        phase=phase,
        checked=list(checked),
    )


def to_attacker_turn_input(turn_input: TurnInput, principal_instruction: str) -> AttackerTurnInput:
    """攻撃モードの求人エージェント向けに、TurnInput へ principal_instruction を足す(§2.7)。

    この型は /a2a/attacker だけが受け付ける(§4.3)。自由文は、この経路以外には入れない。
    """
    return AttackerTurnInput.model_validate(
        {**turn_input.model_dump(by_alias=True), "principal_instruction": principal_instruction}
    )
