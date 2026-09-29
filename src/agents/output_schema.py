"""LLM の構造化出力に渡す `Move` の JSON Schema(design.md §2.7 の最後の行・§4.2)。

軸ごとのグリッド値を列挙値(enum)にした Move のスキーマを LLM に渡し、構造化出力の段階で
グリッド外の値を出させない。スキーマの元は negotiation_core.schema.Move で、ここで別の
定義は持たない。

pydantic が作る JSON Schema をそのまま google-genai に渡すと失敗する(調査事項 R-3 の一部を
先に確かめた結果)。google-genai の `Schema.enum` は文字列しか受け付けず、数値軸(年収・
リモート日数など)の enum(整数)が検証エラーになるため。そこで数値の enum は、Gemini API の
表現に合わせて `type=INTEGER, format=enum, enum=[文字列にした値]` に直す。`const` は要素 1 つの
enum にする。`title`・`description` は LLM に余計な文を渡さないよう落とす。

Vertex AI の実機でこの表現が通るか(整数の enum を文字列の enum で表す形)は、R-3 で確かめる。
"""

import copy
from typing import Any

from google.genai import types

from negotiation_core.schema import Move

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
        node["enum"] = [str(value) for value in node["enum"]]
        node["format"] = "enum"
    for children in (node.get("properties", {}).values(), node.get("$defs", {}).values(), node.get("anyOf", [])):
        for child in children:
            _adapt_for_genai(child)


def build_move_output_schema() -> types.Schema:
    """Move の出力スキーマ(各軸がグリッド値の enum)を、google-genai の Schema として返す。"""
    json_schema = copy.deepcopy(Move.model_json_schema())
    _adapt_for_genai(json_schema)
    return types.Schema.from_json_schema(
        json_schema=types.JSONSchema.model_validate(json_schema),
        api_option="VERTEX_AI",
    )
