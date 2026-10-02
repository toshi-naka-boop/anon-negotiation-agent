"""LLM の出力(`Plan`・`Move`)の受け取り方(design.md §2.7・§4.2・§4.3。台帳 I-19)。

計画(phase=plan)の出力は Plan、決定(phase=decide)の出力は Move。どちらも JSON モード(`response_mime_type` が
`application/json` だけで、応答スキーマは渡さない)で出させる。設計書 §2.7 は「グリッド値の列挙を埋め込んだスキーマで
構造化出力の段階でグリッド外の値を出させない」としていたが、本物の Vertex AI(`gemini-3.5-flash`、global)では、
応答スキーマで縛ると制約付きデコードが計画で 20〜55 秒、決定で 11〜33 秒かかり(思考 MEDIUM では 60 秒を超える)、
レフェリーの 1 回の上限(45 秒)を超えて手番が落ちた(2026-10-03 の実測。JSON モードなら 2〜9 秒)。

グリッド値・`check` という手がないこと・Plan.checks が最大 3 件・checks か move のどちらか、は指示文で伝え、
negotiation_core.schema の Plan・Move でレフェリーが検証する(違反は schema_invalid・off_grid の無効手。§4.1)。
JSON モードでも数値軸が文字列(例: "650")で返ることがあるので、受信口は restore_numeric_axes で整数に戻してから返す。
"""

import re
from typing import Any

from negotiation_core.vocabulary import NUMERIC_AXIS_KEYS

_INTEGER_TEXT = re.compile(r"-?\d+")


def _restore_package(package: Any) -> None:
    """package(dict)の数値軸の文字列を、整数に戻す(その場で書き換える)。dict でなければ何もしない。"""
    if isinstance(package, dict):
        for axis in NUMERIC_AXIS_KEYS:
            value = package.get(axis)
            if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
                package[axis] = int(value)


def restore_numeric_axes(output: dict[str, Any]) -> dict[str, Any]:
    """LLM の出力(Plan または Move)の package の数値軸を、文字列(例: "650")から整数に戻す(その場で書き換えて返す)。

    対象は `package` と、Plan の `checks` の各要素。整数として読める文字列だけを戻し、それ以外の値はそのまま残す
    (Plan・Move としての検証はレフェリーの仕事で、グリッド外や形の違反はそこで schema_invalid になる。§4.1)。
    """
    _restore_package(output.get("package"))
    checks = output.get("checks")
    if isinstance(checks, list):
        for package in checks:
            _restore_package(package)
    return output
