"""A2A の線(wire)上の取り決めと、値の変換(サーバとクライアントで共有。design.md §4.3)。

- 受信口の名前とパス: `/a2a/{candidate,employer,attacker}`。
- 呼び出しの種類(`TurnInput.phase`): 計画(plan。出力は Plan)と決定(decide。出力は Move)。
- A2A(protobuf)の DataPart の `data` は `google.protobuf.Value` で、数値はすべて double になる。
  A2A を通ると `0` は `0.0` で届く。strict な pydantic スキーマ(negotiation_core.schema)は
  `int` に `0.0` を受け付けないので、整数として読める数値は int に戻してから使う。
"""

from typing import Any, Literal, get_args

from a2a.types import Part
from google.protobuf import struct_pb2

from negotiation_core.schema import Phase

Role = Literal["candidate", "employer", "attacker"]
ROLES: tuple[Role, ...] = get_args(Role)
PHASES: tuple[Phase, ...] = get_args(Phase)

# DataPart に付ける media type(Plan・Move・TurnInput は JSON)。
DATA_MEDIA_TYPE = "application/json"

# 一時的なエラー(LLM の 429・5xx、時間切れなど。再試行してよい失敗)を示すための、A2A エラーの
# data のキー。受信口は InternalError(-32603)にこの印を付けて返す(§4.3・§4.1)。
TRANSIENT_ERROR_KEY = "transient"

# 出力が `max_output_tokens` で切れた(finish_reason が MAX_TOKENS)ことを示す、A2A エラーの data のキー。受信口は
# InternalError(-32603)にこの印と、その呼び出しの usage を付けて返す(§4.3・台帳 C-53)。一時的ではない失敗なので、
# `transient` の印は付けない。
TRUNCATED_ERROR_KEY = "truncated"

# 応答の A2A タスクの artifact の metadata(と、切れた出力のエラーの data)で、その呼び出しの使用量(`Usage`)を載せるキー
# (§4.3・台帳 X-58)。artifact の metadata は、このキーだけを持つ。
USAGE_KEY = "usage"

# 受信口のパス(design.md §4.3 の表)。JSON-RPC の口が置かれる場所そのもの。
_ENDPOINT_PREFIX = "/a2a"


def endpoint_path(role: Role) -> str:
    """role の受信口のパス(例: `/a2a/candidate`)。"""
    return f"{_ENDPOINT_PREFIX}/{role}"


def value_to_python(value: struct_pb2.Value) -> Any:
    """`google.protobuf.Value` を、JSON 相当の Python の値に直す。

    整数として読める数値(`650.0` など)は int にする。それ以外の数値は float のまま返すので、
    strict なスキーマは小数を受け付けない。
    """
    kind = value.WhichOneof("kind")
    if kind == "struct_value":
        return struct_to_python(value.struct_value)
    if kind == "list_value":
        return [value_to_python(v) for v in value.list_value.values]
    if kind == "number_value":
        number = value.number_value
        return int(number) if number.is_integer() else number
    if kind == "string_value":
        return value.string_value
    if kind == "bool_value":
        return value.bool_value
    return None  # null_value、または未設定


def struct_to_python(struct: struct_pb2.Struct) -> dict[str, Any]:
    """`google.protobuf.Struct` を dict に直す(数値の扱いは value_to_python と同じ)。"""
    return {key: value_to_python(value) for key, value in struct.fields.items()}


def part_kind(part: Part) -> Literal["text", "raw", "url", "data"] | None:
    """Part の種類(v1 の A2A は、text・raw・url・data のどれか 1 つを持つ)。空なら None。"""
    return part.WhichOneof("content")  # type: ignore[return-value]
