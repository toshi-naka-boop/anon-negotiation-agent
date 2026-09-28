"""金庫が発行する ID(交渉 ID)の生成(design.md §2.7 の ID の扱いを流用)。

negotiation_core.schema の ID_PATTERN(システムが乱数で作る 16 桁の 16 進数)を
交渉 ID(nid)にもそのまま使う。同じ処理の重複を避けるため、生成器だけをここに置く。
"""

import secrets

from negotiation_core import ID_PATTERN

__all__ = ["ID_PATTERN", "generate_id"]


def generate_id() -> str:
    """16 桁の 16 進数の ID を作る(§2.7 と同じ形式)。"""
    return secrets.token_hex(8)
