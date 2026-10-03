"""面談のアンカーの組み立て・確認・「最悪ここまで」・送信の形(design.md §5 の 4〜7・9、§2.2〜§2.5。FR-03・FR-04)。

面談の途中の状態(二択の回答と、発言)から、アンカーの一覧(AnchorEntry)を作る。一覧は毎回、状態から作り直す(外した軸が変わっても、
発言が外した軸に触れていないかを、そのつど確かめ直せる)。アンカーは、丸める前の生の値のまま持つ。丸め(§2.5)は、
- 「最悪ここまで」(丸めた後のマス)を見せるとき、
- 矛盾を調べるとき(金庫は丸めた後で矛盾を拒否する。§2.2)、
- 送信のとき(web.api_models.InterviewSubmitRequest が丸めて、金庫に置く丸め済みの値だけを作る)、
に行う。
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from negotiation_core import (
    AXES,
    AXIS_KEYS,
    CATEGORICAL_AXIS_KEYS,
    Anchor,
    CandidateAttributeBands,
    PartialStatement,
    TwoChoiceResponse,
    best_value,
    is_contradictory,
    round_anchor,
    worst_value,
)
from negotiation_core.vocabulary import AnchorType

from web.api_models import InterviewSubmitRequest, RawAnchor
from web.interview.sentences import categorical_phrase, numeric_boundary_phrase
from web.interview.statements import choice_not_saved_reason, choice_to_raw, statement_skip_reason, statement_to_raw

EntrySource = Literal["choice", "comment", "reason"]
KIND_LABEL: dict[str, str] = {"accept": "受ける条件", "reject": "受けない条件"}
REMOVED_AXIS_TEXT = "外しています(交渉中に確認)"


@dataclass(frozen=True)
class ChoiceAnswer:
    """二択の回答 1 つ。values は見せた値(shown_values。外した軸は中立の値)。"""

    values: dict[str, Any]
    response: TwoChoiceResponse


@dataclass(frozen=True)
class StatementRecord:
    """自由コメント・辞めた理由から取り出した発言 1 つ(構造化したもの。原文は持たない)。key は面談の中で一意。"""

    key: str
    source: Literal["comment", "reason"]
    statement: PartialStatement


@dataclass(frozen=True)
class AnchorEntry:
    """確認画面の 1 項目(アンカー 1 つ)。raw は全軸の値(数値軸は丸める前。区分軸は * もあり得る)。"""

    key: str
    source: EntrySource
    polarity: AnchorType
    raw: dict[str, Any]
    active: bool = True


@dataclass
class DerivedEntries:
    """状態から作った、アンカーの一覧と、保存しなかったもの。"""

    entries: list[AnchorEntry] = field(default_factory=list)
    # 外した軸のせいで保存しなかったもの(§2.4): (キー, 種類)。画面に「保存せず、交渉中に確認します」と示す
    not_saved: list[tuple[str, EntrySource]] = field(default_factory=list)
    # どの軸にも触れていないので捨てた発言の数
    ignored_statements: int = 0


def choice_key(pair_id: str, option: str) -> str:
    return f"choice-{pair_id}-{option}"


def derive_entries(
    answers: Mapping[tuple[str, str], ChoiceAnswer],
    statements: Sequence[StatementRecord],
    inactive: Collection[str],
    removed_axes: Collection[str],
) -> DerivedEntries:
    """二択の回答と発言から、アンカーの一覧を作る。inactive は、本人が消した項目のキー(付け直すまで、一覧に残して無効にする)。"""
    derived = DerivedEntries()
    for (pair_id, option), answer in answers.items():
        key = choice_key(pair_id, option)
        converted = choice_to_raw(answer.values, answer.response, removed_axes)
        if converted is None:
            if choice_not_saved_reason(answer.response, removed_axes) is not None:
                derived.not_saved.append((key, "choice"))
            continue
        derived.entries.append(AnchorEntry(key, "choice", converted[0], converted[1], key not in inactive))
    for record in statements:
        reason = statement_skip_reason(record.statement, removed_axes)
        if reason == "removed_axis":
            derived.not_saved.append((record.key, record.source))
            continue
        if reason is not None:
            derived.ignored_statements += 1
            continue
        polarity, raw = statement_to_raw(record.statement)
        derived.entries.append(AnchorEntry(record.key, record.source, polarity, raw, record.key not in inactive))
    return derived


def rounded_anchor(entry: AnchorEntry) -> Anchor:
    """項目を、共通グリッドに丸めたアンカーにする(§2.5)。受けるアンカーは良い側、受けないアンカーは悪い側へ。"""
    return round_anchor(entry.raw, entry.polarity, "candidate")


def active_entries(entries: Sequence[AnchorEntry]) -> list[AnchorEntry]:
    return [entry for entry in entries if entry.active]


def count_by_polarity(entries: Sequence[AnchorEntry]) -> dict[str, int]:
    """有効な項目の数(受ける・受けない)。"""
    active = active_entries(entries)
    return {
        "accept": sum(1 for entry in active if entry.polarity == "accept"),
        "reject": sum(1 for entry in active if entry.polarity == "reject"),
    }


def find_conflicts(entries: Sequence[AnchorEntry]) -> list[tuple[str, str]]:
    """有効な項目の中の、矛盾する (受けるアンカーのキー, 受けないアンカーのキー) の組(丸めた後で調べる。§2.2・§2.5)。"""
    active = active_entries(entries)
    accepts = [(entry.key, rounded_anchor(entry)) for entry in active if entry.polarity == "accept"]
    rejects = [(entry.key, rounded_anchor(entry)) for entry in active if entry.polarity == "reject"]
    return [
        (accept_key, reject_key)
        for accept_key, accept in accepts
        for reject_key, reject in rejects
        if is_contradictory(accept, reject, "candidate")
    ]


def to_submit_request(
    entries: Sequence[AnchorEntry], removed_axes: Collection[str], bands: CandidateAttributeBands
) -> InterviewSubmitRequest:
    """有効な項目から、既存の送信 API(web.api_models.InterviewSubmitRequest)の入力を作る。丸めは、その to_put_policy_request が行う。"""
    active = active_entries(entries)
    return InterviewSubmitRequest(
        accept_anchors=[RawAnchor(**entry.raw) for entry in active if entry.polarity == "accept"],
        reject_anchors=[RawAnchor(**entry.raw) for entry in active if entry.polarity == "reject"],
        removed_axes=[axis for axis in AXIS_KEYS if axis in removed_axes],
        attribute_bands=bands,
    )


# --- 「最悪ここまで」(§5 の 7。FR-04) ---


def _salary_cell(polarity: AnchorType, value: float) -> dict[str, Any]:
    """年収の、丸めた後のマス(外の人が知り得るのは、境目がどのマスにあるか、まで。§2.5)。

    受けるアンカー(良い側 = 上へ丸める): 元の値は、1 つ下のグリッド点より上、この点以下。
    受けないアンカー(悪い側 = 下へ丸める): 元の値は、この点以上、1 つ上のグリッド点より下。
    """
    grid = AXES["salary"].grid
    index = grid.index(value)
    if polarity == "accept":
        low = grid[index - 1] if index > 0 else None
        return {
            "low": low,
            "low_inclusive": low is None,
            "high": value,
            "high_inclusive": True,
            "text": f"『受ける』の下限は、{low} 万円より上、{value} 万円以下のどこか"
            if low is not None
            else f"『受ける』の下限は、{value} 万円",
        }
    high = grid[index + 1] if index + 1 < len(grid) else None
    return {
        "low": value,
        "low_inclusive": True,
        "high": high,
        "high_inclusive": high is None,
        "text": f"『受けない』の上限は、{value} 万円以上、{high} 万円未満のどこか"
        if high is not None
        else f"『受けない』の上限は、{value} 万円",
    }


def worst_case_view(entries: Sequence[AnchorEntry], removed_axes: Collection[str]) -> list[dict[str, Any]]:
    """軸ごとの「最悪ここまで」: 丸めた後のマスを見せる(§5 の 7)。外した軸は「外しています(交渉中に確認)」。

    年収は、マス(区間)の中のどこかまでしか外から分からない。離散軸(年収以外)は、丸めても情報が減らないので、境目の値そのもの。
    何も制約していない値(受けるアンカーの最も悪い値、受けないアンカーの最も良い値、区分軸の * )は、外の人に何も知らせないので、略す。
    """
    rounded = [(entry.polarity, rounded_anchor(entry)) for entry in active_entries(entries)]
    view: list[dict[str, Any]] = []
    for axis in AXIS_KEYS:
        spec = AXES[axis]
        item: dict[str, Any] = {
            "axis": axis,
            "label": spec.label,
            "discrete": spec.discrete,
            "removed": axis in removed_axes,
            "cells": [],
        }
        if item["removed"]:
            item["text"] = REMOVED_AXIS_TEXT
            view.append(item)
            continue
        seen: set[tuple[str, Any]] = set()
        for polarity, anchor in rounded:
            value = getattr(anchor, axis)
            if (polarity, value) in seen:
                continue
            seen.add((polarity, value))
            cell: dict[str, Any] = {"kind": polarity, "kind_label": KIND_LABEL[polarity]}
            if axis in CATEGORICAL_AXIS_KEYS:
                if value == "*":
                    continue
                cell["text"] = categorical_phrase(axis, value)
            else:
                neutral = worst_value(axis, "candidate") if polarity == "accept" else best_value(axis, "candidate")
                if value == neutral:
                    continue
                if axis == "salary":
                    cell.update(_salary_cell(polarity, value))
                else:
                    cell["text"] = numeric_boundary_phrase(axis, value, polarity)
            item["cells"].append(cell)
        item["note"] = (
            "丸めても情報が減りません(値そのものが、外から分かります)"
            if spec.discrete
            else "50 万円刻みのマスに丸めます。マスの中のどこかは、外からは分かりません"
        )
        view.append(item)
    return view
