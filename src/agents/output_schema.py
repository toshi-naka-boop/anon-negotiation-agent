"""LLM の構造化出力に渡す `Plan`(計画)・`Move`(決定)の JSON Schema(design.md §2.7・§4.2)。

軸ごとのグリッド値を列挙値(enum)にしたスキーマを LLM に渡し、構造化出力の段階でグリッド外の値を出させない。
スキーマの元は negotiation_core.schema の Plan・Move で、ここで別の定義は持たない。どちらも `check` という手を
含まない(確かめは Plan.checks でしか行えない。台帳 X-45)。Plan.checks は最大 3 件(maxItems)。

pydantic が作る JSON Schema をそのまま google-genai に渡すと失敗する(調査事項 R-3)。
google-genai の `Schema.enum` は文字列しか受け付けない。さらに Vertex AI は、enum を持つスキーマの型が
STRING でなければ 400 INVALID_ARGUMENT で断る(「For schema with enum values, schema type should be STRING」。
2026-10-02 の DV-15 の実機で確かめた。`type=INTEGER, format=enum` の形も断られた)。そこで数値軸(年収・
リモート日数など)の enum も、`type=STRING, format=enum, enum=[文字列にした値]` で渡し、LLM の出力の
数値軸は、返す前に restore_numeric_axes で整数に戻す。`const` は要素 1 つの enum にする。null を許す項目
(Plan.move・package など)は `nullable: true` になる(`oneOf` は使わない。R-9 の §5)。
`title`・`description` は LLM に余計な文を渡さないよう落とす。
"""

import copy
import re
from typing import Any

from google.genai import types
from pydantic import BaseModel

from negotiation_core.schema import Move, Phase, Plan
from negotiation_core.vocabulary import NUMERIC_AXIS_KEYS

_INTEGER_TEXT = re.compile(r"-?\d+")

# LLM に渡すスキーマに不要な、説明用の項目。
_DROPPED_KEYS = ("title", "description")


def _adapt_for_genai(node: dict[str, Any]) -> None:
    """JSON Schema の 1 つの節(dict)と、その下の節を、google-genai が受け付ける形に直す(その場で書き換える)。

    Plan・Move のスキーマに出てくる構造(object の properties、$defs、anyOf)だけをたどる。`$ref` の先(Package)は
    `$defs` の側で直すので、checks の要素のように `$ref` で参照される節も、直した形で展開される。
    """
    for key in _DROPPED_KEYS:
        node.pop(key, None)
    if "const" in node:
        node["enum"] = [node.pop("const")]
    if node.get("type") in ("integer", "number") and "enum" in node:
        node["type"] = "string"
        node["enum"] = [str(value) for value in node["enum"]]
        node["format"] = "enum"
    for children in (node.get("properties", {}).values(), node.get("$defs", {}).values(), node.get("anyOf", [])):
        for child in children:
            _adapt_for_genai(child)


def _restore_package(package: Any) -> None:
    """package(dict)の数値軸の文字列を、整数に戻す(その場で書き換える)。dict でなければ何もしない。"""
    if isinstance(package, dict):
        for axis in NUMERIC_AXIS_KEYS:
            value = package.get(axis)
            if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
                package[axis] = int(value)


def restore_numeric_axes(output: dict[str, Any]) -> dict[str, Any]:
    """LLM の出力(Plan または Move)の package の数値軸を、文字列の列挙値(例: "650")から整数に戻す(その場で書き換えて返す)。

    出力スキーマで数値軸を STRING の enum にしているための戻し。対象は `package` と、Plan の `checks` の各要素。
    整数として読める文字列だけを戻し、それ以外の値はそのまま残す(Plan・Move としての検証はレフェリーの仕事で、
    グリッド外や形の違反はそこで schema_invalid になる。§4.1)。
    """
    _restore_package(output.get("package"))
    checks = output.get("checks")
    if isinstance(checks, list):
        for package in checks:
            _restore_package(package)
    return output


def _build_output_schema(model: type[BaseModel]) -> types.Schema:
    """pydantic のモデルの JSON Schema から、google-genai の Schema を作る(各軸はグリッド値の enum)。"""
    json_schema = copy.deepcopy(model.model_json_schema())
    _adapt_for_genai(json_schema)
    return types.Schema.from_json_schema(
        json_schema=types.JSONSchema.model_validate(json_schema),
        api_option="VERTEX_AI",
    )


def build_plan_output_schema() -> types.Schema:
    """計画(phase=plan)の出力 Plan のスキーマ。checks は最大 3 件で、各要素・package の各軸がグリッド値の enum。"""
    return _build_output_schema(Plan)


def build_move_output_schema() -> types.Schema:
    """決定(phase=decide)の出力 Move のスキーマ(move に check はなく、package の各軸がグリッド値の enum)。"""
    return _build_output_schema(Move)


def build_output_schema(phase: Phase) -> types.Schema:
    """phase の出力スキーマ(plan は Plan、decide は Move。§4.2)。"""
    return build_plan_output_schema() if phase == "plan" else build_move_output_schema()
