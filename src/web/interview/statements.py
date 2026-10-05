"""面談の回答・発言を、アンカーに直す(design.md §2.3・§2.4・§5 の 4・5)。

変換の規則そのもの(§2.3 の補完・§2.4 の外した軸の扱い)は negotiation_core.statements にある。そこは外した軸を 1 つしか
受け取らないので、ここでは、複数の軸を外したとき(§5 の手順 3 は軸ごとに選べる)の扱いを足す。足りない関数はここに置き、
negotiation_core は変えない。

- 発言(PartialStatement): 外した軸のどれかに触れる発言は保存しない(§2.4)。どの軸にも触れない発言も保存しない(受けないアンカーなら
  「何があっても行かない」になってしまい、受けるアンカーなら何も言っていないのと同じなので)。
- 二択の回答: 外した軸があるとき、「行く」は外した軸を中立にした受けるアンカーとして保存し、「行かない」は保存しない(§2.4 の表)。
  「迷う」はアンカーにしない。
- 返すアンカーは、丸める前の生の値のまま(数値軸はグリッド外もあり得る)。丸め(§2.5)は、確認の後、送信のときに行う。

ConstraintList は LLM の出力の型(自由コメント・辞めた理由を発言単位に構造化したもの。§5 の 4・5)。応答スキーマは使わず JSON モードで
出させて(台帳 I-19)、この型で検証する。1 つの発言が検証に通らなくても、ほかの発言は活かす(通らなかった数を返す)。
"""

import re
from collections.abc import Collection, Mapping
from typing import Any

from pydantic import ValidationError

from negotiation_core import (
    AXES,
    AXIS_KEYS,
    CATEGORICAL_AXIS_KEYS,
    NUMERIC_AXIS_KEYS,
    PartialStatement,
    TwoChoiceResponse,
    apply_axis_removal,
    convert_statement_to_anchor,
    convert_two_choice_answer_to_anchor,
)
from negotiation_core.policy import StrictModel
from negotiation_core.vocabulary import AnchorType

_NUMBER_TEXT = re.compile(r"-?\d+(\.\d+)?")


class ConstraintList(StrictModel):
    """面談エージェントが、自由コメント・辞めた理由から取り出した発言の並び(§5 の 4・5)。"""

    statements: list[PartialStatement]


def coerce_numeric_fields(item: Any, keys: Collection[str]) -> Any:
    """LLM の出力の数値の項目が、整数や文字列(例: "650")で来ても、数値(小数)として読めるものは数値に戻す。それ以外はそのまま。"""
    if not isinstance(item, dict):
        return item
    fixed = dict(item)
    for key in keys:
        value = fixed.get(key)
        if isinstance(value, str) and _NUMBER_TEXT.fullmatch(value.strip()):
            fixed[key] = float(value.strip())
        elif isinstance(value, int) and not isinstance(value, bool):
            fixed[key] = float(value)
    return fixed


def known_fields_only(item: Any, keys: Collection[str]) -> Any:
    """LLM の出力のオブジェクトから、知っている項目だけを残す(余計な項目は、意味を持たないので捨てる)。オブジェクトでなければそのまま。"""
    return {key: value for key, value in item.items() if key in keys} if isinstance(item, dict) else item


def parse_constraint_list(payload: Any, *, max_statements: int) -> tuple[ConstraintList, int]:
    """LLM の出力(JSON を読んだもの)から、検証に通る発言を取り出す。返り値は (発言の並び, 通らずに捨てた発言の数)。

    形が違う(オブジェクトでない・statements が配列でない)ときは ValueError。max_statements を超える分は、捨てた数に数える。
    発言の中の知らない項目は無視する(LLM が説明を添えても、読み取りを捨てない。面談には、直させる手がかりを返す往復がない)。
    知っている項目の型・範囲・列挙が違う発言は、その 1 件だけを捨てる。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("statements"), list):
        raise ValueError("the constraint list is malformed")
    valid: list[PartialStatement] = []
    dropped = 0
    for index, item in enumerate(payload["statements"]):
        if index >= max_statements:
            dropped += 1
            continue
        try:
            valid.append(
                PartialStatement.model_validate(
                    coerce_numeric_fields(known_fields_only(item, ("polarity", *AXIS_KEYS)), NUMERIC_AXIS_KEYS)
                )
            )
        except ValidationError:
            dropped += 1
    return ConstraintList(statements=valid), dropped


def statement_skip_reason(statement: PartialStatement, removed_axes: Collection[str]) -> str | None:
    """発言を保存しない理由(保存するなら None)。no_axis: どの軸にも触れていない。removed_axis: 外した軸に触れている(§2.4)。"""
    touched = [axis for axis in AXIS_KEYS if getattr(statement, axis) is not None]
    if not touched:
        return "no_axis"
    if any(axis in removed_axes for axis in touched):
        return "removed_axis"
    return None


def statement_to_raw(statement: PartialStatement) -> tuple[AnchorType, dict[str, Any]]:
    """発言を、丸める前の生のアンカー(全軸の値)に直す。触れていない軸は §2.3 の規則で埋める。

    埋める規則は negotiation_core.convert_statement_to_anchor のとおり(その結果の、触れていない軸の値を使う)。
    触れた数値軸だけは、丸める前の生の値のままにする(確認画面で、本人の言葉のまま見せるため)。
    """
    filled = convert_statement_to_anchor(statement, "candidate")
    assert filled is not None  # removed_axis を渡していないので、None にならない
    raw: dict[str, Any] = {}
    for axis in AXIS_KEYS:
        stated = getattr(statement, axis)
        raw[axis] = stated if stated is not None and AXES[axis].kind == "numeric" else getattr(filled, axis)
    return statement.polarity, raw


def shown_values(values: Mapping[str, Any], removed_axes: Collection[str]) -> dict[str, Any]:
    """二択の選択肢として見せる値: 外した軸は、順序のある軸なら最も悪い値、順序のない区分軸なら「どちらでも(*)」(§2.4)。

    問うていない区分軸(None)も「どちらでも(*)」にする(§2.3 の最終行)。
    """
    shown = dict(values)
    for axis in removed_axes:
        shown = apply_axis_removal(shown, axis, "candidate")
    for axis in CATEGORICAL_AXIS_KEYS:
        if shown.get(axis) is None:
            shown[axis] = "*"
    return shown


def choice_not_saved_reason(response: TwoChoiceResponse, removed_axes: Collection[str]) -> str | None:
    """二択の回答を、外した軸のせいで保存しないときの理由(保存する・迷う〔もともとアンカーにしない〕なら None)。"""
    if removed_axes and response == "no_go":
        return "removed_axis"
    return None


def choice_to_raw(
    values: Mapping[str, Any], response: TwoChoiceResponse, removed_axes: Collection[str]
) -> tuple[AnchorType, dict[str, Any]] | None:
    """二択の回答を、アンカー(丸める前。二択の値はもともとグリッド上)に直す。保存しないときは None。

    values は、見せた値(shown_values)。「行く」は受けるアンカー、「行かない」は受けないアンカー、「迷う」は保存しない。
    外した軸があるときの「行かない」も保存しない(§2.4)。
    """
    if response == "undecided" or choice_not_saved_reason(response, removed_axes) is not None:
        return None
    converted = convert_two_choice_answer_to_anchor(dict(values), response, "candidate", None)
    assert converted is not None
    anchor, polarity = converted
    return polarity, anchor.model_dump()
