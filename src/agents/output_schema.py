"""LLM の構造化出力に渡す `Move` の JSON Schema(design.md §2.7 の最後の行・§4.2)。

軸ごとのグリッド値を列挙値(enum)にした Move のスキーマを LLM に渡し、構造化出力の段階で
グリッド外の値を出させない。スキーマの元は negotiation_core.schema.Move で、ここで別の
定義は持たない。

pydantic が作る JSON Schema をそのまま google-genai に渡すと失敗する(調査事項 R-3)。
google-genai の `Schema.enum` は文字列しか受け付けない。さらに Vertex AI は、enum を持つスキーマの型が
STRING でなければ 400 INVALID_ARGUMENT で断る(「For schema with enum values, schema type should be STRING」。
2026-10-02 の DV-15 の実機で確かめた。`type=INTEGER, format=enum` の形も断られた)。そこで数値軸(年収・
リモート日数など)の enum も、`type=STRING, format=enum, enum=[文字列にした値]` で渡し、LLM の出力の
数値軸は、返す前に restore_numeric_axes で整数に戻す。`const` は要素 1 つの enum にする。
`title`・`description` は LLM に余計な文を渡さないよう落とす。
"""

import copy
import re
from typing import Any

from google.genai import types

from negotiation_core.schema import Move
from negotiation_core.vocabulary import NUMERIC_AXIS_KEYS

_INTEGER_TEXT = re.compile(r"-?\d+")

# LLM に渡すスキーマに不要な、説明用の項目。
_DROPPED_KEYS = ("title", "description")


def _adapt_for_genai(node: dict[str, Any]) -> None:
    """JSON Schema の 1 つの節(dict)と、その下の節を、google-genai が受け付ける形に直す(その場で書き換える)。

    Move のスキーマに出てくる構造(object の properties、$defs、anyOf)だけをたどる。
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


def restore_numeric_axes(move: dict[str, Any]) -> dict[str, Any]:
    """LLM の出力の package の数値軸を、文字列の列挙値(例: "650")から整数に戻す(その場で書き換えて返す)。

    出力スキーマで数値軸を STRING の enum にしているための戻し。整数として読める文字列だけを戻し、それ以外の値は
    そのまま残す(Move としての検証はレフェリーの仕事で、グリッド外や形の違反はそこで schema_invalid になる。§4.1)。
    """
    package = move.get("package")
    if isinstance(package, dict):
        for axis in NUMERIC_AXIS_KEYS:
            value = package.get(axis)
            if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
                package[axis] = int(value)
    return move


def build_move_output_schema() -> types.Schema:
    """Move の出力スキーマ(各軸がグリッド値の enum)を、google-genai の Schema として返す。"""
    json_schema = copy.deepcopy(Move.model_json_schema())
    _adapt_for_genai(json_schema)
    return types.Schema.from_json_schema(
        json_schema=types.JSONSchema.model_validate(json_schema),
        api_option="VERTEX_AI",
    )
